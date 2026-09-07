"""Watchdog de conexões — avisa quando WhatsApp ou ML cai.

Roda no processo `monitor` do Procfile (`manage.py monitorar`). Compara o estado
atual de cada conexão com o último estado salvo no Perfil; em transição registra um
EventoOperacional (pipeline "conexao", visível em /painel-admin/saude) e manda e-mail
ao usuário, com cooldown p/ não floodar enquanto seguir caído.

O estado atual vem de `conexoes.py` — a fonte única que as telas também leem. Este
módulo não decide mais o que é "conectado"; ele só detecta TRANSIÇÃO e reage. Era
justamente por decidir por conta própria que ele divergia do dashboard.

O evento e o e-mail são independentes de propósito: o e-mail depende de SMTP
configurado e é para o usuário; o evento é nosso e precisa existir mesmo quando o
e-mail não sai — foi assim que quedas passaram meses invisíveis.

Hoje WhatsApp/ML são globais (single-tenant em transição). As funções já recebem o
usuário p/ quando a Fase 3 isolar conexão por usuário (sessão WA + auth_{id}.json).
"""
import logging

from django.utils import timezone


logger = logging.getLogger(__name__)


def ml_conectado(user=None) -> bool:
    """True se o ML ainda aceita a sessão salva. Wrapper sobre conexoes.estado_ml.

    Mantido pela assinatura: relatorios.py e automacao.py já dependiam dele. A
    regra real (sonda de sessão, antes era a idade do arquivo) mora em conexoes.py.
    """
    from apps.scrapers.conexoes import estado_ml
    return estado_ml(user).conectado


def wa_conectado(session=None) -> bool:
    """True se o worker reporta o WhatsApp pareado. Wrapper sobre conexoes.estado_whatsapp."""
    from apps.scrapers.conexoes import estado_whatsapp
    return estado_whatsapp(session=session).conectado


def verificar_e_notificar() -> dict:
    """Checa todos os perfis verificados e dispara alertas. Retorna contadores."""
    from datetime import timedelta
    from django.conf import settings
    from apps.accounts.models import Membership, Perfil, WhatsAppConnection
    from apps.accounts.emails import enviar_alerta_conexao
    from apps.scrapers.conexoes import estado_amazon_relatorios, estado_ml, estado_whatsapp

    agora = timezone.now()
    cooldown = timedelta(hours=getattr(settings, "ALERTA_CONEXAO_COOLDOWN_H", 6))
    enviados = 0
    checados = 0

    perfis = list(Perfil.objects.select_related("user")
                  .filter(user__is_active=True, email_verificado=True)
                  .exclude(user__email=""))
    perfis_por_usuario = {perfil.user_id: perfil for perfil in perfis}
    # WhatsApp e sessão ML pertencem à organização. Uma verificação por perfil
    # duplicava sonda, evento e e-mail quando dois usuários compartilhavam o tenant.
    for connection in WhatsAppConnection.objects.select_related("organization").filter(
            organization__status="active"):
        member_ids = list(Membership.objects.filter(
            organization=connection.organization, is_active=True,
            user__is_active=True,
        ).order_by("role", "created_at").values_list("user_id", flat=True))
        perfil = next((perfis_por_usuario.get(user_id) for user_id in member_ids
                       if user_id in perfis_por_usuario), None)
        if perfil is None:
            continue
        checados += 1
        # Estado rico (não bool): o motivo entra no evento, e é ele que a Saúde
        # mostra. "WhatsApp caiu" sem dizer se foi o pareamento ou o serviço fora
        # do ar não é acionável.
        wa = estado_whatsapp(perfil.user, session=connection.instance_id)
        ml = estado_ml(perfil.user)
        enviados += _processar(perfil, "WhatsApp", "wa", wa, agora, cooldown,
                               enviar_alerta_conexao)
        enviados += _processar(perfil, "Mercado Livre", "ml", ml, agora, cooldown,
                               enviar_alerta_conexao)
        # Reconcilia DB ↔ manifest ↔ runtime com capability amarrada exatamente à
        # organização/sessão. Falha de rede não altera a propriedade no banco.
        try:
            from apps.scrapers.whatsapp_client import reconciliar_sessao
            reconciliar_sessao(connection.instance_id)
        except Exception as exc:
            logger.warning(
                "Reconciliação WhatsApp indisponível para a instância %s: %s",
                connection.instance_id, type(exc).__name__,
            )

    # Relatórios Amazon continuam por usuário: a sessão desse portal não é a
    # conexão compartilhada do WhatsApp/ML.
    for perfil in perfis:
        amazon = estado_amazon_relatorios(perfil.user)
        # Só alertamos quem já usa Amazon: uma conta sem tag não pediu integração.
        if perfil.amazon_conectado():
            enviados += _processar(perfil, "Amazon Relatórios", "amazon_relatorio", amazon,
                                   agora, cooldown, enviar_alerta_conexao)
    return {"checados": checados, "alertas_enviados": enviados}


def _processar(perfil, nome_servico, campo, estado, agora, cooldown, enviar) -> int:
    """Compara estado atual vs salvo; alerta em transição (com cooldown). 1 se enviou e-mail."""
    from apps.scrapers.eventos import log_event

    # Fase transitória (worker religando após deploy/restart) não é queda: alarmar
    # aqui mandava "WhatsApp caiu" a cada deploy do spreading-wa. Também não grava
    # o estado no perfil — se a reativação falhar, a próxima checagem ainda vê a
    # transição e alerta como primeira vez.
    if estado.detalhe == "conectando":
        return 0

    estado_attr = f"{campo}_estado"
    alerta_attr = f"alerta_{campo}_em"
    anterior = getattr(perfil, estado_attr)        # True | False | None (nunca checado)
    ultimo_alerta = getattr(perfil, alerta_attr)
    conectado = estado.conectado
    enviou = 0

    if not conectado:
        # Nunca esteve de pé ≠ caiu. Uma integração que o usuário simplesmente
        # não conectou (`sem_sessao`, sem histórico de sucesso) não é incidente:
        # é passo de configuração pendente. Alarmar isso como erro enchia o
        # relatório e o Sentry com "Amazon Relatórios de fulano está fora do ar"
        # para um portal que ninguém jamais ligou — inclusive de contas de teste.
        # Continua virando evento, para a tela de Saúde poder mostrar o que falta,
        # mas em nível de aviso e sem e-mail.
        nunca_conectou = anterior is None and estado.detalhe == "sem_sessao"
        if nunca_conectou:
            setattr(perfil, estado_attr, False)
            perfil.save(update_fields=[estado_attr])
            log_event(
                "conexao", "conexao_ausente",
                f"{nome_servico} de {perfil.user.get_username()} nunca foi conectado: "
                f"{estado.motivo or 'falta configurar'}",
                level="warning", usuario=perfil.user,
                contexto={"servico": nome_servico, "motivo": estado.motivo,
                          "detalhe": estado.detalhe,
                          "availability_code": estado.availability_code},
            )
            return 0
        primeira_vez = anterior is not False        # True ou None -> acabou de cair
        cooldown_ok = ultimo_alerta is None or (agora - ultimo_alerta) >= cooldown
        if primeira_vez or cooldown_ok:
            # O carimbo marca a TENTATIVA, não o sucesso do e-mail. Antes só era gravado
            # quando o envio dava certo, e com SMTP quebrado ele ficava None para sempre:
            # o cooldown nunca fechava e o alerta era retentado a cada tick (5min). Isso
            # passava despercebido porque ninguém contava e-mail que não sai — mas agora
            # cada tentativa gera evento, e o relatório afogaria em 288 linhas/dia por
            # usuário caído. Retentar SMTP quebrado de 5 em 5 min também nunca ajudou.
            setattr(perfil, alerta_attr, agora)
            # Evento independente do e-mail: o alerta depende de SMTP configurado, o
            # relatório não pode depender. Cai no mesmo cooldown, então uma conexão
            # cronicamente fora gera ~4 eventos/dia, não 288.
            log_event(
                "conexao", "conexao_caiu",
                f"{nome_servico} de {perfil.user.get_username()} está fora do ar: "
                f"{estado.motivo or 'motivo não informado'}",
                level="error", usuario=perfil.user,
                contexto={"servico": nome_servico, "repique": not primeira_vez,
                          "motivo": estado.motivo, "detalhe": estado.detalhe,
                          "availability_code": estado.availability_code,
                          "fonte": estado.fonte},
            )
            if enviar(perfil.user, nome_servico, caiu=True):
                enviou = 1
    else:
        if anterior is False:                       # estava caído -> reconectou
            log_event(
                "conexao", "conexao_voltou",
                f"{nome_servico} de {perfil.user.get_username()} reconectou.",
                usuario=perfil.user, contexto={"servico": nome_servico},
            )
            if enviar(perfil.user, nome_servico, caiu=False):
                enviou = 1

    setattr(perfil, estado_attr, conectado)
    perfil.save(update_fields=[estado_attr, alerta_attr])
    return enviou

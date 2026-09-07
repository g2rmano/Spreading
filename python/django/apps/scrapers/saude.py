"""Relatório de saúde legível — o que quebrou nas últimas horas e o que fazer.

Existe para inverter a origem do diagnóstico: antes, quem descobria que o produto
tinha parado era a cliente testando na mão e avisando a gente. Os eventos já eram
gravados (EventoOperacional), mas ninguém lê uma lista crua de 30 linhas todo dia.

Duas decisões dão a forma deste módulo:

1. AGRUPAR, não listar. 40 falhas do mesmo grupo apagado são UM problema, não 40
   linhas. A tela mostra ocorrências agregadas por (pipeline, evento) com contagem,
   todas as contas afetadas e quando aconteceu por último.

2. TRADUZIR, não despejar. Cada evento tem uma entrada no CATALOGO com o que
   significa e o que fazer. "send_failed x12" não diz nada às 8h da manhã; "as
   ofertas não estão saindo para o grupo X — o WhatsApp dele caiu" diz.

O silêncio também é resposta: `sinais` conta os sucessos do período. Zero erro com
zero envio não é saúde, é worker desligado — e é isso que a seção de workers mostra.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count, Max
from django.utils import timezone

from apps.scrapers import automacao_state as st
from apps.scrapers.models import EventoOperacional, IncidenteSaude

logger = logging.getLogger(__name__)


# Cada entrada responde às duas únicas perguntas que importam num relatório diário:
# "isso é grave?" e "o que eu faço?". Evento sem entrada aqui ainda aparece na tela
# (com fallback), então esquecer de catalogar degrada a leitura, não esconde o erro.
CATALOGO = {
    # ── Publicação: o produto em si ──
    "config_pausada": {
        "titulo": "Automação pausada sozinha",
        "significa": "A regra de envio bateu o limite de falhas seguidas e se desligou. "
                     "O usuário parou de receber ofertas e não foi avisado.",
        "acao": "Veja o motivo abaixo. Resolvido, reative a regra na tela de Envios do usuário.",
    },
    "send_failed": {
        "titulo": "Oferta não foi entregue",
        "significa": "Uma publicação falhou. Falhas transitórias (worker piscou, timeout) "
                     "não contam contra a regra; as permanentes pausam a automação.",
        "acao": "Se repete no mesmo destino, confira se o grupo ainda existe e se o "
                "WhatsApp do dono está conectado.",
    },
    "publicacao_falhou": {
        "titulo": "Oferta não foi entregue",
        "significa": "Uma publicação falhou antes de uma causa mais específica ser identificada.",
        "acao": "Confira o detalhe técnico, corrija a origem e execute um novo teste seguro.",
    },
    # Causa gerada por incidentes_saude.causa_do_evento a partir de send_timeout.
    # Sem entrada aqui ela renderizava com o nome cru na tela: o mapa de compat em
    # _incidentes preenche a chave `evento`, mas quem busca a tradução é
    # descrever(causa), que não passa por ele.
    "whatsapp_timeout_entrega": {
        "titulo": "Serviço WhatsApp não respondeu a tempo",
        "significa": "O transporte do WhatsApp ficou indisponível ou não confirmou a "
                     "mensagem dentro do prazo. O resultado pode ser incerto para evitar "
                     "duplicar uma oferta no grupo.",
        "acao": "Confirme a mensagem no grupo antes de reenviar. Se repetir para várias "
                 "contas, investigue a máquina spreading-wa e o Chromium.",
    },
    "send_timeout": {
        "titulo": "Serviço WhatsApp não respondeu a tempo",
        "significa": "O transporte do WhatsApp ficou indisponível ou não confirmou a "
                     "mensagem dentro do prazo. O resultado pode ser incerto para evitar "
                     "duplicar uma oferta no grupo.",
        "acao": "Confirme a mensagem no grupo antes de reenviar. Se repetir para várias "
                 "contas, investigue a máquina spreading-wa e o Chromium.",
    },
    "whatsapp_preflight_timeout": {
        "titulo": "WhatsApp travou antes do envio",
        "significa": "O WhatsApp Web não respondeu ao teste de conexão. Nenhuma promoção foi publicada nessa tentativa.",
        "acao": "A sessão é recuperada automaticamente. Use “Retestar” para validar sessão e grupo sem enviar mensagem.",
    },
    "whatsapp_grupo_timeout": {
        "titulo": "WhatsApp travou ao validar o grupo",
        "significa": "A sessão estava viva, mas não respondeu ao conferir o destino antes do envio.",
        "acao": "A sessão é recuperada automaticamente. Reteste o grupo antes de tentar publicar de novo.",
    },
    "whatsapp_store_recarregado": {
        "titulo": "WhatsApp Web perdeu os módulos internos",
        "significa": "O WhatsApp Web recarregou e a sessão perdeu a referência interna "
                     "antes do envio; nenhuma mensagem saiu nessa tentativa.",
        "acao": "A sessão é reciclada automaticamente. Aguarde alguns segundos e reteste; "
                "a próxima tentativa deve enviar normalmente.",
    },
    "whatsapp_frame_recarregado": {
        "titulo": "WhatsApp Web estava recarregando",
        "significa": "A página do WhatsApp foi trocada durante a preparação do envio; não houve repetição automática.",
        "acao": "Aguarde a recuperação e use o reteste seguro antes de uma nova publicação.",
    },
    "whatsapp_confirmacao": {
        "titulo": "Confirmação de envio inconsistente",
        "significa": "O WhatsApp aceitou ou iniciou a mensagem, mas não devolveu confirmação confiável.",
        "acao": "Confira o grupo antes de reenviar; o sistema não repete mensagens ambíguas.",
    },
    "whatsapp_erro_minificado": {
        "titulo": "WhatsApp devolveu erro interno",
        "significa": "O WhatsApp Web devolveu uma falha sem detalhe útil; o erro foi separado para investigação.",
        "acao": "Reteste a sessão e o grupo. Se voltar a ocorrer, revise a versão do WhatsApp Web/Chromium.",
    },
    "link_afiliado_recusado": {
        "titulo": "Link de afiliado recusado",
        "significa": "O marketplace não gerou um link com atribuição para a promoção.",
        "acao": "Reteste o produto sem publicar. Se falhar, reconecte o marketplace ou descarte a oferta.",
    },
    "link_reprovado": {
        "titulo": "Link da oferta reprovado",
        "significa": "A verificação não confirmou que o link ainda representa a promoção esperada.",
        "acao": "Reteste sem publicar; mantenha a oferta fora dos grupos enquanto não for aprovada.",
    },
    "cupons_vazios": {
        "titulo": "A raspagem não trouxe nenhum cupom",
        "significa": "Vieram ofertas, mas zero cupons. Quase sempre o ML mudou o "
                     "formato da página de cupons e o parser parou de reconhecer.",
        "acao": "Abra /ofertas/cupons e /cupons/filter no navegador e compare com os "
                 "seletores dos scrapers de cupom. O catálogo anterior foi preservado.",
    },
    "cupons_campanha_erro": {
        "titulo": "Cupons de campanha não foram raspados",
        "significa": "A leitura de /cupons/filter falhou. Sem a tabela de cupons, "
                     "produto com campanha não gera link de afiliado e fica pendente.",
        "acao": "Veja o detalhe técnico: se citar __NORDIC_RENDERING_CTX__, o ML "
                 "renomeou o bundle e o parser precisa ser ajustado.",
    },
    "links_sem_sessao": {
        "titulo": "Conta sem sessão do Mercado Livre não gera links",
        "significa": "O worker pulou esta conta: sem o Mercado Livre conectado não dá "
                     "para abrir o Link Builder, então as ofertas dela ficam pendentes.",
        "acao": "Peça para o usuário reconectar em Conexão Mercado Livre. O reteste "
                 "confirma assim que a sessão voltar.",
    },
    "links_ciclo_erro": {
        "titulo": "Worker de links de afiliado falhou",
        "significa": "O ciclo inteiro de geração de links parou antes de concluir.",
        "acao": "Confira o worker de links e a sessão Mercado Livre; um novo lote bem-sucedido confirma o ajuste.",
    },
    "tick_erro": {
        "titulo": "Ciclo de envio quebrou",
        "significa": "O worker de envio falhou no ciclo inteiro — nenhum usuário recebeu "
                     "oferta nessa rodada.",
        "acao": "Erro nosso, não do usuário. Veja o traceback e corrija no código.",
    },
    # ── Conexão: a causa raiz mais comum ──
    "conexao_caiu": {
        "titulo": "Conexão caiu",
        "significa": "O WhatsApp ou o Mercado Livre do usuário saiu do ar. Enquanto isso, "
                     "as ofertas dele não saem.",
        "acao": "O usuário precisa reconectar (parear o WhatsApp de novo ou refazer o "
                "login do ML). Cheque se o alerta por e-mail chegou nele.",
    },
    "conexao_voltou": {
        "titulo": "Conexão restabelecida",
        "significa": "A conexão voltou sozinha ou o usuário reconectou. Não é problema — "
                     "aparece para você ver quanto tempo ficou fora.",
        "acao": "Nenhuma.",
    },
    "watchdog_erro": {
        "titulo": "O monitor de conexões falhou",
        "significa": "Grave: é o watchdog que detecta queda de conexão. Com ele quebrado, "
                     "o sistema fica cego justamente para o que mais importa.",
        "acao": "Corrija com prioridade — enquanto isso, este relatório subestima as quedas.",
    },
    # ── Scraper: envenena o catálogo devagar ──
    "scrape_erro": {
        "titulo": "Ciclo de raspagem quebrou",
        "significa": "A coleta de ofertas falhou por inteiro. O catálogo para de receber "
                     "novidades e as ofertas enviadas vão ficando velhas.",
        "acao": "Veja o traceback. Normalmente é seletor que mudou ou bloqueio do site.",
    },
    "fonte_falhou": {
        "titulo": "Uma loja parou de responder",
        "significa": "Só uma fonte quebrou; as outras seguiram. O ciclo não falha, então "
                     "isso passa despercebido enquanto o catálogo daquela loja envelhece.",
        "acao": "Se repetir por dias, o seletor daquela loja provavelmente mudou.",
    },
    "flash_erro": {
        "titulo": "Feed rápido quebrou",
        "significa": "A lane de feed rápido do ML falhou. Impacto menor: a raspagem "
                     "completa ainda alimenta o catálogo.",
        "acao": "Se for isolado, ignore. Se repetir, investigue junto com o scrape.",
    },
    "links_erro": {
        "titulo": "Links de afiliado não foram gerados",
        "significa": "O lote de links do Mercado Livre falhou total ou parcialmente; "
                     "as ofertas podem continuar pendentes para aquele usuário.",
        "acao": "Veja o detalhe técnico. Se indicar contexto assíncrono ou navegador, "
                 "corrija o worker; se indicar login, peça para reconectar o Mercado Livre.",
    },
    # ── Onboarding: o usuário nem entra ──
    "verificacao_nao_enviada": {
        "titulo": "Conta nova travada na porta",
        "significa": "O usuário se cadastrou mas o e-mail de verificação não saiu. O "
                     "middleware barra quem não verificou, então ele não consegue usar nada.",
        "acao": "Grave. Confira o SMTP (secrets EMAIL_*) e reenvie a verificação.",
    },
    "signup": {
        "titulo": "Conta criada",
        "significa": "Cadastro novo no sistema.",
        "acao": "Nenhuma.",
    },
    # ── Sistema ──
    "email_falhou": {
        "titulo": "E-mail não foi entregue",
        "significa": "O envio de e-mail falhou. Atinge verificação de conta, boas-vindas e "
                     "alerta de conexão — todos silenciosos por natureza.",
        "acao": "Cheque os secrets EMAIL_HOST_USER / EMAIL_HOST_PASSWORD no Fly.",
    },
    "sse_throttled": {
        "titulo": "Usuário limitado por excesso de execuções",
        "significa": "Alguém disparou um endpoint pesado várias vezes por minuto e foi "
                     "barrado. Proteção funcionando.",
        "acao": "Nenhuma, salvo se repetir muito — aí pode ser botão que redispara sozinho.",
    },
    # ── Relatórios de comissão ──
    "sync_failed": {
        "titulo": "Sincronização de comissão falhou",
        "significa": "Não foi possível buscar a receita de afiliado do marketplace. O "
                     "dashboard de ganhos fica desatualizado.",
        "acao": "Veja o traceback; costuma ser sessão do marketplace expirada.",
    },
    "sync_action_required": {
        "titulo": "Conta precisa ser reconectada",
        "significa": "O marketplace pediu login de novo para liberar o relatório de comissão.",
        "acao": "Só o usuário resolve: peça para ele reconectar a conta.",
    },
}

_FALLBACK = {
    "titulo": "",
    "significa": "Evento ainda não catalogado em saude.CATALOGO.",
    "acao": "Veja a mensagem e o traceback abaixo.",
}

# Sucessos que provam que o sistema está vivo. Sem isto, "nenhum erro" é ambíguo:
# pode ser tudo funcionando ou tudo parado.
SINAIS = (
    ("send_ok", "Ofertas publicadas"),
    ("sync_ok", "Relatórios sincronizados"),
    ("signup", "Contas criadas"),
    ("conexao_voltou", "Reconexões"),
)

# Nomes de worker legíveis (JOBS do automacao_state + o que cada um faz).
# `controle` é a flag que liga/desliga o job — None = sempre ligado, sem flag.
WORKERS = (
    ("scrape", "scrape", "Raspagem de ofertas"),
    ("scrape_rapido", "scrape", "Feed rápido de ofertas"),
    # "links", não "scrape": a lane roda com `is_enabled("links")`, e essa
    # função já herda de `scrape` enquanto a lane não recebe escolha própria.
    # Ler a flag de `scrape` aqui fazia a Saúde divergir da realidade assim que
    # alguém configurava Links — e o alarme "ligado mas morto" passava a ser
    # calculado contra a flag errada.
    ("links", "links", "Links de afiliado"),
    # O pipeline de cupons mantém a janela de preparo mesmo com a raspagem geral
    # desligada, portanto sua saúde não pode herdar a flag de ``scrape``.
    ("cupons", None, "Manutenção de cupons"),
    ("envio", "envio", "Envio de ofertas"),
    # Sem flag: relink de canais é ligado por secret de política, não pela tela.
    ("canais", None, "Canais monitorados"),
    ("relatorios", "relatorios", "Relatórios de comissão"),
    # Sem flag de propósito: monitorar conexão não pode depender de a automação
    # estar ligada. Se este morrer, o sistema fica cego para quedas.
    ("monitor", None, "Monitor de conexões"),
)


def descrever(evento: str) -> dict:
    """Tradução humana de um evento. Nunca levanta: evento novo cai no fallback."""
    info = CATALOGO.get(evento) or _FALLBACK
    return {**_FALLBACK, **info, "titulo": info.get("titulo") or evento}


def _problemas(qs) -> list[dict]:
    """Erros e avisos agrupados por (pipeline, evento), mais grave e mais frequente no topo."""
    grupos = (
        qs.filter(level__in=("error", "warning"))
        .values("pipeline", "evento", "level")
        .annotate(n=Count("id"), ultimo=Max("criado_em"),
                  usuarios=Count("usuario", distinct=True))
    )
    out = []
    for g in grupos:
        base = qs.filter(pipeline=g["pipeline"], evento=g["evento"], level=g["level"])

        def _ultimo(**extra):
            return base.filter(**extra).select_related("usuario").order_by("-criado_em").first()

        # Detalhe POR conta: o erro específico da última ocorrência de CADA conta
        # afetada — para o superadmin diagnosticar todas as contas nesta tela, sem
        # abrir conta por conta. A saúde é visão do sistema, não da conta logada;
        # por isso não limitamos a lista (o "e mais N" antigo escondia justamente a
        # conta que precisava ser atendida). Poucos grupos e poucas contas: o N+1 é
        # irrelevante.
        afetados = []
        ids = (base.filter(usuario__isnull=False)
               .values_list("usuario_id", flat=True).distinct())
        for uid in ids:
            ex = _ultimo(usuario_id=uid)
            if ex:
                afetados.append({
                    "usuario_id": uid,
                    "usuario__username": ex.usuario.get_username() if ex.usuario else "",
                    "exemplo": ex,
                })
        afetados.sort(key=lambda a: (a["usuario__username"], a["usuario_id"]))

        # Bucket "Sistema": eventos sem conta (usuario=None, ex.: fonte_falhou "Uma
        # loja parou de responder"). É global — vale para todas as contas —, então
        # nunca deve sumir da tela por não estar amarrado a um usuário.
        sistema = _ultimo(usuario__isnull=True)

        out.append({
            **g, **descrever(g["evento"]),
            "afetados": afetados,
            "sistema": sistema,
            # Exemplo de fallback do "Detalhe técnico": o do sistema, ou o mais recente
            # de qualquer conta.
            "exemplo": sistema or (afetados[0]["exemplo"] if afetados else None),
            "critico": g["level"] == "error",
        })
    # Erros antes de avisos; dentro de cada faixa, o mais frequente primeiro.
    out.sort(key=lambda p: (p["level"] != "error", -p["n"]))
    return out


def _workers() -> list[dict]:
    """Estado dos loops. Explica o silêncio: zero erro com worker parado não é saúde."""
    out = []
    for job, controle, nome in WORKERS:
        erro_leitura = ""
        try:
            # controle=None: job sem flag, sempre ligado (ex.: monitor).
            ligado = st.is_enabled(controle) if controle else True
            vivo = st.worker_alive(job)
            estado = st.read_state(job) or {}
        except Exception as e:
            # Não pode virar "desligado": isso ZERAVA o alerta (ligado and not vivo)
            # e escondia o problema em vez de mostrá-lo. Não saber o estado de um
            # worker é, ele próprio, um alerta.
            logger.warning("Não foi possível ler o estado do worker %s: %s", job, e)
            ligado, vivo, estado = True, False, {}
            erro_leitura = "Não foi possível ler o estado deste worker."
        out.append({
            "job": job, "nome": nome, "ligado": ligado, "vivo": vivo,
            "controlavel": bool(controle),
            "controle": controle or "",
            "fase": estado.get("fase", "?"),
            "ultima_msg": erro_leitura or estado.get("ultima_msg", ""),
            "erro": erro_leitura or estado.get("erro", ""),
            # Ligado mas sem processo vivo é o pior caso: a tela diz "ligado" e
            # nada roda. É o que o usuário chamaria de "o site parou sozinho".
            "alerta": ligado and not vivo,
        })
    return out


# Causas que a tela sabe retestar sozinha (ver views_admin._retestar_incidente).
# Nenhum destes testes tem efeito visível para o usuário final — reteste que publica
# oferta ou manda mensagem seria pior que o problema.
_RETESTAVEIS_PREFIXO = ("whatsapp_", "link_", "sync_", "email_", "conexao_", "scrape_")
_RETESTAVEIS_EXATO = ("fonte_falhou", "flash_erro", "cupons_vazios",
                      "cupons_campanha_erro", "cupons_projecao_erro",
                      "links_sem_sessao")


def _retestavel(causa: str) -> bool:
    return causa.startswith(_RETESTAVEIS_PREFIXO) or causa in _RETESTAVEIS_EXATO


def _conexoes_ao_vivo(usuario=None) -> list[dict]:
    """Estado atual das conexões — o MESMO dado que o dashboard mostra.

    A Saúde nunca consultava conexão: inferia de incidentes `conexao_*` abertos, sem
    janela de tempo. Um `conexao_caiu` de semanas atrás seguia vermelho aqui ao lado
    de um dashboard verde, e era essa a divergência que mais confundia. Lendo da
    fonte única (conexoes.py), divergir virou impossível.

    Só com uma conta escolhida: sondar todas as contas a cada carregamento (e a cada
    polling de 15s) seria uma ida à rede por usuário.
    """
    if usuario is None:
        return []
    from apps.scrapers.conexoes import estados_do_usuario
    try:
        estados = estados_do_usuario(usuario)
    except Exception as e:
        logger.warning("Não foi possível ler as conexões de %s: %s", usuario, e)
        return []
    # Os quatro escopos, não dois: `ml_linkbuilder` (portal de afiliados, SSO
    # próprio) e as sessões de relatório eram calculadas por estados_do_usuario e
    # descartadas aqui. O painel que existe para acabar com divergência de tela era
    # justamente o que não mostrava a conexão cuja queda ninguém enxergava.
    return [estados["whatsapp"].as_dict(), estados["mercadolivre"].as_dict(),
            estados["ml_linkbuilder"].as_dict(), estados["ml_relatorios"].as_dict(),
            estados["amazon_relatorios"].as_dict()]


def _configuracao_silenciosa() -> list[dict]:
    """Variáveis de ambiente que desligam funcionalidades SEM gerar erro nenhum.

    Cada uma destas produz um sintoma que aponta para o lugar errado: o usuário vê
    "0 cupons prontos" ou "sessão expirada" e vai reconectar o Mercado Livre — que
    está perfeito. Nada disso aparece em log de erro, porque do ponto de vista do
    código não houve falha: o fluxo simplesmente não rodou.
    """
    from django.conf import settings

    avisos = []
    if not getattr(settings, "ML_SYSTEM_ORGANIZATION_ID", ""):
        avisos.append({
            "chave": "ML_SYSTEM_ORGANIZATION_ID",
            "efeito": "Cupons públicos do Mercado Livre nunca ficam prontos: o "
                      "preparo deles roda sem credencial e o container volta vazio.",
        })
    if not getattr(settings, "ML_LINK_BUILDER_ENABLED", False):
        avisos.append({
            "chave": "ML_LINK_BUILDER_ENABLED",
            "efeito": "Nenhum link de afiliado do Mercado Livre é gerado; a tela de "
                      "Promoções mostra tudo como pendente.",
        })
    if not getattr(settings, "ML_BROWSER_LOGIN_ENABLED", False):
        avisos.append({
            "chave": "ML_BROWSER_LOGIN_ENABLED",
            "efeito": "A tela de Conexão Mercado Livre não abre o login.",
        })
    if not getattr(settings, "ML_BROWSER_REPORTS_ENABLED", False):
        avisos.append({
            "chave": "ML_BROWSER_REPORTS_ENABLED",
            "efeito": "O portal de afiliados não pode ser conectado; a sincronização "
                      "de comissões fica parada.",
        })
    return avisos


def _incidentes(usuario, desde):
    """Incidentes abertos sempre aparecem; concluídos seguem o período escolhido."""
    base = IncidenteSaude.objects.select_related("usuario", "evento_origem")
    if usuario is not None:
        base = base.filter(usuario=usuario)
    abertos = base.filter(status="aberto")
    concluidos = base.filter(status="concluido", confirmado_em__gte=desde)

    def agrupar(itens):
        grupos = {}
        for incidente in itens:
            chave = (incidente.pipeline, incidente.causa, incidente.escopo)
            grupo = grupos.setdefault(chave, [])
            grupo.append(incidente)
        saida = []
        for (_, causa, _), itens_grupo in grupos.items():
            ultimo = max(itens_grupo, key=lambda i: i.ultima_ocorrencia)
            info = descrever(causa)
            afetados = [{"usuario_id": i.usuario_id,
                         "usuario__username": i.usuario.get_username(),
                         "exemplo": i.evento_origem}
                        for i in itens_grupo if i.usuario]
            afetados.sort(key=lambda a: (a["usuario__username"], a["usuario_id"]))
            sistema = next((i.evento_origem for i in itens_grupo if not i.usuario), None)
            saida.append({
                # Âncora do grupo: a view retesta (pipeline, causa, escopo) a partir
                # dela. Era None em grupo com mais de um incidente, porque o reteste
                # antigo só sabia lidar com um — e era esse None que apagava o botão
                # justamente quando várias contas sofriam do mesmo problema.
                "id": ultimo.id,
                "causa": causa,
                # Compatibilidade da leitura anterior; causa é o identificador novo.
                "evento": {"publicacao_falhou": "send_failed", "whatsapp_timeout_entrega": "send_timeout"}.get(causa, causa),
                "pipeline": ultimo.pipeline, "escopo": ultimo.escopo,
                "n": sum(i.ocorrencias for i in itens_grupo),
                "level": "error" if any(i.level == "error" for i in itens_grupo) else "warning",
                "critico": any(i.level == "error" for i in itens_grupo),
                "ultimo": ultimo.ultima_ocorrencia, "mensagem": ultimo.ultima_mensagem,
                "contexto": ultimo.contexto, "usuario": ultimo.usuario if len(itens_grupo) == 1 else None,
                "usuarios": len(afetados), "afetados": afetados, "sistema": sistema,
                "confirmado_em": ultimo.confirmado_em, "confirmacao": ultimo.confirmacao,
                # Sem o limite de 1 incidente: o reteste roda por grupo. Exigir grupo
                # unitário deixava sem botão justamente o caso que mais importa — o
                # mesmo problema atingindo várias contas.
                "retestavel": _retestavel(causa),
                **info,
            })
        return saida

    chave = lambda x: (x["level"] != "error", -x["n"], x["ultimo"])
    return sorted(agrupar(abertos), key=chave), sorted(
        agrupar(concluidos), key=lambda x: x["confirmado_em"], reverse=True)


def _custo_ia() -> dict:
    """Gasto de IA acumulado no mês. Nunca derruba a tela de Saúde."""
    try:
        from apps.scrapers import ia_custo
        return ia_custo.resumo_do_mes()
    except Exception:
        logger.exception("Falha ao ler o gasto de IA do mês")
        # `disponivel` em vez de custo None: no template do Django, `None` é
        # resolvido como variável e vira string vazia — a comparação silenciosa
        # daria sempre o mesmo resultado.
        return {"disponivel": False, "custo_brl": 0, "teto_brl": 0,
                "estourou": False, "por_origem": []}


def resumo(horas: int = 24, agora=None, usuario=None, usuario_nome: str = "") -> dict:
    """Fotografia do período: veredito, problemas agrupados, sinais de vida, workers."""
    agora = agora or timezone.now()
    desde = agora - timedelta(hours=horas)
    qs = EventoOperacional.objects.filter(criado_em__gte=desde)
    if usuario is not None:
        qs = qs.filter(usuario=usuario)
    elif usuario_nome:
        # Busca sem correspondência não pode cair silenciosamente no relatório global.
        qs = qs.none()

    # A projeção dos eventos em incidentes é do worker `monitor`
    # (incidentes_saude.reconciliar_pendentes), não daqui: esta função roda em GET e
    # agora também no polling do auto-refresh — escrever aqui inflaria as ocorrências
    # a cada carregamento. resumo() é SÓ LEITURA.
    problemas, concluidos = _incidentes(usuario, desde) if not usuario_nome or usuario else ([], [])
    n_erros = sum(p["n"] for p in problemas if p["critico"])
    n_avisos = sum(p["n"] for p in problemas if not p["critico"])
    grupos_criticos = sum(1 for p in problemas if p["critico"])

    contagens = dict(
        qs.values_list("evento").annotate(n=Count("id")).values_list("evento", "n")
    )
    sinais = [{"nome": nome, "n": contagens.get(ev, 0)} for ev, nome in SINAIS]
    workers = _workers()
    from apps.scrapers.manual_scraping import queue_wait_metrics
    queue_metrics = queue_wait_metrics(
        since=desde, now=agora, usuario=usuario,
    )

    if grupos_criticos:
        estado = "critico"
        texto = (f"{grupos_criticos} problema{'s' if grupos_criticos > 1 else ''} "
                 f"precisa{'m' if grupos_criticos > 1 else ''} de atenção")
    elif any(w["alerta"] for w in workers):
        estado = "critico"
        texto = "Nenhum erro registrado, mas há worker ligado que não está rodando"
    elif problemas:
        estado = "atencao"
        texto = f"{len(problemas)} aviso{'s' if len(problemas) > 1 else ''}, nada crítico"
    else:
        estado = "ok"
        texto = f"Nenhum erro nas últimas {horas}h"

    return {
        "horas": horas, "desde": desde, "agora": agora,
        "estado": estado, "texto": texto,
        "problemas": problemas,
        "concluidos": concluidos,
        "n_erros": n_erros, "n_avisos": n_avisos,
        "sinais": sinais, "workers": workers,
        "conexoes": _conexoes_ao_vivo(usuario),
        "configuracao": _configuracao_silenciosa(),
        "manual_queue": queue_metrics,
        # Gasto de IA do mês. Fica na Saúde, e não numa tela própria, porque é a
        # mesma pergunta das outras linhas daqui: o sistema está dentro do que
        # deveria? Um teto que ninguém observa não é teto.
        "custo_ia": _custo_ia(),
        "total": qs.count(),
    }

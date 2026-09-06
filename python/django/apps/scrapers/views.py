import logging
import queue
import threading
from contextlib import redirect_stdout
from functools import wraps
from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import (
    Case, F, ExpressionWrapper, Exists, FloatField, IntegerField, OuterRef, Q,
    Count, Max, Sum, When,
)
from django.http import StreamingHttpResponse, HttpResponse, JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.tenant import organization_thread_target, public_link_context
from apps.scrapers.models import (
    CliquePublicacao, ConfiguracaoEnvio, Cupom, LinkAfiliadoUsuario, Produto,
    Publicacao, ReceitaAfiliado, RelatorioSync, FonteIngestao, CupomNormalizado,
    CupomDisponibilidade, IntegracaoAfiliado, ProgramaAfiliado, ExecucaoRaspagem,
    normalizar_busca,
)
from apps.scrapers.progresso import emitir_fase
from apps.scrapers.scraper_mercadolivre.scraper import main as scrapper_main

logger = logging.getLogger(__name__)


def _resumo_do_envio(resultado, o_que="Oferta") -> str:
    """Uma linha dizendo QUANDO saiu, QUANTO demorou e se o WhatsApp confirmou.

    "Enviado" sozinho não é informação: o transporte pode levar segundos (upload
    de mídia dentro do Chromium) e o ACK do WhatsApp pode chegar depois disso. A
    tela precisa distinguir "o WhatsApp confirmou" de "aceitou e ainda não
    confirmou" — os dois são resultados legítimos, e tratá-los como a mesma coisa
    foi o que fez um envio lento parecer um envio perdido.
    """
    from django.utils import timezone as _tz
    from django.utils.dateparse import parse_datetime

    partes = [f"{o_que} enviada" if o_que == "Oferta" else f"{o_que} enviado"]

    carimbo = resultado.get("enviado_em") or ""
    momento = _tz.localtime()
    if carimbo:
        analisado = parse_datetime(carimbo)
        if analisado is not None:
            momento = _tz.localtime(analisado)
    partes.append(f"às {momento.strftime('%H:%M:%S')}")

    transporte_ms = resultado.get("transporte_ms") or resultado.get("duracao_ms") or 0
    if transporte_ms:
        partes.append(f"em {transporte_ms / 1000:.1f}s")

    if resultado.get("confirmacao") == "ack":
        ack_ms = resultado.get("ack_ms")
        confirmacao = "confirmado pelo WhatsApp"
        if ack_ms:
            confirmacao += f" após {ack_ms / 1000:.1f}s"
        partes.append(f"— {confirmacao}")
    else:
        partes.append("— aceito pelo WhatsApp, confirmação ainda não chegou")

    via = resultado.get("via")
    if via:
        partes.append(f"(via {via})")
    link = resultado.get("link")
    if link:
        partes.append(f"Link: {link}")
    return " ".join(partes)


def _send_pipeline_v2_enabled(user) -> bool:
    from apps.accounts.feature_flags import send_pipeline_v2_enabled
    return send_pipeline_v2_enabled(user)


def staff_required(view):
    """Restringe a view a administradores (is_staff).

    A raspagem (e o login/sessão de ML compartilhada) é controlada só pelo admin;
    usuários comuns usam Promoções, Envios e Conexões. 403 em vez de redirect p/
    proteger também as chamadas diretas aos endpoints SSE (não só esconder no menu).
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied("Apenas administradores controlam a raspagem.")
        return view(request, *args, **kwargs)
    return _wrapped


def pode_ligar_envio(user) -> bool:
    """Quem pode ligar/desligar o worker de envio: staff ou delegado pelo superadmin.

    Só vale para o loop `envio`; a raspagem segue exclusiva de is_staff. Ver o
    comentário de Perfil.pode_ligar_envio para o porquê da delegação existir.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_staff:
        return True
    perfil = getattr(user, "perfil", None)
    return bool(perfil and perfil.pode_ligar_envio)


def superadmin_required(view):
    """Restringe a view ao superadmin (is_superuser).

    Workspace do superadmin: lista de usuários, uso/máquinas, cotas, suspensão e
    impersonação. 403 (não redirect) p/ proteger chamadas diretas aos endpoints.
    """
    @wraps(view)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            raise PermissionDenied("Apenas o superadmin acessa este painel.")
        return view(request, *args, **kwargs)
    return _wrapped


_TAXONOMIA_TTL_S = 600


def _taxonomia_cacheada(sufixo: str, request, produzir):
    """Cacheia listas de categoria derivadas de um DISTINCT sobre `Produto`.

    Esses DISTINCT varrem a tabela de produtos INTEIRA e rodavam a cada GET de
    /scrapers/config/ e /scrapers/top/, só para preencher os <select> de filtro. A
    taxonomia muda no ritmo da raspagem (horas), não no ritmo dos cliques.

    A chave inclui a organização porque a RLS decide o que cada tenant enxerga: uma
    chave global entregaria a um tenant a lista derivada das linhas de outro.
    """
    from django.core.cache import cache

    # A organização já foi resolvida pelo OrganizationMiddleware; chamar
    # `organization_for_user` de novo seria uma query só para montar a chave.
    organization = getattr(request, "organization", None)
    chave = f"taxonomia:{sufixo}:org:{getattr(organization, 'pk', '_sem_org')}"
    valor = cache.get(chave)
    if valor is None:
        valor = produzir()
        cache.set(chave, valor, _TAXONOMIA_TTL_S)
    return valor


def throttle_sse(max_por_min=10):
    """Limita quantas vezes/min um usuário dispara um endpoint SSE pesado.

    Cada stream sobe uma thread (Playwright/HTTP) na MÁQUINA COMPARTILHADA;
    sem teto, um tenant satura CPU/RAM/Chromium dos demais. Ao estourar, devolve um
    stream curto de erro (EventSource-friendly) em vez de rodar o job.
    """
    def deco(view):
        @wraps(view)
        def _wrapped(request, *args, **kwargs):
            from django.core.cache import cache
            from apps.scrapers.eventos import log_event
            uid = getattr(request.user, "id", None) or "anon"
            key = f"sse-throttle:{view.__name__}:{uid}"
            if cache.get(key, 0) >= max_por_min:
                log_event("sistema", "sse_throttled", "Endpoint pesado limitado.",
                          level="warning", usuario=request.user,
                          contexto={"view": view.__name__})
                def _err():
                    yield "retry: 30000\n\n"
                    yield "data: [ERRO] Muitas execuções seguidas. Aguarde ~1 minuto.\n\n"
                    # As telas de login (ml_conexao.html) só escutam eventos
                    # NOMEADOS ('frame', 'heartbeat', 'done'): sem este 'done'
                    # o EventSource reconectava para sempre e renovava o próprio
                    # bloqueio. As telas de raspagem leem as linhas 'data:' —
                    # mantidas intactas acima e abaixo.
                    yield "event: done\ndata: [ERRO] Muitas execuções seguidas. Aguarde ~1 minuto.\n\n"
                    yield "data: __DONE__\n\n"
                resp = StreamingHttpResponse(_err(), content_type="text/event-stream")
                resp["Cache-Control"] = "no-cache"
                return resp
            cache.set(key, cache.get(key, 0) + 1, 60)
            return view(request, *args, **kwargs)
        return _wrapped
    return deco


# O EventSource reconecta sozinho sempre que o stream cai. Sem um `retry:` explícito o
# navegador tenta de novo em ~3s, e 6 tentativas cabem em menos de 20 segundos: o teto
# de throttle_sse(6) estourava e a tela ficava SEM IMAGEM justamente quando o stream
# estava instável — o pior momento possível. 15s por tentativa dá 4 reconexões/minuto:
# folga real sob o teto, com espaço para um reload da página sem estourar o throttle.
SSE_RETRY_MS = 15000


def _stream_login_sse(eventos):
    """Serializa os eventos do transporte de login no formato do EventSource."""
    yield f"retry: {SSE_RETRY_MS}\n\n"
    for event in eventos:
        if event.get("id") is not None:
            yield f"id: {event['id']}\n"
        yield f"event: {event['event']}\ndata: {event['data']}\n\n"
    yield "event: done\ndata: __DONE__\n\n"


def _resposta_login_sse(eventos):
    """Resposta SSE das telas de login (ML, portal de relatórios e Amazon).

    `X-Accel-Buffering: no` impede que qualquer proxy segure os frames num buffer, o
    que transformaria o live view numa sequência de saltos.
    """
    resposta = StreamingHttpResponse(
        _stream_login_sse(eventos), content_type="text/event-stream",
    )
    resposta["Cache-Control"] = "no-cache"
    resposta["X-Accel-Buffering"] = "no"
    return resposta


class _QueueWriter:
    """File-like object that feeds lines into a Queue for SSE streaming."""

    def __init__(self, q: queue.Queue):
        self._q = q
        self._buf = ""

    def write(self, text: str):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line:
                self._q.put(line)

    def flush(self):
        if self._buf:
            self._q.put(self._buf)
            self._buf = ""


# Espelha as heurísticas de causa de ofertas.falhar(): o texto cru da exceção fica
# em Publicacao.erro (admin/Saúde); na home entra só a versão para o usuário.
_ERROS_PUBLICACAO = [
    (("link de afiliado", "link builder"),
     "Não foi possível gerar o link de afiliado — verifique a conexão com a loja."),
    (("link reprovado",), "O link foi reprovado na verificação de afiliação."),
    (("módulos internos", "recarregando", "frame"),
     "O WhatsApp Web recarregou durante o envio."),
    (("timeout", "demorou"), "O WhatsApp demorou para responder ao envio."),
    (("confirma", "ack"), "O envio saiu, mas não veio confirmação do WhatsApp."),
    (("login", "sessão", "sessao"), "A sessão da loja expirou — reconecte na aba Conta."),
]


def _erro_publicacao(texto):
    t = (texto or "").lower()
    if not t:
        return ""
    for chaves, msg in _ERROS_PUBLICACAO:
        if any(c in t for c in chaves):
            return msg
    return "Falha no envio — verifique as conexões se persistir."


def operations_dashboard(request):
    """Centro operacional e de receita do afiliado."""
    from datetime import timedelta
    from apps.scrapers.monitor_conexao import ml_conectado, wa_conectado

    from apps.scrapers.relatorios import resumo_financeiro

    desde = timezone.now() - timedelta(days=30)
    pubs = Publicacao.objects.filter(usuario=request.user, criada_em__gte=desde)
    resumo = pubs.aggregate(
        enviados=Count("id", filter=Q(status="enviado"), distinct=True),
        falhas=Count("id", filter=Q(status="falhou"), distinct=True),
        pendentes=Count("id", filter=Q(status="pendente"), distinct=True),
    )
    # Snapshot mais recente por loja, não Sum de 30 dias: ver resumo_financeiro.
    financeiro = resumo_financeiro(request.user)
    comissao = financeiro.get("comissao") or 0
    posts = resumo.get("enviados") or 0
    financeiro["comissao_por_post"] = comissao / posts if posts else 0
    # Envios primeiro: com o link direto da loja na mensagem, cliques internos
    # pararam de contar — ordenar por eles fossilizaria o ranking no legado.
    melhores_categorias = list(
        pubs.filter(status="enviado").values("categoria")
        .annotate(envios=Count("id", distinct=True), cliques=Count("cliques"))
        .order_by("-envios", "-cliques")[:5]
    )
    melhores_destinos = list(
        pubs.filter(status="enviado").values("destino_nome", "destino_id")
        .annotate(envios=Count("id", distinct=True), cliques=Count("cliques"))
        .order_by("-envios", "-cliques")[:5]
    )
    from apps.scrapers.conexoes import (
        estado_ml, estado_ml_linkbuilder, estado_whatsapp,
    )

    perfil = request.user.perfil
    configs = ConfiguracaoEnvio.objects.filter(owner=request.user)
    alertas = []
    est_ml = estado_ml(request.user)
    est_wa = estado_whatsapp(request.user, session=perfil.sessao_whatsapp())
    ml_ok, wa_ok = est_ml.conectado, est_wa.conectado
    # Só faz sentido perguntar pelo Link Builder quando o site já está OK: sem
    # sessão nenhuma o aviso relevante é o de baixo, e dois avisos para a mesma
    # causa era parte da confusão que esta tela produzia.
    est_lb = estado_ml_linkbuilder(request.user) if ml_ok else None
    lb_ok = est_lb.conectado if est_lb else True
    if ml_ok and est_lb and est_lb.detalhe == "login_required":
        # A falha que antes só aparecia como erro dentro do stream de geração de
        # links, depois de o usuário clicar e esperar.
        alertas.append(("Link Builder pedindo login", est_lb.motivo,
                        "scraper-ml-conexao"))
    elif ml_ok and est_lb and est_lb.detalhe == "temporarily_unavailable":
        alertas.append((
            "Link Builder temporariamente indisponível",
            est_lb.motivo,
            "scraper-ml-conexao",
        ))
    if not ml_ok and not perfil.amazon_conectado():
        # O motivo vem do estado: "sessão expirou" e "nunca conectou" pedem ações
        # diferentes, e o texto fixo dizia a mesma coisa para os dois.
        alertas.append(("Loja desconectada",
                        est_ml.motivo or "Conecte Mercado Livre ou Amazon para gerar links comissionados.",
                        "scraper-ml-conexao"))
    # "conectando" é o worker religando após deploy — piscar "Nenhum canal
    # conectado" nesses segundos assustava sem haver o que fazer.
    if not wa_ok and est_wa.detalhe != "conectando" and not perfil.telegram_conectado():
        alertas.append(("Nenhum canal conectado", "Conecte WhatsApp ou Telegram antes de ativar envios.", "scraper-whatsapp"))
    pausadas = configs.filter(ativo=False).exclude(motivo_pausa="").count()
    if pausadas:
        alertas.append((f"{pausadas} regra(s) pausada(s)", "Revise as falhas consecutivas e reative quando estiver pronto.", "scraper-configuracoes"))
    if not configs.exists():
        alertas.append(("Crie sua primeira automação", "Personalize um destino e faça um envio de teste.", "scraper-configuracoes"))
    syncs = {
        s.marketplace: s for s in RelatorioSync.objects.filter(usuario=request.user)
    }
    for marketplace in ("mercadolivre", "amazon"):
        syncs.setdefault(marketplace, RelatorioSync(
            usuario=request.user, marketplace=marketplace))
    from apps.scrapers.relatorios import report_prerequisites
    for sync in syncs.values():
        sync.preflight = report_prerequisites(request.user, sync.marketplace)
        # "nao_configurado" fica de fora: não é incidente e não há ação do usuário —
        # alertar sobre isso era um aviso permanente que ele não podia resolver. O
        # estado aparece na lista de sincronizações, que é onde ele pertence.
        if sync.status in {"erro", "acao"}:
            alertas.append((
                f"Relatório {sync.marketplace} precisa de atenção",
                sync.erro_publico or "Sincronização automática não concluiu.",
                # Reconectar a conta é o que resolve; "home" apontava pra esta mesma
                # página, então clicar no alerta não levava a lugar nenhum. E é a
                # conexão de RELATÓRIOS que quebrou (report_sessions, sessão própria
                # por usuário): mandar para a conexão do site era pedir um login que
                # não conserta o sync.
                "scraper-amazon" if sync.marketplace == "amazon"
                else "scraper-ml-relatorio-conexao",
            ))
    publicacoes = list(
        pubs.select_related("produto", "configuracao", "cupom_normalizado")
        .prefetch_related("tentativas")
        .order_by("-criada_em")[:10])
    for p in publicacoes:
        p.erro_publico = _erro_publicacao(p.erro)
        p.ultima_tentativa = max(p.tentativas.all(), key=lambda t: t.numero, default=None)
        if p.stage == "uncertain":
            p.acao_recomendada = "Verifique o destino; não reenvie automaticamente."
        elif p.next_retry_at:
            p.acao_recomendada = "Retry automático agendado."
        elif p.stage == "permanent_failed":
            p.acao_recomendada = "Revise destino, payload ou credencial revogada."
        elif p.status == "falhou":
            p.acao_recomendada = "Revise a causa e tente novamente após corrigir."
        else:
            p.acao_recomendada = ""
    return render(request, "home.html", {
        "resumo": resumo, "financeiro": financeiro,
        "melhores_categorias": melhores_categorias,
        "melhores_destinos": melhores_destinos,
        "publicacoes": publicacoes,
        "alertas": alertas, "configs": configs, "syncs": list(syncs.values()),
        "ml_ok": ml_ok, "wa_ok": wa_ok, "lb_ok": lb_ok,
        "est_ml": est_ml, "est_wa": est_wa, "est_lb": est_lb,
        "tg_ok": perfil.telegram_conectado(),
    })


def _responder_clique(publicacao):
    """Registra somente o evento de clique e redireciona ao link afiliado."""
    destino = publicacao.link_afiliado or ""
    # Defesa: só redireciona p/ http(s). Barra esquemas perigosos (javascript:, data:)
    # caso um link corrompido chegue ao banco.
    if not destino.startswith(("https://", "http://")):
        return HttpResponse("Link inválido ou indisponível.", status=404)
    CliquePublicacao.objects.create(publicacao=publicacao)
    response = redirect(destino)
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


@login_not_required
def redirect_rastreado(request, token):
    """Formato antigo (token assinado): mantém válidos os links já publicados."""
    try:
        payload = signing.loads(token, salt="click")
    except (signing.BadSignature, KeyError):
        return HttpResponse("Link inválido ou indisponível.", status=404)
    # A busca e o registro do clique moram DENTRO do contexto: quem clica não tem
    # sessão, e sem ele a RLS esconde a linha e o link publicado responde 404.
    with public_link_context(payload.get("p")):
        try:
            publicacao = Publicacao.objects.get(id_publico=payload["p"], status="enviado")
        except (KeyError, ValueError, Publicacao.DoesNotExist):
            return HttpResponse("Link inválido ou indisponível.", status=404)
        return _responder_clique(publicacao)


@login_not_required
def redirect_curto(request, slug):
    """Formato curto (/r/<slug>/) que entra nas mensagens novas."""
    with public_link_context(slug):
        try:
            publicacao = Publicacao.objects.get(slug_curto=slug, status="enviado")
        except Publicacao.DoesNotExist:
            return HttpResponse("Link inválido ou indisponível.", status=404)
        return _responder_clique(publicacao)


@require_POST
def sincronizar_receitas(request):
    """Agenda a sincronização dos relatórios do marketplace selecionado.

    Agenda, não executa: o sync sobe um Chromium (Playwright, goto de 45s) e fazer
    isso DENTRO do request punha um browser inteiro no processo do gunicorn, contra o
    timeout de 120s e disputando a CPU com o resto do painel. Quem executa é o worker
    "relatorios" do Procfile, que já roda sync_due_reports; aqui só marcamos o
    registro como vencido, e ele pega no próximo poll (~1min).
    """
    marketplace = (request.POST.get("marketplace") or "").lower()
    if marketplace not in {"mercadolivre", "amazon"}:
        messages.error(request, "Marketplace inválido para sincronização.")
        return redirect("home")
    from apps.scrapers.relatorios import report_prerequisites
    preflight = report_prerequisites(request.user, marketplace)
    if not preflight["ok"]:
        status = "acao" if preflight["code"] == "session_missing" else "nao_configurado"
        sync, _ = RelatorioSync.objects.get_or_create(
            usuario=request.user, marketplace=marketplace)
        RelatorioSync.objects.filter(pk=sync.pk).update(
            status=status, prerequisite_code=preflight["code"],
            erro=preflight["instruction"][:500], proxima_execucao=None,
        )
        messages.error(request, preflight["instruction"])
        return redirect("home")
    sync, _ = RelatorioSync.objects.get_or_create(
        usuario=request.user, marketplace=marketplace)
    RelatorioSync.objects.filter(pk=sync.pk).update(proxima_execucao=timezone.now())
    messages.success(
        request, f"{marketplace}: sincronização agendada. "
                 "O resultado aparece aqui em instantes.")
    return redirect("home")


@staff_required
@ensure_csrf_cookie
def dashboard(request):
    """Painel + checklist de primeiros passos (onboarding orientado a conexões)."""
    from apps.scrapers.monitor_conexao import ml_conectado, wa_conectado

    user = request.user
    perfil = getattr(user, "perfil", None)

    ml_ok = ml_conectado(user)
    # O passo "afiliado conectado" media a MESMA coisa que o passo anterior (a sessão
    # do site) e por isso ficava verde mesmo com o Link Builder recusando o login —
    # o checklist declarava pronta exatamente a etapa que estava quebrada. Agora ele
    # mede o portal de afiliados, que é o que a geração de link atravessa.
    from apps.scrapers.conexoes import estado_ml_linkbuilder

    est_lb = estado_ml_linkbuilder(user) if ml_ok else None
    tag_ok = bool(est_lb and est_lb.conectado)
    wa_ok = wa_conectado(perfil.sessao_whatsapp() if perfil else str(user.id))
    tg_ok = bool(perfil and perfil.telegram_conectado())
    canal_ok = wa_ok or tg_ok
    regra_ok = ConfiguracaoEnvio.objects.filter(owner=user).exists()

    # Uma etapa = {título, feito, CTA}. Ordem = caminho até o "aha" (enviar 1 oferta).
    passos = [
        {"key": "ml", "titulo": "Conectar sua conta do Mercado Livre", "feito": ml_ok,
         "desc": "Login seguro na própria página — gera seus links de afiliado.",
         "cta": "Conectar", "url": "/scrapers/ml/", "icon": "shopping-bag"},
        {"key": "tag", "titulo": "Mercado Livre afiliado conectado", "feito": tag_ok,
         "desc": (est_lb.motivo if est_lb and not tag_ok
                  else "O Link Builder usa a conta logada para gerar o link comissionado."),
         "cta": "Conectar", "url": "/scrapers/ml/", "icon": "badge-dollar-sign"},
        {"key": "canal", "titulo": "Conectar um canal de envio", "feito": canal_ok,
         "desc": "WhatsApp (QR) ou Telegram (bot) — por onde as ofertas saem.",
         "cta": "Conectar", "url": "/scrapers/whatsapp/", "icon": "message-circle"},
        {"key": "regra", "titulo": "Criar uma regra de envio", "feito": regra_ok,
         "desc": "Nicho → canal → intervalo. Depois é só ligar o automático.",
         "cta": "Criar regra", "url": "/scrapers/config/", "icon": "list-checks"},
    ]
    feitos = sum(1 for p in passos if p["feito"])
    return render(request, "scrapers/dashboard.html", {
        "passos": passos,
        "passos_feitos": feitos,
        "passos_total": len(passos),
        "onboarding_completo": feitos == len(passos),
    })


def comecar(request):
    """Checklist de onboarding self-serve: cada passo lê o estado real e mostra ✓/todo.

    Objetivo: um usuário novo consegue ficar operacional sozinho (tags → conexão →
    regra → ligar envio) sem depender do suporte."""
    from apps.scrapers.conexoes import estado_ml, estado_whatsapp

    perfil = getattr(request.user, "perfil", None)
    # Estado ao vivo, não perfil.wa_estado/ml_estado: aquelas colunas são o último
    # estado visto pelo watchdog e ficam `None` até ele rodar a primeira vez. Esta
    # tela mostrava "desconectado" para quem estava conectado, enquanto o dashboard
    # ao lado mostrava conectado.
    wa_ok = estado_whatsapp(request.user).conectado
    loja_ok = estado_ml(request.user).conectado or bool(perfil and perfil.amazon_conectado())
    tem_config = ConfiguracaoEnvio.objects.filter(owner=request.user).exists()
    teste_ok = Publicacao.objects.filter(usuario=request.user, status="enviado").exists()
    envio_ligado = ConfiguracaoEnvio.objects.filter(owner=request.user, ativo=True).exists()

    passos = [
        {"titulo": "Conectar WhatsApp", "feito": wa_ok,
         "desc": "Pareie seu aparelho pelo QR Code para disparar as ofertas.",
         "url": "scraper-whatsapp", "cta": "Conectar WhatsApp"},
        {"titulo": "Conectar loja (login no ML / Amazon)", "feito": loja_ok,
         "desc": "Faça login no Mercado Livre e salve a sessão no robô (ou conecte a Amazon Creators).",
         "url": "scraper-conta", "cta": "Conectar loja"},
        {"titulo": "Descrever o público de um grupo", "feito": tem_config,
         "desc": "Cada grupo pode ter nichos, descontos, horários e voz próprios.",
         "url": "scraper-configuracoes", "cta": "Criar regra"},
        {"titulo": "Publicar uma oferta de teste", "feito": teste_ok,
         "desc": "Valide produto, preço, cupom, mensagem e link antes de automatizar.",
         "url": "scraper-configuracoes", "cta": "Fazer teste"},
        {"titulo": "Ativar uma automação", "feito": envio_ligado and teste_ok,
         "desc": "Depois do teste, mantenha ativa somente a regra que estiver pronta.",
         "url": "scraper-configuracoes", "cta": "Ir para Envios"},
    ]
    feitos = sum(1 for p in passos if p["feito"])
    return render(request, "scrapers/comecar.html", {
        "passos": passos, "feitos": feitos, "total": len(passos),
        "completo": feitos == len(passos),
    })


def _tem_sessao_ml(user) -> bool:
    """True se a sessão do ML deste usuário existe E o ML ainda a aceita.

    Antes só checava a existência do arquivo, ignorando a validade — então a tela de
    Conta dizia "sessão ok" com um auth de 30 dias enquanto o dashboard, que aplicava
    a regra de staleness, dizia "loja desconectada".
    """
    from apps.scrapers.conexoes import estado_ml
    return estado_ml(user).conectado


def _salvar_campos_amazon(perfil, post) -> None:
    """Persiste tag de afiliado e credenciais Creators API da Amazon.

    Único caminho de escrita desses campos: a página da integração Amazon usa
    este helper. A secret só é sobrescrita quando o campo vem preenchido
    (em branco mantém a atual); o valor é criptografado pelo campo do model."""
    perfil.afiliado_tag_amazon = (post.get("afiliado_tag_amazon") or "").strip()
    perfil.amazon_credential_id = (post.get("amazon_credential_id") or "").strip()
    perfil.amazon_creators_host = (post.get("amazon_creators_host") or "").strip()
    campos = ["afiliado_tag_amazon", "amazon_credential_id", "amazon_creators_host"]
    novo_secret = (post.get("amazon_credential_secret") or "").strip()
    if novo_secret:
        perfil.amazon_credential_secret = novo_secret
        campos.append("amazon_credential_secret")
    perfil.save(update_fields=campos)


def amazon_painel(request):
    """Página da integração Amazon: tag de afiliado, Creators API (opcional),
    portal de relatórios e saúde das fontes. Antes essa configuração vivia
    embutida em Conta — agora a Amazon tem página própria, como ML/WhatsApp/
    Telegram, reutilizando o MESMO salvamento (sem duplicar regras)."""
    perfil = getattr(request.user, "perfil", None)
    if perfil and request.method == "POST":
        _salvar_campos_amazon(perfil, request.POST)
        messages.success(request, "Integração Amazon atualizada.")
        return redirect("scraper-amazon")

    from apps.scrapers.conexoes import estado_amazon_relatorios
    from apps.scrapers.report_sessions import has_report_session
    sync = RelatorioSync.objects.filter(
        usuario=request.user, marketplace="amazon").first()
    fontes = FonteIngestao.objects.filter(
        marketplace="amazon", habilitada=True).order_by("nome")
    return render(request, "scrapers/amazon.html", {
        "perfil": perfil,
        "tem_secret": bool(perfil and perfil.amazon_credential_secret),
        # Ortogonal ao "conectado": a Creators API é upgrade opcional, exibido só
        # como informação. Não pode voltar a virar requisito de conexão.
        "amazon_conectado": bool(perfil and perfil.amazon_conectado()),
        "amazon_creators_ativa": bool(perfil and perfil.amazon_creators_ativa()),
        "amazon_shop_conectado": has_report_session(request.user, "amazon_shop"),
        "est_relatorios": estado_amazon_relatorios(request.user),
        "sync": sync,
        "fontes": fontes,
    })


def configurar_conta(request):
    """Conta do afiliado: plano, identidade de publicação, templates e resumo
    das integrações. A configuração da Amazon mora na página própria da
    integração (scraper-amazon); a sessão ML é conectada em Conexão Mercado
    Livre. Cada usuário configura a PRÓPRIA conta (multi-tenant)."""
    perfil = getattr(request.user, "perfil", None)
    if perfil and request.method == "POST":
        perfil.nome_marca = (request.POST.get("nome_marca") or perfil.nome_marca).strip()[:80]
        perfil.tom_marca = (request.POST.get("tom_marca") or perfil.tom_marca).strip()[:20]
        perfil.chamada_acao = (request.POST.get("chamada_acao") or perfil.chamada_acao).strip()[:120]
        perfil.divulgacao_afiliado = (
            request.POST.get("divulgacao_afiliado") or perfil.divulgacao_afiliado
        ).strip()[:180]
        perfil.template_a = (request.POST.get("template_a") or "").strip()
        perfil.template_b = (request.POST.get("template_b") or "").strip()
        perfil.save(update_fields=["nome_marca", "tom_marca", "chamada_acao",
                                   "divulgacao_afiliado", "template_a", "template_b"])
        messages.success(request, "Conta atualizada.")
        return redirect("scraper-conta")

    from apps.scrapers.conexoes import estado_amazon_relatorios, estado_ml_relatorios
    from apps.scrapers.report_sessions import has_report_session
    awin_integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin").first()
    shopee_integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="shopee").first()
    return render(request, "scrapers/conta.html", {
        "shopee_enabled": getattr(settings, "SHOPEE_INTEGRATION_ENABLED", False),
        "shopee_integracao": shopee_integracao,
        "shopee_shop_conectado": has_report_session(request.user, "shopee_shop"),
        "perfil": perfil,
        "tem_secret": bool(perfil and perfil.amazon_credential_secret),
        "ml_sessao_ok": _tem_sessao_ml(request.user),
        "amazon_conectado": bool(perfil and perfil.amazon_conectado()),
        # Ortogonal ao "conectado": a Creators API é upgrade opcional, exibido só
        # como informação. Não pode voltar a virar requisito de conexão.
        "amazon_creators_ativa": bool(perfil and perfil.amazon_creators_ativa()),
        "amazon_relatorio_conectado": bool(estado_amazon_relatorios(request.user).conectado),
        "ml_relatorio_conectado": bool(estado_ml_relatorios(request.user).conectado),
        "billing_checkout_url": settings.BILLING_CHECKOUT_URL,
        "billing_portal_url": settings.BILLING_PORTAL_URL,
        "awin_enabled": getattr(settings, "AWIN_INTEGRATION_ENABLED", False),
        "awin_integracao": awin_integracao,
        "awin_programas": list(awin_integracao.programas.order_by("nome"))
        if awin_integracao else [],
        # Mantém a escolha disponível após refresh/erro de formulário; ela é
        # removida somente quando uma conta é efetivamente selecionada.
        "awin_contas": request.session.get("awin_contas", []),
    })


@require_POST
def awin_conectar(request):
    if not getattr(settings, "AWIN_INTEGRATION_ENABLED", False):
        return JsonResponse({"erro": "Integração Awin indisponível."}, status=404)
    from apps.scrapers.awin import AwinError, listar_contas, sincronizar_integracao
    token = (request.POST.get("token") or "").strip()
    if len(token) < 20:
        messages.error(request, "Cole um token Awin válido.")
        return redirect("scraper-conta")
    try:
        contas = listar_contas(token)
    except AwinError as exc:
        messages.error(request, exc.public_message)
        return redirect("scraper-conta")
    integracao, _ = IntegracaoAfiliado.objects.get_or_create(
        owner=request.user, provedor="awin")
    integracao.token = token
    integracao.habilitada = True
    integracao.status = "pendente"
    integracao.erro_publico = ""
    integracao.save(update_fields=["token", "habilitada", "status", "erro_publico"])
    if len(contas) > 1:
        request.session["awin_contas"] = contas
        messages.info(request, "Token validado. Escolha qual conta Publisher usar.")
        return redirect("scraper-conta")
    conta = contas[0]
    integracao.identificador_conta = conta["id"]
    integracao.nome_conta = conta["nome"]
    integracao.status = "conectada"
    integracao.save(update_fields=["identificador_conta", "nome_conta", "status"])
    try:
        sincronizar_integracao(integracao, forcar_programas=True)
        messages.success(request, "Awin conectada e sincronizada.")
    except AwinError as exc:
        messages.warning(request, exc.public_message)
    return redirect("scraper-conta")


@require_POST
def awin_selecionar_conta(request):
    from apps.scrapers.awin import AwinError, listar_contas, sincronizar_integracao
    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin").first()
    if not integracao or not integracao.token:
        messages.error(request, "Conecte a Awin novamente.")
        return redirect("scraper-conta")
    selected = (request.POST.get("publisher_id") or "").strip()
    try:
        conta = next((c for c in listar_contas(integracao.token) if c["id"] == selected), None)
        if not conta:
            raise AwinError("A conta escolhida não pertence a este token.")
        integracao.identificador_conta = conta["id"]
        integracao.nome_conta = conta["nome"]
        integracao.status = "conectada"
        integracao.habilitada = True
        integracao.save(update_fields=[
            "identificador_conta", "nome_conta", "status", "habilitada"])
        request.session.pop("awin_contas", None)
        sincronizar_integracao(integracao, forcar_programas=True)
        messages.success(request, "Conta Awin selecionada e sincronizada.")
    except AwinError as exc:
        messages.error(request, exc.public_message)
    return redirect("scraper-conta")


@require_POST
def awin_sincronizar(request):
    from apps.scrapers.awin import AwinError, sincronizar_integracao
    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin", habilitada=True).first()
    if not integracao:
        messages.error(request, "Awin não conectada.")
    else:
        try:
            result = sincronizar_integracao(integracao, forcar_programas=True)
            messages.success(request, f"Awin sincronizada: {result['coupons']} campanha(s).")
        except AwinError as exc:
            messages.error(request, exc.public_message)
    return redirect("scraper-conta")


@require_POST
def awin_programa_toggle(request, programa_id):
    programa = ProgramaAfiliado.objects.filter(
        pk=programa_id, integracao__owner=request.user,
        integracao__provedor="awin").first()
    if not programa:
        raise PermissionDenied("Programa não pertence a esta conta.")
    programa.habilitado = not programa.habilitado
    programa.save(update_fields=["habilitado"])
    messages.success(request, f"{programa.nome}: {'ativo' if programa.habilitado else 'pausado'}.")
    return redirect("scraper-conta")


@require_POST
def awin_desconectar(request):
    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="awin").first()
    if integracao:
        integracao.token = ""
        integracao.habilitada = False
        integracao.status = "desativada"
        integracao.proxima_sincronizacao = None
        integracao.erro_publico = ""
        integracao.save(update_fields=[
            "token", "habilitada", "status", "proxima_sincronizacao", "erro_publico"])
        CupomNormalizado.objects.filter(owner=request.user, integracao=integracao).update(
            estado="inativo")
    messages.success(request, "Awin desconectada. O histórico foi preservado.")
    return redirect("scraper-conta")


# ── Shopee ───────────────────────────────────────────────────────────────────
# Conexão por credencial, sem login remoto e sem navegador: a Shopee tem API
# oficial, então conectar é colar AppId e Secret do painel de afiliados. As
# credenciais moram em IntegracaoAfiliado (Secret no campo cifrado `token`), o mesmo
# contrato da Awin — nada de campo novo no Perfil, que é o caminho legado de ML e
# Amazon e não escala para uma loja por linha.

@require_POST
def shopee_conectar(request):
    if not getattr(settings, "SHOPEE_INTEGRATION_ENABLED", False):
        return JsonResponse({"erro": "Integração Shopee indisponível."}, status=404)
    from apps.scrapers.shopee import ShopeeError, validar_credenciais

    app_id = (request.POST.get("app_id") or "").strip()
    secret = (request.POST.get("app_secret") or "").strip()
    if not app_id or not secret:
        messages.error(request, "Informe o App ID e o Secret do painel de afiliados.")
        return redirect("scraper-conta")
    try:
        # Valida ANTES de gravar. Credencial errada tem de falhar com a pessoa
        # olhando a tela, e não seis horas depois num worker silencioso.
        validar_credenciais(app_id, secret)
    except ShopeeError as exc:
        messages.error(request, exc.public_message)
        return redirect("scraper-conta")

    integracao, _ = IntegracaoAfiliado.objects.get_or_create(
        owner=request.user, provedor="shopee")
    integracao.identificador_conta = app_id[:120]
    integracao.nome_conta = f"Shopee {app_id[:6]}…"
    integracao.token = secret
    integracao.habilitada = True
    integracao.status = "conectada"
    integracao.erro_publico = ""
    integracao.falhas_consecutivas = 0
    integracao.ultimo_sucesso = timezone.now()
    integracao.save(update_fields=[
        "identificador_conta", "nome_conta", "token", "habilitada", "status",
        "erro_publico", "falhas_consecutivas", "ultimo_sucesso"])
    messages.success(
        request,
        "Shopee conectada. As ofertas e campanhas entram no próximo ciclo de coleta.",
    )
    return redirect("scraper-conta")


@require_POST
def shopee_sincronizar(request):
    from apps.scrapers.shopee import ShopeeError
    from apps.scrapers.sources import run_source

    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="shopee", habilitada=True).first()
    if not integracao:
        messages.error(request, "Shopee não conectada.")
        return redirect("scraper-conta")
    try:
        campanhas = run_source("shopee-campaigns", owner=request.user)
        ofertas = run_source("shopee-offers", owner=request.user)
    except ShopeeError as exc:
        messages.error(request, exc.public_message)
        return redirect("scraper-conta")
    messages.success(
        request,
        f"Shopee sincronizada: {len(ofertas.get('offers') or [])} oferta(s) e "
        f"{len(campanhas.get('coupons') or [])} campanha(s).",
    )
    return redirect("scraper-conta")


@require_POST
def shopee_desconectar(request):
    integracao = IntegracaoAfiliado.objects.filter(
        owner=request.user, provedor="shopee").first()
    if integracao:
        integracao.token = ""
        integracao.habilitada = False
        integracao.status = "desativada"
        integracao.proxima_sincronizacao = None
        integracao.erro_publico = ""
        integracao.save(update_fields=[
            "token", "habilitada", "status", "proxima_sincronizacao", "erro_publico"])
        # Inativa, não apaga: o histórico de envio continua explicável depois que a
        # conta sai, e reconectar não recria linhas duplicadas.
        CupomNormalizado.objects.filter(owner=request.user, integracao=integracao).update(
            estado="inativo")
    messages.success(request, "Shopee desconectada. O histórico foi preservado.")
    return redirect("scraper-conta")


def _data_form_aware(value, *, fim=False):
    from datetime import datetime, time
    from django.utils.dateparse import parse_date, parse_datetime
    raw = (value or "").strip()
    parsed = parse_datetime(raw)
    if parsed is None:
        day = parse_date(raw)
        if day:
            parsed = datetime.combine(day, time(23, 59, 59) if fim else time.min)
    if parsed and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _url_manual_valida(marketplace, url):
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if marketplace == "mercadolivre":
        return host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")
    if marketplace == "amazon":
        return host == "amazon.com.br" or host.endswith(".amazon.com.br")
    if marketplace == "shopee":
        return host == "shopee.com.br" or host.endswith(".shopee.com.br")
    return marketplace == "awin"


@require_POST
def cupom_manual_salvar(request, cupom_id=None):
    import uuid
    from apps.scrapers.awin import AwinError, gerar_deeplink, url_permitida
    from apps.scrapers.coupon_rules import (
        classificar_contrato_cupom, derivar_categoria_cupom,
        normalizar_regras_cupom,
    )

    coupon = None
    if cupom_id:
        coupon = CupomNormalizado.objects.filter(
            pk=cupom_id, owner=request.user, fonte__slug="manual-private").first()
        if not coupon:
            raise PermissionDenied("Cupom não pertence a esta conta.")
    marketplace = (request.POST.get("marketplace") or "").strip().lower()
    if marketplace not in {"mercadolivre", "amazon", "shopee", "awin"}:
        messages.error(request, "Escolha uma loja conectada.")
        return redirect("scraper-top")
    original_url = (request.POST.get("url") or "").strip()
    if not _url_manual_valida(marketplace, original_url):
        messages.error(request, "Informe uma URL HTTPS válida da loja selecionada.")
        return redirect("scraper-top")
    code = (request.POST.get("codigo") or "").strip()[:120]
    title = (request.POST.get("titulo") or "").strip()[:255]
    if not title:
        messages.error(request, "Informe um título para o cupom.")
        return redirect("scraper-top")
    integration = program = None
    affiliate_url = original_url
    state = "ativo"
    if marketplace == "awin":
        try:
            program_id = int(request.POST.get("programa") or 0)
        except (TypeError, ValueError):
            program_id = 0
        program = ProgramaAfiliado.objects.select_related("integracao").filter(
            pk=program_id, integracao__owner=request.user,
            integracao__provedor="awin", habilitado=True,
            status_vinculo="joined", link_status="online").first()
        if not program or not url_permitida(program, original_url):
            messages.error(request, "A URL não pertence ao anunciante Awin escolhido.")
            return redirect("scraper-top")
        integration = program.integracao
        try:
            affiliate_url = gerar_deeplink(integration, program, original_url)
        except AwinError as exc:
            state = "rascunho"
            messages.warning(request, f"Cupom salvo como rascunho: {exc.public_message}")

    source, _ = FonteIngestao.objects.get_or_create(
        slug="manual-private",
        defaults={"marketplace": "multiloja", "nome": "Cupons privados do afiliado",
                  "status": "ok", "habilitada": True})
    rules = normalizar_regras_cupom({
        "tipo_desconto": request.POST.get("tipo_desconto"),
        "valor_desconto": request.POST.get("valor_desconto"),
        "valor_minimo": request.POST.get("valor_minimo"),
        "desconto_maximo": request.POST.get("desconto_maximo"),
        "modo_resgate": "codigo" if code else "ativacao",
        "escopo": (request.POST.get("condicoes") or "").strip()[:500],
        "dia_inicio": request.POST.get("inicio"), "dia_fim": request.POST.get("validade"),
    }, external_id=coupon.external_id if coupon else "manual", codigo=code)
    values = {
        "owner": request.user, "integracao": integration, "programa": program,
        "marketplace": marketplace, "tipo_conteudo": "voucher" if code else "promotion",
        "anunciante_nome": program.nome if program else (
            "Mercado Livre" if marketplace == "mercadolivre" else "Amazon"),
        "titulo": title, "codigo": code, "regras": rules,
        "categoria": derivar_categoria_cupom(title, rules), "link": affiliate_url[:1000],
        "inicio": _data_form_aware(request.POST.get("inicio")),
        "validade": _data_form_aware(request.POST.get("validade"), fim=True),
        "restrito": bool(request.POST.get("restrito")),
        "relampago": bool(request.POST.get("relampago")), "estado": state,
        "confianca": "media", "evidencia": {"manual": True, "url_original": original_url},
    }
    values.update(classificar_contrato_cupom(
        regras=rules,
        external_id=coupon.external_id if coupon else "manual",
        codigo=code,
        evidencia=values["evidencia"],
        categoria=values["categoria"],
        owner=request.user,
        data_scope="organization",
    ))
    if coupon:
        for field, value in values.items():
            setattr(coupon, field, value)
        coupon.save()
    else:
        coupon = CupomNormalizado.objects.create(
            fonte=source, external_id=f"manual:{uuid.uuid4().hex}", **values)
    from apps.scrapers.coupon_products import atualizar_chave_cupom
    atualizar_chave_cupom(coupon)
    if state == "ativo":
        messages.success(request, "Cupom salvo e enviado para validação automática de produtos.")
    return redirect("scraper-top")


@require_POST
def cupom_manual_desativar(request, cupom_id):
    updated = CupomNormalizado.objects.filter(
        pk=cupom_id, owner=request.user, fonte__slug="manual-private").update(estado="inativo")
    if not updated:
        raise PermissionDenied("Cupom não pertence a esta conta.")
    messages.success(request, "Cupom privado desativado.")
    return redirect("scraper-top")


def _wa_session(request):
    """Sessão WhatsApp DESTE usuário (multi-tenant). Cada um pareia a própria conta."""
    perfil = getattr(request.user, "perfil", None)
    return perfil.sessao_whatsapp() if perfil else str(request.user.id)


def whatsapp_painel(request):
    """Tela de conexão do WhatsApp: status + QR Code para parear pelo navegador.

    O GET não revive a sessão: consultar não pode ter efeito colateral (era a
    metade "otimista" da divergência com a Saúde). Reviver segue existindo, mas
    como intenção explícita: o front dá POST em whatsapp/iniciar/ quando vê uma
    fase terminal.
    """
    from apps.scrapers import whatsapp_client
    session = _wa_session(request)
    return render(request, "scrapers/whatsapp.html", {
        "status": whatsapp_client.status(session),
    })


@require_GET
def whatsapp_status_json(request):
    """JSON de status para polling do front."""
    from apps.scrapers import whatsapp_client
    try:
        return JsonResponse(whatsapp_client.status(_wa_session(request)))
    except whatsapp_client.WhatsAppError as e:
        # Falha de assinatura/RLS não é worker fora do ar: devolve o motivo real
        # no contrato {"erro": ...} em vez de vazar 500 para o front.
        logger.warning("status WhatsApp sem autorização: %s", e)
        return JsonResponse({"erro": str(e)})


@require_POST
def whatsapp_refresh_grupos(request):
    """Força re-sincronização da lista de grupos no Node e devolve o resultado.

    POST porque dispara trabalho pesado (getChats no Chromium, 45s de timeout).
    Em GET a rota ficava sem proteção CSRF — acionável por um <img src> de
    qualquer site — e sujeita a pré-fetch do navegador. Espelha
    whatsapp_desconectar e o POST /api/grupos/refresh do próprio Node.
    """
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.refresh_grupos(_wa_session(request)))


@require_GET
def whatsapp_grupos_json(request):
    """Lista grupos (GET leve) para o front carregar via AJAX sem travar o render."""
    from apps.scrapers import whatsapp_client
    try:
        return JsonResponse(whatsapp_client.listar_grupos(_wa_session(request)))
    except whatsapp_client.WhatsAppError as e:
        # Idem whatsapp_status_json: erro de chave vira {"erro": ...} com o
        # motivo real, não um 500 indistinguível de worker morto.
        logger.warning("grupos WhatsApp sem autorização: %s", e)
        return JsonResponse({"erro": str(e)})


@require_POST
def whatsapp_iniciar(request):
    """Revive/inicia a sessão de WhatsApp deste usuário.

    POST /api/sessoes é o único caminho que tira uma sessão de fase terminal no
    worker Node (expirado, falha_auth, recuperacao_pausada, ausente do Map).
    Antes isso acontecia como efeito colateral do GET da tela — o que a tornava
    otimista e divergente da Saúde. POST espelha whatsapp_desconectar (CSRF).
    """
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.iniciar_sessao(_wa_session(request)))


@require_POST
def whatsapp_desconectar(request):
    """Desfaz o pareamento do WhatsApp deste usuário (espelha telegram_desconectar)."""
    from apps.scrapers import whatsapp_client
    return JsonResponse(whatsapp_client.desconectar(_wa_session(request)))


@require_POST
def whatsapp_cancelar_reconexao(request):
    """Aborta a recuperação em curso e começa do zero, com QR novo.

    Saída manual do loop de reconexão: o worker tenta 6 vezes, purga a
    credencial, tenta de novo e para numa fase terminal — e cada F5 na tela
    reviveu esse mesmo ciclo. Sem este botão o usuário não tinha como interromper
    (o "Desconectar" só aparece conectado, que é justamente o estado que falta).

    O worker executa a transição atomicamente para que o polling não consiga
    reviver a credencial antiga entre a limpeza e a criação da sessão nova.
    """
    from apps.scrapers import whatsapp_client
    session = _wa_session(request)
    return JsonResponse(whatsapp_client.reiniciar_com_qr(session))


# --- Conexão web do Mercado Livre (login via browser remoto, sem script local) ---

def ml_conexao_painel(request):
    """Tela de conexão do ML: o usuário loga no ML dentro de um live view embutido."""
    from apps.scrapers import ml_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": ml_conexao.status(request.user.id),
        "marketplace_nome": "Mercado Livre", "conexao_prefix": "/scrapers/ml",
        # Só escolhe os textos da tela (ML x Amazon). O transporte é o mesmo nos três
        # fluxos desde que a Amazon saiu do screencast legado.
        "marketplace_ml": True,
    })


@require_GET
def ml_conexao_status_json(request):
    """JSON de status para polling do front (fase, live_view_url, auth_valido)."""
    from apps.scrapers import ml_conexao
    return JsonResponse(ml_conexao.status(request.user.id))


def amazon_conexao_painel(request):
    """Login interativo da Amazon Associates, exclusivo para relatórios."""
    from apps.scrapers import amazon_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": amazon_conexao.status(request.user.id),
        "marketplace_nome": "Amazon Associados", "conexao_prefix": "/scrapers/amazon",
        "relatorio": True, "marketplace_ml": False,
    })


@require_GET
def amazon_conexao_status_json(request):
    from apps.scrapers import amazon_conexao
    return JsonResponse(amazon_conexao.status(request.user.id))


@require_POST
def amazon_conexao_start(request):
    from apps.scrapers import amazon_conexao
    import json
    if len(request.body or b"") > 4096:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        client = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(amazon_conexao.criar_sessao(request.user, client))


@require_POST
def amazon_conexao_salvar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.salvar_agora(request.user.id)
    return JsonResponse(amazon_conexao.status(request.user.id))


@require_POST
def amazon_conexao_cancelar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.cancelar(request.user.id)
    return JsonResponse({"ok": True})


# Um stream de frames prende UMA das 8 threads do gunicorn enquanto a aba
# estiver aberta (--workers 1 --threads 8, ver python/Procfile). Sem teto,
# recarregar a tela algumas vezes esgotava o pool e o painel inteiro parava
# de responder — os streams de raspagem/envio já eram limitados, estes não.
@throttle_sse(6)
@require_GET
def amazon_conexao_frames(request):
    from apps.scrapers import amazon_conexao
    return _resposta_login_sse(amazon_conexao.frames(
        request.user.id, request.GET.get("session_id"),
    ))


@require_POST
def amazon_conexao_input(request):
    import json
    from apps.scrapers import amazon_conexao
    if len(request.body or b"") > 65536:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(amazon_conexao.enfileirar_input(
        request.user.id, payload.get("session_id"), payload.get("events"),
    ))


# --- Conexão do portal de RELATÓRIOS do ML (afiliados), separada do site principal ---

def amazon_shop_conexao_painel(request):
    """Sessão da loja Amazon usada somente para validar cupom sem comprar."""
    from apps.scrapers import amazon_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": amazon_conexao.status(request.user.id, shopper=True),
        "marketplace_nome": "Amazon Compras",
        "conexao_prefix": "/scrapers/amazon-shop",
        "checkout_validation": True, "marketplace_ml": False,
    })


@require_GET
def amazon_shop_conexao_status_json(request):
    from apps.scrapers import amazon_conexao
    return JsonResponse(amazon_conexao.status(request.user.id, shopper=True))


@require_POST
def amazon_shop_conexao_start(request):
    from apps.scrapers import amazon_conexao
    import json
    if len(request.body or b"") > 4096:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        client = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(amazon_conexao.criar_sessao(
        request.user, client, shopper=True,
    ))


@require_POST
def amazon_shop_conexao_salvar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.salvar_agora(request.user.id, shopper=True)
    return JsonResponse(amazon_conexao.status(request.user.id, shopper=True))


@require_POST
def amazon_shop_conexao_cancelar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.cancelar(request.user.id, shopper=True)
    return JsonResponse({"ok": True})


@require_POST
def amazon_shop_conexao_desconectar(request):
    from apps.scrapers import amazon_conexao
    from apps.scrapers.report_sessions import delete_report_state
    amazon_conexao.cancelar(request.user.id, shopper=True)
    delete_report_state(request.user, "amazon_shop")
    return JsonResponse({"ok": True})


@throttle_sse(6)
@require_GET
def amazon_shop_conexao_frames(request):
    from apps.scrapers import amazon_conexao
    return _resposta_login_sse(amazon_conexao.frames(
        request.user.id, request.GET.get("session_id"), shopper=True,
    ))


@require_POST
def amazon_shop_conexao_input(request):
    import json
    from apps.scrapers import amazon_conexao
    if len(request.body or b"") > 65536:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(amazon_conexao.enfileirar_input(
        request.user.id, payload.get("session_id"), payload.get("events"),
        shopper=True,
    ))


def shopee_shop_conexao_painel(request):
    """Sessão da Shopee usada exclusivamente para validar cupom sem comprar."""
    from apps.scrapers import amazon_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": amazon_conexao.status(request.user.id, shopper="shopee"),
        "marketplace_nome": "Shopee Compras",
        "conexao_prefix": "/scrapers/shopee-shop",
        "checkout_validation": True, "marketplace_ml": False,
    })


@require_GET
def shopee_shop_conexao_status_json(request):
    from apps.scrapers import amazon_conexao
    return JsonResponse(amazon_conexao.status(request.user.id, shopper="shopee"))


@require_POST
def shopee_shop_conexao_start(request):
    from apps.scrapers import amazon_conexao
    import json
    if len(request.body or b"") > 4096:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        client = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(amazon_conexao.criar_sessao(
        request.user, client, shopper="shopee",
    ))


@require_POST
def shopee_shop_conexao_salvar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.salvar_agora(request.user.id, shopper="shopee")
    return JsonResponse(amazon_conexao.status(request.user.id, shopper="shopee"))


@require_POST
def shopee_shop_conexao_cancelar(request):
    from apps.scrapers import amazon_conexao
    amazon_conexao.cancelar(request.user.id, shopper="shopee")
    return JsonResponse({"ok": True})


@require_POST
def shopee_shop_conexao_desconectar(request):
    from apps.scrapers import amazon_conexao
    from apps.scrapers.report_sessions import delete_report_state
    amazon_conexao.cancelar(request.user.id, shopper="shopee")
    delete_report_state(request.user, "shopee_shop")
    return JsonResponse({"ok": True})


@throttle_sse(6)
@require_GET
def shopee_shop_conexao_frames(request):
    from apps.scrapers import amazon_conexao
    return _resposta_login_sse(amazon_conexao.frames(
        request.user.id, request.GET.get("session_id"), shopper="shopee",
    ))


@require_POST
def shopee_shop_conexao_input(request):
    import json
    from apps.scrapers import amazon_conexao
    if len(request.body or b"") > 65536:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(amazon_conexao.enfileirar_input(
        request.user.id, payload.get("session_id"), payload.get("events"),
        shopper="shopee",
    ))


def ml_relatorio_conexao_painel(request):
    """Login interativo no portal de afiliados do ML, exclusivo para relatórios."""
    from apps.scrapers import ml_relatorio_conexao
    return render(request, "scrapers/ml_conexao.html", {
        "status": ml_relatorio_conexao.status(request.user.id),
        "marketplace_nome": "Relatórios Mercado Livre",
        "conexao_prefix": "/scrapers/ml-relatorio",
        "relatorio": True, "marketplace_ml": True,
    })


@require_GET
def ml_relatorio_conexao_status_json(request):
    from apps.scrapers import ml_relatorio_conexao
    return JsonResponse(ml_relatorio_conexao.status(request.user.id))


@require_POST
def ml_relatorio_conexao_start(request):
    from apps.scrapers import ml_relatorio_conexao
    import json
    if len(request.body or b"") > 4096:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        client = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(ml_relatorio_conexao.criar_sessao(request.user, client))


@require_POST
def ml_relatorio_conexao_qr_retry(request):
    """Retoma o portal na mesma thread depois que o titular ativa o QR."""
    from apps.scrapers import ml_relatorio_conexao

    ok, payload = ml_relatorio_conexao.retentar_apos_configurar_qr(request.user.id)
    return JsonResponse(payload, status=200 if ok else 409)


@require_POST
def ml_relatorio_conexao_salvar(request):
    from apps.scrapers import ml_relatorio_conexao
    ml_relatorio_conexao.salvar_agora(request.user.id)
    return JsonResponse(ml_relatorio_conexao.status(request.user.id))


@require_POST
def ml_relatorio_conexao_cancelar(request):
    from apps.scrapers import ml_relatorio_conexao
    ml_relatorio_conexao.cancelar(request.user.id)
    return JsonResponse({"ok": True})


# Um stream de frames prende UMA das 8 threads do gunicorn enquanto a aba
# estiver aberta (--workers 1 --threads 8, ver python/Procfile). Sem teto,
# recarregar a tela algumas vezes esgotava o pool e o painel inteiro parava
# de responder — os streams de raspagem/envio já eram limitados, estes não.
@throttle_sse(6)
@require_GET
def ml_relatorio_conexao_frames(request):
    from apps.scrapers import ml_relatorio_conexao
    return _resposta_login_sse(ml_relatorio_conexao.frames(
        request.user.id, request.GET.get("session_id"),
    ))


@require_POST
def ml_relatorio_conexao_input(request):
    import json
    from apps.scrapers import ml_relatorio_conexao
    if len(request.body or b"") > 65536:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        payload = json.loads((request.body or b"").decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(ml_relatorio_conexao.enfileirar_input(
        request.user.id, payload.get("session_id"), payload.get("events"),
    ))


@require_POST
def ml_conexao_start(request):
    """Abre (ou reaproveita) a sessão remota de login do ML e devolve o estado."""
    import json
    from apps.scrapers import ml_conexao
    if len(request.body or b"") > 4096:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        client = json.loads((request.body or b"{}").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(ml_conexao.criar_sessao(request.user, client))


@require_POST
def ml_conexao_qr_retry(request):
    """Retoma o login na mesma thread depois que o titular ativa o QR."""
    from apps.scrapers import ml_conexao

    ok, payload = ml_conexao.retentar_apos_configurar_qr(request.user.id)
    return JsonResponse(payload, status=200 if ok else 409)


@require_POST
def ml_conexao_salvar(request):
    """Dispara a sonda autenticada; nunca força a gravação de cookies incompletos."""
    from apps.scrapers import ml_conexao
    ml_conexao.salvar_agora(request.user.id)
    return JsonResponse(ml_conexao.status(request.user.id))


@require_POST
def ml_conexao_cancelar(request):
    """Cancela a sessão de login em andamento."""
    from apps.scrapers import ml_conexao
    ml_conexao.cancelar(request.user.id)
    return JsonResponse({"ok": True})


@require_POST
def ml_conexao_desconectar(request):
    """Apaga a sessão salva do ML (espelha whatsapp_desconectar).

    Faltava a contraparte do "Desconectar" do WhatsApp: quando a sessão ficava
    presa em "conectado" — a sonda aceitando um cookie que os fluxos reais já não
    conseguiam usar — não havia nenhuma saída manual. Apagar aqui é a única forma
    de forçar um login novo de verdade, já que `criar_sessao` reaproveita o que
    estiver no banco.
    """
    from apps.accounts.ml_sessions import delete_storage_state
    from apps.scrapers import conexoes, ml_conexao

    ml_conexao.cancelar(request.user.id)
    apagou = delete_storage_state(request.user)
    conexoes.invalidar_ml(request.user)
    ml_conexao.esquecer(request.user.id)
    return JsonResponse({
        "ok": True,
        "apagou": bool(apagou),
        "mensagem": ("Sessão do Mercado Livre apagada. Clique em Conectar para "
                     "entrar de novo." if apagou
                     else "Não havia sessão salva do Mercado Livre."),
    })


# Um stream de frames prende UMA das 8 threads do gunicorn enquanto a aba
# estiver aberta (--workers 1 --threads 8, ver python/Procfile). Sem teto,
# recarregar a tela algumas vezes esgotava o pool e o painel inteiro parava
# de responder — os streams de raspagem/envio já eram limitados, estes não.
@throttle_sse(6)
@require_GET
def ml_conexao_frames(request):
    """SSE — transmite os frames (JPEG base64) do Chromium local pro <canvas> do front.

    Live view self-hosted: o worker publica capturas JPEG determinísticas e numeradas;
    aqui só empurramos o frame atual de CADA usuário (isolado por request.user.id e
    pelo session_id opaco — um tenant nunca vê a tela do outro)."""
    from apps.scrapers import ml_conexao
    return _resposta_login_sse(ml_conexao.frames(
        request.user.id, request.GET.get("session_id"),
    ))


@require_POST
def ml_conexao_input(request):
    """Recebe eventos de mouse/teclado do front e encaminha pro browser de login.

    Body JSON: {"session_id":"...", "events":[{"seq":1,"t":"click",...}]}.
    A validação, ordenação, deduplicação e os limites ficam no transporte compartilhado."""
    import json
    from apps.scrapers import ml_conexao
    if len(request.body or b"") > 65536:
        return JsonResponse({"ok": False, "erro": "payload_muito_grande"}, status=413)
    try:
        payload = json.loads((request.body or b"").decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "erro": "json_invalido"}, status=400)
    return JsonResponse(ml_conexao.enfileirar_input(
        request.user.id, payload.get("session_id"), payload.get("events"),
    ))


@require_GET
def whatsapp_qr_png(request):
    """Renderiza o QR do WhatsApp como PNG (vindo do serviço Node)."""
    import qrcode
    from io import BytesIO
    from apps.scrapers import whatsapp_client

    info = whatsapp_client.qrcode(_wa_session(request))
    qr = info.get("qr")
    if not qr:
        # 204 = sem QR (já conectado ou ainda gerando)
        return HttpResponse(status=204)
    buf = BytesIO()
    qrcode.make(qr).save(buf, format="PNG")
    return HttpResponse(buf.getvalue(), content_type="image/png")


def telegram_painel(request):
    """Tela de conexão do Telegram: o usuário cola o token do próprio bot (via web)."""
    return render(request, "scrapers/telegram.html")


def _token_telegram_valido(token: str) -> bool:
    """Formato canônico do BotFather: <id numérico>:<segredo>. Rejeita qualquer coisa
    com '/', espaço ou caracteres fora do alfabeto — evita truques de path na URL do
    getMe (f'.../bot{token}/getMe') e chamadas malformadas."""
    import re
    return bool(re.fullmatch(r"\d{3,}:[A-Za-z0-9_-]{30,}", token or ""))


def _telegram_getme(token: str) -> dict:
    """Valida um token via getMe (só HTTP, sem browser). Não levanta."""
    import requests as _rq
    if not token:
        return {"token": False, "ok": False}
    if not _token_telegram_valido(token):
        return {"token": True, "ok": False, "erro": "Formato de token inválido."}
    try:
        r = _rq.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8)
        d = r.json()
        if d.get("ok"):
            info = d.get("result", {})
            return {"token": True, "ok": True,
                    "username": info.get("username"), "nome": info.get("first_name")}
        return {"token": True, "ok": False, "erro": d.get("description") or "getMe falhou"}
    except Exception as e:
        return {"token": True, "ok": False, "erro": str(e)}


@require_GET
def telegram_status_json(request):
    """Status do bot do usuário (token no Perfil; fallback global) via getMe."""
    from apps.scrapers.senders.telegram import resolver_token
    return JsonResponse(_telegram_getme(resolver_token(request.user)))


@require_POST
def telegram_conectar(request):
    """Salva o token do bot do usuário no Perfil — depois de validar via getMe."""
    token = (request.POST.get("token") or "").strip()
    if not token:
        return JsonResponse({"ok": False, "erro": "Cole o token do seu bot."}, status=400)
    res = _telegram_getme(token)
    if not res.get("ok"):
        return JsonResponse({"ok": False, "erro": res.get("erro") or "Token inválido."}, status=400)
    perfil = request.user.perfil
    perfil.telegram_bot_token = token
    perfil.save(update_fields=["telegram_bot_token"])
    return JsonResponse({"ok": True, **res})


@require_POST
def telegram_desconectar(request):
    """Remove o token do bot do usuário."""
    perfil = request.user.perfil
    perfil.telegram_bot_token = ""
    perfil.save(update_fields=["telegram_bot_token"])
    return JsonResponse({"ok": True})


# Sanidade contra POST forjado, não limite de produto: marcar TODOS os 70
# sub-nichos dos 17 macro-nichos de SUBNICHOS soma 2129 caracteres, e o maior
# macro-nicho sozinho (Eletrodomésticos) soma 395. A folga é de propósito — a
# taxonomia cresce, e o usuário não pode voltar a esbarrar num teto invisível.
LIMITE_TERMOS_POR_REGRA = 8000

# Dias da semana em ISO (1=segunda … 7=domingo), na ordem em que o Brasil lê a
# semana. Fonte única do formulário e do rótulo da listagem.
DIAS_SEMANA_OPCOES = [(1, "Seg"), (2, "Ter"), (3, "Qua"), (4, "Qui"),
                      (5, "Sex"), (6, "Sáb"), (7, "Dom")]


def _dias_semana_do_post(request) -> str:
    """CSV normalizado de dias ISO a partir do POST. '' = todos os dias.

    Marcar os sete equivale a não marcar nenhum, e gravar '' nesse caso mantém a
    coluna com um único valor para "sem restrição" — o resto do código só precisa
    conhecer uma forma de dizer isso.
    """
    escolhidos = set()
    for bruto in request.POST.getlist("dias_semana"):
        bruto = str(bruto or "").strip()
        if bruto.isdigit() and 1 <= int(bruto) <= 7:
            escolhidos.add(int(bruto))
    if len(escolhidos) == 7:
        return ""
    return ",".join(str(d) for d in sorted(escolhidos))


def _rotulo_dias(cfg) -> str:
    dias = cfg.dias_permitidos()
    if not dias:
        return "todos os dias"
    if dias == {1, 2, 3, 4, 5}:
        return "seg a sex"
    if dias == {6, 7}:
        return "fins de semana"
    return ", ".join(rotulo for valor, rotulo in DIAS_SEMANA_OPCOES if valor in dias)


def configuracoes(request):
    """Painel do afiliado: cria/edita/remove regras de divulgação (nicho→grupo→intervalo)."""
    if request.method == "POST":
        acao = request.POST.get("acao")
        if acao == "delete":
            # Só apaga regra do próprio usuário (isolamento multi-tenant).
            ConfiguracaoEnvio.objects.filter(
                id=request.POST.get("id"), owner=request.user).delete()
        elif acao == "perfil":
            # Identidade de afiliado + credenciais Amazon por-usuário (via web, não .env).
            perfil = request.user.perfil
            perfil.afiliado_tag_ml = (request.POST.get("afiliado_tag_ml") or "").strip()
            perfil.afiliado_tag_amazon = (request.POST.get("afiliado_tag_amazon") or "").strip()
            perfil.amazon_credential_id = (request.POST.get("amazon_credential_id") or "").strip()
            perfil.amazon_creators_host = (request.POST.get("amazon_creators_host") or "").strip()
            # Secret só sobrescreve se o usuário digitou algo (campo vem mascarado/vazio).
            novo_secret = (request.POST.get("amazon_credential_secret") or "").strip()
            campos = ["afiliado_tag_ml", "afiliado_tag_amazon",
                      "amazon_credential_id", "amazon_creators_host"]
            if novo_secret:
                perfil.amazon_credential_secret = novo_secret
                campos.append("amazon_credential_secret")
            perfil.save(update_fields=campos)
        else:
            cfg_id = request.POST.get("id")
            # Sub-nichos: multi-select -> junta as strings de termos (OR no filtro)
            termos = [t.strip() for t in request.POST.getlist("termo_busca") if t.strip()]
            termo_busca = ", ".join(termos)
            # `termo_busca` é TextField: escolher muitos sub-nichos numa regra só é
            # o uso esperado, não um erro. O teto abaixo não é o da coluna — é só
            # sanidade contra POST forjado, já que a tela monta a lista a partir de
            # uma taxonomia fixa. Fica largo o bastante para marcar todos os
            # sub-nichos de todos os macro-nichos sem esbarrar nele.
            if len(termo_busca) > LIMITE_TERMOS_POR_REGRA:
                messages.error(
                    request,
                    "A lista de sub-nichos desta regra é longa demais "
                    f"({len(termo_busca)} caracteres). Divida em duas regras para "
                    "o mesmo grupo.")
                return redirect("scraper-configuracoes")
            canal = (request.POST.get("canal") or "whatsapp").strip()
            if canal not in {"whatsapp", "telegram"}:
                messages.error(request, "Canal de envio inválido.")
                return redirect("scraper-configuracoes")
            # Telegram usa o campo de chat_id digitado; WhatsApp usa o grupo escolhido.
            grupo_id = (request.POST.get("telegram_chat_id") if canal == "telegram"
                        else request.POST.get("grupo_id")) or ""
            grupo_id = grupo_id.strip()
            if not grupo_id:
                messages.error(request, "Escolha ou informe um grupo de destino.")
                return redirect("scraper-configuracoes")
            # O destino não pode ser truncado — mandaria a promoção para um chat que
            # não é o pedido. Fora do tamanho, é erro de digitação: recusa e avisa.
            if len(grupo_id) > ConfiguracaoEnvio._meta.get_field("grupo_id").max_length:
                messages.error(request, "O identificador do grupo de destino é longo demais.")
                return redirect("scraper-configuracoes")
            try:
                intervalo = int(request.POST.get("intervalo_minutos") or 60)
                janela_inicio = int(request.POST.get("janela_inicio") or 8)
                janela_fim = int(request.POST.get("janela_fim") or 20)
                desconto = float(request.POST.get("min_desconto_percent") or 15)
                max_envios_dia = int(request.POST.get("max_envios_dia") or 20)
                pausar_apos_falhas = int(request.POST.get("pausar_apos_falhas") or 5)
            except (TypeError, ValueError):
                messages.error(request, "Intervalo, horários ou desconto possuem valor inválido.")
                return redirect("scraper-configuracoes")
            if intervalo < 1 or not (0 <= janela_inicio <= 23 and 0 <= janela_fim <= 23):
                messages.error(request, "Use intervalo positivo e horários entre 0 e 23.")
                return redirect("scraper-configuracoes")
            if not (0 <= desconto <= 100):
                messages.error(request, "O desconto mínimo deve ficar entre 0% e 100%.")
                return redirect("scraper-configuracoes")
            if max_envios_dia < 1 or pausar_apos_falhas < 1:
                messages.error(request, "Limites diários e de falhas devem ser positivos.")
                return redirect("scraper-configuracoes")
            tipo_regra = (
                ConfiguracaoEnvio.TIPO_AVISO_CUPONS
                if request.POST.get("tipo") == ConfiguracaoEnvio.TIPO_AVISO_CUPONS
                else ConfiguracaoEnvio.TIPO_OFERTAS
            )
            marketplace_regra = (request.POST.get("marketplace") or "").strip()[:20]
            if (tipo_regra == ConfiguracaoEnvio.TIPO_AVISO_CUPONS
                    and marketplace_regra not in {"mercadolivre", "amazon", "shopee"}):
                messages.error(
                    request,
                    "Escolha Mercado Livre, Amazon ou Shopee para o aviso de cupons. "
                    "Crie uma regra por loja para manter o envio previsível.",
                )
                return redirect("scraper-configuracoes")
            campos = dict(
                macro_categoria=request.POST.get("macro_categoria", "").strip()[:100],
                termo_busca=termo_busca,
                canal=canal,
                marketplace=marketplace_regra,
                grupo_id=grupo_id,
                grupo_nome=request.POST.get("grupo_nome", "").strip()[:255],
                intervalo_minutos=intervalo,
                janela_inicio=janela_inicio,
                janela_fim=janela_fim,
                dias_semana=_dias_semana_do_post(request),
                tipo=tipo_regra,
                min_desconto_percent=desconto,
                max_envios_dia=max_envios_dia,
                pausar_apos_falhas=pausar_apos_falhas,
                variante_template=(request.POST.get("variante_template") or "alternar"),
                nome_marca=(request.POST.get("nome_marca") or "").strip()[:80],
                tom_marca=(request.POST.get("tom_marca") or "").strip()[:20],
                chamada_acao=(request.POST.get("chamada_acao") or "").strip()[:120],
                divulgacao_afiliado=(request.POST.get("divulgacao_afiliado") or "").strip()[:180],
                template_a=(request.POST.get("template_a") or "").strip(),
                template_b=(request.POST.get("template_b") or "").strip(),
                incluir_restritos=bool(request.POST.get("incluir_restritos")),
                incluir_sem_desconto=bool(request.POST.get("incluir_sem_desconto")),
                ativo=bool(request.POST.get("ativo")),
                # Salvar a regra é ação humana deliberada: solta o freio automático e
                # zera o contador. Sem isto, corrigir o que causou as falhas não
                # bastava — a regra continuava dormindo até o prazo vencer.
                pausada_ate=None,
                motivo_pausa="",
                falhas_consecutivas=0,
            )
            program_ids = list(ProgramaAfiliado.objects.filter(
                id__in=request.POST.getlist("programas"),
                integracao__owner=request.user, habilitado=True,
            ).values_list("id", flat=True))
            if cfg_id:
                # update() não dispara validação, mas o filtro por owner garante posse.
                ConfiguracaoEnvio.objects.filter(id=cfg_id, owner=request.user).update(**campos)
                cfg_obj = ConfiguracaoEnvio.objects.filter(id=cfg_id, owner=request.user).first()
                if cfg_obj:
                    cfg_obj.programas.set(program_ids)
            else:
                # Cota de regras por usuário (protege a máquina compartilhada).
                perfil = getattr(request.user, "perfil", None)
                limite = perfil.cota_max_configs() if perfil else 0
                atuais = ConfiguracaoEnvio.objects.filter(owner=request.user).count()
                if limite and atuais >= limite:
                    messages.error(
                        request,
                        f"Limite de {limite} regras atingido. Remova uma ou peça mais ao suporte.")
                    return redirect("scraper-configuracoes")
                cfg_obj = ConfiguracaoEnvio.objects.create(owner=request.user, **campos)
                cfg_obj.programas.set(program_ids)
        return redirect("scraper-configuracoes")

    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import SUBNICHOS
    subnichos = [{"macro": m, "itens": [{"label": l, "termos": t} for l, t in itens]}
                 for m, itens in SUBNICHOS.items()]

    from apps.scrapers.marketplaces.registry import MARKETPLACES
    from apps.scrapers.senders.registry import SENDERS

    configs_qs = ConfiguracaoEnvio.objects.filter(owner=request.user).prefetch_related(
        "programas").order_by("macro_categoria")
    configs = list(configs_qs)
    # Esta tela precisa da taxonomia editorial, não de um DISTINCT sobre todo o
    # catálogo acumulado. Em produção o primeiro GET gastava dezenas de segundos
    # só para montar o select.
    # Preserva também macros de regras antigas que não estejam mais no dicionário.
    macros = sorted(
        set(SUBNICHOS)
        | {config.macro_categoria for config in configs if config.macro_categoria}
    )
    # Grupos do WhatsApp NÃO são buscados aqui: a chamada ao Node pode travar o render
    # (até 15s) quando o serviço está offline. Carregados via AJAX (ver whatsapp_grupos_json).
    from apps.scrapers.content_ranking import previa_melhor_conteudo
    from apps.scrapers.ofertas import selecionar_cupons_para_aviso
    for config in configs:
        config.rotulo_dias = _rotulo_dias(config)
        if config.tipo == ConfiguracaoEnvio.TIPO_AVISO_CUPONS:
            # A prévia do ranking de ofertas não descreve este tipo de regra. O que
            # importa aqui é quantos códigos novos entrariam na próxima mensagem.
            config.previa_conteudo = None
            novos = selecionar_cupons_para_aviso(config, request.user)
            config.previa_aviso = (
                f"{len(novos)} cupom(ns) com código novo: "
                + ", ".join(c.codigo for c in novos[:4])
                + ("…" if len(novos) > 4 else "")) if novos else ""
            continue
        config.previa_conteudo = previa_melhor_conteudo(config) if config.ativo else None
        config.previa_aviso = ""
    return render(request, "scrapers/configuracoes.html", {
        "configs": configs,
        "dias_semana_opcoes": DIAS_SEMANA_OPCOES,
        "tipos_regra": ConfiguracaoEnvio.TIPOS,
        "macros": macros,
        "subnichos": subnichos,
        "marketplaces": list(MARKETPLACES.keys()),
        "canais": list(SENDERS.keys()),
        "perfil": request.user.perfil,
        "pode_ligar_envio": pode_ligar_envio(request.user),
        "awin_programas": ProgramaAfiliado.objects.filter(
            integracao__owner=request.user, integracao__status="conectada",
            habilitado=True, status_vinculo="joined", link_status="online").order_by("nome"),
    })


@require_POST
@throttle_sse(6)
def enviar_agora_stream(request):
    """SSE via POST — dispara um envio de teste para uma ConfiguracaoEnvio."""
    from apps.scrapers.ofertas import selecionar_e_enviar

    try:
        cfg_id = int(request.POST.get("config") or 0)
    except (TypeError, ValueError):
        cfg_id = 0
    uid = request.user.id  # capturado fora da thread (request.user não cruza thread)

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            # Nada de bypass de ORM aqui: quem precisa de query com o Playwright
            # aberto usa apps.accounts.tenant.executar_no_tenant (o porquê está lá).
            try:
                with redirect_stdout(writer):
                    # Só a própria regra do usuário (isolamento multi-tenant).
                    cfg = ConfiguracaoEnvio.objects.filter(id=cfg_id, owner_id=uid).first()
                    if not cfg:
                        print("[ERRO] Configuração não encontrada.")
                        return
                    macros = [cfg.macro_categoria] if cfg.macro_categoria else None
                    alvo = cfg.termo_busca or cfg.macro_categoria or 'qualquer/ofertas'
                    print(f"Selecionando item de '{alvo}'...")
                    r = selecionar_e_enviar(
                        macros, cfg.grupo_id,
                        min_desconto_percent=cfg.min_desconto_percent,
                        horas_cooldown=cfg.horas_cooldown,
                        verificar=True,
                        termo=cfg.termo_busca,
                        canal=getattr(cfg, "canal", "whatsapp"),
                        marketplace=getattr(cfg, "marketplace", "") or None,
                        usuario=cfg.owner,
                        configuracao=cfg,
                        destino_nome=cfg.grupo_nome,
                        enqueue_only=_send_pipeline_v2_enabled(cfg.owner),
                    )
                    if r.get("queued"):
                        print("__QUEUED__ OK Envio reservado; o worker fará a preparação e o transporte.")
                    elif r.get("sucesso"):
                        from django.utils import timezone
                        cfg.ultimo_envio = timezone.now()
                        cfg.save(update_fields=["ultimo_envio"])
                        print(f"OK Enviado (via {r.get('via')}). Link: {r.get('link')}")
                    else:
                        print(f"[ERRO] {r.get('motivo')}")
                        if r.get("precisa_login_ml"):
                            print("__ML_LOGIN__")
            except Exception as exc:
                logger.exception("Falha inesperada no envio de teste")
                q.put("[ERRO] Falha inesperada ao preparar o envio.")
            finally:
                writer.flush()
                q.put(None)

        thread = threading.Thread(
            target=organization_thread_target(request.organization.pk, _run),
            daemon=True,
        )
        thread.start()
        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _imagem_upload_b64(arquivo, max_bytes=5 * 1024 * 1024):
    """Foto anexada no envio -> base64 JPEG, ou None quando não houve upload.

    Arquivo presente e inválido levanta ``ValueError``: ignorá-lo silenciosamente
    fazia o usuário acreditar que a foto escolhida tinha sido enviada.
    """
    if not arquivo:
        return None
    try:
        if getattr(arquivo, "size", 0) and arquivo.size > max_bytes:
            raise ValueError("A foto excede o limite de 5 MiB.")
        import base64
        from io import BytesIO
        from PIL import Image
        img = Image.open(arquivo).convert("RGB")
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("A foto enviada não é uma imagem válida.") from exc


@require_POST
@throttle_sse(6)
def enviar_produto_stream(request):
    """SSE — envia UM produto específico (tela Promoções) p/ o destino escolhido no popup.

    Reusa enviar_oferta_de_produto -> grava HistoricoEnvio em sucesso, então o item
    fica permanentemente bloqueado p/ o envio automático (anti-repetição global).
    """
    from apps.scrapers.ofertas import enviar_oferta_de_produto
    try:
        prod_id = int(request.POST.get("produto") or 0)
    except (TypeError, ValueError):
        prod_id = 0
    grupo_id = (request.POST.get("grupo") or "").strip()[:100]
    grupo_nome = (request.POST.get("grupo_nome") or "").strip()[:255]
    canal = (request.POST.get("canal") or "whatsapp").strip().lower()
    # Foto opcional: lida AQUI (presa ao request), fora da thread _job.
    try:
        imagem_custom = _imagem_upload_b64(request.FILES.get("foto"))
        imagem_erro = ""
    except ValueError as exc:
        imagem_custom, imagem_erro = None, str(exc)
    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        from apps.accounts.tenant import executar_no_tenant
        # Tenant apenas ANOTADO (segurar_transacao=False): toda ida ao banco
        # reinstala o escopo numa transação curta via executar_no_tenant.
        if imagem_erro:
            print(f"[ERRO] {imagem_erro}")
            return
        usuario = executar_no_tenant(
            lambda: get_user_model().objects.filter(id=uid).first())
        if not usuario:
            print("[ERRO] Usuário não encontrado ou sessão encerrada.")
            return
        if not grupo_id:
            print("[ERRO] Nenhum destino informado (grupo/chat).")
            return
        # Isolamento multi-tenant: só o pool compartilhado (owner=None, ex: ML) ou
        # itens privados DESTE usuário (Amazon dele). Impede enviar item de outro dono.
        prod = executar_no_tenant(
            lambda: Produto.objects.filter(
                Q(owner__isnull=True) | Q(owner_id=uid), id=prod_id).first())
        if not prod:
            print("[ERRO] Produto não encontrado.")
            return
        from django.utils import timezone as _tz
        print(f"[{_tz.localtime().strftime('%H:%M:%S')}] Enviando "
              f"'{prod.nome[:60]}' → {grupo_nome or grupo_id} ({canal})…")
        if canal == "whatsapp":
            print("Subindo a imagem pelo WhatsApp Web; mídia leva alguns segundos.")
        try:
            r = enviar_oferta_de_produto(
                prod, grupo_id, verificar=True, canal=canal, usuario=usuario,
                destino_nome=grupo_nome, imagem_b64_custom=imagem_custom,
                enqueue_only=_send_pipeline_v2_enabled(usuario))
        except Exception as exc:
            # O núcleo re-levanta o inesperado para fechar a Publicacao; o SSE não pode
            # devolver o erro genérico do runner e esconder a regressão do log.
            logger.exception("Falha não tratada ao enviar produto %s", prod_id)
            from apps.scrapers.eventos import log_event
            try:
                executar_no_tenant(
                    log_event,
                    "publicacao", "offer_sse_failed",
                    "Não foi possível concluir o envio da oferta.", level="error",
                    usuario=usuario, contexto={"produto_id": prod_id, "canal": canal,
                                               "destino": grupo_nome or grupo_id,
                                               "causa": type(exc).__name__}, exc=exc,
                )
            except Exception:
                logger.exception("Não foi possível auditar falha SSE da oferta %s", prod_id)
            print("[ERRO] O envio encontrou uma falha temporária. Nada foi publicado; "
                  "tente novamente em instantes.")
            return
        if not isinstance(r, dict):
            logger.error("Envio do produto %s retornou resultado inválido: %r", prod_id, r)
            print("[ERRO] Não foi possível concluir o envio. Atualize a tela e tente novamente.")
            return
        if r.get("queued"):
            print("__QUEUED__ OK Envio reservado; acompanhe o resultado no painel de falhas.")
        elif r.get("sucesso"):
            print(f"__SENT__ OK {_resumo_do_envio(r)}")
        else:
            print(f"[ERRO] {r.get('motivo')}")
            if r.get("precisa_login_ml"):
                print("__ML_LOGIN__")  # a UI troca por um botão "Reconectar Mercado Livre"
            elif r.get("precisa_login_wa"):
                print("__WA_LOGIN__")  # a UI troca por "Reconectar WhatsApp"

    # segurar_transacao=False: gerar/verificar o link passa minutos no Link
    # Builder (Chromium) e uma transação aberta esse tempo todo vira
    # `idle in transaction` até o proxy da Fly matar o socket — dentro de
    # transação o Django não renova a conexão ("the connection is closed").
    # As idas ao banco vão por executar_no_tenant, aqui e dentro de
    # ofertas.enviar_oferta_de_produto.
    return _sse_runner(_job, request.organization, segurar_transacao=False)


@require_POST
@throttle_sse(6)
def enviar_cupom_stream(request):
    """SSE — envia um cupom afiliado, auditado e deduplicado por 24 horas."""
    from apps.scrapers.ofertas import enviar_cupom

    try:
        cupom_id = int(request.POST.get("cupom") or 0)
    except (TypeError, ValueError):
        cupom_id = 0
    grupo_id = (request.POST.get("grupo") or "").strip()[:100]
    grupo_nome = (request.POST.get("grupo_nome") or "").strip()[:255]
    canal = (request.POST.get("canal") or "whatsapp").strip().lower()
    try:
        imagem_custom = _imagem_upload_b64(request.FILES.get("foto"))
        imagem_erro = ""
    except ValueError as exc:
        imagem_custom, imagem_erro = None, str(exc)
    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        from apps.scrapers.coupon_rules import codigo_publicavel
        from apps.accounts.tenant import executar_no_tenant
        # Tenant apenas ANOTADO (segurar_transacao=False): toda ida ao banco
        # reinstala o escopo numa transação curta via executar_no_tenant.
        if imagem_erro:
            print(f"[ERRO] {imagem_erro}")
            return
        usuario = executar_no_tenant(
            lambda: get_user_model().objects.filter(id=uid).first())
        if not usuario:
            print("[ERRO] Usuário não encontrado ou sessão encerrada.")
            return
        if not grupo_id:
            print("[ERRO] Nenhum destino informado (grupo/chat).")
            return
        from apps.scrapers.maintenance import cupons_frescos_q
        from apps.scrapers.coupon_rules import cupons_visiveis_q
        from apps.accounts.models import organization_for_user
        organization = executar_no_tenant(organization_for_user, usuario)
        cupom = executar_no_tenant(
            lambda: CupomNormalizado.objects.filter(
                cupons_visiveis_q(usuario), id=cupom_id, estado="ativo",
                disponibilidades__organization=organization,
                disponibilidades__usuario=usuario,
                disponibilidades__channel=canal,
                disponibilidades__stage="ready",
            ).filter(cupons_frescos_q()).first())
        if not cupom:
            print("[ERRO] Cupom não encontrado, inativo ou ainda não disponível para envio.")
            return
        rotulo = codigo_publicavel(cupom) or "Ativar no link"
        print(f"Enviando cupom '{rotulo}' → {grupo_nome or grupo_id} ({canal})...")
        try:
            resultado = enviar_cupom(
                cupom, grupo_id, canal=canal, usuario=usuario, destino_nome=grupo_nome,
                imagem_b64_custom=imagem_custom,
                enqueue_only=_send_pipeline_v2_enabled(usuario))
        except Exception as exc:
            # O núcleo deve devolver um dict, mas o SSE não pode esconder uma
            # regressão nova atrás do erro genérico do runner.
            logger.exception("Falha não tratada ao enviar cupom %s", cupom_id)
            from apps.scrapers.eventos import log_event
            try:
                executar_no_tenant(
                    log_event,
                    "publicacao", "coupon_sse_failed",
                    "Não foi possível concluir o envio do cupom.", level="error",
                    usuario=usuario, contexto={"cupom_id": cupom_id, "canal": canal,
                                               "destino": grupo_nome or grupo_id,
                                               "causa": type(exc).__name__}, exc=exc,
                )
            except Exception:
                logger.exception("Não foi possível auditar falha SSE do cupom %s", cupom_id)
            print("[ERRO] O envio encontrou uma falha temporária. Nenhum cupom foi confirmado; tente novamente em instantes.")
            return
        if not isinstance(resultado, dict):
            logger.error("Envio de cupom %s retornou resultado inválido: %r", cupom_id, resultado)
            print("[ERRO] Não foi possível concluir o envio do cupom. Atualize a tela e tente novamente.")
            return
        if resultado.get("queued"):
            print("__QUEUED__ OK Cupom reservado; o worker fará a preparação e o transporte.")
        elif resultado.get("sucesso"):
            print(f"__SENT__ OK {_resumo_do_envio(resultado, o_que='Cupom')}")
        else:
            print(f"[ERRO] {resultado.get('motivo') or 'falha ao enviar o cupom'}")
            if resultado.get("precisa_login_ml"):
                print("__ML_LOGIN__")  # a UI troca por "Reconectar Mercado Livre"
            elif resultado.get("precisa_login_wa"):
                print("__WA_LOGIN__")  # a UI troca por "Reconectar WhatsApp"

    # segurar_transacao=False: afiliar os produtos do cupom passa minutos no
    # Link Builder (Chromium) e uma transação aberta esse tempo todo vira
    # `idle in transaction` até o proxy da Fly matar o socket — dentro de
    # transação o Django não renova a conexão ("the connection is closed").
    # As idas ao banco vão por executar_no_tenant, aqui e dentro de
    # ofertas.enviar_cupom.
    return _sse_runner(_job, request.organization, segurar_transacao=False)


@require_POST
@throttle_sse(6)
def enviar_aviso_cupons_stream(request):
    """SSE — dispara na hora o aviso "NOVOS CUPONS" de uma loja para um destino.

    Mesmo núcleo que a regra automática usa, então o texto que sai daqui é o mesmo
    que sairá sozinho — é o botão que a cliente usa para conferir antes de agendar.
    """
    from apps.scrapers.ofertas import (
        LIMITE_CUPONS_AVISO, enviar_aviso_cupons, selecionar_cupons_para_aviso,
    )

    grupo_id = (request.POST.get("grupo") or "").strip()[:100]
    grupo_nome = (request.POST.get("grupo_nome") or "").strip()[:255]
    canal = (request.POST.get("canal") or "whatsapp").strip().lower()
    marketplace = (request.POST.get("marketplace") or "").strip().lower()[:20]
    uid = request.user.id

    def _job():
        from django.contrib.auth import get_user_model
        from apps.accounts.tenant import executar_no_tenant
        # Tenant apenas ANOTADO (segurar_transacao=False): toda ida ao banco
        # reinstala o escopo numa transação curta via executar_no_tenant.
        usuario = executar_no_tenant(
            lambda: get_user_model().objects.filter(id=uid).first())
        if not usuario:
            print("[ERRO] Usuário não encontrado ou sessão encerrada.")
            return
        if not grupo_id:
            print("[ERRO] Nenhum destino informado (grupo/chat).")
            return
        if marketplace not in ("mercadolivre", "amazon", "shopee"):
            print("[ERRO] Escolha a loja do aviso (Mercado Livre, Amazon ou Shopee).")
            return
        # Objeto solto no lugar da regra salva: o núcleo só lê estes campos, e o
        # disparo manual não deve depender de existir uma ConfiguracaoEnvio.
        avulso = SimpleNamespace(
            marketplace=marketplace, grupo_id=grupo_id, horas_cooldown=24,
            incluir_restritos=True,
        )
        cupons = executar_no_tenant(
            selecionar_cupons_para_aviso, avulso, usuario,
            limite=LIMITE_CUPONS_AVISO)
        if not cupons:
            print("[ERRO] Nenhum cupom com código novo para anunciar nesta loja. "
                  "Cupons de ativação (sem código digitável) não entram neste aviso.")
            return
        print(f"Enviando aviso com {len(cupons)} cupom(ns) → "
              f"{grupo_nome or grupo_id} ({canal})...")
        try:
            resultado = enviar_aviso_cupons(
                cupons, grupo_id, canal=canal, usuario=usuario,
                destino_nome=grupo_nome,
                enqueue_only=_send_pipeline_v2_enabled(usuario))
        except Exception as exc:
            logger.exception("Falha não tratada no aviso de cupons")
            print("[ERRO] O envio encontrou uma falha temporária. Nenhum aviso foi "
                  f"confirmado ({type(exc).__name__}); tente novamente em instantes.")
            return
        if resultado.get("queued"):
            print(f"__QUEUED__ OK Aviso com {resultado.get('cupons', 0)} cupom(ns) reservado; "
                  "o worker fará o transporte.")
        elif resultado.get("sucesso"):
            print(f"__SENT__ OK Aviso com {resultado.get('cupons', 0)} cupom(ns) "
                  f"enviado (via {resultado.get('via', canal)}).")
        else:
            print(f"[ERRO] {resultado.get('motivo') or 'falha ao enviar o aviso'}")
            if resultado.get("precisa_login_ml"):
                print("__ML_LOGIN__")
            elif resultado.get("precisa_login_wa"):
                print("__WA_LOGIN__")

    # segurar_transacao=False: gerar o link do aviso passa minutos no Link
    # Builder (Chromium), e uma transação aberta esse tempo todo vira
    # `idle in transaction` até o proxy da Fly matar o socket — dentro de
    # transação o Django não renova a conexão e a query seguinte estoura
    # "the connection is closed" (o OperationalError do disparo manual). As
    # idas ao banco deste job vão por executar_no_tenant, aqui e dentro de
    # ofertas.enviar_aviso_cupons.
    return _sse_runner(_job, request.organization, segurar_transacao=False)


@require_GET
@throttle_sse(6)
def buscar_promocoes_stream(request):
    """SSE — busca itens por termo em TODAS as lojas (ML + Amazon) p/ a tela Promoções."""
    from apps.scrapers.marketplaces.registry import MARKETPLACES, get_marketplace

    termo = (request.GET.get("termo") or "").strip()
    uid = request.user.id
    try:
        min_desc = int(float(request.GET.get("min_desconto") or 15))
    except (TypeError, ValueError):
        min_desc = 15

    def _job():
        from django.contrib.auth import get_user_model
        from apps.scrapers.marketplaces.base import Marketplace, MarketplaceIndisponivel
        usuario = get_user_model().objects.filter(id=uid).first()
        if not termo:
            print("[ERRO] Digite um termo de busca.")
            return
        total = 0
        for slug in MARKETPLACES:
            mp = get_marketplace(slug)
            # Loja sem busca por termo (Awin vive de feed) devolvia "0 item(ns)" e
            # parecia uma loja vazia. Não consultar não é o mesmo que não achar.
            if type(mp).buscar_por_termo is Marketplace.buscar_por_termo:
                continue
            try:
                print(f"Buscando '{termo}' em {slug}...")
                # Amazon usa a conta do usuário (itens privados); ML é compartilhado.
                n = mp.buscar_por_termo(termo, min_desconto=min_desc, usuario=usuario) or 0
                total += n
                print(f"  {slug}: {n} item(ns).")
            except MarketplaceIndisponivel as e:
                # Motivo escrito para o usuário: "0 itens" escondia conta
                # desconectada atrás do mesmo texto de "não há oferta".
                print(f"  {slug} não foi consultada: {e}")
            except Exception as e:
                logger.exception("Busca por termo falhou em %s", slug)
                print(f"  {slug} falhou: {e}")
        print(f"Concluído. {total} item(ns) novos no total.")

    return _sse_runner(_job, request.organization)


# Itens por tela em Promoções (ofertas e cupons).
POR_PAGINA = 20

# Opção de subcategoria para o item que nenhuma loja classificou. Não é um valor
# gravado em `Produto.categoria`: é um rótulo de UI que a view traduz de volta para
# "DESCONHECIDO, vazio ou nulo". O prefixo evita colisão com categoria real do ML
# (domain_id, ex.: 'VACUUM_CLEANERS') ou da Amazon (nome do browse node).
SEM_SUBCATEGORIA = "__sem_subcategoria__"
ROTULO_SEM_SUBCATEGORIA = "Sem subcategoria"

# A partir de quanto tempo sem tentativa uma fonte deixa de ser lida como "com
# defeito" e passa a ser lida como "parada". O ciclo mais lento é o `scrape` do
# Procfile (--scrape-horas 3), então 8h cobre duas janelas perdidas com folga.
HORAS_FONTE_SILENCIOSA = 8


def top_promocoes(request):
    from apps.scrapers.models import HistoricoEnvio
    from apps.scrapers.marketplaces.registry import MARKETPLACES
    from apps.scrapers.senders.registry import SENDERS

    filtros_key = "top_promocoes_filtros"
    if request.GET.get("reset") == "1":
        request.session.pop(filtros_key, None)
        return redirect("scraper-top")

    tem_filtros_na_url = any(
        chave in request.GET
        for chave in ("macro", "categoria", "loja", "ordenar", "q", "min_desconto",
                      "tipo", "fonte", "confianca", "atualizado_desde", "afiliado",
                      "categoria_cupom", "como_usar", "anunciante")
    )
    if tem_filtros_na_url:
        filtros = {
            "macro": request.GET.getlist("macro"),
            "categoria": request.GET.getlist("categoria"),
            "loja": (request.GET.get("loja") or "").strip(),
            "ordenar": "valor" if request.GET.get("ordenar") == "valor" else "percent",
            "q": (request.GET.get("q") or "").strip()[:120],
            "min_desconto": (request.GET.get("min_desconto") or "").strip(),
            "tipo": "cupom" if request.GET.get("tipo") == "cupom" else "oferta",
            "fonte": (request.GET.get("fonte") or "").strip()[:80],
            "confianca": (request.GET.get("confianca") or "").strip()[:20],
            "atualizado_desde": (request.GET.get("atualizado_desde") or "").strip(),
            # Filtros exclusivos da aba Cupons.
            "categoria_cupom": (request.GET.get("categoria_cupom") or "").strip()[:100],
            "anunciante": (request.GET.get("anunciante") or "").strip()[:100],
            "como_usar": (request.GET.get("como_usar")
                          if request.GET.get("como_usar") in ("codigo", "ativacao")
                          else ""),
            # Default = só afiliados: enviar item sem link de afiliado não comissiona.
            # "todos" existe só para diagnóstico (ver o que está travado na fila).
            "afiliado": "todos" if request.GET.get("afiliado") == "todos" else "prontos",
        }
        request.session[filtros_key] = filtros
    else:
        filtros = request.session.get(filtros_key, {})

    macros_selecionados = filtros.get("macro", [])
    categorias_selecionadas = filtros.get("categoria", [])
    loja_selecionada = filtros.get("loja", "")
    busca = filtros.get("q", "")
    tipo = filtros.get("tipo", "oferta")
    fonte_selecionada = filtros.get("fonte", "")
    confianca_selecionada = filtros.get("confianca", "")
    categoria_cupom_selecionada = filtros.get("categoria_cupom", "")
    anunciante_selecionado = filtros.get("anunciante", "")
    como_usar_selecionado = filtros.get("como_usar", "")
    so_afiliados = filtros.get("afiliado", "prontos") != "todos"
    try:
        atualizado_desde = max(0, min(168, int(filtros.get("atualizado_desde") or 0)))
    except (TypeError, ValueError):
        atualizado_desde = 0
    try:
        min_desconto = max(0, min(100, int(float(filtros.get("min_desconto") or 0))))
    except (TypeError, ValueError):
        min_desconto = 0

    # A página vem só da URL, nunca da sessão de filtros: guardá-la faria uma
    # visita nova cair na página 12 de uma lista que já mudou. Fora de
    # `tem_filtros_na_url` de propósito — paginar não pode reescrever os filtros.
    pagina = request.GET.get("pagina")

    # A tela tem duas abas EXCLUSIVAS no template (`{% if tipo == 'cupom' %}` /
    # `{% else %}`), mas a view montava as duas em todo GET: quem abria Ofertas
    # pagava o catálogo inteiro de cupons (materializar, normalizar regras, pontuar
    # e paginar) para renderizar zero cupom, e vice-versa. Cada aba agora só monta o
    # que a sua metade do template lê.
    aba_cupons = tipo == "cupom"

    # Painel técnico (saúde das fontes, fila de afiliação, motivos de
    # indisponibilidade, contadores de etapa) é leitura de operação, não de quem só
    # quer escolher uma oferta e enviar. Fora de `is_staff` ele não é apenas ruído:
    # cada bloco desses custa consulta agregada sobre o catálogo inteiro, então
    # esconder também é o que tira o peso do GET do usuário comum.
    diagnostico = request.user.is_staff

    from django.db.models import Q
    from apps.scrapers.maintenance import produtos_frescos_q

    def _produtos_da_taxonomia():
        # Os filtros devem descrever o mesmo universo que a lista: itens frescos,
        # publicáveis e visíveis para o usuário. Consultar o histórico inteiro fazia
        # o DISTINCT atravessar anos de observações mortas e travava a navegação.
        return Produto.objects.filter(
            produtos_frescos_q(),
        ).exclude(
            estado__in=["indisponivel", "invalido", "expirado", "stale"]
        ).filter(
            Q(owner__isnull=True) | Q(owner=request.user)
        )

    def _montar_taxonomia_top():
        # Uma amostra recente basta para montar filtros navegáveis. Os dois
        # DISTINCT anteriores ainda precisavam varrer o universo visível inteiro;
        # sob a raspagem ativa chegaram a somar 8s. Uma leitura limitada alimenta
        # macros e categorias de uma vez e fica cacheada por organização.
        agrupado = {}
        macros_sem_categoria = set()
        for row in (
            _produtos_da_taxonomia()
            .exclude(macro_categoria__isnull=True).exclude(macro_categoria="")
            .values("macro_categoria", "categoria")
            .order_by("-ultima_observacao")[:500]
        ):
            macro = row["macro_categoria"]
            categoria = row["categoria"]
            # 'DESCONHECIDO'/vazio significa "ninguém classificou ainda", e a loja
            # que não classifica é justamente a que mais tem item. Omitir esses
            # produtos da lista não os filtrava: tornava-os INALCANÇÁVEIS — quem
            # escolhesse qualquer subcategoria perdia a loja inteira sem explicação.
            # Eles viram uma opção própria, então a escolha fica visível e reversível.
            if not categoria or categoria == "DESCONHECIDO":
                macros_sem_categoria.add(macro)
                agrupado.setdefault(macro, [])
                continue
            agrupado.setdefault(macro, []).append(categoria)
        for macro in macros_sem_categoria:
            agrupado[macro].append(SEM_SUBCATEGORIA)
        categorias = {
            macro: sorted(set(cats)) for macro, cats in agrupado.items() if cats
        }
        return {"macros": sorted(agrupado), "categorias": categorias}

    taxonomia_top = {} if aba_cupons else _taxonomia_cacheada(
        "top", request, _montar_taxonomia_top)
    macro_categorias = sorted(
        set(taxonomia_top.get("macros", [])) | set(macros_selecionados)
    )
    categorias_por_macro = taxonomia_top.get("categorias", {})

    # Ordenação: 'percent' (padrão — melhor p/ deal bot) ou 'valor' (R$ absoluto economizado).
    ordenar = "valor" if filtros.get("ordenar") == "valor" else "percent"

    # Cada aba preenche apenas o seu lado. A outra fica nestes neutros: o template
    # ainda cita as variáveis, mas dentro de um `{% if tipo %}` que não abre.
    cupons_catalogo, cupom_categorias, cupom_anunciantes, cupons_motivos = [], [], [], []
    cupons_page = Paginator([], POR_PAGINA).get_page(1)
    cupons_coletados = cupons_elegiveis = cupons_preparados = cupons_prontos = 0
    cupons_descartados = cupons_aguardando_preparo = cupons_aguardando_link = 0
    cupons_aguardando_verificacao = cupons_aguardando_conexao = 0
    cupons_fontes_sem_resultado = 0
    # Contador para TODO usuário (não só admin): é o que explica uma lista curta
    # sem oferecer um cupom que o envio ainda não consegue montar.
    cupons_em_preparo = 0
    produtos, pendentes_ocultos, amazon_diagnostico = [], 0, ""
    prontos_por_loja, pendentes_por_loja, contagem_estrita = {}, {}, True
    page_obj = Paginator([], POR_PAGINA).get_page(1)
    afiliacao, afiliacao_ultimo_erro, fontes_com_aviso = None, "", []

    perfil = getattr(request.user, "perfil", None)
    fontes_qs = FonteIngestao.objects.filter(habilitada=True).exclude(
        slug="manual-private").order_by("marketplace", "nome")
    # Campanhas coletadas em area autenticada pertencem a organizacao cuja
    # sessao as observou. Para as demais contas, esta fonte nao e aplicavel e
    # nao deve aparecer como incidente operacional no painel.
    from apps.accounts.models import organization_for_user
    organization = organization_for_user(request.user)
    ml_system_organization_id = str(
        getattr(settings, "ML_SYSTEM_ORGANIZATION_ID", "") or ""
    )
    if not organization or str(organization.id) != ml_system_organization_id:
        fontes_qs = fontes_qs.exclude(slug="mercadolivre-campanhas")
    # Fontes Amazon are account-specific. Do not present an adapter that cannot
    # run for this user as an operational incident.
    from apps.scrapers.scraper_amazon.creators_api import creds_de_usuario
    if not perfil or not creds_de_usuario(request.user).completo():
        fontes_qs = fontes_qs.exclude(slug="amazon-creators-api")
    fontes = list(fontes_qs)

    if aba_cupons:
        from apps.scrapers.coupon_rules import cupons_visiveis_q
        cupons_visiveis = cupons_visiveis_q(request.user)
        from apps.scrapers.maintenance import cupons_frescos_q
        cupons_qs = CupomNormalizado.objects.select_related(
            "fonte", "integracao", "programa").filter(
            cupons_visiveis, estado="ativo"
        ).filter(cupons_frescos_q())
        if loja_selecionada:
            cupons_qs = cupons_qs.filter(marketplace=loja_selecionada)
        if fonte_selecionada:
            cupons_qs = cupons_qs.filter(fonte__slug=fonte_selecionada)
        # Confiança não é mais filtrável na aba Cupons (todos são "media"); ignora um
        # valor herdado da aba Ofertas para não zerar a lista sem querer.
        if categoria_cupom_selecionada:
            cupons_qs = cupons_qs.filter(categoria=categoria_cupom_selecionada)
        if anunciante_selecionado:
            cupons_qs = cupons_qs.filter(anunciante_nome=anunciante_selecionado)
        if busca:
            cupons_qs = cupons_qs.filter(Q(titulo__icontains=busca) | Q(codigo__icontains=busca))
        # "Como usar" (código vs. ativar no link) vem da normalização de `regras`, não
        # de coluna — então materializa, calcula por cupom e filtra em Python, igual ao
        # corte de afiliação das ofertas. O conjunto de cupons ativos é pequeno.
        from apps.scrapers.coupon_rules import (
            codigo_publicavel, cupom_publicavel, regras_do_cupom, score_cupom,
        )
        # Base por recência (desempate estável); depois ordena por qualidade do cupom —
        # feedback da cliente: bons cupons vendem mais, então os melhores vêm primeiro.
        cupons_lista = list(cupons_qs.order_by("-ultima_observacao"))
        for cupom_catalogo in cupons_lista:
            cupom_catalogo.codigo_publico = codigo_publicavel(cupom_catalogo)
            cupom_catalogo.modo_resgate = regras_do_cupom(cupom_catalogo)["modo_resgate"]
        from apps.scrapers.coupon_readiness import disponibilidade_resumo
        # SOMENTE LEITURA, sempre. Não chame aqui `projetar_disponibilidade_cupons`:
        # quem materializa a projeção é o worker `cupons` (Procfile) e o comando
        # `backfill_disponibilidade_cupons`, que rodam fora do processo web.
        #
        # O guard anterior ("projeta só se o usuário não tem nenhuma linha") não
        # fechava nunca: OrganizationContextMiddleware envolve a request inteira num
        # transaction.atomic() para instalar o escopo RLS com SET LOCAL, então o
        # atomic() por cupom lá dentro é savepoint, não commit. O laço de ~5.800
        # cupons morria no lock_timeout de 15s contra o worker, TODO o trabalho
        # voltava atrás, o usuário seguia com zero linhas e o reload seguinte
        # recomeçava do zero -- 500 em /scrapers/top/ e as 8 threads do gunicorn
        # presas em transações de escrita longas (o /healthz caía junto).
        readiness = disponibilidade_resumo(request.user)
        channel = "whatsapp"
        projecoes = {
            row.cupom_id: row
            for row in CupomDisponibilidade.objects.filter(
                organization=organization, usuario=request.user, channel=channel,
                cupom_id__in=[c.pk for c in cupons_lista],
            )
        }
        cupons_publicaveis = []
        for cupom in cupons_lista:
            projecao = projecoes.get(cupom.pk)
            if not projecao:
                # A coleta e a projeção são workers independentes. Um cupom que
                # chegou entre os ciclos não pode desaparecer: a view continua
                # somente leitura e representa o intervalo como fila de preparo.
                projecao = SimpleNamespace(
                    stage="eligible", category="waiting",
                    reason_code="preparation_pending",
                    safe_detail="Cupom coletado; aguardando o próximo ciclo de preparo.",
                    retry_at=None,
                    get_stage_display=lambda: "Aguardando preparo",
                )
                cupom.disponibilidade = projecao
                # Coletado entre os ciclos: ainda NÃO é enviável, então conta na
                # fila em vez de entrar na lista.
                if cupom_publicavel(cupom, usuario=request.user):
                    cupons_em_preparo += 1
                continue
            cupom.disponibilidade = projecao
            # REGRA DA TELA: o que está listado tem de poder ser enviado.
            #
            # Cupom de código entrava aqui com qualquer `stage != "discarded"`, e
            # era daí que vinham os "aguardando link" que a lista oferecia sem
            # conseguir enviar. `ready` é o único estágio em que produto, preço e
            # link afiliado estão comprovados — o mesmo corte que os cupons de
            # ativação sempre tiveram. O resto vira contador, não item.
            if projecao.stage == "ready":
                cupons_publicaveis.append(cupom)
            elif projecao.stage != "discarded":
                cupons_em_preparo += 1
        if como_usar_selecionado == "codigo":
            cupons_publicaveis = [c for c in cupons_publicaveis if c.codigo_publico]
        elif como_usar_selecionado == "ativacao":
            cupons_publicaveis = [c for c in cupons_publicaveis if not c.codigo_publico]
        if diagnostico:
            stages = readiness.get("stages", {})
            cupons_coletados = readiness.get("total", 0)
            cupons_elegiveis = stages.get("eligible", 0)
            cupons_preparados = stages.get("prepared", 0)
            cupons_prontos = stages.get("ready", 0)
            cupons_descartados = stages.get("discarded", 0)
            cupons_aguardando_preparo = stages.get("collected", 0) + stages.get("eligible", 0)
            cupons_aguardando_link = stages.get("prepared", 0) + stages.get("waiting_link", 0)
            cupons_aguardando_verificacao = sum(
                count for reason, count in readiness.get("reasons", {}).items()
                if "verification" in reason
            )
            cupons_aguardando_conexao = sum(
                count for reason, count in readiness.get("reasons", {}).items()
                if "session" in reason or "login_required" in reason or "disconnected" in reason
            )
            cupons_motivos = list(
                CupomDisponibilidade.objects.filter(
                    organization=organization, usuario=request.user,
                    channel=channel,
                    cupom_id__in=[c.pk for c in cupons_lista],
                )
                .exclude(reason_code="")
                .values("reason_code")
                .annotate(total=Count("id"), safe_detail=Max("safe_detail"))
                .order_by("-total", "reason_code")[:12]
            )
            cupons_fontes_sem_resultado = FonteIngestao.objects.filter(
                habilitada=True, ultimo_total=0,
                status__in=("degraded", "blocked"),
            ).count()
        # A lista continua estrita: o contador torna a fila visível sem oferecer um
        # cupom cujo envio ainda não consegue montar produtos, imagem e link afiliado.
        cupons_lista = cupons_publicaveis
        # Os filtros tambem devem refletir somente o catalogo realmente publicavel.
        cupom_categorias = sorted({c.categoria for c in cupons_lista if c.categoria})
        cupom_anunciantes = sorted({c.anunciante_nome for c in cupons_lista
                                    if c.anunciante_nome})
        cupons_lista.sort(key=lambda c: score_cupom(c, usuario=request.user), reverse=True)
        cupons_page = Paginator(cupons_lista, POR_PAGINA).get_page(pagina)
        cupons_catalogo = list(cupons_page)
        if cupons_catalogo:
            # A lista já contém apenas `ready`; agora explica POR QUE cada item
            # merece confiança. Tudo é resolvido em lote para não reintroduzir o
            # antigo N+1 que travava esta página com milhares de cupons.
            from apps.scrapers.coupon_rules import (
                corroboracoes_independentes_em_lote, cupom_de_comunidade,
                fontes_independentes_em_lote,
            )
            from apps.scrapers.maintenance import COUPON_MAX_AGE_HOURS
            from apps.scrapers.models import CupomFonteObservacao, CupomValidacao

            ids_pagina = [cupom.pk for cupom in cupons_catalogo]
            checkout_confirmado = set(CupomValidacao.objects.filter(
                organization=organization, usuario=request.user,
                cupom_id__in=ids_pagina, status="accepted", no_purchase=True,
                discount_amount__gt=0,
            ).values_list("cupom_id", flat=True))
            fontes_independentes = fontes_independentes_em_lote(cupons_catalogo)
            corroboracoes = corroboracoes_independentes_em_lote(
                cupons_catalogo, fontes=fontes_independentes,
            )
            observadas_desde = timezone.now() - timezone.timedelta(
                hours=COUPON_MAX_AGE_HOURS,
            )
            fontes_por_cupom = {
                row["cupom_id"]: row["total"]
                for row in CupomFonteObservacao.objects.filter(
                    cupom_id__in=ids_pagina, outcome="accepted",
                    observed_at__gte=observadas_desde,
                ).values("cupom_id").annotate(
                    total=Count("fonte_id", distinct=True),
                )
            }
            fontes_diretas = {
                "amazon-public-coupons", "amazon-public-web",
                "ml-cupons-afiliados", "ml-lightning-coupons",
                "ml-official-promotions", "shopee-campaigns",
                "shopee-public-coupons", "licensed-affiliate-feed",
            }
            for cupom in cupons_catalogo:
                evidencia = cupom.evidencia if isinstance(cupom.evidencia, dict) else {}
                slug = str(cupom.fonte.slug or "")
                chave = (
                    str(cupom.marketplace or "").casefold(),
                    str(cupom.codigo or "").strip().upper(),
                )
                cupom.evidencia_fontes = max(
                    1, fontes_por_cupom.get(cupom.pk, 0),
                    fontes_independentes.get(chave, 0),
                )
                if cupom.pk in checkout_confirmado:
                    cupom.evidencia_rotulo = "Confirmado no carrinho"
                    cupom.evidencia_detalhe = (
                        "O desconto foi observado antes da compra; nenhuma compra foi concluída."
                    )
                    cupom.evidencia_css = "badge-green"
                elif cupom_de_comunidade(cupom) and chave in corroboracoes:
                    cupom.evidencia_rotulo = "Corroborado"
                    cupom.evidencia_detalhe = (
                        "O código e o desconto concordam em fontes independentes e recentes."
                    )
                    cupom.evidencia_css = "badge-green"
                elif slug == "manual-private":
                    cupom.evidencia_rotulo = "Privado"
                    cupom.evidencia_detalhe = (
                        "Cupom da sua organização, preparado com link verificado para envio."
                    )
                    cupom.evidencia_css = "badge-accent"
                elif (
                    slug in fontes_diretas
                    or "official" in str(evidencia.get("association") or "").casefold()
                    or "official" in str(evidencia.get("transport") or "").casefold()
                ):
                    cupom.evidencia_rotulo = "Observado na loja"
                    cupom.evidencia_detalhe = (
                        "A campanha e seu escopo foram observados em uma fonte direta da loja."
                    )
                    cupom.evidencia_css = "badge-green"
                else:
                    cupom.evidencia_rotulo = "Fonte estruturada"
                    cupom.evidencia_detalhe = (
                        "O cupom passou pelos gates de escopo, produto, preço e link de envio."
                    )
                    cupom.evidencia_css = "badge-muted"
    else:
        from apps.scrapers.ofertas import anotacao_preco_publicado
        qs = Produto.objects.filter(
            produtos_frescos_q(), preco_sem_desconto__gt=0,
        ).exclude(
            estado__in=["indisponivel", "invalido", "expirado", "stale"]
        ).filter(
            # Pool compartilhado (ML, owner=None) + itens privados do usuário (Amazon dele).
            Q(owner__isnull=True) | Q(owner=request.user)
        ).annotate(
            # A tela tem de mostrar o MESMO número que a mensagem anuncia — fonte única
            # em ofertas.py, ao lado de preco_publicavel().
            preco_publicado=anotacao_preco_publicado(),
        ).annotate(
            economia=ExpressionWrapper(F("preco_sem_desconto") - F("preco_publicado"), output_field=FloatField()),
            percent=ExpressionWrapper(
                (F("preco_sem_desconto") - F("preco_publicado")) * 100.0 / F("preco_sem_desconto"),
                output_field=FloatField(),
            ),
        ).filter(
            # Produto com preço "de" igual ao "por" não é promoção. Além de
            # poluir o ranking, ele chegava ao modo padrão da Amazon com badge 0% e
            # botão de envio, embora nenhum abatimento estivesse confirmado.
            percent__gt=0,
            # A seleção automática já rejeita 90%+: além de raros, esses valores
            # quase sempre são `savingBasis` em escala errada (ex.: 63990 vs 63,99).
            # A vitrine precisa aplicar a mesma barreira, inclusive para fontes novas.
            percent__lt=90,
        )
        if macros_selecionados:
            qs = qs.filter(macro_categoria__in=macros_selecionados)
        if categorias_selecionadas:
            # "Sem subcategoria" é rótulo de UI, não valor de coluna: traduz de volta
            # para as três formas com que "ninguém classificou" chega ao banco.
            reais = [c for c in categorias_selecionadas if c != SEM_SUBCATEGORIA]
            condicao = Q(categoria__in=reais) if reais else Q(pk__in=[])
            if SEM_SUBCATEGORIA in categorias_selecionadas:
                condicao |= (Q(categoria="DESCONHECIDO") | Q(categoria="")
                             | Q(categoria__isnull=True))
            qs = qs.filter(condicao)
        if loja_selecionada:
            qs = qs.filter(marketplace=loja_selecionada)
        if busca:
            # Casa contra a coluna normalizada: "robo", "Robô" e "ROBÔ" têm de trazer
            # o mesmo conjunto. Categoria e macro-categoria continuam em icontains —
            # são valores da nossa taxonomia, não texto livre da loja, e normalizá-las
            # exigiria mais duas colunas para nada.
            busca_norm = normalizar_busca(busca)
            qs = qs.filter(
                Q(nome_norm__icontains=busca_norm)
                | Q(categoria__icontains=busca)
                | Q(macro_categoria__icontains=busca)
            )
        if min_desconto:
            qs = qs.filter(percent__gte=min_desconto)
        if fonte_selecionada:
            qs = qs.filter(fonte=fonte_selecionada)
        if confianca_selecionada:
            qs = qs.filter(confianca=confianca_selecionada)
        if atualizado_desde:
            qs = qs.filter(ultima_observacao__gte=timezone.now() - timezone.timedelta(hours=atualizado_desde))

        # A afiliação é resolvida antes da paginação. Não pode haver um corte de ranking
        # aqui: ele fazia a tela anunciar centenas de links prontos no resumo, mas
        # paginava somente os afiliados que por acaso estivessem entre os 200 maiores
        # descontos. O conjunto cabe em memória e cada marketplace resolve os links em
        # lote, portanto todos os produtos afiliados podem entrar na paginação.
        # A mesma oferta chega por feed completo, lane rápida e busca, com querystrings
        # diferentes. Seleciona primeiro a observação mais recente de cada identidade;
        # só então ranqueia, evitando duplicatas e preço antigo vencer pelo desconto.
        from apps.scrapers.product_identity import deduplicar_por_produto
        # A dedup e o ranking ainda são em Python, mas nunca mais materializam o
        # catálogo fresco inteiro para mostrar 20 itens. A janela cobre 500
        # observações recentes na primeira página e cresce conforme o usuário
        # avança; assim a navegação tem custo limitado sem congelar num top fixo.
        # `only` limita também as colunas ao que este caminho realmente lê:
        # dedup (product_identity), `preparar_exibicao` de cada loja, o histórico de
        # preço e as colunas do template.
        #
        # Ao acrescentar um campo ao template desta tela, acrescente-o AQUI também:
        # campo de fora da lista continua correto, mas custa uma query por item
        # exibido (Django busca sob demanda o que ficou deferido).
        try:
            pagina_janela = max(1, int(pagina or 1))
        except (TypeError, ValueError):
            pagina_janela = 1
        limite_catalogo = min(5000, max(500, pagina_janela * 250))
        candidatos = deduplicar_por_produto(
            qs.order_by("-ultima_observacao", "-id").only(
                "id", "marketplace", "asin", "nome", "campanha_id",
                "link_produto", "link_afiliado", "imagem_url",
                "categoria", "macro_categoria",
                "preco_sem_desconto", "preco_com_cupom", "preco_efetivo",
                "ultima_observacao", "owner_id", "afiliado_ok",
            )[:limite_catalogo]
        )
        campo_ordem = "economia" if ordenar == "valor" else "percent"
        candidatos.sort(
            key=lambda produto: (
                float(getattr(produto, campo_ordem, 0) or 0),
                produto.ultima_observacao or timezone.datetime.min.replace(
                    tzinfo=timezone.get_current_timezone()),
                produto.id,
            ),
            reverse=True,
        )
        # Atribuição é regra de cada loja (ver Marketplace.preparar_exibicao). Em lote e
        # agrupado por loja: por item seria uma query por produto da página.
        from apps.scrapers.marketplaces.registry import get_marketplace

        def _preparar(itens):
            por_loja = {}
            for p in itens:
                por_loja.setdefault(p.marketplace, []).append(p)
            for slug, do_slug in por_loja.items():
                get_marketplace(slug).preparar_exibicao(do_slug, request.user)

        # O corte por afiliação é em Python, e não em SQL, de propósito —
        # `afiliado_pronto` é contrato de cada loja (na Amazon sai de tag + ASIN, não de
        # linha no banco), então só `preparar_exibicao` sabe respondê-lo. Isso obriga a
        # preparar o conjunto INTEIRO quando o filtro está ligado: paginar antes contaria
        # itens que a tela nunca mostra.
        #
        # Quando o filtro está DESLIGADO, porém, nada depende de `afiliado_pronto` fora
        # da página exibida — e preparar o catálogo inteiro só para renderizar 20 linhas
        # era trabalho jogado fora em cima da consulta mais pesada do sistema.
        from apps.scrapers.vitrine import (
            contar_por_marketplace, equilibrar_primeira_pagina,
        )

        pendentes_ocultos = 0
        if so_afiliados:
            # Resolver o catálogo inteiro exigia validar milhares de linhas sob RLS
            # antes de mostrar 20 cards. Também não podemos usar EXISTS correlacionado:
            # a política de tenant é deliberadamente cara e seria reavaliada quatro
            # vezes por produto. Caminhamos pelo ranking em lotes: o IN volta pequeno
            # e `limite_catalogo` já limita quanto se percorre.
            #
            # O que NÃO pode existir aqui é parada antecipada ao encher a página
            # pedida. `pendentes_ocultos`, o funil por loja, a promoção da loja
            # menor (equilibrar_primeira_pagina) e o total de páginas descrevem a
            # JANELA INTEIRA, não os 20 cards. Medido contra 500 ofertas ML e 30
            # Amazon prontas, o corte antecipado dava: zero Amazon na primeira
            # página, Amazon fora do funil, o aviso de tag ausente sumido, 5
            # páginas oferecidas de 27 e `pendentes_ocultos` em 0 de 60 — ou seja,
            # a loja menor desaparecia atrás do volume da maior.
            tamanho_lote = 500
            prontos, pendentes = [], []
            for inicio in range(0, len(candidatos), tamanho_lote):
                lote = candidatos[inicio:inicio + tamanho_lote]
                _preparar(lote)
                for produto in lote:
                    alvo = (prontos if getattr(produto, "afiliado_pronto", False)
                            else pendentes)
                    alvo.append(produto)
            pendentes_ocultos = len(pendentes)
            pendentes_por_loja = contar_por_marketplace(pendentes)
            candidatos = prontos
            # Com o corte de afiliação resolvido, sabemos exatamente quais lojas têm
            # item PRONTO — é a hora certa de garantir a presença de cada uma na
            # primeira página. Só quando o usuário não escolheu uma loja: filtrar por
            # loja é um pedido explícito de ver só aquela.
            if not loja_selecionada:
                candidatos = equilibrar_primeira_pagina(candidatos, POR_PAGINA)
            page_obj = Paginator(candidatos, POR_PAGINA).get_page(pagina)
            produtos = list(page_obj)
        else:
            pendentes_por_loja = {}
            page_obj = Paginator(candidatos, POR_PAGINA).get_page(pagina)
            produtos = list(page_obj)
            _preparar(produtos)
        # Contadores do MESMO universo que a tela pagina (mesma janela de frescor,
        # mesmos filtros, mesmo corte de afiliação) — ver vitrine.contar_por_marketplace.
        #
        # Em "mostrar também os pendentes" a prontidão NÃO é resolvida para o
        # catálogo inteiro (só para a página exibida), então aqui o número é de
        # itens listados, não de prontos. O rótulo acompanha: prometer "pronto(s)"
        # sobre uma contagem que não foi apurada é exatamente o tipo de indicador
        # que não reconcilia com a tela.
        prontos_por_loja = contar_por_marketplace(candidatos)
        contagem_estrita = so_afiliados

        cupons_map = {
            c.campanha_id: c
            for c in Cupom.objects.filter(
                campanha_id__in=[p.campanha_id for p in produtos],
                estado="ativo",
            ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now()))
        }
        # Marca itens já enviados POR ESTE usuário (manual OU automático): bloqueia reenvio na UI.
        ja_enviados = set(
            HistoricoEnvio.objects.filter(
                produto_id__in=[p.id for p in produtos], usuario=request.user)
            .values_list("produto_id", flat=True)
        )

        # Histórico de preço de todos os itens da página numa query só (era uma por item).
        from apps.scrapers.precos import chave_produto, stats_em_lote
        historico = stats_em_lote(produtos, dias=30)

        for p in produtos:
            p.cupom = cupons_map.get(p.campanha_id)
            p.ja_enviado = p.id in ja_enviados
            p.motivos_score = [f"{p.percent:.0f}% de desconto"]
            hist_preco = historico.get(chave_produto(p))
            # Contra o preço publicado: o histórico é gravado com o valor que o cliente
            # paga, então comparar a vitrine com ele daria selo em item que não está na
            # mínima (e tiraria de item que está).
            if hist_preco and hist_preco["n"] >= 3 and p.preco_publicado <= hist_preco["minimo"] * 1.02:
                p.motivos_score.append("mínima de 30 dias")
    if diagnostico:
        # Fora do `if aba_cupons`/`else` de propósito: a faixa que mostra esta frase
        # aparece nas DUAS abas. Enquanto ela morava no ramo de Ofertas, abrir Cupons
        # apagava a linha da Amazon.
        #
        # `.exists()` no lugar do `.count()` que existia aqui: a frase só distingue
        # "tem oferta Amazon" de "não tem", e contar percorria de novo a consulta
        # mais cara da tela para jogar o número fora.
        tem_oferta_amazon = Produto.objects.filter(
            produtos_frescos_q(), preco_sem_desconto__gt=0, marketplace="amazon",
        ).filter(Q(owner__isnull=True) | Q(owner=request.user)).exclude(
            estado__in=["indisponivel", "invalido", "expirado", "stale"]
        ).exists()
        if tem_oferta_amazon:
            amazon_diagnostico = "Amazon ativa para sua conta."
        elif perfil and not perfil.afiliado_tag_amazon:
            amazon_diagnostico = (
                "O catálogo público é coletado sem tag; cadastre a tag Amazon para liberar links e envios."
            )
        elif perfil and perfil.amazon_elegivel is False:
            amazon_diagnostico = "Creators API inelegível; o fallback público tentará alimentar sua conta."
        else:
            amazon_diagnostico = "Nenhuma oferta Amazon confirmada no último ciclo."
        # `status` é um texto persistido que ninguém envelhece: uma fonte que parou de
        # rodar guarda o último veredito para sempre. Sem esta derivação, "Atenção" tanto
        # significava "falhou agora" quanto "ninguém a executa há dias" — dois problemas
        # com soluções opostas. A coluna não muda; só a leitura da tela.
        from apps.scrapers import automacao_state
        from apps.scrapers.automacao_state import lane_da_fonte

        _limite_silencio = timezone.now() - timezone.timedelta(
            hours=HORAS_FONTE_SILENCIOSA)
        _lanes_ligadas = {}
        fontes_com_aviso, _paradas, _desligadas = [], [], []
        for fonte in fontes:
            fonte.silenciosa = (
                fonte.ultima_tentativa is None or fonte.ultima_tentativa < _limite_silencio)
            # "Parada" e "desligada" pedem ações OPOSTAS, e a faixa dizia a mesma
            # coisa para as duas. Em produção as três fontes da lane de raspagem
            # apareciam como "Sem coleta há mais de 8h" — mandando procurar defeito
            # numa coleta que ninguém tinha mandado rodar. Quem religa é a tela
            # Scraper, e é isso que a faixa passa a dizer.
            lane = lane_da_fonte(fonte.slug)
            if lane and lane not in _lanes_ligadas:
                _lanes_ligadas[lane] = automacao_state.is_enabled(lane)
            fonte.lane_desligada = bool(
                fonte.silenciosa and lane and not _lanes_ligadas.get(lane, True))
            fonte.status_exibicao = (
                "off" if fonte.lane_desligada
                else "silent" if fonte.silenciosa else fonte.status)
            fonte.motivo_exibicao = (
                "A raspagem está desligada; ligue na tela Scraper."
                if fonte.lane_desligada else
                f"Sem coleta há mais de {HORAS_FONTE_SILENCIOSA}h."
                if fonte.silenciosa else (fonte.erro_publico or ""))
            # Só o que exige leitura desce para a lista abaixo da faixa. Fonte parada tem
            # sempre o mesmo motivo, então vai numa linha agregada: repetir a frase seis
            # vezes é ruído que esconde o aviso específico de quem falhou de verdade.
            if fonte.lane_desligada:
                _desligadas.append(fonte.nome)
            elif fonte.silenciosa:
                _paradas.append(fonte.nome)
            elif fonte.motivo_exibicao:
                fontes_com_aviso.append(
                    {"nome": fonte.nome, "motivo": fonte.motivo_exibicao})
        if _desligadas:
            fontes_com_aviso.append({
                "nome": "Raspagem desligada na tela Scraper:",
                "motivo": ", ".join(_desligadas),
            })
        if _paradas:
            fontes_com_aviso.append({
                "nome": f"Sem coleta há mais de {HORAS_FONTE_SILENCIOSA}h:",
                "motivo": ", ".join(_paradas),
            })
        from apps.scrapers.afiliado import resumo_afiliacao
        afiliacao = resumo_afiliacao(request.user)
        afiliacao_ultimo_erro = (
            LinkAfiliadoUsuario.objects
            .filter(usuario=request.user, estado="erro")
            .exclude(ultimo_erro="")
            .order_by("-ultima_tentativa", "-id")
            .values_list("ultimo_erro", flat=True)
            .first()
        ) or ""

    # base da querystring (mantém filtros ao trocar a ordenação)
    from urllib.parse import urlencode
    qs_pairs = [("macro", m) for m in macros_selecionados] + [("categoria", c) for c in categorias_selecionadas]
    if busca:
        qs_pairs.append(("q", busca))
    if min_desconto:
        qs_pairs.append(("min_desconto", min_desconto))
    qs_pairs.append(("tipo", tipo))
    if fonte_selecionada:
        qs_pairs.append(("fonte", fonte_selecionada))
    if confianca_selecionada:
        qs_pairs.append(("confianca", confianca_selecionada))
    if atualizado_desde:
        qs_pairs.append(("atualizado_desde", atualizado_desde))
    if categoria_cupom_selecionada:
        qs_pairs.append(("categoria_cupom", categoria_cupom_selecionada))
    if anunciante_selecionado:
        qs_pairs.append(("anunciante", anunciante_selecionado))
    if como_usar_selecionado:
        qs_pairs.append(("como_usar", como_usar_selecionado))
    # base p/ o chip de afiliação: preserva o resto dos filtros e troca só ele.
    qs_base_sem_afiliado = urlencode(qs_pairs + (
        [("loja", loja_selecionada)] if loja_selecionada else []))
    qs_base_sem_afiliado = (qs_base_sem_afiliado + "&") if qs_base_sem_afiliado else ""
    if not so_afiliados:
        qs_pairs.append(("afiliado", "todos"))
    # base p/ os chips de loja: preserva macro/categoria/ordem, troca só a loja.
    qs_sem_loja = list(qs_pairs)
    if ordenar == "valor":
        qs_sem_loja.append(("ordenar", "valor"))
    qs_base_sem_loja = (urlencode(qs_sem_loja) + "&") if qs_sem_loja else ""
    if loja_selecionada:
        qs_pairs.append(("loja", loja_selecionada))
    qs_base = (urlencode(qs_pairs) + "&") if qs_pairs else ""
    # Base dos links de página: como `pagina` nunca entra em qs_*, preserva
    # filtros E ordenação sem arrastar a página atual junto.
    qs_pagina = list(qs_pairs)
    if ordenar == "valor":
        qs_pagina.append(("ordenar", "valor"))
    qs_base_pagina = (urlencode(qs_pagina) + "&") if qs_pagina else ""

    return render(request, "scrapers/top_promocoes.html", {
        # Gate único do painel técnico. O template consulta só esta chave, para que
        # ligar/desligar o painel não dependa de lembrar de dez `{% if %}`.
        "diagnostico": diagnostico,
        "produtos": produtos,
        "page_obj": page_obj,
        "cupons_page": cupons_page,
        "qs_base_pagina": qs_base_pagina,
        "macro_categorias": macro_categorias,
        "categorias_por_macro": categorias_por_macro,
        "sem_subcategoria_valor": SEM_SUBCATEGORIA,
        "sem_subcategoria_rotulo": ROTULO_SEM_SUBCATEGORIA,
        "macros_selecionados": macros_selecionados,
        "categorias_selecionadas": categorias_selecionadas,
        "loja_selecionada": loja_selecionada,
        # Awin já está em MARKETPLACES, então somá-la de novo duplicava a opção no
        # seletor de loja. Ela é condicional: sem integração conectada, filtrar por
        # Awin só devolve lista vazia.
        "lojas": [slug for slug in MARKETPLACES if slug != "awin"] + (
            ["awin"] if IntegracaoAfiliado.objects.filter(
                owner=request.user, provedor="awin", status="conectada",
                habilitada=True).exists() else []),
        "canais": list(SENDERS.keys()),
        "ordenar": ordenar,
        "busca": busca,
        "min_desconto": min_desconto,
        "tipo": tipo,
        "fontes": fontes,
        "afiliacao": afiliacao,
        "afiliacao_ultimo_erro": afiliacao_ultimo_erro,
        "fontes_com_aviso": fontes_com_aviso,
        "fonte_selecionada": fonte_selecionada,
        "confianca_selecionada": confianca_selecionada,
        "atualizado_desde": atualizado_desde,
        "cupom_categorias": cupom_categorias,
        "categoria_cupom_selecionada": categoria_cupom_selecionada,
        "cupom_anunciantes": cupom_anunciantes,
        "anunciante_selecionado": anunciante_selecionado,
        "como_usar_selecionado": como_usar_selecionado,
        "cupons_catalogo": cupons_catalogo,
        "cupons_coletados": cupons_coletados,
        "cupons_elegiveis": cupons_elegiveis,
        "cupons_preparados": cupons_preparados,
        "cupons_prontos": cupons_prontos,
        "cupons_descartados": cupons_descartados,
        "cupons_motivos": cupons_motivos,
        "cupons_aguardando_preparo": cupons_aguardando_preparo,
        "cupons_aguardando_link": cupons_aguardando_link,
        "cupons_aguardando_verificacao": cupons_aguardando_verificacao,
        "cupons_aguardando_conexao": cupons_aguardando_conexao,
        "cupons_fontes_sem_resultado": cupons_fontes_sem_resultado,
        "cupons_em_preparo": cupons_em_preparo,
        "awin_programas": ProgramaAfiliado.objects.filter(
            integracao__owner=request.user, integracao__provedor="awin",
            integracao__status="conectada", habilitado=True,
            status_vinculo="joined", link_status="online").order_by("nome"),
        "manual_coupons_enabled": True,
        "amazon_diagnostico": amazon_diagnostico,
        "so_afiliados": so_afiliados,
        "pendentes_ocultos": pendentes_ocultos,
        # Funil por loja, calculado sobre a MESMA lista paginada — reconcilia com o
        # que a tela mostra, ao contrário de uma contagem própria no banco.
        # União das duas contagens: uma loja com itens só na fila também precisa
        # aparecer — é justamente ela que o usuário procura quando "sumiu".
        "prontos_por_loja": [
            {
                "slug": slug,
                "nome": "Mercado Livre" if slug == "mercadolivre" else slug.title(),
                "prontos": prontos_por_loja.get(slug, 0),
                "pendentes": pendentes_por_loja.get(slug, 0),
            }
            for slug in sorted(set(prontos_por_loja) | set(pendentes_por_loja))
        ],
        "contagem_estrita": contagem_estrita,
        "acao_amazon_tag": (
            not (getattr(perfil, "afiliado_tag_amazon", "") or "").strip()
            and bool(pendentes_por_loja.get("amazon"))
        ),
        "qs_base_sem_afiliado": qs_base_sem_afiliado,
        "filtros_ativos": len(macros_selecionados) + len(categorias_selecionadas)
            + bool(loja_selecionada) + bool(busca) + bool(min_desconto)
            + bool(fonte_selecionada) + bool(confianca_selecionada) + bool(atualizado_desde)
            + bool(categoria_cupom_selecionada) + bool(anunciante_selecionado)
            + bool(como_usar_selecionado),
        "qs_base": qs_base,
        "qs_base_sem_loja": qs_base_sem_loja,
    })


@staff_required
@require_GET
@throttle_sse(10)
def run_scraper_stream(request):
    """SSE endpoint — streams every print() from the scraper to the browser."""

    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            # Nada de bypass de ORM aqui: quem precisa de query com o Playwright
            # aberto usa apps.accounts.tenant.executar_no_tenant (o porquê está lá).
            try:
                with redirect_stdout(writer):
                    scrapper_main()
            except Exception:
                logger.exception("Falha inesperada no scraper principal")
                q.put("[ERRO] Falha inesperada ao processar a solicitação.")
            finally:
                writer.flush()
                q.put(None)  # sentinel

        thread = threading.Thread(
            target=organization_thread_target(request.organization.pk, _run),
            daemon=True,
        )
        thread.start()

        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def _criar_raspagem_manual(request, tipo):
    from apps.scrapers import automacao_state as state
    from apps.scrapers.manual_scraping import criar_execucao, serializar_execucao

    execucao, criada = criar_execucao(
        organization=request.organization,
        usuario=request.user,
        tipo=tipo,
    )
    # Em produção o processo já vive no Procfile. Em desenvolvimento, garante o
    # worker sem ligar o toggle da raspagem automática.
    state.spawn_worker("manual")
    payload = serializar_execucao(execucao)
    payload["reutilizada"] = not criada
    if not criada and execucao.tipo != tipo:
        payload["mensagem"] = (
            "Já existe outra raspagem em andamento para esta organização."
        )
        return JsonResponse(payload, status=409)
    return JsonResponse(payload, status=202 if criada else 200)


@staff_required
@require_POST
def scrape_ofertas_stream(request):
    """Enfileira Promoções; o worker persiste progresso e resultado."""
    return _criar_raspagem_manual(request, "ofertas")


def _sse_runner(fn, organization, *, segurar_transacao=True):
    """Roda fn() capturando prints e streamando via SSE (reusa o padrão _QueueWriter).

    `segurar_transacao=False` para jobs que passam minutos fora do banco (geração de
    links): sem isso o tenant é instalado com um `transaction.atomic()` aberto do
    início ao fim, a conexão fica `idle in transaction` enquanto o Link Builder
    trabalha, e o proxy da Fly a derruba. Dentro de transação o Django nem consegue
    renovar a conexão — só marca `closed_in_transaction`. Quem usa este modo tem de
    passar suas leituras/gravações por `executar_no_tenant`, senão a RLS as bloqueia.
    """
    def _event_stream():
        q: queue.Queue = queue.Queue()
        writer = _QueueWriter(q)

        def _run():
            # Sem bypass de ORM aqui: o Playwright agora é sempre fechado antes de
            # qualquer query (ver auxiliar.iniciar_browser) ou a query vai por
            # apps.accounts.tenant.executar_no_tenant. DJANGO_ALLOW_ASYNC_UNSAFE era
            # setada no os.environ do PROCESSO e nunca restaurada — com 8 threads no
            # gunicorn, o `finally` de outro fluxo a removia no meio deste.
            # O set_event_loop também saiu: o Playwright cria o próprio loop e
            # get_running_loop() ignora o loop "setado" — isso só vazava um epoll por
            # request SSE.
            try:
                with redirect_stdout(writer):
                    fn()
            except Exception:
                logger.exception("Falha inesperada no job SSE %s", fn.__name__)
                q.put("[ERRO] Falha inesperada ao processar a solicitação.")
            finally:
                writer.flush()
                q.put(None)

        threading.Thread(
            target=organization_thread_target(organization, _run,
                                              segurar_transacao=segurar_transacao),
            daemon=True,
        ).start()
        while True:
            line = q.get()
            if line is None:
                yield "data: __DONE__\n\n"
                break
            yield f"data: {line}\n\n"

    response = StreamingHttpResponse(_event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def automacao_control(request):
    """Liga/desliga loops independentes. ?tipo=scrape|envio. POST acao=start|stop.

    scrape = raspagem 24/7 (tela Scraper);  envio = envio pelas regras (tela Envios).
    Um não afeta o outro — processos e PID files separados.
    """
    import sys
    import subprocess
    from apps.scrapers import automacao_state as st

    tipo = request.GET.get("tipo") or request.POST.get("tipo") or "scrape"
    if tipo not in st.JOBS:
        tipo = "scrape"

    # Os workers (scrape/envio) são loops GLOBAIS compartilhados por todos os tenants:
    # ligar/desligar afeta todo mundo, então é controle de infra (staff). O usuário
    # comum liga/desliga o PRÓPRIO envio pelo flag `ativo` de cada regra, sem derrubar
    # o worker dos demais. GET (status) segue liberado para o polling do front.
    # Exceção estreita: o superadmin pode delegar o botão do ENVIO a um usuário
    # (Perfil.pode_ligar_envio). A raspagem continua exclusiva de is_staff — quem tem
    # só a delegação e manda tipo=scrape leva 403 como antes.
    if request.method == "POST":
        autorizado = (pode_ligar_envio(request.user) if tipo == "envio"
                      else request.user.is_staff)
        if not autorizado:
            raise PermissionDenied("Apenas administradores controlam os workers de automação.")

    if request.method != "POST":
        habilitada = st.is_enabled(tipo)
        worker_vivo = st.worker_alive(tipo)
        estado = st.read_state(tipo) if habilitada else {}
        # O estado é global e pode conter diagnóstico gravado por versões antigas.
        # Nunca exponha traceback, caminhos do servidor ou detalhes do banco no UI.
        if estado.get("erro"):
            estado = {**estado, "erro": "Falha temporária no serviço. Uma nova tentativa será feita no próximo ciclo."}
        fase = estado.get("fase", "")
        degradada = fase == "degradado" or bool(estado.get("erro"))
        saudavel = habilitada and worker_vivo and not degradada
        return JsonResponse({
            # Compatibilidade com os clientes antigos: rodando agora significa
            # que o loop foi habilitado E há heartbeat recente.
            "rodando": habilitada and worker_vivo,
            "habilitada": habilitada,
            "worker_vivo": worker_vivo,
            "saudavel": saudavel,
            "tipo": tipo,
            "estado": estado,
        })

    # As telas de Scraper/Envios chamam por fetch e leem o JSON. A Saúde usa um form
    # comum (sem JS), então precisa voltar para uma página: com `next`, redireciona.
    def _responder(payload, msg_ok):
        destino = request.POST.get("next")
        if destino and url_has_allowed_host_and_scheme(
                destino, allowed_hosts={request.get_host()},
                require_https=request.is_secure()):
            messages.success(request, msg_ok)
            return redirect(destino)
        return JsonResponse(payload)

    acao = request.POST.get("acao")
    if acao == "stop":
        st.parar(tipo)
        return _responder({"rodando": False, "tipo": tipo, "msg": "Parado."},
                          f"Worker '{tipo}' desligado.")

    # start — liga o flag; garante que exista um worker. Em prod (honcho) o worker
    # já roda (heartbeat fresco) e o spawn é no-op; em dev (runserver) sobe um
    # subprocess destacado cross-platform. O loop trabalha no próximo ciclo.
    if st.is_running(tipo):
        st.spawn_worker(tipo)  # religa o worker se tiver morrido (dev)
        return _responder({"rodando": True, "tipo": tipo, "msg": "Já estava ligado."},
                          f"Worker '{tipo}' já estava ligado.")
    st.iniciar(tipo)
    st.spawn_worker(tipo)
    return _responder({"rodando": True, "tipo": tipo, "msg": "Ligado."},
                      f"Worker '{tipo}' ligado. O primeiro ciclo roda em instantes.")


@staff_required
@require_POST
def scrape_cupons_codigo_stream(request):
    """Enfileira Cupons; etapas independentes podem concluir parcialmente."""
    return _criar_raspagem_manual(request, "cupons")


@staff_required
@require_GET
def raspagem_atual(request):
    from apps.scrapers.manual_scraping import serializar_execucao

    execucao = (
        ExecucaoRaspagem.objects
        .filter(organization=request.organization)
        .order_by(
            # jobs ativos primeiro; depois, o resultado mais recente
            Case(
                When(status__in=("queued", "running"), then=0),
                default=1,
                output_field=IntegerField(),
            ),
            "-criada_em",
        )
        .first()
    )
    return JsonResponse(
        {"execucao": serializar_execucao(execucao) if execucao else None},
    )


@staff_required
@require_GET
def raspagem_status(request, execucao_id):
    from apps.scrapers.manual_scraping import serializar_execucao

    execucao = ExecucaoRaspagem.objects.filter(
        pk=execucao_id, organization=request.organization,
    ).first()
    if execucao is None:
        return JsonResponse({"erro": "Raspagem não encontrada."}, status=404)
    try:
        after = max(0, int(request.GET.get("after", 0)))
    except (TypeError, ValueError):
        after = 0
    return JsonResponse(serializar_execucao(execucao, after=after))


def _produtos_sem_link(usuario, origens=None, limite=80, macros=None):
    """Produtos visíveis ao usuário que ainda não têm link de afiliado dele.

    Mesmo predicado de `gerar_links_stream` (pendente é por USUÁRIO: o link mora em
    LinkAfiliadoUsuario, não no Produto), fatorado porque o fluxo de cupons passou a
    precisar dele também.
    """
    ja_tem = LinkAfiliadoUsuario.objects.filter(
        usuario=usuario, produto=OuterRef("pk")).exclude(link_afiliado="")
    qs = (
        Produto.objects
        .filter(Q(owner__isnull=True) | Q(owner=usuario))
        .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
        .exclude(Exists(ja_tem))
    )
    from apps.scrapers.maintenance import produtos_frescos_q
    qs = qs.filter(produtos_frescos_q())
    if origens:
        qs = qs.filter(origem__in=origens)
    # Mesmo filtro de categoria da tela de Promoções (macro_categoria): gera link só
    # do nicho escolhido no seletor ao lado do botão.
    if macros:
        qs = qs.filter(macro_categoria__in=macros)
    from apps.scrapers.product_identity import deduplicar_por_produto
    return deduplicar_por_produto(
        qs.order_by("-ultima_observacao", "-id")
    )[:limite]


@require_GET
@throttle_sse(6)
def buscar_termo_stream(request):
    """SSE — busca direcionada por termo de uma config (?config=ID)."""
    from apps.scrapers.marketplaces.registry import get_marketplace
    cfg_id = request.GET.get("config")
    uid = request.user.id

    def _job():
        cfg = ConfiguracaoEnvio.objects.filter(id=cfg_id, owner_id=uid).first()
        if not cfg or not cfg.termo_busca:
            print("[ERRO] Config sem termo de busca.")
            return
        macro = cfg.macro_categoria or None
        # Busca na loja da config (Amazon=Creators API do dono, ML=Playwright compartilhado).
        mp = get_marketplace(cfg.marketplace or "mercadolivre")
        mp.buscar_por_termo(cfg.termo_busca, min_desconto=int(cfg.min_desconto_percent),
                            macro=macro, usuario=cfg.owner)

    return _sse_runner(_job, request.organization)


@require_GET
@throttle_sse(10)
def gerar_links_stream(request):
    """SSE endpoint — prepara links leves e enfileira o browser no worker.

    Não é mais só para staff: a fila é por usuário (LinkAfiliadoUsuario) e a tela de
    Promoções só lista item afiliado, então quem não é staff precisava esperar o
    worker para ter QUALQUER produto enviável.
    """
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.auxiliar import SessaoExpirada
    from apps.scrapers.progresso import usar_reporter
    from apps.scrapers.scraper_mercadolivre.link import (
        AntiBotError, AuthError, LoginError,
    )

    try:
        limite = int(request.GET.get("limite", 50))
    except (TypeError, ValueError):
        limite = 50
    # Filtro opcional de categoria (mesmo campo `macro` da tela de Promoções): gera
    # link só do nicho escolhido no seletor ao lado do botão. Vazio = todos.
    macros = [m for m in request.GET.getlist("macro") if m.strip()]
    uid = request.user.id  # capturado fora da thread

    def _job():
        from django.contrib.auth import get_user_model
        from apps.scrapers.afiliado import frase_resumo_afiliacao
        from apps.accounts.tenant import executar_no_tenant

        # Este job roda com o tenant apenas ANOTADO (segurar_transacao=False, ver
        # _sse_runner): não há transação nem GUC abertos enquanto o Link Builder
        # trabalha. Em troca, TODA ida ao banco passa por executar_no_tenant, que
        # reinstala o escopo numa transação de milissegundos — sem isso a RLS
        # devolveria zero linha.
        usuario = executar_no_tenant(
            lambda: get_user_model().objects.filter(id=uid).first())

        pendentes = executar_no_tenant(
            _produtos_sem_link, usuario, limite=limite, macros=macros or None)
        if not pendentes:
            if macros:
                print(f"Nenhum produto sem link na categoria selecionada ({', '.join(macros)}).")
            else:
                print("Nenhum produto na fila — todos já têm link de afiliado.")
            print(executar_no_tenant(frase_resumo_afiliacao, usuario))
            return
        alvo = f" ({', '.join(macros)})" if macros else ""
        print(f"Gerando link de afiliado para {len(pendentes)} produto(s){alvo}...")

        def _quando_o_robo_volta():
            """" — ele os pega no próximo ciclo, às HH:MM" quando dá para saber."""
            try:
                from apps.scrapers import automacao_state as st
                from django.utils.dateparse import parse_datetime
                proximo = parse_datetime((st.read_state("links") or {})
                                         .get("proximo_ciclo") or "")
                if proximo:
                    return f" — o robô os pega no próximo ciclo, às {timezone.localtime(proximo):%H:%M}"
            except Exception:
                pass
            return " — o robô os pega no próximo ciclo"

        # Agrupa por loja: cada marketplace gera seus links (ML=Playwright,
        # Amazon=puro Python). Evita rodar o Link Builder do ML num ASIN.
        por_loja = {}
        for p in pendentes:
            por_loja.setdefault(p.marketplace or "mercadolivre", []).append(p)
        def _gerar(slug, grupo):
            """Uma loja. Traduz cada falha na AÇÃO que o usuário precisa tomar."""
            try:
                # usar_reporter faz o `emitir_fase("Link i/N")` que já existe em
                # gerar_links_em_lote chegar à caixa de log. Sem ele o progresso ia
                # só para o logger do servidor: o usuário via a primeira linha e
                # mais nada por ~4 minutos, o que fazia qualquer falha parecer
                # instantânea e total. As linhas periódicas também mantêm o stream
                # vivo — um SSE ocioso por minutos é candidato a ser cortado pelo
                # proxy da Fly, o que vira "A conexão foi perdida" na tela.
                with usar_reporter(lambda msg, progresso=None: print(msg)):
                    get_marketplace(slug).prefetch_links(grupo, usuario=usuario)
            except (LoginError, SessaoExpirada) as exc:
                # Sessão morta DE VERDADE: aqui o "Reconectar" resolve. A mensagem
                # da exceção já nomeia o assunto (link.MSG_SESSAO_EXPIRADA /
                # MSG_SEM_SESSAO); o prefixo antigo "Sessão do Mercado Livre
                # expirada:" repetia isso e o usuário lia duas afirmações
                # sobrepostas sobre a mesma coisa.
                print(f"[ERRO] {exc}")
                print("__ML_LOGIN__")
            except AntiBotError:
                # A conta está boa; foi o gateway anti-bot do ML reagindo ao IP do
                # servidor. Oferecer "Reconectar" aqui mandava o usuário refazer um
                # login que estava perfeito.
                print("[AVISO] O Mercado Livre pediu verificação de segurança ao abrir "
                      "o Link Builder (proteção contra robôs, dispara pelo IP do "
                      "servidor). Sua conta está conectada e não há nada a corrigir — "
                      "o robô tenta de novo sozinho.")
            except AuthError:
                print("[AVISO] Não foi possível abrir o Link Builder agora (o Mercado "
                      "Livre não respondeu). Tente de novo em alguns minutos.")
            except Exception as exc:
                print(f"Aviso: geração de links em {slug} falhou ({exc}).")

        # Primeiro as lojas LEVES, fora do lock: a Amazon é Python puro (concatena a
        # tag na URL) e não abre navegador. Fazer um usuário só-Amazon esperar o
        # worker seria dano gratuito.
        for slug, grupo in por_loja.items():
            if slug != "mercadolivre":
                _gerar(slug, grupo)

        grupo_ml = por_loja.get("mercadolivre")
        if grupo_ml:
            # A ausência de LinkAfiliadoUsuario já é a fila durável e idempotente
            # consumida pelo processo `links`. A request web nunca adquire o lease
            # system-only nem abre Chromium: isso preserva FORCE RLS, evita dois
            # browsers na mesma sessão ML e impede uma conexão SSE de ficar presa
            # durante o Link Builder.
            print(f"Seus {len(grupo_ml)} produto(s) do Mercado Livre continuam na "
                  f"fila segura de links{_quando_o_robo_volta()}.")
            print("__LINKS_ENFILEIRADOS__")
        # O resumo é informativo: se ELE falhar, o lote já feito continua válido e
        # não pode ser reportado como erro da operação inteira.
        try:
            print(executar_no_tenant(frase_resumo_afiliacao, usuario))
        except Exception:
            logger.exception("Resumo de afiliação falhou após gerar links")
            print("Links processados. (Não foi possível montar o resumo final.)")

    # segurar_transacao=False: o lote passa minutos no Link Builder, e uma
    # transação aberta esse tempo todo vira `idle in transaction` até o proxy
    # da Fly matar o socket — e dentro de transação o Django nem consegue
    # renovar a conexão. As idas ao banco deste job vão por executar_no_tenant.
    return _sse_runner(_job, request.organization, segurar_transacao=False)

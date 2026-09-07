"""Vigia externo do worker WhatsApp (spreading-wa).

O watchdog interno do worker morre junto quando a VM inteira congela — e foi o que
aconteceu em 08/08: a máquina aceitava TCP em 0,03s e não devolvia uma linha de HTTP
por 20+ minutos, o SSH não entrava e o processo não logava. O Fly NÃO reinicia
máquina por health check critical (o worker é só 6PN, sem [[services]]), então sem
alguém de fora a VM fica travada para sempre. Este módulo é esse alguém: sonda o
/health do worker na cadência interna do `monitor` (POLL=15s) e, depois de falhas
seguidas demais, levanta a máquina pela Fly Machines API — /restart quando ela está
viva e travada, /start quando já caiu (a API recusa /restart em máquina parada).

Decisões:
- Sonda o /health de propósito: é a única rota sem capability auth, então a sonda
  nunca confunde problema de chave/RLS com worker morto.
- Pendurado no POLL de 15s, NÃO no tick de 5min do monitor: com 6 falhas a 15s a
  recuperação leva ~90s em vez de ~30min.
- O contador e o cooldown vivem no cache: sobrevivem a um restart do próprio
  monitor, que é o que impede um loop de restarts.
- O cooldown é armado pela TENTATIVA, não pelo sucesso: uma chamada que falha não
  pode reabrir a temporada de gestos 15s depois (ver _COOLDOWN_TENTATIVA_S).
- Sem FLY_API_TOKEN (dev) loga uma vez e vira no-op, como o snapshot do fly_infra.
"""
from __future__ import annotations

import logging
import time

import requests
from django.conf import settings
from django.core.cache import caches

from apps.scrapers import fly_infra
from apps.scrapers.eventos import log_event

logger = logging.getLogger(__name__)


def _cache():
    """Cache PERSISTENTE, não o `default`.

    O `default` em produção é LocMemCache: por processo e morto a cada deploy. A
    docstring do silêncio abaixo promete sobreviver a um restart do monitor — e
    não sobrevivia, então cada deploy rearmava o alarme da mesma sessão. Em
    06/09/2026 isso rendeu 24 eventos idênticos em seis horas.

    Resolvido a cada chamada, não no import: prender o handle no import congela a
    configuração e faz `override_settings(CACHES=...)` deixar de valer.
    """
    return caches["persistente"]

_FALHAS_KEY = "wa_supervisor_falhas"
_COOLDOWN_KEY = "wa_supervisor_cooldown"
# Sessão travada não se resolve com restart — `expirado`, `falha_auth` e
# `recuperacao_pausada` são estados dos quais a sessão não sai sozinha e que só
# um humano reconecta. Por isso este caso NÃO entra no contador de falhas: ele
# vira incidente, que é o que chama alguém. Uma hora de silêncio por chave evita
# encher EventoOperacional a cada sonda enquanto ninguém reconecta.
_TRAVADA_KEY = "wa_supervisor_sessao_travada_avisada"
_TRAVADA_SILENCIO_S = 3600
# Marca desde quando a contagem está zerada. Ver `_avisar_sessao_travada`.
_SAUDAVEL_DESDE_KEY = "wa_supervisor_saudavel_desde"
# Quanto tempo de contagem zerada conta como recuperação de verdade.
_RECUPERACAO_ESTAVEL_S = 900
# O contador expira sozinho: se o monitor morrer no meio de uma sequência de
# falhas, a contagem velha não pode viver para sempre e assombrar o processo novo.
_FALHAS_TTL_S = 600
# Cooldown curto armado por TENTATIVA (não por sucesso). Uma tentativa que falha
# não pode voltar em 15s: em 18/08 o /restart devolveu 409 (máquina parada), a
# exceção pulou o cooldown, e o vigia passou a bater de 15 em 15s — cada chamada
# derrubava o boot do worker no meio (ele leva ~60s), deixando o WhatsApp fora do
# ar por 8h. 120s > um boot inteiro, então nunca matamos uma VM que está subindo.
_COOLDOWN_TENTATIVA_S = 120
# Estados em que a máquina JÁ está indo para 'started' sozinha: mexer nela agora
# só interrompe a transição.
_ESTADOS_EM_TRANSICAO = frozenset(
    {"created", "starting", "stopping", "replacing", "destroying"}
)
_avisou_sem_token = False
# Último corpo do /health, preenchido por _sonda_saudavel (ver docstring de lá).
_ULTIMO_CORPO: dict = {}


def decidir_acao(*, token: str, falhas_seguidas: int, em_cooldown: bool,
                 falhas_limite: int) -> str:
    """Decisão pura do vigia: 'noop' | 'aguardar' | 'reiniciar'.

    O restart é o gesto mais destrutivo disponível (derruba a VM no hardware),
    então ele exige o limite inteiro de falhas SEGUIDAS e respeita o cooldown —
    um boot de worker leva ~1min e sondar durante o subir seria falso positivo.
    """
    if not token:
        return "noop"
    if falhas_seguidas < falhas_limite:
        return "aguardar"
    if em_cooldown:
        return "aguardar"
    return "reiniciar"


def gesto_para_estado(estado: str) -> str:
    """'restart' | 'start' | 'aguardar' — o gesto certo p/ o estado da máquina.

    A Fly Machines API recusa /restart em máquina parada (409 Conflict), e o
    vigia antigo só conhecia o /restart: com a VM em 'stopped' ele nunca
    conseguia levantá-la. Estado desconhecido cai em 'restart', que é o gesto
    da máquina viva e o comportamento histórico.
    """
    estado = (estado or "").strip().lower()
    if estado in _ESTADOS_EM_TRANSICAO:
        return "aguardar"
    if estado in ("stopped", "suspended"):
        return "start"
    return "restart"


def _sonda_saudavel() -> bool:
    """Liveness do processo, e só isso. Continua sendo o gatilho do restart.

    O corpo da resposta fica em `_ULTIMO_CORPO` porque ele carrega uma informação
    de outra natureza — o estado das sessões — que exige outro tipo de resposta
    (avisar um humano, não reiniciar a VM). Guardar em vez de devolver mantém a
    assinatura que o resto do módulo e os testes já usam, e evita uma segunda
    requisição a cada sonda de 15s só para ler dois contadores.
    """
    global _ULTIMO_CORPO
    # timeout=(connect, read): com a VM travada o connect ainda completa rápido
    # (é o kernel que aceita); quem delata o travamento é o READ não responder.
    url = f"{settings.WHATSAPP_API_URL.rstrip('/')}/health"
    _ULTIMO_CORPO = {}
    try:
        resp = requests.get(url, timeout=(2, 5))
        if resp.status_code != 200:
            return False
        try:
            corpo = resp.json()
        except ValueError:
            corpo = {}
        if isinstance(corpo, dict):
            _ULTIMO_CORPO = corpo
        return True
    except requests.RequestException:
        return False


def _avisar_sessao_travada(corpo: dict) -> None:
    """O worker responde, mas a sessão está num estado do qual não sai sozinha.

    Era o buraco do vigia: `/health` só dizia se o processo estava mudo, então
    sessão expirada ou em `recuperacao_pausada` podia durar horas sem ninguém
    saber — a tela de Saúde mostrava, e ninguém abre a tela de Saúde. Restart
    aqui seria pior que inútil (a sessão volta no mesmo estado e o loop recomeça),
    então o gesto certo é abrir incidente, que é o que aciona o canal de alerta.
    """
    try:
        travadas = int(corpo.get("sessions_stuck") or 0)
        # Sessão que JÁ foi pareada e voltou ao QR conta igual. Ela não está em fase
        # terminal — está "esperando alguém escanear" —, e é exatamente por isso que
        # passou despercebida em 06/09/2026: pareada em 04/09 12:31, de volta ao QR
        # sem nenhum evento de logout, tratada pelo sistema como instalação nova.
        # Instalação nova pode esperar; sessão que caiu, não.
        repareamento = int(corpo.get("sessions_repareamento") or 0)
    except (TypeError, ValueError):
        return
    total = travadas + repareamento
    if total <= 0:
        # Zero numa sonda não é recuperação: uma sessão que oscila entre "travada"
        # e "sumiu do Map" zerava a contagem por um instante, apagava o silêncio, e
        # o alarme voltava no próximo tique. Era isso, e não uma queda nova, que
        # gerava um evento a cada poucos minutos. Só um período contínuo de saúde
        # devolve o direito de alarmar.
        primeiro_zero = _cache().get(_SAUDAVEL_DESDE_KEY)
        agora = time.monotonic()
        if primeiro_zero is None:
            _cache().set(_SAUDAVEL_DESDE_KEY, agora, timeout=_TRAVADA_SILENCIO_S)
        elif agora - float(primeiro_zero) >= _RECUPERACAO_ESTAVEL_S:
            _cache().delete(_TRAVADA_KEY)
            _cache().delete(_SAUDAVEL_DESDE_KEY)
        return
    _cache().delete(_SAUDAVEL_DESDE_KEY)
    if _cache().get(_TRAVADA_KEY):
        return
    _cache().set(_TRAVADA_KEY, True, timeout=_TRAVADA_SILENCIO_S)
    if repareamento and not travadas:
        motivo = (f"{repareamento} sessão(ões) WhatsApp caíram e estão pedindo QR de "
                  f"novo. O worker responde, mas não envia até alguém reconectar.")
    elif travadas and not repareamento:
        motivo = (f"{travadas} sessão(ões) WhatsApp em estado terminal "
                  f"(expirado/falha_auth/recuperacao_pausada). O worker responde, "
                  f"mas não envia: precisa reconectar.")
    else:
        motivo = (f"{travadas} sessão(ões) WhatsApp em estado terminal e "
                  f"{repareamento} pedindo QR de novo. Nenhuma delas envia.")
    logger.error("wa_supervisor: %s", motivo)
    log_event(
        "whatsapp", "sessao_travada", motivo,
        level="error",
        contexto={
            "sessions_stuck": travadas,
            "sessions_repareamento": repareamento,
            "sessions_ready": corpo.get("sessions_ready"),
            "sessions_total": corpo.get("sessions_total"),
        },
    )


def _armar_cooldown(segundos: int) -> None:
    _cache().set(_COOLDOWN_KEY, time.time(), timeout=segundos)
    # Zera a contagem: o boot leva ~1min e as sondas desse período não podem
    # herdar as falhas da encarnação anterior.
    _cache().set(_FALHAS_KEY, 0, timeout=_FALHAS_TTL_S)


def _recuperar(falhas: int) -> str:
    """Levanta o worker de volta. Devolve o gesto tomado (p/ log)."""
    app = settings.WA_MACHINE_APP
    # O cooldown curto é armado ANTES de tocar na API: uma tentativa que falha
    # (409/412/timeout) sai por exceção e pularia o cooldown lá embaixo, e é
    # exatamente esse buraco que virou um loop de restarts de 15 em 15s.
    _armar_cooldown(_COOLDOWN_TENTATIVA_S)
    # Descobre o id pela API em cada restart: id fixo no código ficaria obsoleto
    # no primeiro replace da máquina. O app é de máquina única por desenho
    # (volume wa_data), então a primeira é a única.
    maquinas = fly_infra._listar_maquinas(app)
    if not maquinas:
        raise RuntimeError(f"nenhuma máquina encontrada no app {app}")
    alvo = maquinas[0]["id"]
    estado = str(maquinas[0].get("estado") or "")
    gesto = gesto_para_estado(estado)

    if gesto == "aguardar":
        logger.warning(
            "wa_supervisor: máquina %s (%s) em '%s'; já está subindo, sem gesto.",
            alvo, app, estado,
        )
        return "aguardar"

    if gesto == "start":
        fly_infra.iniciar_maquina(app, alvo)
        acao_humana = "ligada"
    else:
        fly_infra.reiniciar_maquina(app, alvo)
        acao_humana = "reiniciada"

    _armar_cooldown(settings.WA_SUPERVISOR_COOLDOWN_MIN * 60)
    logger.error(
        "wa_supervisor: worker sem responder em %s sondas; máquina %s (%s) %s (estado '%s').",
        falhas, alvo, app, acao_humana, estado or "?",
    )
    log_event(
        "whatsapp", "worker_reiniciado",
        f"Worker WhatsApp sem responder ao /health em {falhas} sondas seguidas; "
        f"máquina {alvo} ({app}) {acao_humana} pela API do Fly.",
        level="error",
        contexto={"app": app, "machine_id": alvo, "falhas_seguidas": falhas,
                  "estado": estado, "gesto": gesto},
    )
    return gesto


def verificar() -> str:
    """Uma passada do vigia. Nunca levanta exceção: o monitor não pode cair por
    causa de quem existe para protegê-lo. Devolve a ação tomada (p/ log)."""
    global _avisou_sem_token
    if not settings.WA_SUPERVISOR_ENABLED:
        return "desligado"
    if not settings.FLY_API_TOKEN:
        if not _avisou_sem_token:
            logger.info(
                "wa_supervisor: FLY_API_TOKEN não configurado; vigia externo em no-op (dev)."
            )
            _avisou_sem_token = True
        return "sem_token"
    try:
        if _sonda_saudavel():
            _cache().set(_FALHAS_KEY, 0, timeout=_FALHAS_TTL_S)
            _avisar_sessao_travada(_ULTIMO_CORPO)
            return "ok"
        falhas = _cache().get(_FALHAS_KEY, 0) + 1
        _cache().set(_FALHAS_KEY, falhas, timeout=_FALHAS_TTL_S)
        acao = decidir_acao(
            token=settings.FLY_API_TOKEN,
            falhas_seguidas=falhas,
            em_cooldown=bool(_cache().get(_COOLDOWN_KEY)),
            falhas_limite=settings.WA_SUPERVISOR_FALHAS,
        )
        if acao != "reiniciar":
            logger.warning(
                "wa_supervisor: /health do worker fora do ar (%s falha(s) seguidas).", falhas
            )
            return acao
        if _recuperar(falhas) == "aguardar":
            return "aguardar"
        return "reiniciado"
    except Exception as e:  # API Fly fora, cache fora — tenta de novo na próxima
        logger.warning("wa_supervisor: passada do vigia falhou: %s", e)
        return "erro"

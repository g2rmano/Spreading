"""
Cliente HTTP fino para o serviço Node de WhatsApp (node.js/index.js).

O Node expõe rotas privadas protegidas por capacidades Ed25519 curtas:
  POST /api/enviar         -> texto: {grupoid|numero, mensagem}
                              midia: {grupoid|numero, base64, mimetype, nomeArquivo, legenda}
  POST /api/sessoes        -> cria/revive a sessão do usuário
  POST /api/sessoes/reset  -> descarta a sessão e inicia um QR novo atomicamente
  POST /api/sessoes/logout -> desfaz o pareamento (revoga + limpa credencial)
  GET  /api/status         -> {conectado, fase, progresso, mensagem, grupos, qr, ...}
  GET  /api/grupos         -> {conectado, fase, sincronizando, grupos_indisponivel, grupos}
  POST /api/grupos/refresh -> {sucesso, ...mesmo payload de /api/grupos}

Toda função retorna dicts simples; nunca levanta por falha de envio
(o orquestrador decide o que fazer com sucesso=False).

CONTRATO: a chave "erro" num payload de status/grupos significa exclusivamente
que o Node está inalcançável — ela só é injetada por _request_json, e o Node não
emite "erro" para estados normais (sincronizando, desconectado, capacidade). O
front depende disso para distinguir "serviço fora do ar" de "WhatsApp
desconectado". Não adicione raise_for_status nem checagem de status_code aqui:
o significado tem de vir do corpo.
"""
import time
import uuid

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

# Classificação de falha de envio. Espelha node.js/error_taxonomy.js — o Node
# manda a `classe` no corpo de /api/enviar e ela tem precedência aqui.
#
# Existe porque o orquestrador (ofertas.processar_configs_de_envio) conta
# falhas_consecutivas e desliga a ConfiguracaoEnvio ao bater o teto. Sem separar
# "o worker piscou" de "o grupo foi apagado", algumas horas de indisponibilidade
# desligavam a automação sozinhas e nada a religava.
TRANSITORIO = "transitorio"
PERMANENTE = "permanente"
DESCONHECIDO = "desconhecido"

_CLASSES = frozenset({TRANSITORIO, PERMANENTE, DESCONHECIDO})
_SEND_HTTP_TIMEOUT_S = 65

_SAFE_SEND_FIELDS = frozenset({
    "sucesso", "via", "tipo", "instancia", "mensagem_id", "confirmacao",
    "resultado", "repetir", "ack", "etapa", "duracao_ms", "falha_infra",
    "classe", "erro", "causa",
    # Quando a mensagem saiu, quanto o transporte demorou e quanto o ACK do
    # WhatsApp levou para chegar. Sem estes três a tela volta a dizer só
    # "Enviado": o worker passou a devolvê-los, e a allowlist os apagava antes de
    # o Django ler. Medido no canário de 07/09: `ack=1` chegava, `ack_ms` não.
    "enviado_em", "ack_ms", "transporte_ms",
})


def _safe_send_body(body) -> dict:
    """Aceita somente telemetria conhecida e redige texto vindo do worker.

    Mesmo sendo um serviço interno, o Node processa erros do navegador e conteúdo
    de terceiros. Nunca confie que um corpo inesperado não ecoará capability,
    destino, mídia ou cookie para o banco/dashboard Django.
    """
    if not isinstance(body, dict):
        return {"erro": "O worker WhatsApp devolveu uma resposta ilegível."}
    from core.logging import redact_log_text

    result = {}
    for key in _SAFE_SEND_FIELDS:
        if key not in body:
            continue
        value = body[key]
        if key in {"erro", "causa", "etapa", "instancia", "mensagem_id",
                   "confirmacao", "resultado", "via", "tipo", "classe"}:
            result[key] = redact_log_text(value)[:500]
        elif key == "duracao_ms":
            try:
                result[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        elif key == "ack":
            result[key] = value if isinstance(value, int) else None
        elif key in {"sucesso", "repetir", "falha_infra"}:
            result[key] = bool(value)
    return result


class WhatsAppError(Exception):
    """Falha de configuração/transporte ao falar com o serviço Node."""
    pass


def _classe_do_corpo(corpo) -> str | None:
    """A classe que o Node mandou, se ele for uma versão que a conhece.

    Node antigo no ar não manda o campo → None → o chamador cai em DESCONHECIDO,
    que é o comportamento anterior a esta taxonomia. Deploy fora de ordem
    (Django novo, Node velho) degrada em vez de quebrar.
    """
    if not isinstance(corpo, dict):
        return None
    classe = corpo.get("classe")
    return classe if classe in _CLASSES else None


def _classe_do_status(status_code: int) -> str:
    """Classifica pelo HTTP quando o Node não disse nada.

    429 (limiter) e 503 (sessão fora do ar / capacidade) somem sozinhos; 5xx é
    worker doente, não config errada. Os demais 4xx são pedido malformado: só
    ação humana resolve, então pausar a config é a atitude certa.
    """
    if status_code == 429 or status_code >= 500:
        return TRANSITORIO
    if 400 <= status_code < 500:
        return PERMANENTE
    return DESCONHECIDO


def _base_url() -> str:
    return settings.WHATSAPP_API_URL.rstrip("/")


def _headers(session, action: str, *, single_use=False) -> dict:
    """Capacidade curta por organização; não existe chave mestre de fallback."""
    try:
        from apps.accounts.wa_capabilities import issue_capability
        token = issue_capability(session, [action], single_use=single_use)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
    except Exception as exc:
        raise WhatsAppError(
            "Não foi possível autorizar a sessão WhatsApp."
        ) from exc


def _disabled_payload():
    return {
        "sucesso": False,
        "conectado": False,
        "fase": "indisponivel",
        "mensagem": "WhatsApp Web está desativado para esta organização.",
    }


def _enabled(session=None):
    from apps.accounts.feature_flags import enabled_for_whatsapp_session
    return enabled_for_whatsapp_session(session)


def _params(session=None) -> dict:
    """Query da sessão (multi-tenant). Node multi-cliente usa ?session=<clientId>.
    None = sessão única/global (compat). Node antigo ignora o param."""
    return {"session": session} if session else None


# Connect curto, read no valor do chamador: numa 6PN saudável o connect completa
# em ~0,03s, então esperar mais de 2s por ele nunca vale — com a máquina morta é
# o CONNECT que pendura, e 2s × 2 tentativas cortam o pior caso de ~10s para ~4s.
# (Com a VM travada mas aceitando TCP quem delata é o read, que segue do chamador.)
_TIMEOUT_CONNECT_S = 2


def _request_json(method: str, path: str, *, headers=None, params=None, json=None,
                  timeout=5, attempts=2) -> dict:
    """`timeout` é o orçamento de READ de cada tentativa; o connect usa
    _TIMEOUT_CONNECT_S. O contrato {"erro": ...} é deliberado: nada de
    raise_for_status aqui."""
    url = f"{_base_url()}{path}"
    last_error = None
    for attempt in range(attempts):
        try:
            r = requests.request(method, url, headers=headers, params=params,
                                 json=json, timeout=(_TIMEOUT_CONNECT_S, timeout))
            return r.json()
        except Exception as e:
            last_error = e
            if attempt + 1 < attempts:
                time.sleep(0.35)
    return {
        "erro": "Não foi possível falar com o serviço de WhatsApp.",
        "causa": type(last_error).__name__ if last_error else "transport_error",
    }


_STATUS_TTL_S = 5


def _chave_status(session=None) -> str:
    return f"wa_status:{session or '_global'}"


def invalidar_status(session=None) -> None:
    """Descarta o status cacheado — chame depois de mexer no pareamento."""
    cache.delete(_chave_status(session))


def _persistir_telemetria(session, data) -> None:
    """Projeção compartilhada do payload seguro do Node; nunca persiste QR/erro cru."""
    if not session or not isinstance(data, dict):
        return
    from apps.accounts.models import WhatsAppConnection
    from apps.accounts.tenant import executar_orm_ou_direto

    capacidade = data.get("capacidade") if isinstance(data.get("capacidade"), dict) else {}
    fase = str(data.get("fase") or "")
    consistencia = str(data.get("consistency_status") or "")
    if consistencia not in {"unknown", "consistent", "adopted", "orphan", "mismatch"}:
        consistencia = ""
    numero = str(data.get("numero_mascarado") or "")
    if numero and (not numero.startswith("***") or not numero[3:].isdigit()):
        numero = ""
    fields = {
        "status": "active" if data.get("conectado", fase == "conectado") else "inactive",
        "worker_instance": str(data.get("worker") or "")[:120],
        "phase": fase[:32],
        "masked_number": numero[:24],
        "last_event": str(data.get("ultimo_evento") or "")[:80],
        "last_event_at": timezone.now(),
        "unavailable_reason": str(
            data.get("motivo_indisponibilidade") or (
                "worker_unavailable" if data.get("erro") else ""
            )
        )[:80],
        "capacity_used": max(0, int(capacidade.get("usadas") or 0)),
        "capacity_max": max(0, int(capacidade.get("maximas") or 0)),
        "consistency_status": consistencia or (
            "mismatch" if fase == "inconsistente" else "consistent"
        ),
    }

    def _update():
        WhatsAppConnection.objects.filter(instance_id=str(session)).update(**fields)

    try:
        executar_orm_ou_direto(_update)
    except Exception:
        # Telemetria jamais pode transformar uma leitura de status em indisponibilidade.
        return


def status(session=None) -> dict:
    """Retorna {conectado: bool}. Exige api-key (rota fechada).

    Cacheado por _STATUS_TTL_S porque o painel faz polling e cada chamada custa um
    request ao Node — que, com a máquina fora do ar, leva ~4s (connect 2s × 2
    tentativas; ver _TIMEOUT_CONNECT_S) segurando uma thread do gunicorn. Com poucas
    threads e várias abas abertas isso derrubava o app inteiro. Cachear a resposta
    de erro é intencional: é justamente quando não se deve martelar o Node.
    """
    if not _enabled(session):
        return _disabled_payload()
    chave = _chave_status(session)
    cacheado = cache.get(chave)
    if cacheado is not None:
        return cacheado
    data = _request_json("GET", "/api/status", headers=_headers(session, "status"),
                         params=_params(session), timeout=5)
    data.setdefault("conectado", False)
    _persistir_telemetria(session, data)
    cache.set(chave, data, timeout=_STATUS_TTL_S)
    return data


def iniciar_sessao(session) -> dict:
    """Inicia explicitamente o único runtime WhatsApp deste usuário."""
    if not session:
        return {"sucesso": False, "erro": "Sessão de usuário ausente."}
    if not _enabled(session):
        return _disabled_payload()
    invalidar_status(session)
    return _request_json(
        "POST", "/api/sessoes", headers=_headers(session, "provision", single_use=True),
        json={"session": session}, timeout=10, attempts=1,
    )


def desconectar(session) -> dict:
    """Desfaz o pareamento: revoga no celular e apaga a credencial do volume."""
    if not session:
        return {"sucesso": False, "erro": "Sessão de usuário ausente."}
    if not _enabled(session):
        return _disabled_payload()
    invalidar_status(session)
    # timeout 25 > os 15s do client.logout no Node + margem do destroy.
    # attempts=1 de propósito: o retry cego de _request_json dobraria a espera
    # para 50s, e o Node já trata o logout como idempotente.
    return _request_json(
        "POST", "/api/sessoes/logout",
        headers=_headers(session, "logout", single_use=True),
        json={"session": session}, timeout=25, attempts=1,
    )


def reiniciar_com_qr(session) -> dict:
    """Descarta a sessão atual e inicia uma sessão limpa para emitir novo QR."""
    if not session:
        return {"sucesso": False, "erro": "Sessão de usuário ausente."}
    if not _enabled(session):
        return _disabled_payload()
    invalidar_status(session)
    try:
        # attempts=1: repetir um reset cujo resultado se perdeu pode derrubar o
        # Chromium novo que já está gerando o QR. O Node coalesce concorrência.
        return _request_json(
            "POST", "/api/sessoes/reset",
            headers=_headers(session, "reset", single_use=True),
            json={"session": session}, timeout=25, attempts=1,
        )
    finally:
        # Um GET concorrente pode repopular o cache entre a invalidação acima e
        # o fim do reset. Limpar de novo impede a UI de reviver estado antigo.
        invalidar_status(session)


def reconciliar_sessao(session) -> dict:
    if not session or not _enabled(session):
        return _disabled_payload()
    cache_key = f"wa_reconcile:{session}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    data = _request_json(
        "POST", "/api/sessoes/reconcile",
        headers=_headers(session, "session_reconcile"),
        json={"session": session}, timeout=10, attempts=1,
    )
    if "erro" not in data:
        _persistir_telemetria(session, {
            "fase": data.get("runtime"),
            "worker": data.get("worker"),
            "capacidade": data.get("capacidade"),
            "consistency_status": (
                "mismatch" if data.get("consistencia") == "manifest_mismatch"
                else data.get("consistencia")
            ),
            "motivo_indisponibilidade": (
                "" if data.get("consistencia") in {"consistent", "adopted"}
                else data.get("consistencia")
            ),
        })
    cache.set(
        cache_key, data,
        timeout=(
            max(1, int(settings.WA_SESSION_RECONCILE_SECONDS))
            if "erro" not in data else 10
        ),
    )
    return data


def consultar_operacao(session, operation_key) -> dict:
    if not session or not operation_key:
        return {"encontrado": False}
    from urllib.parse import quote
    return _request_json(
        "GET", f"/api/envios/status/{quote(str(operation_key), safe='')}",
        headers=_headers(session, "send_status"), params=_params(session),
        timeout=8, attempts=1,
    )


def qrcode(session=None) -> dict:
    """Retorna {conectado, qr?} do serviço Node. Exige api-key (rota fechada)."""
    if not _enabled(session):
        return _disabled_payload()
    data = _request_json("GET", "/api/qrcode", headers=_headers(session, "status"),
                         params=_params(session), timeout=8)
    data.setdefault("conectado", False)
    data.setdefault("qr", None)
    return data


def listar_grupos(session=None) -> dict:
    """Lista grupos do WhatsApp conectado. Usado pelo dashboard para escolher destino."""
    if not _enabled(session):
        return _disabled_payload()
    return _request_json("GET", "/api/grupos", headers=_headers(session, "groups"),
                         params=_params(session), timeout=15)


def refresh_grupos(session=None) -> dict:
    """Força o Node a re-sincronizar a lista de grupos. POST /api/grupos/refresh."""
    # attempts=1 de propósito: este POST não é idempotente. Se o Node aceita o
    # pedido mas a resposta estoura os 30s, o retry cego de _request_json dispara
    # um segundo refresh — e um segundo getChats no mesmo Chromium — além de
    # dobrar a espera do usuário para 60s. O Node já coalesce pedidos repetidos
    # num único repique (group_sync.js), então insistir aqui só custa.
    if not _enabled(session):
        return _disabled_payload()
    data = _request_json("POST", "/api/grupos/refresh",
                         headers=_headers(session, "groups"),
                         params=_params(session), timeout=30, attempts=1)
    if "erro" in data:
        data.setdefault("sucesso", False)
    return data


def diagnosticar(session=None, grupoid: str = "") -> dict:
    """Confere sessão e grupo sem criar ou enviar uma mensagem."""
    if not session:
        return {"sucesso": False, "causa": "whatsapp_desconectado",
                "mensagem": "Sessão WhatsApp do usuário ausente."}
    if not _enabled(session):
        return _disabled_payload()
    data = _request_json(
        "POST", "/api/diagnostico", headers=_headers(session, "status"),
        params=_params(session),
        json={"session": session, "grupoid": grupoid}, timeout=30, attempts=1,
    )
    if "erro" in data:
        return {"sucesso": False, "causa": "whatsapp_transporte",
                "mensagem": "Não foi possível falar com o serviço WhatsApp."}
    return data


def enviar_oferta(grupoid: str, mensagem: str, imagem_base64: str = None,
                  mimetype: str = "image/jpeg", legenda: str = None,
                  session=None, idempotency_key: str = None) -> dict:
    """
    Envia uma oferta para um grupo (ou número) via serviço Node.

    Args:
        grupoid: id do grupo (ex '12345@g.us'), obrigatório.
        mensagem: texto da oferta (vira legenda quando há imagem).
        imagem_base64: opcional; se informado envia mídia em vez de texto puro.

    Returns:
        {sucesso: bool, via?: 'local'|'evolution', erro?: str, classe?: str}

    Toda falha carrega `classe` (TRANSITORIO/PERMANENTE/DESCONHECIDO); o
    orquestrador usa isso para decidir se conta a falha contra a config.
    """
    # Sem destino padrão: num app multi-tenant o grupo vem sempre de
    # ConfiguracaoEnvio.grupo_id do usuário. Um "grupo global" mandaria a oferta
    # de um usuário para o grupo de outro.
    if not grupoid:
        return {"sucesso": False, "erro": "Nenhum grupoid informado.",
                "classe": PERMANENTE}
    if not _enabled(session):
        return {**_disabled_payload(), "classe": TRANSITORIO}

    idempotency_key = str(idempotency_key or uuid.uuid4())
    if not 8 <= len(idempotency_key) <= 128:
        return {"sucesso": False, "erro": "Identidade idempotente inválida.",
                "classe": PERMANENTE}

    payload = {"grupoid": grupoid}
    if session:
        payload["session"] = session   # Node multi-cliente roteia pela sessão do usuário
    if imagem_base64:
        payload.update({
            "base64": imagem_base64,
            "mimetype": mimetype,
            "legenda": legenda or mensagem,
            "nomeArquivo": "oferta.jpg",
        })
    else:
        payload["mensagem"] = mensagem

    try:
        inicio = time.monotonic()
        r = requests.post(
            f"{_base_url()}/api/enviar",
            json=payload,
            headers={
                **_headers(session, "send"),
                "Idempotency-Key": idempotency_key,
            },
            timeout=_SEND_HTTP_TIMEOUT_S,
        )
        try:
            corpo = _safe_send_body(r.json())
        except ValueError:
            corpo = {"erro": "O worker WhatsApp devolveu uma resposta ilegível."}
        # Node devolve 200 com sucesso:true, ou 4xx/503 com erro.
        if r.status_code == 200 and corpo.get("sucesso") and corpo.get("mensagem_id"):
            return corpo
        if r.status_code == 200 and corpo.get("sucesso"):
            # Regressão do Node (sucesso sem id). Transitório: a config do
            # usuário está correta e nada que ele faça resolve — quem conserta é
            # um deploy. Pausá-lo puniria o usuário pelo nosso bug.
            return {
                **corpo,
                "sucesso": False,
                "status": r.status_code,
                "erro": "WhatsApp não devolveu o ID de confirmação da mensagem.",
                "classe": TRANSITORIO,
            }
        classe = _classe_do_corpo(corpo) or _classe_do_status(r.status_code)
        return {**corpo, "sucesso": False, "status": r.status_code, "classe": classe,
                "duracao_ms": corpo.get("duracao_ms", round((time.monotonic() - inicio) * 1000))}
    except WhatsAppError as e:
        # Falha no signer/vínculo não é defeito da configuração do usuário.
        return {"sucesso": False,
                "erro": "Não foi possível autorizar a sessão WhatsApp.",
                "causa": type(e).__name__, "classe": TRANSITORIO}
    except (requests.Timeout, requests.ConnectionError) as e:
        # Os dois piores casos nunca chegam classificados: o Node não responde
        # (worker reiniciando/deploy) ou demora mais que o timeout. Ambos somem
        # sozinhos — mas um Timeout pode acontecer com a mensagem JÁ despachada
        # pelo Chromium. Classificar como transitório simples re-seleciona o
        # produto num tick futuro (Publicacao nova ⇒ Idempotency-Key nova, o
        # ledger do Node nunca deduplica) e a oferta sai DUAS vezes no grupo.
        # O ledger sabe em que fase o envio parou; pergunte antes de rotular.
        consulta = consultar_operacao(session, idempotency_key)
        fase = consulta.get("fase") if consulta.get("encontrado") else None
        if fase == "confirmed":
            resultado = consulta.get("resultado") or {}
            if resultado.get("sucesso") and resultado.get("mensagem_id"):
                # O Node concluiu o envio depois do nosso prazo de leitura.
                return resultado
        if fase in ("transport_started", "uncertain"):
            return {"sucesso": False, "resultado": "incerto", "repetir": False,
                    "erro": "O envio foi iniciado, mas a confirmação não chegou "
                            "a tempo; confirme no grupo antes de repetir.",
                    "causa": type(e).__name__,
                    "classe": TRANSITORIO, "etapa": "http",
                    "duracao_ms": _SEND_HTTP_TIMEOUT_S * 1000,
                    "falha_infra": True}
        # 'selected'/'preflight_failed'/ausente (ou ledger inalcançável): o
        # sendMessage não chegou a começar — repetir continua seguro.
        return {"sucesso": False,
                "erro": "O serviço WhatsApp não respondeu dentro do prazo.",
                "causa": type(e).__name__,
                "classe": TRANSITORIO, "etapa": "http",
                "duracao_ms": _SEND_HTTP_TIMEOUT_S * 1000,
                "falha_infra": True}
    except Exception as e:
        return {"sucesso": False,
                "erro": "Falha inesperada ao acessar o serviço WhatsApp.",
                "causa": type(e).__name__, "classe": DESCONHECIDO}

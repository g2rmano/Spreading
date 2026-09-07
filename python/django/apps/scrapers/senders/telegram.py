"""
Telegram Sender — Bot API (HTTP puro, sem browser).

Vantagens sobre o WhatsApp atual: API oficial, aceita URL de imagem direto (dispensa
o download/conversão webp->jpeg), Markdown/HTML nativo, sem risco de ban por automação.

Setup: o usuário cria um bot no @BotFather e cola o token na tela Conexão Telegram
(salvo por-usuário no Perfil). Depois adiciona o bot como ADMIN do canal/grupo.
`destino` = '@nomedocanal' ou id numérico (ex '-1001234567890').
"""
import requests
import time
import re
import base64
from django.conf import settings

from apps.scrapers.senders.base import Sender, TelegramHTMLMarkup, padronizar_resultado


def resolver_token(usuario=None) -> str:
    """Token do bot do usuário (Perfil); se vazio, cai no global de settings."""
    if usuario is not None:
        perfil = getattr(usuario, "perfil", None)
        if perfil and perfil.telegram_bot_token:
            return str(perfil.telegram_bot_token).strip()
        return ""
    return (getattr(settings, "TELEGRAM_BOT_TOKEN", "") or "").strip()


def _sem_token(texto: str, token: str) -> str:
    """Tira o token do bot do texto do erro.

    O token vive no CAMINHO da URL (`api.telegram.org/bot<id>:<segredo>/…`), e é
    exatamente isso que `requests` coloca na mensagem de `ConnectionError`. A
    redação de log do projeto cobre query-string, `Bearer` e `campo=valor` — não
    cobre caminho — então o segredo ia inteiro para `EventoOperacional.contexto`
    e para o Sentry.
    """
    limpo = str(texto or "")
    if token:
        limpo = limpo.replace(token, "<token>")
    # Rede de segurança: qualquer `/bot<algo>:<algo>/` que sobre vira máscara,
    # inclusive de um token que não seja o desta chamada.
    return re.sub(r"/bot\d+:[A-Za-z0-9_-]+", "/bot<token>", limpo)


class TelegramSender(Sender):
    slug = "telegram"
    prefers_image = "url"          # sendPhoto aceita a URL da imagem do ML direto
    markup = TelegramHTMLMarkup()

    def _resultado(self, dados):
        return padronizar_resultado(dados, self.slug)

    def _api(self, metodo, token):
        if not token:
            return None
        return f"https://api.telegram.org/bot{token}/{metodo}"

    def enviar_oferta(self, destino, mensagem, *, imagem_url=None, imagem_b64=None,
                      mimetype="image/jpeg", legenda=None, usuario=None, session=None,
                      operation_id=None):
        # session ignorado: o Bot API do Telegram usa `usuario` (token do bot por-usuário).
        destino = str(destino or "").strip()
        mensagem = str(mensagem or "")
        if not destino or not mensagem:
            return self._resultado({"sucesso": False, "erro": "Destino ou mensagem vazia.",
                    "classe": "permanente", "via": "telegram"})
        if not re.fullmatch(r"(?:@[A-Za-z][A-Za-z0-9_]{4,31}|-?\d+)", destino):
            return self._resultado({"sucesso": False,
                    "erro": "Destino do Telegram inválido. Use @canal ou o ID numérico.",
                    "classe": "permanente", "via": "telegram"})
        token = resolver_token(usuario)
        if not token:
            return self._resultado({"sucesso": False,
                    "erro": "Bot do Telegram não conectado. Conecte em Conexão Telegram.",
                    "classe": "permanente", "via": "telegram"})

        # Telegram: legenda de foto tem limite de 1024 chars; texto puro até 4096.
        arquivos = None
        if imagem_b64:
            try:
                conteudo = base64.b64decode(imagem_b64, validate=True)
            except (ValueError, TypeError):
                return self._resultado({"sucesso": False,
                        "erro": "Imagem base64 inválida.", "classe": "permanente",
                        "via": "telegram"})
            url = self._api("sendPhoto", token)
            payload = {"chat_id": destino, "caption": (legenda or mensagem)[:1024],
                       "parse_mode": "HTML"}
            arquivos = {"photo": ("cupom.jpg", conteudo, mimetype or "image/jpeg")}
        elif imagem_url:
            url = self._api("sendPhoto", token)
            payload = {"chat_id": destino, "photo": imagem_url,
                       "caption": (legenda or mensagem)[:1024], "parse_mode": "HTML"}
        else:
            url = self._api("sendMessage", token)
            payload = {"chat_id": destino, "text": mensagem[:4096],
                       "parse_mode": "HTML", "disable_web_page_preview": False}

        try:
            inicio = time.monotonic()
            r = (requests.post(url, data=payload, files=arquivos, timeout=30)
                 if arquivos else requests.post(url, json=payload, timeout=30))
            corpo = r.json()
            if corpo.get("ok"):
                msg = corpo.get("result") if isinstance(corpo.get("result"), dict) else {}
                return self._resultado({"sucesso": True, "via": "telegram",
                        "mensagem_id": str(msg.get("message_id") or ""),
                        "duracao_ms": round((time.monotonic() - inicio) * 1000)})
            codigo = int(corpo.get("error_code") or r.status_code)
            classe = "transitorio" if codigo == 429 or codigo >= 500 else "permanente"
            return self._resultado({"sucesso": False,
                    "erro": corpo.get("description") or f"HTTP {r.status_code}",
                    "status": codigo, "classe": classe, "via": "telegram",
                    "duracao_ms": round((time.monotonic() - inicio) * 1000)})
        except requests.ConnectTimeout as e:
            # Nem chegou a conectar: nada saiu, repetir é seguro.
            return self._resultado({"sucesso": False,
                    "erro": _sem_token(f"Falha ao conectar: {e}", token),
                    "classe": "transitorio", "via": "telegram", "etapa": "http",
                    "duracao_ms": 30000, "falha_infra": True})
        except requests.Timeout as e:
            # Estourou ESPERANDO a resposta: o Bot API pode já ter aceitado e
            # publicado. Marcar transitório fazia `padronizar_resultado` calcular
            # `repetir=True` e a oferta saía duas vezes no canal — o mesmo buraco
            # que o caminho do WhatsApp fechou com ledger e chave de idempotência.
            # Aqui não há ledger para consultar, então o desfecho honesto é
            # "incerto": não conta como enviado e não repete.
            return self._resultado({"sucesso": False, "resultado": "incerto",
                    "repetir": False,
                    "erro": _sem_token(
                        f"O envio foi iniciado, mas a confirmação não chegou a "
                        f"tempo; confira no canal antes de repetir: {e}", token),
                    "classe": "transitorio", "via": "telegram", "etapa": "http",
                    "duracao_ms": 30000, "falha_infra": True,
                    "operacao": str(operation_id or "")})
        except requests.ConnectionError as e:
            return self._resultado({"sucesso": False,
                    "erro": _sem_token(f"Falha de transporte: {e}", token),
                    "classe": "transitorio", "via": "telegram", "etapa": "http",
                    "duracao_ms": 30000, "falha_infra": True})
        except Exception as e:
            return self._resultado({"sucesso": False,
                    "erro": _sem_token(f"Falha de transporte: {e}", token),
                    "classe": "desconhecido", "via": "telegram"})

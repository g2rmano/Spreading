"""Descoberta estrita e cache de produtos aplicaveis a cupons.

Nao existe fallback por categoria: um item so entra quando a fonte fornece uma
relacao verificavel (container/campanha, id/link direto, promocao com o codigo ou
escopo explicitamente site-wide).
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import timedelta, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.scrapers.models import (
    CupomPreparacao, LinkAfiliadoUsuario, Produto, ProdutoCupom,
)
from apps.scrapers.carga import (
    BrowserResourceUnavailable, ml_site_browser_resource,
)
from apps.scrapers.identidade_produto import link_canonico

logger = logging.getLogger(__name__)

CACHE_HORAS = 3
# O preparo volta para a fila a cada 3h, mas uma relação comprovada continua
# exibível enquanto o próprio produto estiver dentro da janela segura do catálogo.
# Separar as duas janelas evita que o teto do lote vire, acidentalmente, um teto de
# cupons visíveis.
EXIBICAO_HORAS = 48
# Espera antes de reprocessar um cupom que não rendeu nenhum produto. Sem isto o
# preparo vazio é reagendado imediatamente e consome o lote inteiro em repetição.
BACKOFF_VAZIO = timedelta(hours=6)
# Espera quando o ML barrou a leitura por falta de sessão. Curta de propósito: o
# cupom NÃO foi julgado, e a sessão volta assim que alguém reconecta. Com o
# BACKOFF_VAZIO de 6h, um catálogo inteiro reprovado por sessão morta continuaria
# fora da tela por horas depois de a conexão voltar.
BACKOFF_SEM_SESSAO = timedelta(minutes=20)
BACKOFF_TRANSPORTE = timedelta(minutes=20)
# Espera quando o único Chromium da máquina estava com outra tarefa. Curtíssima de
# propósito: não houve julgamento nenhum sobre o cupom, só fila.
BACKOFF_CAPACIDADE = timedelta(minutes=3)
MAX_CANDIDATOS = 36
PREPARO_LOTE_POR_CICLO = 12
# Teto da varredura por GET. Dimensionado pela demanda real: ~2.400 cupons ativos
# com janela de preparo de 3h pedem ~800/h, e o ciclo de cupons roda a cada 15 min.
# 400/ciclo ≈ 1600/h de catch-up. Um GET de container custa ~1s e não disputa o
# Chromium. O lote Playwright continua em PREPARO_LOTE_POR_CICLO (12).
PREPARO_LOTE_HTTP_POR_CICLO = 400
# Disjuntor do transporte do container. Cada cupom já tem backoff próprio de 20
# min, mas isso é POR CUPOM: com 400 cupons por ciclo e o host respondendo 403 a
# todos, o backoff individual não impede o lote seguinte de repetir a rodada
# inteira. Medido em 06/09/2026: ~1.600 requisições por hora, todas recusadas,
# afogando o log e gastando a janela de preparo sem produzir um único vínculo.
#
# Depois de N falhas SEGUIDAS de transporte, o passo HTTP para por um tempo e
# devolve os cupons à fila sem tocar na rede. Uma única resposta boa reabre.
CONTAINER_FALHAS_ATE_ABRIR = int(
    os.getenv("CONTAINER_FALHAS_ATE_ABRIR", "8") or "8")
CONTAINER_CIRCUITO_S = int(os.getenv("CONTAINER_CIRCUITO_S", "900") or "900")
_CIRCUITO_KEY = "container-ml:circuito"
_FALHAS_KEY = "container-ml:falhas-seguidas"


def _cache_circuito():
    """Cache persistente: o disjuntor não pode zerar a cada deploy."""
    from django.core.cache import caches

    return caches["persistente"]


def _circuito_aberto() -> bool:
    try:
        return bool(_cache_circuito().get(_CIRCUITO_KEY))
    except Exception:
        return False  # sem cache, o comportamento é o de antes


def _registrar_falha_de_transporte():
    """Conta a falha e abre o disjuntor quando a sequência estoura o teto."""
    try:
        cache = _cache_circuito()
        falhas = int(cache.get(_FALHAS_KEY) or 0) + 1
        cache.set(_FALHAS_KEY, falhas, timeout=CONTAINER_CIRCUITO_S)
        if falhas >= CONTAINER_FALHAS_ATE_ABRIR:
            cache.set(_CIRCUITO_KEY, True, timeout=CONTAINER_CIRCUITO_S)
            cache.delete(_FALHAS_KEY)
            logger.warning(
                "Container ML: %s falhas seguidas de transporte; pausando o passo "
                "HTTP por %ss.", falhas, CONTAINER_CIRCUITO_S,
            )
    except Exception:
        logger.exception("Falha ao contabilizar o disjuntor do container")


def _registrar_sucesso_de_transporte():
    try:
        cache = _cache_circuito()
        cache.delete(_FALHAS_KEY)
        cache.delete(_CIRCUITO_KEY)
    except Exception:
        pass
_CENT = Decimal("0.01")

# Mensagem estável do preparo bloqueado por sessão. É ela que a projeção lê para
# dizer ao usuário "reconecte", em vez de "nenhum produto aplicável" — que era o
# diagnóstico errado e mandava procurar defeito no cupom.
ERRO_SESSAO_ML = "Mercado Livre exigiu sessão para abrir a lista deste cupom."
ERRO_CONTAINER_FETCH_FAILED = "container_fetch_failed"
ERRO_CONTAINER_EMPTY_PROVEN = "container_empty_proven"
# O cupom existe e o código é publicável, mas a fonte nunca disse A QUE PRODUTOS
# ele se aplica. Não é "container vazio" (lista aberta e sem item): é lista
# nenhuma. Separar os dois importa porque o vazio comprovado é veredito sobre o
# cupom e este aqui é ausência de dado sobre o escopo.
ERRO_ESCOPO_INDEFINIDO = "scope_undelimited"
ERRO_MINIMUM_NOT_MET = "minimum_not_met"
# Marcador de FILA, não de defeito: o preparo nem começou porque o Chromium estava
# com outra tarefa. A projeção lê este valor para dizer "aguardando capacidade".
ERRO_CAPACIDADE_BROWSER = "browser_capacity_deferred"


class SessaoMLIndisponivelError(RuntimeError):
    """O ML respondeu com parede de login/verificação ao ler a lista do cupom.

    Não é "cupom sem produto": nada foi observado. Existe para separar as duas
    coisas no preparo, no backoff e na mensagem que chega à tela.
    """


class ContainerFetchFailedError(RuntimeError):
    """A listagem não foi observada por falha transitória de transporte."""


class BrowserNecessarioError(RuntimeError):
    """O container só sai com Chromium, e esta passada é a de HTTP em massa.

    NÃO é veredito: nada foi observado e nada é gravado. Existe para o lote poder
    varrer centenas de cupons por GET (que resolve a maioria dos containers) sem
    que os poucos que exigem navegador consumam o teto do passo caro.
    """


# Sessões que o ML barrou há pouco, por id de usuário (None = a de sistema). Um
# lote de preparo tem centenas de cupons e todos tentariam a MESMA sessão morta:
# sem esta memória curta, cada cupom paga um GET só para receber a mesma parede.
_PAREDES_POR_CREDENCIAL: dict = {}
MEMORIA_PAREDE = timedelta(minutes=10)


def _parede_recente(chave, agora) -> bool:
    quando = _PAREDES_POR_CREDENCIAL.get(chave)
    return bool(quando and agora - quando < MEMORIA_PAREDE)


def _marcar_parede(chave, agora) -> None:
    _PAREDES_POR_CREDENCIAL[chave] = agora


class _MLCardsHTMLParser(HTMLParser):
    """Extrai os cards SSR do container sem subir um navegador."""

    def __init__(self, limite=9):
        super().__init__(convert_charrefs=True)
        self.limite = limite
        self.rows = []
        self.card = None
        self.div_depth = 0
        self.in_title = False
        self.in_previous = False
        self.current_depth = None
        self.capture = None
        self.buffer = []

    @staticmethod
    def _classes(attrs):
        return set(dict(attrs).get("class", "").split())

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = self._classes(attrs)
        if tag == "div":
            if self.card is None and "poly-card" in classes and len(self.rows) < self.limite:
                self.card = {
                    "nome_produto": "", "link_produto": "", "imagem_url": "",
                    "previous_fraction": "", "previous_cents": "",
                    "current_fraction": "", "current_cents": "", "frete_full": False,
                }
                self.div_depth = 1
            elif self.card is not None:
                self.div_depth += 1
            if self.card is not None and "poly-price__current" in classes:
                self.current_depth = self.div_depth
        if self.card is None:
            return
        if tag == "a" and "poly-component__title" in classes:
            self.in_title = True
            self.buffer = []
            self.card["link_produto"] = html.unescape(attrs_dict.get("href", "")).split("#")[0]
        elif tag == "img" and (
            "poly-component__picture" in classes or not self.card["imagem_url"]
        ):
            image = attrs_dict.get("data-src") or attrs_dict.get("src") or ""
            if image and not image.startswith("data:"):
                self.card["imagem_url"] = html.unescape(image).split("?", 1)[0]
        elif tag == "s" and "andes-money-amount--previous" in classes:
            self.in_previous = True
        elif tag == "span" and "andes-money-amount__fraction" in classes:
            mode = "previous" if self.in_previous else (
                "current" if self.current_depth is not None else "")
            if mode:
                self.capture, self.buffer = f"{mode}_fraction", []
        elif tag == "span" and "andes-money-amount__cents" in classes:
            mode = "previous" if self.in_previous else (
                "current" if self.current_depth is not None else "")
            if mode:
                self.capture, self.buffer = f"{mode}_cents", []
        if "full" in (attrs_dict.get("aria-label") or "").casefold():
            self.card["frete_full"] = True

    def handle_data(self, data):
        if self.card is not None and (self.in_title or self.capture):
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if self.card is None:
            return
        if tag == "span" and self.capture:
            self.card[self.capture] = "".join(self.buffer).strip()
            self.capture, self.buffer = None, []
        elif tag == "a" and self.in_title:
            self.card["nome_produto"] = " ".join("".join(self.buffer).split())[:255]
            self.in_title, self.buffer = False, []
        elif tag == "s":
            self.in_previous = False
        if tag == "div":
            if self.current_depth == self.div_depth:
                self.current_depth = None
            if self.div_depth == 1:
                self._finish_card()
                self.card = None
                self.div_depth = 0
            else:
                self.div_depth -= 1

    @staticmethod
    def _price(fraction, cents):
        raw = str(fraction or "").replace(".", "").replace(" ", "")
        try:
            return float(f"{raw}.{str(cents or '0').strip().zfill(2)}")
        except ValueError:
            return 0.0

    def _finish_card(self):
        row = self.card
        current = self._price(row["current_fraction"], row["current_cents"])
        previous = self._price(row["previous_fraction"], row["previous_cents"]) or current
        if not row["nome_produto"] or not row["link_produto"] or not row["imagem_url"]:
            return
        if current <= 0 or previous < current:
            return
        row["preco_original_sem_desconto"] = f"{previous:.2f}"
        row["preco_vitrine_atual"] = f"{current:.2f}"
        row.pop("previous_fraction", None)
        row.pop("previous_cents", None)
        row.pop("current_fraction", None)
        row.pop("current_cents", None)
        self.rows.append(row)


def _produtos_ml_do_html(html_text, limite=9):
    parser = _MLCardsHTMLParser(limite=limite)
    parser.feed(html_text or "")
    return parser.rows


def _decimal(value):
    try:
        if value in (None, ""):
            return None
        return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _url_canonica(url):
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.netloc:
        return ""
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))


def chave_produtos_cupom(cupom) -> str:
    """Fingerprint dos campos que alteram escopo ou calculo do cupom."""
    payload = {
        "marketplace": str(getattr(cupom, "marketplace", "") or "").lower(),
        "codigo": str(getattr(cupom, "codigo", "") or "").strip().upper(),
        "link": _url_canonica(getattr(cupom, "link", "")),
        "regras": getattr(cupom, "regras", {}) or {},
        "evidencia": {
            key: (getattr(cupom, "evidencia", {}) or {}).get(key)
            for key in ("product_ids", "asins", "item_ids", "association")
        },
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def atualizar_chave_cupom(cupom, *, salvar=True) -> str:
    chave = chave_produtos_cupom(cupom)
    if getattr(cupom, "produtos_chave", "") != chave:
        cupom.produtos_chave = chave
        if salvar and getattr(cupom, "pk", None):
            type(cupom).objects.filter(pk=cupom.pk).update(produtos_chave=chave)
    return chave


def _usuario_do_preparo(cupom, usuario):
    # Fontes públicas preparam associação/preço uma vez. Tag e link continuam por
    # usuário. Cupons privados ou organizacionais preservam seu contexto.
    marketplace = str(cupom.marketplace).lower()
    public_amazon_inventory = (
        marketplace == "amazon"
        and getattr(getattr(cupom, "fonte", None), "slug", "")
        in {"amazon-public-coupons", "amazon-public-web"}
    )
    if ((marketplace == "mercadolivre" or public_amazon_inventory)
            and getattr(cupom, "owner_id", None) is None
            and getattr(cupom, "audience_scope", "public")
            in {"public", "organization"}):
        return None
    return usuario or getattr(cupom, "owner", None)


def _site_inteiro(cupom):
    """Este cupom prova TODO produto do catálogo de uma vez?

    É o portão mais caro do sistema: quem passa aqui recebe um `ProdutoCupom`
    "confirmado" para cada item ativo, e a mensagem passa a anunciar o código em
    qualquer oferta. Em 20/08/2026 um único cupom marcado site inteiro pela fonte
    — MELIPROMO, desmentido pela própria fonte na linha seguinte — gerou 32
    vínculos e apareceu em 45 das 73 publicações do dia.

    Por isso a alegação da fonte passa antes por `site_wide_confiavel`.
    """
    from apps.scrapers.coupon_rules import site_wide_confiavel

    regras = getattr(cupom, "regras", {}) or {}
    if regras.get("is_mar_aberto") or regras.get("site_wide") is True:
        return site_wide_confiavel(cupom)
    escopo = str(regras.get("escopo") or "").strip().casefold()
    return escopo in {"site inteiro", "todo o site", "todos os produtos", "loja inteira"}


def _ids_explicitos(cupom):
    evidence = getattr(cupom, "evidencia", {}) or {}
    out = set()
    for key in ("product_ids", "asins", "item_ids"):
        value = evidence.get(key) or []
        if isinstance(value, str):
            value = re.split(r"[,;\s]+", value)
        if isinstance(value, (list, tuple, set)):
            out.update(str(x).strip().upper() for x in value if str(x).strip())
    return out


def _produto_direto(cupom, produto):
    cupom_url = _url_canonica(getattr(cupom, "link", ""))
    produto_url = _url_canonica(getattr(produto, "link_produto", ""))
    if cupom_url and produto_url and cupom_url == produto_url:
        return True
    ids = _ids_explicitos(cupom)
    if not ids:
        return False
    asin = str(getattr(produto, "asin", "") or "").upper()
    if asin and asin in ids:
        return True
    evidencia = getattr(produto, "evidencia", {}) or {}
    pid = str(evidencia.get("product_id") or evidencia.get("item_id") or "").upper()
    return bool(pid and pid in ids)


_MIN_CODIGO_PROVA = 4


def _promocao_confirma_codigo(cupom, produto):
    """Confirma somente o código completo, nunca uma substring promocional."""
    codigo = str(getattr(cupom, "codigo", "") or "").strip()
    if len(codigo) < _MIN_CODIGO_PROVA:
        return False
    padrao = re.compile(
        rf"(?<![0-9A-Za-z]){re.escape(codigo)}(?![0-9A-Za-z])",
        re.IGNORECASE,
    )
    ev = getattr(produto, "evidencia", {}) or {}
    promo = ev.get("promotion") or {}
    textos = [
        ev.get("promotional_text"), ev.get("promotion_text"),
        promo.get("label") if isinstance(promo, dict) else "",
        promo.get("code") if isinstance(promo, dict) else "",
    ]
    return any(padrao.search(str(texto or "")) for texto in textos)


def _base_produtos(cupom, usuario):
    mkt = str(cupom.marketplace or "").lower()
    qs = Produto.objects.filter(marketplace=mkt).exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"]
    ).exclude(imagem_url="").filter(preco_com_cupom__gt=0)
    from apps.scrapers.maintenance import produtos_frescos_q
    qs = qs.filter(produtos_frescos_q())
    if mkt == "amazon":
        qs = qs.filter(Q(owner__isnull=True) | Q(owner=usuario))
    elif mkt == "awin":
        qs = qs.filter(owner=usuario)
    else:
        qs = qs.filter(Q(owner__isnull=True) | Q(owner=usuario))

    confirmados = ProdutoCupom.objects.filter(
        cupom=cupom, status="confirmado", produto__in=qs,
    ).values_list("produto_id", flat=True)
    ids_confirmados = set(confirmados)

    external = str(cupom.external_id or "")
    campanha = external.split(":", 1)[1] if external.startswith("campanha:") else ""
    programa_id = str(getattr(getattr(cupom, "programa", None), "external_id", "") or "")

    candidatos = []
    identidades = set()
    from apps.scrapers.product_identity import identidade_produto

    # Quem JÁ tem vínculo confirmado entra primeiro e por consulta própria. Antes
    # todo candidato saía de `qs.order_by("-ultima_observacao")[:500]` — uma janela
    # de 500 sobre os 6.208 produtos ML ativos em produção —, então a prova gravada
    # por `_coletar_ml_remoto`/`casar_cupons_container` sumia assim que o catálogo
    # recebia 500 itens mais novos, e o preparo concluía "nenhum produto aplicável"
    # sobre um cupom que tinha associação comprovada.
    confirmados_qs = qs.filter(id__in=ids_confirmados).order_by("-ultima_observacao")
    demais_qs = qs.exclude(id__in=ids_confirmados).order_by("-ultima_observacao")

    def _fluxo():
        for produto in confirmados_qs[:MAX_CANDIDATOS]:
            yield produto, True
        for produto in demais_qs[:500]:
            yield produto, False

    for produto, confirmado in _fluxo():
        provado = confirmado
        if not provado and campanha:
            provado = bool(produto.campanha_id == campanha)
        if not provado and _produto_direto(cupom, produto):
            provado = True
        if not provado and _promocao_confirma_codigo(cupom, produto):
            provado = True
        if not provado and _site_inteiro(cupom):
            if mkt == "awin":
                ev = produto.evidencia or {}
                provado = bool(programa_id and str(ev.get("advertiser_id") or "") == programa_id)
            else:
                provado = True
        if provado:
            identidade = identidade_produto(produto)
            if identidade in identidades:
                continue
            identidades.add(identidade)
            candidatos.append(produto)
            if len(candidatos) >= MAX_CANDIDATOS:
                break
    return candidatos


def calcular_precos(cupom, produto):
    """(original, atual, final) ou None; todos Decimal e específicos do cupom.

    ``atual`` é o preço de vitrine, usado para compra mínima e desconto. O preço
    de tabela serve apenas de contexto e nunca aumenta o benefício do cupom.
    """
    from apps.scrapers.coupon_rules import regras_do_cupom

    regras = regras_do_cupom(cupom)
    atual = _decimal(getattr(produto, "preco_com_cupom", None))
    original = _decimal(getattr(produto, "preco_sem_desconto", None))
    if atual is None or atual <= 0:
        return None
    if original is None or original < atual or original > atual * 10:
        original = atual
    minimo = _decimal(regras.get("valor_minimo")) or Decimal("0")
    minimo_nao_atingido = bool(minimo and atual < minimo)

    # O mínimo do Mercado Livre é de carrinho, não de item. O produto continua
    # sendo uma associação válida; só não podemos anunciar o abatimento como se
    # uma unidade isolada já cumprisse a condição.
    if minimo_nao_atingido:
        return original, atual, atual

    ev = getattr(produto, "evidencia", {}) or {}
    final_explicito = _decimal(ev.get("coupon_final_price"))
    if final_explicito is not None:
        final = final_explicito
    else:
        valor = _decimal(regras.get("valor_desconto"))
        tipo = str(regras.get("tipo_desconto") or "").lower()
        if valor is None or valor <= 0:
            return None
        desconto = (atual * valor / Decimal("100")
                    if tipo in ("porcentagem", "percentual") else valor
                    if tipo == "fixo" else None)
        if desconto is None:
            return None
        teto = _decimal(regras.get("desconto_maximo"))
        if teto and desconto > teto:
            desconto = teto
        final = (atual - desconto).quantize(_CENT, rounding=ROUND_HALF_UP)

    if final <= 0 or (final >= atual and not minimo_nao_atingido):
        return None
    if (original - final) / original >= Decimal("0.90"):
        return None
    return original, atual, final


def _ordem_por_valor_do_cupom(relacao):
    """Ordena pelo abatimento causado pelo cupom, não pela promoção da vitrine."""
    atual = relacao.preco_atual or relacao.preco_original or Decimal("0")
    final = relacao.preco_final or Decimal("0")
    if atual <= 0:
        return Decimal("0"), Decimal("0")
    economia = atual - final
    return economia / atual, economia


def _coletar_ml_remoto(cupom, usuario=None, credenciais_alternativas=(),
                       permitir_browser=True):
    """Materializa a listagem oficial do ML quando ela ainda nao esta no banco.

    `usuario` é o contexto do preparo (None em cupom global, que cai na
    organização de sistema — ver scrapers/ml_auth.py). O container do ML é SSR e
    só rende a listagem completa com sessão.

    `permitir_browser=False` corta o fallback de Chromium: a passada em massa do
    lote resolve por GET (que é o que basta para a maioria dos containers) e
    devolve o cupom à fila sem gastar o único navegador da máquina.

    `credenciais_alternativas` são os usuários cuja sessão pode LER a listagem
    quando a do contexto não serve — TODOS eles, em ordem, porque o primeiro
    candidato pode ser justamente o dono da sessão morta. O catálogo público do ML
    é preparado no contexto de sistema (`usuario=None`), então uma única sessão
    expirada — a da organização em ML_SYSTEM_ORGANIZATION_ID — parava a esteira de
    TODAS as organizações, inclusive as que estavam com o ML conectado. A listagem
    lida é a mesma página pública para qualquer conta; o que a sessão decide é
    apenas se o gateway do ML entrega o HTML.
    """
    # A LISTAGEM, não qualquer página do ML. O teste anterior era só de host
    # (`*.mercadolivre.com.br`), e é por isso que este passo fabricava associação:
    # um código raspado da vitrine é gravado com `link=/ofertas/cupons` e a home
    # entra como `link` de todo cupom cuja fonte não publicou container. Ambas
    # respondem 200 com dezenas de `poly-card` — perfume, notebook, whey — e cada
    # card virava um `ProdutoCupom` "confirmado" logo abaixo. Foi assim que um
    # cupom de acessórios automotivos saiu colado num tablet.
    #
    # `listagem_publica_ml` é o mesmo recorte que `ativacao_publicavel` já exige:
    # `lista.mercadolivre.com.br`, que é uma lista de participantes e não uma
    # vitrine da loja inteira. Continua barrando host arbitrário (SSRF), agora
    # também barrando o ML genérico.
    from apps.scrapers.coupon_rules import listagem_publica_ml

    link = listagem_publica_ml(cupom)
    if not link:
        return {"total": 0, "veredito": "escopo_indefinido"}
    from apps.scrapers.auxiliar import iniciar_browser
    from apps.scrapers.ml_auth import (
        avisar_sem_sessao, parede_de_login, storage_state,
    )
    from apps.scrapers.scraper_mercadolivre.scraper import (
        _ml_http_session, listar_itens_por_cupom,
    )

    state = storage_state(usuario)
    if state is None:
        avisar_sem_sessao(f"Coleta do container do cupom {cupom.pk}", usuario)
    candidatos = [usuario]
    for alternativo in credenciais_alternativas or ():
        if alternativo is None:
            continue
        if any(getattr(alternativo, "id", None) == getattr(escolhido, "id", None)
               for escolhido in candidatos):
            continue
        candidatos.append(alternativo)

    payload = {
        "campaignId": (str(cupom.external_id).split(":", 1)[1]
                       if str(cupom.external_id).startswith("campanha:") else f"norm-{cupom.id}"),
        "title": cupom.titulo, "link_produtos": link,
        "desconto": {"tipo": (cupom.regras or {}).get("tipo_desconto"),
                     "valor": (cupom.regras or {}).get("valor_desconto")},
        "valor_minimo": (cupom.regras or {}).get("valor_minimo") or 0,
        "desconto_maximo": (cupom.regras or {}).get("desconto_maximo"),
    }
    # Os containers são SSR: um GET autenticado já contém nome, anúncio, imagem e
    # preços. Isso prepara todos os cupons oficiais em segundos, em vez de manter um
    # Chromium aberto por vários minutos. Browser fica como fallback para challenge.
    resultado = None
    barrado = False
    houve_credencial = False
    houve_resposta_http = False
    houve_falha_transporte = False
    listagem_inexistente = False

    def _tentar_http(credencial):
        """(linhas, barrado, falha_transporte, inexistente)."""
        if _circuito_aberto():
            # Sem tocar na rede: o host recusou a sequência inteira há pouco. O
            # cupom volta para a fila como falha de transporte, que é o que ele é.
            return None, False, True, False
        try:
            response = _ml_http_session(credencial).get(link, timeout=8)
            if parede_de_login(response):
                return None, True, False, False
            if response.status_code in (404, 410):
                # VEREDITO, não falha: a listagem não existe mais (vendedor saiu,
                # container encerrado). Cair no `except` abaixo transformava isso em
                # "falha de transporte" e, com a memória de parede de outro cupom
                # ligada, em "o ML exigiu sessão" — um cupom morto voltando à fila a
                # cada 20 min, para sempre, e ainda pedindo reconexão ao usuário.
                return [], False, False, True
            response.raise_for_status()
            _registrar_sucesso_de_transporte()
            return _produtos_ml_do_html(response.text, limite=9), False, False, False
        except Exception as exc:
            logger.info("Container ML via HTTP falhou para %s: %s", cupom.pk, exc)
            _registrar_falha_de_transporte()
            return None, False, True, False

    # Percorre os candidatos até um passar pelo gateway. Uma sessão de OUTRA
    # organização é recuperação de indisponibilidade, não preferência: a primeira
    # da fila é sempre a do contexto do preparo.
    rows = None
    agora = timezone.now()
    for indice, candidato in enumerate(candidatos):
        chave = getattr(candidato, "id", None)
        if _parede_recente(chave, agora):
            # Já barrou nesta janela. Um lote tem centenas de cupons: repetir o GET
            # para colher a mesma parede custa um round-trip por cupom.
            barrado = True
            continue
        credencial = state if indice == 0 else storage_state(candidato)
        if credencial is None:
            continue
        houve_credencial = True
        rows, parede, falha_transporte, inexistente = _tentar_http(credencial)
        houve_falha_transporte = houve_falha_transporte or falha_transporte
        houve_resposta_http = houve_resposta_http or not (parede or falha_transporte)
        if inexistente:
            # 404/410 é resposta definitiva do ML e vale para qualquer credencial:
            # não há por que tentar as outras nem subir Chromium.
            listagem_inexistente = True
            barrado = False
            break
        if rows:
            barrado = False
            state = credencial
            break
        if not parede and not falha_transporte:
            # Uma resposta HTTP real, ainda que sem cards, supera a parede vista
            # numa credencial anterior. O browser pode agora confirmar o vazio com
            # esta credencial em vez de classificar tudo como sessão expirada.
            barrado = False
            state = credencial
            break
        if parede:
            _marcar_parede(chave, agora)
            barrado = True
    if listagem_inexistente:
        return {"total": 0, "veredito": "vazio_comprovado"}
    if rows:
        resultado = {**payload, "produtos_aplicaveis": rows}
    # Parede de login não é challenge que o Chromium desfaz: ele bate na mesma
    # porta, consome um slot de browser (que é escasso) e devolve o mesmo vazio. O
    # preparo precisa saber que NADA foi observado, para não gravar "sem produto".
    if resultado is None and barrado:
        raise SessaoMLIndisponivelError(ERRO_SESSAO_ML)
    # Nenhuma credencial utilizável significa que nada foi observado. Marcar vazio
    # aqui era a mentira que prendia o cupom por seis horas.
    if resultado is None and not houve_credencial:
        raise SessaoMLIndisponivelError(ERRO_SESSAO_ML)
    # Timeout/5xx em todas as tentativas também não prova catálogo vazio. Devolve
    # um veredito operacional curto e preserva vínculos anteriores.
    if (resultado is None and houve_falha_transporte
            and not houve_resposta_http):
        return {"total": 0, "veredito": "falha_transporte"}
    if resultado is None and not permitir_browser:
        # Passada em massa: o GET não resolveu este container, e abrir Chromium
        # aqui gastaria o recurso mais escasso da máquina no item menos provável.
        # Volta para a fila do passo com navegador, sem gravar veredito nenhum.
        raise BrowserNecessarioError(cupom.pk)
    if resultado is None:
        with ml_site_browser_resource(
            usuario, owner_kind="coupon_products",
        ) as acquired:
            if not acquired:
                raise BrowserResourceUnavailable(
                    "Capacidade de browser ocupada; preparo do cupom será retomado."
                )
            with iniciar_browser(
                storage_state=state, headless=True,
            ) as (page, _context):
                resultado = listar_itens_por_cupom(payload, page, max_paginas=2)
    total = 0
    for row in (resultado or {}).get("produtos_aplicaveis", []):
        # Canonicaliza ANTES do lookup: a mesma chave que sources/persistence.py
        # usa para o mesmo Produto. Sem isto, o cupom e a raspagem de ofertas
        # gravavam o mesmo anúncio com URLs de tracking diferentes e criavam
        # duas linhas — não existe constraint de unicidade que pegasse isso.
        link_produto = link_canonico("mercadolivre", str(row.get("link_produto") or ""))
        imagem = str(row.get("imagem_url") or "")[:1000]
        if not link_produto or not imagem:
            continue
        produto = Produto.objects.filter(
            marketplace="mercadolivre", owner__isnull=True,
            link_produto=link_produto).first()
        defaults = {
            "campanha_id": payload["campaignId"], "origem": "cupom",
            "fonte": "mercadolivre-cupom", "nome": str(row["nome_produto"])[:255],
            "preco_sem_desconto": float(row["preco_original_sem_desconto"]),
            "preco_com_cupom": float(row["preco_vitrine_atual"]),
            "preco_fonte": float(row["preco_vitrine_atual"]),
            "preco_efetivo": float(row["preco_vitrine_atual"]),
            "link_produto": link_produto, "imagem_url": imagem,
            "estado": "ativo", "ultima_verificacao": timezone.now(),
        }
        if produto:
            # Um item já coletado por outra lane mantém sua proveniência. O cupom
            # atualiza preço/disponibilidade, mas não pode reivindicar a identidade
            # e fazer o produto divulgar uma campanha à qual não pertence.
            campos = dict(defaults)
            if produto.origem and produto.origem != "cupom":
                for campo in ("campanha_id", "origem", "fonte"):
                    campos.pop(campo, None)
            for key, value in campos.items():
                setattr(produto, key, value)
            # `ultima_observacao` é auto_now, e auto_now NÃO grava quando o
            # `update_fields` é restrito e não a inclui. Sem esta linha, abrir a
            # página oficial do cupom e reconfirmar preço e disponibilidade não
            # contava como observação: o produto envelhecia como se ninguém o
            # tivesse visto, saía de `produtos_frescos_q` e depois virava `stale`
            # em `expire_stale` — enquanto esta função continuava "atualizando" um
            # item que `_base_produtos` já não enxergava. Era essa a maior parte de
            # "Nenhum produto comprovadamente aplicável": em produção, 13 dos 17
            # produtos provados de um mesmo cupom estavam fora da janela.
            produto.save(update_fields=list(campos) + ["ultima_observacao"])
        else:
            # `create()` puro perdia a corrida: entre o filter acima e esta
            # linha, outra lane pode gravar o mesmo anúncio, e desde a chave
            # natural de Produto isso vira IntegrityError — que subiria até o
            # loop de cupons e pausaria o ciclo inteiro por backoff de banco.
            # A colisão significa "alguém já criou": buscar e atualizar.
            try:
                with transaction.atomic():
                    produto = Produto.objects.create(
                        marketplace="mercadolivre", **defaults)
            except IntegrityError:
                produto = Produto.objects.filter(
                    marketplace="mercadolivre", owner__isnull=True,
                    link_produto=link_produto).first()
                if produto is None:
                    raise
                for key, value in defaults.items():
                    setattr(produto, key, value)
                produto.save()
        ProdutoCupom.objects.update_or_create(
            produto=produto, cupom=cupom,
            defaults={"status": "confirmado", "verificado_em": timezone.now(),
                      "activation_key": str(payload["campaignId"] or "")[:160],
                      "evidencia": {"regra": "pagina_oficial", "url": link}},
        )
        total += 1
    return {
        "total": total,
        "veredito": "itens_provados" if total else "vazio_comprovado",
    }


def preparar_cupom(cupom, usuario=None, *, force=False, permitir_rede=True,
                   credenciais_alternativas=(), permitir_browser=True,
                   source_run_id="", sessao_ml_disponivel=None,
                   log_sessao_indisponivel=True):
    """Prepara e devolve ProdutoCupom confirmados, ou [] sem fallback inseguro.

    `credenciais_alternativas`: usuários cuja sessão do ML pode ler a listagem
    pública quando a do contexto está barrada — ver `_coletar_ml_remoto`.

    `permitir_browser=False` levanta `BrowserNecessarioError` em vez de abrir
    Chromium, SEM gravar veredito: é a passada em massa por HTTP do lote, e o
    cupom que precisa de navegador fica intacto para o passo caro.
    """
    from apps.scrapers.coupon_rules import cupom_publicavel

    contexto = _usuario_do_preparo(cupom, usuario)
    chave = atualizar_chave_cupom(cupom)
    preparo, _ = CupomPreparacao.objects.get_or_create(cupom=cupom, usuario=contexto)
    inicio_monotonic = time.monotonic()

    def _registrar_preparo(*, status, reason_code="", detail="", retry_at=None,
                           verified_at=None, **extra):
        valores = {
            "status": status, "reason_code": reason_code,
            "safe_detail": str(detail or "")[:255],
            "erro": str(detail or reason_code or "")[:500],
            "proxima_tentativa": retry_at,
            "verificado_em": verified_at or timezone.now(),
            "duracao_ms": max(0, int((time.monotonic() - inicio_monotonic) * 1000)),
            "tentativas": preparo.tentativas + 1,
            "source_run_id": str(source_run_id or "")[:80],
        }
        valores.update(extra)
        CupomPreparacao.objects.filter(pk=preparo.pk).update(**valores)
    fresco_desde = timezone.now() - timedelta(hours=CACHE_HORAS)
    if (not force and preparo.status == "pronto" and preparo.produtos_chave == chave
            and preparo.verificado_em and preparo.verificado_em >= fresco_desde):
        cached = list(ProdutoCupom.objects.filter(
            cupom=cupom, status="confirmado", preco_final__isnull=False,
        ).select_related("produto"))
        cached.sort(key=_ordem_por_valor_do_cupom, reverse=True)
        return cached[:9]

    if not cupom_publicavel(cupom, usuario=usuario):
        _registrar_preparo(
            status="vazio", reason_code="scope_unverified",
            detail="Cupom sem código público ou ativação comprovada.",
            retry_at=timezone.now() + BACKOFF_VAZIO, produtos_chave=chave,
        )
        return []

    try:
        candidatos = _base_produtos(cupom, contexto)
        coleta_ml = None
        if (not candidatos and permitir_rede
                and str(cupom.marketplace).lower() == "mercadolivre"):
            # O lote já verificou todas as organizações uma única vez. Sem esta
            # pista, cada um dos até 400 cupons repetia consultas de sessão e
            # tentava a mesma porta fechada. Nada foi observado, portanto o
            # veredito continua sendo retomável e vínculos anteriores são mantidos.
            if sessao_ml_disponivel is False:
                raise SessaoMLIndisponivelError(ERRO_SESSAO_ML)
            # No catálogo compartilhado `contexto` é None (sessão de sistema).
            # `usuario` é só quem tornou este cupom elegível no lote — pode ser
            # justamente o dono da sessão morta, então ele entra na fila junto com
            # as demais credenciais oferecidas pelo chamador, nunca sozinho.
            coleta_ml = _coletar_ml_remoto(
                cupom, usuario=contexto,
                credenciais_alternativas=(
                    [usuario, *(credenciais_alternativas or ())]
                    if contexto is None else ()
                ),
                permitir_browser=permitir_browser,
            )
            if coleta_ml["veredito"] == "falha_transporte":
                raise ContainerFetchFailedError(ERRO_CONTAINER_FETCH_FAILED)
            candidatos = _base_produtos(cupom, contexto)

        validos = []
        vistos = set()
        precos_nao_comprovados = 0
        agora = timezone.now()
        for produto in candidatos:
            identidade = _url_canonica(produto.link_produto) or f"id:{produto.id}"
            if identidade in vistos:
                continue
            precos = calcular_precos(cupom, produto)
            if not precos:
                precos_nao_comprovados += 1
                continue
            original, atual, final = precos
            from apps.scrapers.coupon_rules import regras_do_cupom
            minimo = _decimal(regras_do_cupom(cupom).get("valor_minimo")) or Decimal("0")
            minimo_nao_atingido = bool(minimo and atual < minimo)
            evidencia = {
                "regra": "associacao_comprovada", "produtos_chave": chave,
            }
            if minimo_nao_atingido:
                evidencia.update({
                    "minimum_not_met": True,
                    "valor_minimo": str(minimo),
                })
            relacao, _ = ProdutoCupom.objects.update_or_create(
                produto=produto, cupom=cupom,
                defaults={"status": "confirmado", "verificado_em": agora,
                          "activation_key": (
                              str(cupom.external_id).split(":", 1)[1][:160]
                              if str(cupom.external_id or "").startswith("campanha:")
                              else ""
                          ),
                          "preco_original": original, "preco_atual": atual,
                          "preco_final": final,
                          "evidencia": evidencia},
            )
            validos.append(relacao)
            vistos.add(identidade)
        ids = [r.id for r in validos]
        with transaction.atomic():
            ProdutoCupom.objects.filter(cupom=cupom, status="confirmado").exclude(
                id__in=ids).update(status="expirado")
            if validos:
                empty_reason = ""
                empty_detail = ""
            elif coleta_ml and coleta_ml["veredito"] == "escopo_indefinido":
                empty_reason = ERRO_ESCOPO_INDEFINIDO
                empty_detail = ("A fonte não publicou a lista de produtos deste "
                                "cupom; sem ela não há como provar que o desconto "
                                "vale para um item.")
            elif coleta_ml and coleta_ml["veredito"] == "vazio_comprovado":
                empty_reason = ERRO_CONTAINER_EMPTY_PROVEN
                empty_detail = ERRO_CONTAINER_EMPTY_PROVEN
            elif precos_nao_comprovados:
                empty_reason = "price_claim_unproven"
                empty_detail = "A associação existe, mas o preço anunciado não foi comprovado."
            else:
                empty_reason = "association_missing"
                empty_detail = "Nenhum produto comprovadamente aplicável."
            _registrar_preparo(
                status="pronto" if validos else "vazio", produtos_chave=chave,
                verified_at=agora,
                # Preparo vazio agendava proxima_tentativa=None, o que a prioridade
                # de preparar_lote lê como "retomável" — o cupom voltava TODO ciclo e
                # ocupava os 12 slots para dar vazio de novo. Com 22 cupons isso era
                # irrelevante; com os milhares que a ativação do ML libera, os vazios
                # girariam para sempre e nenhum cupom novo seria preparado.
                retry_at=None if validos else agora + BACKOFF_VAZIO,
                reason_code=empty_reason, detail=empty_detail)
        validos.sort(key=_ordem_por_valor_do_cupom, reverse=True)
        return validos[:9]
    except SessaoMLIndisponivelError:
        # Nenhuma observação foi feita: NÃO toque nos ProdutoCupom já confirmados e
        # não grave "vazio" — este cupom continua tão bom quanto era. Só registra
        # por que ninguém conseguiu olhar, com espera curta.
        if log_sessao_indisponivel:
            logger.warning(
                "Preparo do cupom %s adiado: o Mercado Livre exigiu sessão para "
                "abrir a listagem.", cupom.pk,
            )
        _registrar_preparo(
            status="erro", reason_code="ml_session_required_for_preparation",
            detail=ERRO_SESSAO_ML, produtos_chave=chave,
            retry_at=timezone.now() + BACKOFF_SEM_SESSAO,
        )
        return []
    except ContainerFetchFailedError:
        logger.warning(
            "Preparo do cupom %s adiado por falha de transporte do container.",
            cupom.pk,
        )
        _registrar_preparo(
            status="erro", reason_code=ERRO_CONTAINER_FETCH_FAILED,
            detail=ERRO_CONTAINER_FETCH_FAILED, produtos_chave=chave,
            retry_at=timezone.now() + BACKOFF_TRANSPORTE,
        )
        return []
    except BrowserNecessarioError:
        # Passada de HTTP em massa: nada foi observado e NADA é gravado. O passo
        # com navegador, logo a seguir no mesmo ciclo, decide este cupom.
        raise
    except BrowserResourceUnavailable:
        # FILA, NÃO AVARIA. A máquina tem UM Chromium; quando outra lane está com
        # ele, este preparo nem começou — nada foi observado sobre o cupom. Caindo
        # no `except Exception` abaixo isso virava `status="erro"` com 30 min de
        # castigo e `preparation_failed` na tela: em produção eram 188 cupons
        # penalizados por espera de capacidade, com a tela mandando procurar um
        # defeito que não existe. Mesma leitura que `sources/registry` já dá a
        # `capacity_deferred`: espera curta e o ciclo seguinte retoma.
        logger.info(
            "Preparo do cupom %s adiado: navegador ocupado por outra tarefa.",
            cupom.pk,
        )
        _registrar_preparo(
            status="pendente", reason_code="capacity_deferred",
            detail=ERRO_CAPACIDADE_BROWSER, produtos_chave=chave,
            retry_at=timezone.now() + BACKOFF_CAPACIDADE,
        )
        return []
    except Exception as exc:
        logger.exception("Preparacao do cupom %s falhou", cupom.pk)
        _registrar_preparo(
            status="erro", reason_code="preparation_failed",
            detail="Falha operacional no preparo.", produtos_chave=chave,
            retry_at=timezone.now() + timedelta(minutes=30),
        )
        return []


def relacoes_preparadas_para_envio(cupom, usuario, limite=9):
    """Devolve relações preparadas sem alterar o catálogo compartilhado.

    O envio manual roda no contexto RLS do usuário e, por isso, não pode tentar
    "atualizar" cupom/produto/preparo compartilhados. Este é deliberadamente um
    predicado de leitura: o worker é o único responsável por materializar e
    renovar o preparo.
    """
    contexto = _usuario_do_preparo(cupom, usuario)
    chave = chave_produtos_cupom(cupom)
    fresco_desde = timezone.now() - timedelta(hours=EXIBICAO_HORAS)
    preparo = CupomPreparacao.objects.filter(
        cupom=cupom, usuario=contexto, status="pronto", produtos_chave=chave,
        verificado_em__gte=fresco_desde,
    ).first()
    if not preparo:
        return []
    from apps.scrapers.maintenance import produtos_frescos_q
    relacoes = list(ProdutoCupom.objects.filter(
        produtos_frescos_q(prefix="produto__"),
        cupom=cupom, status="confirmado", preco_original__isnull=False,
        preco_final__isnull=False, produto__imagem_url__gt="",
    ).exclude(produto__estado__in=["indisponivel", "invalido", "expirado", "stale"])
      .select_related("produto"))
    from apps.scrapers.product_identity import deduplicar_por_produto
    relacoes.sort(key=lambda r: (r.produto.ultima_observacao, r.produto_id), reverse=True)
    relacoes = deduplicar_por_produto(relacoes, produto_de=lambda r: r.produto)
    relacoes.sort(key=_ordem_por_valor_do_cupom, reverse=True)
    return relacoes[:limite]


def relacoes_prontas_para_envio(cupom, usuario, limite=9):
    """Relações preparadas que também possuem link afiliado utilizável.

    O veredito por usuário é obrigatório. Um link legado no produto não registra
    qual conta receberá a comissão nem se o destino foi verificado, portanto não
    pode liberar um cupom na listagem estrita.
    """
    relacoes = relacoes_preparadas_para_envio(cupom, usuario, limite=limite)
    if not relacoes or usuario is None:
        return []
    from apps.scrapers.coupon_links import coupon_link_verified_and_fresh
    from apps.scrapers.models import LinkAfiliadoProdutoCupomUsuario
    relation_ids = {
        row.relacao_id for row in LinkAfiliadoProdutoCupomUsuario.objects.filter(
        usuario=usuario, relacao_id__in=[relacao.pk for relacao in relacoes],
        ).exclude(link_afiliado="") if coupon_link_verified_and_fresh(row)
    }
    # Compatibilidade durante a migração: caches antigos por produto só podem
    # provar uma relação ML quando a URL isca carrega a campanha desta relação.
    legacy = {
        row.produto_id: row for row in LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, produto_id__in=[relacao.produto_id for relacao in relacoes],
        ).exclude(link_afiliado="")
    }
    marketplace = str(cupom.marketplace or "").lower()
    for relation in relacoes:
        row = legacy.get(relation.produto_id)
        activation = str(relation.activation_key or "")
        if coupon_link_verified_and_fresh(row) and (
            marketplace != "mercadolivre" or not activation
            or f"coupon_campaign_id={activation}" in str(row.url_isca or "")
        ):
            relation_ids.add(relation.pk)
    return [
        relacao for relacao in relacoes
        if relacao.pk in relation_ids
    ]


def cupom_pronto_para_usuario(cupom, usuario) -> bool:
    return bool(relacoes_prontas_para_envio(cupom, usuario))


def mapa_relacoes_prontas(usuario, cupons, limite=9):
    """(preparados, prontos) para uma LISTA de cupons, em 3 queries fixas.

    A versão por cupom (`relacoes_prontas_para_envio`) custa 3 queries CADA, e a
    tela a chamava para todo cupom ativo: com os 2379 de homologação eram ~7 mil
    queries por carregamento, quase todas descartadas logo depois pelo filtro de
    publicáveis. Aqui o número de queries não depende da quantidade de cupons.

    Devolve:
      preparadas: dict id -> [ProdutoCupom] com preparo pronto, fresco e produtos
                  válidos (o que a tela usa para diagnosticar a fila)
      prontas:    dict id -> [ProdutoCupom] que também têm link afiliado verificado

    `relacoes_prontas_para_envio` continua sendo a fonte do ENVIO: ela é o caminho
    crítico e não vale o risco de as duas divergirem. Um teste de paridade amarra
    as duas.
    """
    cupons = list(cupons)
    if not cupons:
        return {}, {}
    ids = [c.id for c in cupons]
    agora = timezone.now()
    fresco_desde = agora - timedelta(hours=EXIBICAO_HORAS)

    # `chave_produtos_cupom` e `_usuario_do_preparo` são puras (só hash de campos do
    # próprio objeto), então o casamento acontece em Python, sem ida ao banco.
    chaves = {c.id: chave_produtos_cupom(c) for c in cupons}
    contextos = {c.id: _usuario_do_preparo(c, usuario) for c in cupons}
    contexto_id = {cid: (u.id if u is not None else None)
                   for cid, u in contextos.items()}

    preparados = set()
    for prep in CupomPreparacao.objects.filter(
            cupom_id__in=ids, status="pronto", verificado_em__gte=fresco_desde):
        if (prep.produtos_chave == chaves.get(prep.cupom_id)
                and prep.usuario_id == contexto_id.get(prep.cupom_id)):
            preparados.add(prep.cupom_id)
    if not preparados:
        return {}, {}

    por_cupom = defaultdict(list)
    identidades_por_cupom = defaultdict(set)
    from apps.scrapers.maintenance import produtos_frescos_q
    from apps.scrapers.product_identity import identidade_produto
    relacoes = list(ProdutoCupom.objects.filter(
            produtos_frescos_q(prefix="produto__"),
            cupom_id__in=preparados, status="confirmado",
            preco_original__isnull=False, preco_final__isnull=False,
            produto__imagem_url__gt="")
            .exclude(produto__estado__in=["indisponivel", "invalido", "expirado", "stale"])
            .select_related("produto")
            .order_by("-produto__ultima_observacao", "-produto_id"))
    for relacao in relacoes:
        identidade = identidade_produto(relacao.produto)
        if identidade in identidades_por_cupom[relacao.cupom_id]:
            continue
        identidades_por_cupom[relacao.cupom_id].add(identidade)
        por_cupom[relacao.cupom_id].append(relacao)

    # Mesma ordenação de relacoes_preparadas_para_envio: maior desconto primeiro.
    preparadas = {}
    for cupom_id, relacoes in por_cupom.items():
        relacoes.sort(key=_ordem_por_valor_do_cupom, reverse=True)
        preparadas[cupom_id] = relacoes[:limite]

    if usuario is None:
        return preparadas, {}

    from apps.scrapers.coupon_links import coupon_link_verified_and_fresh
    from apps.scrapers.models import LinkAfiliadoProdutoCupomUsuario
    todas_relacoes = {r.pk for rs in preparadas.values() for r in rs}
    com_link = {
        row.relacao_id for row in LinkAfiliadoProdutoCupomUsuario.objects.filter(
            usuario=usuario, relacao_id__in=todas_relacoes,
        ).exclude(link_afiliado="") if coupon_link_verified_and_fresh(row)
    }
    produtos_relacoes = {
        r.produto_id for rs in preparadas.values() for r in rs
    }
    legacy = {
        row.produto_id: row for row in LinkAfiliadoUsuario.objects.filter(
            usuario=usuario, produto_id__in=produtos_relacoes,
        ).exclude(link_afiliado="")
    }
    marketplace_by_coupon = {c.pk: str(c.marketplace or "").lower() for c in cupons}
    for coupon_id, relations in preparadas.items():
        for relation in relations:
            row = legacy.get(relation.produto_id)
            activation = str(relation.activation_key or "")
            if coupon_link_verified_and_fresh(row) and (
                marketplace_by_coupon.get(coupon_id) != "mercadolivre"
                or not activation
                or f"coupon_campaign_id={activation}" in str(row.url_isca or "")
            ):
                com_link.add(relation.pk)

    prontas = {}
    for cupom_id, relacoes in preparadas.items():
        utilizaveis = [r for r in relacoes if r.pk in com_link]
        if utilizaveis:
            prontas[cupom_id] = utilizaveis
    return preparadas, prontas


def ids_cupons_prontos(usuario, cupons):
    return set(mapa_relacoes_prontas(usuario, cupons)[1])


def preparar_lote(
    limite=PREPARO_LOTE_POR_CICLO, *, usuarios=None, detalhado=False,
    permitir_rede=True, limite_http=None,
):
    """Prepara cupons ativos com round-robin por fonte, usuário e organização.

    O catálogo de uma fonte grande (notadamente campanhas/feeds) não pode consumir
    todo o ciclo. Cada bucket avança um item por rodada, mantendo a janela de três
    horas renovada também para cupons privados e integrações menores.

    `limite` é o teto do passo com Chromium; `limite_http` é o da varredura por
    GET, que é barata e resolve a maior parte dos containers. Sem `limite_http`
    explícito os dois passos usam `limite` — quem pede um lote pequeno recebe um
    lote pequeno, e quem conhece a cadência (o pipeline) escolhe o teto maior.
    """
    from django.contrib.auth import get_user_model
    from apps.scrapers.coupon_rules import (
        _FONTES_ML_ATIVACAO, coupon_mode_enabled, cupom_publicavel,
    )
    from apps.scrapers.models import CupomNormalizado

    agora = timezone.now()
    # Pré-filtro barato das origens que PODEM ser publicáveis; `cupom_publicavel`
    # reconfere item a item no laço. Cupons de ativação (Amazon e, quando a flag
    # está ligada, campanhas ML com container público) não têm código digitável,
    # então não podem ser filtrados por `codigo__gt=""`.
    cupons = list(CupomNormalizado.objects.filter(estado="ativo").filter(
        Q(codigo__gt="")
        | Q(fonte__slug__in=(
            "amazon-public-coupons", "amazon-public-web",
        ) + _FONTES_ML_ATIVACAO)
    ).filter(
        Q(validade__isnull=True) | Q(validade__gte=agora)
    ).order_by("-ultima_observacao"))
    fresco_desde = agora - timedelta(hours=CACHE_HORAS)
    preparos = {
        (prep.cupom_id, prep.usuario_id): prep
        for prep in CupomPreparacao.objects.filter(
            cupom_id__in=[cupom.id for cupom in cupons],
        )
    }
    from apps.scrapers.models import ExecucaoIngestao
    latest_run_by_source = {}
    for source_id, run_id in ExecucaoIngestao.objects.filter(
        fonte_id__in={cupom.fonte_id for cupom in cupons},
    ).order_by("fonte_id", "-pk").values_list("fonte_id", "pk"):
        latest_run_by_source.setdefault(source_id, str(run_id))

    from apps.scrapers.coupon_rules import (
        FORCA_EVIDENCIA_ORDEM, codigo_publicavel as _codigo_publicavel,
        escopo_delimitado, forca_evidencia,
    )

    def _peso_evidencia(cupom):
        """Códigos oficiais primeiro; depois container, estruturado e sintético.

        Um `synthetic_candidate` é uma URL que NÓS construímos como hipótese. Sem
        esta ordem, milhares deles disputavam os 12 slots do lote (e o único
        Chromium) em pé de igualdade com campanhas cujo container a fonte publicou —
        gastando a capacidade nos candidatos com menos chance de render produto.
        """
        if _codigo_publicavel(cupom):
            # Ter código digitável não diz a que produtos o desconto se aplica.
            # Um código raspado da vitrine, sem lista nenhuma, encabeçava o lote a
            # cada ciclo e voltava vazio — agora ele vai para o fim da fila, atrás
            # das campanhas cujo escopo a fonte publicou.
            return -1 if escopo_delimitado(cupom) else 3
        if str(cupom.marketplace or "").lower() != "mercadolivre":
            return 0
        return FORCA_EVIDENCIA_ORDEM.get(forca_evidencia(cupom), 3)

    def prioridade(cupom, usuario):
        """Evita que cupons já frescos ocupem sempre o começo do lote."""
        contexto = _usuario_do_preparo(cupom, usuario)
        prep = preparos.get((cupom.id, getattr(contexto, "id", None)))
        if prep is None:
            estado = 0  # nunca preparado
        elif prep.proxima_tentativa and prep.proxima_tentativa > agora:
            estado = 3  # respeita backoff; fica por último e será pulado abaixo
        elif prep.status == "pronto" and prep.verificado_em and prep.verificado_em >= fresco_desde:
            estado = 4  # já está visível na tela
        else:
            estado = 1  # preparação vencida, vazia ou com erro retomável
        validade = cupom.validade or timezone.datetime.max.replace(tzinfo=datetime_timezone.utc)
        return (estado, _peso_evidencia(cupom), 0 if cupom.relampago else 1,
                validade, cupom.id)

    usuarios = list(
        usuarios if usuarios is not None
        else get_user_model().objects.filter(is_active=True)
    )
    from apps.accounts.ml_sessions import has_storage_state
    # Uma consulta por usuário/organização, uma vez por ciclo. O caminho antigo
    # repetia a leitura de credenciais em cada cupom e gerava centenas de warnings
    # idênticos quando nenhuma conta estava conectada.
    ml_session_available = any(has_storage_state(user) for user in usuarios)
    from apps.accounts.models import organization_for_user
    usuarios_por_organizacao = defaultdict(list)
    for candidato in usuarios:
        organization = organization_for_user(candidato)
        if organization is not None:
            usuarios_por_organizacao[str(organization.pk)].append(candidato)
    buckets = defaultdict(list)
    for cupom in cupons:
        if not coupon_mode_enabled(cupom):
            continue
        compartilhado = _usuario_do_preparo(cupom, None) is None
        if compartilhado:
            # O preparo de catálogo público é compartilhado, mas a habilitação de
            # ativação ML é por organização. Basta uma organização elegível para
            # materializar a prova; a projeção por usuário continua decidindo quem
            # pode vê-la/enviá-la.
            candidatos_contexto = (
                usuarios_por_organizacao.get(str(cupom.organization_id), [])
                if getattr(cupom, "audience_scope", "public") == "organization"
                else usuarios
            )
            elegivel = next((
                u for u in candidatos_contexto
                if cupom_publicavel(cupom, usuario=u)
            ), None)
            if not elegivel:
                continue
            contextos = [elegivel]
        else:
            contextos = [cupom.owner] if cupom.owner_id else usuarios
        for usuario in contextos:
            if usuario is None and cupom.owner_id:
                continue
            organization_id = (
                getattr(cupom, "organization_id", None)
                or getattr(usuario, "organization_id", None)
                or getattr(getattr(usuario, "perfil", None), "organization_id", None)
            )
            fonte = getattr(getattr(cupom, "fonte", None), "slug", "") or "sem-fonte"
            buckets[(fonte, getattr(usuario, "id", None), organization_id)].append(
                (cupom, usuario)
            )
    for items in buckets.values():
        items.sort(key=lambda item: prioridade(*item))

    filas = deque(
        deque(items)
        for _key, items in sorted(
            buckets.items(), key=lambda pair: tuple(str(value or "") for value in pair[0])
        )
        if items
    )
    feitos = prontos = adiados = 0
    por_fonte = defaultdict(lambda: {"processados": 0, "prontos": 0})
    # DOIS ORÇAMENTOS, porque os dois passos custam ordens de grandeza diferentes.
    # O container do ML é SSR: um GET autenticado resolve a maioria em ~1s, e é só
    # o resto que precisa de Chromium — recurso único da máquina, disputado com a
    # geração de links e com o login interativo. Com um orçamento só, o teto do
    # passo caro (12) virava o teto de TUDO: 40 cupons por ciclo de 15 min contra
    # ~2.400 cupons ativos que a janela de 3h manda repreparar.
    orcamento_http = max(limite, int(limite_http or 0))
    fila_do_browser = deque()

    def _passar(cupom, usuario, *, permitir_browser):
        nonlocal prontos
        fonte = getattr(getattr(cupom, "fonte", None), "slug", "") or "sem-fonte"
        por_fonte[fonte]["processados"] += 1
        credenciais = (
            usuarios_por_organizacao.get(str(cupom.organization_id), [])
            if getattr(cupom, "audience_scope", "public") == "organization"
            else usuarios
        )
        if preparar_cupom(
            cupom, usuario=usuario, force=True, permitir_rede=permitir_rede,
            # Ler a listagem pública do ML depende de UMA sessão viva, qualquer
            # uma. Oferecer o lote inteiro de usuários evita que a esteira
            # compartilhada pare porque a primeira credencial da fila expirou.
            credenciais_alternativas=credenciais,
            permitir_browser=permitir_browser,
            source_run_id=latest_run_by_source.get(cupom.fonte_id, ""),
            sessao_ml_disponivel=(
                ml_session_available
                if str(cupom.marketplace or "").lower() == "mercadolivre"
                else None
            ),
            log_sessao_indisponivel=False,
        ):
            prontos += 1
            por_fonte[fonte]["prontos"] += 1

    while filas and feitos < orcamento_http:
        fila = filas.popleft()
        cupom, usuario = fila.popleft()
        if fila:
            filas.append(fila)
        contexto = _usuario_do_preparo(cupom, usuario)
        chave = atualizar_chave_cupom(cupom)
        prep = preparos.get((cupom.id, getattr(contexto, "id", None)))
        if prep and prep.produtos_chave == chave and prep.verificado_em:
            if prep.status == "pronto" and prep.verificado_em >= fresco_desde:
                continue
            if prep.proxima_tentativa and prep.proxima_tentativa > agora:
                continue
        feitos += 1
        try:
            _passar(cupom, usuario, permitir_browser=False)
        except BrowserNecessarioError:
            # Nada foi observado nem gravado: só o passo caro decide este.
            fila_do_browser.append((cupom, usuario))

    # Passo caro, com o teto de sempre.
    com_browser = 0
    cedeu_browser = False
    while fila_do_browser and com_browser < limite:
        if com_browser:
            from apps.scrapers.resource_control import interesse_pendente
            if interesse_pendente(
                "django_chromium", exceto="coupon_products",
            ):
                cedeu_browser = True
                break
        cupom, usuario = fila_do_browser.popleft()
        com_browser += 1
        _passar(cupom, usuario, permitir_browser=True)
    adiados = len(fila_do_browser)

    if not ml_session_available and any(
        str(cupom.marketplace or "").lower() == "mercadolivre"
        for cupom in cupons
    ):
        logger.info(
            "Preparo ML: nenhuma sessão utilizável; cupons sem associação local "
            "foram preservados e aguardam reconexão."
        )

    resultado = {
        "processados": feitos,
        "prontos": prontos,
        "adiados_sem_browser": adiados,
        "cedeu_browser": cedeu_browser,
    }
    if detalhado:
        resultado["por_fonte"] = dict(por_fonte)
    return resultado

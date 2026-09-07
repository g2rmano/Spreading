"""Deal — a unidade que o sistema publica: produto do nicho + cupom que se aplica a ele.

Até aqui o envio escolhia ENTRE um produto e um cupom. Os dois eram pontuados por
fórmulas diferentes, e `content_ranking` resolvia o empate por decreto editorial
(cupom ganhava sempre). O efeito no grupo era o oposto do objetivo: saía um código de
loja em vez do produto do nicho cujo preço, COM aquele código, era o melhor do dia.

Pior: o preço que o ranking comparava era `Produto.preco_com_cupom`, que apesar do
nome é a VITRINE (ver o comentário em `models.Produto`). O abatimento do cupom só
entrava na hora de montar a mensagem. Ou seja, a qualidade do negócio era medida sem
o cupom dentro da conta.

Aqui o par vira uma coisa só. O cupom passa a vencer PELO PREÇO FINAL, não por regra:
um deal sem cupom continua competindo e ganha quando o preço sozinho é melhor. Isso
elimina a necessidade de qualquer desempate por tipo.

Nada de matching novo: a escada de prova de `coupon_products` e os portões de
`coupon_rules` continuam sendo a autoridade sobre "este cupom vale para este item".
Este módulo só passou a usá-los ANTES de pontuar, em vez de depois de escolher.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

# De onde vem a autoridade de que o cupom se aplica a ESTE produto.
PROVA_CONFIRMADA = "confirmado"          # ProdutoCupom status=confirmado
PROVA_CHECKOUT = "checkout_validado"     # CupomValidacao aprovada no carrinho
PROVA_SITEWIDE = "sitewide_confiavel"    # site inteiro, sem escopo contestado
PROVA_SEM_CUPOM = "sem_cupom"            # deal de preço puro

PROVAS_VALIDAS = frozenset({
    PROVA_CONFIRMADA, PROVA_CHECKOUT, PROVA_SITEWIDE, PROVA_SEM_CUPOM,
})

# Motivos de rejeição. São os mesmos nomes que o relatório de cobertura usa, porque
# um déficit sem endereço não é diagnóstico — é reclamação.
MOTIVO_TERMO_NEGATIVO = "termo_negativo"
MOTIVO_FORA_DA_FAIXA = "fora_da_faixa_de_preco"
MOTIVO_SEM_CUPOM_APLICAVEL = "sem_cupom_aplicavel"
# Sub-motivos de `sem_cupom_aplicavel`. O nome antigo dizia "este produto não tem
# cupom" para quatro situações diferentes, três delas com par CONFIRMADO no banco.
MOTIVO_SEM_PAR_CONFIRMADO = "sem_par_confirmado"        # nenhum ProdutoCupom confirmado
MOTIVO_AGUARDA_CORROBORACAO = "aguarda_corroboracao"    # par existe, prova única
MOTIVO_REGRA_ILEGIVEL = "regra_do_cupom_ilegivel"       # par existe, regra é lixo
MOTIVO_BENEFICIO_ZERO = "beneficio_zero_neste_item"     # par existe, não abate nada
MOTIVO_BENEFICIO = "beneficio_irrelevante"
MOTIVO_SEM_HISTORICO = "sem_historico_de_preco"
MOTIVO_PRECO_DE_SEMPRE = "preco_de_sempre"
MOTIVO_ABAIXO_DO_MINIMO = "abaixo_do_minimo"
MOTIVO_COOLDOWN = "cooldown"
MOTIVO_PRECO_VELHO = "preco_nao_reobservado"
MOTIVO_MINIMO_DE_COMPRA = "compra_minima_acima_do_preco"

# Um cupom que existe há mais de 30 dias e continua ativo já está embutido na série
# de preços de vitrine que usamos como referência. Creditar o abatimento dele como
# "profundidade" faria todo item da loja parecer mínima histórica para sempre.
DIAS_CUPOM_PERENE = 30


def _beneficio_minimo_percent() -> float:
    """Quanto do preço o cupom precisa abater para o par valer uma mensagem.

    É diferente de `cupom_e_lixo`, que julga o TETO genérico do cupom: aqui o
    julgamento é sobre o que ele vale NESTE item. "R$ 20 OFF" é ótimo num item de
    R$ 120 e irrelevante num de R$ 4.000.
    """
    try:
        return max(0.0, float(getattr(settings, "DEAL_BENEFICIO_MINIMO_PERCENT", 5.0)))
    except (TypeError, ValueError):
        return 5.0


def _beneficio_minimo_reais() -> float:
    try:
        return max(0.0, float(
            getattr(settings, "COUPON_VALOR_MINIMO_RELEVANTE_REAIS", 10) or 10))
    except (TypeError, ValueError):
        return 10.0


@dataclass
class DealCandidate:
    """Produto + cupom aplicável, com o preço que o comprador realmente paga."""

    produto: object
    cupom: object = None
    relacao: object = None          # ProdutoCupom, quando a prova veio de lá
    preco_vitrine: float = 0.0
    beneficio_rs: float = 0.0
    preco_final: float = 0.0
    prova: str = PROVA_SEM_CUPOM
    historico: dict = None
    cupom_perene: bool = False
    desconto_comprovado: bool = False
    score: float = 0.0
    motivos: list = field(default_factory=list)
    componentes: dict = field(default_factory=dict)

    @property
    def tem_cupom(self) -> bool:
        return self.cupom is not None

    @property
    def economia_total(self) -> float:
        """Da lista até o preço final. Só use quando o desconto for comprovado."""
        lista = float(getattr(self.produto, "preco_sem_desconto", 0) or 0)
        return max(0.0, lista - self.preco_final)

    @property
    def beneficio_publicavel(self) -> float:
        """Quanto do cupom pode entrar na CONTA que a mensagem anuncia.

        Só o que foi provado no checkout. Sem essa prova o desconto continua
        valendo para RANQUEAR — um cupom bom é motivo para publicar o item — mas
        não para afirmar um total.

        O caso que obrigou a separação, em 07/09/2026: a vitrine do Mercado Livre
        anunciava R$ 578,55 para um cooktop cujo carrinho cobrava R$ 609 — a
        diferença exata de 5%, ou seja, o preço da vitrine JÁ era pós-cupom, e o
        produto não trazia prova nenhuma disso (`evidencia` tinha só `transport`).
        Em cima desse preço o sistema abateu um segundo cupom, o LIBERAESSA, de
        R$ 46,28, e publicou R$ 532,27. No checkout o cupom estava esgotado e o
        cliente via R$ 609. Dois descontos empilhados, nenhum comprovado.
        """
        return self.beneficio_rs if self.prova == PROVA_CHECKOUT else 0.0

    def coerente(self) -> bool:
        """M2: o preço final é sempre vitrine menos benefício. Sem exceção."""
        return abs(
            (self.preco_vitrine - self.beneficio_publicavel) - self.preco_final
        ) < 0.005


def _termos(valor) -> list:
    return [t.strip() for t in str(valor or "").split(",") if t.strip()]


def _casa_algum_termo(produto, termos) -> bool:
    if not termos:
        return False
    from apps.scrapers.models import normalizar_busca

    alvo = normalizar_busca(" ".join([
        str(getattr(produto, "nome", "") or ""),
        str(getattr(produto, "categoria", "") or ""),
    ]))
    return any(normalizar_busca(termo) in alvo for termo in termos)


def _cupom_perene(cupom, *, agora) -> bool:
    primeira = getattr(cupom, "primeira_observacao", None)
    if not primeira:
        return False
    return (agora - primeira) >= timedelta(days=DIAS_CUPOM_PERENE)


def _catalogo_de_cupons(*, usuario, marketplaces, agora):
    """Cupons ativos, frescos e visíveis, uma vez — não um por produto.

    `_melhor_cupom_normalizado_obj` varre o catálogo inteiro por item e só serve
    Mercado Livre. Com um pool de centenas de produtos isso é O(n x m) e deixa as
    outras lojas sem cupom nenhum.
    """
    from apps.scrapers.coupon_rules import cupons_visiveis_q
    from apps.scrapers.maintenance import cupons_frescos_q
    from apps.scrapers.models import CupomNormalizado

    consulta = CupomNormalizado.objects.select_related(
        "fonte", "programa", "integracao",
    ).filter(estado="ativo").filter(
        cupons_visiveis_q(usuario),
        Q(inicio__isnull=True) | Q(inicio__lte=agora),
        cupons_frescos_q(agora=agora),
    )
    if marketplaces:
        consulta = consulta.filter(marketplace__in=list(marketplaces))
    return list(consulta)


def _cupons_com_prova_de_checkout(cupons) -> set:
    """Cupons cujo abatimento já foi observado num carrinho real."""
    from apps.scrapers.models import CupomValidacao

    ids = [c.pk for c in cupons]
    if not ids:
        return set()
    return set(
        CupomValidacao.objects.filter(cupom_id__in=ids, status="approved")
        .values_list("cupom_id", flat=True)
    )


def _mapa_confirmados(produtos, cupons):
    """produto_id -> [(cupom, relacao)] com status confirmado."""
    from apps.scrapers.models import ProdutoCupom

    ids_produto = [p.pk for p in produtos if getattr(p, "pk", None)]
    ids_cupom = [c.pk for c in cupons]
    if not ids_produto or not ids_cupom:
        return {}
    por_cupom = {c.pk: c for c in cupons}
    mapa = {}
    for relacao in ProdutoCupom.objects.filter(
        produto_id__in=ids_produto, cupom_id__in=ids_cupom, status="confirmado",
    ):
        cupom = por_cupom.get(relacao.cupom_id)
        if cupom is not None:
            mapa.setdefault(relacao.produto_id, []).append((cupom, relacao))
    return mapa


def _cupons_sitewide(cupons, *, usuario, contestados):
    """marketplace -> [cupom] válido para qualquer item daquela loja."""
    from apps.scrapers.coupon_rules import (
        cupom_publicavel, escopo_delimitado, site_wide_confiavel,
    )

    mapa = {}
    for cupom in cupons:
        if not site_wide_confiavel(cupom, codigos_contestados=contestados):
            continue
        # Um "site inteiro" sem escopo delimitado é exatamente a alegação que a
        # migração 0064 teve de expurgar: o código liga a qualquer coisa que
        # estivesse na vitrine naquele minuto.
        if not escopo_delimitado(cupom, codigos_contestados=contestados):
            continue
        if not cupom_publicavel(cupom, usuario=usuario):
            continue
        mapa.setdefault(str(cupom.marketplace or "").casefold(), []).append(cupom)
    return mapa


def _beneficio_do_cupom(cupom, preco_vitrine) -> float:
    from apps.scrapers.coupon_rules import desconto_para_comprador, regras_do_cupom
    from apps.scrapers.ofertas import _desconto_em_reais

    if not desconto_para_comprador(cupom):
        # Taxa de comissão da Shopee gravada como `valor_desconto`. Anunciar isso
        # como abatimento mente para quem compra.
        return 0.0
    regras = regras_do_cupom(cupom)
    return _desconto_em_reais(
        preco_vitrine, regras.get("tipo_desconto"), regras.get("valor_desconto"),
        regras.get("desconto_maximo"),
    )


def _melhor_cupom_para(produto, *, confirmados, sitewide, com_checkout, usuario,
                       corroboracoes, rejeicoes):
    """Melhor (cupom, relação, prova, benefício) para este item, ou None."""
    from apps.scrapers.coupon_rules import (
        aguarda_corroboracao_oficial, codigo_publicavel, cupom_e_lixo,
        regras_do_cupom,
    )

    preco_vitrine = float(getattr(produto, "preco_com_cupom", 0) or 0)
    marketplace = str(getattr(produto, "marketplace", "") or "").casefold()
    # Só cupom PROVADO NESTE PRODUTO entra na conta do preço. "Site inteiro" é
    # alegação sobre a loja, não sobre o item: em 03/09/2026 um cupom sitewide do
    # ML entrou num air fryer, a mensagem anunciou o abatimento dele, e no checkout
    # o código estava ESGOTADO — o comprador viu um preço que ninguém cobrava.
    # Um cupom sem prova pode continuar existindo no catálogo; o que ele não pode é
    # mudar o número que a mensagem afirma.
    candidatos = [
        (c, r, PROVA_CONFIRMADA)
        for c, r in confirmados.get(getattr(produto, "pk", None), [])
    ]
    if not candidatos:
        rejeicoes[MOTIVO_SEM_PAR_CONFIRMADO] += 1
        return None

    melhor = melhor_chave = None
    beneficio_baixo = False
    for cupom, relacao, prova in candidatos:
        # Os três `continue` abaixo eram mudos, e o item caía no
        # MOTIVO_SEM_CUPOM_APLICAVEL lá no fim — que se lê como "este produto não
        # tem cupom". Não é o que aconteceu: ele TEM par confirmado, e o par foi
        # descartado por um motivo com nome. Medido em 04/09/2026: 3.416 pares
        # confirmados com cupom ativo cobrindo 3.177 produtos, e 5 deals com cupom
        # saindo do outro lado. Um déficit sem endereço não é diagnóstico.
        if aguarda_corroboracao_oficial(cupom, corroboracoes=corroboracoes):
            rejeicoes[MOTIVO_AGUARDA_CORROBORACAO] += 1
            continue
        regras = regras_do_cupom(cupom)
        if cupom_e_lixo(regras):
            rejeicoes[MOTIVO_REGRA_ILEGIVEL] += 1
            continue
        minimo = float(regras.get("valor_minimo") or 0)
        if minimo and minimo > preco_vitrine:
            rejeicoes[MOTIVO_MINIMO_DE_COMPRA] += 1
            continue
        beneficio = _beneficio_do_cupom(cupom, preco_vitrine)
        if beneficio <= 0:
            rejeicoes[MOTIVO_BENEFICIO_ZERO] += 1
            continue
        # O portão que faltava: benefício real NESTE item, não o teto do cupom.
        if (beneficio < _beneficio_minimo_reais()
                or (preco_vitrine > 0
                    and beneficio / preco_vitrine * 100.0
                    < _beneficio_minimo_percent())):
            beneficio_baixo = True
            continue
        if cupom.pk in com_checkout:
            prova = PROVA_CHECKOUT
        chave = (
            round(beneficio, 2),
            1 if prova in (PROVA_CONFIRMADA, PROVA_CHECKOUT) else 0,
            1 if codigo_publicavel(cupom) else 0,
        )
        if melhor_chave is None or chave > melhor_chave:
            melhor = (cupom, relacao, prova, beneficio)
            melhor_chave = chave
    if melhor is None:
        rejeicoes[MOTIVO_BENEFICIO if beneficio_baixo
                  else MOTIVO_SEM_CUPOM_APLICAVEL] += 1
    return melhor


def _valor_real(deal, *, historico_90) -> tuple:
    """Quanto o preço de hoje é bom contra o histórico do próprio item. 0..1."""
    from apps.scrapers.ofertas import FOLGA_MINIMA_HISTORICA

    # Cupom perene não credita profundidade: a série de vitrine já conviveu com ele.
    referencia = deal.preco_vitrine if deal.cupom_perene else deal.preco_final
    motivos = []
    if historico_90 and historico_90.get("n") and historico_90.get("mediana"):
        mediana = float(historico_90["mediana"])
        minimo = float(historico_90.get("minimo") or 0)
        profundidade = (
            max(0.0, (mediana - referencia) / mediana) if mediana > 0 else 0.0
        )
        # 40% abaixo da mediana já é o topo da escala; acima disso o número quase
        # sempre denuncia preço de lista inflado, não negócio melhor.
        nota = min(1.0, profundidade / 0.40) * 0.7
        if minimo > 0 and referencia <= minimo * FOLGA_MINIMA_HISTORICA:
            nota += 0.3
            motivos.append("no fundo do histórico de 90 dias")
        elif profundidade > 0:
            motivos.append(f"{profundidade * 100:.0f}% abaixo da mediana observada")
        return min(1.0, nota), motivos
    # Sem histórico a nota se apoia só na economia absoluta, e com teto: não dá
    # para afirmar profundidade sobre uma série que não existe.
    economia = float(getattr(deal.produto, "economia_rs", 0) or 0)
    if deal.preco_vitrine > 0 and economia > 0:
        motivos.append("sem histórico ainda; economia medida pela vitrine")
        return min(0.5, economia / (deal.preco_vitrine * 0.6)), motivos
    return 0.0, motivos


def _afinidade(deal, *, config) -> tuple:
    positivos = _termos(getattr(config, "termo_busca", ""))
    macro = str(getattr(config, "macro_categoria", "") or "").strip()
    nota, motivos = 0.0, []
    if macro:
        if str(getattr(deal.produto, "macro_categoria", "") or "").strip() == macro:
            nota += 0.4
            motivos.append(f"categoria {macro}")
        else:
            nota += 0.1
    else:
        nota += 0.25
    if positivos:
        if _casa_algum_termo(deal.produto, positivos):
            nota += 0.4
            motivos.append("casa o sub-nicho configurado")
    else:
        nota += 0.25
    nota += 0.2  # faixa de preço: quem está fora já foi rejeitado no portão
    return min(1.0, nota), motivos


def _confianca(deal, *, agora) -> tuple:
    from apps.scrapers.coupon_rules import (
        EVIDENCIA_CONTAINER, EVIDENCIA_ESTRUTURADA, EVIDENCIA_SINTETICA,
        codigo_publicavel, forca_evidencia,
    )
    from apps.scrapers.maintenance import freshness_points

    nota, motivos = 0.0, []
    if deal.prova == PROVA_CONFIRMADA:
        nota += 0.4
        motivos.append("cupom comprovado neste produto")
    elif deal.prova == PROVA_CHECKOUT:
        nota += 0.4
        motivos.append("abatimento visto no carrinho")
    elif deal.prova == PROVA_SITEWIDE:
        nota += 0.25
    else:
        nota += 0.3
    if deal.cupom is not None:
        if codigo_publicavel(deal.cupom):
            nota += 0.15
        else:
            nota += {
                EVIDENCIA_CONTAINER: 0.2,
                EVIDENCIA_ESTRUTURADA: 0.12,
                EVIDENCIA_SINTETICA: 0.04,
            }.get(forca_evidencia(deal.cupom), 0.0)
    else:
        nota += 0.15
    nota += {"alta": 0.2, "media": 0.12, "baixa": 0.04}.get(
        str(getattr(deal.produto, "confianca", "media")), 0.04)
    nota += freshness_points(
        getattr(deal.produto, "ultima_observacao", None), agora=agora,
        max_points=1.0,
    ) * 0.2
    return min(1.0, nota), motivos


def _urgencia(deal, *, agora) -> tuple:
    nota, motivos = 0.0, []
    if getattr(deal.produto, "relampago", False) or getattr(
            deal.cupom, "relampago", False):
        nota += 0.5
        motivos.append("oferta relâmpago")
    validade = getattr(deal.cupom, "validade", None)
    if validade:
        restante = validade - agora
        if restante <= timedelta(hours=12):
            nota += 0.35
            motivos.append("cupom termina em menos de 12 h")
        elif restante <= timedelta(hours=48):
            nota += 0.15
    primeira = getattr(deal.cupom, "primeira_observacao", None)
    if primeira and (agora - primeira) <= timedelta(hours=12):
        nota += 0.2
        motivos.append("cupom novo")
    return min(1.0, nota), motivos


def _desempenho(deal, *, performance) -> tuple:
    linha = performance.get(getattr(deal.produto, "pk", None))
    if not linha or not linha.get("posts"):
        return 0.0, []
    posts = float(linha["posts"])
    clicks = float(linha.get("clicks") or 0)
    # Suavização por tamanho de amostra: 1 clique em 1 publicação não pode valer o
    # mesmo que 40 em 40. Mesmo raciocínio do Wilson usado em `content_ranking`.
    peso = posts / (posts + 20.0)
    nota = peso * min(1.0, (clicks / posts) / 2.0)
    motivos = [f"{int(clicks)} clique(s) em envios anteriores"] if clicks else []
    return nota, motivos


PESOS = {
    "valor_real": 0.40,
    "afinidade": 0.25,
    "confianca": 0.20,
    "urgencia": 0.10,
    "desempenho": 0.05,
}


def pontuar(deal, *, config, historico_90, performance, agora):
    """Score 0-100 por soma ponderada de componentes normalizados.

    Sem multiplicadores encadeados. O score antigo multiplicava x1,15 . x1,5 . x1,4
    . x1,6 sobre uma base sem teto: dois itens com a mesma nota final podiam ter
    chegado lá por caminhos incomparáveis, e calibrar um peso mexia em todos.
    """
    componentes, motivos = {}, []
    for nome, funcao in (
        ("valor_real", lambda: _valor_real(deal, historico_90=historico_90)),
        ("afinidade", lambda: _afinidade(deal, config=config)),
        ("confianca", lambda: _confianca(deal, agora=agora)),
        ("urgencia", lambda: _urgencia(deal, agora=agora)),
        ("desempenho", lambda: _desempenho(deal, performance=performance)),
    ):
        nota, razoes = funcao()
        componentes[nome] = round(nota, 4)
        motivos.extend(razoes)
    deal.componentes = componentes
    deal.score = round(
        100.0 * sum(componentes[nome] * peso for nome, peso in PESOS.items()), 2,
    )
    if deal.tem_cupom and deal.beneficio_rs > 0:
        motivos.insert(0, f"cupom abate R$ {deal.beneficio_rs:.2f} neste item")
    deal.motivos = motivos
    return deal


def _item_ml(produto) -> str:
    from apps.scrapers.scraper_mercadolivre.link import _extrair_item_id

    return _extrair_item_id(str(getattr(produto, "link_produto", "") or "")) or ""


def _precos_medidos_agora(produtos) -> dict:
    """Preços lidos AGORA da vitrine, para os produtos de Mercado Livre do pool."""
    # A suíte não sai para a rede: sem esta guarda, a varredura real do Mercado
    # Livre roda em cada teste e filtra todo produto fictício, transformando um
    # teste de regra de negócio em teste de conectividade.
    if getattr(settings, "RUNNING_TESTS", False):
        return {}
    if not any(str(getattr(p, "marketplace", "")).casefold() == "mercadolivre"
               for p in produtos):
        return {}
    try:
        from apps.scrapers.preco_ao_vivo import varrer_ofertas_ml

        return varrer_ofertas_ml() or {}
    except Exception:
        logger.info("varredura de preços indisponível; seleção segue sem ela")
        return {}


def gerar_deals(config, limite=8, *, agora=None, incluir_sem_cupom=True,
                rejeicoes=None):
    """Deals elegíveis para esta regra de envio, do melhor para o pior.

    `rejeicoes` é um contador opcional que o relatório de cobertura usa para dizer
    POR QUE faltou deal — um déficit sem motivo nomeado não serve para agir.
    """
    from collections import defaultdict

    from apps.scrapers.coupon_rules import (
        codigos_com_escopo_contestado, corroboracoes_oficiais_em_lote,
    )
    from apps.scrapers.models import Publicacao
    from apps.scrapers.ofertas import (
        _desconto_comprovado, _passa_no_minimo, pool_de_produtos_elegiveis,
    )
    from apps.scrapers.precos import chave_produto, stats_em_lote

    agora = agora or timezone.now()
    if rejeicoes is None:
        rejeicoes = defaultdict(int)
    usuario = getattr(config, "owner", None)
    macro = str(getattr(config, "macro_categoria", "") or "").strip()

    produtos = pool_de_produtos_elegiveis(
        macros_selecionadas=[macro] if macro else None,
        min_desconto_percent=float(getattr(config, "min_desconto_percent", 15.0) or 0),
        termo=getattr(config, "termo_busca", "") or None,
        marketplace=getattr(config, "marketplace", "") or None,
        usuario=usuario,
    )
    if not produtos:
        return []

    negativos = _termos(getattr(config, "termos_negativos", ""))
    preco_min = getattr(config, "preco_min", None)
    preco_max = getattr(config, "preco_max", None)

    historicos_90 = stats_em_lote(produtos, dias=90)
    historicos_30 = stats_em_lote(produtos, dias=30)
    marketplaces = {str(p.marketplace or "").casefold() for p in produtos}
    cupons = _catalogo_de_cupons(
        usuario=usuario, marketplaces=marketplaces, agora=agora)
    contestados = codigos_com_escopo_contestado(cupons)
    corroboracoes = corroboracoes_oficiais_em_lote(cupons)
    confirmados = _mapa_confirmados(produtos, cupons)
    sitewide = _cupons_sitewide(cupons, usuario=usuario, contestados=contestados)
    com_checkout = _cupons_com_prova_de_checkout(cupons)

    recentes, performance = {}, {}
    destino = getattr(config, "grupo_id", "")
    if usuario and destino:
        desde = agora - timedelta(
            hours=int(getattr(config, "horas_cooldown", 24) or 24))
        for pub in Publicacao.objects.filter(
            usuario=usuario, destino_id=destino, produto__isnull=False,
        ).filter(
            Q(status="enviado", enviada_em__gte=desde)
            | Q(status="incerto", criada_em__gte=desde)
        ).order_by("produto_id", "-criada_em"):
            recentes.setdefault(pub.produto_id, pub.preco_final)
    if usuario:
        for linha in Publicacao.objects.filter(
            usuario=usuario, status="enviado",
        ).values("produto_id").annotate(
            posts=Count("id", distinct=True), clicks=Count("cliques"),
        ):
            performance[linha["produto_id"]] = linha

    minimo_config = float(getattr(config, "min_desconto_percent", 15.0) or 0)
    # Vazio quando a vitrine não respondeu (ou em teste, sem rede): aí nenhum
    # candidato é descartado por isto e o portão do envio continua sendo o juiz.
    medidos = _precos_medidos_agora(produtos)
    deals = []
    for produto in produtos:
        if negativos and _casa_algum_termo(produto, negativos):
            rejeicoes[MOTIVO_TERMO_NEGATIVO] += 1
            continue
        preco_vitrine = float(getattr(produto, "preco_com_cupom", 0) or 0)
        if preco_vitrine <= 0:
            continue
        # A medição do envio alimenta a SELEÇÃO, não só o portão final: se o preço
        # deste item não está na varredura de agora, ele não pode ser afirmado, e
        # não adianta pontuá-lo para descobrir isso só na hora de publicar. Quando
        # há varredura, o preço dela substitui o do catálogo — é ele que a mensagem
        # vai imprimir.
        if medidos:
            medido = medidos.get(_item_ml(produto))
            if not medido:
                rejeicoes[MOTIVO_PRECO_VELHO] += 1
                continue
            preco_vitrine = float(medido[0])
        escolha = _melhor_cupom_para(
            produto, confirmados=confirmados, sitewide=sitewide,
            com_checkout=com_checkout, usuario=usuario,
            corroboracoes=corroboracoes, rejeicoes=rejeicoes,
        )
        if escolha is None and not incluir_sem_cupom:
            continue
        if escolha is None:
            cupom = relacao = None
            prova, beneficio = PROVA_SEM_CUPOM, 0.0
        else:
            cupom, relacao, prova, beneficio = escolha
        # O preço anunciado desconta SÓ o que está provado no checkout. Ver
        # `DealCandidate.beneficio_publicavel`: sem essa prova, subtrair o cupom é
        # afirmar um total que ninguém conferiu — e, quando o preço da vitrine já é
        # pós-cupom, é empilhar dois descontos que não acumulam.
        beneficio_no_preco = beneficio if prova == PROVA_CHECKOUT else 0.0
        preco_final = round(preco_vitrine - beneficio_no_preco, 2)
        if preco_min is not None and preco_final < float(preco_min):
            rejeicoes[MOTIVO_FORA_DA_FAIXA] += 1
            continue
        if preco_max is not None and preco_final > float(preco_max):
            rejeicoes[MOTIVO_FORA_DA_FAIXA] += 1
            continue
        anterior = recentes.get(produto.pk)
        if anterior and preco_final > float(anterior) * 0.95:
            rejeicoes[MOTIVO_COOLDOWN] += 1
            continue
        chave = chave_produto(produto)
        historico_30 = historicos_30.get(chave)
        historico_90 = historicos_90.get(chave)
        if not _passa_no_minimo(produto, preco_final, historico_30, minimo_config):
            rejeicoes[MOTIVO_ABAIXO_DO_MINIMO] += 1
            continue
        # "Preço de sempre" com amostra suficiente é rejeição dura: anunciar isso
        # queima o grupo mais rápido do que qualquer ganho de volume compensa.
        if historico_30 and historico_30.get("n", 0) >= 3 and (
                preco_final >= float(historico_30["mediana"]) * 0.98):
            rejeicoes[MOTIVO_PRECO_DE_SEMPRE] += 1
            continue
        deal = DealCandidate(
            produto=produto, cupom=cupom, relacao=relacao,
            preco_vitrine=round(preco_vitrine, 2), beneficio_rs=round(beneficio, 2),
            preco_final=preco_final, prova=prova, historico=historico_90,
            cupom_perene=bool(cupom is not None and _cupom_perene(cupom, agora=agora)),
        )
        deal.desconto_comprovado = _desconto_comprovado(
            produto, preco_final, historico=historico_90)
        if not historico_90:
            rejeicoes[MOTIVO_SEM_HISTORICO] += 1
        pontuar(deal, config=config, historico_90=historico_90,
                performance=performance, agora=agora)
        deals.append(deal)

    # Cupom primeiro, e não por empate: oferta sem cupom vende muito menos, então
    # ela é o fundo da fila e não disputa posição com um par produto+cupom. O score
    # continua ordenando DENTRO de cada grupo. É decisão de operação, medida no
    # grupo, não estética de ranking.
    deals.sort(key=lambda d: (not d.tem_cupom, -d.score, getattr(d.produto, "pk", 0)))
    return deals[:limite] if limite else deals

"""Macro-categoria a partir do NOME, para quem não tem `categoria` do marketplace.

`scraper_mercadolivre/cateorize.py` deriva a macro do `domain_id` do Mercado Livre
(`MLB-CELLPHONES` -> "Celulares, Telefonia e Wearables"). Funciona para o que veio da
raspagem de catálogo, e não funciona para nada mais: produto criado pelo pipeline de
cupom, a partir de página de container ou campanha, nasce com `categoria` em
`DESCONHECIDO`.

Medido em produção em 04/09/2026: dos 300 candidatos que tinham par cupom+produto
confirmado, cupom ativo e ficha completa, **249 estavam sem macro-categoria**. Toda
`ConfiguracaoEnvio` filtra por macro. Ou seja, 83% do material que sustenta o produto
— produto do nicho com cupom que se aplica a ele — era invisível para qualquer regra
de envio. As três regras da conta enxergavam 2, 10 e 5 candidatos.

O nome, esse eles têm, e é descritivo: "Airtag Para Coleira De Cachorro",
"Anéis De Vedação Para Processador", "Toalha Mesa Tnt 70x70".

**Precisão acima de cobertura, de propósito.** Classificar errado é pior do que não
classificar: manda cápsula de gelatina para a regra de Ferramentas e devolve ao grupo
exatamente a "promoção de merda" que o filtro de nicho existe para evitar. Então:

- palavra inteira, nunca substring — "cama" não casa em "câmara", "tv" não casa em
  "tvs" por acidente de acento;
- empate entre duas macros com a mesma pontuação deixa o produto SEM macro. Dúvida
  não vira palpite;
- só grava quando o campo está vazio. Categoria vinda do marketplace é autoridade
  maior e nunca é sobrescrita aqui.

Os nomes das macros são exatamente os de `cateorize.macro_dict` — é o valor que as
regras já guardam em `ConfiguracaoEnvio.macro_categoria`, e divergir aqui criaria uma
segunda taxonomia que não casa com nada.
"""
from __future__ import annotations

import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


# Palavra -> macro. Só termo que identifica a categoria sozinho, no vocabulário de
# anúncio brasileiro. Termo genérico ("kit", "conjunto", "premium", "original") fica
# de fora: aparece em tudo e só produziria empate ou erro. Marca que atravessa
# categoria também fica de fora — "xiaomi" vende celular, aspirador, patinete e TV,
# então casar por ela é sorteio, não classificação.
#
# `("termo", peso)` quando o peso natural (número de palavras) classifica errado.
# São poucos e cada um veio de um caso real:
#   "Relógio Smartwatch Forestory"      -> relogio(Joias) empatava com smartwatch
#   "Robô Aspirador ... Alexa"          -> aspirador precisa ganhar do resto
#   "Compressor ... Calibrador De Pneu" -> é peça automotiva, não ferramenta
PALAVRAS_POR_MACRO: dict[str, tuple] = {
    "Celulares, Telefonia e Wearables": (
        "celular", "celulares", "smartphone", "smartphones", "iphone", "galaxy",
        "redmi", "motorola", ("smartwatch", 3), ("smartband", 3), "chip",
        "capinha", "capa de celular", "pelicula", "carregador", "powerbank",
        "fone de ouvido", "airpods", "airtag",
    ),
    "Eletrônicos e Informática": (
        "notebook", "laptop", "computador", "desktop", "monitor", "teclado",
        "mouse", "mousepad", "impressora", "roteador", "modem", "pendrive",
        "ssd", "hd externo", "memoria ram", "placa de video", "processador",
        "webcam", "tablet", "estabilizador", "nobreak", "cabo hdmi",
    ),
    "Áudio, Vídeo e Fotografia": (
        "caixa de som", "soundbar", "fone bluetooth", "headset", "headphone",
        "microfone", "camera", "cameras", "gopro", "drone", "projetor",
        "televisao", "smart tv", "lente", "tripe", "ring light",
    ),
    "Eletrodomésticos": (
        "geladeira", "refrigerador", "fogao", "cooktop", "microondas",
        "lava louca", "lava roupas", "lavadora", "secadora", "freezer",
        "airfryer", "air fryer", "fritadeira", "liquidificador", "batedeira",
        ("aspirador", 3),
        "cafeteira", "sanduicheira", "forno eletrico", "purificador", "bebedouro", "ferro de passar",
    ),
    "Climatização e Aquecimento": (
        "ar condicionado", "ventilador", "climatizador", "aquecedor",
        "umidificador", "circulador de ar", "exaustor",
    ),
    "Casa, Móveis e Decoração": (
        "sofa", "poltrona", "cadeira", "mesa", "estante", "armario", "guarda roupa",
        "colchao", "travesseiro", "edredom", "lencol", "cortina", "tapete",
        "toalha", "almofada", "quadro decorativo", "espelho", "luminaria",
        "abajur", "criado mudo", "rack", "painel de tv", "cabideiro",
    ),
    "Cozinha, Mesa e Bar": (
        "panela", "frigideira", "talher", "talheres", "prato", "copo", "taca",
        "caneca", "garrafa termica", "jogo de jantar", "faqueiro", "assadeira",
        "escorredor", "pote hermetico", "marmita", "churrasqueira", "espetinho",
    ),
    "Limpeza e Lavanderia": (
        "detergente", "sabao", "amaciante", "desinfetante", "agua sanitaria",
        "vassoura", "rodo", "balde", "esfregao", "pano de chao", "cesto de roupa",
        "varal", "cabide",
    ),
    "Casa e Construção": (
        "torneira", "chuveiro", "registro", "sifao", "vaso sanitario", "pia",
        "azulejo", "porcelanato", "argamassa", "cimento", "tinta", "verniz",
        "fechadura", "dobradica", "cadeado", "mangueira", "caixa dagua",
    ),
    "Ferramentas e Manutenção": (
        "furadeira", "parafusadeira", "esmerilhadeira", "serra", "martelo",
        "alicate", "chave de fenda", "chave philips", "chave inglesa",
        "jogo de chaves", "trena", "nivel a laser", "lixadeira", "soldador",
        "compressor", "macaco hidraulico", "morsa", "broca", "parafuso",
        "arruela", "porca", "vedacao", "multimetro", "paquimetro",
    ),
    "Materiais Elétricos e Componentes": (
        "lampada", "reator", "disjuntor", "tomada", "interruptor", "fita led",
        "refletor", "extensao eletrica", "filtro de linha", "pilha", "bateria",
        "placa solar", "inversor", "fio eletrico",
    ),
    "Jardim, Piscina e Área Externa": (
        "piscina", "mangueira de jardim", "regador", "vaso de planta", "adubo",
        "substrato", "semente", "cortador de grama", "aparador de cerca",
        "pergolado", "churrasqueira de jardim", "rede de descanso",
    ),
    "Automotivo": (
        ("pneu", 2), ("calibrador de pneu", 4), "roda automotiva", "amortecedor", "pastilha de freio", "oleo motor",
        "bateria automotiva", "farol", "lanterna automotiva", "retrovisor",
        "capa de banco", "tapete automotivo", "som automotivo", "cera automotiva",
        "aditivo", "limpador de para brisa", "capacete", "moto peca",
    ),
    "Pets e Animais": (
        "cachorro", "gato", "pet", "racao", "coleira", "guia para cachorro",
        "arranhador", "aquario", "petisco", "areia higienica", "caixa de transporte",
        "comedouro", "bebedouro pet", "tapete higienico",
    ),
    "Bebês e Maternidade": (
        "bebe", "fralda", "mamadeira", "chupeta", "carrinho de bebe",
        "bebe conforto", "berco", "papinha", "lenco umedecido", "babador",
    ),
    "Beleza e Cuidados Pessoais": (
        "shampoo", "condicionador", "hidratante", "perfume", "batom", "esmalte",
        "maquiagem", "base facial", "protetor solar", "secador de cabelo",
        "chapinha", "prancha de cabelo", "barbeador", "depilador", "creme facial",
        "sabonete", "desodorante", "escova de cabelo",
    ),
    "Saúde, Ortopedia e Equipamentos Médicos": (
        "termometro", "oximetro", "medidor de pressao", "nebulizador", "mascara",
        "atadura", "colar cervical", "joelheira", "tornozeleira", "muleta",
        "cadeira de rodas", "escova de dente", "creme dental", "fio dental",
        "suplemento", "whey", "creatina", "colageno", "vitamina",
    ),
    "Alimentos e Bebidas": (
        "cafe", "cha", "chocolate", "biscoito", "bolacha", "cerveja", "vinho",
        "whisky", "vodka", "refrigerante", "suco", "azeite", "arroz", "feijao",
        "macarrao", "farinha", "acucar", "leite", "achocolatado", "castanha",
        "amendoim", "macadamia", "noz", "mel", "tempero", "gelatina",
    ),
    "Moda, Calçados e Acessórios": (
        "camiseta", "camisa", "calca", "bermuda", "short", "vestido", "saia",
        "jaqueta", "moletom", "blusa", "tenis", "sapato", "sandalia", "chinelo",
        "bota", "meia", "cueca", "calcinha", "sutia", "oculos de sol", "cinto",
        "bone", "pijama",
    ),
    "Bolsas, Malas e Viagem": (
        "mochila", "bolsa", "mala de viagem", "carteira", "necessaire",
        "pochete", "mala de bordo",
    ),
    "Joias, Relógios e Bijuterias": (
        "relogio", "colar", "pulseira", "brinco", "anel", "corrente de prata",
        "bijuteria", "piercing",
    ),
    "Esportes e Fitness": (
        "halter", "haltere", "anilha", "barra de supino", "esteira ergometrica",
        "bicicleta ergometrica", "corda de pular", "colchonete", "tapete de yoga",
        "bola de futebol", "chuteira", "luva de boxe", "skate", "patins",
        "prancha de equilibrio", "elastico de exercicio",
    ),
    "Camping, Pesca e Outdoor": (
        "barraca", "saco de dormir", "lanterna de cabeca", "canivete",
        "vara de pesca", "molinete", "anzol", "isca", "cantil", "cadeira de camping",
    ),
    "Games, Brinquedos e Hobbies": (
        "playstation", "xbox", "nintendo", "controle de video game", "joystick",
        "boneca", "boneco", "lego", "quebra cabeca", "jogo de tabuleiro",
        "carrinho de brinquedo", "pelucia", "cubo magico", "video game",
    ),
    "Papelaria, Escritório e Escola": (
        "caderno", "caneta", "lapis", "borracha escolar", "mochila escolar",
        "estojo", "agenda", "papel sulfite", "grampeador", "pasta arquivo",
        "marca texto", "cartucho", "toner", "fita adesiva",
    ),
    "Música e Instrumentos": (
        "violao", "guitarra", "baixo eletrico", "teclado musical", "bateria musical",
        "pedaleira", "ukulele", "cavaquinho", "amplificador", "palheta",
    ),
    "Arte, Artesanato e Costura": (
        "linha de costura", "agulha", "maquina de costura", "tecido", "feltro",
        "tinta acrilica", "pincel", "tela para pintura", "cola quente", "biscuit",
    ),
    "Festas, Eventos e Presentes": (
        "balao", "bexiga", "confete", "vela de aniversario", "topo de bolo",
        "lembrancinha", "painel de festa", "descartavel para festa",
    ),
    "Embalagens e Descartáveis": (
        "saco plastico", "embalagem", "caixa de papelao", "plastico bolha",
        "copo descartavel", "pote descartavel", "sacola",
    ),
}

# Índice invertido, com o número de palavras do termo — termo mais específico
# ("fone de ouvido") pesa mais do que termo de uma palavra ("fone").
_INDICE: list[tuple[re.Pattern, str, int]] = []


def _dobrar(texto: str) -> str:
    """Minúsculo, sem acento, espaço único. `Cápsulas` e `capsulas` casam igual."""
    normalizado = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in normalizado if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acento).casefold().strip()


def _construir_indice() -> None:
    if _INDICE:
        return
    for macro, palavras in PALAVRAS_POR_MACRO.items():
        for entrada in palavras:
            palavra, peso_fixo = entrada if isinstance(entrada, tuple) else (entrada, None)
            termo = _dobrar(palavra)
            # `\b` nas duas pontas: palavra inteira. Sem isso "cama" casaria dentro
            # de "camarao" e "tv" dentro de "tvs" — erro silencioso e caro, porque
            # manda o item para o nicho errado em vez de deixá-lo de fora.
            padrao = re.compile(r"\b" + r"\s+".join(
                re.escape(p) for p in termo.split(" ")) + r"\b")
            _INDICE.append((padrao, macro, peso_fixo or len(termo.split(" "))))


def macro_do_nome(nome: str) -> str:
    """Macro-categoria do nome, ou "" quando não dá para afirmar.

    Pontua por especificidade: um termo de duas palavras vale mais que um de uma,
    porque "fone de ouvido" identifica melhor que "fone". Empate no topo devolve
    vazio — duas categorias igualmente plausíveis é ausência de resposta, não
    escolha entre elas.
    """
    _construir_indice()
    alvo = _dobrar(nome)
    if not alvo:
        return ""
    pontos: dict[str, int] = {}
    for padrao, macro, peso in _INDICE:
        if padrao.search(alvo):
            pontos[macro] = pontos.get(macro, 0) + peso
    if not pontos:
        return ""
    ordenado = sorted(pontos.items(), key=lambda kv: -kv[1])
    if len(ordenado) > 1 and ordenado[0][1] == ordenado[1][1]:
        return ""
    return ordenado[0][0]


def popular_macro_por_nome(*, limite=None, apenas_com_cupom=False) -> int:
    """Preenche `macro_categoria` vazia a partir do nome. Idempotente.

    Só toca em linha com o campo vazio: `categoria` vinda do marketplace é
    autoridade maior e `cateorize.popular_macro_categorias` continua sendo quem
    manda onde ela existe.

    `apenas_com_cupom` restringe ao que realmente muda o funil hoje — produto com
    par confirmado e cupom ativo —, para o ciclo de 15 minutos não varrer o
    catálogo inteiro atrás de linha que ninguém vai publicar.
    """
    from django.db.models import Q

    from apps.scrapers.models import Produto

    qs = Produto.objects.filter(
        Q(macro_categoria__isnull=True) | Q(macro_categoria="")
    ).exclude(nome="")
    if apenas_com_cupom:
        qs = qs.filter(
            cupons_normalizados__status="confirmado",
            cupons_normalizados__cupom__estado="ativo",
        ).distinct()
    if limite:
        qs = qs.order_by("-ultima_observacao", "-id")[:limite]

    lote, atualizados = [], 0
    for produto in qs.iterator(chunk_size=500) if not limite else list(qs):
        macro = macro_do_nome(produto.nome)
        if not macro:
            continue
        produto.macro_categoria = macro
        lote.append(produto)
        if len(lote) >= 500:
            Produto.objects.bulk_update(lote, ["macro_categoria"])
            atualizados += len(lote)
            lote = []
    if lote:
        Produto.objects.bulk_update(lote, ["macro_categoria"])
        atualizados += len(lote)
    if atualizados:
        logger.info("Macro por nome: %s produto(s) classificado(s)", atualizados)
    return atualizados

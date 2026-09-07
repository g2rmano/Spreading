"""
Scraper das OFERTAS do Mercado Livre (https://www.mercadolivre.com.br/ofertas).

Diferente dos cupons: aqui o desconto JÁ está no preço (de/por), visível na PDP,
sem necessidade de resgate. Toda oferta raspada tem desconto real.

Cada oferta vira um Produto com origem='oferta'. preco_sem_desconto = "de",
preco_com_cupom = "por" (mantemos o nome do campo p/ reaproveitar a seleção/envio).
"""
import os
import re
import logging

from django.db import OperationalError, connections

from apps.scrapers.auxiliar import iniciar_browser, pausa_humana
from apps.scrapers.carga import coordinated_ml_browser
from apps.scrapers.ml_auth import storage_state
from apps.scrapers.models import Produto
from apps.scrapers.progresso import emitir_progresso
from apps.scrapers.resource_control import interesse_pendente

caminho_atual = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger(__name__)


def _reconectar_db():
    """Descarta conexões possivelmente mortas; o Django reabre na próxima query.

    A raspagem coleta primeiro (minutos na fase de browser) e só depois salva. Nesse
    intervalo a conexão aberta no início do ciclo fica ociosa, e o Postgres/proxy da
    Fly derruba o socket sem o Django saber. Sem isto, a 1ª query do save reusa o
    socket morto e estoura OperationalError("server closed the connection
    unexpectedly"). Chamado no começo de cada fase de save.
    """
    connections.close_all()


def _upsert_resiliente(**kwargs):
    """Upsert tolerante a socket morto e observações históricas duplicadas.

    O ranking aceita múltiplas observações do mesmo anúncio (inclusive variações
    de URL). Uma normalização pode fazer duas delas convergirem para a mesma chave;
    nesse caso, atualizar a mais recente não pode derrubar a coleta inteira.
    """
    def salvar():
        try:
            return Produto.objects.update_or_create(**kwargs)
        except Produto.MultipleObjectsReturned:
            lookup = {
                chave: valor for chave, valor in kwargs.items()
                if chave not in {"defaults", "create_defaults"}
            }
            candidatos = list(Produto.objects.filter(**lookup).order_by(
                "-ultima_observacao", "-pk"
            ))
            total = len(candidatos)
            produto = candidatos[0] if candidatos else None
            if produto is None:  # corrida: as duplicatas sumiram entre GET e SELECT
                return Produto.objects.update_or_create(**kwargs)
            for campo, valor in (kwargs.get("defaults") or {}).items():
                setattr(produto, campo, valor() if callable(valor) else valor)
            produto.save()
            # O histórico de preço NÃO se perde aqui: `PrecoHistorico` é chaveado por
            # (marketplace, chave-normalizada), não pelo id do Produto — justamente
            # porque o Produto é recriado a cada raspagem. Ou seja, duplicata suja o
            # log e desperdiça trabalho, mas não parte a série que alimenta o portão
            # de desconto falso. Registrado aqui porque a leitura oposta é intuitiva
            # e levaria alguém a "consolidar" um FK que não existe.
            logger.warning(
                "Produto duplicado no upsert; observação mais recente atualizada "
                "(id=%s, candidatos=%s).",
                produto.pk, total,
            )
            return produto, False

    try:
        return salvar()
    except OperationalError:
        logger.warning("Conexão do banco caiu no save; reconectando e tentando de novo.")
        _reconectar_db()
        return salvar()


def _normalizar_link_produto(link: str) -> str:
    """Reduz URLs de tracking do ML a um destino persistível e estável.

    Alguns cards de busca entregam URLs ``click1/mclics`` com mais de 1.000
    caracteres. Além de criarem duplicatas, elas estouram o ``URLField`` de
    ``Produto`` só no fim de uma busca longa. O Link Builder já conhece todas as
    formas de extrair o item MLB; reutilizamos a mesma regra e mantemos um fallback
    sem query/fragmento para páginas de catálogo não afiliáveis.

    Exceção que existe por medição, não por teoria: quando a URL é de tracking
    (``click1``/``mclics``) e o item MLB **não** pode ser extraído, a identidade do
    produto vive inteira dentro do parâmetro opaco ``?a=``. Tirar a query ali não
    normaliza: apaga a identidade e faz produtos DIFERENTES colidirem na mesma
    chave. Medido em produção em 02/09/2026: 2.522 anúncios distintos colapsavam
    em ``.../mclics/clicks/external/MLB/count``. Nesse caso preservamos a URL
    inteira — chave feia, mas 1:1 com o anúncio.
    """
    from urllib.parse import urlsplit, urlunsplit
    from apps.scrapers.scraper_mercadolivre.link import _montar_url_isca

    bruto = str(link or "").strip()
    canonico = _montar_url_isca(bruto, "")
    if canonico:
        return canonico[:1000]
    eh_tracking = "click1.mercadolivre" in bruto or "/mclics/" in bruto
    if eh_tracking:
        return bruto[:1000]
    try:
        parsed = urlsplit(bruto)
        limpo = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        limpo = bruto.split("#", 1)[0].split("?", 1)[0]
    return limpo[:1000]

# Classificação de OFERTAS por palavra-chave no nome (PT), mapeando para os mesmos
# nomes de macro de cateorize.py. Ordem importa: mais específico primeiro.
_PT_MACRO = [
    ("Celulares, Telefonia e Wearables", ["celular", "smartphone", "iphone", "galaxy", "moto g", "xiaomi", "redmi", "smartwatch", "smart watch", "fone bluetooth", "chip ", "capa de celular", "capinha", "pelicula"]),
    ("Eletrônicos e Informática", ["notebook", "laptop", "computador", "pc gamer", "monitor", "teclado", "mouse", "ssd", "hd ", "pen drive", "pendrive", "placa de video", "placa mae", "processador", "memoria ram", "roteador", "impressora", "tablet", "webcam", "cooler", "gabinete"]),
    ("Áudio, Vídeo e Fotografia", ["smart tv", " tv ", "televisor", "caixa de som", "soundbar", "fone de ouvido", "headset", "headphone", "microfone", "camera", "câmera", "drone", "projetor", "echo dot", "alexa"]),
    ("Eletrodomésticos", ["geladeira", "refrigerador", "fogao", "fogão", "microondas", "micro-ondas", "lava roupas", "lava louças", "lava loucas", "lava-louças", "lavadora", "secadora", "cooktop", "coifa", "depurador", "air fryer", "fritadeira", "liquidificador", "batedeira", "cafeteira", "aspirador", "ventilador", "ar condicionado", "climatizador", "purificador", "forno eletrico"]),
    ("Cozinha, Mesa e Bar", ["panela", "frigideira", "talher", "faqueiro", "copo", "taca", "taça", "jogo de pratos", "garrafa termica", "potes", "assadeira", "cafeteira italiana", "tabua de corte", "escorredor"]),
    ("Casa, Móveis e Decoração", ["sofa", "sofá", "cama", "colchao", "colchão", "guarda roupa", "guarda-roupa", "mesa", "cadeira", "estante", "armario", "armário", "cortina", "tapete", "luminaria", "luminária", "rack", "criado mudo", "escrivaninha", "lencol", "lençol", "edredom", "travesseiro", "toalha", "colcha", "cobre leito", "cobre-leito", "lixeira", "organizador", "cabide", "varal"]),
    ("Beleza e Cuidados Pessoais", ["perfume", "eau de parfum", "eau de toilette", "eau de cologne", "edp ", "edt ", "deo parfum", "deo colonia", "colonia ", "body splash", "desodorante", "maquiagem", "blush", "batom", "shampoo", "condicionador", "creme facial", "hidratante", "secador de cabelo", "chapinha", "barbeador", "depilador", "esmalte", "protetor solar", "skincare"]),
    ("Moda, Calçados e Acessórios", ["tenis", "tênis", "sapato", "sandalia", "sandália", "chinelo", "camiseta", "camisa", "calca", "calça", "vestido", "blusa", "jaqueta", "bermuda", "short", "bone", "boné", "oculos", "óculos", "relogio", "relógio", "bolsa", "mochila", "carteira", "cinto", "meia"]),
    ("Esportes e Fitness", ["bicicleta", "halter", "anilha", "esteira", "academia", "musculacao", "musculação", "whey", "creatina", "suplemento", "barra fixa", "corda de pular", "bola ", "patins", "skate", "caneleira"]),
    ("Games, Brinquedos e Hobbies", ["playstation", "ps5", "ps4", "xbox", "nintendo", "controle ", "joystick", "jogo ", "brinquedo", "boneca", "lego", "quebra cabeca", "carrinho de brinquedo", "pelucia", "pelúcia"]),
    ("Ferramentas e Manutenção", ["furadeira", "parafusadeira", "serra ", "chave de fenda", "kit ferramentas", "esmerilhadeira", "lixadeira", "soldador", "trena", "alicate", "martelo", "compressor", "plaina", "tomada", "extensao 5", "interruptor", "cabo pp", "disjuntor", "fita isolante"]),
    ("Automotivo", ["pneu", "oleo motor", "óleo motor", "bateria automotiva", "farol", "retrovisor", "limpador para-brisa", "som automotivo", "capa banco", "tapete carro", "terminal direcao", "terminal direção", "amortecedor", "pastilha de freio", "moto ", "capacete"]),
    ("Pets e Animais", ["racao", "ração", "petisco", "coleira", "aquario", "aquário", "arranhador", "casinha cachorro", "comedouro", "areia gato"]),
    ("Bebês e Maternidade", ["fralda", "carrinho de bebe", "carrinho de bebê", "bercco", "berço", "mamadeira", "chupeta", "cadeira para auto", "body bebe"]),
    ("Alimentos e Bebidas", ["cafe ", "café ", "chocolate", "whisky", "vinho", "cerveja", "azeite", "biscoito", "achocolatado", "leite ", "energetico", "energético"]),
    ("Saúde, Ortopedia e Equipamentos Médicos", ["termometro", "termômetro", "oximetro", "medidor de pressao", "massageador", "cadeira de rodas", "fralda geriatrica", "vitamina", "colageno", "colágeno", "capsulas", "cápsulas", "caps)", "colagen"]),
    ("Papelaria, Escritório e Escola", ["caderno", "caneta", "mochila escolar", "estojo", "lapis", "lápis", "papel sulfite", "agenda"]),
]


def _sem_acento(texto: str) -> str:
    """Minúsculas e sem acento, dos DOIS lados da comparação.

    A lista mistura termos com e sem acento ("garrafa termica" convivendo com
    "câmera"), e a comparação era literal — então "Garrafa Térmica Stanley", que é
    exatamente o que o Mercado Livre escreve, não casava com "garrafa termica" e
    o produto ficava sem macro. Sem macro ele não chega a nenhum grupo de nicho:
    some do funil sem erro nenhum. Normalizar os dois lados resolve a família
    inteira de uma vez, em vez de duplicar cada palavra na lista.
    """
    import unicodedata

    decomposto = unicodedata.normalize("NFKD", str(texto or "").lower())
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def classificar_oferta_por_nome(nome: str):
    """Mapeia o nome (PT) da oferta para uma macro-categoria. None se não bater."""
    n = _sem_acento(nome)
    for macro, kws in _PT_MACRO:
        if any(_sem_acento(k) in n for k in kws):
            return macro
    return None


# Classificação de TÍTULOS DE CUPOM. Diferente das ofertas, o título do cupom traz
# o TERMO DE CATEGORIA ('produtos de Anadi Ferramentas', 'cupom em Moda'), não o nome
# do produto — então casa por substantivo de categoria. Ordem: mais específico
# primeiro (termos ambíguos como 'casa'/'mesa' por último). Sem 'mercado' (pega
# 'Mercado Livre'). Cobre as 17 macros; None quando o título não denuncia nada.
_PT_MACRO_CUPOM = [
    ("Bebês e Maternidade", ["bebê", "bebe", "bebês", "bebes", "maternidade", "infantil", "fralda", "gestante"]),
    ("Pets e Animais", ["pet ", "pets", "petshop", "animais", "cachorro", "gato ", "ração", "racao", "aquarismo"]),
    ("Ferramentas e Manutenção", ["ferramenta", "ferramentas", "manutenção", "manutencao", "furadeira", "parafusadeira", "construção", "construcao", "elétrica predial"]),
    ("Automotivo", ["automotivo", "automotiva", "automóvel", "automovel", "acessórios automotivos", "pneu", "pneus", "carro ", "motocicleta", "capacete"]),
    ("Games, Brinquedos e Hobbies", ["game", "games", "gamer", "brinquedo", "brinquedos", "hobbies", "geek", "colecionável", "colecionavel", "playstation", "xbox", "nintendo"]),
    ("Esportes e Fitness", ["esporte", "esportes", "esportivo", "fitness", "academia", "musculação", "musculacao", "suplemento", "suplementos", "camping"]),
    ("Beleza e Cuidados Pessoais", ["beleza", "cosmético", "cosmetico", "cosméticos", "cosmeticos", "perfumaria", "perfume", "perfumes", "maquiagem", "skincare", "cabelo", "dermocosmético"]),
    ("Saúde, Ortopedia e Equipamentos Médicos", ["saúde", "saude", "ortopedia", "ortopédico", "ortopedico", "farmácia", "farmacia", "vitamina", "vitaminas", "suplemento alimentar"]),
    ("Moda, Calçados e Acessórios", ["moda", "vestuário", "vestuario", "calçado", "calcado", "calçados", "calcados", "roupa", "roupas", "acessório", "acessorio", "acessórios", "fashion", "tênis", "tenis", "sapato", "bolsa", "óculos", "oculos", "relógio", "relogio"]),
    ("Papelaria, Escritório e Escola", ["papelaria", "escritório", "escritorio", "escolar", "material escolar", "escola", "livro", "livros", "caderno"]),
    ("Alimentos e Bebidas", ["alimento", "alimentos", "bebida", "bebidas", "supermercado", "mercearia", "hortifruti", "café ", "cafe ", "vinho", "vinhos", "cerveja", "adega"]),
    ("Celulares, Telefonia e Wearables", ["celular", "celulares", "smartphone", "smartphones", "telefonia", "wearable", "smartwatch"]),
    ("Eletrodomésticos", ["eletrodoméstico", "eletrodomestico", "eletrodomésticos", "eletrodomesticos", "eletroportátil", "eletroportatil", "eletroportáteis", "linha branca"]),
    ("Eletrônicos e Informática", ["eletrônico", "eletronico", "eletrônicos", "eletronicos", "informática", "informatica", "notebook", "notebooks", "computador", "periférico", "periferico"]),
    ("Áudio, Vídeo e Fotografia", ["áudio", "audio", "fotografia", "câmera", "camera", "câmeras", "soundbar", "home theater", "televisor", "televisão", "televisao"]),
    ("Cozinha, Mesa e Bar", ["cozinha", "utensílio", "utensilio", "utensílios", "utensilios", "mesa e bar", "panelas", "louças", "loucas"]),
    ("Casa, Móveis e Decoração", ["móveis", "moveis", "móvel", "movel", "decoração", "decoracao", "cama, mesa e banho", "cama mesa e banho", "casa e decoração", "casa e construção", "itens para casa", "para o lar", "jardim", "organização", "organizacao"]),
]


def classificar_cupom_por_titulo(titulo: str):
    """Macro-categoria a partir do TÍTULO do cupom (PT). None se nada bater.

    Casa primeiro por termo de categoria (`_PT_MACRO_CUPOM`); se falhar, tenta os
    nomes de produto (`classificar_oferta_por_nome`), pois alguns títulos citam o
    item direto ('cupom em furadeiras').
    """
    t = (titulo or "").lower()
    for macro, kws in _PT_MACRO_CUPOM:
        if any(k in t for k in kws):
            return macro
    return classificar_oferta_por_nome(titulo)


# Catálogo de SUB-NICHOS: macro -> [(rótulo, termos separados por vírgula)].
# O 'value' do option é a própria string de termos (vai pro termo_busca).
SUBNICHOS = {
    "Celulares, Telefonia e Wearables": [
        ("Smartphones", "celular, smartphone, iphone, galaxy, moto g, xiaomi, redmi"),
        ("Smartwatch", "smartwatch, smart watch, relogio inteligente"),
        ("Fones bluetooth", "fone bluetooth, earbuds, tws, airpods"),
        ("Capas e películas", "capinha, capa de celular, pelicula"),
    ],
    "Eletrônicos e Informática": [
        ("Notebooks", "notebook, laptop"),
        ("Monitores", "monitor"),
        ("Teclado/Mouse/Headset", "teclado, mouse, headset"),
        ("Armazenamento (SSD/HD)", "ssd, hd externo, pen drive, pendrive, cartao de memoria"),
        ("Componentes PC", "placa de video, placa mae, processador, memoria ram, fonte atx, gabinete"),
        ("Tablets", "tablet, ipad"),
        ("Roteador/Rede", "roteador, repetidor, mesh"),
    ],
    "Áudio, Vídeo e Fotografia": [
        ("Smart TVs", "smart tv, televisor, tv 4k, tv led"),
        ("Caixas de som/Soundbar", "caixa de som, soundbar, jbl"),
        ("Câmeras", "camera, câmera, gopro, action cam"),
        ("Drones", "drone"),
        ("Projetores", "projetor"),
        ("Alexa/Echo", "echo dot, alexa"),
    ],
    "Eletrodomésticos": [
        ("Robô aspirador", "aspirador robo, robô aspirador, robot vacuum, robo aspirador"),
        ("Aspirador de pó", "aspirador de po, aspirador vertical"),
        ("Air fryer", "air fryer, fritadeira eletrica, fritadeira sem oleo"),
        ("Geladeira", "geladeira, refrigerador, frigobar"),
        ("Fogão/Cooktop", "fogao, cooktop, forno eletrico"),
        ("Micro-ondas", "microondas, micro-ondas"),
        ("Lava-roupas", "lava roupas, lavadora, maquina de lavar"),
        ("Ar-condicionado", "ar condicionado, climatizador"),
        ("Ventilador", "ventilador"),
        ("Liquidificador/Mixer", "liquidificador, batedeira, mixer"),
        ("Cafeteira", "cafeteira, nespresso, dolce gusto"),
    ],
    "Cozinha, Mesa e Bar": [
        ("Panelas", "panela, frigideira, jogo de panelas"),
        ("Garrafa térmica", "garrafa termica, stanley"),
    ],
    "Casa, Móveis e Decoração": [
        ("Colchões", "colchao, colchão"),
        ("Sofá", "sofa, sofá"),
        ("Cama/Guarda-roupa", "cama, guarda roupa, guarda-roupa, beliche"),
        ("Cadeira escritório/gamer", "cadeira de escritorio, cadeira gamer"),
        ("Cama, mesa e banho", "lencol, lençol, edredom, toalha, jogo de cama"),
    ],
    "Beleza e Cuidados Pessoais": [
        ("Perfumes", "perfume"),
        ("Secador/Chapinha", "secador de cabelo, chapinha, prancha"),
        ("Barbeador/Aparador", "barbeador, aparador, maquina de cortar cabelo"),
        ("Maquiagem", "maquiagem, batom, base, paleta"),
    ],
    "Moda, Calçados e Acessórios": [
        ("Tênis", "tenis, tênis"),
        ("Relógios", "relogio, relógio"),
        ("Óculos", "oculos, óculos"),
        ("Mochilas/Bolsas", "mochila, bolsa, carteira"),
    ],
    "Esportes e Fitness": [
        ("Suplementos", "whey, creatina, suplemento"),
        ("Bicicletas", "bicicleta, bike"),
        ("Musculação", "halter, anilha, barra fixa, kettlebell"),
        ("Esteira/Elíptico", "esteira, eliptico"),
    ],
    "Games, Brinquedos e Hobbies": [
        ("Consoles", "playstation, ps5, ps4, xbox, nintendo switch"),
        ("Controles", "controle, joystick, dualsense"),
        ("Lego/Blocos", "lego, blocos de montar"),
    ],
    "Ferramentas e Manutenção": [
        ("Furadeira/Parafusadeira", "furadeira, parafusadeira"),
        ("Kit ferramentas", "kit ferramentas, jogo de ferramentas"),
        ("Serra/Lixadeira", "serra, esmerilhadeira, lixadeira"),
    ],
    "Automotivo": [
        ("Pneus", "pneu"),
        ("Som automotivo", "som automotivo, multimidia, central multimidia"),
        ("Acessórios carro", "tapete carro, capa banco, suporte celular carro"),
        ("Capacete/Moto", "capacete, moto"),
    ],
    "Pets e Animais": [
        ("Ração", "racao, ração"),
        ("Acessórios pet", "coleira, comedouro, arranhador, casinha"),
    ],
    "Bebês e Maternidade": [
        ("Fraldas", "fralda"),
        ("Carrinho de bebê", "carrinho de bebe, carrinho de bebê"),
        ("Cadeirinha auto", "cadeira para auto, bebe conforto"),
    ],
    "Alimentos e Bebidas": [
        ("Café", "cafe, café"),
        ("Bebidas", "whisky, vinho, cerveja, gin, energetico"),
        ("Chocolate", "chocolate, achocolatado"),
    ],
    "Saúde, Ortopedia e Equipamentos Médicos": [
        ("Massageador", "massageador"),
        ("Medidor pressão/Termômetro", "medidor de pressao, oximetro, termometro"),
        ("Vitaminas", "vitamina, colageno, colágeno, omega"),
    ],
    "Papelaria, Escritório e Escola": [
        ("Mochila escolar", "mochila escolar"),
        ("Material escolar", "caderno, caneta, estojo, lapis"),
    ],
}


# Páginas seguidas sem nenhum card antes de considerar o feed encerrado.
#
# Uma página em branco NÃO é o fim do feed: /ofertas devolve ~40 cards por página até
# a 40ª, e quando vem vazia é quase sempre challenge do anti-bot (o IP de datacenter da
# Fly é desafiado — ver auxiliar.iniciar_browser) ou render que não completou. Parar na
# primeira vazia jogava fora todo o resto da varredura: um soluço na página 3 custava
# ~1.480 ofertas. Só encerramos depois desta quantidade de vazias CONSECUTIVAS.
VAZIAS_PARA_PARAR = 3


def _preco_float(texto_frac, texto_cents="0"):
    frac = (texto_frac or "0").replace(".", "").strip()
    cents = (texto_cents or "0").strip() or "0"
    try:
        return float(f"{frac}.{cents.zfill(2)}")
    except ValueError:
        return 0.0


def _coletar_cards(page):
    """Extrai todos os cards com desconto (de/por) da página atual. Lista de dicts.

    Conta POR QUE cada card foi descartado. Os motivos moravam em `continue` mudos e
    num logger.debug — que o LOGGING em INFO apaga em produção. Um seletor renomeado
    pelo ML zerava a coleta e o único sinal era o total, que só cai quando TUDO
    quebra: enquanto uma parte funcionasse, ninguém via nada.
    """
    from apps.scrapers.scraper_mercadolivre.categorias_pagina import (
        id_do_anuncio, mapear_domain_ids,
    )

    out = []
    descartes = {"sem_nome_ou_link": 0, "sem_desconto": 0, "preco_invalido": 0,
                 "erro_no_card": 0}
    # Uma leitura por página serve todos os cards dela. Esta fonte gravava
    # 'DESCONHECIDO' fixo, e como é a maior do catálogo era ela que deixava o filtro
    # de subcategoria da vitrine (que exclui esse valor) sem nada para oferecer.
    domain_ids = mapear_domain_ids(page)
    cards = page.locator(".poly-card")
    total = cards.count()
    for i in range(total):
        card = cards.nth(i)
        try:
            nome = card.locator(".poly-component__title").first.inner_text(timeout=2000).strip()
            link = card.locator("a.poly-component__title, a[href*='/MLB'], a[href*='mercadolivre']").first.get_attribute("href", timeout=2000)
            if not link or not nome:
                descartes["sem_nome_ou_link"] += 1
                continue

            por = _preco_float(
                card.locator(".poly-price__current .andes-money-amount__fraction").first.inner_text(timeout=2000),
                (card.locator(".poly-price__current .andes-money-amount__cents").first.inner_text(timeout=500)
                 if card.locator(".poly-price__current .andes-money-amount__cents").count() else "0"),
            )
            de_loc = card.locator("s.andes-money-amount--previous .andes-money-amount__fraction")
            if de_loc.count() == 0:
                descartes["sem_desconto"] += 1
                continue  # sem desconto visível
            de = _preco_float(
                de_loc.first.inner_text(timeout=2000),
                (card.locator("s.andes-money-amount--previous .andes-money-amount__cents").first.inner_text(timeout=500)
                 if card.locator("s.andes-money-amount--previous .andes-money-amount__cents").count() else "0"),
            )
            if de <= 0 or por <= 0 or por >= de:
                descartes["preco_invalido"] += 1
                continue

            imagem = ""
            try:
                img = card.locator("img").first
                imagem = (img.get_attribute("data-src", timeout=500)
                          or img.get_attribute("src", timeout=500) or "")
                if imagem.startswith("data:"):
                    imagem = img.get_attribute("data-src", timeout=500) or ""
            except Exception:
                pass

            full = False
            try:
                full = card.locator("svg[aria-label*='Full' i], img[alt*='Full' i]").count() > 0
            except Exception:
                pass

            relampago = False
            try:
                relampago = card.get_by_text(re.compile(r"rel[âa]mpago", re.I)).count() > 0
            except Exception:
                pass

            preco_cupom = 0.0
            try:
                rotulos_cupom = card.get_by_text(
                    re.compile(r"com\s+cupom", re.I),
                )
                if rotulos_cupom.count() > 0:
                    from apps.scrapers.scraper_mercadolivre.link_http import (
                        preco_com_cupom_do_texto,
                    )
                    preco_cupom = preco_com_cupom_do_texto(
                        card.inner_text(timeout=500), por,
                    )
            except Exception:
                # O card continua sendo uma oferta válida. A revalidação da PDP
                # tenta novamente antes do envio.
                pass

            link_normalizado = _normalizar_link_produto(link)
            out.append({
                "nome": nome[:255],
                "link_produto": link_normalizado,
                "preco_sem_desconto": de,
                "preco_com_cupom": por,
                "preco_com_cupom_ativado": preco_cupom,
                "imagem_url": imagem[:1000],
                "frete_full": full,
                "relampago": relampago,
                # "" = este anúncio não estava no payload. Quem grava trata vazio
                # como "não descobri", nunca como "sem categoria".
                "categoria": domain_ids.get(id_do_anuncio(link_normalizado), ""),
            })
        except Exception as e:
            descartes["erro_no_card"] += 1
            logger.debug("Erro num card de oferta ML: %s", e)
    _logar_descartes(total, len(out), descartes)
    return out


def _logar_descartes(total, aproveitados, descartes):
    """Resumo por etapa. É o sinal que teria mostrado um seletor quebrado no dia."""
    perdidos = total - aproveitados
    if not total or not perdidos:
        return
    detalhe = ", ".join(f"{n} {motivo.replace('_', ' ')}"
                        for motivo, n in descartes.items() if n)
    # Descartar card sem desconto é o trabalho normal desta função; o que merece
    # atenção é perder card por erro ou por não achar nome/link — aí o seletor mudou.
    quebrados = descartes["erro_no_card"] + descartes["sem_nome_ou_link"]
    nivel = logger.warning if quebrados else logger.info
    nivel("Cards ML: %s lidos, %s aproveitados, %s descartados (%s)",
          total, aproveitados, perdidos, detalhe)


def _taxonomia(coletado, macro_fixa, defaults):
    """Monta `defaults`/`create_defaults` sem deixar a coleta rebaixar a taxonomia.

    A mesma linha é reescrita por várias coletas (feed completo, lane rápida, busca
    por termo). Categoria e macro só entram no caminho de UPDATE quando ESTA coleta
    descobriu algo: o card que não veio no payload chega com "" e não pode apagar o
    `domain_id` que outra passagem já tinha lido, do mesmo jeito que
    `classificar_oferta_por_nome` devolve None e não pode apagar a macro. É a mesma
    regra de `sources/persistence.py`.
    """
    categoria = str(coletado.get("categoria") or "").strip()[:100]
    macro = macro_fixa or classificar_oferta_por_nome(coletado["nome"])
    atualizacao = dict(defaults)
    if categoria:
        atualizacao["categoria"] = categoria
    if macro:
        atualizacao["macro_categoria"] = macro
    return {
        "defaults": atualizacao,
        # Linha nova nasce preenchida mesmo sem sinal nenhum: 'DESCONHECIDO' é o
        # valor que o resto do código já entende como "ninguém classificou ainda".
        "create_defaults": {**atualizacao,
                            "categoria": categoria or "DESCONHECIDO",
                            "macro_categoria": macro},
    }


def _preco_efetivo_oferta(coletado):
    """Preço pago após cupom, somente quando o próprio card o comprovou."""
    preco_cupom = float(coletado.get("preco_com_cupom_ativado") or 0)
    vitrine = float(coletado.get("preco_com_cupom") or 0)
    return preco_cupom if 0 < preco_cupom < vitrine else vitrine


def _evidencia_oferta(coletado):
    evidencia = {"transport": "public-web"}
    preco_cupom = _preco_efetivo_oferta(coletado)
    vitrine = float(coletado.get("preco_com_cupom") or 0)
    if 0 < preco_cupom < vitrine:
        evidencia["promotion"] = {
            "present": True,
            "coupon_confirmed": True,
            "coupon_final_price": preco_cupom,
            "source": "offer-card",
        }
    return evidencia


def _salvar(coletados, origem, codigo_checkout="", macro_fixa=None):
    """Upsert não destrutivo. Uma coleta parcial nunca apaga o catálogo anterior."""
    _reconectar_db()  # conexão fresca: a fase de browser pode ter matado o socket
    vistos = set()
    salvos = []
    for o in coletados:
        link_produto = _normalizar_link_produto(o["link_produto"])
        if not link_produto or link_produto in vistos:
            continue
        vistos.add(link_produto)
        from apps.scrapers.scraper_mercadolivre.link import e_catalogo_universal
        catalogo = e_catalogo_universal(link_produto)
        defaults = {"campanha_id": "", "origem": origem,
                    "fonte": "mercadolivre-web", "codigo_checkout": codigo_checkout,
                    "nome": o["nome"], "preco_sem_desconto": o["preco_sem_desconto"],
                    "preco_com_cupom": o["preco_com_cupom"],
                    "preco_fonte": o["preco_sem_desconto"],
                    "preco_efetivo": _preco_efetivo_oferta(o),
                    "estado": "invalido" if catalogo else "ativo",
                    "falha_verificacao": (
                        "Catálogo universal sem anúncio individual afiliável."
                        if catalogo else ""), "falhas_consecutivas": 0,
                    "confianca": "media", "evidencia": _evidencia_oferta(o),
                    "imagem_url": o["imagem_url"], "frete_full": o["frete_full"],
                    "relampago": o.get("relampago", False)}
        produto, _ = _upsert_resiliente(
            marketplace="mercadolivre", owner=None, link_produto=link_produto,
            **_taxonomia(o, macro_fixa, defaults))
        if catalogo:
            # Falhas terminais antigas não devem continuar ocupando a tela nem a
            # fila quando a regra agora é global: catálogo universal não publica.
            produto.links_usuario.all().delete()
        else:
            salvos.append(produto)
    # Histórico de preços (B1): 1 observação por item p/ detectar queda real depois.
    from apps.scrapers.precos import registrar_varios
    registrar_varios(salvos)
    return len(salvos)


def _upsert_ofertas(coletados):
    """Insere/atualiza ofertas por link SEM apagar o feed (usado pela LANE RÁPIDA/flash,
    B3, que roda com poucas páginas e não pode zerar o feed completo da lane lenta)."""
    from apps.scrapers.precos import registrar
    _reconectar_db()  # conexão fresca: a fase de browser pode ter matado o socket
    vistos, n = set(), 0
    for o in coletados:
        link_produto = _normalizar_link_produto(o["link_produto"])
        if not link_produto or link_produto in vistos:
            continue
        vistos.add(link_produto)
        from apps.scrapers.scraper_mercadolivre.link import e_catalogo_universal
        catalogo = e_catalogo_universal(link_produto)
        produto, _ = _upsert_resiliente(
            marketplace="mercadolivre", link_produto=link_produto, owner=None,
            **_taxonomia(o, None, {
                "origem": "oferta",
                "nome": o["nome"],
                "preco_sem_desconto": o["preco_sem_desconto"],
                "preco_com_cupom": o["preco_com_cupom"],
                "preco_fonte": o["preco_sem_desconto"],
                "preco_efetivo": _preco_efetivo_oferta(o),
                "fonte": "mercadolivre-web",
                "estado": "invalido" if catalogo else "ativo",
                "falha_verificacao": (
                    "Catálogo universal sem anúncio individual afiliável."
                    if catalogo else ""),
                "imagem_url": o["imagem_url"],
                "frete_full": o["frete_full"],
                "relampago": o.get("relampago", False),
                "evidencia": _evidencia_oferta(o),
            }),
        )
        if catalogo:
            produto.links_usuario.all().delete()
        else:
            registrar("mercadolivre", "", link_produto, o["preco_com_cupom"])
            n += 1
    return n


CURSOR_OFERTAS = "cursor_ofertas_ml"


def _ler_cursor_ofertas(max_paginas: int) -> int:
    """Página em que a próxima passada começa. Sempre dentro de 1..max_paginas.

    Um cursor fora da faixa (o operador baixou `max_paginas`, ou o arquivo veio de
    uma versão anterior) volta ao topo em vez de raspar o vazio.
    """
    from apps.scrapers import automacao_state as st

    try:
        cursor = int(st.read_state("scrape").get(CURSOR_OFERTAS) or 1)
    except (TypeError, ValueError):
        return 1
    return cursor if 1 <= cursor <= max_paginas else 1


def _gravar_cursor_ofertas(proxima_pagina: int) -> None:
    from apps.scrapers import automacao_state as st

    st.write_state("scrape", **{CURSOR_OFERTAS: max(1, int(proxima_pagina))})


def mapear_ofertas(max_paginas=40, substituir=True, usuario=None):
    """Raspa N páginas de /ofertas. substituir=True (lane LENTA): regrava todo o feed.
    substituir=False (lane RÁPIDA/flash, B3): upsert por link, sem zerar o feed.

    /ofertas é público: a sessão aqui é opcional (melhora preço/frete
    personalizado quando existe), não requisito — por isso não avisamos quando
    falta, ao contrário da raspagem de cupons.

    A lane lenta é RETOMÁVEL. Ceder o navegador no meio da varredura só é barato se
    a próxima passada continuar de onde esta parou; recomeçando sempre da página 1,
    ceder na página 5 significaria nunca mais raspar da 6 em diante — as ofertas do
    fundo do feed sumiriam do catálogo justamente quando a máquina está mais
    disputada. O cursor mora no estado do worker (volume, sobrevive a deploy).
    """
    logger.info("Iniciando raspagem de ofertas ML (%s)", "full" if substituir else "flash")
    coletados = []

    vazias_seguidas = 0
    # A lane flash existe para pegar o topo do feed quente; retomar do meio a
    # desvirtuaria. Só a lane lenta, que precisa cobrir o feed inteiro, usa cursor.
    pagina_inicial = _ler_cursor_ofertas(max_paginas) if substituir else 1
    proxima_pagina = 1  # 1 = passada completa; o ciclo seguinte recomeça do topo
    state = storage_state(usuario)
    if pagina_inicial > 1:
        logger.info("Raspagem de ofertas ML retomando da página %s de %s.",
                    pagina_inicial, max_paginas)
    with coordinated_ml_browser(
        usuario=usuario, authenticated=state is not None,
        owner_kind="ml_offers",
        wait_seconds=45 if substituir else 0,
    ), iniciar_browser(storage_state=state, headless=True) as (page, context):
        for n in range(pagina_inicial, max_paginas + 1):
            # Alguém está esperando o navegador AGORA — uma pessoa logando ou uma
            # esteira automática que perdeu a vez. A raspagem completa segura o
            # Chromium da máquina por dezenas de páginas (~93s cada, ~62min o ciclo
            # inteiro medido em 20/08/2026); sem esta saída o login interativo
            # esgotava os 45s de espera e, pior, links/verificação/envio ficavam
            # parados o ciclo inteiro — sem link não há cupom pronto, e sem cupom
            # pronto não há envio.
            # Sair aqui não perde trabalho: o que já foi coletado é salvo e o
            # cursor guarda a próxima página, de onde o ciclo seguinte retoma.
            if n > pagina_inicial and interesse_pendente(
                    "django_chromium", exceto="ml_offers"):
                logger.info(
                    "Raspagem de ofertas ML cedeu o navegador após %s de %s "
                    "página(s); retoma na página %s.",
                    n - pagina_inicial, max_paginas, n,
                )
                proxima_pagina = n
                break
            emitir_progresso(f"[PROGRESSO] Ofertas página {n}/{max_paginas} ({n*100//max_paginas}%)")
            try:
                page.goto(f"https://www.mercadolivre.com.br/ofertas?page={n}",
                          wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("Erro ao carregar pagina de ofertas ML %s: %s", n, e)
                # Falha de carregamento entra na mesma contagem das vazias: um
                # bloqueio total encerra em 3 páginas em vez de gastar 40 timeouts.
                vazias_seguidas += 1
                if vazias_seguidas >= VAZIAS_PARA_PARAR:
                    logger.warning("%s páginas seguidas sem ofertas; encerrando na %s",
                                   vazias_seguidas, n)
                    break
                continue
            cards = _coletar_cards(page)
            if not cards:
                vazias_seguidas += 1
                if vazias_seguidas >= VAZIAS_PARA_PARAR:
                    logger.info("%s páginas seguidas sem ofertas; fim do feed na %s",
                                vazias_seguidas, n)
                    break
                logger.info("Pagina %s sem ofertas; seguindo para a proxima", n)
                pausa_humana()
                continue
            vazias_seguidas = 0
            coletados.extend(cards)
            pausa_humana()  # ritmo humano entre páginas (anti-bloqueio)

    if substituir:
        # Fora do `with`: gravar o cursor não depende do navegador, e sair do
        # contexto primeiro evita segurar o Chromium por uma escrita em disco.
        _gravar_cursor_ofertas(proxima_pagina)

    if not coletados:
        logger.warning("Raspagem de ofertas ML vazia; feed existente preservado")
        return 0
    n = (_salvar(coletados, origem="oferta") if substituir
         else _upsert_ofertas(coletados))
    logger.info("Ofertas ML salvas/atualizadas: %s", n)
    return n


def _slug_busca(termo):
    """Converte 'robô aspirador' -> 'robo-aspirador' para a URL de busca do ML."""
    import unicodedata
    t = unicodedata.normalize("NFKD", termo).encode("ascii", "ignore").decode().lower().strip()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def buscar_por_termo(termo_busca, min_desconto=15, max_paginas=3, macro=None,
                     usuario=None):
    """
    Para cada termo (lista separada por vírgula) raspa a BUSCA do ML com filtro de
    desconto e salva como origem='busca'. Atualiza só os itens 'busca' que casam com
    estes termos (não mexe no feed nem nos cupons-código).

    A busca é pública; a sessão é opcional (ver mapear_ofertas).
    """
    termos = [t.strip() for t in (termo_busca or "").split(",") if t.strip()]
    if not termos:
        return 0
    coletados = []
    termos_confirmados = []

    state = storage_state(usuario)
    with coordinated_ml_browser(
        usuario=usuario, authenticated=state is not None,
        owner_kind="ml_search",
    ), iniciar_browser(storage_state=state, headless=True) as (page, context):
        for indice_termo, termo in enumerate(termos):
            # Mesmo motivo de mapear_ofertas: ceder a quem está na fila em vez de
            # segurar o Chromium até o fim de todos os termos configurados.
            if indice_termo > 0 and interesse_pendente(
                    "django_chromium", exceto="ml_search"):
                logger.info(
                    "Busca ML cedeu o navegador após %s de %s termo(s).",
                    indice_termo, len(termos),
                )
                break
            slug = _slug_busca(termo)
            if not slug:
                continue
            vazias_seguidas = 0  # a tolerância é POR TERMO
            coletados_termo = []
            for p in range(max_paginas):
                desde = p * 50 + 1
                url = f"https://lista.mercadolivre.com.br/{slug}_Discount_{int(min_desconto)}-100"
                if desde > 1:
                    url += f"_Desde_{desde}"
                emitir_progresso(f"[PROGRESSO] Busca '{termo}' pág {p+1}/{max_paginas}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                except Exception as e:
                    logger.warning("Erro na busca ML por termo '%s': %s", termo, e)
                    vazias_seguidas += 1
                    if vazias_seguidas >= VAZIAS_PARA_PARAR:
                        break
                    continue
                cards = _coletar_cards(page)
                if not cards:
                    # Mesma razão de mapear_ofertas: a 1ª página em branco costuma ser
                    # bloqueio/render, não fim dos resultados do termo.
                    vazias_seguidas += 1
                    if vazias_seguidas >= VAZIAS_PARA_PARAR:
                        break
                    pausa_humana()
                    continue
                vazias_seguidas = 0
                coletados_termo.extend(cards)
                pausa_humana()  # ritmo humano entre páginas (anti-bloqueio)
            if coletados_termo:
                termos_confirmados.append(termo)
                coletados.extend(coletados_termo)

    # Uma resposta inteiramente vazia é indistinguível de bloqueio, timeout ou
    # mudança de layout. Nunca apaga o catálogo anterior nesse cenário.
    if not coletados:
        logger.warning("Busca ML '%s' vazia; resultados existentes preservados",
                       termo_busca)
        return 0

    # Refresh escopado: remove itens 'busca' que casam com algum termo, recria.
    # Reconecta antes: o delete é a 1ª query após a longa fase de browser.
    _reconectar_db()
    from django.db.models import Q
    cond = Q()
    for t in termos_confirmados:
        cond |= Q(nome__icontains=t)
    Produto.objects.filter(
        marketplace="mercadolivre", owner__isnull=True, origem="busca"
    ).filter(cond).delete()
    n = _salvar(coletados, origem="busca", macro_fixa=macro)
    logger.info("Busca ML '%s': %s produtos salvos", termo_busca, n)
    return n

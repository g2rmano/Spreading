import hashlib
import json
import logging
import re

from django.conf import settings

from apps.scrapers.ia_custo import registrar_uso

logger = logging.getLogger(__name__)

# Haiku por padrão: estas chamadas são curtas, estruturadas e de alto volume
# (uma por mensagem de canal, uma por publicação). Sonnet custa muitas vezes
# mais por token e não escreve uma chamada de oferta melhor o bastante para
# justificar a diferença. Trocável por LLM_MODELO quando houver motivo medido.
_MODELO_PADRAO = "claude-haiku-4-5-20251001"

# Seis horas: preço revalidado ao vivo muda a chave antes disso, e o cache
# em produção é LocMem (morre no restart), então TTL longo não ajudaria.
_TTL_TEXTO_DEAL_S = 6 * 3600

_PROMPT = """Você escreve a chamada de um achado para grupo de WhatsApp no Brasil.
Tom: assertivo, concreto, curto. Alguém que achou um desconto de verdade, não um influencer.

REGRAS OBRIGATÓRIAS:
1. "titulo": TUDO EM CAIXA ALTA, 3 a 6 palavras, sem aspas, emoji, ponto, preço, %, R$ ou a palavra cupom.
2. O título nomeia o ganho (o que a pessoa leva ou o que muda). Proibido: IMPERDÍVEL, OPORTUNIDADE, CORRE, BORA, CLIQUE, OFERTA, PROMOÇÃO, DESCONTO, OFF, PROMO.
3. Sem piada sobre o comprador. Sem "pra tu que". Sem "se sentir".
4. "nome_curto": tipo + marca + modelo + no máximo 2 características. Máximo 70 caracteres. Não invente.
5. Sem Markdown, asterisco ou formatação.
6. Responda SOMENTE JSON: {{"titulo":"...","nome_curto":"..."}}.

Exemplos:
Produto: Multivitamínico 120 Cáps. Growth Supplements
Resposta: {{"titulo":"VITAMINA SEM ENROLAÇÃO","nome_curto":"Multivitamínico Growth 120 cápsulas"}}

Produto: Cadeira Gamer Wells Preta Healer
Resposta: {{"titulo":"CADEIRA QUE SEGURA PESO","nome_curto":"Cadeira Gamer Healer Wells Preta"}}

Produto: Monitor Gamer Samsung Odyssey G5 27, Resolução QHD, Taxa de atualização de 165Hz & 1ms de tempo de resposta (MPRT), Curvatura com 1000R, HDR 10, AMD FreeSync, Eye Saver Mode & Flicker Free Mode
Resposta: {{"titulo":"TELA QUE NÃO ATRASA","nome_curto":"Monitor Gamer Samsung Odyssey G5 27 QHD 165Hz"}}

Agora:
{contexto}
Resposta:"""

_PROMPT_AVALIACAO = """Você decide se um cupom vale publicar num grupo de WhatsApp de achados
de desconto no Brasil. O piso automático já bloqueou cupons cujo benefício em reais é
irrisório (teto ou valor fixo abaixo de um mínimo configurado) — isso NÃO chega até você.
Sua função é a leitura que um número sozinho não pega: condição confusa, escopo ilegível,
ou cheiro de isca (percentual chamativo com pegadinha na letra miúda).

Rejeite só quando houver um motivo concreto e citável. Na dúvida, aceite — cupom real e
comum não é "ruim" por não ser excepcional.

Dados do cupom:
{contexto}

Responda SOMENTE JSON:
{{"vale_a_pena": true ou false, "motivo": "...", "escopo_legivel": "..."}}
- "motivo": até 12 palavras, em português, direto ao ponto.
- "escopo_legivel": reescreva o campo Escopo de forma humana e curta para aparecer numa
  mensagem de WhatsApp (ex.: "produtos de Glamour.div" vira "loja Glamour"; um handle cru
  de loja vira nome legível). Se já estiver claro, repita como veio. Nunca invente marca,
  loja ou categoria que não esteja no escopo original — só reescreva o que já existe.
"""

# Enxuto de propósito. O prompt é pago por inteiro em TODA chamada, e este roda uma
# vez por publicação: cada linha aqui é custo recorrente. A versão longa gastava 717
# tokens só de instrução para produzir ~120 de saída. Regra prática: se o validador
# em código já derruba a violação, o prompt não precisa explicá-la duas vezes.
_PROMPT_DEAL = """Você escreve UMA linha de apoio para um post de oferta em grupo de
achados no Brasil. A mensagem já mostra, em linhas próprias, o NOME do produto, o
PREÇO e o CUPOM. Sua linha não repete nada disso.

REGRAS:
1. Não escreva o nome, a marca nem o modelo do produto: já estão logo acima.
2. Diga o que a pessoa GANHA com ele — uso, tamanho, autonomia, para quem serve.
3. Só escreva número que esteja em "Números liberados" ou no nome do produto.
4. Só afirme o que está em "Pode afirmar". Fora dela, nada de menor preço, acaba
   hoje, últimas unidades ou frete grátis.
5. Não invente característica que não esteja no nome do produto.
6. Até 14 palavras, em português do Brasil CORRETO e ACENTUADO, sem jargão de
   anúncio. No máximo um emoji, no fim. Sem markdown.
7. Se não houver nada útil a acrescentar, devolva "linha" vazia. Linha vazia é uma
   resposta correta; encher linguiça não é.
8. Responda SOMENTE JSON: {{"linha":"..."}}

Exemplo:
Dados: Produto: Air Fryer Mondial Family 4L Preta | Preço final: R$ 249 | Números liberados: 4, 249
Resposta: {{"linha":"Quatro litros dão conta da janta de duas ou três pessoas"}}

Exemplo:
Dados: Produto: Cabo USB-C 1m Preto | Preço final: R$ 19 | Números liberados: 1, 19
Resposta: {{"linha":""}}

Agora:
{contexto}
Resposta:"""

_PROMPT_NOMES = """Resuma nomes de produtos para mensagens de promoções.

REGRAS:
1. Preserve tipo do produto, marca, modelo e no máximo 2 características essenciais.
2. Remova listas técnicas, recursos secundários, texto publicitário, frete e repetições.
3. Cada nome deve ter no máximo 70 caracteres.
4. Não invente informação e não use Markdown, emoji ou preço.
5. Responda SOMENTE com um array JSON de strings, na mesma ordem da entrada.

Produtos:
{produtos}

Resposta:"""


def _bloco_contexto(nome, preco=None, desconto_percent=None, categoria=None) -> str:
    """Linhas de contexto do produto p/ o prompt; só entra o que existir."""
    linhas = [f"Produto: {nome.strip()}"]
    if preco:
        linhas.append(f"Preço atual: R$ {float(preco):.2f}")
    # Desconto minúsculo não é gancho de venda; só entra quando impressiona.
    if desconto_percent and 5 <= float(desconto_percent) < 90:
        linhas.append(f"Desconto: {float(desconto_percent):.0f}%")
    if categoria:
        linhas.append(f"Categoria: {str(categoria).strip()}")
    return "\n".join(linhas)


def _texto_resposta(resposta) -> str:
    return "".join(
        bloco.text for bloco in resposta.content if getattr(bloco, "type", "") == "text"
    ).strip()


def _json_resposta(texto: str):
    """Aceita JSON puro ou cercado por ```json, sem tolerar prosa adicional."""
    limpo = str(texto or "").strip()
    limpo = re.sub(r"^```(?:json)?\s*", "", limpo, flags=re.I)
    limpo = re.sub(r"\s*```$", "", limpo)
    return json.loads(limpo)


def _sem_formatacao(texto, limite=80) -> str:
    limpo = re.sub(r"[*_`~]+", "", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip().strip("\"'")
    if len(limpo) <= limite:
        return limpo.rstrip(" -–—,;|/")
    cortado = limpo[:limite + 1].rsplit(" ", 1)[0]
    return (cortado or limpo[:limite]).rstrip(" -–—,;|/")


_TITULO_PROIBIDO = re.compile(
    r"\b(?:CUPOM|PROMOÇÃO|PROMOCAO|OFERTA|IMPERD[IÍ]VEL|OPORTUNIDADE|"
    r"CORRE|BORA|CLIQUE|DESCONTO|OFF|PROMO)\b",
    re.I,
)


def _titulo_chamada(texto) -> str:
    limpo = _sem_formatacao(texto, 80).upper()
    palavras = [p for p in re.split(r"\s+", limpo) if p]
    if not 2 <= len(palavras) <= 6:
        return ""
    if _TITULO_PROIBIDO.search(limpo):
        return ""
    if re.search(r"\d+\s*%|R\$", limpo):
        return ""
    return limpo


def _cliente(timeout):
    import anthropic

    return anthropic.Anthropic(
        api_key=getattr(settings, "ANTHROPIC_API_KEY", ""),
        timeout=float(timeout),
    )


def gerar_conteudo(nome: str, timeout: int = 30, preco=None,
                   desconto_percent=None, categoria=None) -> dict:
    """Gera chamada e nome curto em uma única chamada ao Claude.

    Retorna sempre ``{"titulo": str, "nome_curto": str}``; qualquer falha
    degrada para strings vazias e nunca impede o envio.

    Gate: settings.LLM_ATIVO e uma ANTHROPIC_API_KEY presente. Motor trocado do
    Ollama local (que não roda no Fly) para a API do Claude (anthropic SDK).
    Preço/desconto/categoria são opcionais e afiam o gancho da frase; o prompt
    proíbe citar o preço em números porque a frase fica em cache (frase_llm) e
    é reaproveitada em envios com preço já atualizado.
    """
    vazio = {"titulo": "", "nome_curto": ""}
    if not getattr(settings, "LLM_ATIVO", False) or not nome:
        return vazio
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        # Sem título por IA na mensagem = quase sempre isto. Loga uma vez p/ o
        # painel de saúde mostrar o motivo em vez de "sumiu o título".
        logger.warning("LLM sem ANTHROPIC_API_KEY: título por IA não será gerado")
        return vazio

    try:
        contexto = _bloco_contexto(nome, preco, desconto_percent, categoria)
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=180,
            # Sonnet 5 habilita pensamento adaptativo por padrão. Estas respostas
            # são JSON curto e determinístico; desativá-lo preserva latência e
            # deixa todo o orçamento de saída disponível para o conteúdo.
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": _PROMPT.format(contexto=contexto)}],
        )
        registrar_uso(resposta, origem="gerar_conteudo")
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, dict):
            return vazio
        return {
            "titulo": _titulo_chamada(dados.get("titulo")),
            "nome_curto": _sem_formatacao(dados.get("nome_curto"), 70),
        }
    except Exception as exc:
        logger.warning("Falha ao gerar conteúdo por IA: %s: %s", type(exc).__name__, exc)
        return vazio


# Palhaçada de anúncio. Não é purismo: é o que faz o grupo silenciar a lista.
_FRASE_PROIBIDA = re.compile(
    r"\b(?:imperd[ií]vel|corre|voa|bora|clique aqui|"
    r"aproveite j[áa]|garanta j[áa]|oportunidade [úu]nica)\b",
    re.I,
)

# Alegações que o sistema precisa TER PROVADO para deixar passar. A chave é o nome
# da prova; o valor, o que o modelo não pode escrever sem ela.
_ALEGACOES_CONTROLADAS = {
    # Bloquear frases exatas não funciona: pedimos para o modelo não dizer "menor
    # preço" e ele escreveu "menor cotação em 90 dias", que afirma exatamente a
    # mesma coisa. A regra tem de pegar a AFIRMAÇÃO — superlativo de preço ou
    # comparação com o passado — e não a redação escolhida.
    "minima": re.compile(
        r"(?:"
        r"(?:men[oa]r|mais baix[oa]|melhor)\s+(?:pre[çc]o|valor|cota[çc][ãa]o|oferta)"
        r"|pre[çc]o\s+mais\s+baix[oa]"
        r"|mais\s+barat[oa]"
        r"|m[íi]nima\s+hist[óo]rica"
        r"|nunca\s+(?:esteve|custou|foi|ficou)"
        r"|hist[óo]ric[oa]\s+de\s+pre[çc]o"
        r"|(?:em|nos\s+[úu]ltimos|dos\s+[úu]ltimos)\s+\d+\s*(?:dias|meses)"
        r")", re.I),
    "urgencia": re.compile(
        r"(?:[úu]ltima chance|acaba hoje|termina hoje|s[óo] hoje|"
        r"[úu]ltimas horas|expira hoje)", re.I),
    "estoque": re.compile(
        r"(?:[úu]ltimas unidades|acabando|estoque limitado|poucas unidades)", re.I),
    "frete": re.compile(r"frete gr[áa]tis", re.I),
    "gratis": re.compile(r"\b(?:gr[áa]tis|de gra[çc]a)\b", re.I),
}

_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")


def _normalizar_numero(bruto: str) -> str:
    """"R$ 1.299,00" e "1299" viram a mesma coisa. Ponto é milhar, vírgula é decimal."""
    texto = str(bruto).replace(".", "")
    if "," in texto:
        inteiro, _, decimal = texto.partition(",")
        decimal = decimal.rstrip("0")
        return f"{inteiro}.{decimal}" if decimal else inteiro
    return texto


def numeros_do_texto(texto) -> set:
    return {_normalizar_numero(m.group(0)) for m in _NUMERO.finditer(str(texto or ""))}


def _frase_vendavel(texto, *, permitidos, provas, limite=160, palavras=26) -> str:
    """Frase que pode vender, mas só pode afirmar o que o sistema mediu.

    A versão anterior proibia QUALQUER número. Estava errada para este mercado: o
    padrão do Promobit e do Pelando é o número no título ("Fone a R$ 77", "R$ 240
    OFF"), e um gancho sem número não vende oferta nenhuma — foi o defeito apontado.

    A trava certa não é proibir número, é conferir de ONDE ele veio: todo algarismo
    escrito pelo modelo tem de estar na lista que o código passou (preço final,
    economia, abatimento do cupom, percentual comprovado) ou já existir no nome do
    produto. Qualquer outro é invenção e derruba o campo inteiro.

    O mesmo vale para as alegações fortes: "menor preço" só passa quando o histórico
    provou, "acaba hoje" só quando a validade é curta. Sem prova, o campo cai — a
    mensagem sai sem a frase, nunca com a frase falsa.
    """
    limpo = _sem_formatacao(texto, limite)
    if not limpo:
        return ""
    if _FRASE_PROIBIDA.search(limpo):
        return ""
    if len(limpo.split()) > palavras:
        return ""
    if numeros_do_texto(limpo) - set(permitidos):
        return ""
    for nome, padrao in _ALEGACOES_CONTROLADAS.items():
        if nome not in provas and padrao.search(limpo):
            return ""
    return limpo


def _frase_humana(texto, limite=140) -> str:
    """Frase sem número nenhum. Mantida para chamadores que não têm fatos a liberar."""
    return _frase_vendavel(texto, permitidos=set(), provas=set(), limite=limite)


def gerar_texto_deal(*, nome, categoria="", motivo="", tem_cupom=False,
                     preco_final=None, economia=None, beneficio_cupom=None,
                     percentual=None, janela_dias=None, provas=(),
                     timeout: int = 20) -> dict:
    """Post de venda de um deal: um gancho com número e UMA frase de apoio.

    Dois campos de texto viravam quatro linhas de prosa numa mensagem que já tem
    preço, cupom, prova e validade. No grupo isso lê como parede. A frase única
    obriga a escolher o que importa — e corta tokens de saída junto.

    Devolve sempre ``{"gancho","produto","porque_vale"}``; qualquer falha degrada
    para strings vazias e NUNCA impede o envio — a mensagem sem estes campos
    continua completa, porque preço, economia, cupom e prova são impressos pelo
    código.

    `provas` é o conjunto de alegações que o sistema mediu e portanto autoriza
    ("minima", "urgencia"). O que não está lá, o validador derruba.

    Uma chamada por tentativa real de envio, como `avaliar_cupom_ia`, e depois da
    revalidação de preço — por isso os números passados aqui são os mesmos que a
    mensagem vai imprimir. Sem cache: o texto depende do preço daquele momento.
    """
    vazio = {"linha": ""}
    if not getattr(settings, "LLM_ATIVO", False) or not nome:
        return vazio
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        logger.warning("LLM sem ANTHROPIC_API_KEY: texto do deal não será gerado")
        return vazio

    fatos = {
        "Preço final": preco_final,
        "Economia": economia,
        "Cupom abate": beneficio_cupom,
        "Desconto": percentual,
        "Janela do histórico (dias)": janela_dias,
    }
    permitidos = numeros_do_texto(nome)
    partes = [f"Produto: {nome}"]
    if categoria:
        partes.append(f"Categoria: {categoria}")
    for rotulo, valor in fatos.items():
        if valor in (None, "", 0, 0.0):
            continue
        formatado = formatar_valor_br(valor)
        permitidos |= numeros_do_texto(formatado)
        sufixo = "%" if rotulo == "Desconto" else ""
        prefixo = "" if rotulo in ("Desconto", "Janela do histórico (dias)") else "R$ "
        partes.append(f"{rotulo}: {prefixo}{formatado}{sufixo}")
    if motivo:
        partes.append(f"Motivo: {motivo}")
    partes.append(
        "Desconto sai por cupom no checkout." if tem_cupom
        else "Sem cupom: o preço já está aplicado na página."
    )
    partes.append("Números liberados: " + (", ".join(sorted(permitidos)) or "nenhum"))
    rotulos_prova = {
        "minima": "menor preço observado em 90 dias",
        "urgencia": "a oferta termina em poucas horas",
    }
    partes.append("Pode afirmar: " + (", ".join(
        rotulos_prova[p] for p in provas if p in rotulos_prova) or "nenhuma"))
    contexto = " | ".join(partes)

    # Contexto igual, resposta igual: não há motivo para pagar de novo. A chave é o
    # hash do contexto inteiro, então qualquer mudança de preço, cupom ou prova gera
    # uma chave nova — o cache nunca devolve texto que descreva outro preço.
    from django.core.cache import cache

    chave = "deal-copy:" + hashlib.sha256(contexto.encode("utf-8")).hexdigest()[:32]
    guardado = cache.get(chave)
    if guardado is not None:
        return guardado

    try:
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=90,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": _PROMPT_DEAL.format(contexto=contexto),
            }],
        )
        registrar_uso(resposta, origem="gerar_texto_deal")
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, dict):
            return vazio
        provas = set(provas)
        texto = {
            "linha": _frase_vendavel(
                dados.get("linha"), permitidos=permitidos, provas=provas,
                limite=110, palavras=16),
        }
        # Só vale guardar o que sobreviveu ao validador; texto vazio significa que o
        # modelo violou uma regra, e uma nova tentativa pode acertar.
        if any(texto.values()):
            cache.set(chave, texto, _TTL_TEXTO_DEAL_S)
        return texto
    except Exception as exc:
        logger.warning("Falha ao gerar texto do deal: %s: %s",
                       type(exc).__name__, exc)
        return vazio


def formatar_valor_br(valor) -> str:
    """Mesmo formato do corpo da mensagem, para o modelo ver o número que vai sair."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return ""
    texto = f"{numero:.2f}".rstrip("0").rstrip(".")
    inteiro, _, decimal = texto.partition(".")
    return f"{inteiro},{decimal}" if decimal else inteiro


# Exigir que TODA palavra do gancho estivesse no nome do produto foi tentado e
# revertido: o nome que a loja cadastra é frequentemente pior que a realidade
# ("Fritadeira Air Fryer 4,5l Widemax" para uma Midea), e a trava rejeitava
# ganchos corretos. Número continua sob lista branca; texto confia no modelo.
_CONECTIVOS_GANCHO = {
    "POR", "COM", "SEM", "DE", "DA", "DO", "DAS", "DOS", "E", "EM", "NO", "NA",
    "A", "O", "AS", "OS", "OFF", "CUPOM", "ATE", "ATÉ", "SO", "SÓ", "HOJE",
    "AGORA", "CADA", "LEVE", "PAGUE", "R$", "MENOS", "MAIS", "SAI", "FICA", "+",
}


def _tokens_do_nome(nome) -> set:
    import unicodedata

    cru = unicodedata.normalize("NFKD", str(nome or ""))
    cru = "".join(c for c in cru if not unicodedata.combining(c)).upper()
    return {t for t in re.split(r"[^A-Z0-9]+", cru) if t}


def _gancho_de_venda(texto, permitidos, provas, nome_produto="") -> str:
    """Chamada em caixa alta que nomeia produto e número. Pode conter R$ e %.

    Diferente de `_titulo_chamada`, que serve ao formato antigo de produto e proíbe
    OFERTA/DESCONTO/OFF: ali o bloco de preço logo abaixo é que vendia, e o título
    era só a chamada. Aqui o gancho É o anúncio, e nesse mercado "OFF" e "por R$ X"
    são vocabulário corrente, não ruído.
    """
    limpo = _sem_formatacao(texto, 90).upper()
    palavras = [p for p in re.split(r"\s+", limpo) if p]
    if not 3 <= len(palavras) <= 10:
        return ""
    return _frase_vendavel(
        limpo, permitidos=permitidos, provas=provas, limite=90, palavras=10)


def gerar_nomes_curtos(nomes, timeout: int = 10) -> list[str]:
    """Resume vários nomes longos em uma chamada, preservando a ordem."""
    nomes = [str(nome or "").strip() for nome in nomes]
    if not nomes or not getattr(settings, "LLM_ATIVO", False):
        return [""] * len(nomes)
    if not getattr(settings, "ANTHROPIC_API_KEY", ""):
        return [""] * len(nomes)
    try:
        produtos = "\n".join(
            f"{indice + 1}. {nome}" for indice, nome in enumerate(nomes)
        )
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=max(180, len(nomes) * 60),
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": _PROMPT_NOMES.format(produtos=produtos),
            }],
        )
        registrar_uso(resposta, origem="gerar_nomes_curtos")
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, list) or len(dados) != len(nomes):
            return [""] * len(nomes)
        return [_sem_formatacao(nome, 70) for nome in dados]
    except Exception as exc:
        logger.warning("Falha ao resumir nomes por IA: %s: %s", type(exc).__name__, exc)
        return [""] * len(nomes)


def gerar_descricao(nome: str, timeout: int = 30, preco=None,
                    desconto_percent=None, categoria=None) -> str:
    """Compatibilidade: consumidores antigos recebem somente a chamada."""
    return gerar_conteudo(
        nome, timeout=timeout, preco=preco,
        desconto_percent=desconto_percent, categoria=categoria,
    )["titulo"]


def avaliar_cupom_ia(*, escopo="", tipo_desconto="", valor_desconto=None,
                     desconto_maximo=None, valor_minimo=None, restrito=False,
                     timeout: int = 15) -> dict:
    """Segunda opinião sobre um cupom que JÁ passou pelo piso monetário fixo
    (``coupon_rules.cupom_e_lixo``). O piso pega valor irrisório; isto pega o
    que só leitura pega — condição confusa, escopo ilegível, cheiro de isca.

    Chamada UMA VEZ por tentativa real de envio (dentro de ``enviar_cupom``),
    nunca no funil de milhares de cupons — por isso pode ser um Sonnet completo
    sem custar escala.

    Fail-open por desenho: IA desligada ou fora do ar nunca bloqueia o envio
    sozinha — degrada para ``vale_a_pena=True``. O piso monetário fixo já é
    quem segura sozinho o caso claro; a IA é uma camada A MAIS, não a única
    porta antes do envio.
    """
    escopo_limpo = str(escopo or "").strip()
    vazio = {"vale_a_pena": True, "motivo": "", "escopo_legivel": escopo_limpo}
    if not getattr(settings, "LLM_ATIVO", False):
        return vazio
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not api_key:
        return vazio
    try:
        linhas = [f"Escopo: {escopo_limpo or '(não informado)'}"]
        if tipo_desconto:
            linhas.append(f"Tipo de desconto: {tipo_desconto}")
        if valor_desconto is not None:
            unidade = "%" if tipo_desconto == "porcentagem" else "R$"
            linhas.append(f"Valor anunciado: {unidade}{float(valor_desconto):.0f}")
        if desconto_maximo:
            linhas.append(f"Teto do desconto: R${float(desconto_maximo):.2f}")
        if valor_minimo:
            linhas.append(f"Compra mínima: R${float(valor_minimo):.2f}")
        if restrito:
            linhas.append("Público restrito: sim")
        contexto = "\n".join(linhas)
        resposta = _cliente(timeout).messages.create(
            model=getattr(settings, "LLM_MODELO", _MODELO_PADRAO),
            max_tokens=90,
            thinking={"type": "disabled"},
            messages=[{
                "role": "user",
                "content": _PROMPT_AVALIACAO.format(contexto=contexto),
            }],
        )
        registrar_uso(resposta, origem="avaliar_cupom_ia")
        dados = _json_resposta(_texto_resposta(resposta))
        if not isinstance(dados, dict):
            return vazio
        vale = dados.get("vale_a_pena")
        if not isinstance(vale, bool):
            return vazio
        escopo_legivel = _sem_formatacao(dados.get("escopo_legivel"), 120)
        return {
            "vale_a_pena": vale,
            "motivo": _sem_formatacao(dados.get("motivo"), 140),
            "escopo_legivel": escopo_legivel or escopo_limpo,
        }
    except Exception as exc:
        logger.warning("Falha ao avaliar cupom por IA: %s: %s", type(exc).__name__, exc)
        return vazio

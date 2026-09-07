import logging
import os
import random
import re
import requests
from datetime import timedelta
from django.conf import settings
from django.utils import timezone
from django.db import DatabaseError, transaction
from django.db.models import F, FloatField, ExpressionWrapper, Count, Q
from apps.scrapers.models import Produto, Cupom, HistoricoEnvio, Publicacao
from apps.scrapers.precos import (
    chave_produto as _chave_preco,
    stats as _stats_preco,
    stats_em_lote as _stats_preco_em_lote,
)
from apps.scrapers.whatsapp_client import DESCONHECIDO, PERMANENTE, TRANSITORIO

logger = logging.getLogger(__name__)


def _send_pipeline_v2_enabled(user) -> bool:
    from apps.accounts.feature_flags import send_pipeline_v2_enabled
    return send_pipeline_v2_enabled(user)


def _executar_orm(fn, *args, **kwargs):
    """Query de ORM com o escopo de tenant certo em cada chamador deste núcleo.

    Atalho local para `tenant.executar_orm_ou_direto`: no SSE (tenant anotado)
    instala o escopo numa transação curta; no worker de sistema (role BYPASSRLS,
    sem escopo) cai no caminho direto de sempre.
    """
    from apps.accounts.tenant import executar_orm_ou_direto
    return executar_orm_ou_direto(fn, *args, **kwargs)


def _motivo_publico_transporte(resultado) -> str:
    """Traduz falhas externas para mensagens estáveis; o detalhe fica no evento."""
    resultado = resultado or {}
    if resultado.get("resultado") == "incerto":
        return ("A entrega não pôde ser confirmada e, para evitar duplicidade, "
                "não será repetida automaticamente.")
    classe = resultado.get("classe")
    erro = str(resultado.get("erro") or "").lower()
    if classe == "transitorio":
        # Reconexão é automática (o worker vigia o WAState e religa sozinho, e o
        # próprio envio espera por ela). Dizer só "indisponível" fazia o usuário
        # procurar o que reconectar — não há nada para ele fazer aqui.
        if any(p in erro for p in ("reconect", "não está conectado", "nao esta conectado")):
            return ("O WhatsApp estava reconectando neste instante. A conexão volta "
                    "sozinha — tente novamente em alguns segundos.")
        return "O canal está temporariamente indisponível. Tente novamente mais tarde."
    if classe == "permanente":
        if any(p in erro for p in ("destino", "grupo", "chat", "@g.us", "@canal")):
            return "O destino informado é inválido ou não está acessível pelo canal."
        if any(p in erro for p in ("token", "credencial", "conect", "sessão", "bot")):
            return "As credenciais do canal precisam ser reconectadas."
        return "O canal rejeitou o envio. Revise as credenciais e o destino."
    return "Não foi possível confirmar o envio pelo canal selecionado."


def _motivo_reprovacao_da_loja(marketplace, relatorio, confiar_desconto) -> str:
    """Motivo escrito pela loja que realmente verificou o link.

    `link_validacao.motivo_reprovacao` lê o relatório do Mercado Livre (chaves
    `is_pagina_produto`, `preco_visivel`, ...). Aplicá-lo ao relatório da Amazon —
    que traz apenas `ok`/`motivo` — devolvia "O link não abre uma página de produto
    do Mercado Livre" para um item da Amazon: a mesma confusão de loja que o
    verificador em lote produzia, agora no caminho do envio.
    """
    if getattr(marketplace, "slug", "") != "mercadolivre":
        return str(relatorio.get("motivo") or "")[:280] or (
            "O link não passou na verificação da loja.")
    from apps.scrapers.link_validacao import motivo_reprovacao
    return motivo_reprovacao(relatorio, confiar_desconto)


def _canal_pronto_ou_erro(canal, usuario) -> dict | None:
    """Confere a conexão do canal ANTES de tentar enviar.

    O envio para um WhatsApp desconectado só falhava lá no transporte, com uma
    mensagem genérica ("não foi possível confirmar o envio"). O usuário precisa
    saber que o problema é a conexão e ser levado a reconectar — não descobrir
    um erro opaco depois de montar a mensagem. Devolve None quando o canal está
    pronto; senão um dict de falha com o motivo e o flag de reconexão que a UI
    usa para oferecer o botão de reconectar.
    """
    if str(canal or "").lower() != "whatsapp":
        return None
    from apps.scrapers import whatsapp_client
    from apps.scrapers.conexoes import estado_whatsapp

    sessao = _executar_orm(wa_session_de, usuario)
    if not sessao:
        return {"sucesso": False,
                "motivo": "Conecte o WhatsApp antes de enviar.",
                "classe": TRANSITORIO, "precisa_login_wa": True}
    estado = estado_whatsapp(usuario, session=sessao)
    if estado.conectado:
        return None
    # Request web nunca espera o Chromium/worker reconectar. A operação permanece
    # transitória e o pipeline assíncrono decide o retry.
    if estado.detalhe in ("conectando", "capacidade"):
        return {"sucesso": False,
                "motivo": estado.motivo
                or "WhatsApp reativando a conexão — tente novamente em instantes.",
                "classe": TRANSITORIO,
                "etapa": "transport_queued"}
    # Não falamos com o worker: o pareamento pode estar perfeito. Pedir "reconecte
    # sua conta" aqui mandava o usuário reler um QR Code que não era o problema —
    # o defeito está entre o Django e o worker, e é nosso, não dele.
    if estado.detalhe == "servico_fora":
        logger.warning("Gate de envio sem resposta do worker WhatsApp (sessão %s): %s",
                       sessao, estado.motivo)
        return {"sucesso": False,
                "motivo": estado.motivo or "Serviço de WhatsApp indisponível.",
                "classe": TRANSITORIO}
    # 'inativo': o worker tem a credencial mas ela saiu do Map (restore pulado no
    # boot, runtime destruído). Religar é seguro e não precisa de QR.
    religou = False
    try:
        bruto = whatsapp_client.status(sessao)
        if not bruto.get("conectado") and bruto.get("fase") == "inativo":
            whatsapp_client.iniciar_sessao(sessao)
            religou = True
    except Exception:
        pass
    if religou:
        return {"sucesso": False,
                "motivo": "WhatsApp reativando a conexão — tente novamente em instantes.",
                "classe": TRANSITORIO, "etapa": "transport_queued"}
    return {"sucesso": False,
            "motivo": estado.motivo
            or "WhatsApp desconectado. Reconecte sua conta para enviar.",
            "classe": TRANSITORIO, "precisa_login_wa": True}


# Orçamento da espera por reconexão dentro de um request. 20s é o teto: a thread do
# gunicorn fica presa aqui, e o worker religa com backoff a partir de 5s — então
# isto cobre a primeira e a segunda tentativa dele sem transformar o envio numa
# requisição pendurada.
_WA_ESPERA_RECONEXAO_S = 20
_WA_ESPERA_INTERVALO_S = 2.5


def _esperar_wa_reconectar(usuario, sessao):
    """Reconsulta o estado do WhatsApp até ele voltar ou o orçamento acabar.

    Devolve o último Estado lido (nunca None), para o chamador reaproveitar o
    `motivo` já calculado pela fonte única. Invalida o cache de 5s do
    whatsapp_client entre as leituras — sem isso as reconsultas devolveriam o mesmo
    payload velho e a espera não observaria nada.
    """
    import time

    from apps.scrapers import whatsapp_client
    from apps.scrapers.conexoes import estado_whatsapp

    limite = time.monotonic() + _WA_ESPERA_RECONEXAO_S
    estado = estado_whatsapp(usuario, session=sessao)
    while not estado.conectado and time.monotonic() < limite:
        # Estados terminais não voltam sozinhos: esperar por eles só queima o
        # orçamento do request e atrasa a mensagem que pede ação do usuário.
        if estado.detalhe in ("sem_pareamento", "recuperacao_pausada", "servico_fora"):
            break
        time.sleep(_WA_ESPERA_INTERVALO_S)
        whatsapp_client.invalidar_status(sessao)
        estado = estado_whatsapp(usuario, session=sessao)
    if estado.conectado:
        logger.info("WhatsApp voltou durante a espera do envio (sessão %s).", sessao)
    return estado


def esta_vivo(produto):
    """
    Estado da oferta no ML, em TRÊS valores (A1 — seleção não destrutiva):
      True  -> página 200 e sem texto de "pausado/esgotado".
      False -> CONFIRMADO morto (HTTP 404/410 ou texto de pausa/inexistente).
      None  -> DESCONHECIDO (timeout, erro de conexão, status estranho). NÃO apagar:
               uma instabilidade de rede não pode apagar um produto bom do banco.
    Só o chamador apaga, e somente quando recebe False.
    """
    from apps.scrapers.auxiliar import ua_aleatorio
    headers = {'User-Agent': ua_aleatorio()}
    termos_inativos = [
        "Anúncio pausado",
        "Este anúncio foi pausado",
        "Estoque indisponível",
        "Este item não está mais",
        "Página não encontrada",
    ]
    try:
        r = requests.get(produto.link_produto, headers=headers, timeout=5)
        if r.status_code in (404, 410):
            return False                      # confirmado: não existe mais
        if r.status_code != 200:
            return None                       # 5xx/redirect estranho -> incerto, mantém
        for termo in termos_inativos:
            if termo in r.text:
                return False                  # confirmado: pausado/esgotado
        return True
    except Exception:
        return None                           # timeout/conexão -> incerto, mantém

def _selecionar_item_legacy(macros_selecionadas=None, categorias_selecionadas=None,
                           limite_envio=1, horas_cooldown=24,
                           min_desconto_percent=15.0, termo=None,
                           marketplace=None, usuario=None, grupo_id=None):
    """
    Seleciona produtos da base usando a lógica de 'Roleta Viciada' (Weighted Random Choice).
    Leva em conta: O Desconto percentual, desconto absoluto, preço e novidade do cupom.
    Evita reenviar itens enviados há menos de `horas_cooldown`.
    `termo`: sub-nicho opcional — string com termos separados por vírgula; mantém só
    produtos cujo nome casa com ALGUM termo (ex: "aspirador robo, robot vacuum").
    """
    from django.db.models import Q
    from apps.scrapers.marketplaces.registry import get_marketplace

    # 1. Filtro inicial - tudo menos os cupons de RESGATE legados (origem='cupom'),
    # que não aplicam via link. Entram: 'oferta' (feed), 'busca' (termo), 'cupom_codigo'.
    qs = Produto.objects.exclude(origem="cupom")

    # Multi-tenant: pool COMPARTILHADO (owner=None, ex: ML) + itens PRIVADOS do usuário
    # (owner=usuario, ex: Amazon raspada com a conta dele). Sem usuario -> só o compartilhado.
    if usuario is not None:
        qs = qs.filter(Q(owner__isnull=True) | Q(owner=usuario))
    else:
        qs = qs.filter(owner__isnull=True)

    if marketplace:
        qs = qs.filter(marketplace=marketplace)
    if macros_selecionadas:
        qs = qs.filter(macro_categoria__in=macros_selecionadas)
    if categorias_selecionadas:
        qs = qs.filter(categoria__in=categorias_selecionadas)

    # Sub-nicho: filtra pelo nome (OR entre os termos)
    if termo:
        termos = [t.strip() for t in termo.split(",") if t.strip()]
        if termos:
            cond = Q()
            for t in termos:
                cond |= Q(nome__icontains=t)
            qs = qs.filter(cond)

    # 2. NUNCA repetir oferta: exclui produto já enviado alguma vez POR ESTE usuário.
    # (horas_cooldown ignorado de propósito — dedup é permanente, não janela.)
    # usuario=None (chamadas legadas) -> dedup global, como antes.
    hist = HistoricoEnvio.objects.all()
    if usuario is not None:
        hist = hist.filter(usuario=usuario)
    qs = qs.exclude(id__in=hist.values_list('produto_id', flat=True))

    # 3. Calcula economia e desconto (%) - Mantém apenas os válidos (> 10% e < 90%)
    # Desconto >= 90% indica dado corrompido (ex: cupom fixo maior que o preço do produto)
    produtos_elegiveis = qs.annotate(
        economia_rs=ExpressionWrapper(F('preco_sem_desconto') - F('preco_com_cupom'), output_field=FloatField()),
        desconto_percent=ExpressionWrapper(((F('preco_sem_desconto') - F('preco_com_cupom')) / F('preco_sem_desconto')) * 100, output_field=FloatField())
    ).filter(desconto_percent__gte=min_desconto_percent, desconto_percent__lt=90.0, preco_com_cupom__gt=0)

    # Buscamos cupons numa tacada só para checar 'data_criacao' depois
    campanhas_ids = list(produtos_elegiveis.values_list('campanha_id', flat=True))
    cupons_map = {c.campanha_id: c for c in Cupom.objects.filter(campanha_id__in=campanhas_ids)}

    opcoes_sorteio = []
    pesos_sorteio = []

    for prod in produtos_elegiveis:
        cupom = cupons_map.get(prod.campanha_id)

        # Descarta produto que não atinge o valor mínimo de compra do cupom. A base
        # é o preço de VITRINE (`preco_com_cupom`), que é o valor que entra no
        # carrinho — comparar contra o preço de tabela deixava passar item barato.
        if cupom and cupom.valor_minimo > 0 and prod.preco_com_cupom < cupom.valor_minimo:
            continue

        # PONTUAÇÃO BASE: O peso foca bastante no Desconto Percentual
        score = prod.desconto_percent * 2.0

        # BÔNUS ECONOMIA (R$): Ajuda produtos caros com bom desconto em R$
        score += (prod.economia_rs / 20.0)

        # BÔNUS TICKET BAIXO: Produtos baratos (<R$30) recebem mais chance
        if prod.preco_com_cupom < 30.0:
            score += 20.0

        # BÔNUS URGÊNCIA: Cupom novo (criado nas últimas 12h) recebe Boost de 50%
        if cupom and cupom.data_criacao >= timezone.now() - timedelta(hours=12):
            score *= 1.5

        # B1 — HISTÓRICO DE PREÇOS: o "de/por" do ML é frequentemente inflado.
        # Com histórico suficiente (>=3 pontos/30d), comparamos com o próprio preço
        # típico do item: preço "de sempre" -> desconto fictício, NÃO anuncia;
        # perto da mínima de 30 dias -> queda REAL, ganha boost forte.
        h = _stats_preco(prod, dias=30)
        if h and h["n"] >= 3:
            if prod.preco_com_cupom >= h["mediana"] * 0.98:
                continue  # não é oferta de verdade vs. o histórico do item
            if prod.preco_com_cupom <= h["minimo"] * 1.02:
                score *= 1.6  # perto da mínima histórica — oferta genuína

        opcoes_sorteio.append(prod)
        pesos_sorteio.append(score)

    if not opcoes_sorteio:
        return []

    # 4. Sorteio (A 'Roleta Viciada') com VALIDAÇÃO Just-in-Time!
    vencedores = []
    tentativas = 0
    max_tentativas = limite_envio * 10 # proteção contra loop infinito

    while len(vencedores) < limite_envio and opcoes_sorteio and tentativas < max_tentativas:
        tentativas += 1
        escolhido = random.choices(population=opcoes_sorteio, weights=pesos_sorteio, k=1)[0]

        # Checa o estado do anúncio (tri-state, A1) pela loja do produto:
        # ML faz GET na PDP; Amazon usa getItems. Agnóstico de marketplace.
        estado = get_marketplace(getattr(escolhido, "marketplace", "mercadolivre")).is_alive(escolhido)
        if estado is True:
            vencedores.append(escolhido)
        elif estado is False:
            # CONFIRMADO morto (404/pausado): aí sim pode limpar o banco.
            logger.info("Oferta morta confirmada; removendo produto id=%s", escolhido.id)
            escolhido.delete()
        else:
            # None = incerto (timeout/erro). NÃO apaga; só pula nesta rodada.
            logger.info("Estado incerto para produto id=%s; mantendo no banco", escolhido.id)

        # Retira o escolhido da lista de sorteio atual
        idx = opcoes_sorteio.index(escolhido)
        opcoes_sorteio.pop(idx)
        pesos_sorteio.pop(idx)

    # NÃO grava HistoricoEnvio aqui! Só após o envio bem-sucedido (ver
    # management/commands/enviar_oferta.py). Gravar antes congelaria o produto
    # no cooldown mesmo se o link/envio falhasse.
    return vencedores


def pool_de_produtos_elegiveis(*, macros_selecionadas=None,
                               categorias_selecionadas=None,
                               min_desconto_percent=15.0, termo=None,
                               marketplace=None, usuario=None, teto=None):
    """Pool bruto de produtos elegíveis: sem pontuação, sem rede, sem cooldown.

    Extraído de `selecionar_item_para_grupo` para que a camada Deal
    (`apps.scrapers.deals`) parta EXATAMENTE do mesmo conjunto que o envio de
    produto sempre usou. Dois pools diferentes fariam a prévia e o envio
    discordarem sobre o vencedor — que é o defeito que a camada Deal existe para
    não repetir, não para reproduzir em outro lugar.

    Devolve objetos já anotados com `economia_rs` e `desconto_percent` e
    deduplicados por identidade de produto.
    """
    base = Produto.objects.exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"])
    from apps.scrapers.maintenance import produtos_frescos_q
    base = base.filter(produtos_frescos_q())
    base = base.filter(Q(owner__isnull=True) | Q(owner=usuario)) if usuario else base.filter(
        owner__isnull=True)
    if marketplace:
        base = base.filter(marketplace=marketplace)
    if macros_selecionadas:
        base = base.filter(macro_categoria__in=macros_selecionadas)
    if categorias_selecionadas:
        base = base.filter(categoria__in=categorias_selecionadas)
    if termo:
        cond = Q()
        for palavra in [p.strip() for p in termo.split(",") if p.strip()]:
            cond |= Q(nome__icontains=palavra)
        if cond:
            base = base.filter(cond)
    # `origem="cupom"` fica fora da via de preço puro: sem cupom na conta, uma linha
    # dessas não tem desconto de vitrine para exibir. A fatia de cupom mais abaixo
    # a readmite, porque lá o desconto vem justamente do cupom.
    qs = base.exclude(origem="cupom")

    elegiveis_qs = qs.annotate(
        economia_rs=ExpressionWrapper(
            F("preco_sem_desconto") - F("preco_com_cupom"),
            output_field=FloatField()),
        desconto_percent=ExpressionWrapper(
            (F("preco_sem_desconto") - F("preco_com_cupom")) * 100.0
            / F("preco_sem_desconto"), output_field=FloatField()),
    ).filter(
        # Piso ampliado de propósito. O corte final não é mais só o percentual
        # aparente: item no fundo do próprio histórico é oferta mesmo com pouco
        # desconto de vitrine, e era descartado aqui antes de ser avaliado. Quem
        # decide agora é `_passa_no_minimo`, dentro do laço. O piso continua
        # existindo para não trazer o catálogo inteiro à memória.
        desconto_percent__gte=min(PISO_DESCONTO_BRUTO, min_desconto_percent),
        desconto_percent__lt=90, preco_com_cupom__gt=0,
    )
    from apps.scrapers.product_identity import deduplicar_por_produto
    # Teto de candidatos. Baixar o piso de desconto no SQL (para deixar entrar item
    # barato com pouca vitrine) aumenta o conjunto que vem para a memória, e cada
    # candidato custa uma consulta de histórico no laço abaixo. Sem teto, o custo do
    # tick cresce com o catálogo — numa VM cuja cota de CPU já é o gargalo.
    # A ordem é por observação mais recente, então o corte tira o mais velho, que é
    # também o mais provável de já ter sido enviado ou de ter preço vencido.
    limite = teto or TETO_CANDIDATOS
    recentes = list(elegiveis_qs.order_by("-ultima_observacao", "-id")[:limite])

    # Fatia reservada para quem TEM cupom provado neste item.
    #
    # Ordenar só por observação recente parecia neutro e não era: produto com par
    # confirmado costuma ser mais velho no catálogo, então caía fora do teto antes
    # de ser avaliado. Medido em 04/09/2026 — 3.183 produtos com par confirmado e
    # cupom ativo, pool de 400, interseção de 20. O sistema tinha a matéria-prima
    # do seu próprio produto e a cortava por critério de recência.
    #
    # A unidade que este produto publica é produto + cupom que se aplica a ele
    # (ver o topo de `deals.py`); um candidato com par confirmado é o mais valioso
    # que existe, não o mais descartável. Ele continua passando por TODOS os
    # portões — frescor, preço medido, corroboração, benefício mínimo. O que muda
    # é só ter a chance de ser avaliado.
    #
    # O custo é limitado por teto próprio, e não some dentro do outro: se dividisse
    # o mesmo teto, encher a fatia de cupom esvaziaria a de preço puro.
    #
    # E ela NÃO herda o piso de desconto de vitrine, porque esse piso mede a coisa
    # errada para este caso. Medido em 04/09/2026: dos 226 produtos com par
    # confirmado, cupom ativo e ficha completa — preço, preço de lista, imagem,
    # link —, exemplos reais são "de R$ 76,95 por R$ 76,95" e "de R$ 229,57 por
    # R$ 229,57". Vitrine sem desconto nenhum: o abatimento deles É o cupom. Exigir
    # 15% de vitrine antes de olhar o cupom elimina por construção exatamente o
    # deal que este produto existe para publicar — é a mesma inversão que o topo de
    # `deals.py` descreve, "a qualidade do negócio era medida sem o cupom dentro da
    # conta", sobrevivendo um andar acima, no pool.
    #
    # Nada é afrouxado no que importa: benefício mínimo NESTE item, corroboração,
    # `_passa_no_minimo`, frescor e medição de preço no envio continuam valendo, e
    # são eles que protegem a verdade da mensagem. O piso de vitrine só protegia
    # custo de CPU, e para isso já existe o teto da fatia.
    ids_recentes = {p.pk for p in recentes}
    com_cupom = list(
        base.filter(
            cupons_normalizados__status="confirmado",
            cupons_normalizados__cupom__estado="ativo",
            preco_com_cupom__gt=0,
        ).annotate(
            economia_rs=ExpressionWrapper(
                F("preco_sem_desconto") - F("preco_com_cupom"),
                output_field=FloatField()),
            desconto_percent=ExpressionWrapper(
                (F("preco_sem_desconto") - F("preco_com_cupom")) * 100.0
                / F("preco_sem_desconto"), output_field=FloatField()),
        ).filter(desconto_percent__lt=90)
        .exclude(pk__in=ids_recentes)
        .distinct()
        .order_by("-ultima_observacao", "-id")[:TETO_CANDIDATOS_COM_CUPOM]
    )
    return deduplicar_por_produto(recentes + com_cupom)


def selecionar_item_para_grupo(macros_selecionadas=None, categorias_selecionadas=None,
                               limite_envio=1, horas_cooldown=24,
                               min_desconto_percent=15.0, termo=None,
                               marketplace=None, usuario=None, grupo_id=None,
                               verificar=True):
    """Ranking determinístico, explicável e personalizado por desempenho.

    ``verificar=False`` só monta o shortlist. O pipeline v2 faz a confirmação
    just-in-time em ``enviar_oferta_de_produto``; verificar também aqui duplicava
    a mesma chamada externa e fazia um pool de oito itens consumir até oito
    timeouts antes de sequer tentar o primeiro envio.
    """
    from apps.scrapers.marketplaces.registry import get_marketplace

    elegiveis = pool_de_produtos_elegiveis(
        macros_selecionadas=macros_selecionadas,
        categorias_selecionadas=categorias_selecionadas,
        min_desconto_percent=min_desconto_percent, termo=termo,
        marketplace=marketplace, usuario=usuario,
    )
    historicos = _stats_preco_em_lote(elegiveis, dias=30)
    historicos_comprovacao = _stats_preco_em_lote(elegiveis, dias=90)
    cupons = {
        c.campanha_id: c for c in Cupom.objects.filter(
            campanha_id__in=[produto.campanha_id for produto in elegiveis],
            estado="ativo",
        ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now()))
    }
    recentes = {}
    if usuario and grupo_id:
        desde = timezone.now() - timedelta(hours=horas_cooldown)
        for pub in Publicacao.objects.filter(
            usuario=usuario, destino_id=grupo_id, produto__isnull=False,
        ).filter(
            Q(status="enviado", enviada_em__gte=desde)
            | Q(status="incerto", criada_em__gte=desde)
        ).order_by("produto_id", "-criada_em"):
            recentes.setdefault(pub.produto_id, pub.preco_final)
    desempenho = {}
    if usuario:
        for row in Publicacao.objects.filter(
            usuario=usuario, status="enviado"
        ).values("produto_id").annotate(
            posts=Count("id", distinct=True), clicks=Count("cliques")):
            desempenho[row["produto_id"]] = row

    opcoes = []
    for produto in elegiveis:
        cupom = cupons.get(produto.campanha_id)
        # Compra mínima do cupom vs. preço de vitrine (o que vai ao carrinho).
        if cupom and cupom.valor_minimo > produto.preco_com_cupom:
            continue
        anterior = recentes.get(produto.id)
        if anterior and produto.preco_com_cupom > anterior * .95:
            continue
        # O histórico sobe para cá porque agora ele decide ELEGIBILIDADE, não só
        # pontuação: item colado na própria mínima entra mesmo com desconto de
        # vitrine baixo, e antes era descartado no SQL sem chance de ser avaliado.
        historico = historicos.get(_chave_preco(produto))
        if not _passa_no_minimo(produto, produto.preco_com_cupom, historico,
                                min_desconto_percent):
            continue
        # O percentual só entra na nota quando NÓS já observamos o item mais caro.
        #
        # Antes a nota começava em `desconto_percent * 2`, e esse percentual vem do
        # preço de vitrine — que a docstring de `PrecoHistorico` classifica como
        # frequentemente fictício no ML. O efeito era que um "de" inflado COMPRAVA o
        # primeiro lugar: 80% inventado (nota 160) passava na frente de 28% real e
        # comprovado (nota 62), e era o inflado que ia para o grupo.
        #
        # Penalizar não resolvia: o número falso não tem teto, então qualquer fator
        # multiplicativo continua perdendo para um "de" grande o bastante. A regra
        # certa é não pontuar o que não foi verificado. Sem prova, a nota se apoia só
        # na economia em reais — que também vem do "de" mas é limitada pelo preço
        # real do item — e o desconto deixa de ser argumento.
        comprovado = _desconto_comprovado(
            produto, produto.preco_com_cupom,
            historico=historicos_comprovacao.get(_chave_preco(produto)),
        )
        if comprovado:
            score = produto.desconto_percent * 2 + produto.economia_rs / 20
            motivos = [f"{produto.desconto_percent:.0f}% de desconto comprovado"]
        else:
            score = produto.economia_rs / 20
            motivos = ["desconto ainda não comprovado pelo histórico"]
        if produto.confianca == "alta":
            score *= 1.15
            motivos.append("fonte de alta confiança")
        elif produto.confianca == "baixa":
            score *= .75
        if produto.preco_com_cupom < 30:
            score += 20
            motivos.append("ticket acessível")
        # Feedback da cliente: cupom e oferta relâmpago vendem muito mais.
        if cupom and cupom.data_criacao >= timezone.now() - timedelta(hours=12):
            score *= 1.5
            motivos.append("cupom recente")
        elif cupom or getattr(produto, "codigo_checkout", ""):
            score *= 1.2
            motivos.append("tem cupom")
        if getattr(produto, "relampago", False):
            score *= 1.4
            motivos.append("oferta relâmpago")
        if historico and historico["n"] >= 3:
            if produto.preco_com_cupom >= historico["mediana"] * .98:
                continue
            if produto.preco_com_cupom <= historico["minimo"] * 1.02:
                score *= 1.6
                motivos.append("mínima de 30 dias")
        elif _no_fundo_do_historico(produto, produto.preco_com_cupom, historico):
            # Poucas observações ainda não sustentam a mediana, mas já dizem que o
            # preço está no fundo do que vimos. Vale menos que a mínima confirmada
            # por 3+ pontos, e vale mais que um percentual de vitrine sem prova.
            score *= 1.25
            motivos.append("perto da mínima observada")
        produto.desconto_comprovado = comprovado
        perf = desempenho.get(produto.id)
        if perf and perf["posts"]:
            score += min(60, perf["clicks"] / perf["posts"] * 12)
            if perf["clicks"]:
                motivos.append(f"{perf['clicks']} clique(s) anteriores")
        produto.score_oferta = round(score, 2)
        produto.motivos_score = motivos
        opcoes.append(produto)

    opcoes.sort(key=lambda p: (-p.score_oferta, p.id))
    if not verificar:
        return opcoes[:limite_envio]

    escolhidos = []
    for produto in opcoes:
        if len(escolhidos) >= limite_envio:
            break
        estado = get_marketplace(produto.marketplace).is_alive(produto)
        campos = {"ultima_verificacao": timezone.now()}
        if estado is True:
            campos.update(estado="ativo", falha_verificacao="")
            escolhidos.append(produto)
        elif estado is False:
            campos.update(estado="indisponivel",
                          falha_verificacao="Oferta indisponível na verificação")
        else:
            campos["falha_verificacao"] = "Não foi possível confirmar a oferta"
        Produto.objects.filter(pk=produto.pk).update(**campos)
    return escolhidos


def _texto_ia_sem_formatacao(texto, limite=120):
    """Neutraliza marcação que possa existir em caches gerados anteriormente."""
    limpo = re.sub(r"[*_`~]+", "", str(texto or ""))
    limpo = re.sub(r"\s+", " ", limpo).strip().strip("\"'")
    return limpo[:limite].rstrip(" -–—,;|/")


def _salvar_cache_ia(produto, *, titulo="", nome_curto="", persistir=True):
    """Atualiza cache de IA sem deixar uma falha de escrita quebrar a transação.

    O catálogo compartilhado (``organization_id IS NULL``) é legível por qualquer
    tenant e gravável só em contexto de sistema — é o que a política ``tenant_update``
    diz: ``USING ((system) OR organization_id = <org>)``. Como o envio roda no
    contexto da organização, o UPDATE casava zero linhas e o Django levantava
    ``Produto.NotUpdated``; o except abaixo engolia, e o cache NUNCA era gravado.

    O tamanho do desperdício, medido em produção em 20/08/2026: 148 de 47.554
    produtos tinham ``nome_llm``. Ou seja, quase toda mensagem enviada pagava uma
    chamada nova ao modelo (~6-8s no log) para reescrever um título que o produto
    já tinha recebido antes.

    A correção NÃO afrouxa o RLS: abre o contexto de sistema só para gravar o
    cache do catálogo público, que é exatamente para isso que ele existe. Os
    workers do Procfile já rodam com ``TENANT_SYSTEM_PROCESS=1``. No processo web
    (envio manual pela tela) a role não é privilegiada, `system_context` recusa com
    PermissionDenied e o comportamento volta a ser o de hoje: a mensagem sai com o
    título em memória e ninguém quebra.
    """
    from contextlib import nullcontext

    from apps.accounts.tenant import in_system_context, system_context
    campos = []
    if titulo and titulo != (getattr(produto, "frase_llm", "") or ""):
        produto.frase_llm = titulo
        campos.append("frase_llm")
    if nome_curto and nome_curto != (getattr(produto, "nome_llm", "") or ""):
        produto.nome_llm = nome_curto
        campos.append("nome_llm")
    if (not campos or not persistir or not hasattr(produto, "save")
            or not getattr(produto, "pk", None)):
        return
    # Só o catálogo compartilhado precisa do contexto de sistema. Produto de uma
    # organização é gravável pelo tenant dono e não deve sair do escopo dele.
    compartilhado = (getattr(produto, "organization_id", None) is None
                     and not in_system_context())
    try:
        # `system_context` recusa role não privilegiada no __enter__; o except
        # abaixo já é o caminho de "não deu para gravar", igual a hoje.
        with system_context() if compartilhado else nullcontext():
            # Savepoint obrigatório: DatabaseError capturado dentro de um atomic
            # externo (RLS, timeout etc.) não pode contaminar o envio inteiro.
            with transaction.atomic():
                produto.save(update_fields=campos)
    except Exception:
        logger.warning("Cache de IA não foi persistido para o produto %s",
                       getattr(produto, "pk", "?"), exc_info=True)


def _conteudo_marketing(produto, *, persistir_cache=True):
    """Chamada e nome curto, com uma única ida à IA e cache por produto."""
    titulo_cache = _texto_ia_sem_formatacao(
        getattr(produto, "frase_llm", ""), 80
    )
    nome_cache = _texto_ia_sem_formatacao(
        getattr(produto, "nome_llm", ""), 70
    )
    nome_fallback = _nome_principal_produto(getattr(produto, "nome", ""))
    nome_longo = len(str(getattr(produto, "nome", "") or "").strip()) > 70
    if titulo_cache and (nome_cache or not nome_longo):
        return {"titulo": titulo_cache, "nome_curto": nome_cache or nome_fallback}

    from apps.scrapers.llm import gerar_conteudo
    # A IA cita o preço no texto: precisa receber o mesmo valor que a mensagem
    # publica, senão o título gerado contradiz a linha "POR".
    preco = preco_publicavel(produto)
    de = getattr(produto, "preco_sem_desconto", 0) or 0
    desconto = ((de - preco) / de) * 100 if preco and de and de > preco else None
    gerado = gerar_conteudo(
        getattr(produto, "nome", ""), timeout=10, preco=preco,
        desconto_percent=desconto,
        categoria=getattr(produto, "macro_categoria", "") or getattr(produto, "categoria", ""),
    )
    titulo = titulo_cache or gerado.get("titulo", "")
    nome_gerado = gerado.get("nome_curto", "")
    nome_curto = nome_cache or nome_gerado or nome_fallback
    # O fallback mecânico mantém a mensagem bonita quando a API oscila, mas não
    # ocupa o cache da IA: uma tentativa futura ainda poderá produzir nome melhor.
    _salvar_cache_ia(produto, titulo=titulo, nome_curto=nome_gerado,
                     persistir=persistir_cache)
    return {"titulo": titulo, "nome_curto": nome_curto}


def _frase_marketing(produto):
    """Compatibilidade com os chamadores que precisam apenas da chamada."""
    return _conteudo_marketing(produto)["titulo"]


def _preparar_conteudo_ia_cupom(itens):
    """Prepara uma chamada e resume em lote os nomes longos da colagem."""
    if not itens:
        return
    # A chamada do cupom acompanha o principal produto exibido na colagem.
    _conteudo_marketing(itens[0]["produto"], persistir_cache=False)

    pendentes = []
    for item in itens:
        produto = item["produto"]
        if getattr(produto, "nome_llm", ""):
            continue
        if len(str(getattr(produto, "nome", "") or "").strip()) > 70:
            pendentes.append(produto)
    if not pendentes:
        return

    from apps.scrapers.llm import gerar_nomes_curtos
    resumidos = gerar_nomes_curtos([produto.nome for produto in pendentes], timeout=10)
    for produto, nome_curto in zip(pendentes, resumidos):
        if nome_curto:
            _salvar_cache_ia(produto, nome_curto=nome_curto, persistir=False)


def _nome_loja(marketplace, cupom=None) -> str:
    """Nome de exibição da loja (espelha o rótulo da tela de Promoções)."""
    m = str(marketplace or "").strip().lower()
    if m in ("mercadolivre", "mercado livre", "meli"):
        return "Mercado Livre"
    if m == "awin":
        return str(getattr(cupom, "anunciante_nome", "") or "Awin")
    return str(marketplace or "Loja").title()


def _linha_validade_cupom(cupom) -> str:
    """Prazo reproduzível para a mensagem, sem fabricar escassez.

    Fontes que não informam o fim continuam sem chamada de urgência. Quando o
    prazo existe, ele é convertido para o fuso configurado do projeto para o
    leitor não precisar interpretar UTC.
    """
    validade = getattr(cupom, "validade", None) if cupom is not None else None
    if not validade:
        return ""
    try:
        local = timezone.localtime(validade)
    except (TypeError, ValueError):
        return ""
    return f"Válido até {local:%d/%m às %Hh%M}"


def _linha_checagem_cupom(cupom, itens=None) -> str:
    """Instante reproduzível da evidência, sem chamar descoberta de validação."""
    momentos = []
    for item in itens or ():
        momento = getattr(item.get("relacao"), "verificado_em", None)
        if momento:
            momentos.append(momento)
    momento = max(momentos) if momentos else getattr(cupom, "ultima_observacao", None)
    if not momento:
        return ""
    try:
        local = timezone.localtime(momento)
    except (TypeError, ValueError):
        return ""
    rotulo = "Aplicação checada" if momentos else "Fonte checada"
    return f"{rotulo} em {local:%d/%m às %Hh%M}"


def montar_mensagem_cupom(cupom, markup=None, link_afiliado=None,
                          escopo_override=None) -> str:
    """Monta o texto de divulgação de um cupom (CupomNormalizado) p/ envio manual.

    Usa o `Markup` do canal e os dados de `cupom.regras` (valor_desconto/discount_num,
    min_compra, desconto_max) quando existirem — só entra o que houver. Cupom não tem
    foto de produto: sai como mensagem de texto. Segue o modelo pedido:

        Novo cupom ⚡️ Mercado Livre

        🛒 15% DE DESCONTO acima de R$79 (limitado a R$60)
        🎟 Use o cupom TAMOJUNTO

        Abra a loja e aplique o cupom no checkout:

        ➡️ https://mercadolivre.com/sec/2J8HDRK
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    from apps.scrapers.coupon_rules import (
        codigo_publicavel, desconto_para_comprador, escopo_produtos_cupom,
        formatar_numero, regras_do_cupom,
    )
    # `escopo_override` chega já reescrito pela avaliação de IA em enviar_cupom
    # ("produtos de Glamour.div" -> "loja Glamour") — só quando ela rodou e
    # respondeu. Nunca inventa aqui: sem override, cai no texto cru de sempre.
    m = markup or WhatsAppMarkup()
    esc = m.escape
    regras = regras_do_cupom(cupom)
    loja = _nome_loja(getattr(cupom, "marketplace", ""), cupom=cupom)

    cabecalho = (
        f"Cupom relâmpago ⚡️ {esc(loja)}"
        if getattr(cupom, "relampago", False)
        else f"Cupom {esc(loja)}"
    )
    linhas = [m.bold(cabecalho), ""]

    # Linha do desconto: "🛒 15% DE DESCONTO acima de R$79 (limitado a R$60)"
    # Comissão Shopee não entra: não é abatimento na loja.
    numero_desconto = (
        formatar_numero(regras.get("valor_desconto"))
        if desconto_para_comprador(cupom) else ""
    )
    valor = ""
    if numero_desconto:
        valor = (f"{numero_desconto}%" if regras.get("tipo_desconto") == "porcentagem"
                 else f"R$ {numero_desconto}" if regras.get("tipo_desconto") == "fixo"
                 else numero_desconto)
    partes = []
    if valor:
        partes.append(f"{valor} DE DESCONTO")
    minimo_valor = regras.get("valor_minimo")
    minimo = formatar_numero(minimo_valor)
    if minimo and minimo_valor and minimo_valor > 0:
        partes.append(f"acima de R$ {minimo}")
    linha_desc = " ".join(partes).strip()
    desconto_max = formatar_numero(regras.get("desconto_maximo"))
    if desconto_max:
        limite = f"(limitado a R$ {desconto_max})"
        linha_desc = f"{linha_desc} {limite}".strip()
    if linha_desc:
        linhas.append(f"🛒 {m.bold(esc(linha_desc))}")

    escopo_produtos = escopo_override or escopo_produtos_cupom(cupom)
    if escopo_produtos:
        linhas.append(f"🏷️ {m.bold('Válido para:')} {esc(escopo_produtos)}")

    codigo = codigo_publicavel(cupom)
    if codigo:
        linhas.append(f"🎟 Use o cupom {m.bold(esc(codigo))}")
    else:
        linhas.append(f"🎟 {m.bold('Ative o cupom no link')}")

    if getattr(cupom, "restrito", False):
        condicao = str(regras.get("escopo") or "Consulte quem pode usar antes de comprar")
        # Se o único "restrito" é o conjunto de produtos, a linha acima já
        # informa a condição com mais clareza. Restrições de público/pagamento
        # continuam aparecendo obrigatoriamente aqui.
        if not (escopo_produtos and condicao.strip().casefold()
                == escopo_produtos.strip().casefold()):
            linhas.extend(["", f"⚠️ {m.bold('Condição:')} {esc(condicao[:220])}"])

    validade = _linha_validade_cupom(cupom)
    if validade:
        linhas.append(f"⏳ {m.bold(esc(validade))}")
    checagem = _linha_checagem_cupom(cupom)
    if checagem:
        linhas.append(f"🔎 {esc(checagem)}")

    link = str(link_afiliado or getattr(cupom, "link", "") or "").strip()
    if link:
        acao = (
            "Abra a loja e aplique o cupom no checkout:"
            if codigo else
            "Ative o cupom e veja os itens participantes:"
        )
        # Um único CTA, específico para o modo real de resgate. "Navegue na
        # página" não dizia o que fazer depois do clique e aumentava a chance de
        # a pessoa abandonar antes do checkout.
        linhas += ["", f"👉 {m.bold(esc(acao))}", f"➡️ {esc(link)}"]

    return "\n".join(linhas)


def _escopo_curto(texto: str, limite: int = 60) -> str:
    """Escopo em uma linha, do tamanho que cabe numa mensagem de WhatsApp.

    O escopo pode vir da fonte como frase inteira ("Válido em produtos das Lojas
    Oficiais participantes, exceto..."). No aviso em lote há vários cupons na mesma
    mensagem, e um parágrafo por cupom afogaria a lista. Corta na palavra, não no
    meio dela, e nunca inventa: se não couber, termina em reticências para que quem
    lê saiba que há condição além do que está escrito.
    """
    limpo = " ".join(str(texto or "").split())
    if len(limpo) <= limite:
        return limpo
    corte = limpo[:limite].rsplit(" ", 1)[0].rstrip(",;.")
    return f"{corte}…"


def _sigla_loja(marketplace) -> str:
    """Rótulo curto da loja no cabeçalho do aviso: 'ML', 'AMAZON', ..."""
    m = str(marketplace or "").strip().lower()
    if m in ("mercadolivre", "mercado livre", "meli"):
        return "ML"
    if m == "amazon":
        return "AMAZON"
    return _nome_loja(marketplace).upper()


def linha_desconto_cupom(cupom) -> str:
    """"20% OFF em R$79, limitado a R$60 OFF" — sem markup, só o texto.

    Formato pedido pela cliente (ver os modelos de mensagem). Cada parte só entra
    quando a fonte forneceu o número; sem valor de desconto devolve '' e o cupom
    fica de fora do aviso — anunciar "cupom BARATINHO" sem dizer o que ele abate é
    exatamente a mensagem que ninguém clica.

    Usa `_preco_br` e não `coupon_rules.formatar_numero`: este devolve '1499' onde a
    mensagem-modelo diz 'R$1.499'.
    """
    from apps.scrapers.coupon_rules import regras_do_cupom

    regras = regras_do_cupom(cupom)
    valor = regras.get("valor_desconto")
    if valor in (None, "", 0):
        return ""
    tipo = str(regras.get("tipo_desconto") or "").lower()
    if tipo == "porcentagem":
        partes = [f"{_preco_br(valor)}% OFF"]
    elif tipo == "fixo":
        partes = [f"R${_preco_br(valor)} OFF"]
    else:
        return ""
    minimo = regras.get("valor_minimo")
    if minimo not in (None, "", 0):
        partes.append(f"em R${_preco_br(minimo)}")
    linha = " ".join(partes)
    teto = regras.get("desconto_maximo")
    # O teto só informa quando é percentual: em desconto fixo ele repete o valor.
    if tipo == "porcentagem" and teto not in (None, "", 0):
        linha = f"{linha}, limitado a R${_preco_br(teto)} OFF"
    return linha


def montar_mensagem_aviso_cupons(cupons, marketplace, link="", markup=None) -> str:
    """Aviso de cupons novos, sem produto — o formato que a cliente enviou:

        🚨 *NOVOS CUPONS ML* 🚨

        ➡️ _20% OFF em R$79, limitado a R$60 OFF_
        🎟 cupom: *BARATINHO*

        ➡️ _R$300 OFF em R$1.499_
        🎟 cupom: *OFFMELIMAIS*

        Ative em algum produto do link
        🔗 https://...

    Diferente de `montar_mensagem_cupom_produtos`, aqui não há produto, preço nem
    colagem: a mensagem só divulga códigos. Por isso ela aceita apenas cupom com
    CÓDIGO DIGITÁVEL — sem produto na tela, "ative no link" não teria onde ser
    ativado, e o gate de associação cupom↔produto perde o sentido.

    Devolve '' quando nenhum cupom rende linha de desconto; quem chama trata isso
    como "nada a anunciar", não como falha.
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    from apps.scrapers.coupon_rules import codigo_publicavel

    m = markup or WhatsAppMarkup()
    esc = m.escape

    from apps.scrapers.coupon_rules import escopo_produtos_cupom, regras_do_cupom

    blocos = []
    for cupom in cupons or []:
        codigo = codigo_publicavel(cupom)
        desconto = linha_desconto_cupom(cupom)
        if not codigo or not desconto:
            continue
        bloco = [f"➡️ {m.italic(esc(desconto))}"]
        # A REGRA VAI JUNTO DO CÓDIGO. Um aviso lista vários cupons e cada um tem o
        # seu limite: "Tecnologia", "entregas Full", "Lojas Oficiais", "todo site".
        # Sem a linha, quem lê tenta o cupom de Tecnologia numa camiseta, não
        # funciona, e a culpa cai em quem publicou. A mensagem de cupom único já
        # trazia isso ("Válido para:"); o aviso em lote é que mostrava só o desconto.
        escopo = escopo_produtos_cupom(cupom) or str(
            regras_do_cupom(cupom).get("escopo") or ""
        ).strip()
        if escopo:
            bloco.append(f"🏷️ {m.italic(esc(_escopo_curto(escopo)))}")
        validade = _linha_validade_cupom(cupom)
        if validade:
            bloco.append(f"⏳ {m.italic(esc(validade))}")
        bloco.append(f"🎟 cupom: {m.bold(esc(codigo))}")
        blocos.append("\n".join(bloco))
    if not blocos:
        return ""

    titulo = ("NOVO CUPOM" if len(blocos) == 1 else "NOVOS CUPONS")
    linhas = [f"🚨 {m.bold(f'{titulo} {esc(_sigla_loja(marketplace))}')} 🚨", ""]
    linhas.append("\n\n".join(blocos))

    link = str(link or "").strip()
    if link:
        linhas.append("")
        # Só o ML precisa da instrução: lá o cupom é aplicado navegando pela vitrine
        # do afiliado. Na Amazon o link já leva à página onde o código é digitado.
        if _sigla_loja(marketplace) == "ML":
            linhas.append("Ative em algum produto do link")
        linhas.append(f"🔗 {esc(link)}")
    return "\n".join(linhas)


def _produto_para_cupom(cupom):
    """Fallback afiliavel comprovadamente compativel com o cupom."""
    from apps.scrapers.coupon_rules import regras_do_cupom
    from apps.scrapers.models import ProdutoCupom

    ativos = Produto.objects.exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"]
    ).filter(marketplace=getattr(cupom, "marketplace", "mercadolivre"))
    vinculo = (ProdutoCupom.objects.filter(
        cupom=cupom, status="confirmado", produto__in=ativos,
    ).select_related("produto").order_by("-verificado_em", "-produto__ultima_observacao").first())
    if vinculo:
        return vinculo.produto

    external_id = str(getattr(cupom, "external_id", "") or "")
    if external_id.startswith("campanha:"):
        produto = ativos.filter(campanha_id=external_id.split(":", 1)[1]).order_by(
            "-ultima_observacao").first()
        if produto:
            return produto

    from apps.scrapers.coupon_rules import site_wide_confiavel

    regras = regras_do_cupom(cupom)
    # Só cupom site inteiro NÃO desmentido pela própria fonte pode pegar um item
    # qualquer do catálogo como vitrine. Ver `site_wide_confiavel`.
    if site_wide_confiavel(cupom):
        minimo = regras.get("valor_minimo") or 0
        return ativos.filter(preco_sem_desconto__gte=minimo).order_by(
            "-ultima_observacao").first()
    return None


def _macro_do_cupom(cupom) -> str:
    """Macro-categoria temática do cupom p/ agrupar ofertas. '' se não reconhecer.

    Preferência: a `categoria` do cupom quando já é uma macro real; senão classifica
    o título (ex.: 'produtos de Anadi Ferramentas' → 'Ferramentas e Manutenção').
    """
    cat = (getattr(cupom, "categoria", "") or "").strip()
    if cat in _EMOJI_MACRO:
        return cat
    try:
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            classificar_cupom_por_titulo)
        macro = classificar_cupom_por_titulo(getattr(cupom, "titulo", "") or "")
        if macro in _EMOJI_MACRO:
            return macro
    except Exception:
        pass
    return ""


def produtos_do_cupom(cupom, limite=9, macro=None):
    """Produtos p/ a mensagem-colagem do cupom (multi-item), melhores por desconto.

    (1) Ligação real cupom→produto quando existir — vínculo `ProdutoCupom`
        confirmado > campanha (`external_id` "campanha:X") > cupom de site inteiro
        (`is_mar_aberto`). Hoje isso é raro em produção (produto não guarda campanha).
    Nao ha fallback por categoria: proximidade tematica nao prova que o codigo sera
    aceito no checkout.

    Só entra item com foto (a colagem precisa dela).
    """
    from apps.scrapers.coupon_rules import regras_do_cupom
    from apps.scrapers.models import ProdutoCupom

    mkt = getattr(cupom, "marketplace", "mercadolivre") or "mercadolivre"
    ativos = Produto.objects.exclude(
        estado__in=["indisponivel", "invalido", "expirado", "stale"]
    ).filter(marketplace=mkt).exclude(imagem_url="")

    def _por_desconto(qs):
        return qs.filter(preco_com_cupom__gt=0, preco_sem_desconto__gt=0).annotate(
            _desc=ExpressionWrapper(
                (F("preco_sem_desconto") - F("preco_com_cupom")) * 100.0
                / F("preco_sem_desconto"), output_field=FloatField()),
        ).filter(_desc__lt=90).order_by("-_desc")

    # (1) Ligação real, quando existir.
    conf_ids = list(ProdutoCupom.objects.filter(
        cupom=cupom, status="confirmado", produto__in=ativos,
    ).values_list("produto_id", flat=True))
    qs = None
    if conf_ids:
        qs = ativos.filter(id__in=conf_ids)
    else:
        external_id = str(getattr(cupom, "external_id", "") or "")
        if external_id.startswith("campanha:"):
            qs = ativos.filter(campanha_id=external_id.split(":", 1)[1])
        else:
            from apps.scrapers.coupon_rules import site_wide_confiavel
            if site_wide_confiavel(cupom):
                minimo = regras_do_cupom(cupom).get("valor_minimo") or 0
                qs = ativos.filter(preco_sem_desconto__gte=minimo)
    itens = list(_por_desconto(qs)[:limite]) if qs is not None else []
    if itens:
        return itens

    return []


def _motivo_navegador(texto: str, generico: str = "Não foi possível preparar o link afiliado.") -> str:
    """Mensagem de BrowserError que o usuário deve ler.

    Alguns BrowserError já são escritos para o usuário e dizem exatamente o que
    resolver — o principal é "Link Builder por navegador está desativado para esta
    organização", que em produção é o estado DEFAULT (as flags de automação nascem
    desligadas, ver core/settings.py). Substituí-los por um genérico — ou pior, por
    "Sessão expirada, reconecte" — mandava o usuário reconectar em looping uma
    sessão que estava perfeita. Falha técnica de navegador continua genérica: o
    traceback não é texto de tela.
    """
    texto = (texto or "").strip()
    return texto if texto.startswith("Link Builder") else generico


def _preparar_itens_cupom(cupom, usuario, relacoes, limite=9):
    """([{produto, link}], bloqueio) com link afiliado válido + foto p/ a colagem.

    Cada produto leva o PRÓPRIO link comissionado (como na imagem-modelo). Usa o
    cache em lote (`situacao_dos_links`) e, só p/ quem não tem, gera via Link
    Builder. Se a sessão do ML cair, para de tentar (evita N falhas lentas) e
    devolve o que houver mais o motivo.

    `bloqueio` é None ou um dict {mensagem, precisa_login_ml}. São dois casos
    diferentes que antes se confundiam: sessão do ML caída (o usuário reconecta e
    resolve) e Link Builder indisponível — tipicamente a feature flag
    ML_LINK_BUILDER_ENABLED desligada, que em produção é o DEFAULT. Mandar
    "Sessão expirada. Reconecte sua conta." para o segundo caso fazia o usuário
    reconectar em looping sobre uma sessão perfeita, sem nunca saber que o
    problema era uma variável de ambiente.
    `relacoes` vem do predicado de leitura de preparo fresco. O envio manual não
    chama preparar_cupom: esse caminho pode escrever no catálogo público e é
    reservado ao worker de preparação.
    """
    from apps.scrapers.coupon_links import (
        canonical_coupon_link, coupon_link_verified_and_fresh,
    )
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.models import LinkAfiliadoProdutoCupomUsuario
    from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError
    from apps.scrapers.auxiliar import BrowserError, SessaoExpirada

    if not relacoes:
        return [], False
    produtos = [r.produto for r in relacoes]
    mkt = str(getattr(cupom, "marketplace", "mercadolivre") or "mercadolivre").lower()
    mp = get_marketplace(mkt)
    def _links_relacao():
        return {
            row.relacao_id: row
            for row in LinkAfiliadoProdutoCupomUsuario.objects.filter(
                usuario=usuario, relacao_id__in=[r.pk for r in relacoes],
            )
        }
    situacao = _executar_orm(_links_relacao)

    itens, bloqueio = [], None
    relacao_por_produto = {r.produto_id: r for r in relacoes}
    for p in produtos:
        if len(itens) >= limite:
            break
        relation = relacao_por_produto[p.id]
        row = situacao.get(relation.pk)
        link = canonical_coupon_link(row) if coupon_link_verified_and_fresh(row) else ""
        if not link and bloqueio is None:
            try:
                info = mp.build_affiliate_link(
                    p, usuario=usuario,
                    activation_key=getattr(relation, "activation_key", ""),
                )
            except (LoginError, AuthError, SessaoExpirada) as exc:
                logger.warning("Sessão/navegador ao afiliar produto %s do cupom %s: %s",
                               getattr(p, "id", "?"), getattr(cupom, "pk", "?"), exc)
                bloqueio = {"mensagem": "Sessão do Mercado Livre expirada. "
                                        "Reconecte sua conta.",
                            "precisa_login_ml": True}
                info = None
            except BrowserError as exc:
                # Navegador/Link Builder fora — NÃO é sessão. Repassa o texto real
                # (ex.: "Link Builder por navegador está desativado para esta
                # organização"), que diz o que de fato precisa ser resolvido.
                logger.warning("Link Builder indisponível ao afiliar produto %s do "
                               "cupom %s: %s", getattr(p, "id", "?"),
                               getattr(cupom, "pk", "?"), exc)
                bloqueio = {"mensagem": str(exc), "precisa_login_ml": False}
                info = None
            except Exception as exc:
                logger.debug("Falha ao afiliar produto %s do cupom: %s",
                             getattr(p, "id", "?"), exc)
                info = None
            if info and info.get("link_afiliado") and info.get("afiliado_ok") is not False:
                link = info["link_afiliado"]
                try:
                    def _salvar_relacao():
                        LinkAfiliadoProdutoCupomUsuario.objects.update_or_create(
                            usuario=usuario, relacao=relation,
                            defaults={
                                "url_isca": info.get("url_isca", ""),
                                "link_afiliado": link, "estado": "pronto",
                                "verificado_ok": info.get("verificado_ok"),
                                "verificado_em": timezone.now()
                                if info.get("verificado_ok") is not None else None,
                                "url_canonica": info.get("url_canonica", "")
                                if info.get("verificado_ok") else "",
                                "verificacao_motivo": info.get("verificacao_motivo", ""),
                                "ultima_tentativa": timezone.now(),
                            },
                        )
                    _executar_orm(_salvar_relacao)
                except Exception:
                    pass
        if link:
            itens.append({"produto": p, "link": link,
                          "relacao": relacao_por_produto[p.id]})
    return itens, bloqueio


def montar_mensagem_cupom_produtos(cupom, itens, markup=None) -> str:
    """Mensagem de cupom no formato pedido pela cliente: cabeçalho + lista de produtos.

        *Cupom ⚡️ Mercado Livre*

        📖 Chama de Ferro | Capa dura
        🛒 De R$197,90 por R$83,54
        ➡️ https://meli.la/...

        🎟 Use o cupom *PRESENTE*

    Negrito APENAS no cabeçalho e no código do cupom (pedido explícito). Nome e
    preço de cada produto vão em texto puro. Cada produto leva o próprio link; a
    foto vai na colagem (imagem única acima da mensagem), via `montar_colagem_b64`.
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    from apps.scrapers.coupon_rules import codigo_publicavel
    m = markup or WhatsAppMarkup()
    esc = m.escape

    loja = _nome_loja(getattr(cupom, "marketplace", ""), cupom=cupom)
    linhas = []
    titulo_ia = (
        _texto_ia_sem_formatacao(
            getattr(itens[0]["produto"], "frase_llm", ""), 80
        )
        if itens else ""
    )
    if titulo_ia:
        # A chamada da IA é propositalmente texto puro; cabeçalho/código mantêm
        # o destaque próprio da mensagem de cupom.
        linhas += [esc(titulo_ia), ""]
    cabecalho = (
        f"Cupom relâmpago ⚡️ {esc(loja)}"
        if getattr(cupom, "relampago", False)
        else f"Cupom {esc(loja)}"
    )
    linhas += [m.bold(cabecalho), ""]
    for it in itens:
        p = it["produto"]
        relacao = it.get("relacao")
        # "De" é o preço de VITRINE (`preco_atual`), não o de tabela: assim a
        # diferença anunciada é exatamente o que o cupom abate no checkout. Com o
        # preço de tabela, a economia mostrada somava a promoção que o item já tinha
        # e o cliente não conseguia reproduzir o valor.
        # `is None` em vez de `or`: um preço legítimo de 0,00 caía no fallback.
        de_val = getattr(relacao, "preco_atual", None)
        if de_val is None:
            de_val = p.preco_com_cupom
        por_val = getattr(relacao, "preco_final", None)
        if por_val is None:
            por_val = p.preco_com_cupom
        nome = getattr(p, "nome_llm", "") or _nome_principal_produto(p.nome)
        linhas.append(f"{_emoji_produto(p)} {esc(_nome_principal_produto(nome))}")
        de = _preco_br(de_val)
        por = _preco_br(por_val)
        linhas.append(f"🛒 De R${de} por R${por}")
        linhas.append(f"➡️ {esc(it['link'])}")
        linhas.append("")

    # Linha do cupom no fim (mesmo formato do texto puro): só o código em negrito.
    codigo = codigo_publicavel(cupom)
    if codigo:
        linhas.append(f"🎟 Use o cupom {m.bold(esc(codigo))}")
        linhas.append("👉 Abra um produto acima e aplique o cupom no checkout.")
    else:
        # Os links de produto de campanhas ML carregam `coupon_campaign_id`; nas
        # demais lojas a mensagem ainda manda conferir o abatimento antes de
        # pagar, sem prometer que um simples clique validou o checkout.
        linhas.append(f"🎟 {m.bold('Cupom de ativação')}")
        linhas.append(
            "👉 Abra um produto acima, ative o cupom e confirme o desconto antes de pagar."
        )
    condicao = _condicao_do_cupom(cupom)
    if condicao:
        linhas.append(f"⚠️ {m.bold('Condição:')} {esc(condicao)}")
    validade = _linha_validade_cupom(cupom)
    if validade:
        linhas.append(f"⏳ {m.bold(esc(validade))}")
    checagem = _linha_checagem_cupom(cupom, itens)
    if checagem:
        linhas.append(f"🔎 {esc(checagem)}")
    return "\n".join(linhas).strip()


def _linha_prova_do_deal(deal) -> str:
    """Uma frase que o histórico SUSTENTA, ou nada.

    Não é adjetivo de vendedor: ou existe série de 90 dias que prova a posição do
    preço, ou a mensagem não afirma nada sobre "estar barato". Quando o cupom é
    perene, a referência é a vitrine — o abatimento dele já convive com a série e
    creditá-lo aqui faria todo item da loja virar "menor preço".
    """
    historico = getattr(deal, "historico", None) or {}
    # Cinco observações, não uma. O `pontuar` pode se apoiar no que houver — é um
    # número interno que só ordena. Isto aqui vira uma AFIRMAÇÃO pública assinada
    # pelo creator, e "menor preço em 90 dias" apoiado em duas leituras é a mesma
    # classe de erro que mandou "De R$ 289 por R$ 183,91" para o grupo.
    if int(historico.get("n") or 0) < 5:
        return ""
    referencia = deal.preco_vitrine if deal.cupom_perene else deal.preco_final
    minimo = float(historico.get("minimo") or 0)
    mediana = float(historico.get("mediana") or 0)
    if minimo > 0 and referencia <= minimo * FOLGA_MINIMA_HISTORICA:
        return "Menor preço que observamos em 90 dias"
    if mediana > 0 and referencia < mediana:
        queda = (mediana - referencia) / mediana * 100
        if queda >= 5:
            return f"{queda:.0f}% abaixo do preço habitual de 90 dias"
    return ""


def _fatos_do_deal(deal) -> dict:
    """Números e alegações que o modelo pode usar — e só eles.

    É a lista branca do `gerar_texto_deal`. O que sai daqui é exatamente o que a
    mensagem vai imprimir logo abaixo, então o texto vendedor e o bloco de preço
    nunca podem divergir. `provas` autoriza as afirmações fortes: "menor preço" só
    entra quando o histórico sustenta, urgência só quando a validade é curta.
    """
    lista = float(getattr(deal.produto, "preco_sem_desconto", 0) or 0)
    economia = lista - deal.preco_final if (
        deal.desconto_comprovado and lista > deal.preco_final) else None
    percentual = None
    if economia and lista > 0:
        percentual = round(economia / lista * 100)
    # "menor preço em 90 dias" saiu da mensagem por decisão do usuário — e sair
    # significa não aparecer TAMBÉM na frase da IA. Manter a prova aqui só mudava
    # o lugar onde a mesma afirmação era feita, que é o oposto de removê-la.
    provas = set()
    validade = getattr(deal.cupom, "validade", None)
    if validade and validade - timezone.now() <= timedelta(hours=12):
        provas.add("urgencia")
    return {
        "preco_final": deal.preco_final,
        "economia": economia,
        "beneficio_cupom": deal.beneficio_rs or None,
        "percentual": percentual,
        "provas": provas,
        # A janela do histórico é impressa pelo próprio código na linha de prova
        # ("Menor preço que observamos em 90 dias"). Sem liberá-la, o modelo
        # escrevia "em 90 dias" e o validador derrubava a frase inteira por um
        # número que a mensagem já mostra.
        "janela_dias": 90 if provas else None,
    }


def _frase_acrescenta(frase, nome, minimo_novas=3) -> str:
    """A frase da IA só entra se disser algo que o nome do produto não diz.

    Sem isto a mensagem repetia o título em prosa logo abaixo dele. A régua é
    grosseira de propósito: contar quantas palavras de conteúdo a frase traz que
    não estão no nome. Menos que três, ela é paráfrase e não vale a linha.
    """
    texto = _texto_ia_sem_formatacao(frase, 110)
    if not texto:
        return ""
    from apps.scrapers.models import normalizar_busca

    do_nome = {p for p in normalizar_busca(nome).split() if len(p) > 3}
    palavras = [p for p in normalizar_busca(texto).split() if len(p) > 3]
    if not palavras:
        return ""
    novas = [p for p in palavras if p not in do_nome]
    if len(novas) < minimo_novas:
        return ""
    # E, mesmo trazendo palavras novas, a frase não pode REPRODUZIR o título:
    # "Fone Bluetooth JBL para ouvir o dia inteiro" acrescenta três palavras e
    # ainda assim imprime o nome do produto pela segunda vez na mesma tela.
    if do_nome and len(do_nome & set(palavras)) / len(do_nome) > 0.5:
        return ""
    return texto


def montar_mensagem_deal(deal, link, markup=None, *, texto_ia=None, usuario=None,
                         configuracao=None) -> str:
    """Mensagem de um Deal, no formato que os canais de oferta usam de verdade.

    Anatomia copiada de quem vende (nerdofertas, promobit e afins no Telegram):

        ➡️ Nome do produto
        ✅ R$ 590  (de R$ 890)
        🏷 Cupom: TEMNAAMZON
        🛒 link

    O NOME APARECE UMA VEZ. A versão anterior tinha gancho, nome e frase da IA
    dizendo a mesma coisa em sequência — "ASPIRADOR PHILCO PAS4000V POR R$ 220,91"
    / "Philco PAS4000V aspirador de pó 127 V" / "Aspirador de pó Philco 127 V com
    42% de desconto". Três linhas, uma informação. Some o gancho: a linha do
    produto já é a chamada, como nos canais que convertem.

    A frase da IA é opcional e só entra quando acrescenta algo que o nome não diz;
    `_frase_acrescenta` derruba a que só repete o título.

    A foto do produto vai acima, pelo caminho de envio de produto.
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    from apps.scrapers.coupon_rules import codigo_publicavel

    m = markup or WhatsAppMarkup()
    esc = m.escape
    texto_ia = texto_ia or {}
    produto = deal.produto
    perfil = getattr(usuario, "perfil", None) if usuario else None

    linhas = []
    if getattr(produto, "relampago", False) or getattr(deal.cupom, "relampago", False):
        linhas.append(m.bold("⚡ RELÂMPAGO"))

    nome = (getattr(produto, "nome_llm", "") or "").strip() or (
        _nome_principal_produto(getattr(produto, "nome", ""), limite=72))
    linhas.append(f"➡️ {m.bold(esc(nome))}")

    frase = _frase_acrescenta(texto_ia.get("linha") or "", nome)
    if frase:
        linhas.append(esc(frase))

    # Frete grátis é argumento de compra, não detalhe: os canais que convertem
    # (Pechinchou e afins) põem essa linha antes do preço. O dado já existia em
    # `Produto.frete_full` e a mensagem nunca o usava.
    if getattr(produto, "frete_full", False):
        linhas.append("📦 Frete grátis")

    # Preço: uma linha. O "de" só com desconto comprovado pelo nosso histórico —
    # riscar um preço que talvez nunca tenha existido é o falso positivo mais caro
    # do produto, porque quem assina a mensagem é o creator.
    lista = float(getattr(produto, "preco_sem_desconto", 0) or 0)
    preco = m.bold(f"R$ {_preco_br(deal.preco_final)}")
    if deal.desconto_comprovado and lista > deal.preco_final:
        # "(de R$ 1.399)" e não "chega a custar R$ 1.399": a segunda lê como se o
        # preço fosse SUBIR, e é a forma que todo canal de oferta usa.
        linhas.append(f"🔥 {preco}  (de R$ {_preco_br(lista)})")
    else:
        linhas.append(f"🔥 {preco}")
    prova = _linha_prova_do_deal(deal)
    if prova:
        linhas.append(f"📉 {esc(prova)}")
    loja = _nome_loja(getattr(produto, "marketplace", ""))
    if loja:
        linhas.append(f"🏬 Achado no {esc(loja)}")

    if deal.tem_cupom:
        codigo = codigo_publicavel(deal.cupom)
        abate = (f" — abate R$ {_preco_br(deal.beneficio_rs)}"
                 if deal.beneficio_rs > 0 else "")
        if codigo:
            linhas.append(f"🏷 Cupom: {m.bold(esc(codigo))}{abate}")
        else:
            linhas.append(f"🏷 {m.bold('Cupom de ativação')}{abate} — ative na página")
        minimo = _aviso_minimo_nao_atingido(deal.cupom, produto)
        if minimo:
            linhas.append(f"⚠️ {esc(minimo.capitalize())}")
        validade = _linha_validade_cupom(deal.cupom)
        if validade:
            linhas.append(f"⏳ {esc(validade)}")

    linhas.append(f"🛒 {esc(link)}")
    disclosure = (
        getattr(configuracao, "divulgacao_afiliado", "")
        or getattr(perfil, "divulgacao_afiliado", "") or ""
    ).strip()
    if disclosure:
        linhas.append(esc(disclosure))
    return "\n".join(linhas).strip()


def resolver_link_afiliado_cupom(cupom, usuario):
    """Gera link comissionado direto; cai para produto confirmado quando preciso."""
    from apps.scrapers.models import LinkAfiliadoCupomUsuario
    from apps.scrapers.marketplaces.registry import get_marketplace

    if usuario is None:
        return {"sucesso": False, "motivo": "Usuário ausente para gerar o link afiliado."}
    if getattr(cupom, "owner_id", None) and cupom.owner_id != usuario.id:
        return {"sucesso": False, "motivo": "Este cupom pertence a outra conta."}
    marketplace = str(getattr(cupom, "marketplace", "") or "").strip().lower()
    origem = str(getattr(cupom, "link", "") or "").strip()
    if marketplace == "awin":
        def _vinculos_awin():
            return (getattr(cupom, "integracao", None),
                    getattr(cupom, "programa", None))
        integracao, programa = _executar_orm(_vinculos_awin)
        if (integracao and integracao.owner_id == usuario.id
                and integracao.habilitada and integracao.status == "conectada"
                and programa and programa.habilitado and programa.status_vinculo == "joined"
                and programa.link_status == "online" and origem.startswith(("http://", "https://"))):
            return {"sucesso": True, "link": origem, "cache": True}
        return {"sucesso": False, "motivo": "A conta ou o anunciante Awin não está ativo."}
    if marketplace == "amazon":
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
        from apps.scrapers.afiliado import tag_amazon
        from apps.scrapers.scraper_amazon.link import gerar_link_afiliado_cupom
        tag = _executar_orm(tag_amazon, usuario)
        if not tag:
            return {"sucesso": False, "motivo": "Cadastre sua tag Amazon para usar este cupom."}
        if origem.startswith("https://"):
            parts = urlsplit(origem)
            hostname = (parts.hostname or "").lower()
            if not (hostname == "amazon.com.br" or hostname.endswith(".amazon.com.br")):
                return {"sucesso": False, "motivo": "O link informado não pertence à Amazon Brasil."}
            query = dict(parse_qsl(parts.query, keep_blank_values=True))
            query["tag"] = tag
            return {"sucesso": True,
                    "link": urlunsplit((parts.scheme, parts.netloc, parts.path,
                                         urlencode(query), parts.fragment))}
        destino = _executar_orm(gerar_link_afiliado_cupom, cupom, usuario)
        if str(destino or "").startswith("https://"):
            return {"sucesso": True, "link": destino}
        return {"sucesso": False, "motivo": "Cadastre sua tag Amazon para usar este cupom."}
    if marketplace == "shopee":
        from urllib.parse import urlsplit
        from apps.scrapers.coupon_links import (
            canonical_coupon_link, coupon_link_verified_and_fresh,
        )
        from apps.scrapers.shopee import (
            ShopeeError, credenciais_da_integracao, gerar_link,
        )

        def _estado_shopee():
            from apps.scrapers.models import IntegracaoAfiliado
            integracao = IntegracaoAfiliado.objects.filter(
                owner=usuario, provedor="shopee", habilitada=True,
                status="conectada",
            ).first()
            cache = LinkAfiliadoCupomUsuario.objects.filter(
                usuario=usuario, cupom=cupom,
            ).first()
            return integracao, cache

        integracao, cache = _executar_orm(_estado_shopee)
        marketplace_adapter = get_marketplace("shopee")
        if cache and coupon_link_verified_and_fresh(cache) \
                and marketplace_adapter.verify_affiliate_tag(
                    cache.link_afiliado, usuario=usuario,
                ):
            return {
                "sucesso": True, "link": canonical_coupon_link(cache),
                "cache": True,
            }
        if not integracao:
            return {
                "sucesso": False,
                "motivo": "Conecte sua conta de afiliado da Shopee para usar este cupom.",
                "precisa_integracao_shopee": True,
            }
        destino = origem or "https://shopee.com.br/m/cupom-de-desconto"
        try:
            parts = urlsplit(destino)
            hostname = (parts.hostname or "").casefold()
        except ValueError:
            hostname = ""
        if not (
            destino.startswith("https://")
            and (hostname == "shopee.com.br" or hostname.endswith(".shopee.com.br"))
        ):
            return {"sucesso": False, "motivo": "Destino Shopee invalido para afiliacao."}
        try:
            app_id, secret = _executar_orm(credenciais_da_integracao, integracao)
            link = gerar_link(
                destino, app_id=app_id, secret=secret,
                sub_ids=[f"u{usuario.pk}", "spreading", f"c{cupom.pk}"],
            )
        except ShopeeError as exc:
            return {"sucesso": False, "motivo": exc.public_message}
        if not marketplace_adapter.verify_affiliate_tag(link, usuario=usuario):
            return {"sucesso": False,
                    "motivo": "A Shopee nao confirmou a atribuicao do link."}

        def _gravar_shopee():
            from apps.accounts.models import organization_for_user
            LinkAfiliadoCupomUsuario.objects.update_or_create(
                usuario=usuario, cupom=cupom,
                defaults={
                    "organization": organization_for_user(usuario),
                    "url_origem": destino, "link_afiliado": link,
                    "afiliado_ok": True, "estado": "pronto",
                    "verificado_ok": True, "verificado_em": timezone.now(),
                    "url_canonica": link, "verificacao_motivo": "",
                    "ultimo_erro": "", "ultima_tentativa": timezone.now(),
                    "proxima_tentativa": None,
                },
            )

        _executar_orm(_gravar_shopee)
        return {"sucesso": True, "link": link, "cache": False}
    if marketplace != "mercadolivre":
        return {"sucesso": False,
                "motivo": "Esta loja ainda não oferece link afiliado para cupons."}
    def _cache_do_par():
        return LinkAfiliadoCupomUsuario.objects.filter(
            usuario=usuario, cupom=cupom,
        ).first()
    cache = _executar_orm(_cache_do_par)

    def _registrar_falha_cache(reason, *, state="erro", retry_minutes=30):
        def _save():
            current = LinkAfiliadoCupomUsuario.objects.filter(
                usuario=usuario, cupom=cupom,
            ).first()
            defaults = {
                "url_origem": (getattr(current, "url_origem", "") or origem),
                "link_afiliado": getattr(current, "link_afiliado", "") or "",
                "afiliado_ok": False, "estado": state,
                "verificado_ok": False if getattr(current, "link_afiliado", "") else None,
                "verificacao_motivo": str(reason or "")[:300],
                "ultimo_erro": str(reason or "")[:300],
                "ultima_tentativa": timezone.now(),
                "proxima_tentativa": timezone.now() + timedelta(minutes=retry_minutes),
                "tentativas": (getattr(current, "tentativas", 0) or 0) + 1,
            }
            LinkAfiliadoCupomUsuario.objects.update_or_create(
                usuario=usuario, cupom=cupom, defaults=defaults,
            )
        _executar_orm(_save)
    # O cache pertence ao par usuario+cupom. A URL de origem pode ser a pagina
    # do cupom ou um produto fallback comprovado; em ambos os casos o link salvo
    # já passou pela verificacao de comissionamento.
    if cache and cache.link_afiliado:
        from apps.scrapers.coupon_links import (
            canonical_coupon_link, coupon_link_verified_and_fresh,
        )
        if coupon_link_verified_and_fresh(cache):
            return {"sucesso": True, "link": canonical_coupon_link(cache), "cache": True}
        # Reverificar um link existente não abre navegador. Isto permite promover
        # caches migrados/expirados mesmo quando a sessão do Link Builder caiu.
        if get_marketplace(marketplace).verify_affiliate_tag(
                cache.link_afiliado, usuario=usuario):
            def _aprovar_cache():
                LinkAfiliadoCupomUsuario.objects.filter(pk=cache.pk).update(
                    estado="pronto", afiliado_ok=True, verificado_ok=True,
                    verificado_em=timezone.now(), url_canonica=cache.link_afiliado,
                    verificacao_motivo="", ultimo_erro="", proxima_tentativa=None,
                    ultima_tentativa=timezone.now(), tentativas=F("tentativas") + 1,
                )
            _executar_orm(_aprovar_cache)
            return {"sucesso": True, "link": cache.link_afiliado,
                    "cache": True, "reverified": True}
        _registrar_falha_cache(
            "A atribuição do link afiliado não pôde ser confirmada.",
            state="pendente", retry_minutes=15,
        )

    def _gravar_cache(url_origem, link):
        saved, created = LinkAfiliadoCupomUsuario.objects.update_or_create(
            usuario=usuario, cupom=cupom,
            defaults={"url_origem": url_origem, "link_afiliado": link,
                      "afiliado_ok": True, "estado": "pronto",
                      "verificado_ok": True, "verificado_em": timezone.now(),
                      "url_canonica": link, "verificacao_motivo": "",
                      "ultimo_erro": "", "ultima_tentativa": timezone.now(),
                      "proxima_tentativa": None},
        )
        LinkAfiliadoCupomUsuario.objects.filter(pk=saved.pk).update(
            tentativas=1 if created else F("tentativas") + 1,
        )

    erro_direto = ""
    if origem:
        try:
            from apps.scrapers.scraper_mercadolivre.link import afiliate_link_builder
            link = afiliate_link_builder(origem, usuario=usuario)
            if link and get_marketplace(marketplace).verify_affiliate_tag(
                    link, usuario=usuario):
                _executar_orm(_gravar_cache, origem, link)
                return {"sucesso": True, "link": link, "cache": False}
            erro_direto = "A página do cupom não foi aceita pelo programa de afiliados."
        except Exception as exc:
            from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError
            from apps.scrapers.auxiliar import SessaoExpirada
            if isinstance(exc, AuthError):
                _registrar_falha_cache(
                    "Link Builder temporariamente indisponível.",
                    state="pendente", retry_minutes=5,
                )
                logger.info(
                    "Link Builder temporariamente indisponível ao afiliar cupom %s: %s",
                    cupom.pk, exc,
                )
                return {
                    "sucesso": False,
                    "motivo": "Link Builder temporariamente indisponível; nova tentativa agendada.",
                    "precisa_login_ml": False,
                    "indisponivel_ml": True,
                }
            if isinstance(exc, (LoginError, SessaoExpirada)):
                _registrar_falha_cache(
                    "Sessão necessária para criar ou renovar o link afiliado.",
                    state="pendente", retry_minutes=15,
                )
                logger.warning("Sessão ML expirada ao afiliar cupom %s: %s", cupom.pk, exc)
                return {"sucesso": False,
                        "motivo": "Sessão do Mercado Livre expirada. Reconecte sua conta.",
                        "precisa_login_ml": True}
            logger.warning("Falha ao afiliar pagina do cupom %s: %s", cupom.pk, exc)
            erro_direto = "Não foi possível gerar o link afiliado da página do cupom."

    produto = _executar_orm(_produto_para_cupom, cupom)
    if produto:
        mp = get_marketplace(marketplace)
        try:
            def _relacao_fallback():
                return cupom.produtos.filter(
                    produto=produto, status="confirmado",
                ).only("activation_key").first()
            relation = _executar_orm(_relacao_fallback)
            info = mp.build_affiliate_link(
                produto, usuario=usuario,
                activation_key=getattr(relation, "activation_key", ""),
            )
        except Exception as exc:
            from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError
            from apps.scrapers.auxiliar import SessaoExpirada
            if isinstance(exc, AuthError):
                _registrar_falha_cache(
                    "Link Builder temporariamente indisponível.",
                    state="pendente", retry_minutes=5,
                )
                logger.info(
                    "Link Builder temporariamente indisponível no fallback do cupom %s: %s",
                    cupom.pk, exc,
                )
                return {
                    "sucesso": False,
                    "motivo": "Link Builder temporariamente indisponível; nova tentativa agendada.",
                    "precisa_login_ml": False,
                    "indisponivel_ml": True,
                }
            if isinstance(exc, (LoginError, SessaoExpirada)):
                _registrar_falha_cache(
                    "Sessão necessária para criar ou renovar o link afiliado.",
                    state="pendente", retry_minutes=15,
                )
                logger.warning("Sessão ML expirada no fallback do cupom %s: %s", cupom.pk, exc)
                return {"sucesso": False,
                        "motivo": "Sessão do Mercado Livre expirada. Reconecte sua conta.",
                        "precisa_login_ml": True}
            logger.warning("Falha ao afiliar produto fallback do cupom %s: %s", cupom.pk, exc)
            info = None
        if info and info.get("link_afiliado"):
            link = info["link_afiliado"]
            if info.get("afiliado_ok") is not False:
                _executar_orm(_gravar_cache, produto.link_produto, link)
                return {"sucesso": True, "link": link, "produto": produto}

    failure = erro_direto or (
        "Nenhum produto aplicável permitiu gerar um link afiliado para este cupom."
    )
    _registrar_falha_cache(failure)
    return {"sucesso": False, "motivo": failure}


def enviar_cupom(cupom, grupo_id, *, canal="whatsapp", usuario=None, destino_nome="",
                 imagem_b64_custom=None, configuracao=None, score=0, motivos_score=None,
                 enqueue_only=False, _reserved_publication=None):
    """Nucleo auditavel do envio manual de CupomNormalizado.

    `imagem_b64_custom` (opcional): foto escolhida no envio. Cupom não tem foto de
    produto, então sem ela sai como texto puro (comportamento de sempre); com ela,
    a foto vira a imagem acima da mensagem (só no transporte base64/WhatsApp)."""
    from django.contrib.auth import get_user_model
    from apps.scrapers.coupon_rules import codigo_publicavel
    from apps.scrapers.eventos import log_event
    from apps.scrapers.senders.registry import get_sender

    try:
        sender = get_sender(canal)
    except ValueError as exc:
        return {"sucesso": False, "motivo": str(exc), "classe": "permanente"}
    if not usuario or not grupo_id:
        return {"sucesso": False, "motivo": "Usuário ou destino ausente.",
                "classe": "permanente"}
    # Pré-checa a conexão do canal ANTES de criar a Publicacao ou preparar a
    # mensagem: sem WhatsApp conectado nada sai, e o usuário precisa reconectar.
    erro_canal = None if enqueue_only else _canal_pronto_ou_erro(canal, usuario)
    if erro_canal:
        return erro_canal
    agora = timezone.now()
    cupom_id = getattr(cupom, "pk", None)
    if not cupom_id:
        return {"sucesso": False,
                "motivo": "Este cupom foi atualizado e não está mais disponível. Atualize a tela e tente outro.",
                "classe": "permanente", "cupom_atualizado": True}
    if cupom.estado != "ativo" or (cupom.validade and cupom.validade < agora):
        return {"sucesso": False, "motivo": "Cupom não encontrado, inativo ou vencido.",
                "classe": "permanente"}

    # Cupom de código é um aviso de loja: não inventa produto nem associação. Ele
    # exige um destino afiliado válido, mas não passa pelo gate de ProdutoCupom que
    # pertence exclusivamente às ativações. A página pode mudar entre render e
    # clique, então ambos os modos são revalidados aqui.
    tem_codigo = bool(codigo_publicavel(cupom))
    from apps.scrapers.coupon_products import (
        relacoes_preparadas_para_envio, relacoes_prontas_para_envio,
    )
    relacoes_preparadas = _executar_orm(
        relacoes_preparadas_para_envio, cupom, usuario,
    )
    # Se uma fonte realmente comprovou produtos, preservamos a publicação rica com
    # colagem e preço por item. Sem essa prova, o código continua publicável apenas
    # como aviso de loja, que é o contrato seguro pedido para cupons digitáveis.
    modo_codigo = bool(tem_codigo and not relacoes_preparadas)
    modo_link_direto = False
    link_codigo = ""
    if modo_codigo and not enqueue_only:
        resolucao_codigo = resolver_link_afiliado_cupom(cupom, usuario)
        if not resolucao_codigo.get("sucesso"):
            return {
                "sucesso": False,
                "motivo": resolucao_codigo.get("motivo")
                or "O link afiliado deste cupom ainda não está disponível.",
                "classe": "transitorio",
                "precisa_login_ml": bool(resolucao_codigo.get("precisa_login_ml")),
                "link_afiliado_pendente": True,
            }
        link_codigo = resolucao_codigo["link"]

    if not modo_codigo and not relacoes_preparadas:
        # Shopee/Awin: a API já devolve HTTPS afiliado. Amazon oficial: ASIN +
        # tag, sem Chromium. Exigir ProdutoCupom (mapa ML) marcava ready na
        # tela e recusava no envio.
        from apps.scrapers.coupon_rules import ativacao_publicavel
        destino = str(getattr(cupom, "link", "") or "")
        marketplace = str(getattr(cupom, "marketplace", "") or "").lower()
        if (marketplace == "awin"
                and ativacao_publicavel(cupom, usuario=usuario)
                and destino.startswith("https://")):
            modo_link_direto = True
            link_codigo = destino
        elif marketplace == "shopee" and ativacao_publicavel(
                cupom, usuario=usuario):
            resolucao_shopee = resolver_link_afiliado_cupom(cupom, usuario)
            if resolucao_shopee.get("sucesso"):
                modo_link_direto = True
                link_codigo = resolucao_shopee["link"]
        elif marketplace == "amazon" and ativacao_publicavel(cupom, usuario=usuario):
            from apps.scrapers.scraper_amazon.link import gerar_link_afiliado_cupom
            destino_az = _executar_orm(gerar_link_afiliado_cupom, cupom, usuario)
            if str(destino_az or "").startswith("https://"):
                modo_link_direto = True
                link_codigo = destino_az
        elif marketplace == "mercadolivre" and ativacao_publicavel(cupom, usuario=usuario):
            from apps.scrapers.coupon_links import gerar_link_afiliado_listagem_ml
            destino_ml = _executar_orm(gerar_link_afiliado_listagem_ml, cupom, usuario)
            if str(destino_ml or "").startswith("https://"):
                modo_link_direto = True
                link_codigo = destino_ml

    aviso_sem_produto = modo_codigo or modo_link_direto

    # Segunda opinião por IA: o piso monetário fixo (cupom_e_lixo) já barrou o
    # caso claro — teto/valor irrisório — antes de o cupom chegar a "pronto".
    # Isto pega o que só leitura pega: condição confusa, escopo ilegível,
    # cheiro de isca. Uma chamada por TENTATIVA REAL de envio, não por cupom no
    # catálogo — por isso pode usar o modelo cheio sem custar escala. Falha ou
    # IA desligada nunca bloqueia sozinha (fail-open); só bloqueia quando a IA
    # respondeu e disse explicitamente que não vale.
    from apps.scrapers.coupon_rules import escopo_produtos_cupom, regras_do_cupom
    from apps.scrapers.llm import avaliar_cupom_ia
    regras_cupom = regras_do_cupom(cupom)
    escopo_original = escopo_produtos_cupom(cupom) or str(regras_cupom.get("escopo") or "")
    avaliacao_ia = avaliar_cupom_ia(
        escopo=escopo_original,
        tipo_desconto=regras_cupom.get("tipo_desconto") or "",
        valor_desconto=regras_cupom.get("valor_desconto"),
        desconto_maximo=regras_cupom.get("desconto_maximo"),
        valor_minimo=regras_cupom.get("valor_minimo"),
        restrito=bool(getattr(cupom, "restrito", False)),
    )
    if not avaliacao_ia["vale_a_pena"]:
        _executar_orm(
            log_event,
            "publicacao", "coupon_rejected_by_ai",
            f"Cupom recusado pela IA: {avaliacao_ia['motivo'] or 'sem motivo informado'}.",
            level="info",
            usuario=usuario, contexto={"cupom_id": cupom_id, "canal": canal,
                                       "destino": destino_nome or grupo_id,
                                       "motivo_ia": avaliacao_ia["motivo"]},
        )
        return {
            "sucesso": False,
            "motivo": avaliacao_ia["motivo"] or "Este cupom não passou na avaliação de qualidade.",
            "classe": "permanente", "rejeitado_por_ia": True,
        }

    if not aviso_sem_produto and not relacoes_preparadas:
        _executar_orm(
            log_event,
            "publicacao", "coupon_not_ready",
            "Cupom aguardando preparação ou atualização.", level="warning",
            usuario=usuario, contexto={"cupom_id": cupom_id, "canal": canal,
                                       "destino": destino_nome or grupo_id},
        )
        return {"sucesso": False,
                "motivo": "Este cupom está sendo atualizado e ainda não está disponível para envio.",
                "classe": "transitorio", "cupom_em_preparo": True}
    relacoes_prontas = (
        [] if aviso_sem_produto
        else _executar_orm(relacoes_prontas_para_envio, cupom, usuario)
    )
    if not aviso_sem_produto and not relacoes_prontas:
        _executar_orm(
            log_event,
            "publicacao", "coupon_link_pending",
            "Cupom preparado sem link afiliado utilizável.", level="warning",
            usuario=usuario, contexto={"cupom_id": cupom_id, "canal": canal,
                                       "destino": destino_nome or grupo_id},
        )
        return {"sucesso": False,
                "motivo": "Os links afiliados deste cupom ainda estão sendo preparados. Aguarde alguns instantes.",
                "classe": "transitorio", "link_afiliado_pendente": True}

    desde = agora - timedelta(hours=24)

    def _reservar():
        """Transação curta de reserva: lock do usuário, deduplicação por destino,
        cota diária e a Publicacao pendente. Roda via _executar_orm para não
        herdar a transação longa do chamador (ver _sse_runner/segurar_transacao).
        Devolve (cupom_relido, publicacao) ou um dict de falha."""
        with transaction.atomic():
            # O lock do usuário serializa cota e deduplicação por destino. Um cupom
            # público, porém, só é legível pelo tenant: `FOR UPDATE` exige a policy
            # de escrita e o PostgreSQL o esconde como se não existisse. Não há
            # escrita no cupom neste fluxo, portanto buscá-lo sem lock é suficiente.
            get_user_model().objects.select_for_update().get(pk=usuario.pk)
            cupom_qs = type(cupom).objects.filter(
                pk=cupom_id, estado="ativo",
            ).filter(Q(validade__isnull=True) | Q(validade__gte=agora))
            if getattr(cupom, "owner_id", None) is None:
                cupom_atual = cupom_qs.filter(owner__isnull=True).first()
            else:
                cupom_atual = cupom_qs.filter(owner=usuario).select_for_update().first()
            if not cupom_atual:
                log_event(
                    "publicacao", "coupon_unavailable",
                    "Cupom atualizado antes da reserva do envio.", level="warning",
                    usuario=usuario, contexto={"cupom_id": cupom_id, "canal": canal,
                                               "destino": destino_nome or grupo_id},
                )
                return {"sucesso": False,
                        "motivo": "Este cupom foi atualizado e não está mais disponível. Atualize a tela e tente outro.",
                        "classe": "permanente", "cupom_atualizado": True}
            recente = Publicacao.objects.filter(
                usuario=usuario, origem="cupom", cupom_normalizado=cupom_atual,
                canal=canal, destino_id=grupo_id,
            ).filter(
                Q(status="pendente", criada_em__gte=agora - timedelta(minutes=30))
                | Q(status="enviado", enviada_em__gte=desde)
                | Q(status="incerto", criada_em__gte=desde)
            ).order_by("-criada_em").first()
            if recente:
                motivo = ("Este cupom já está sendo enviado para o destino."
                          if recente.status == "pendente"
                          else "Este destino já recebeu o cupom nas últimas 24h.")
                return {"sucesso": False, "motivo": motivo, "duplicado": True,
                        "classe": "permanente"}

            perfil = getattr(usuario, "perfil", None)
            if perfil and perfil.bloqueado:
                return {"sucesso": False, "motivo": "Conta bloqueada para envios.",
                        "classe": "permanente"}
            inicio_dia = timezone.localtime(agora).replace(hour=0, minute=0, second=0,
                                                           microsecond=0)
            limite = perfil.cota_max_envios_dia() if perfil else 0
            usados = Publicacao.objects.filter(
                usuario=usuario, criada_em__gte=inicio_dia,
                status__in=("pendente", "enviado", "incerto"),
            ).count()
            if limite and usados >= limite:
                return {"sucesso": False, "motivo": "Limite diário de envios atingido.",
                        "classe": "permanente"}
            return cupom_atual, Publicacao.objects.create(
                usuario=usuario, origem="cupom", cupom_normalizado=cupom_atual,
                configuracao=configuracao,
                canal=canal, destino_id=str(grupo_id)[:100],
                destino_nome=str(destino_nome or "")[:255],
                cupom=str(codigo_publicavel(cupom_atual) or cupom_atual.titulo or "")[:255],
                categoria="Cupom", score=float(score or 0),
                motivos_score=list(motivos_score or []),
            )

    if _reserved_publication is None:
        try:
            reserva = _executar_orm(_reservar)
        except Exception as exc:
            logger.exception("Falha ao reservar envio do cupom %s", cupom_id)
            _executar_orm(
                log_event,
                "publicacao", "coupon_reservation_failed",
                "Não foi possível reservar o envio do cupom.", level="error",
                usuario=usuario, contexto={"cupom_id": cupom_id, "canal": canal,
                                           "destino": destino_nome or grupo_id}, exc=exc,
            )
            return {"sucesso": False,
                    "motivo": "Não foi possível reservar este cupom para envio. Atualize a tela e tente novamente.",
                    "classe": "transitorio", "causa": type(exc).__name__}
        if isinstance(reserva, dict):
            return reserva
        cupom, publicacao = reserva
    else:
        publicacao = _reserved_publication

    if enqueue_only:
        from apps.scrapers.send_pipeline import queue_publications
        try:
            queue_publications(
                [publicacao], image_b64=imagem_b64_custom, mime="image/jpeg",
            )
        except ValueError as exc:
            Publicacao.objects.filter(pk=publicacao.pk, status="pendente").update(
                status="falhou", stage="rejected", transport_state="invalid_payload",
                erro=str(exc)[:500],
            )
            return {"sucesso": False, "motivo": str(exc), "classe": "permanente"}
        return {
            "sucesso": True, "queued": True, "publicacao": publicacao,
            "motivo": "Cupom reservado e aguardando o worker.",
        }

    # No worker v2 o link do aviso de código é resolvido somente depois do claim.
    if modo_codigo and not link_codigo:
        resolucao_codigo = resolver_link_afiliado_cupom(cupom, usuario)
        if not resolucao_codigo.get("sucesso"):
            return {
                "sucesso": False,
                "motivo": resolucao_codigo.get("motivo")
                or "O link afiliado deste cupom ainda não está disponível.",
                "classe": "transitorio",
                "precisa_login_ml": bool(resolucao_codigo.get("precisa_login_ml")),
                "link_afiliado_pendente": True,
            }
        link_codigo = resolucao_codigo["link"]

    _executar_orm(
        log_event,
        "publicacao", "send_started", "Preparando envio do cupom.",
        usuario=usuario, contexto={"publicacao_id": publicacao.id, "cupom_id": cupom.id,
                                   "canal": canal, "destino": destino_nome or grupo_id},
    )

    def falhar(motivo, **extra):
        erro_tecnico = extra.pop("_erro_tecnico", "")
        pipeline_recorded = extra.pop("_pipeline_recorded", False)
        incerto = extra.get("resultado") == "incerto"

        def _fechar_e_logar():
            with transaction.atomic():
                if not pipeline_recorded:
                    Publicacao.objects.filter(pk=publicacao.pk, status="pendente").update(
                        status="incerto" if incerto else "falhou", erro=str(motivo)[:500])
            log_event("publicacao", "send_failed", str(motivo), level="warning",
                      usuario=usuario, contexto={"publicacao_id": publicacao.id,
                                                 "cupom_id": cupom.id, "canal": canal,
                                                 "destino": destino_nome or grupo_id,
                                                 "erro_tecnico": erro_tecnico, **extra})
        try:
            _executar_orm(_fechar_e_logar)
        except Exception:
            logger.exception("Falha ao fechar publicação de cupom %s", publicacao.pk)
        return {"sucesso": False, "motivo": str(motivo), **extra}

    try:
        # Código validado é aviso sem produto. Ativação continua estrita e nunca usa
        # foto manual para aparentar associação que a fonte não comprovou.
        img_kwargs = {}
        relacao_topo = None
        itens_cupom, bloqueio_afiliacao = [], None
        if aviso_sem_produto:
            link_registro = link_codigo
            mensagem = montar_mensagem_cupom(
                cupom, link_afiliado=link_registro, markup=sender.markup,
                escopo_override=avaliacao_ia["escopo_legivel"],
            )
            if imagem_b64_custom:
                img_kwargs = {
                    "imagem_b64": imagem_b64_custom,
                    "mimetype": "image/jpeg",
                }
        else:
            itens_cupom, bloqueio_afiliacao = _preparar_itens_cupom(
                cupom, usuario, relacoes_prontas)
        if (not aviso_sem_produto and itens_cupom
                and getattr(settings, "PRECO_REVALIDA_ANTES_ENVIO", True)):
            # Antes da IA (para a chamada nascer do preço fresco), antes do corte
            # do Telegram e antes da colagem — que é quem garante foto↔texto.
            from apps.scrapers import preco_ao_vivo
            itens_cupom, _removidos = _executar_orm(
                preco_ao_vivo.revalidar_colagem, cupom, itens_cupom, usuario=usuario)
            if not itens_cupom:
                return falhar("Os preços deste cupom mudaram; nenhum produto "
                              "continua dentro das regras dele.", classe="transitorio")
        if not aviso_sem_produto and itens_cupom:
            _preparar_conteudo_ia_cupom(itens_cupom)
            # Telegram limita legendas de foto a 1024 caracteres. Como a regra e
            # "ate 9", remove os itens de menor prioridade ate a mensagem caber.
            if canal == "telegram":
                while len(itens_cupom) > 1 and len(montar_mensagem_cupom_produtos(
                        cupom, itens_cupom, markup=sender.markup)) > 1024:
                    itens_cupom.pop()
            from apps.scrapers.colagem import montar_colagem_itens
            colagem_b64, colagem_mime, itens_cupom = montar_colagem_itens(itens_cupom)
            if not colagem_b64 or not itens_cupom:
                return falhar("Nenhuma foto válida foi encontrada para os produtos do cupom.",
                              classe="transitorio")
            mensagem = montar_mensagem_cupom_produtos(
                cupom, itens_cupom, markup=sender.markup)
            link_registro = itens_cupom[0]["link"]
            img_kwargs = {"imagem_b64": colagem_b64, "mimetype": colagem_mime}
        elif not aviso_sem_produto and bloqueio_afiliacao:
            # Havia produtos comprovados, mas a sessão do Mercado Livre caiu na
            # hora de gerar os links afiliados. Não é "cupom sem produtos": é
            # reconexão. Transitório para não pausar a automação por queda de
            # sessão, e com o flag que a UI usa para oferecer o botão de reconectar.
            return falhar(bloqueio_afiliacao["mensagem"], classe="transitorio",
                          precisa_login_ml=bloqueio_afiliacao["precisa_login_ml"])
        elif not aviso_sem_produto:
            return falhar("Cupom sem produtos comprovadamente aplicáveis, com foto e link afiliado.",
                          classe="permanente")
        if not mensagem.strip():
            return falhar("Não foi possível montar uma mensagem válida.", classe="permanente")
        # Preços do primeiro item da colagem: sem isto a Publicacao de cupom ficava
        # com 0/0 e não havia como reconciliar "o preço anunciado não bate".
        # Aqui o par é (vitrine, pós-cupom) — nas publicações de produto o par é
        # (tabela, vitrine), que é o que aquela mensagem anuncia.
        if not aviso_sem_produto:
            relacao_topo = itens_cupom[0].get("relacao")

        def _gravar_mensagem():
            Publicacao.objects.filter(pk=publicacao.pk).update(
                mensagem=mensagem, link_afiliado=link_registro,
                link_rastreado=link_registro,
                preco_original=float(getattr(relacao_topo, "preco_atual", 0) or 0),
                preco_final=float(getattr(relacao_topo, "preco_final", 0) or 0))
        _executar_orm(_gravar_mensagem)
        sessao_wa = _executar_orm(wa_session_de, usuario)
        from apps.scrapers.send_pipeline import begin_transport, finish_transport
        publicacao_transporte, tentativa = _executar_orm(begin_transport, publicacao)
        resultado = sender.enviar_oferta(
            grupo_id, mensagem, legenda=mensagem, usuario=usuario,
            session=sessao_wa,
            operation_id=publicacao_transporte.operation_key, **img_kwargs)
        _executar_orm(
            finish_transport, publicacao_transporte, tentativa, resultado,
            duration_ms=resultado.get("duracao_ms", 0),
        )
        if resultado.get("sucesso"):
            def _gravar_envio():
                Publicacao.objects.filter(pk=publicacao.pk).update(
                    status="enviado", enviada_em=timezone.now())
                log_event("publicacao", "send_ok", "Cupom publicado com sucesso.",
                          usuario=usuario, contexto={"publicacao_id": publicacao.id,
                                                     "cupom_id": cupom.id, "canal": canal,
                                                     "destino": destino_nome or grupo_id,
                                                     "via": resultado.get("via")})
            _executar_orm(_gravar_envio)
            return {"sucesso": True, "via": resultado.get("via", canal),
                    "canal": resultado.get("canal", canal),
                    "link": link_registro, "mensagem": mensagem,
                    "publicacao": publicacao,
                    "mensagem_id": resultado.get("mensagem_id"),
                    "classe": resultado.get("classe", ""),
                    "resultado": resultado.get("resultado", "confirmado"),
                    "repetir": resultado.get("repetir", False),
                    "etapa": resultado.get("etapa", "transporte"),
                    "duracao_ms": resultado.get("duracao_ms", 0)}
        return falhar(_motivo_publico_transporte(resultado),
                      _erro_tecnico=resultado.get("erro") or "",
                      _pipeline_recorded=True,
                      classe=resultado.get("classe"), resultado=resultado.get("resultado"),
                      repetir=resultado.get("repetir"), etapa=resultado.get("etapa"),
                      duracao_ms=resultado.get("duracao_ms"),
                      falha_infra=resultado.get("falha_infra", False))
    except Exception as exc:
        logger.exception("Erro inesperado ao enviar cupom %s", cupom.pk)
        return falhar("Falha inesperada ao preparar o cupom.", classe="desconhecido",
                      causa=type(exc).__name__, _erro_tecnico=str(exc))


# Teto de cupons por aviso. Os modelos da cliente têm 8; acima de ~10 a mensagem
# passa a rolar demais no celular e o WhatsApp começa a truncar a prévia.
LIMITE_CUPONS_AVISO = 10

# Origem da Publicacao do broadcast. Separada de "cupom" de propósito: o aviso não
# anuncia produto, então misturar os dois quebraria tanto a deduplicação quanto
# qualquer leitura futura de desempenho por tipo de mensagem.
ORIGEM_AVISO_CUPONS = "aviso_cupons"


def selecionar_cupons_para_aviso(configuracao, usuario, limite=LIMITE_CUPONS_AVISO):
    """Cupons de CÓDIGO ainda não anunciados neste destino, melhores primeiro.

    Deliberadamente NÃO passa por `ProdutoCupom`, `relacoes_prontas_para_envio` nem
    pela flag de cupons de ativação: o aviso não promete produto nenhum, então o
    portão de associação cupom↔produto — que existe para impedir "use o cupom X
    neste item" quando X não vale ali — não tem o que proteger aqui. O que ele exige
    é o oposto: um código que a pessoa consiga digitar no checkout.
    """
    from apps.scrapers.coupon_rules import (
        codigo_publicavel, codigos_com_escopo_contestado, cupons_visiveis_q,
        regras_do_cupom, score_cupom,
    )
    from apps.accounts.models import organization_for_user
    from apps.scrapers.maintenance import cupons_frescos_q
    from apps.scrapers.models import CupomNormalizado

    agora = timezone.now()
    marketplace = str(getattr(configuracao, "marketplace", "") or "").strip().lower()
    # O disparo avulso da tela usa um SimpleNamespace em vez de uma regra salva.
    # Ele não possui ``organization``; usar esse atributo no filtro transformava a
    # consulta em ``organization IS NULL`` e esvaziava o lote, pois a projeção de
    # disponibilidade sempre pertence a uma organização. A autoridade aqui é o
    # usuário autenticado, comum tanto ao disparo avulso quanto ao agendado.
    organization = organization_for_user(usuario)
    if not marketplace or organization is None:
        return []

    # A constraint uniq_coupon_readiness_projection garante no máximo uma projeção
    # para este conjunto exato de dimensões. DISTINCT aqui só fazia o Postgres
    # ordenar/copiar todas as colunas largas de CupomNormalizado antes do LIMIT.
    base = CupomNormalizado.objects.select_related("fonte", "programa", "integracao").filter(
        cupons_visiveis_q(usuario),
        Q(inicio__isnull=True) | Q(inicio__lte=agora),
        cupons_frescos_q(agora=agora),
        marketplace=marketplace, estado="ativo",
        disponibilidades__usuario=usuario,
        disponibilidades__organization=organization,
        disponibilidades__channel=getattr(configuracao, "canal", "whatsapp"),
        disponibilidades__use_mode="code_notice",
        disponibilidades__stage="ready",
    ).exclude(codigo="")
    if not getattr(configuracao, "incluir_restritos", True):
        base = base.filter(restrito=False)

    # Já anunciado neste destino dentro do cooldown não volta: o grupo receberia a
    # mesma lista de códigos várias vezes por dia.
    desde = agora - timedelta(hours=getattr(configuracao, "horas_cooldown", 24) or 24)
    ja_anunciados = set(Publicacao.objects.filter(
        usuario=usuario, destino_id=configuracao.grupo_id,
        origem=ORIGEM_AVISO_CUPONS, cupom_normalizado__isnull=False,
    ).filter(Q(status="enviado", enviada_em__gte=desde)
             | Q(status__in=("pendente", "incerto"), criada_em__gte=desde)
             ).values_list("cupom_normalizado_id", flat=True))

    # O aviso não promete produto, mas promete ESCOPO: cada bloco sai com a linha
    # "🏷️ <onde vale>". Quando a fonte publica o mesmo código duas vezes — uma como
    # site inteiro e outra com recorte — anunciar a primeira é escrever no grupo que
    # o cupom vale em tudo, que é justamente o que o checkout desmente. A linha
    # estreita do mesmo código continua elegível e leva o escopo verdadeiro.
    #
    # A contradição é procurada no catálogo ATIVO inteiro da loja, não só no lote
    # elegível: a linha estreita pode não ter projeção de disponibilidade e ainda
    # assim é ela que desmente a alegação de site inteiro.
    lote = list(base.order_by("-ultima_observacao")[:200])
    contestados = codigos_com_escopo_contestado(
        CupomNormalizado.objects.filter(
            marketplace=marketplace, estado="ativo",
        ).exclude(codigo="").only(
            "id", "codigo", "regras", "external_id", "redemption_mode",
        )
    )

    candidatos = []
    for cupom in lote:
        if cupom.id in ja_anunciados:
            continue
        if not codigo_publicavel(cupom):
            continue
        if (regras_do_cupom(cupom).get("is_mar_aberto")
                and str(cupom.codigo or "").strip().upper() in contestados):
            continue
        if not linha_desconto_cupom(cupom):
            continue
        if cupom.programa and not (
                cupom.programa.habilitado and cupom.programa.status_vinculo == "joined"
                and cupom.programa.link_status == "online"):
            continue
        if cupom.integracao and not (
                cupom.integracao.habilitada and cupom.integracao.status == "conectada"):
            continue
        candidatos.append(cupom)
    # Um código, um bloco. O mesmo código chega por até três fontes ao mesmo tempo
    # (oficial, Promobit, Telegram) e cada uma é uma linha própria do catálogo:
    # sem deduplicar, a mensagem repetia "cupom: CUPOMDOML" três vezes, cada uma
    # com os termos da fonte que a gerou. Entre as cópias vence a que NÃO é de
    # comunidade — é ela que traz validade e escopo publicados pela loja.
    from apps.scrapers.coupon_rules import cupom_de_comunidade

    candidatos.sort(key=lambda c: (cupom_de_comunidade(c), -score_cupom(c)))
    unicos, vistos = [], set()
    for cupom in candidatos:
        codigo = str(cupom.codigo or "").strip().upper()
        if codigo in vistos:
            continue
        vistos.add(codigo)
        unicos.append(cupom)
    return unicos[:limite]


def enviar_aviso_cupons(cupons, grupo_id, *, canal="whatsapp", usuario=None,
                        destino_nome="", configuracao=None, enqueue_only=False,
                        _reserved_publications=None):
    """Publica o aviso de cupons novos: só códigos, banner da loja, um link.

    Espelha a contabilidade de `enviar_cupom` (lock do usuário, cota diária,
    deduplicação por destino, uma Publicacao por cupom anunciado) e dispensa tudo
    que só faz sentido quando há produto: preparo, colagem e verificação de link
    afiliado por item.
    """
    from django.contrib.auth import get_user_model
    from apps.scrapers.banners import sortear_banner_b64
    from apps.scrapers.coupon_rules import codigo_publicavel
    from apps.scrapers.eventos import log_event
    from apps.scrapers.senders.registry import get_sender

    try:
        sender = get_sender(canal)
    except ValueError as exc:
        return {"sucesso": False, "motivo": str(exc), "classe": PERMANENTE}
    if not usuario or not grupo_id:
        return {"sucesso": False, "motivo": "Usuário ou destino ausente.",
                "classe": PERMANENTE}
    cupons = [c for c in (cupons or []) if getattr(c, "pk", None)]
    if not cupons:
        return {"sucesso": False, "motivo": "Nenhum cupom novo para anunciar.",
                "classe": TRANSITORIO}
    erro_canal = None if enqueue_only else _canal_pronto_ou_erro(canal, usuario)
    if erro_canal:
        return erro_canal

    agora = timezone.now()
    # Só os cupons realmente publicáveis pertencem ao lote. Esta seleção é barata
    # e pode ocorrer no request; link, banner e transporte ficam no worker v2.
    anunciados = [c for c in cupons
                  if codigo_publicavel(c) and linha_desconto_cupom(c)][:LIMITE_CUPONS_AVISO]
    if not anunciados:
        return {"sucesso": False, "motivo": "Nenhum cupom novo para anunciar.",
                "classe": TRANSITORIO}
    marketplace = str(getattr(anunciados[0], "marketplace", "") or "")
    # Um aviso tem um único banner e um único destino afiliado. Mesmo que um
    # chamador interno passe uma lista mista, nunca atribua cupons de outra loja ao
    # link escolhido para o primeiro item válido.
    anunciados = [
        coupon for coupon in anunciados
        if str(getattr(coupon, "marketplace", "") or "") == marketplace
    ]

    # O link é UM por mensagem (o dos modelos), gerado a partir do melhor cupom do
    # lote pelo caminho que já existe: cache por usuário+cupom, Link Builder e
    # verificação da tag. Sessão caída aqui é transitória — a mensagem sai no
    # próximo tick, sem contar para o freio automático da regra.
    link = mensagem = ""
    if not enqueue_only:
        resolucao = resolver_link_afiliado_cupom(anunciados[0], usuario)
        if not resolucao.get("sucesso"):
            return {"sucesso": False,
                    "motivo": resolucao.get("motivo") or "Não foi possível gerar o link do aviso.",
                    "classe": TRANSITORIO,
                    "precisa_login_ml": bool(resolucao.get("precisa_login_ml"))}
        link = resolucao["link"]

        mensagem = montar_mensagem_aviso_cupons(
            anunciados, marketplace, link=link, markup=sender.markup)
        if not mensagem.strip():
            return {"sucesso": False, "motivo": "Nenhum cupom novo para anunciar.",
                    "classe": TRANSITORIO}

    def _reservar():
        """Transação curta de reserva: lock do usuário, cota diária e uma
        Publicacao por cupom anunciado. Roda via _executar_orm para não herdar
        a transação longa do chamador (ver _sse_runner/segurar_transacao)."""
        pubs = []
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=usuario.pk)
            perfil = getattr(usuario, "perfil", None)
            if perfil and perfil.bloqueado:
                return {"sucesso": False, "motivo": "Conta bloqueada para envios.",
                        "classe": PERMANENTE}
            inicio_dia = timezone.localtime(agora).replace(
                hour=0, minute=0, second=0, microsecond=0)
            limite = perfil.cota_max_envios_dia() if perfil else 0
            usados = Publicacao.objects.filter(
                usuario=usuario, criada_em__gte=inicio_dia,
                status__in=("pendente", "enviado", "incerto"),
            ).count()
            # A mensagem é UMA; a cota conta uma publicação por envio, não por cupom.
            if limite and usados >= limite:
                return {"sucesso": False, "motivo": "Limite diário de envios atingido.",
                        "classe": PERMANENTE}
            for cupom in anunciados:
                pubs.append(Publicacao.objects.create(
                    usuario=usuario, origem=ORIGEM_AVISO_CUPONS,
                    cupom_normalizado=cupom, configuracao=configuracao,
                    canal=canal, destino_id=str(grupo_id)[:100],
                    destino_nome=str(destino_nome or "")[:255],
                    cupom=str(codigo_publicavel(cupom) or "")[:255],
                    categoria="Aviso de cupons",
                    mensagem=mensagem, link_afiliado=link, link_rastreado=link,
                ))
        return pubs

    if _reserved_publications is None:
        try:
            reserva = _executar_orm(_reservar)
        except Exception as exc:
            logger.exception("Falha ao reservar o aviso de cupons para %s", grupo_id)
            return {"sucesso": False,
                    "motivo": "Não foi possível reservar este aviso para envio. Tente novamente.",
                    "classe": TRANSITORIO, "causa": type(exc).__name__}
        if isinstance(reserva, dict):
            return reserva
        publicacoes = reserva
    else:
        publicacoes = list(_reserved_publications)

    if enqueue_only:
        from apps.scrapers.send_pipeline import queue_publications
        try:
            queue_publications(publicacoes)
        except ValueError as exc:
            Publicacao.objects.filter(
                pk__in=[row.pk for row in publicacoes], status="pendente",
            ).update(
                status="falhou", stage="rejected", transport_state="invalid_payload",
                erro=str(exc)[:500],
            )
            return {"sucesso": False, "motivo": str(exc), "classe": PERMANENTE}
        return {
            "sucesso": True, "queued": True,
            "publicacao": publicacoes[0] if publicacoes else None,
            "cupons": len(publicacoes),
            "motivo": "Aviso reservado e aguardando o worker.",
        }

    ids = [p.pk for p in publicacoes]

    def _fechar(status, erro="", pipeline_recorded=False):
        def _update():
            if not pipeline_recorded:
                Publicacao.objects.filter(pk__in=ids, status="pendente").update(
                    status=status, erro=str(erro)[:500],
                    **({"enviada_em": timezone.now()} if status == "enviado" else {}))
        _executar_orm(_update)

    _executar_orm(
        log_event, "publicacao", "send_started", "Preparando aviso de cupons.",
        usuario=usuario, contexto={"cupons": len(anunciados), "canal": canal,
                                   "marketplace": marketplace,
                                   "destino": destino_nome or grupo_id})
    transportes = []
    try:
        banner_b64, banner_mime = sortear_banner_b64(marketplace)
        img_kwargs = ({"imagem_b64": banner_b64, "mimetype": banner_mime}
                      if banner_b64 else {})
        sessao_wa = _executar_orm(wa_session_de, usuario)
        from apps.scrapers.send_pipeline import begin_transport, finish_transport
        transportes = [
            _executar_orm(begin_transport, publicacao)
            for publicacao in publicacoes
        ]
        resultado = sender.enviar_oferta(
            grupo_id, mensagem, legenda=mensagem, usuario=usuario,
            session=sessao_wa,
            operation_id=transportes[0][0].operation_key if transportes else None,
            **img_kwargs)
        for publicacao_transporte, tentativa in transportes:
            _executar_orm(
                finish_transport, publicacao_transporte, tentativa, resultado,
                duration_ms=resultado.get("duracao_ms", 0),
            )
    except Exception as exc:
        logger.exception("Erro inesperado ao enviar o aviso de cupons")
        if transportes:
            from apps.scrapers.send_pipeline import finish_transport
            incerto = {
                "sucesso": False, "resultado": "incerto", "repetir": False,
                "etapa": "transport_started", "causa": type(exc).__name__,
            }
            for publicacao_transporte, tentativa in transportes:
                _executar_orm(
                    finish_transport, publicacao_transporte, tentativa, incerto,
                )
        _fechar("incerto" if transportes else "falhou", str(exc),
                pipeline_recorded=bool(transportes))
        return {"sucesso": False, "motivo": "Falha inesperada ao enviar o aviso.",
                "classe": DESCONHECIDO, "causa": type(exc).__name__,
                "resultado": "incerto" if transportes else "falha"}

    if resultado.get("sucesso"):
        _fechar("enviado", pipeline_recorded=True)
        _executar_orm(
            log_event, "publicacao", "send_ok", "Aviso de cupons publicado.",
            usuario=usuario, contexto={"cupons": len(anunciados), "canal": canal,
                                       "destino": destino_nome or grupo_id,
                                       "via": resultado.get("via")})
        return {"sucesso": True, "via": resultado.get("via", canal),
                "canal": resultado.get("canal", canal), "link": link,
                "mensagem": mensagem, "cupons": len(anunciados),
                "publicacao": publicacoes[0] if publicacoes else None,
                "mensagem_id": resultado.get("mensagem_id"),
                "classe": resultado.get("classe", ""),
                "resultado": resultado.get("resultado", "confirmado"),
                "repetir": resultado.get("repetir", False),
                "etapa": resultado.get("etapa", "transporte"),
                "duracao_ms": resultado.get("duracao_ms", 0)}

    motivo = _motivo_publico_transporte(resultado)
    _fechar(
        "incerto" if resultado.get("resultado") == "incerto" else "falhou",
        motivo, pipeline_recorded=True,
    )
    _executar_orm(
        log_event, "publicacao", "send_failed", motivo, level="warning",
        usuario=usuario, contexto={"cupons": len(anunciados), "canal": canal,
                                   "destino": destino_nome or grupo_id,
                                   "erro_tecnico": resultado.get("erro") or ""})
    return {"sucesso": False, "motivo": motivo,
            "classe": resultado.get("classe") or DESCONHECIDO,
            "resultado": resultado.get("resultado"),
            "repetir": resultado.get("repetir"),
            "etapa": resultado.get("etapa"),
            "duracao_ms": resultado.get("duracao_ms")}


# Emoji por macro-categoria p/ a linha do produto na mensagem curta. Fallback 🛍️.
_EMOJI_MACRO = {
    "Celulares, Telefonia e Wearables": "📱",
    "Eletrônicos e Informática": "💻",
    "Áudio, Vídeo e Fotografia": "🎧",
    "Eletrodomésticos": "🔌",
    "Cozinha, Mesa e Bar": "🍽️",
    "Casa, Móveis e Decoração": "🛋️",
    "Beleza e Cuidados Pessoais": "💄",
    "Moda, Calçados e Acessórios": "👕",
    "Esportes e Fitness": "🏋️",
    "Games, Brinquedos e Hobbies": "🎮",
    "Ferramentas e Manutenção": "🔧",
    "Automotivo": "🚗",
    "Pets e Animais": "🐾",
    "Bebês e Maternidade": "🍼",
    "Alimentos e Bebidas": "🍫",
    "Saúde, Ortopedia e Equipamentos Médicos": "💊",
    "Papelaria, Escritório e Escola": "✏️",
    "Livros, Mídia e Conteúdo": "📖",
}


def _emoji_produto(produto) -> str:
    macro = getattr(produto, "macro_categoria", "") or ""
    return _EMOJI_MACRO.get(macro, "🛍️")


_RUIDO_NOME_PRODUTO = re.compile(
    r"\b(?:frete\s+gr[aá]tis|envio\s+imediato|pronta\s+entrega|loja\s+oficial|"
    r"produto\s+original|oferta|promo[cç][aã]o|imperd[ií]vel|mercado\s+livre)\b",
    re.I,
)


# Palavras que não podem terminar uma linha: o corte em palavra respeita o limite
# mas deixa a frase pendurada. Medido em produção em 03/09/2026: o nome saiu como
# "...Fórmula com Máxima Concentração e" e a condição do cupom terminou em
# "compra a partir de R$" — anunciando um mínimo sem dizer qual.
_CAUDA_PENDURADA = re.compile(
    # `à` e `ao` faltavam, e é neles que o nome do ML costuma quebrar:
    # "Smartwatch ... Bluetooth Ip68 À" — o "À Prova D'água" ficou do outro lado do
    # corte. `+` e `&` entram porque "8gb +" e "Notebook &" penduram igual.
    r"[\s,;:|/–—+&-]*(?:\b(?:e|ou|de|da|do|das|dos|com|sem|para|por|at[ée]|em|no|na|"
    r"nos|nas|[àá]|ao|aos|[àá]s|sob|sobre|"
    r"a\s+partir\s+de)\b|R\$)\s*$",
    re.I,
)


def _sem_cauda_pendurada(texto: str) -> str:
    anterior = None
    while texto != anterior:
        anterior = texto
        texto = _CAUDA_PENDURADA.sub("", texto).rstrip(" -–—,;:|/")
    return texto


def _nome_principal_produto(nome, limite=70) -> str:
    """Limpa ruido comercial e corta em SEGMENTO, sem depender de IA externa.

    Nome de marketplace é lista de specs separada por vírgula — "Celular Samsung
    Galaxy A17 Com Ia, 128gb, 4gb Ram, Câm De 50mp, Tela De 6.7 , Nfc, Ip54 -
    Preto". Cortar na palavra mais próxima do limite parava no meio de uma spec, e
    o grupo lia "…Câm De 50mp, Tela": a frase morre no ar e parece raspagem
    quebrada — o oposto do que um canal que converte publica.

    Quando há vírgula suficientemente adiante, ela é o corte certo, porque cada
    segmento é uma spec inteira. Sem vírgula, volta ao corte por palavra.
    """
    texto = re.sub(r"\s+", " ", str(nome or "")).strip(" -–—,;")
    texto = _RUIDO_NOME_PRODUTO.sub("", texto)
    # O ML emite " ," de verdade ("Tela De 6.7 , Nfc"); sem normalizar, o corte por
    # segmento enxerga um segmento vazio e erra a fronteira.
    texto = re.sub(r"\s+([,;])", r"\1", texto)
    texto = re.sub(r"\s{2,}", " ", texto).strip(" -–—,;")
    if len(texto) <= limite:
        return texto
    janela = texto[:limite + 1]
    virgula = max(janela.rfind(","), janela.rfind(";"))
    # 55% do limite: abaixo disso sobra nome de menos para identificar o produto,
    # e aí vale mais cortar na palavra do que num segmento inicial solto.
    if virgula >= limite * 0.55:
        return (_sem_cauda_pendurada(janela[:virgula].rstrip(" -–—,;|/"))
                or janela[:virgula])
    cortado = janela.rsplit(" ", 1)[0].rstrip(" -–—,;|/")
    # Parêntese aberto e não fechado: o corte por palavra respeitou o limite mas
    # deixou "(8gb Ram+8gb Ram" pendurado no fim do nome. Descarta o trecho a
    # partir da abertura órfã — o que estava lá dentro era detalhe, não o produto.
    if cortado.count("(") > cortado.count(")"):
        cortado = cortado[:cortado.rfind("(")].rstrip(" -–—,;|/")
    return _sem_cauda_pendurada(cortado) or texto[:limite]


def _condicao_legivel(texto, limite=180) -> str:
    """Condição do cupom que termina numa frase inteira, sem repetir selo.

    O escopo bruto do Mercado Livre repete o mesmo selo ("25% de Desconto 25% OFF
    25% OFF produtos Mercado Livre"), e o corte cego em 220 caracteres cortava a
    última oração no meio. Preferir o fim de frase e remover a repetição é o que
    faz a linha ser lida como condição, não como sobra de raspagem.
    """
    limpo = re.sub(r"\s+", " ", str(texto or "")).strip()
    if not limpo:
        return ""
    anterior = None
    while limpo != anterior:
        anterior = limpo
        limpo = re.sub(r"\b(.{3,40}?)\s+\1\b", r"\1", limpo, flags=re.I).strip()
    if len(limpo) <= limite:
        return _sem_cauda_pendurada(limpo)
    corte = limpo[:limite + 1]
    fim = max(corte.rfind(". "), corte.rfind("; "))
    if fim > limite * 0.4:
        return corte[:fim].strip()
    return _sem_cauda_pendurada(corte.rsplit(" ", 1)[0]) + "…"


def _preco_br(valor) -> str:
    """R$ no formato brasileiro sem 'R$' e sem centavos zerados: 49,90 / 1.352."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return ""
    bruto = f"{numero:,.0f}" if numero.is_integer() else f"{numero:,.2f}"
    # en-US -> pt-BR: 1,299.90 vira 1.299,90 (o X é ponte para não trocar duas vezes).
    return bruto.replace(",", "X").replace(".", ",").replace("X", ".")


def preco_publicavel(produto) -> float:
    """Preço que o cliente realmente paga na página.

    `preco_com_cupom` guarda a vitrine; quando a fonte comprova um terceiro preço
    ativável (cupons oficiais da Amazon ou badge "com Cupom" do ML), o valor pago
    fica em `preco_efetivo`. No ML ele só é aceito com o badge lido na PRÓPRIA
    PDP: o card da vitrine anuncia cupom que a página não cobra (ver
    FONTES_CUPOM_INLINE_ML).
    """
    atual = getattr(produto, "preco_com_cupom", 0) or 0
    if getattr(produto, "marketplace", "") == "mercadolivre":
        inline = _preco_cupom_inline_ml(produto)
        return inline or atual
    efetivo = getattr(produto, "preco_efetivo", 0) or 0
    return efetivo if 0 < efetivo < atual else atual


# O card da vitrine ANUNCIA cupom; a página do anúncio é quem COBRA. Medido em
# 06/09/2026 no produto 80220 (MLB63561701, Loren Shower Ultra 7,5 kW): o card de
# `/ofertas` trazia "R$ 93,60 com cupom" às 23:14 UTC e a PDP aberta pelo cliente
# no mesmo dia cobrava R$ 117, sem cupom nenhum no buybox. Por isso `offer-card`
# vale como INDÍCIO e não como prova de preço: só `pdp-live` — o badge lido na
# própria página, em `preco_ao_vivo._aplicar_cupom_ml` — libera o pós-cupom.
#
# Consequência aceita: quando o ML desafia o IP da Fly, a revalidação é
# inconclusiva e o item sai pela vitrine. Anunciar R$ 93,60 e a página cobrar
# R$ 117 é pior do que anunciar os R$ 117 que ela cobra.
FONTES_CUPOM_INLINE_ML = {"pdp-live"}


def _preco_cupom_inline_ml(produto) -> float:
    """Terceiro preço comprovado na PDP do Mercado Livre. Ver FONTES_CUPOM_INLINE_ML."""
    if getattr(produto, "marketplace", "") != "mercadolivre":
        return 0.0
    atual = getattr(produto, "preco_com_cupom", 0) or 0
    efetivo = getattr(produto, "preco_efetivo", 0) or 0
    promocao = (getattr(produto, "evidencia", {}) or {}).get("promotion") or {}
    if not promocao.get("coupon_confirmed"):
        return 0.0
    if promocao.get("source") not in FONTES_CUPOM_INLINE_ML:
        return 0.0
    try:
        observado = float(promocao.get("coupon_final_price") or efetivo)
    except (TypeError, ValueError):
        return 0.0
    if 0 < observado < atual and abs(observado - float(efetivo or 0)) < 0.011:
        return observado
    return 0.0


def anotacao_preco_publicado():
    """`preco_publicavel` em SQL, para a tela anotar o mesmo número da mensagem.

    Existe para que haja UMA definição de "preço publicado". A tela mostrava
    `preco_com_cupom` enquanto a mensagem publicava `preco_publicavel()`, e o item
    aparecia com um valor na lista e saía com outro no WhatsApp.

    O ML tem porteiro próprio, o mesmo de `_preco_cupom_inline_ml`: o terceiro
    preço só conta com o badge lido na PDP. Sem esta condição a
    anotação era `min(vitrine, efetivo)` para todo mundo, e um `preco_efetivo`
    de cupom que já expirou fazia a lista mostrar R$ 93,60 enquanto a mensagem
    publicava os R$ 117 da vitrine — a divergência que esta função existe para
    impedir.

    Diferença residual conhecida: o Python também exige que
    `coupon_final_price` bata com `preco_efetivo` (1 centavo de folga). Quem
    grava a evidência escreve os dois juntos (`_evidencia_oferta`,
    `_aplicar_cupom_ml`), então a condição só separaria linha corrompida;
    comparar número dentro de JSON muda de forma entre SQLite e Postgres e não
    vale o risco no filtro da vitrine.
    """
    from django.db.models import Case, F, FloatField, Q, Value, When
    from django.db.models.functions import Coalesce, Least, NullIf

    pos_cupom = Least(
        F("preco_com_cupom"),
        Coalesce(NullIf(F("preco_efetivo"), Value(0.0)), F("preco_com_cupom")),
        output_field=FloatField(),
    )
    # Sem negação de propósito: `~Q(...)` sobre chave de JSON ausente vira NULL e
    # a linha cairia no `default`, que é justamente o ramo errado para o ML.
    fontes = None
    for fonte in sorted(FONTES_CUPOM_INLINE_ML):
        condicao = Q(evidencia__promotion__source=fonte)
        fontes = condicao if fontes is None else (fontes | condicao)
    cupom_inline_ml = Q(
        marketplace="mercadolivre",
        evidencia__promotion__coupon_confirmed=True,
    ) & fontes
    return Case(
        When(cupom_inline_ml, then=pos_cupom),
        When(marketplace="mercadolivre", then=F("preco_com_cupom")),
        default=pos_cupom,
        output_field=FloatField(),
    )


# Quanto acima da mínima observada um preço ainda conta como "oferta". 5% é o número
# que o CamelCamelCamel publica como definição de good deal ("5% higher than the best
# price ever seen"), e é o mesmo raciocínio que a moderação do Promobit descreve:
# o veredito sai do histórico, não do percentual de vitrine.
FOLGA_MINIMA_HISTORICA = 1.05

# Piso bruto do SQL. Não é o critério — é só o que evita carregar catálogo inteiro
# para a memória. Item sem desconto nenhum não tem o que ser avaliado.
PISO_DESCONTO_BRUTO = 5.0

# Quantos candidatos o ranking avalia por vez. Cada um custa uma consulta de
# histórico, então isto é o que mantém o custo do tick constante em vez de
# proporcional ao catálogo. Folgado de propósito: o envio escolhe 1 ou 2 itens, e
# 400 candidatos ordenados por observação recente cobrem qualquer nicho com sobra.
TETO_CANDIDATOS = 400
# Fatia PRÓPRIA para candidatos que já têm par confirmado com cupom ativo. Teto
# separado de propósito: dentro do mesmo, encher um lado esvaziaria o outro. 300
# cobre a folga real — dos 3.183 produtos com par, 330 foram observados nas
# últimas 48h, que é o que o filtro de frescor deixa passar de qualquer forma.
TETO_CANDIDATOS_COM_CUPOM = int(
    os.getenv("TETO_CANDIDATOS_COM_CUPOM", "300"))


def _no_fundo_do_historico(produto, preco_final: float, historico) -> bool:
    """O preço está colado na mínima que nós mesmos observamos em 30 dias?

    É o critério que o mercado usa e que faltava aqui. Um TV cuja mínima histórica é
    R$ 799 não vira oferta por estar "40% OFF" de um preço de lista inventado — e um
    item a R$ 810 É oferta mesmo anunciando 8% de desconto. O percentual de vitrine
    responde "quanto a loja diz que baixou"; a mínima responde "isso é barato".
    """
    if not historico or preco_final <= 0:
        return False
    minimo = historico.get("minimo") or 0
    if minimo <= 0:
        return False
    return preco_final <= minimo * FOLGA_MINIMA_HISTORICA


def _passa_no_minimo(produto, preco_final, historico, min_desconto_percent) -> bool:
    """Elegível por desconto aparente OU por estar no fundo do histórico.

    Os dois caminhos existem porque medem coisas diferentes, e usar só o primeiro
    descartava a melhor oferta real: o mesmo preço de lista fictício que inflava
    item ruim também escondia item bom, agora por baixo. Basta um dos dois.
    """
    # `desconto_percent` é anotação do queryset da seleção. Fora dali (teste, ou
    # qualquer chamador futuro) o objeto não tem o atributo, e cair com AttributeError
    # transformaria uma regra de negócio em erro de infraestrutura. Recalcula.
    aparente = getattr(produto, "desconto_percent", None)
    if aparente is None:
        lista = float(getattr(produto, "preco_sem_desconto", 0) or 0)
        aparente = ((lista - preco_final) / lista * 100.0) if lista > 0 else 0.0
    if aparente >= min_desconto_percent:
        return True
    return _no_fundo_do_historico(produto, preco_final, historico)


_SEM_HISTORICO_PRECARREGADO = object()


def _desconto_comprovado(
    produto, preco_final: float, *, historico=_SEM_HISTORICO_PRECARREGADO,
) -> bool:
    """Nós já vimos este item custar mais caro?

    É a diferença entre "a loja diz que estava R$ 500" e "nós observamos R$ 500".
    Só a segunda sustenta um preço riscado numa mensagem assinada pelo usuário.

    A prova vem do nosso próprio `PrecoHistorico`, que é chaveado por
    (marketplace, chave-normalizada) e sobrevive ao Produto ser recriado a cada
    raspagem. Janela de 90 dias, e não os 30 do filtro de seleção, porque aqui a
    pergunta é outra: não é "está barato agora?", é "este preço mais alto existiu?".

    Uma observação já basta. Exigir três — como faz o filtro de mediana da seleção —
    protegeria o mesmo item duas vezes e deixaria a maioria das ofertas do dia sem
    nenhuma proteção, que é justamente o buraco que este portão fecha. O que não
    basta é observação no mesmo patamar do preço atual: 2% de folga evita tratar
    ruído de arredondamento como se fosse queda.
    """
    if preco_final <= 0:
        return False
    if historico is _SEM_HISTORICO_PRECARREGADO:
        try:
            historico = _stats_preco(produto, dias=90)
        except Exception:
            # Sem conseguir consultar, o desconto fica NÃO comprovado. Falha fechada:
            # a mensagem perde o "DE" e não afirma nada que não foi verificado.
            logger.warning("Histórico indisponível para o produto %s; desconto não "
                           "comprovado.", getattr(produto, "pk", "?"))
            return False
    if not historico or not historico.get("n"):
        return False
    return historico["mediana"] > preco_final * 1.02


def montar_mensagem(produto, link_afiliado: str, cupom_pai, markup=None,
                    usuario=None, configuracao=None, variante="A") -> str:
    """
    Monta o texto da oferta usando o `Markup` do canal (WhatsApp *neg*, Telegram <b>).
    Conteúdo dinâmico passa por markup.escape p/ não quebrar HTML do Telegram.

    Formato curto (modelo dos grupos): título da IA em caixa alta, produto, preço
    DE|POR, cupom (quando há código publicável) e link.
    """
    from apps.scrapers.senders.base import WhatsAppMarkup
    m = markup or WhatsAppMarkup()
    esc = m.escape

    preco_final = preco_publicavel(produto)
    economia_rs = produto.preco_sem_desconto - preco_final
    desconto_percent = (economia_rs / produto.preco_sem_desconto) * 100 if produto.preco_sem_desconto else 0
    perfil = getattr(usuario, "perfil", None) if usuario else None
    marca = (
        getattr(configuracao, "nome_marca", "")
        or getattr(perfil, "nome_marca", "") or "Ofertas"
    ).strip()
    cta = (
        getattr(configuracao, "chamada_acao", "")
        or getattr(perfil, "chamada_acao", "") or "Compre aqui"
    ).strip()
    disclosure = (
        getattr(configuracao, "divulgacao_afiliado", "")
        or getattr(perfil, "divulgacao_afiliado", "") or ""
    ).strip()
    template = (
        getattr(configuracao, "template_b" if variante == "B" else "template_a", "")
        or getattr(perfil, "template_b" if variante == "B" else "template_a", "")
    )
    conteudo_ia = _conteudo_marketing(produto)
    nome_exibicao = (
        conteudo_ia.get("nome_curto") or _nome_principal_produto(produto.nome)
    )
    if template:
        desconto_coerente_template = (
            0 < desconto_percent < 90
            and produto.preco_sem_desconto > preco_final
            and _desconto_comprovado(produto, preco_final)
        )
        try:
            return template.format(
                marca=esc(marca), nome=esc(nome_exibicao),
                preco=esc(f"R$ {_preco_br(preco_final)}"),
                desconto=esc(
                    f"{desconto_percent:.0f}%" if desconto_coerente_template else ""
                ),
                link=esc(link_afiliado), cta=esc(cta),
                divulgacao_afiliado=esc(disclosure),
            )
        except (KeyError, ValueError):
            pass

    # Blocos separados por linha em branco, no estilo dos grupos:
    #   TÍTULO
    #   (blank)
    #   {emoji} Produto
    #   (blank)
    #   🔥 DE X | POR Y     [+ 🎟️ CUPOM: ... colado embaixo]
    #   🔗 link
    linhas = []
    if getattr(produto, "relampago", False):
        linhas += [m.bold("⚡ OFERTA RELÂMPAGO"), ""]
    # Título da IA (frase_llm) em caixa alta, no topo — a "chamada" do grupo.
    titulo = conteudo_ia.get("titulo", "")
    if titulo:
        linhas += [esc(titulo), ""]

    linhas += [f"{_emoji_produto(produto)} {m.bold(esc(nome_exibicao))}", ""]

    # Guarda final: desconto >= 90% (ou "De:" <= "Por:") indica preço corrompido
    # (ex.: savingBasis em escala errada). Em vez de imprimir "100% OFF" absurdo,
    # esconde a parte "DE" e mostra só o "POR".
    desconto_coerente = (
        0 < desconto_percent < 90 and produto.preco_sem_desconto > preco_final
    )
    # E o portão que faltava: coerente não é o mesmo que COMPROVADO.
    #
    # O "DE" vem do preço de vitrine da loja, e a docstring de `PrecoHistorico` diz
    # o que ele vale: "o preço 'de' do ML costuma ser fictício". A revalidação de
    # envio (`preco_ao_vivo.revalidar`) reconfirma o preço ATUAL e nunca o "DE".
    # O filtro de mediana, alguns blocos acima em `selecionar_item_para_grupo`, só
    # roda com 3+ observações — ou seja, produto novo (a maioria das ofertas do dia)
    # passava sem nenhuma prova e a mensagem afirmava um desconto que ninguém
    # verificou. Riscar um preço que talvez nunca tenha existido é o falso positivo
    # mais caro do produto: quem assina a mensagem é o influenciador.
    #
    # Regra: sem prova nossa de que o item já custou mais, a mensagem mostra só o
    # POR. A oferta continua saindo — o que sai é a afirmação não comprovada.
    desconto_valido = desconto_coerente and _desconto_comprovado(produto, preco_final)
    por = _preco_br(preco_final)
    if desconto_valido:
        de = _preco_br(produto.preco_sem_desconto)
        linhas.append(f"🔥 DE {m.strike(de)} | {m.bold(f'POR {por}')}")
    else:
        linhas.append(f"🔥 {m.bold(f'POR {por}')}")

    # REGRA: cupons NÃO acumulam no ML. Cada item anuncia no máximo UM cupom.
    # Prioridade: cupom do link (cupom_pai) > código do próprio item (codigo_checkout)
    # > melhor código genérico VÁLIDO para este item. Nunca os três juntos.
    cod_item = getattr(produto, "codigo_checkout", "")
    linha_cupom = None
    cupom_escolhido = cupom_pai
    if cupom_pai is not None:
        linha_cupom = f"🎟️ {m.bold('CUPOM: ative no link')}"
    elif cod_item:
        linha_cupom = f"🎟️ {m.bold(f'CUPOM: {esc(cod_item)}')}"
    elif (getattr(produto, "marketplace", "") == "amazon"
          and (getattr(produto, "evidencia", {}) or {}).get("promotion", {}).get("coupon_confirmed")):
        # Dizer só "ative na página" deixava a linha sem a informação que muda a
        # decisão: o preço anunciado logo acima é o PÓS-cupom. Quem não ativasse
        # pagava o outro valor e concluía, com razão, que o anúncio mentiu.
        linha_cupom = f"🎟️ {m.bold('CUPOM: ative na Amazon — o preço já é com ele')}"
    elif _preco_cupom_inline_ml(produto):
        linha_cupom = f"🎟️ {m.bold('CUPOM: ative no Mercado Livre — o preço já é com ele')}"
    else:
        # Códigos genéricos (CupomCodigo) são de checkout do ML — NÃO valem na Amazon.
        mkt = getattr(produto, "marketplace", "mercadolivre")
        codigo = None
        if mkt in ("mercadolivre", ""):
            do_catalogo = _melhor_cupom_normalizado_obj(produto, usuario=usuario)
            if do_catalogo is not None:
                from apps.scrapers.coupon_rules import codigo_publicavel
                codigo = codigo_publicavel(do_catalogo) or None
                cupom_escolhido = do_catalogo if codigo else None
                # Cupom de ATIVAÇÃO (clique, sem código digitável) é hoje a quase
                # totalidade do catálogo de campanhas do ML. Sem este ramo, um
                # produto com cupom confirmado saía sem nenhuma linha 🎟️ — o
                # sintoma que a cliente relatou como "cupom não vem na mensagem".
                # O portão de confiança já foi aplicado em
                # `_melhor_cupom_normalizado_obj`: só chega aqui cupom de site
                # inteiro ou com `ProdutoCupom` confirmado para ESTE item.
                if not codigo:
                    from apps.scrapers.coupon_rules import ativacao_publicavel
                    if ativacao_publicavel(do_catalogo, usuario=usuario):
                        cupom_escolhido = do_catalogo
                        linha_cupom = f"🎟️ {m.bold('CUPOM: ative no link')}"
            if linha_cupom is None and not codigo:
                codigo, cupom_escolhido = _melhor_codigo(produto), None
        if codigo:
            linha_cupom = f"🎟️ {m.bold(f'CUPOM: {esc(codigo)}')}"

    if linha_cupom:
        aviso_minimo = _aviso_minimo_nao_atingido(cupom_escolhido, produto)
        if aviso_minimo:
            linha_cupom = f"{linha_cupom} — {esc(aviso_minimo)}"
        # Com cupom: cola embaixo do preço e separa o link com uma linha em branco.
        linhas.append(linha_cupom)
        # Cupom que vale para um recorte (categoria, marca, container) sai com o
        # recorte escrito. A fonte oficial entrega esse texto — "Vehicle Parts &
        # Accessories" no cupom que gerou a reclamação — e a mensagem o descartava,
        # anunciando um código nu. Quem recebe não tem como saber que o desconto é
        # de um departamento só, e a oferta "não funciona" no checkout.
        escopo = _escopo_do_cupom(cupom_escolhido)
        if escopo:
            linhas.append(f"📌 {m.bold('Vale em:')} {esc(escopo)}")
        # Cupom com restrição de público (primeira compra, app, cartão, pix) só pode
        # ser anunciado junto da condição — sem isso a mensagem promete a quem não
        # tem direito e a oferta "não funciona" no checkout.
        condicao = _condicao_do_cupom(cupom_escolhido)
        if condicao:
            linhas.append(f"⚠️ {m.bold('Condição:')} {esc(condicao)}")
        validade = _linha_validade_cupom(cupom_escolhido)
        if validade:
            linhas.append(f"⏳ {m.bold(esc(validade))}")
        linhas.append("")
    linhas.append(f"👉 {m.bold(esc(cta))}")
    linhas.append(f"🔗 {esc(link_afiliado)}")
    if marca and marca.casefold() != "ofertas":
        linhas.extend(["", m.italic(esc(marca))])
    if disclosure:
        linhas.append(m.italic(esc(disclosure)))
    return "\n".join(linhas)


def _escopo_do_cupom(cupom) -> str:
    """Recorte de produtos do cupom p/ a mensagem; '' quando vale para tudo.

    Cupom de site inteiro não ganha linha nenhuma: dizer "vale em todos os
    produtos" só ocupa espaço. Os demais saem com o texto que a fonte publicou.
    """
    if cupom is None or not hasattr(cupom, "regras"):
        return ""
    from apps.scrapers.coupon_rules import escopo_produtos_cupom, regras_do_cupom

    if regras_do_cupom(cupom).get("is_mar_aberto"):
        return ""
    return escopo_produtos_cupom(cupom)[:120]


def _condicao_do_cupom(cupom) -> str:
    """Texto da condição de uso quando o cupom é restrito; '' quando não é."""
    if cupom is None or not getattr(cupom, "restrito", False):
        return ""
    from apps.scrapers.coupon_rules import regras_do_cupom
    escopo = ""
    if hasattr(cupom, "regras"):
        escopo = str(regras_do_cupom(cupom).get("escopo") or "")
    return _condicao_legivel(escopo or "Consulte quem pode usar antes de comprar")


def _aviso_minimo_nao_atingido(cupom, produto) -> str:
    """Condição de carrinho para item que, sozinho, não alcança o mínimo."""
    if cupom is None or produto is None or not hasattr(cupom, "regras"):
        return ""
    from apps.scrapers.coupon_rules import regras_do_cupom

    try:
        minimo = float(regras_do_cupom(cupom).get("valor_minimo") or 0)
        atual = float(getattr(produto, "preco_com_cupom", 0) or 0)
    except (TypeError, ValueError):
        return ""
    if minimo <= 0 or atual >= minimo:
        return ""
    return f"válido em compras acima de R${_preco_br(minimo)}"


# Back-compat: chamadas antigas continuam funcionando (markup WhatsApp default).
def montar_mensagem_whatsapp(produto, link_afiliado: str, cupom_pai) -> str:
    return montar_mensagem(produto, link_afiliado, cupom_pai)


def _desconto_em_reais(preco, tipo, valor, teto=None):
    """Desconto de um cupom convertido para R$, respeitando o teto.

    Comparar "20%" com "R$ 50" pelo número cru elegia sempre o desconto fixo, mesmo
    num item de R$ 3.000 onde os 20% valem R$ 600. E sem o teto o valor anunciado
    ficava acima do que o ML realmente abate.
    """
    try:
        valor = float(valor or 0)
        preco = float(preco or 0)
    except (TypeError, ValueError):
        return 0.0
    if valor <= 0:
        return 0.0
    bruto = preco * valor / 100.0 if str(tipo).lower() in (
        "porcentagem", "percentual") else valor
    try:
        teto = float(teto) if teto not in (None, "") else 0.0
    except (TypeError, ValueError):
        teto = 0.0
    if teto > 0:
        bruto = min(bruto, teto)
    return min(bruto, preco)


def _melhor_codigo(produto):
    """
    Devolve o ÚNICO melhor código de checkout VÁLIDO para este item (ou None).

    Cupons não acumulam: escolhemos um só. Filtra por categoria/mínimo/validade
    via CupomCodigo.aplica_em e prioriza o de maior desconto percentual estimado.
    """
    from apps.scrapers.models import CupomCodigo
    # Códigos descobertos por regex na página do ML não possuem vínculo comprovado
    # com o produto. Permanecem no catálogo, mas nunca entram automaticamente.
    #
    # `valor_desconto > 0` não é refinamento de ranking, é portão: a coluna nasce
    # em 0.0 e o scraper antigo gravava a linha sem preencher desconto nenhum. Um
    # código desses tem `categorias` vazio (vale para todas), `valor_minimo` 0 e
    # `validade` nula — ou seja, `aplica_em` diz sim para QUALQUER produto — e o
    # `max()` abaixo, comparando zeros, elegia sempre o mesmo. Resultado: um código
    # órfão carimbado em todas as ofertas do dia, sem desconto nenhum para
    # prometer.
    candidatos = [c for c in CupomCodigo.objects.filter(
        ativo=True, automatico=False, valor_desconto__gt=0)
        if c.aplica_em(produto)]
    if not candidatos:
        return None

    melhor = max(candidatos, key=lambda c: _desconto_em_reais(
        produto.preco_com_cupom, c.tipo_desconto, c.valor_desconto))
    return f"{melhor.codigo} — {melhor.descricao}" if melhor.descricao else melhor.codigo


def _melhor_cupom_normalizado_obj(produto, *, usuario=None):
    """Melhor CupomNormalizado (catálogo das fontes) VÁLIDO p/ este item ML, ou None.

    GATE DE CONFIANÇA: só entra na mensagem um cupom cuja aplicação a ESTE produto é
    segura — ou ele vale para o site inteiro (regras.is_mar_aberto), ou existe um
    ProdutoCupom 'confirmado' ligando os dois. Cupom de container/categoria sem match
    confirmado NÃO entra: melhor não anunciar cupom do que colar um que o produto não
    aceita no checkout. A compra mínima é condição de carrinho e segue no texto da
    mensagem, sem apagar a associação do produto. Escolhe o de maior desconto —
    convertido para R$ e já com o teto aplicado, senão um "R$ 50 OFF"
    ganhava de "20% OFF" mesmo num item de R$ 3.000.

    Aceita tanto cupom de CÓDIGO quanto de ATIVAÇÃO (`cupom_publicavel`). Exigir
    código digitável descartava praticamente todo o catálogo de campanhas do ML, que
    migrou para ativação por clique — e o produto saía sem linha de cupom nenhuma.
    Empate no desconto vai para o código, que é o que o comprador consegue conferir
    no checkout.

    Devolve o objeto (não só o código) porque a mensagem precisa saber se o cupom é
    restrito para imprimir a linha de condição.
    """
    from apps.scrapers.models import CupomNormalizado, ProdutoCupom
    from apps.scrapers.coupon_rules import (
        codigo_publicavel, codigos_com_escopo_contestado, cupom_publicavel,
        cupons_visiveis_q, escopo_delimitado, regras_do_cupom,
        site_wide_confiavel,
    )
    from apps.scrapers.maintenance import cupons_frescos_q
    if getattr(produto, "marketplace", "mercadolivre") not in ("mercadolivre", ""):
        return None
    agora = timezone.now()
    base = CupomNormalizado.objects.filter(
        marketplace="mercadolivre", estado="ativo",
    ).filter(cupons_visiveis_q(usuario)).filter(cupons_frescos_q(agora=agora))

    ids_confirmados = set()
    if getattr(produto, "pk", None):
        ids_confirmados = set(ProdutoCupom.objects.filter(
            produto=produto, status="confirmado", cupom__in=base,
        ).values_list("cupom_id", flat=True))

    preco = getattr(produto, "preco_com_cupom", 0) or 0
    melhor, melhor_chave = None, None
    # Uma passada pelo catálogo já carregado: evita uma consulta por cupom dentro
    # do laço só para descobrir se a alegação de site inteiro é contestada.
    contestados = codigos_com_escopo_contestado(base)
    for c in base:
        regras = regras_do_cupom(c)
        site_inteiro = site_wide_confiavel(c, codigos_contestados=contestados)
        if not (site_inteiro or c.id in ids_confirmados):
            continue
        # Segundo portão, e o que faltava: um `ProdutoCupom` confirmado só vale
        # como prova se o cupom delimita algum conjunto de produtos. Quando ele
        # não delimita, a "prova" foi colhida numa vitrine genérica do ML e liga o
        # código a qualquer item que estivesse na página naquele minuto. As relações
        # fabricadas assim são expiradas pela migração 0064; esta guarda impede que
        # qualquer caminho novo volte a publicá-las.
        if not escopo_delimitado(c, codigos_contestados=contestados):
            continue
        if not cupom_publicavel(c):
            continue
        desc = _desconto_em_reais(preco, regras.get("tipo_desconto"),
                                  regras.get("valor_desconto"),
                                  regras.get("desconto_maximo"))
        if desc <= 0:
            continue
        chave = (desc, 1 if codigo_publicavel(c) else 0)
        if melhor_chave is None or chave > melhor_chave:
            melhor, melhor_chave = c, chave
    return melhor


def _melhor_cupom_normalizado(produto):
    """Código publicável do melhor cupom do catálogo p/ este item, ou None."""
    from apps.scrapers.coupon_rules import codigo_publicavel
    melhor = _melhor_cupom_normalizado_obj(produto)
    return (codigo_publicavel(melhor) or None) if melhor else None


def _baixar_imagem_b64(url):
    """
    Baixa a imagem e converte p/ JPEG -> (base64, 'image/jpeg').
    Converte porque o whatsapp-web.js falha ao enviar webp (formato padrão do ML).
    ('', '') se falhar/sem url.

    Reusa o download da colagem (valida o host antes do GET, recusa redirect para
    endereço não público e corta o corpo no teto) e o encoder dela, que reduz a
    foto até caber no orçamento de upload do worker. Este caminho fazia o oposto
    dos dois: GET direto na URL raspada e JPEG na resolução original da loja.
    """
    if not url or not url.startswith("http"):
        return "", ""
    from apps.scrapers.colagem import _baixar_imagem, preparar_jpeg_b64
    try:
        img = _baixar_imagem(url)
        if img is None:
            # Sem foto o envio continua (vai só texto), então isto não pode ser
            # mudo: é a diferença entre "a loja não respondeu" e "a URL foi
            # recusada pela validação" quando alguém for investigar depois.
            logger.info("Foto da oferta não pôde ser usada (%s).", url[:120])
            return "", ""
        return preparar_jpeg_b64(img)
    except Exception as e:
        logger.debug("Falha ao processar imagem da oferta: %s", e)
        return "", ""


def _link_publicado(publicacao, link_afiliado: str) -> str:
    """Link que entra na mensagem enviada ao grupo: sempre o link de afiliado
    direto (meli.la / amazon.com.br).

    Uma URL do sistema (spreading-web.fly.dev/r/...) na mensagem denuncia
    promoção automatizada — decisão de produto. O custo aceito é a contagem
    interna de cliques parar nos envios novos; a comissão continua vindo dos
    relatórios das lojas. O redirecionador (/r/<slug>/ e /scrapers/r/<token>/)
    segue no ar só para as mensagens já publicadas.
    """
    return link_afiliado


def _variante_para_envio(configuracao) -> str:
    """Escolhe A/B por exposicoes, sem deixar falhas enviesarem o teste."""
    if not configuracao or configuracao.variante_template == "A":
        return "A"
    if configuracao.variante_template == "B":
        return "B"
    from django.db.models import Count

    contagens = {
        row["variante"]: row["total"]
        for row in configuracao.publicacoes.filter(
            status__in=("enviado", "incerto"), variante__in=("A", "B"),
        ).values("variante").annotate(total=Count("id"))
    }
    # Empate comeca em A; depois escolhe a menos exposta. Falhas anteriores ao
    # transporte nao contam porque o publico nunca viu aquela variante.
    return "B" if contagens.get("A", 0) > contagens.get("B", 0) else "A"


def enviar_oferta_de_produto(produto, grupo_id, verificar=True, dry_run=False,
                             canal="whatsapp", usuario=None, configuracao=None,
                             destino_nome="", imagem_b64_custom=None,
                             enqueue_only=False, _reserved_publication=None,
                             deal=None):
    """
    Núcleo de envio reutilizável e AGNÓSTICO de loja/canal:
      resolve marketplace (link afiliado + verificação) e sender (transporte) via registry.
      garante link -> checa tag afiliado (A3) -> (opcional) verifica destino -> monta msg
      no markup do canal -> envia. Grava HistoricoEnvio SOMENTE em envio bem-sucedido.

    Retorna dict: {sucesso, motivo?, link?, mensagem?, verificacao?, via?}
    """
    from django.conf import settings
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.senders.registry import get_sender
    from apps.scrapers.eventos import log_event

    mp = get_marketplace(getattr(produto, "marketplace", "mercadolivre"))
    try:
        sender = get_sender(canal)
    except ValueError as exc:
        return {"sucesso": False, "motivo": str(exc), "classe": "permanente"}
    publicacao = _reserved_publication
    _executar_orm(
        log_event,
        "publicacao", "send_started", f"Preparando envio para {destino_nome or grupo_id}.",
        usuario=usuario,
        contexto={
            "produto_id": getattr(produto, "id", None),
            "marketplace": getattr(produto, "marketplace", ""),
            "canal": canal,
            "destino": destino_nome or grupo_id,
        },
    )
    if usuario is not None and publicacao is None:
        from django.contrib.auth import get_user_model
        agora_abertura = timezone.now()

        def _reservar():
            """Transação curta de reserva: lock do usuário, cota, deduplicação e a
            Publicacao pendente. Via _executar_orm para não herdar a transação
            longa do chamador (ver _sse_runner/segurar_transacao)."""
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=usuario.pk)
                # O lock do usuário já serializa cota e deduplicação. O catálogo do ML,
                # porém, é pool COMPARTILHADO (owner=None): o RLS o deixa legível por
                # qualquer tenant, mas `FOR UPDATE` exige a policy de ESCRITA e o
                # PostgreSQL esconde a linha como se não existisse — era isto que
                # derrubava TODO envio de oferta do ML com o erro genérico do SSE.
                # Este fluxo não escreve no produto: relê só p/ confirmar que ele ainda
                # existe e bloqueia apenas o item privado (Amazon do próprio usuário).
                # É o mesmo tratamento que o caminho de cupom já recebeu acima.
                produto_qs = Produto.objects.filter(pk=produto.pk)
                if getattr(produto, "owner_id", None) is None:
                    atual = produto_qs.filter(owner__isnull=True).first()
                else:
                    atual = produto_qs.filter(
                        owner_id=produto.owner_id).select_for_update().first()
                if not atual:
                    return {"sucesso": False,
                            "motivo": "Esta oferta foi atualizada e não está mais "
                                      "disponível. Atualize a tela e tente outra.",
                            "classe": "permanente", "produto_atualizado": True}
                perfil = getattr(usuario, "perfil", None)
                inicio_dia = timezone.localtime(agora_abertura).replace(
                    hour=0, minute=0, second=0, microsecond=0)
                limite = perfil.cota_max_envios_dia() if perfil else 0
                usados = Publicacao.objects.filter(
                    usuario=usuario, criada_em__gte=inicio_dia,
                    status__in=("pendente", "enviado", "incerto"),
                ).count()
                if perfil and perfil.bloqueado:
                    return {"sucesso": False, "motivo": "Conta bloqueada para envios.",
                            "classe": "permanente"}
                if limite and usados >= limite:
                    return {"sucesso": False, "motivo": "Limite diário de envios atingido.",
                            "classe": "permanente"}
                desde = agora_abertura - timedelta(hours=24)
                recente = Publicacao.objects.filter(
                    usuario=usuario, origem="produto", produto=produto,
                    canal=canal, destino_id=grupo_id,
                ).filter(
                    Q(status="pendente", criada_em__gte=agora_abertura - timedelta(minutes=30))
                    | Q(status="enviado", enviada_em__gte=desde)
                    | Q(status="incerto", criada_em__gte=desde)
                ).order_by("-criada_em").first()
                if recente and preco_publicavel(produto) > recente.preco_final * .95:
                    motivo = ("Esta oferta já está sendo enviada para o destino."
                              if recente.status == "pendente"
                              else "Este destino recebeu a oferta nas últimas 24h.")
                    return {"sucesso": False, "motivo": motivo, "duplicado": True,
                            "classe": "permanente"}
                return Publicacao.objects.create(
                    usuario=usuario, origem="produto", produto=produto,
                    configuracao=configuracao, canal=canal,
                    destino_id=str(grupo_id or "")[:100],
                    destino_nome=str(destino_nome or "")[:255],
                    preco_original=produto.preco_sem_desconto,
                    preco_final=preco_publicavel(produto),
                    categoria=produto.macro_categoria or produto.categoria or "",
                    score=getattr(produto, "score_oferta", 0),
                    motivos_score=getattr(produto, "motivos_score", []),
                )

        # Reservar o envio é escrita em banco: se falhar, o usuário precisa de um
        # motivo, não do erro genérico do runner SSE (nada foi enviado aqui).
        try:
            reserva = _executar_orm(_reservar)
        except Exception as exc:
            logger.exception("Falha ao reservar envio do produto %s", produto.pk)
            _executar_orm(
                log_event,
                "publicacao", "offer_reservation_failed",
                "Não foi possível reservar o envio da oferta.", level="error",
                usuario=usuario, contexto={
                    "produto_id": getattr(produto, "id", None),
                    "marketplace": getattr(produto, "marketplace", ""),
                    "canal": canal, "destino": destino_nome or grupo_id,
                    "causa": type(exc).__name__,
                }, exc=exc,
            )
            return {"sucesso": False,
                    "motivo": "Não foi possível reservar esta oferta para envio. "
                              "Atualize a tela e tente novamente.",
                    "classe": "transitorio", "causa": type(exc).__name__}
        if isinstance(reserva, dict):
            return reserva
        publicacao = reserva
    if enqueue_only:
        if publicacao is None:
            return {"sucesso": False, "motivo": "Envio sem usuário não pode ser enfileirado.",
                    "classe": "permanente"}
        from apps.scrapers.send_pipeline import queue_publications
        try:
            queue_publications(
                [publicacao], image_b64=imagem_b64_custom, mime="image/jpeg",
            )
        except ValueError as exc:
            Publicacao.objects.filter(pk=publicacao.pk, status="pendente").update(
                status="falhou", stage="rejected", transport_state="invalid_payload",
                erro=str(exc)[:500],
            )
            return {"sucesso": False, "motivo": str(exc), "classe": "permanente"}
        return {
            "sucesso": True, "queued": True, "publicacao": publicacao,
            "motivo": "Envio reservado e aguardando o worker.",
        }
    transport_context = {}
    def falhar(motivo, **extra):
        erro_tecnico = extra.pop("_erro_tecnico", "")
        pipeline_recorded = extra.pop("_pipeline_recorded", False)
        texto_motivo = str(motivo).lower()
        etapa = extra.get("etapa") or ""
        causa = extra.get("causa") or (
            "whatsapp_preflight_timeout" if etapa == "getState" and (extra.get("falha_infra") or "timeout" in texto_motivo) else
            "whatsapp_grupo_timeout" if etapa == "verificar_grupo" and (extra.get("falha_infra") or "timeout" in texto_motivo) else
            "whatsapp_store_recarregado" if etapa == "verificar_store" or "módulos internos" in texto_motivo else
            "whatsapp_frame_recarregado" if "frame" in texto_motivo or "recarregando" in texto_motivo else
            "whatsapp_confirmacao" if "confirma" in texto_motivo or "ack" in texto_motivo else
            "link_afiliado_recusado" if "link de afiliado" in texto_motivo or "link builder" in texto_motivo else
            "link_reprovado" if "link reprovado" in texto_motivo else
            "marketplace_login" if extra.get("precisa_login_ml") else
            "publicacao_falhou"
        )
        incerto = bool(extra.get("resultado") == "incerto")
        status = "incerto" if incerto else "falhou"
        contexto = {
            "produto_id": getattr(produto, "id", None),
            "marketplace": getattr(produto, "marketplace", ""),
            "canal": canal,
            "destino": destino_nome or grupo_id,
            "publicacao_id": getattr(publicacao, "id", None),
            "causa": causa,
            "erro_tecnico": erro_tecnico,
            **extra,
        }

        def _fechar_e_logar():
            if publicacao and not pipeline_recorded:
                Publicacao.objects.filter(pk=publicacao.pk).update(
                    status=status, erro=str(motivo)[:500])
            log_event(
                "publicacao", "send_failed", str(motivo), level="warning",
                usuario=usuario, contexto=contexto,
            )
            if extra.get("falha_infra") or incerto:
                log_event(
                    "whatsapp", "send_timeout",
                    "Serviço WhatsApp não confirmou o envio dentro do prazo.",
                    level="error", usuario=usuario, contexto=contexto,
                )
        _executar_orm(_fechar_e_logar)
        return {"sucesso": False, "motivo": str(motivo), **extra}

    from apps.scrapers.auxiliar import BrowserError, SessaoExpirada
    from apps.scrapers.carga import BrowserResourceUnavailable
    from apps.scrapers.scraper_mercadolivre.link import LoginError, AuthError

    # O trabalho roda aninhado para que QUALQUER exceção inesperada (a Publicacao já
    # existe como 'pendente' neste ponto) feche a linha antes de propagar. Sem isto,
    # um erro não previsto deixa a publicação pendente para sempre no dashboard.
    def _executar():
        try:
            info = mp.build_affiliate_link(produto, usuario=usuario)
        except BrowserResourceUnavailable as e:
            # Fila de navegador, não erro de envio. Precisa de um `except` PRÓPRIO
            # porque a classe é RuntimeError (carga.py), não BrowserError — sem ele
            # a exceção subia por `enviar_oferta_de_produto` ->
            # `processar_configs_de_envio` -> `_loop_envio` e derrubava o tick
            # INTEIRO: as configurações seguintes, de TODOS os usuários e não só do
            # dono deste produto, não eram avaliadas, e esta nem chegava a chamar
            # `agendar_proximo`. Em produção o rastro terminava sempre em
            # `carga.coordinated_ml_browser`.
            #
            # TRANSITÓRIO pelo mesmo motivo da sessão expirada: a vaga volta no
            # próximo ciclo e não há nada errado com a regra de envio. Punir a regra
            # aqui a desligaria sozinha depois de cinco disputas de navegador.
            logger.info(
                "Envio adiado para o produto %s: navegador ocupado por outra tarefa.",
                produto.pk,
            )
            return falhar(
                "Navegador ocupado por outra tarefa; o envio será retomado.",
                classe=TRANSITORIO, _erro_tecnico=str(e),
            )
        except (LoginError, AuthError, SessaoExpirada) as e:
            # Sessão do ML caída: sem link de afiliado NENHUM produto sai. Motivo claro
            # + flag p/ a UI oferecer a reconexão e o chamador parar de retentar.
            #
            # TRANSITÓRIO, como no caminho de cupom: a sessão volta quando o usuário
            # reconecta, e nada na regra de envio está errado. Sem a classe, cada
            # tick contava uma falha "permanente" e em cinco a automação se
            # desligava sozinha (`ativo=False`) — em produção foi exatamente isso
            # que parou TODOS os envios, e religá-la exige ação manual mesmo depois
            # da reconexão. O gate do WhatsApp já pula sem punir a regra pela mesma
            # razão.
            logger.warning("Sessão ML expirada ao afiliar produto %s: %s", produto.pk, e)
            return falhar("Sessão do Mercado Livre expirada. Reconecte sua conta.",
                          classe=TRANSITORIO, precisa_login_ml=True,
                          _erro_tecnico=str(e))
        except BrowserError as e:
            texto = str(e)
            logger.warning("Falha do navegador ao afiliar produto %s: %s", produto.pk, e)
            precisa_login = "LOGIN_REQUIRED" in texto
            # Só o caso de sessão é transitório. O resto do BrowserError inclui
            # ML_LINK_BUILDER_ENABLED desligada, que não se resolve sozinha nunca:
            # classificá-la como transitória faria a regra bater no mesmo erro a
            # cada tick, para sempre, sem nada no painel escalar.
            classe = {"classe": TRANSITORIO} if precisa_login else {}
            return falhar(
                "Sessão do Mercado Livre expirada. Reconecte sua conta."
                if precisa_login else _motivo_navegador(texto),
                precisa_login_ml=precisa_login, _erro_tecnico=texto, **classe)
        if not info:
            return falhar("falha ao gerar link de afiliado "
                          "(URL não afiliável ou o Link Builder recusou)")
        link = info["link_afiliado"]

        # Fonte única do veredito: quando o link já foi APROVADO na verificação de
        # destino (na geração/reverificação), o envio confia nele e usa EXATAMENTE a
        # url_canonica aprovada — sem reconstruir o link nem reconferir com uma
        # segunda regra que poderia divergir. É isto que garante que "exibido como
        # enviável" e "aceito no envio" sejam a mesma coisa.
        verificado_ok = info.get("verificado_ok")
        if verificado_ok is True and info.get("url_canonica"):
            link = info["url_canonica"]

        # A3 — sem tag de afiliado o clique não gera comissão. Recusa (ou avisa).
        afiliado_ok = info.get("afiliado_ok")
        if afiliado_ok is None:
            afiliado_ok = mp.verify_affiliate_tag(link, usuario=usuario)
        if not afiliado_ok:
            if getattr(settings, "AFILIADO_EXIGIR", True):
                return falhar("link sem tag de afiliado — não enviado", link=link)
            logger.warning("Link sem tag de afiliado; envio permitido por configuracao")

        verificacao = None
        origem = getattr(produto, "origem", "cupom")
        confiar = origem in ("oferta", "busca")
        if verificar and verificado_ok is True:
            # Já aprovado pela fonte única: não reverifica ao vivo (evita a segunda
            # implementação divergente) e envia a url_canonica.
            verificacao = {"ok": True, "cache": True, "url_final": link}
        elif verificar:
            # Link ainda sem veredito (ex.: envio automático que não passou pela
            # tela): confere ao vivo com a MESMA regra e PERSISTE o resultado, para
            # o item nunca mais aparecer enviável se for reprovado.
            # 'oferta'/'busca' têm de/por confirmado na raspagem; 'cupom_codigo' precisa
            # confirmar o desconto/badge na PDP (confiar_desconto=False).
            try:
                verificacao = mp.verify_link(link, nome_esperado=produto.nome,
                                             confiar_desconto=confiar, usuario=usuario)
            except (LoginError, AuthError, SessaoExpirada) as e:
                # Mesma semântica do build: sessão caída na verificação também precisa
                # marcar a Publicacao como falha e acionar a reconexão na UI.
                logger.warning("Sessão ML expirada ao verificar produto %s: %s", produto.pk, e)
                return falhar("Sessão do Mercado Livre expirada. Reconecte sua conta.",
                              precisa_login_ml=True, _erro_tecnico=str(e))
            except BrowserError as e:
                texto = str(e)
                logger.warning("Falha do navegador ao verificar produto %s: %s", produto.pk, e)
                precisa_login = "LOGIN_REQUIRED" in texto
                return falhar(
                    "Sessão do Mercado Livre expirada. Reconecte sua conta."
                    if precisa_login
                    else _motivo_navegador(texto, "Não foi possível verificar a oferta."),
                    precisa_login_ml=precisa_login, _erro_tecnico=texto)
            # Persiste o veredito na fonte única (self-heal): um link que reprova ao
            # vivo é marcado como inválido e some da tela de envio; um que aprova
            # fixa a url_canonica, para não reverificar da próxima vez.
            from apps.scrapers.afiliado import registrar_aprovacao, registrar_reprovacao
            if verificacao.get("ok"):
                _executar_orm(registrar_aprovacao, usuario, produto, link,
                              url_canonica=link)
            elif verificacao.get("transitorio"):
                # Verificação inconclusiva (CAPTCHA, timeout, DOM ausente) não é
                # veredito: persistir reprovação aqui gastava uma tentativa do item
                # e, na oitava, o marcava `nao_afiliavel` por um problema que nunca
                # foi dele. O link segue sem veredito e a lane tenta de novo.
                return falhar("verificação inconclusiva; tente novamente em instantes",
                              link=link, verificacao=verificacao)
            else:
                _executar_orm(registrar_reprovacao, usuario, produto,
                              _motivo_reprovacao_da_loja(mp, verificacao, confiar))
                return falhar("link reprovado na verificação",
                              link=link, verificacao=verificacao)

        # Preço ao vivo: depois da verificação (não faz sentido revalidar link
        # reprovado) e ANTES de montar a mensagem, para o texto gravado na
        # Publicacao ser exatamente o que foi enviado.
        #
        # Roda mesmo no caminho de cache acima (verificado_ok is True), e é isso que
        # tira o envio do "zero contato com a página": o veredito do LINK continua
        # vindo da fonte única, mas o PREÇO é medido agora, no `link` publicado —
        # um GET segue meli.la -> PDP e mede a página que o assinante vai abrir.
        if getattr(settings, "PRECO_REVALIDA_ANTES_ENVIO", True):
            from apps.scrapers import preco_ao_vivo
            checagem = _executar_orm(
                preco_ao_vivo.revalidar,
                produto, usuario=usuario, configuracao=configuracao, url=link,
                exigir_medicao=deal is not None)
            if not checagem["ok"]:
                return falhar(f"preço mudou antes do envio: {checagem['motivo']}",
                              link=link)
            # Para o caminho de DEAL, "inconclusivo" não pode virar publicação.
            # A política geral do preço ao vivo é deliberada — o ML devolve
            # challenge em rajadas para o IP de datacenter da Fly, e tratar isso
            # como reprovação pararia todos os envios. Mas ela foi escrita para
            # decidir SE envia, não para autorizar a mensagem a AFIRMAR um preço.
            # Um deal é exatamente uma afirmação de preço: "De R$ 289 por R$ 183,91"
            # com o catálogo desatualizado saiu para o grupo em 03/09/2026 enquanto
            # o checkout real cobrava R$ 249,50. Transitório de propósito: a
            # próxima janela costuma medir, e a regra de envio não tem culpa.
            if deal is not None and checagem.get("fonte") == "inconclusivo":
                # Sem medição AGORA não sai. Aceitar a observação salva era o
                # atalho que fez a mensagem anunciar R$ 199,90 num item de
                # R$ 249,50: aquele preço tinha 17 horas. São duas verificações,
                # uma na ingestão e uma aqui, e esta não tem substituto.
                return falhar(
                    "preço não medido no envio; deal não é publicado",
                    classe=TRANSITORIO, link=link)

        # Ofertas (origem='oferta') não têm Cupom; só busca quando há campanha_id
        cupom = None
        if produto.campanha_id:
            def _cupom_da_campanha():
                return Cupom.objects.filter(
                    campanha_id=produto.campanha_id, estado="ativo",
                ).filter(Q(validade__isnull=True) | Q(validade__gte=timezone.now())).first()
            cupom = _executar_orm(_cupom_da_campanha)
        variante = _executar_orm(_variante_para_envio, configuracao)
        link_publicado = _link_publicado(publicacao, link)
        if deal is not None:
            # A revalidação ao vivo confirmou a VITRINE (inconclusivo já abortou
            # acima). O deal foi
            # montado com o preço do catálogo, que pode ter mudado no meio do tick,
            # e um cupom percentual escala com ele: recalcular aqui é o que mantém
            # `preco_final = vitrine - benefício` verdadeiro na hora do envio, e não
            # só na hora da seleção.
            from apps.scrapers.deals import _beneficio_do_cupom
            deal.preco_vitrine = round(float(preco_publicavel(produto)), 2)
            if deal.cupom is not None:
                deal.beneficio_rs = round(
                    _beneficio_do_cupom(deal.cupom, deal.preco_vitrine), 2)
            deal.preco_final = round(deal.preco_vitrine - deal.beneficio_rs, 2)

        if publicacao:
            publicacao.variante = variante
            publicacao.link_afiliado = link
            publicacao.link_rastreado = link_publicado
            publicacao.cupom = (
                # Precedência: o cupom do deal manda, porque é ele que a
                # mensagem anuncia e é sobre ele que o preço final foi calculado.
                # Depois o cupom da campanha, depois o código de checkout, e por
                # último o rótulo de ativação inline do ML — que veio de
                # `ad46360` e cobre o caso em que o desconto existe na página e
                # não há código nenhum para citar.
                getattr(getattr(deal, "cupom", None), "titulo", "")
                or (cupom.titulo if cupom else "")
                or getattr(produto, "codigo_checkout", "")
                or (
                    "Ative no Mercado Livre — o preço já é com ele"
                    if _preco_cupom_inline_ml(produto) else ""
                )
            )
            # preco_final foi gravado antes da revalidação; realinhar aqui mantém
            # o registro igual ao número que a mensagem anuncia.
            publicacao.preco_final = (
                deal.preco_final if deal is not None else preco_publicavel(produto))
            publicacao.preco_original = produto.preco_sem_desconto
            _executar_orm(publicacao.save, update_fields=[
                "variante", "link_afiliado", "link_rastreado", "cupom",
                "preco_final", "preco_original"])
        if deal is not None:
            # Uma chamada de IA por tentativa REAL de envio, não por item de
            # catálogo — mesmo critério de `avaliar_cupom_ia`. Falha degrada para
            # texto vazio: preço, cupom e prova continuam impressos pelo código.
            from apps.scrapers.llm import gerar_texto_deal
            texto_ia = gerar_texto_deal(
                nome=getattr(produto, "nome", ""),
                categoria=getattr(produto, "macro_categoria", "")
                or getattr(produto, "categoria", "") or "",
                motivo="; ".join(getattr(deal, "motivos", [])[:2]),
                tem_cupom=bool(getattr(deal, "cupom", None)),
                **_fatos_do_deal(deal),
            )
            mensagem = _executar_orm(
                montar_mensagem_deal, deal, link_publicado,
                markup=sender.markup, texto_ia=texto_ia, usuario=usuario,
                configuracao=configuracao)
        else:
            mensagem = _executar_orm(
                montar_mensagem,
                produto, link_publicado, cupom, markup=sender.markup, usuario=usuario,
                configuracao=configuracao, variante=variante)
        if publicacao:
            publicacao.mensagem = mensagem
            _executar_orm(publicacao.save, update_fields=["mensagem"])

        if dry_run:
            if publicacao:
                publicacao.status = "ignorado"
                _executar_orm(publicacao.save, update_fields=["status"])
            return {"sucesso": True, "dry_run": True, "link": link,
                    "mensagem": mensagem, "verificacao": verificacao}

        # Sessão WhatsApp do DONO (multi-tenant): envia pela conexão dele, não pela default.
        wa_session = _executar_orm(wa_session_de, usuario)

        # Imagem conforme o canal: Telegram aceita URL direto; WhatsApp precisa de base64.
        # Foto custom (opcional, escolhida no envio) só entra no caminho base64/WhatsApp;
        # sem ela, mantém a foto do produto como sempre.
        foto_bytes = 0
        from apps.scrapers.send_pipeline import begin_transport, finish_transport
        if publicacao:
            publicacao_transporte, tentativa = _executar_orm(
                begin_transport, publicacao,
            )
            transport_context.update(
                publicacao=publicacao_transporte, tentativa=tentativa,
            )
            operation_id = publicacao_transporte.operation_key
        else:
            publicacao_transporte = tentativa = None
            operation_id = None
        if sender.prefers_image == "url" and not imagem_b64_custom:
            resultado = sender.enviar_oferta(grupo_id, mensagem,
                                             imagem_url=getattr(produto, "imagem_url", "") or None,
                                             legenda=mensagem, usuario=usuario, session=wa_session,
                                             operation_id=operation_id)
        else:
            if imagem_b64_custom:
                imagem_b64, img_mime = imagem_b64_custom, "image/jpeg"
            else:
                imagem_b64, img_mime = _baixar_imagem_b64(getattr(produto, "imagem_url", ""))
            # Tamanho da mídia no evento: é a variável que decide entre o envio
            # confirmado e o 'incerto' por estouro do prazo de upload, e sem ela
            # os dois desfechos ficam indistinguíveis no histórico.
            foto_bytes = len(imagem_b64 or "") * 3 // 4
            resultado = sender.enviar_oferta(grupo_id, mensagem, imagem_b64=imagem_b64 or None,
                                             mimetype=img_mime or "image/jpeg", legenda=mensagem,
                                             usuario=usuario, session=wa_session,
                                             operation_id=operation_id)

        if publicacao_transporte:
            _executar_orm(
                finish_transport, publicacao_transporte, tentativa, resultado,
                duration_ms=resultado.get("duracao_ms", 0),
            )
            transport_context.clear()

        if resultado.get("sucesso"):
            def _gravar_envio():
                HistoricoEnvio.objects.create(produto=produto, usuario=usuario)  # só após sucesso
                if publicacao:
                    Publicacao.objects.filter(pk=publicacao.pk).update(
                        status="enviado", enviada_em=timezone.now())
                log_event(
                    "publicacao", "send_ok", "Oferta publicada com sucesso.",
                    usuario=usuario,
                    contexto={
                        "produto_id": getattr(produto, "id", None),
                        "marketplace": getattr(produto, "marketplace", ""),
                        "canal": canal,
                        "destino": destino_nome or grupo_id,
                        "via": resultado.get("via"),
                        "publicacao_id": getattr(publicacao, "id", None),
                        "foto_bytes": foto_bytes,
                        "duracao_ms": resultado.get("duracao_ms", 0),
                    },
                )
            _executar_orm(_gravar_envio)
            return {"sucesso": True, "link": link, "mensagem": mensagem,
                    "via": resultado.get("via"), "verificacao": verificacao,
                    "canal": resultado.get("canal", canal),
                    "mensagem_id": resultado.get("mensagem_id"),
                    "classe": resultado.get("classe", ""),
                    "resultado": resultado.get("resultado", "confirmado"),
                    "repetir": resultado.get("repetir", False),
                    "etapa": resultado.get("etapa", "transporte"),
                    "duracao_ms": resultado.get("duracao_ms", 0),
                    # Como o transporte terminou, e não só que terminou. `ack` é a
                    # prova do WhatsApp de que a mensagem saiu daqui; sem ela o
                    # envio foi aceito mas ainda não confirmado, e a tela precisa
                    # dizer qual dos dois foi.
                    "confirmacao": resultado.get("confirmacao", ""),
                    "ack": resultado.get("ack"),
                    "ack_ms": resultado.get("ack_ms"),
                    "transporte_ms": resultado.get("transporte_ms"),
                    "enviado_em": resultado.get("enviado_em", ""),
                    "publicacao": publicacao}
        # `classe` decide se esta falha conta contra a config (ver
        # processar_configs_de_envio). Sem propagá-la aqui, toda falha de envio
        # chegaria ao orquestrador como 'desconhecido' e a taxonomia não valeria nada.
        return falhar(_motivo_publico_transporte(resultado),
                      _erro_tecnico=resultado.get("erro") or "",
                      _pipeline_recorded=bool(publicacao_transporte),
                      link=link, verificacao=verificacao,
                      classe=resultado.get("classe"),
                      resultado=resultado.get("resultado"),
                      repetir=resultado.get("repetir"),
                      etapa=resultado.get("etapa"),
                      duracao_ms=resultado.get("duracao_ms"),
                      # É aqui que o número importa: 'incerto' com foto grande e
                      # duracao_ms no teto é estouro de upload, não canal quebrado.
                      foto_bytes=foto_bytes,
                      falha_infra=resultado.get("falha_infra", False))

    try:
        return _executar()
    except Exception as e:
        # Fecha a linha SÓ se ainda estiver pendente: uma exceção posterior ao desfecho
        # (ex.: no log do sucesso) não pode reescrever um envio que já deu certo. Depois
        # re-levanta — o estado no banco fica honesto sem alterar o fluxo de controle
        # que os chamadores (e o loop de automacao) já esperam.
        motivo = f"erro inesperado no envio: {e}"

        if transport_context:
            from apps.scrapers.send_pipeline import finish_transport
            incerto = {
                "sucesso": False, "resultado": "incerto", "repetir": False,
                "etapa": "transport_started", "causa": type(e).__name__,
            }
            try:
                _executar_orm(
                    finish_transport, transport_context["publicacao"],
                    transport_context["tentativa"], incerto,
                )
            except Exception:
                logger.exception(
                    "Falha ao registrar resultado incerto da publicação %s",
                    getattr(publicacao, "pk", None),
                )

        def _fechar_inesperado():
            if publicacao and Publicacao.objects.filter(
                pk=publicacao.pk, status="pendente",
            ).update(status="falhou", erro=motivo[:500]):
                log_event(
                    "publicacao", "send_failed", motivo, level="warning", usuario=usuario,
                    contexto={
                        "produto_id": getattr(produto, "id", None),
                        "marketplace": getattr(produto, "marketplace", ""),
                        "canal": canal,
                        "destino": destino_nome or grupo_id,
                        "publicacao_id": publicacao.id,
                        "causa": "publicacao_inesperada",
                    },
                )
        _executar_orm(_fechar_inesperado)
        raise


def wa_session_de(usuario):
    """Sessão WhatsApp do dono (multi-tenant). None = sem dono (pool legado)."""
    if usuario is None:
        return None
    perfil = getattr(usuario, "perfil", None)
    if perfil is not None:
        return perfil.sessao_whatsapp()
    return str(getattr(usuario, "id", "")) or None


def selecionar_e_enviar(macros, grupo_id, min_desconto_percent=15.0,
                        horas_cooldown=24, max_tentativas=8, verificar=True, dry_run=False,
                        termo=None, canal="whatsapp", marketplace=None, usuario=None,
                        configuracao=None, destino_nome="", enqueue_only=False):
    """
    Seleciona um POOL de candidatos do nicho e tenta enviar um por um até o primeiro
    que passa na verificação. Devolve o resultado do envio bem-sucedido, ou o último
    erro / 'sem item elegível'. Evita abortar por causa de um único item que reprova.
    """
    if configuracao is not None:
        from apps.scrapers.content_ranking import selecionar_conteudo_para_grupo
        pool = selecionar_conteudo_para_grupo(configuracao, limit=max_tentativas)
    else:
        pool = selecionar_item_para_grupo(
            macros_selecionadas=macros,
            limite_envio=max_tentativas,
            horas_cooldown=horas_cooldown,
            min_desconto_percent=min_desconto_percent,
            termo=termo,
            marketplace=marketplace,
            usuario=usuario,
            grupo_id=grupo_id,
        )
    if not pool:
        # Estoque vazio não é defeito da regra: resolve sozinho quando o scrape
        # traz produto novo. Marcar como transitório é o que impede a config de
        # nicho estreito de se autodesligar por simples falta de oferta.
        return {"sucesso": False, "motivo": "sem item elegível", "classe": TRANSITORIO}

    ultimo = None
    for entry in pool:
        candidate = entry if hasattr(entry, "kind") else None
        deal_atual = candidate.obj if candidate and candidate.kind == "deal" else None
        prod = deal_atual.produto if deal_atual else (
            candidate.obj if candidate else entry)
        logger.debug(
            "Tentando enviar conteúdo id=%s origem=%s marketplace=%s",
            getattr(prod, "id", None), getattr(prod, "origem", "cupom"),
            getattr(prod, "marketplace", "?"),
        )
        if candidate and candidate.kind == "coupon":
            r = enviar_cupom(
                prod, grupo_id, canal=canal, usuario=usuario,
                configuracao=configuracao, destino_nome=destino_nome,
                score=candidate.score, motivos_score=candidate.reasons,
                enqueue_only=enqueue_only)
        else:
            if candidate:
                prod.score_oferta = candidate.score
                prod.motivos_score = candidate.reasons
            r = enviar_oferta_de_produto(
                prod, grupo_id, verificar=verificar, dry_run=dry_run, canal=canal,
                usuario=usuario, configuracao=configuracao, destino_nome=destino_nome,
                enqueue_only=enqueue_only, deal=deal_atual)
        if r.get("sucesso"):
            return r
        logger.debug("Produto id=%s reprovado no envio: %s", getattr(prod, "id", None), r.get("motivo"))
        ultimo = r
        if r.get("precisa_login_ml"):
            # Sessão do ML caiu: os demais candidatos falhariam igual (cada tentativa
            # abre um browser e leva ~30s). Aborta e devolve o motivo real.
            return r
        if r.get("classe") == TRANSITORIO:
            # Mesma lógica do precisa_login_ml, para o outro lado do envio: o
            # WhatsApp caiu (ou o worker piscou) no meio do tick. Insistir nos 7
            # candidatos restantes custa ~30s de Playwright cada para colecionar
            # a mesma falha 8 vezes — e enchia o histórico de Publicacao 'falhou'.
            return r
    return ultimo or {"sucesso": False, "motivo": "nenhum candidato passou"}


def processar_configs_de_envio():
    """
    Percorre ConfiguracaoEnvio ativas; para cada uma vencida (now - ultimo_envio >=
    intervalo), seleciona 1 item do nicho e envia. Chamado pelo tick do Celery.
    Retorna lista de resultados por config.
    """
    from apps.scrapers import whatsapp_client
    from apps.scrapers.eventos import log_event
    from apps.scrapers.models import ConfiguracaoEnvio

    agora = timezone.now()
    hoje = timezone.localtime(agora).date()
    # Limites do dia LOCAL como datetimes aware. Com __date=hoje o Postgres aplicava
    # timezone(...)::date na coluna e o índice de data_envio/enviada_em virava enfeite;
    # com __range ele compara datetime com datetime e usa o índice.
    _inicio_hoje = timezone.make_aware(
        timezone.datetime.combine(hoje, timezone.datetime.min.time()),
        timezone.get_current_timezone())
    _hoje_range = (_inicio_hoje, _inicio_hoje + timedelta(days=1) - timedelta(microseconds=1))
    resultados = []
    # Cache por-owner dentro do tick: quantos envios já saíram hoje (cota diária).
    _envios_hoje: dict = {}
    # Mesmo padrão, para o estado da sessão WhatsApp: uma leitura por sessão por
    # tick, não uma por config.
    _wa_status: dict = {}

    def _wa_pronto(cfg) -> bool:
        """Dá para enviar pelo WhatsApp deste dono agora?

        Este gate é o que impede o pior efeito de uma sessão caída: sem ele,
        `selecionar_e_enviar` gasta ~30s de Playwright por candidato (8 deles)
        montando link de afiliado para só então descobrir, no POST, que não há
        WhatsApp do outro lado — por config, por tick, indefinidamente.

        Também religa a sessão 'inativo'. É o único estado em que POST
        /api/sessoes reconecta sem humano: o worker tem a credencial no volume
        mas ela não está no Map (restore pulado por capacidade no boot, ou
        runtime destruído depois). 'expirado' fica DE FORA de propósito — o Node
        só chega nele depois de purgar a credencial (session_policy.reconnectOutcome
        só devolve 'expire' com authPurges > 0), então revivê-lo aqui não
        reconecta ninguém: só fabrica um QR que ninguém está olhando e prende um
        dos 4 slots de Chromium. Quem precisa de QR abre o painel, e o painel já
        chama iniciar_sessao (views.whatsapp_painel).
        """
        sessao = wa_session_de(cfg.owner)
        if not sessao:
            return True   # pool legado sem dono: mantém o caminho de antes
        if sessao not in _wa_status:
            estado = whatsapp_client.status(sessao)
            if not estado.get("conectado") and estado.get("fase") == "inativo":
                whatsapp_client.iniciar_sessao(sessao)
                # Não relê o status: initializeSession é assíncrono no Node e
                # ainda não terminou. Este tick não envia; o próximo encontra a
                # sessão de pé. Reler aqui só somaria latência para o mesmo 'não'.
                logger.info("Sessão WhatsApp %s estava inativa; religada.", sessao)
            _wa_status[sessao] = estado
        return bool(_wa_status[sessao].get("conectado"))

    def _cota_estourada(owner) -> bool:
        """True se o dono está suspenso ou já bateu a cota diária de envios.
        owner=None (pool legado/compartilhado) não tem dono → sem cota/bloqueio."""
        if owner is None:
            return False
        perfil = getattr(owner, "perfil", None)
        if perfil and perfil.bloqueado:
            return True
        if owner.id not in _envios_hoje:
            _envios_hoje[owner.id] = Publicacao.objects.filter(
                usuario=owner, criada_em__range=_hoje_range,
                status__in=("pendente", "enviado", "incerto"),
            ).count()
        limite = perfil.cota_max_envios_dia() if perfil else 0
        return bool(limite) and _envios_hoje[owner.id] >= limite

    for cfg in ConfiguracaoEnvio.objects.filter(ativo=True).select_related("owner__perfil"):
        # 0. Dono suspenso ou cota diária estourada → nunca envia.
        if _cota_estourada(cfg.owner):
            continue
        # 0.5. Freio automático de falhas seguidas. Diferente de `ativo=False`, ele
        # expira sozinho no próximo dia habilitado — sair dele zera o contador e a
        # regra volta a tentar sem depender de alguém lembrar de religá-la.
        if cfg.freio_ativo(agora):
            continue
        if cfg.pausada_ate is None and cfg.motivo_pausa:
            # `freio_ativo` acabou de expirar o freio: persiste a soltura mesmo que
            # este tick pare mais adiante (fora da janela, sem estoque, cota).
            cfg.motivo_pausa = ""
            cfg.save(update_fields=["pausada_ate", "falhas_consecutivas",
                                    "motivo_pausa"])
        # 1. Respeita a janela de horário (ex: 8h-20h) e os dias da semana marcados.
        if not cfg.dentro_da_janela(agora) or not cfg.dia_permitido(agora):
            continue
        # 2. Vencido = sem agendamento ainda OU passou do proximo_envio (intervalo + jitter).
        vencido = cfg.proximo_envio is None or agora >= cfg.proximo_envio
        if not vencido:
            continue
        enviados_config_hoje = Publicacao.objects.filter(
            configuracao=cfg, status="enviado", enviada_em__range=_hoje_range).count()
        if cfg.max_envios_dia and enviados_config_hoje >= cfg.max_envios_dia:
            continue
        # 3. WhatsApp do dono fora do ar: não é falha da regra. Sai sem tocar em
        # falhas_consecutivas e sem reagendar — quando a sessão voltar, a config
        # continua vencida e envia no primeiro tick seguinte.
        if getattr(cfg, "canal", "whatsapp") == "whatsapp" and not _wa_pronto(cfg):
            logger.info(
                "Config %s pulada: WhatsApp do dono não está conectado.", cfg.id)
            continue

        # CERCA DE TENANT. Este laço percorre as regras de TODOS os donos, então
        # uma exceção que escape daqui não atrasa um envio: cancela o ciclo inteiro
        # e todos os outros clientes ficam sem oferta naquele tick. Foi exatamente
        # isso que aconteceu com `BrowserResourceUnavailable` (ver o `except`
        # próprio em `_executar`). A correção pontual daquela classe resolve o caso
        # conhecido; esta cerca resolve a categoria, inclusive para o erro que
        # ainda não aconteceu. O custo é um `continue` no lugar de um tick perdido.
        try:
            if getattr(cfg, "tipo", "") == ConfiguracaoEnvio.TIPO_AVISO_CUPONS:
                r = enviar_aviso_cupons(
                    selecionar_cupons_para_aviso(cfg, cfg.owner), cfg.grupo_id,
                    canal=getattr(cfg, "canal", "whatsapp"), usuario=cfg.owner,
                    destino_nome=cfg.grupo_nome, configuracao=cfg,
                    enqueue_only=_send_pipeline_v2_enabled(cfg.owner),
                )
            else:
                macros = [cfg.macro_categoria] if cfg.macro_categoria else None  # vazio = qualquer (inclui ofertas)
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
        except DatabaseError:
            # Banco fora é problema do processo inteiro, não desta regra: o loop de
            # envio tem tratamento próprio (pausa progressiva) e precisa vê-lo.
            raise
        except Exception as exc:
            logger.exception("Config %s falhou no tick de envio", cfg.id)
            log_event(
                "publicacao", "config_erro",
                f"A regra de envio {cfg.id} falhou neste ciclo: {exc}",
                level="error", usuario=cfg.owner,
                contexto={"configuracao": cfg.id, "destino": cfg.grupo_id},
                exc=exc,
            )
            # Reagenda como falha transitória: a regra tenta de novo no próximo
            # vencimento em vez de martelar este tick, e não conta falha permanente.
            r = {
                "sucesso": False,
                "motivo": "Falha temporária ao processar esta regra de envio.",
                "classe": TRANSITORIO,
            }
        # Reagenda sempre (sucesso ou não) p/ não ficar martelando o mesmo tick;
        # jitter ±1-10min deixa o ritmo humano. ultimo_envio só em sucesso (display).
        cfg.agendar_proximo(agora)
        if r.get("sucesso") and not r.get("queued"):
            cfg.ultimo_envio = agora
            cfg.falhas_consecutivas = 0
            cfg.motivo_pausa = ""
            cfg.pausada_ate = None
            if cfg.owner_id is not None:
                _envios_hoje[cfg.owner_id] = _envios_hoje.get(cfg.owner_id, 0) + 1
        elif not r.get("sucesso"):
            from apps.scrapers.send_pipeline import classify_result
            failure_class = classify_result(r)
        if not r.get("sucesso") and failure_class in {"transient", "uncertain"}:
            # Falha que some sozinha (worker piscou, timeout, 429, estoque vazio).
            # Não conta e não pausa: era exatamente isto que desligava a automação
            # de quem não tinha defeito nenhum na regra. Também não zera o
            # contador — uma falha permanente intercalada com blips transitórios
            # ainda precisa chegar ao teto.
            logger.info("Config %s: falha transitória ignorada (%s).",
                        cfg.id, r.get("motivo"))
        elif not r.get("sucesso"):
            # Somente destino/payload inválido ou credencial explicitamente revogada
            # são permanentes. Desconhecido é infraestrutura até prova em contrário.
            cfg.falhas_consecutivas += 1
            if cfg.pausar_apos_falhas and cfg.falhas_consecutivas >= cfg.pausar_apos_falhas:
                cfg.frear(agora, r.get("motivo"))
                # Nível error: a automação do usuário acabou de parar. Ela volta
                # sozinha no próximo dia habilitado, mas se a causa for crônica o
                # ciclo se repete — então o evento continua saltando no relatório.
                log_event(
                    "publicacao", "config_pausada",
                    f"Automação pausada após {cfg.falhas_consecutivas} falhas até "
                    f"{timezone.localtime(cfg.pausada_ate):%d/%m %H:%M}: {cfg.motivo_pausa}",
                    level="error", usuario=cfg.owner,
                    contexto={
                        "config_id": cfg.id,
                        "destino": cfg.grupo_nome or cfg.grupo_id,
                        "canal": getattr(cfg, "canal", "whatsapp"),
                        "falhas_consecutivas": cfg.falhas_consecutivas,
                        "motivo": cfg.motivo_pausa,
                        "pausada_ate": cfg.pausada_ate.isoformat(),
                    },
                )
        cfg.save(update_fields=[
            "proximo_envio", "ultimo_envio", "falhas_consecutivas",
            "motivo_pausa", "pausada_ate", "ativo"])
        resultados.append({"config": cfg.id, **r})
    return resultados

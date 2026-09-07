"""Loops full-time de coleta, cupons, afiliação, envio e relatórios.

Cada modo roda em processo separado. ``cupons`` é deliberadamente independente
do toggle da raspagem geral para renovar a janela segura de preparo.
"""
import logging
import threading
import time
from contextlib import contextmanager
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import DatabaseError, connections
from django.utils import timezone
from apps.accounts.tenant import system_context

from apps.scrapers import automacao_state as st
from apps.scrapers.eventos import log_event

logger = logging.getLogger("apps.automacao")


ERRO_PUBLICO = "Falha temporária no serviço. Uma nova tentativa será feita no próximo ciclo."
RETRY_MINUTOS = 5
BACKOFF_BANCO_MAX_S = 300
# Intervalo até retomar uma varredura que cedeu o navegador no meio do caminho.
RETOMADA_MINUTOS = 5


def _resta_varredura():
    """Página em que a varredura de ofertas parou, ou 0 se a passada terminou."""
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import CURSOR_OFERTAS

    try:
        cursor = int(st.read_state("scrape").get(CURSOR_OFERTAS) or 1)
    except (TypeError, ValueError):
        return 0
    return cursor if cursor > 1 else 0


@contextmanager
def _heartbeat_durante(job, intervalo=15):
    """Mantém o estado operacional vivo enquanto uma coleta bloqueante executa."""
    parar = threading.Event()

    def _pulse():
        while not parar.wait(intervalo):
            st.write_state(job)

    thread = threading.Thread(target=_pulse, daemon=True, name=f"heartbeat-{job}")
    thread.start()
    try:
        yield
    finally:
        parar.set()
        thread.join(timeout=1)
        st.write_state(job)


def _renovar_conexoes_db():
    """Descarta conexões herdadas/ociosas antes de cada ciclo do worker.

    Estes comandos vivem por dias e passam horas dormindo. Nesse intervalo o
    Postgres/proxy pode encerrar o socket sem que o Django saiba; reutilizá-lo
    causava ``OperationalError: the connection is closed`` no ciclo seguinte.
    """
    connections.close_all()


def _pausar_por_banco(job, erro, falhas: int):
    """Evita retry em loop quando o Postgres/proxy está indisponível.

    Não gravamos EventoOperacional aqui: ele também depende do mesmo banco. O estado
    do worker fica no volume e permite que a tela de Saúde mostre o ocorrido assim
    que a conexão voltar.
    """
    espera = min(15 * (2 ** max(0, falhas - 1)), BACKOFF_BANCO_MAX_S)
    proximo = timezone.now() + timedelta(seconds=espera)
    connections.close_all()
    logger.warning("%s pausado por banco indisponível; nova tentativa em %ss: %s",
                   job, espera, erro)
    st.write_state(job, fase="aguardando_banco", erro=ERRO_PUBLICO,
                   proximo_ciclo=proximo.isoformat(),
                   ultima_msg=f"Banco indisponível; nova tentativa em {espera}s.")
    return proximo


def _rodar_scrape(*, lojas_alvo=None):
    from apps.scrapers.carga import BrowserResourceUnavailable
    from apps.scrapers.marketplaces.registry import MARKETPLACES
    from apps.scrapers.models import ConfiguracaoEnvio

    termos = list(
        ConfiguracaoEnvio.objects.filter(ativo=True)
        .exclude(termo_busca="").values_list("termo_busca", flat=True)
    )
    lojas = list(MARKETPLACES.items())
    if lojas_alvo:
        alvos = set(lojas_alvo)
        lojas = [(slug, mp) for slug, mp in lojas if slug in alvos]
    # Agnóstico de loja: cada marketplace raspa suas fontes. Habilitar Amazon/Shopee
    # depois não precisa editar este loop — basta registrar a loja no registry.
    falhas = []
    adiados = []
    for i, (slug, mp) in enumerate(lojas):
        msg = f"[{timezone.now():%H:%M}] SCRAPE: {slug}..."
        logger.info(msg)
        st.write_state(
            "scrape", fase="raspando", loja_atual=slug,
            loja_idx=i + 1, lojas_total=len(lojas), ultima_msg=msg,
        )
        inicio_loja = timezone.now()
        try:
            mp.scrape_all(termos=termos)
        except BrowserResourceUnavailable:
            # O único Chromium está com outra lane. A fonte não respondeu mal: ela
            # nem começou. Preservar o snapshot e retomar logo evita dois defeitos
            # observados em produção: badge vermelho falso e espera de três horas
            # quando a disputa aconteceu antes de o cursor sair da página 1.
            adiados.append(slug)
            logger.info(
                "Scrape '%s' adiado por capacidade; catálogo anterior preservado.",
                slug,
            )
        except Exception as e:
            logger.exception("Scrape '%s' falhou", slug)
            # Por loja: uma fonte quebrada (seletor mudou, bloqueio) não derruba o
            # ciclo, então some do radar. É a falha que envenena o catálogo devagar.
            log_event("scraper", "fonte_falhou", f"A coleta da loja {slug} falhou.",
                      level="error", contexto={"marketplace": slug}, exc=e)
            falhas.append(slug)
            from django.db.models import Q
            from apps.scrapers.models import FonteIngestao
            # SÓ as fontes que ainda não deram veredito próprio neste ciclo. Sem o
            # recorte por `ultima_tentativa`, uma exceção tardia em `scrape_all`
            # rebaixava as três linhas da Amazon de uma vez — inclusive as que
            # tinham acabado de reportar sucesso —, e nada nunca as promovia de
            # volta a não ser um ciclo inteiro sem nenhum defeito.
            FonteIngestao.objects.filter(
                Q(ultima_tentativa__isnull=True) | Q(ultima_tentativa__lt=inicio_loja),
                marketplace=slug, habilitada=True,
            ).update(
                status="degraded", ultima_tentativa=timezone.now(),
                erro_publico="Falha temporária na coleta; dados anteriores preservados.")
            st.write_state("scrape", erro=ERRO_PUBLICO)
    sucessos = len(lojas) - len(falhas) - len(adiados)
    if sucessos:
        from apps.scrapers.maintenance import expire_stale
        expire_stale()
    if not sucessos and falhas and not adiados:
        raise RuntimeError(f"Todas as fontes falharam: {', '.join(falhas)}")
    if falhas:
        logger.warning("SCRAPE concluído parcialmente; falharam: %s", ", ".join(falhas))
    elif adiados:
        logger.info("SCRAPE cedeu capacidade; retomarão: %s", ", ".join(adiados))
    else:
        logger.info("[%s] SCRAPE concluido", timezone.now().strftime("%H:%M"))
    return {"sucessos": sucessos, "falhas": falhas, "adiados": adiados}


def _rodar_scrape_rapido(paginas=8):
    """Lane de 5 min: radares HTTP de cupom e poucas páginas do feed flash ML.

    Telegram e Pelando entram somente como descoberta: os mesmos gates de
    corroboração/checkout do ciclo central continuam obrigatórios. Não resolvemos
    redirects nem abrimos Chromium para essas duas fontes aqui.
    """
    from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas
    from apps.scrapers.carga import BrowserResourceUnavailable
    from apps.scrapers.coupon_pipeline import _coletar_adaptador, _metricas_vazias
    from apps.scrapers.models import FonteIngestao
    logger.info("[%s] SCRAPE-FLASH: feed ML (%s paginas)", timezone.now().strftime("%H:%M"), paginas)
    # A agenda oficial e HTTP/SSR e deve rodar mesmo quando o Chromium do feed
    # estiver ocupado. Assim um cupom de uma hora nao espera o ciclo de 15 min.
    _coletar_adaptador("ml-lightning-coupons", _metricas_vazias())
    _coletar_adaptador("pelando-cupons", _metricas_vazias())
    _coletar_adaptador(
        "telegram-publico", _metricas_vazias(), items=("coupons",),
        include_offers=False,
    )
    try:
        total = mapear_ofertas(max_paginas=paginas, substituir=False)
    except BrowserResourceUnavailable:
        # O Chromium e deliberadamente unico. A coleta HTTP acima ja terminou e a
        # disputa esperada com scrape/link/login nao pode transformar o ciclo em
        # incidente nem rebaixar o ultimo snapshot saudavel do feed flash.
        logger.info(
            "SCRAPE-FLASH: feed ML adiado; navegador ocupado e radares HTTP concluidos."
        )
        return None
    now = timezone.now()
    fonte, _ = FonteIngestao.objects.get_or_create(
        slug="mercadolivre-ofertas-flash",
        defaults={"marketplace": "mercadolivre", "nome": "Mercado Livre — ofertas flash"},
    )
    fonte.ultima_tentativa = now
    fonte.ultimo_total = total
    if total:
        fonte.status = "ok"
        fonte.ultimo_sucesso = now
        fonte.falhas_consecutivas = 0
        fonte.erro_publico = ""
    elif not fonte.ultimo_sucesso:
        fonte.status = "degraded"
        fonte.erro_publico = "Coleta vazia; catálogo anterior preservado."
    fonte.save()
    return total


def _rodar_feed_afiliados():
    """Compatibilidade: a implementação efetiva mora no pipeline central."""
    from apps.scrapers.coupon_pipeline import coletar_feed_licenciado

    return coletar_feed_licenciado()


def _rodar_cupons(lote=40):
    """Mantém coleta, preparo e links mesmo com a raspagem geral desligada."""
    from apps.scrapers.coupon_pipeline import executar_pipeline_cupons

    resultado = executar_pipeline_cupons(
        coletar=True,
        limite_preparo=max(12, lote),
        limite_links=max(1, lote),
    )
    # Lote deliberadamente pequeno: checkout disputa o mesmo Chromium das fontes e
    # dos links. O adaptador só usa a sessão do próprio usuário, isola um carrinho
    # vazio e nunca cruza a ação que cria/paga um pedido.
    from apps.scrapers.coupon_validation_adapters import CHECKOUT_VALIDATION_ADAPTERS
    from apps.scrapers.coupon_validation_runner import (
        defer_missing_checkout_sessions, run_validation_batch,
    )
    resultado["validacoes_sem_sessao"] = defer_missing_checkout_sessions()
    resultado["validacoes_checkout"] = run_validation_batch(
        adapters=CHECKOUT_VALIDATION_ADAPTERS, limit=2,
    )
    # Produto criado pelo pipeline de cupom nasce com `categoria=DESCONHECIDO`, e o
    # classificador de macro do ML deriva justamente da categoria. Sem macro, nenhuma
    # `ConfiguracaoEnvio` enxerga o item — todas filtram por ela. Medido em 04/09:
    # 249 dos 300 candidatos com cupom provado estavam assim, invisíveis. Aqui só o
    # que já tem par confirmado e cupom ativo, que é o que muda o funil hoje.
    from apps.scrapers.categorizar_por_nome import popular_macro_por_nome

    resultado["macros_por_nome"] = popular_macro_por_nome(
        limite=max(200, lote * 5), apenas_com_cupom=True,
    )
    logger.info(
        "CUPONS: %s encontrado(s), %s persistido(s), %s preparado(s), "
        "%s link(s) verificado(s), %s cupom(ns) pronto(s), %s falha(s)",
        resultado["encontrados"], resultado["persistidos"],
        resultado["preparados"], resultado["links_verificados"],
        resultado["prontos"], resultado["falhos"] + resultado["links_falhos"],
    )
    return resultado


def _rodar_links(lote=40):
    """Pré-gera links de afiliado dos produtos pendentes — um lote por ciclo.

    Sem isto nada em produção gerava link: o scrape só cria Produto (com link vazio),
    e cada raspagem só aumentava a pilha de "pendente" na tela de Promoções.

    Por usuário, porque o link carrega a conta de afiliado de quem envia: quem não
    tem sessão ML válida é pulado (gerar exigiria o Link Builder logado). O lote é
    pequeno de propósito — cada item custa uma ida ao Link Builder (~5s), e este
    processo divide o Chromium e a CPU com a raspagem e o painel.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Exists, OuterRef, Q

    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.models import LinkAfiliadoUsuario, Produto
    from apps.scrapers.monitor_conexao import ml_conectado

    agora = timezone.now()
    gerados = falhas = pulados = adiados = 0
    por_marketplace = {}
    for user in get_user_model().objects.filter(is_active=True):
        # Amazon primeiro, e fora do gate do Mercado Livre: o link dela é montado em
        # memória (tag + ASIN), não abre navegador nem depende de sessão do ML.
        # Estar no mesmo `continue` do gate do ML fazia uma conta com a Amazon
        # perfeitamente configurada não gerar link nenhum só porque o Mercado Livre
        # estava desconectado.
        g_amazon, f_amazon = _gerar_links_amazon(user, lote=lote, agora=agora)
        gerados += g_amazon
        falhas += f_amazon
        if g_amazon or f_amazon:
            destino = por_marketplace.setdefault("amazon", {"gerados": 0, "falhas": 0})
            destino["gerados"] += g_amazon
            destino["falhas"] += f_amazon
        # Shopee também usa HTTP puro (Open API) e não depende do login do ML.
        # Ela precisa rodar antes do gate abaixo pelo mesmo motivo da Amazon: uma
        # conta ML desconectada não pode paralisar outra loja perfeitamente pronta.
        g_shopee, f_shopee = _gerar_links_shopee(user, lote=lote, agora=agora)
        gerados += g_shopee
        falhas += f_shopee
        if g_shopee or f_shopee:
            destino = por_marketplace.setdefault("shopee", {"gerados": 0, "falhas": 0})
            destino["gerados"] += g_shopee
            destino["falhas"] += f_shopee
        if not ml_conectado(user):
            # Antes isto era um `continue` mudo: o usuário simplesmente nunca gerava
            # link e nada em lugar nenhum dizia por quê. Agora a Saúde mostra.
            pulados += 1
            _avisar_sem_sessao_ml(user)
            continue
        # Fora da fila: quem já tem link utilizável (aprovado ou aguardando
        # veredito), quem é terminal (não afiliável / desistimos) e quem está de
        # castigo no backoff. Sem isto, produtos que nunca afiliam ocupavam o lote de
        # 40 a cada ciclo — os mais recentes primeiro — e nenhum outro produto
        # chegava a ser tentado. A pilha de "pendente" não saía nunca.
        #
        # O que NÃO está fora da fila: link reprovado com backoff vencido. Ele volta
        # justamente para ter a URL substituída — antes, "tem URL" o excluía da
        # geração e a verificação só reabria a mesma URL reprovada, para sempre.
        fora_da_fila = LinkAfiliadoUsuario.objects.filter(
            usuario=user, produto=OuterRef("pk")).filter(
                Q(estado__in=["nao_afiliavel", "erro"])
                | Q(proxima_tentativa__gt=agora)
                | (~Q(link_afiliado="") & (Q(verificado_ok=True)
                                           | Q(verificado_ok__isnull=True))))
        pendentes = list(
            Produto.objects.filter(marketplace="mercadolivre", preco_sem_desconto__gt=0)
            .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
            .filter(Q(owner__isnull=True) | Q(owner=user))
            .exclude(Exists(fora_da_fila))
            .order_by("-ultima_observacao")[:lote]
        )
        if not pendentes:
            continue
        try:
            g, f = get_marketplace("mercadolivre").prefetch_links(pendentes, usuario=user)
        except Exception as e:
            from apps.scrapers.afiliado import causa_de_capacidade

            if causa_de_capacidade(e):
                # A máquina tem UM Chromium: quando a lane de cupons está com ele,
                # esta nem começa. Isso é fila, não avaria — logar como erro (com
                # traceback) enchia a tela de Saúde de alarmes que ninguém pode
                # acionar e escondia o atraso real por capacidade. O ciclo seguinte
                # retoma exatamente de onde parou.
                #
                # MEDIDO EM 19/08: esta lane perde o navegador em TODO ciclo, para
                # todos os usuários. Sem ela nenhum link de afiliado nasce, nenhum
                # cupom fica pronto, e o funil inteiro fica atrás de uma vaga —
                # `code_not_ready_20m` subiu de 196 para 288 justamente assim.
                #
                # A saída ÓBVIA não funciona: chamar `sinalizar_interesse_manual`
                # daqui faz esta lane ceder para si mesma. O marcador é global
                # (`django_chromium`), e o próprio lote de links consulta
                # `interesse_interativo_pendente` entre itens — ele veria o interesse
                # que acabou de anunciar e devolveria o navegador. Três testes de
                # cessão cooperativa reprovaram a tentativa, e estavam certos.
                #
                # A prioridade real exige um sinal que saiba QUEM pediu, para que
                # apenas as outras lanes cedam. É mudança no contrato de
                # `resource_control`, não um remendo aqui.
                adiados += 1
                logger.info(
                    "Geração de links adiada para %s: navegador ocupado por outra "
                    "tarefa; retoma no próximo ciclo.", user,
                )
                continue
            # Sessão expirada/queda do Link Builder é de UM usuário: não pode
            # impedir que os outros gerem os deles.
            logger.warning("Geração de links falhou para %s: %s", user, e)
            log_event("scraper", "links_erro",
                      f"Não foi possível gerar links de afiliado: {e}",
                      level="warning", usuario=user, exc=e)
            continue
        gerados += g
        falhas += f
        destino = por_marketplace.setdefault(
            "mercadolivre", {"gerados": 0, "falhas": 0})
        destino["gerados"] += g
        destino["falhas"] += f
        logger.info("Links ML p/ %s: %s gerado(s), %s falha(s) de %s pendente(s)",
                    user, g, f, len(pendentes))
    return {"gerados": gerados, "falhas": falhas, "pulados": pulados,
            "adiados": adiados, "por_marketplace": por_marketplace}


def _gerar_links_amazon(user, *, lote, agora):
    """Lote de links Amazon de UM usuário. Sem navegador, sem sessão do ML.

    A tag ausente é tratada como bloqueio de conta dentro de `prefetch_links`: ele
    devolve (0, 0) e registra um único aviso, em vez de gravar uma falha por
    produto.
    """
    from django.db.models import Exists, OuterRef, Q

    from apps.scrapers.afiliado import tag_amazon
    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.models import LinkAfiliadoUsuario, Produto

    # Sem tag não há link possível: sair antes evita varrer o catálogo Amazon a
    # cada tick para descobrir, no fim, que a conta não está configurada.
    if not tag_amazon(user):
        return (0, 0)

    fora_da_fila = LinkAfiliadoUsuario.objects.filter(
        usuario=user, produto=OuterRef("pk")).filter(
            Q(estado__in=["nao_afiliavel", "erro"])
            | Q(proxima_tentativa__gt=agora)
            | (~Q(link_afiliado="") & Q(verificado_ok=True)))
    pendentes = list(
        Produto.objects.filter(marketplace="amazon", preco_sem_desconto__gt=0)
        .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
        .filter(Q(owner__isnull=True) | Q(owner=user))
        .exclude(Exists(fora_da_fila))
        .order_by("-ultima_observacao")[:lote]
    )
    if not pendentes:
        return (0, 0)
    try:
        return get_marketplace("amazon").prefetch_links(pendentes, usuario=user)
    except Exception as e:
        logger.warning("Geração de links Amazon falhou para %s: %s", user, e)
        return (0, 0)


def _gerar_links_shopee(user, *, lote, agora):
    """Lote de links Shopee de UM usuário via API, sem Chromium.

    A esteira geral tinha caminhos explícitos apenas para Amazon e Mercado Livre.
    Produtos Shopee descobertos fora do pipeline de cupons ficavam pendentes para
    sempre, embora o adaptador já soubesse gerar o short link oficial.
    """
    from django.db.models import Exists, OuterRef, Q

    from apps.scrapers.marketplaces.registry import get_marketplace
    from apps.scrapers.models import (
        IntegracaoAfiliado, LinkAfiliadoUsuario, Produto,
    )

    if not IntegracaoAfiliado.objects.filter(
        owner=user, provedor="shopee", habilitada=True,
    ).exists():
        return (0, 0)

    fora_da_fila = LinkAfiliadoUsuario.objects.filter(
        usuario=user, produto=OuterRef("pk"),
    ).filter(
        Q(estado__in=["nao_afiliavel", "erro"])
        | Q(proxima_tentativa__gt=agora)
        | (~Q(link_afiliado="") & Q(verificado_ok=True))
    )
    pendentes = list(
        Produto.objects.filter(marketplace="shopee", preco_sem_desconto__gt=0)
        .exclude(estado__in=["indisponivel", "invalido", "expirado", "stale"])
        .filter(Q(owner__isnull=True) | Q(owner=user))
        .exclude(Exists(fora_da_fila))
        .order_by("-ultima_observacao")[:lote]
    )
    if not pendentes:
        return (0, 0)
    try:
        return get_marketplace("shopee").prefetch_links(
            pendentes, usuario=user,
        )
    except Exception as e:
        logger.warning("Geração de links Shopee falhou para %s: %s", user, e)
        return (0, 0)


def _rodar_verificacao_links(limite=40):
    """Aprova o DESTINO dos links já gerados — o passo que torna a oferta enviável.

    Era um "passageiro" da geração: ficava DEPOIS do `if not pendentes: continue`
    dentro de _rodar_links, então assim que a fila de geração esvaziava (todo
    produto já com link) a verificação nunca mais rodava. Em homologação isso
    deixou 287 links gerados com apenas 6 verificados — e como a tela só lista item
    com verificado_ok=True, o catálogo inteiro ficava invisível. Agora é uma lane
    própria, que roda tenha ou não link novo para gerar.

    NÃO exige sessão do ML: a verificação abre a página pública do destino
    (validar_sessao=False, e `verificar_link_afiliado` nem usa o usuário). Um
    usuário com a conta desconectada tem centenas de links esperando veredito, e
    exigir sessão aqui os manteria invisíveis sem motivo.

    Uma lane POR LOJA, resolvida pelo registry: cada marketplace julga só os
    próprios links. Chamar direto o verificador do ML era o que fazia link Amazon
    ser reprovado por "não abre uma página de produto do Mercado Livre".
    """
    from django.contrib.auth import get_user_model
    from apps.scrapers.marketplaces.registry import MARKETPLACES

    total = {"aprovados": 0, "reprovados": 0, "transitorios": 0, "bloqueados": 0}
    por_loja = {}
    for user in get_user_model().objects.filter(is_active=True):
        for slug, mp in MARKETPLACES.items():
            try:
                r = mp.verificar_links_pendentes(user, limite=limite)
            except Exception as e:
                from apps.scrapers.afiliado import causa_de_capacidade

                if causa_de_capacidade(e):
                    # Fila, não avaria — mesma situação da geração de links acima, e
                    # com a mesma consequência: sem veredito de destino o link não é
                    # aprovado, e sem link aprovado o cupom não fica pronto. Fica em
                    # INFO para não alarmar sobre algo que ninguém pode acionar.
                    logger.info(
                        "Verificação de destino %s adiada para %s: navegador ocupado; "
                        "retoma no próximo ciclo.", slug, user,
                    )
                    continue
                logger.warning("Verificação de destino %s falhou para %s: %s",
                               slug, user, e)
                continue
            destino = por_loja.setdefault(slug, {})
            for chave, valor in r.items():
                if not isinstance(valor, int):
                    continue
                destino[chave] = destino.get(chave, 0) + valor
                if chave in total:
                    total[chave] += valor
    if any(total.values()):
        logger.info("Verificação de destino: %s aprovado(s), %s reprovado(s), "
                    "%s transitório(s) — por loja: %s", total["aprovados"],
                    total["reprovados"], total["transitorios"], por_loja)
    total["por_marketplace"] = por_loja
    return total


def _avisar_sem_sessao_ml(user):
    """Registra que este usuário não gera link por falta de sessão ML — com cooldown.

    Sem cooldown seriam 288 eventos/dia por usuário desconectado (tick de 5min), e a
    tela de Saúde afogaria justamente no aviso que precisa ser lido.
    """
    from django.core.cache import cache

    chave = f"links_sem_sessao:{user.id}"
    if cache.get(chave):
        return
    cache.set(chave, True, timeout=6 * 3600)
    log_event("scraper", "links_sem_sessao",
              f"{user.get_username()} não gera links de afiliado: a sessão do "
              f"Mercado Livre não está conectada.",
              level="warning", usuario=user, contexto={"servico": "Mercado Livre"})


class Command(BaseCommand):
    help = ("Loop de automação: scrape (full) / scrape_rapido (flash) / envio / "
            "cupons / links (afiliação) / relatorios / manual.")

    def add_arguments(self, parser):
        parser.add_argument("--modo",
                            choices=("scrape", "scrape_rapido", "envio", "cupons",
                                     "links", "relatorios", "manual"),
                            required=True,
                            help="scrape = raspagem completa; scrape_rapido = feed flash; "
                                 "envio = envio pelas regras; cupons = manutenção "
                                 "independente; links = pré-gera links "
                                 "de afiliado dos pendentes.")
        parser.add_argument("--tick", type=int, default=5, help="Minutos entre ciclos (envio/flash/links).")
        parser.add_argument("--lote", type=int, default=40, help="Links gerados por ciclo, por usuário.")
        parser.add_argument("--scrape-horas", type=float, default=3.0, help="Horas entre raspagens completas.")

    def handle(self, *args, **opts):
        """Boot resiliente: banco fora na hora de subir não pode crashar o processo.

        ``@system_job`` faz SQL (checagem de role + set_config) antes do primeiro
        ciclo do loop escolhido. Sem retry aqui, um Postgres fora do ar bem na
        hora do boot derrubava os 8 processos do grupo honcho antes mesmo de
        eles começarem, e o Fly reiniciava a máquina — crash-loop com Chromium
        frio a cada volta. ``connection_created`` já reinstala o contexto numa
        conexão nova (tenant.py), então repetir após ``close_all()`` é seguro.
        """
        falhas = 0
        while True:
            try:
                with system_context():
                    return self._despachar(opts)
            except DatabaseError as exc:
                falhas += 1
                espera = min(15 * (2 ** max(0, falhas - 1)), BACKOFF_BANCO_MAX_S)
                logger.warning(
                    "Boot do automacao (modo=%s) aguardando banco (tentativa %s): %s",
                    opts.get("modo"), falhas, exc,
                )
                connections.close_all()
                time.sleep(espera)

    def _despachar(self, opts):
        if opts["modo"] == "scrape":
            self._loop_scrape(opts)
        elif opts["modo"] == "scrape_rapido":
            self._loop_scrape_rapido(opts)
        elif opts["modo"] == "envio":
            self._loop_envio(opts)
        elif opts["modo"] == "cupons":
            self._loop_cupons(opts)
        elif opts["modo"] == "links":
            self._loop_links(opts)
        elif opts["modo"] == "manual":
            self._loop_manual(opts)
        else:
            self._loop_relatorios(opts)

    def _loop_manual(self, opts):
        from apps.scrapers.manual_scraping import (
            atualizar_diagnostico_fila, existe_job_pendente, processar_proximo_job,
        )
        from apps.scrapers.resource_control import (
            leased_resource, limpar_interesse_manual, machine_resource_slot,
            pulse_worker, sinalizar_interesse_manual, worker_activity,
            worker_identity,
        )

        idle_poll = 5
        queue_poll = 2
        worker_id = worker_identity("manual")
        logger.info("MANUAL worker no ar; consumidor dedicado da fila do painel")
        while True:
            try:
                _renovar_conexoes_db()
                pulse_worker("manual", worker_id=worker_id, state="idle")
                atualizar_diagnostico_fila()
                if not existe_job_pendente():
                    limpar_interesse_manual("django_chromium")
                    time.sleep(idle_poll)
                    continue
                # Renova a preferência enquanto o holder automático termina o item
                # corrente. Os lotes consultam este marcador entre páginas/itens.
                sinalizar_interesse_manual("django_chromium")
                with leased_resource("django_chromium", owner_kind="manual") as (
                    acquired, detail,
                ):
                    if not acquired:
                        atualizar_diagnostico_fila(
                            resource_owner=detail.get("owner_kind", "scheduled"),
                        )
                        time.sleep(queue_poll)
                        continue
                    # Não deixe o job ceder para o próprio pedido. A partir daqui
                    # o lease + flock já reservam o navegador para esta execução.
                    limpar_interesse_manual("django_chromium")
                    machine_busy = False
                    with machine_resource_slot("django_chromium") as machine_acquired:
                        if not machine_acquired:
                            atualizar_diagnostico_fila(resource_owner="interactive")
                            machine_busy = True
                        else:
                            with worker_activity("manual", worker_id, "manual_scraping"):
                                processar_proximo_job(
                                    worker_id, detail.get("lease_token", ""),
                                )
                # Dorme depois que os dois locks foram liberados. Dormir dentro do
                # lease impediria os demais workers de usar o browser sem necessidade.
                if machine_busy:
                    sinalizar_interesse_manual("django_chromium")
                    time.sleep(queue_poll)
                    continue
            except DatabaseError as exc:
                logger.warning("Fila manual aguardando banco: %s", exc)
                connections.close_all()
                time.sleep(idle_poll)
            except Exception:
                logger.exception("Falha no consumidor dedicado da fila manual")
                time.sleep(idle_poll)

    def _loop_cupons(self, opts):
        from apps.scrapers.manual_scraping import existe_job_pendente

        tick = max(1, opts["tick"])
        lote = max(1, opts["lote"])
        poll = 15
        logger.info(
            "CUPONS worker no ar; ciclo a cada %smin, independente da raspagem geral",
            tick,
        )
        proximo = timezone.now()
        falhas_banco = 0
        while True:
            if timezone.now() < proximo:
                st.write_state("cupons", fase="aguardando")
                time.sleep(poll)
                continue
            agora = timezone.now()
            try:
                # O ciclo automático também percorre/prepara milhares de cupons.
                # Mesmo quando ainda está na parte HTTP/SQL (antes de pedir o
                # Chromium), ele compete com a ação do painel e alonga sua etapa
                # final. A fila manual é durável no banco, então esta verificação
                # funciona inclusive logo após um deploy, antes de o consumidor
                # manual recuperar uma execução interrompida.
                _renovar_conexoes_db()
                if existe_job_pendente():
                    proximo = timezone.now() + timedelta(seconds=poll)
                    st.write_state(
                        "cupons", fase="aguardando_manual",
                        proximo_ciclo=proximo.isoformat(),
                        ultima_msg="Ciclo automático cedido à geração manual de cupons.",
                    )
                    continue
                st.write_state("cupons", fase="processando", erro="")
                # HTTP, parsing e banco não seguram o slot global. Cada adaptador
                # que realmente abre Playwright adquire o lease no ponto de uso.
                with _heartbeat_durante("cupons"):
                    resultado = _rodar_cupons(lote=lote)
                falhas_banco = 0
                proximo = timezone.now() + timedelta(minutes=tick)
                falhas = resultado["falhos"] + resultado["links_falhos"]
                st.write_state(
                    "cupons",
                    fase="degradado" if falhas else "aguardando",
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=proximo.isoformat(),
                    encontrados=resultado["encontrados"],
                    persistidos=resultado["persistidos"],
                    preparados=resultado["preparados"],
                    vinculados=resultado["vinculados"],
                    links_gerados=resultado["links_gerados"],
                    links_verificados=resultado["links_verificados"],
                    prontos=resultado["prontos"],
                    falhas=falhas,
                    fontes=resultado["fontes"],
                    erro="" if not falhas else "Uma ou mais fontes/links falharam.",
                    ultima_msg=(
                        f"{resultado['prontos']} cupom(ns) pronto(s), "
                        f"{resultado['links_verificados']} link(s) verificado(s) "
                        f"às {agora:%H:%M}."
                    ),
                )
            except DatabaseError as exc:
                falhas_banco += 1
                proximo = _pausar_por_banco("cupons", exc, falhas_banco)
            except Exception as exc:
                logger.exception("Erro no ciclo central de cupons")
                log_event(
                    "scraper", "cupons_ciclo_erro",
                    f"Ciclo central de cupons falhou: {exc}",
                    level="error", exc=exc,
                )
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state(
                    "cupons", fase="aguardando",
                    proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO,
                )

    def _loop_links(self, opts):
        # Lane própria: pode drenar a fila mesmo quando a coleta geral está parada.
        # Em instalações ainda sem escolha explícita, `is_enabled("links")` herda
        # `scrape` para preservar o comportamento anterior ao deploy.
        tick = max(1, opts["tick"])
        lote = max(1, opts["lote"])
        POLL = 15
        logger.info("LINKS worker no ar; até %s link(s)/usuário a cada %smin quando ligado",
                    lote, tick)
        proximo = timezone.now()
        falhas_banco = 0
        while True:
            if not st.is_enabled("links"):
                herdando = st.links_herda_scrape()
                st.write_state("links", fase="desligado",
                               ultima_msg=(
                                   "Parado porque ainda herda a flag da Raspagem, que "
                                   "está desligada; configure a lane Links para torná-la "
                                   "independente."
                                   if herdando else
                                   "Geração de links desligada pela flag própria."
                               ))
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                st.write_state("links", fase="aguardando")
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("links", fase="gerando", erro="")
                _renovar_conexoes_db()
                # Geração e verificação adquirem o slot apenas enquanto o Chromium
                # está vivo. As queries que selecionam os lotes ficam fora do lease.
                with _heartbeat_durante("links"):
                    res = _rodar_links(lote=lote)
                st.write_state("links", fase="verificando", erro="")
                with _heartbeat_durante("links"):
                    ver = _rodar_verificacao_links(limite=lote)
                falhas_banco = 0
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state(
                    "links", fase="aguardando", proximo_ciclo=proximo.isoformat(),
                    gerados=res["gerados"], falhas=res["falhas"], erro="",
                    verificados=ver["aprovados"], reprovados=ver["reprovados"],
                    ultima_msg=(f"{res['gerados']} link(s) gerado(s), "
                                f"{res['falhas']} falha(s), "
                                f"{ver['aprovados']} verificado(s) às {agora:%H:%M}."),
                )
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("links", e, falhas_banco)
            except Exception as e:
                logger.exception("Erro no ciclo de links")
                log_event("scraper", "links_ciclo_erro",
                          f"Ciclo de geração de links falhou: {e}", level="error", exc=e)
                proximo = timezone.now() + timedelta(minutes=tick)
                st.write_state("links", fase="aguardando",
                               proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO)

    def _loop_scrape_rapido(self, opts):
        # Lane flash: gate no MESMO flag "scrape" (se a raspagem está ligada, roda).
        tick = max(1, opts["tick"])
        POLL = 15
        logger.info("SCRAPE-FLASH worker no ar; feed a cada %smin quando ligado", tick)
        proximo = timezone.now()
        falhas_banco = 0
        while True:
            # Heartbeat: marca o worker vivo (evita spawn duplicado em dev; worker_alive).
            if not st.is_enabled("scrape"):
                st.write_state("scrape_rapido", fase="ocioso")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                st.write_state("scrape_rapido", fase="aguardando")
                time.sleep(POLL)
                continue
            st.write_state("scrape_rapido", fase="raspando")
            try:
                _renovar_conexoes_db()
                # Sem lease externo: `mapear_ofertas` já pega `django_chromium`
                # página a página. O wrap aqui segurava o slot o ciclo inteiro e
                # o yield interno virava mentira — links/prep morriam
                # BrowserResourceUnavailable até o flash acabar.
                with _heartbeat_durante("scrape_rapido"):
                    _rodar_scrape_rapido()
                falhas_banco = 0
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("scrape_rapido", e, falhas_banco)
                continue
            except Exception as e:
                logger.exception("Erro no scrape-flash")
                log_event("scraper", "flash_erro", f"Ciclo do feed rápido falhou: {e}",
                          level="error", exc=e)
            proximo = timezone.now() + timedelta(minutes=tick)
            st.write_state("scrape_rapido", fase="aguardando",
                           proximo=proximo.isoformat())

    def _loop_scrape(self, opts):
        # Processo SEMPRE vivo (honcho). Trabalha só quando o flag "scrape" está
        # ligado (tela Scraper); senão fica ocioso, checando a cada POLL segundos.
        scrape_seg = max(0.1, opts["scrape_horas"]) * 3600
        POLL = 15
        logger.info("SCRAPE worker no ar; raspa a cada %sh quando ligado", opts["scrape_horas"])
        ciclos = 0
        proximo = timezone.now()  # vencido: raspa assim que ligarem
        falhas_banco = 0
        retomar_lojas = set()
        while True:
            # Heartbeat também durante as horas de espera; sem isto o supervisor
            # considera o processo morto após 90s e pode iniciar workers duplicados.
            st.write_state("scrape")
            if not st.is_enabled("scrape"):
                st.write_state("scrape", fase="desligado", loja_atual=None,
                               ultima_msg="Desligado — ligue na tela Scraper.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                time.sleep(POLL)
                continue
            try:
                st.write_state("scrape", fase="raspando", ciclos=ciclos, erro="")
                _renovar_conexoes_db()
                # Sem lease externo. Creators API é HTTP; ML/Amazon-página já
                # lockam no inner. Outer wrap prendia Chromium ~62min
                # (ofertas + checkout + campanhas + Amazon HTTP) e o funil
                # de cupons não gerava link.
                with _heartbeat_durante("scrape"):
                    resultado = _rodar_scrape(
                        lojas_alvo=retomar_lojas or None,
                    )
                falhas_banco = 0
                ciclos += 1
                fim = timezone.now()
                degradado = bool(resultado["falhas"])
                adiado = bool(resultado.get("adiados"))
                # Passada interrompida no meio não pode esperar o intervalo cheio.
                # A varredura agora cede o navegador para as esteiras que estão na
                # fila (links, verificação, envio) e guarda a página em que parou;
                # se ela só voltasse daqui a três horas, o cursor andaria duas
                # páginas por ciclo e o fundo do feed levaria dias para ser lido.
                # Retomar em minutos mantém a cobertura E a cessão.
                retomando = _resta_varredura()
                retomar_lojas = set(resultado.get("adiados") or [])
                if retomando:
                    retomar_lojas.add("mercadolivre")
                proximo = fim + (
                    timedelta(minutes=RETOMADA_MINUTOS) if retomando or adiado
                    else timedelta(minutes=30) if degradado
                    else timedelta(seconds=scrape_seg))
                erro = ("Falha parcial: " + ", ".join(resultado["falhas"])
                        if degradado else "")
                st.write_state(
                    "scrape", fase="degradado" if degradado else "aguardando", loja_atual=None,
                    ultimo_ciclo_fim=fim.isoformat(), proximo_ciclo=proximo.isoformat(),
                    ciclos=ciclos, erro=erro,
                    ultima_msg=(
                        f"Ciclo {ciclos} cedeu o navegador na página {retomando}; "
                        f"retoma em {RETOMADA_MINUTOS} min." if retomando
                        else f"Ciclo {ciclos} aguardou capacidade; "
                        f"retoma em {RETOMADA_MINUTOS} min." if adiado
                        else f"Ciclo {ciclos} parcial; nova tentativa em 30 min."
                        if degradado
                        else f"Ciclo {ciclos} concluído às {fim:%H:%M}."),
                )
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("scrape", e, falhas_banco)
            except Exception as e:
                logger.exception("Erro no scrape")
                log_event("scraper", "scrape_erro", f"Ciclo de raspagem falhou: {e}",
                          level="error", contexto={"ciclos": ciclos}, exc=e)
                proximo = timezone.now() + timedelta(minutes=RETRY_MINUTOS)
                st.write_state("scrape", fase="aguardando", loja_atual=None,
                               proximo_ciclo=proximo.isoformat(), erro=ERRO_PUBLICO)

    def _faxina_de_orfas(self):
        """Fecha o que ficou pendurado. Roda com a lane ligada OU desligada.

        Uma publicação nasce `pendente` antes do trabalho; o processo que morre no
        meio (deploy, crash) não deixa nenhum `except` para marcá-la. Antes isto
        vivia dentro do tick, ou seja, atrás do portão da lane — e uma órfã criada
        pelo botão "Enviar agora" com a esteira desligada não tinha coveiro nenhum.
        Nunca derruba o loop: faxina não vale um tique de envio.
        """
        try:
            from apps.scrapers.maintenance import (
                reconciliar_execucoes_ingestao_orfas,
                reconciliar_publicacoes_orfas,
            )
            orfas = reconciliar_publicacoes_orfas()
            if orfas:
                logger.warning("%s publicacao(oes) orfa(s) fechada(s) como falha", orfas)
            ingestoes_orfas = reconciliar_execucoes_ingestao_orfas()
            if ingestoes_orfas:
                logger.warning(
                    "%s execução(ões) de ingestão órfã(s) fechada(s)",
                    ingestoes_orfas,
                )
        except Exception as e:
            logger.warning("Reconciliacao de publicacoes falhou: %s", e)

    def _loop_envio(self, opts):
        from django.conf import settings
        from apps.scrapers.ofertas import processar_configs_de_envio

        def _consumir_fila_v2():
            if not settings.SEND_PIPELINE_V2_ENABLED:
                return []
            from apps.scrapers.send_pipeline import process_queued_publications
            return process_queued_publications(limit=20)

        tick = max(1, opts["tick"])
        POLL = 15
        logger.info("ENVIO worker no ar; processa regras a cada %smin quando ligado", tick)
        ticks = 0
        ultima_purga = None  # data da última purga do log (1x/dia, ver abaixo)
        proximo = timezone.now()  # vencido: processa assim que ligarem
        falhas_banco = 0
        while True:
            # A fila e a faxina rodam ANTES do portão da lane, e de propósito.
            #
            # A flag "Envios" governa a esteira automática — criar envios novos a
            # partir das regras. Ela nunca governou o botão "Enviar agora" da tela,
            # que enfileira do mesmo jeito e responde "reservado". Com a lane
            # desligada, nada drenava a fila e nada a enterrava: o item ficava
            # `pendente` para sempre, sem consumidor e sem coveiro. Pior, o
            # reconciliador de órfãs decide se reagenda perguntando se a fila v2
            # está ligada para aquela organização — e a resposta era "sim" enquanto
            # ninguém consumia, então a mesma linha voltava a cada 30 min gravando
            # um evento novo, sem teto.
            try:
                fila = _consumir_fila_v2()
                if fila:
                    logger.info("Fila de envio v2: %s lote(s) processado(s)", len(fila))
            except Exception:
                logger.exception("Falha ao consumir fila de envio v2")
            if not st.is_enabled("envio"):
                st.write_state("envio", fase="desligado",
                               ultima_msg="Desligado — ligue na tela Envios.")
                self._faxina_de_orfas()
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                # Heartbeat também entre os ticks. O intervalo normal (~5min) é maior
                # que o TTL de 90s do worker_alive(), então sem renovar aqui um processo
                # vivo aparecia como morto/"Desligado" na tela — igual ao scrape.
                # Só o timestamp: fase/erro/proximo_ciclo já vêm do fim do tick, e
                # reescrevê-los aqui apagaria o erro do último ciclo na hora seguinte.
                st.write_state("envio")
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("envio", fase="processando", loja_atual=None)
                _renovar_conexoes_db()
                # Faxina antes do tick: fecha publicações que ficaram 'pendente' porque
                # o worker morreu no meio de um envio (deploy/crash). Nunca derruba o
                # tick — envio é o que importa aqui.
                self._faxina_de_orfas()
                # Purga do log 1x/dia. Mora neste loop porque é o único ligado o dia
                # todo em produção; se o envio estiver desligado nada gera evento, então
                # não purgar também não é problema. Nunca derruba o tick.
                hoje_purga = timezone.localdate()
                if ultima_purga != hoje_purga:
                    try:
                        from apps.scrapers.maintenance import purgar_eventos_antigos
                        apagados = purgar_eventos_antigos()
                        ultima_purga = hoje_purga
                        if apagados:
                            logger.info("Purga de eventos: %s linha(s) removida(s)", apagados)
                    except Exception as e:
                        logger.warning("Purga de eventos falhou: %s", e)
                # Antes de escolher a próxima oferta, fecha as pendências do ciclo
                # anterior: um envio que ficou "incerto" no orçamento do worker
                # muitas vezes já foi confirmado pelo ledger logo depois. Nada é
                # reenviado aqui — só o registro é corrigido. Try próprio porque
                # nenhuma reconciliação vale um tick de envio.
                try:
                    from apps.scrapers.send_pipeline import reconciliar_incertos
                    reconciliar_incertos()
                except Exception as e:
                    logger.warning("Reconciliação de envios incertos falhou: %s", e)
                fila = _consumir_fila_v2()
                res = processar_configs_de_envio()
                falhas_banco = 0
                enviados = sum(1 for r in res if r.get("sucesso"))
                # O watchdog de conexões saiu daqui: virou o processo `monitor` do
                # Procfile. Como este loop é gated pela flag "envio", o watchdog
                # herdava o gate — envio desligado, ninguém via queda nem retomada
                # de conexão, e os incidentes ficavam abertos para sempre.
                ticks += 1
                logger.info("[%s] tick: %s config(s) vencida(s), %s enviada(s)", agora.strftime("%H:%M"), len(res), enviados)
                st.write_state(
                    "envio", fase="aguardando", ticks=ticks,
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    vencidas=len(res), enviados=enviados, erro="",
                    fila_v2_processada=len(fila),
                    ultima_msg=f"{enviados} enviada(s) de {len(res)} vencida(s) às {agora:%H:%M}.",
                )
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("envio", e, falhas_banco)
                continue
            except Exception as e:
                logger.exception("Erro no tick de envio")
                # Tick inteiro morto = nenhum usuário recebe oferta neste ciclo.
                log_event("publicacao", "tick_erro", f"Ciclo de envio falhou: {e}",
                          level="error", contexto={"ticks": ticks}, exc=e)
                st.write_state(
                    "envio", fase="aguardando",
                    proximo_ciclo=(timezone.now() + timedelta(minutes=tick)).isoformat(),
                    erro=ERRO_PUBLICO,
                )
            proximo = timezone.now() + timedelta(minutes=tick)

    def _loop_relatorios(self, opts):
        from apps.scrapers.relatorios import sync_due_reports

        # Quem decide a cadência é o proxima_execucao de cada RelatorioSync (6h após
        # cada sync), e sync_due_reports já respeita isso — este loop só precisa
        # perguntar de vez em quando. O --tick de 360min era um segundo agendador por
        # cima do primeiro, e fazia o botão "Sincronizar" da tela esperar até 6h.
        POLL = 60
        logger.info("RELATORIOS worker no ar; checa vencidos a cada %ss quando ligado", POLL)
        ciclos = 0
        falhas_banco = 0
        proximo = timezone.now()
        while True:
            if not st.is_enabled("relatorios"):
                st.write_state("relatorios", fase="desligado",
                               ultima_msg="Desligado — ligue quando quiser sync automático.")
                time.sleep(POLL)
                continue
            if timezone.now() < proximo:
                st.write_state("relatorios", fase="aguardando_banco")
                time.sleep(POLL)
                continue
            agora = timezone.now()
            try:
                st.write_state("relatorios", fase="sincronizando", erro="")
                _renovar_conexoes_db()
                # Cada adapter adquire, em ordem estável, o slot global de Chromium
                # e a sessão exclusiva do portal. Um lock externo aqui causaria
                # auto-contenção ao tentar adquirir `django_chromium` novamente.
                with _heartbeat_durante("relatorios"):
                    resultados = sync_due_reports()
                falhas_banco = 0
                if not resultados:
                    # Nada vencido: não é um ciclo, é silêncio. Não mexe no estado
                    # visível pra não zerar o "última sincronização" da tela.
                    st.write_state("relatorios", fase="aguardando")
                    proximo = timezone.now() + timedelta(seconds=POLL)
                    time.sleep(POLL)
                    continue
                ok = sum(1 for s in resultados if s.status == "ok")
                acao = sum(1 for s in resultados if s.status == "acao")
                erros = sum(1 for s in resultados if s.status == "erro")
                ciclos += 1
                proximo = timezone.now() + timedelta(seconds=POLL)
                st.write_state(
                    "relatorios", fase="aguardando", ciclos=ciclos,
                    ultimo_ciclo_fim=timezone.now().isoformat(),
                    proximo_ciclo=proximo.isoformat(), ok=ok, acao=acao,
                    erro_count=erros,
                    ultima_msg=f"{ok} ok, {acao} ação, {erros} erro às {agora:%H:%M}.",
                    erro="",
                )
            except DatabaseError as e:
                falhas_banco += 1
                proximo = _pausar_por_banco("relatorios", e, falhas_banco)
            except Exception:
                logger.exception("Erro no sync de relatórios")
                st.write_state("relatorios", fase="aguardando", erro=ERRO_PUBLICO)
            time.sleep(POLL)

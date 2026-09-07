"""
Mercado Livre — wrapper sobre o código existente (scrapers + link.py).
Sem mudança de comportamento: só expõe o que já existe pelo contrato Marketplace.
"""
import logging

from apps.scrapers.marketplaces.base import Marketplace

logger = logging.getLogger(__name__)


class MercadoLivre(Marketplace):
    slug = "mercadolivre"

    def scrape_para_usuario(self, usuario, termos=None) -> int:
        """Feed público de ofertas do ML.

        O catálogo do ML é pool COMPARTILHADO (owner=None), então a coleta é a mesma
        para qualquer usuário — `usuario` serve só para o reporter de progresso.
        """
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import mapear_ofertas

        return mapear_ofertas(max_paginas=40, usuario=usuario) or 0

    # Componentes do `mercadolivre-web`. A fonte agregada continua sendo a dona dos
    # cupons (é o `fonte` de CupomNormalizado e a chave de `_FONTES_ML_ATIVACAO`),
    # mas a SAÚDE de cada coletor passa a ter linha própria: agrupados, a falha de um
    # deles tornava impossível saber qual estava obsoleto — o painel mostrava um
    # único badge para feed de ofertas, vitrine de checkout e campanhas autenticadas.
    COMPONENTES = {
        "mercadolivre-ofertas": "Mercado Livre — feed de ofertas",
        "mercadolivre-cupons-checkout": "Mercado Livre — vitrine de cupons",
        "mercadolivre-campanhas": "Mercado Livre — campanhas autenticadas",
    }

    @staticmethod
    def _componente(slug, nome):
        from apps.scrapers.models import FonteIngestao

        fonte, _ = FonteIngestao.objects.get_or_create(
            slug=slug, defaults={"marketplace": "mercadolivre", "nome": nome},
        )
        return fonte

    @classmethod
    def componente_habilitado(cls, slug) -> bool:
        """Kill switch individual: desligar um coletor não derruba os outros."""
        return cls._componente(slug, cls.COMPONENTES.get(slug, slug)).habilitada

    @classmethod
    def _reportar_componente(cls, slug, *, total, erro=""):
        """Publica o resultado de UM coletor, sem contaminar os vizinhos."""
        from django.utils import timezone

        fonte = cls._componente(slug, cls.COMPONENTES.get(slug, slug))
        agora = timezone.now()
        fonte.ultima_tentativa = agora
        fonte.ultimo_total = int(total or 0)
        if erro:
            fonte.status = "degraded"
            fonte.falhas_consecutivas += 1
            fonte.erro_publico = str(erro)[:255]
        else:
            fonte.status = "ok"
            fonte.ultimo_sucesso = agora
            fonte.falhas_consecutivas = 0
            fonte.erro_publico = "" if total else (
                "Coleta concluída sem itens novos; catálogo anterior preservado.")
        fonte.save(update_fields=[
            "ultima_tentativa", "ultimo_total", "status", "ultimo_sucesso",
            "falhas_consecutivas", "erro_publico",
        ])

    @classmethod
    def _coletar_componente(cls, slug, coletor, *, propagar=True):
        """Roda um coletor e registra a saúde DELE, preservando o fluxo de erro.

        `propagar=True` mantém exatamente o encaminhamento que já existia: quem
        derrubava o ciclo continua derrubando, quem era isolado continua isolado.
        A única novidade é que cada coletor passa a ter linha própria de saúde —
        antes, os três compartilhavam o badge de `mercadolivre-web` e não havia como
        saber qual deles estava obsoleto.

        Componente desligado no admin (`FonteIngestao.habilitada=False`) devolve 0
        sem executar: é o kill switch individual que o botão geral de raspagem não
        oferecia.
        """
        if not cls.componente_habilitado(slug):
            logger.info("Componente %s desabilitado; coleta pulada", slug)
            cls._reportar_componente(
                slug, total=0, erro="Coletor desligado na administração.",
            )
            return 0
        try:
            total = coletor() or 0
        except Exception as exc:
            from apps.scrapers.afiliado import causa_de_capacidade

            if causa_de_capacidade(exc):
                # Não houve veredito da origem. Manter a última saúde é essencial:
                # a disputa normal entre lanes não pode parecer quebra do ML.
                raise
            cls._reportar_componente(
                slug, total=0,
                erro="Falha temporária na coleta; dados anteriores preservados.",
            )
            if propagar:
                raise
            logger.warning("Componente %s falhou isoladamente: %s", slug, exc)
            return 0
        cls._reportar_componente(slug, total=total)
        return total

    def scrape_all(self, termos=None) -> None:
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
            mapear_ofertas, buscar_por_termo,
        )
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import mapear_cupons_codigo
        from apps.scrapers.scraper_mercadolivre.scraper import mapear_cupons, projetar_catalogo_cupons

        from django.utils import timezone
        from apps.scrapers.eventos import log_event
        from apps.scrapers.models import FonteIngestao, ExecucaoIngestao
        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web", defaults={
                "marketplace": "mercadolivre", "nome": "Mercado Livre — páginas públicas"})
        run = ExecucaoIngestao.objects.create(fonte=fonte)
        inicio_ciclo = timezone.now()
        fonte.ultima_tentativa = inicio_ciclo
        fonte.save(update_fields=["ultima_tentativa"])
        parciais = []
        try:
            ofertas = self._coletar_componente(
                "mercadolivre-ofertas", lambda: mapear_ofertas(max_paginas=40),
                propagar=True,
            )
            cupons_codigo = self._coletar_componente(
                "mercadolivre-cupons-checkout", mapear_cupons_codigo,
                propagar=True,
            )
            # Cupons de campanha (tabela Cupom). Estava fora do loop automático: só
            # rodava no clique manual da tela de Scraper, então em produção a tabela
            # ficava vazia -- e link.py aborta a geração de link quando o produto tem
            # campanha_id sem Cupom correspondente. Cupom faltando virava link pendente.
            #
            # Isolado do resto: o parser depende de um JSON embutido num bundle do ML
            # (__NORDIC_RENDERING_CTX__), que é a peça mais frágil daqui. Se ele
            # quebrar, as ofertas e os códigos ainda entram.
            try:
                cupons_campanha = self._coletar_componente(
                    "mercadolivre-campanhas", mapear_cupons, propagar=True,
                )
            except Exception as e:
                parciais.append("cupons de campanha")
                logger.warning("Raspagem de cupons de campanha ML falhou: %s", e)
                log_event("scraper", "cupons_campanha_erro",
                          f"Não foi possível raspar os cupons de campanha do ML: {e}",
                          level="warning", contexto={"marketplace": "mercadolivre"}, exc=e)
                cupons_campanha = 0
            # A aba "Cupons" do site lê só o CupomNormalizado. Projeta a tabela Cupom
            # (campanhas) para o catálogo, lendo o banco — vale mesmo quando a raspagem
            # deste ciclo veio vazia (anti-wipe preserva as campanhas anteriores).
            try:
                # Só o que ESTA execução observou renova a projeção — ver a
                # docstring de projetar_catalogo_cupons.
                projetar_catalogo_cupons(desde=inicio_ciclo)
            except Exception as e:
                parciais.append("projeção de cupons")
                logger.warning("Projeção do catálogo de cupons ML falhou: %s", e)
                log_event("scraper", "cupons_projecao_erro",
                          "Não foi possível publicar os cupons no catálogo.",
                          level="warning", contexto={"marketplace": "mercadolivre"}, exc=e)
            # Coleta oficial, preparo e afiliação pertencem ao pipeline central
            # ``automacao --modo cupons``. Este scraper mantém apenas as fontes
            # autenticadas do ML e o catálogo interno de campanhas.
            cupons = cupons_codigo
            for t in (termos or []):
                try:
                    buscar_por_termo(t)
                except Exception as e:
                    parciais.append("busca por termo")
                    logger.warning("Busca ML '%s' falhou: %s", t, e)
        except Exception as exc:
            now = timezone.now()
            from apps.scrapers.afiliado import causa_de_capacidade

            if causa_de_capacidade(exc):
                run.status, run.finalizada_em = "blocked", now
                run.health_status = "capacity_deferred"
                run.erro_publico = (
                    "Coleta adiada por capacidade; catálogo anterior preservado."
                )
                run.save(update_fields=[
                    "status", "finalizada_em", "health_status", "erro_publico",
                ])
                raise
            run.status, run.finalizada_em = "error", now
            run.erro_publico = "Falha temporária na coleta; dados anteriores preservados."
            run.save(update_fields=["status", "finalizada_em", "erro_publico"])
            fonte.status, fonte.erro_publico = "degraded", run.erro_publico
            fonte.falhas_consecutivas += 1
            fonte.save(update_fields=["status", "erro_publico", "falhas_consecutivas"])
            raise
        total = ofertas + cupons
        now = timezone.now()
        run.status = "ok" if total else "empty"
        run.total_ofertas, run.total_cupons = ofertas, cupons
        run.finalizada_em = now
        run.save()
        fonte.ultimo_total = total
        # O produto DESTA fonte são as ofertas das páginas públicas. A vitrine de
        # cupons (`/ofertas/cupons`) é gated por login e devolve zero produto por
        # experimento de layout, variação geográfica ou sessão ausente — situações
        # rotineiras que não significam que a fonte quebrou.
        #
        # Antes as duas medições eram um AND só (`not ofertas or not cupons`), então
        # 800 ofertas + zero produtos na vitrine de cupons marcava `degraded`,
        # nunca gravava `ultimo_sucesso` e somava `falhas_consecutivas` a cada ciclo.
        # O badge ficava em "Atenção" para sempre enquanto a coleta principal ia bem,
        # e uma quebra de verdade virava indistinguível do estado normal.
        degradou = bool(parciais or not ofertas)
        fonte.status = "degraded" if degradou else "ok"
        if degradou:
            componentes = ", ".join(sorted(set(parciais))) or "ofertas"
            fonte.erro_publico = (
                f"Coleta parcial ({componentes}); dados anteriores preservados."
            )[:255]
            fonte.falhas_consecutivas += 1
        else:
            fonte.ultimo_sucesso, fonte.falhas_consecutivas = now, 0
            # Vitrine vazia continua visível como aviso — só não derruba a fonte.
            fonte.erro_publico = "" if cupons else (
                "Ofertas coletadas; a vitrine de cupons não devolveu itens.")
        fonte.save()
        logger.info(
            "Raspagem ML: %s oferta(s), %s produto(s) na vitrine de cupons, "
            "%s campanha(s) personalizada(s) interna(s)",
            ofertas, cupons_codigo, cupons_campanha)
        # Cupons sumirem em silêncio foi um incidente real: o evento continua saindo
        # mesmo quando a fonte segue verde, porque é o único sinal de que a vitrine
        # parou de render.
        if degradou or not cupons:
            log_event("scraper", "cupons_vazios",
                      f"A raspagem do ML terminou parcialmente: {fonte.erro_publico}",
                      level="warning", contexto={"marketplace": "mercadolivre",
                                                 "ofertas": ofertas,
                                                 "cupons": cupons,
                                                 "componentes": parciais})

    def build_affiliate_link(self, produto, usuario=None, activation_key=""):
        from apps.scrapers.scraper_mercadolivre.link import gerar_link_afiliado_para_produto
        return gerar_link_afiliado_para_produto(
            produto, usuario=usuario, activation_key=activation_key,
        )

    def verify_affiliate_tag(self, link, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import link_tem_tag_afiliado
        return link_tem_tag_afiliado(link, usuario=usuario)

    def can_affiliate(self, produto, usuario=None) -> bool:
        # A identidade vem da conta autenticada no Link Builder, não de uma tag: ter o
        # link pré-gerado é a única evidência de atribuição disponível sem rede.
        #
        # O link mora em LinkAfiliadoUsuario (cada usuário afilia com a conta dele).
        # Este predicado lia só o Produto.link_afiliado global e por isso mostrava
        # "pendente" em item que o usuário já tinha afiliado. O campo global segue
        # como fallback pelos itens gerados antes do multi-tenant.
        from apps.scrapers.afiliado import link_cacheado
        cacheado = link_cacheado(usuario, produto)
        if cacheado and cacheado.link_afiliado:
            return True
        return bool(getattr(produto, "link_afiliado", ""))

    def preparar_exibicao(self, produtos, usuario=None) -> None:
        from apps.scrapers.afiliado import situacao_dos_links

        # UMA query para a página toda, e dela sai tudo: quem tem link e, para quem
        # não tem, POR QUE não tem. Marcar tudo de "pendente" era desonesto — um item
        # que o Programa de Afiliados nunca vai aceitar não está numa fila, e o
        # usuário ficava olhando uma pilha esperando link que jamais viria.
        situacao = situacao_dos_links(usuario, produtos)
        for p in produtos:
            info = situacao.get(p.id) or {}
            tem_link_usuario = bool(info.get("link_afiliado"))
            verificado = info.get("verificado_ok") if info else None
            # O link_afiliado do Produto é o fallback legado (pré-multi-tenant): não
            # há linha por usuário, então mantém o comportamento antigo (enviável).
            legado = (not info) and bool(getattr(p, "link_afiliado", ""))

            # ENVIÁVEL só quando o destino já foi aprovado (verificado_ok is True).
            # Ter um link cacheado NÃO basta: era exatamente isso que deixava um link
            # que caía na vitrine /social/ aparecer como enviável e só reprovar no
            # clique de enviar.
            p.afiliado_pronto = bool(
                (tem_link_usuario and verificado is True) or legado)

            if p.afiliado_pronto:
                p.afiliado_estado, p.afiliado_motivo = "pronto", ""
            elif tem_link_usuario and verificado is False:
                # Link existe mas o destino foi reprovado: mostra o motivo técnico
                # e NÃO oferece envio.
                p.afiliado_estado = "link_invalido"
                p.afiliado_motivo = info.get("verificacao_motivo") or info.get("ultimo_erro") or ""
            elif tem_link_usuario:
                # Link gerado, aguardando a conferência de destino (verificado_ok=None).
                p.afiliado_estado, p.afiliado_motivo = "verificando", ""
            elif info:
                p.afiliado_estado = info["estado"]
                p.afiliado_motivo = info["ultimo_erro"]
            else:
                # Nunca tentado ainda — está mesmo na fila.
                p.afiliado_estado, p.afiliado_motivo = "pendente", ""

    def verify_link(self, link, nome_esperado=None, confiar_desconto=False, usuario=None):
        from apps.scrapers.scraper_mercadolivre.link import verificar_link_afiliado
        return verificar_link_afiliado(link, nome_esperado=nome_esperado,
                                       confiar_desconto=confiar_desconto,
                                       usuario=usuario)

    def verificar_links_pendentes(self, usuario, limite=20, produto_ids=None):
        from apps.scrapers.scraper_mercadolivre.link import verificar_links_pendentes
        return verificar_links_pendentes(
            usuario, limite=limite, produto_ids=produto_ids,
        )

    def is_alive(self, produto):
        """Vivo pela vitrine primeiro; a PDP só como segunda opinião.

        A PDP responde interstitial de verificação para este IP desde 08/2026, e
        um interstitial não prova nada — nem que o anúncio existe, nem que sumiu.
        Confiar só nela deixava o portão cego (200 sem os termos de "pausado" =
        "vivo"); corrigir isso sem mais nada pararia o Mercado Livre inteiro, já
        que nenhuma PDP é legível daqui.

        `/ofertas` é a porta que responde. Estar na varredura do tique é prova
        POSITIVA e forte: o item existe, está anunciado e o preço acabou de ser
        lido — é a mesma leitura que `preco_ao_vivo` usa para revalidar. Não
        estar não prova o contrário (a vitrine mostra um recorte), então aí sim
        vale perguntar à PDP, que hoje devolve "indeterminado" quando bloqueada.
        """
        from apps.scrapers.ofertas import esta_vivo
        from apps.scrapers.preco_ao_vivo import varrer_ofertas_ml
        from apps.scrapers.scraper_mercadolivre.link import _extrair_item_id

        item = _extrair_item_id(str(getattr(produto, "link_produto", "") or ""))
        if item:
            try:
                if item in varrer_ofertas_ml():
                    return True
            except Exception as exc:  # varredura é oportunista, nunca veredito
                logger.info("varredura de ofertas indisponível em is_alive: %s",
                            str(exc)[:120])
        return esta_vivo(produto)

    def buscar_por_termo(self, termo_busca, min_desconto=15, macro=None, usuario=None):
        # ML = pool COMPARTILHADO (owner=None p/ todos): o RESULTADO não é do
        # usuário. `usuario` serve só para escolher a CREDENCIAL da navegação —
        # sem ele, a organização de sistema (ver scrapers/ml_auth.py).
        from apps.scrapers.scraper_mercadolivre.ofertas_scraper import buscar_por_termo
        return buscar_por_termo(termo_busca, min_desconto=min_desconto, macro=macro,
                                usuario=usuario)

    def prefetch_links(self, produtos, usuario=None, faixa=None, activation_keys=None):
        """Pré-gera links em lote (uma sessão Playwright). Retorna (gerados, falhas).

        O pool ML é compartilhado, mas a SESSÃO é por usuário: `usuario` diz qual
        auth_{id}.json abrir. Sem ele, link.py resolve a sessão disponível.
        """
        from apps.scrapers.carga import (
            BrowserResourceUnavailable, ml_site_browser_resource,
        )
        from apps.scrapers.scraper_mercadolivre.link import gerar_links_em_lote

        with ml_site_browser_resource(
            usuario, owner_kind="links_generate",
        ) as acquired:
            if not acquired:
                raise BrowserResourceUnavailable(
                    "Capacidade de browser ou sessão ML ocupada; links serão retomados."
                )
            return gerar_links_em_lote(
                produtos, usuario=usuario, faixa=faixa,
                activation_keys=activation_keys,
            )

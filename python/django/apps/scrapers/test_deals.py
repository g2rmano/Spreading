"""A camada Deal: produto do nicho + cupom aplicável, com preço final honesto.

Cada teste aqui corresponde a uma das metas de aceite da camada:

M1  o cupom vence pelo PREÇO FINAL, nunca por privilégio de tipo;
M2  `preco_final = vitrine - benefício`, sempre, e cupom perene não credita
    profundidade;
M3  termo negativo e faixa de preço são portões duros do nicho;
M6  shadow calcula o vencedor da camada e NÃO troca o envio.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import ensure_personal_organization
from apps.scrapers import deals
from apps.scrapers.models import (
    ConfiguracaoEnvio, CupomNormalizado, CupomValidacao, FonteIngestao,
    PrecoHistorico, Produto, ProdutoCupom,
)
from apps.scrapers.precos import chave_produto


class BaseDeals(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("deals", password="x")
        ensure_personal_organization(cls.user)
        cls.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-afiliados",
            defaults={"marketplace": "mercadolivre", "nome": "ML oficial",
                      "status": "ok"},
        )

    def _config(self, **kwargs):
        dados = {
            "owner": self.user, "grupo_id": "123@g.us", "grupo_nome": "Teste",
            "canal": "whatsapp", "min_desconto_percent": 5.0,
            "macro_categoria": "", "termo_busca": "",
        }
        dados.update(kwargs)
        return ConfiguracaoEnvio.objects.create(**dados)

    def _produto(self, *, nome="Fone Bluetooth JBL", preco=100.0, de=200.0,
                 link=None, macro="Eletrônicos"):
        return Produto.objects.create(
            owner=self.user, marketplace="mercadolivre", nome=nome, origem="oferta",
            preco_sem_desconto=de, preco_com_cupom=preco, macro_categoria=macro,
            confianca="alta",
            link_produto=link or f"https://produto.mercadolivre.com.br/MLB-{nome[:6]}",
        )

    def _observar(self, produto, *precos):
        for preco in precos:
            PrecoHistorico.objects.create(
                marketplace=produto.marketplace, chave=chave_produto(produto),
                preco=preco,
            )

    def _cupom(self, *, produto=None, codigo="PRESENTE", percentual=20.0, teto=100.0,
               minimo=0.0, primeira=None, sitewide=True, checkout=False):
        """Cupom + a prova de que ele se aplica AO PRODUTO.

        Sem `ProdutoCupom` confirmado o cupom não entra mais na conta do preço:
        "site inteiro" é alegação sobre a loja, não sobre o item, e foi assim que
        um código esgotado foi anunciado num air fryer em produção.
        """
        cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"checkout:{codigo}",
            marketplace="mercadolivre", titulo=f"Cupom {codigo}", codigo=codigo,
            redemption_mode="code", scope_type="sitewide", estado="ativo",
            confianca="alta",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": percentual,
                    "valor_minimo": minimo, "desconto_maximo": teto,
                    "modo_resgate": "codigo", "is_mar_aberto": bool(sitewide)},
            evidencia={"transport": "official-api", "association": "verified"},
        )
        if primeira is not None:
            CupomNormalizado.objects.filter(pk=cupom.pk).update(
                primeira_observacao=primeira)
            cupom.refresh_from_db()
        if produto is not None:
            ProdutoCupom.objects.get_or_create(
                produto=produto, cupom=cupom, defaults={"status": "confirmado"})
        if checkout:
            # `checkout=True` = o abatimento foi OBSERVADO num carrinho real. É a
            # única prova que autoriza a mensagem a anunciar um total com o cupom
            # descontado; sem ela o cupom aparece, mas sem conta.
            CupomValidacao.objects.create(
                usuario=self.user, cupom=cupom, marketplace="mercadolivre",
                cart_fingerprint=f"teste-{codigo}", status="approved",
            )
        return cupom


class PrecoFinalCoerenteTests(BaseDeals):
    """M2 — o preço anunciado é sempre vitrine menos o abatimento medido."""

    def test_preco_final_e_vitrine_menos_beneficio(self):
        produto = self._produto(preco=100.0)
        self._observar(produto, 180.0, 175.0, 170.0)
        self._cupom(produto=produto, percentual=20.0, teto=100.0, checkout=True)
        resultado = deals.gerar_deals(self._config(), limite=5)
        self.assertTrue(resultado)
        deal = resultado[0]
        self.assertTrue(deal.coerente())
        self.assertEqual(deal.beneficio_rs, 20.0)
        self.assertEqual(deal.preco_final, 80.0)

    def test_teto_do_cupom_limita_o_abatimento(self):
        """"50% OFF" com teto de R$1 nunca pode virar metade do preço."""
        produto = self._produto(preco=400.0, de=800.0)
        self._observar(produto, 700.0, 690.0, 680.0)
        # Teto de R$ 60 sobre um item de R$ 400: 50% seriam R$ 200.
        self._cupom(produto=produto, percentual=50.0, teto=60.0, checkout=True)
        resultado = deals.gerar_deals(self._config(), limite=5)
        deal = resultado[0]
        self.assertEqual(deal.beneficio_rs, 60.0)
        self.assertEqual(deal.preco_final, 340.0)
        self.assertTrue(deal.coerente())

    def test_prova_sempre_pertence_ao_conjunto_conhecido(self):
        produto = self._produto(preco=100.0)
        self._observar(produto, 180.0)
        self._cupom(produto=produto)
        for deal in deals.gerar_deals(self._config(), limite=None):
            self.assertIn(deal.prova, deals.PROVAS_VALIDAS)
            self.assertTrue(deal.coerente())


class CupomPereneTests(BaseDeals):
    """M2 — cupom que existe há meses já está embutido na série de vitrine."""

    # A vitrine (100) fica no MEIO da série; só o preço com cupom (80) chegaria ao
    # fundo. É exatamente aí que a distinção importa: se o cupom já existia durante
    # toda a série, esse "fundo" é ilusão de contabilidade.
    SERIE = (90.0, 100.0, 110.0)

    def test_cupom_perene_nao_credita_profundidade(self):
        produto = self._produto(preco=100.0)
        self._observar(produto, *self.SERIE)
        antigo = timezone.now() - timedelta(days=90)
        self._cupom(produto=produto, percentual=20.0, teto=100.0, primeira=antigo, checkout=True)
        resultado = deals.gerar_deals(self._config(), limite=5)
        self.assertTrue(resultado)
        deal = resultado[0]
        self.assertTrue(deal.cupom_perene)
        # Publicável, sim; "mínima histórica", não.
        self.assertNotIn("no fundo do histórico de 90 dias", " ".join(deal.motivos))
        self.assertEqual(deal.componentes["valor_real"], 0.0)

    def test_cupom_novo_credita_profundidade(self):
        produto = self._produto(preco=100.0)
        self._observar(produto, *self.SERIE)
        self._cupom(produto=produto, percentual=20.0, teto=100.0, primeira=timezone.now(), checkout=True)
        deal = deals.gerar_deals(self._config(), limite=5)[0]
        self.assertFalse(deal.cupom_perene)
        self.assertGreater(deal.componentes["valor_real"], 0.5)


class PrecoDeSempreTests(BaseDeals):
    """M2 — anunciar o preço de sempre queima o grupo; é rejeição dura."""

    def test_preco_no_patamar_da_mediana_e_rejeitado(self):
        """Vitrine de 50% OFF, mas o item sempre custou isso. Não sai."""
        from collections import defaultdict

        produto = self._produto(preco=100.0, de=200.0)
        self._observar(produto, 100.0, 100.5, 99.9)
        rejeicoes = defaultdict(int)
        resultado = deals.gerar_deals(self._config(), limite=5, rejeicoes=rejeicoes)
        self.assertEqual(resultado, [])
        self.assertEqual(rejeicoes[deals.MOTIVO_PRECO_DE_SEMPRE], 1)


class NichoTests(BaseDeals):
    """M3 — o nicho agora sabe excluir, não só incluir."""

    def test_termo_negativo_rejeita_o_item(self):
        self._produto(nome="Capa para Fone JBL", preco=30.0, de=60.0)
        self._observar(Produto.objects.get(nome="Capa para Fone JBL"), 55.0)
        config = self._config(termo_busca="fone", termos_negativos="capa, película")
        from collections import defaultdict
        rejeicoes = defaultdict(int)
        resultado = deals.gerar_deals(config, limite=5, rejeicoes=rejeicoes)
        self.assertEqual(resultado, [])
        self.assertEqual(rejeicoes[deals.MOTIVO_TERMO_NEGATIVO], 1)

    def test_faixa_de_preco_usa_o_preco_final(self):
        """A faixa julga o que o comprador paga, não a vitrine."""
        produto = self._produto(preco=100.0)
        self._observar(produto, 180.0, 175.0, 170.0)
        self._cupom(produto=produto, percentual=20.0, teto=100.0, checkout=True)   # final = 80
        config = self._config(preco_min=90.0)
        from collections import defaultdict
        rejeicoes = defaultdict(int)
        self.assertEqual(deals.gerar_deals(config, limite=5, rejeicoes=rejeicoes), [])
        self.assertEqual(rejeicoes[deals.MOTIVO_FORA_DA_FAIXA], 1)

    def test_termo_positivo_pontua_afinidade(self):
        produto = self._produto(nome="Fone Bluetooth JBL Tune", preco=100.0)
        self._observar(produto, 180.0)
        com_termo = deals.gerar_deals(self._config(termo_busca="fone"), limite=1)[0]
        sem_termo = deals.gerar_deals(self._config(), limite=1)[0]
        self.assertGreater(com_termo.componentes["afinidade"],
                           sem_termo.componentes["afinidade"])


class BeneficioNoItemTests(BaseDeals):
    """O portão que faltava: o cupom vale alguma coisa NESTE produto?"""

    def test_cupom_irrelevante_no_item_caro_nao_entra(self):
        produto = self._produto(preco=4000.0, de=6000.0)
        self._observar(produto, 5500.0, 5400.0, 5300.0)
        # R$ 20 num item de R$ 4.000 = 0,5%, abaixo do piso de 5%.
        self._cupom(produto=produto, percentual=50.0, teto=20.0)
        deal = deals.gerar_deals(self._config(), limite=1)[0]
        self.assertIsNone(deal.cupom)
        self.assertEqual(deal.prova, deals.PROVA_SEM_CUPOM)
        self.assertEqual(deal.beneficio_rs, 0.0)

    def test_mesmo_cupom_entra_num_item_de_ticket_baixo(self):
        produto = self._produto(nome="Caneca", preco=100.0, de=200.0)
        self._observar(produto, 180.0)
        self._cupom(produto=produto, percentual=50.0, teto=20.0)   # R$ 20 = 20% de R$ 100
        deal = deals.gerar_deals(self._config(), limite=1)[0]
        self.assertIsNotNone(deal.cupom)
        self.assertEqual(deal.beneficio_rs, 20.0)


class OrdenacaoTests(BaseDeals):
    """M1 — cupom não tem privilégio: vence pelo preço final ou não vence."""

    def test_deal_com_cupom_vence_mesmo_com_score_menor(self):
        """Oferta sem cupom é o fundo da fila, não concorrente.

        Decisão de operação, medida no grupo: oferta sem cupom vende muito menos.
        O score continua ordenando dentro de cada grupo, mas não promove um item
        sem cupom acima de um par produto+cupom.
        """
        # A: sem cupom, colado na mínima de 90 dias — score alto.
        a = self._produto(nome="Produto A", preco=50.0, de=200.0,
                          link="https://produto.mercadolivre.com.br/MLB-A")
        self._observar(a, 200.0, 190.0, 180.0)
        # B: com cupom provado, mas preço bem menos impressionante.
        b = self._produto(nome="Produto B", preco=170.0, de=200.0,
                          link="https://produto.mercadolivre.com.br/MLB-B")
        self._observar(b, 200.0, 195.0, 190.0)
        self._cupom(produto=b, percentual=10.0, teto=100.0)
        resultado = deals.gerar_deals(self._config(), limite=5)
        self.assertEqual(resultado[0].produto.pk, b.pk)
        self.assertTrue(resultado[0].tem_cupom)
        self.assertFalse(resultado[1].tem_cupom)
        # E o score do perdedor continua sendo maior: a ordem é por cupom, e a
        # nota não foi adulterada para justificá-la.
        self.assertGreater(resultado[1].score, resultado[0].score)

    def test_cupom_vence_quando_derruba_de_verdade_o_preco(self):
        a = self._produto(nome="Produto A", preco=150.0, de=200.0,
                          link="https://produto.mercadolivre.com.br/MLB-A")
        self._observar(a, 200.0, 195.0, 190.0)
        b = self._produto(nome="Produto B", preco=150.0, de=200.0,
                          link="https://produto.mercadolivre.com.br/MLB-B")
        self._observar(b, 200.0, 195.0, 190.0)
        cupom = self._cupom(percentual=40.0, teto=100.0, sitewide=False)
        # Prova por associação confirmada, só para o B.
        ProdutoCupom.objects.create(produto=b, cupom=cupom, status="confirmado")
        resultado = deals.gerar_deals(self._config(), limite=5)
        self.assertEqual(resultado[0].produto.pk, b.pk)
        self.assertEqual(resultado[0].prova, deals.PROVA_CONFIRMADA)


class RankingIntegradoTests(BaseDeals):
    """M6 — shadow observa, live decide; e o legado continua íntegro."""

    def _cenario(self):
        produto = self._produto(preco=100.0)
        self._observar(produto, 180.0, 175.0, 170.0)
        self._cupom(produto=produto, percentual=20.0, teto=100.0, checkout=True)
        return self._config()

    @override_settings(DEAL_LAYER_SHADOW=True, DEAL_LAYER_LIVE=False)
    def test_shadow_registra_e_nao_altera_o_vencedor(self):
        from apps.scrapers.content_ranking import selecionar_conteudo_para_grupo
        from apps.scrapers.models import EventoOperacional

        config = self._cenario()
        pool = selecionar_conteudo_para_grupo(config, limit=3)
        self.assertTrue(all(item.kind != "deal" for item in pool))
        self.assertTrue(EventoOperacional.objects.filter(
            pipeline="selecao", evento="deal_shadow").exists())

    @override_settings(DEAL_LAYER_SHADOW=False, DEAL_LAYER_LIVE=True,
                       DEAL_LAYER_LIVE_PILOT_ORGANIZATION_IDS=None,
                       PILOT_ORGANIZATION_IDS=set())
    def test_sem_piloto_a_camada_nao_publica(self):
        """Flag global ligada não basta: recurso de efeito externo exige piloto."""
        from apps.scrapers.content_ranking import selecionar_conteudo_para_grupo

        config = self._cenario()
        pool = selecionar_conteudo_para_grupo(config, limit=3)
        self.assertTrue(all(item.kind != "deal" for item in pool))

    @override_settings(DEAL_LAYER_SHADOW=False, DEAL_LAYER_LIVE=True)
    def test_com_piloto_a_camada_publica_deals(self):
        from apps.accounts.models import organization_for_user
        from apps.scrapers.content_ranking import selecionar_conteudo_para_grupo

        config = self._cenario()
        organizacao = organization_for_user(self.user)
        with override_settings(
            DEAL_LAYER_LIVE_PILOT_ORGANIZATION_IDS={str(organizacao.pk)},
        ):
            pool = selecionar_conteudo_para_grupo(config, limit=3)
        self.assertTrue(pool)
        self.assertEqual(pool[0].kind, "deal")
        self.assertTrue(pool[0].obj.coerente())


class MensagemDealTests(BaseDeals):
    """A mensagem: foto do produto, texto humano da IA e números do código."""

    def _deal(self, **kwargs):
        produto = self._produto(preco=100.0)
        self._observar(produto, 180.0, 175.0, 170.0)
        self._cupom(produto=produto, percentual=20.0, teto=100.0, checkout=True)
        resultado = deals.gerar_deals(self._config(), limite=1)
        return resultado[0]

    def test_mensagem_nao_repete_o_nome_do_produto(self):
        """Nome uma vez. Gancho + nome + frase diziam a mesma coisa três vezes."""
        from apps.scrapers.ofertas import montar_mensagem_deal

        deal = self._deal()
        texto = montar_mensagem_deal(
            deal, "https://meli.la/abc", usuario=self.user,
            texto_ia={"linha": "Fone Bluetooth JBL para ouvir o dia inteiro"},
        )
        self.assertEqual(texto.count("Fone Bluetooth JBL"), 1)
        self.assertIn("R$ 80", texto)
        self.assertIn("PRESENTE", texto)
        self.assertIn("https://meli.la/abc", texto)

    def test_frase_que_so_repete_o_nome_nao_entra(self):
        from apps.scrapers.ofertas import _frase_acrescenta

        nome = "Fone Bluetooth JBL Tune 510BT"
        self.assertEqual(_frase_acrescenta("Fone bluetooth JBL Tune", nome), "")
        frase = "Bateria longa e dobrável para jogar na mochila"
        self.assertEqual(_frase_acrescenta(frase, nome), frase)

    def test_sem_texto_de_ia_a_mensagem_continua_completa(self):
        from apps.scrapers.ofertas import montar_mensagem_deal

        deal = self._deal()
        texto = montar_mensagem_deal(deal, "https://meli.la/abc", usuario=self.user)
        self.assertIn("R$ 80", texto)
        self.assertIn("PRESENTE", texto)

    def test_sem_prova_de_historico_nao_ha_frase_de_menor_preco(self):
        from apps.scrapers.ofertas import _linha_prova_do_deal

        deal = deals.DealCandidate(
            produto=self._produto(), preco_vitrine=100.0, preco_final=100.0,
            historico=None,
        )
        self.assertEqual(_linha_prova_do_deal(deal), "")

    def test_serie_curta_nao_sustenta_afirmacao_publica(self):
        """Duas leituras nao autorizam dizer "menor preco em 90 dias".

        O score pode se apoiar no que houver: e numero interno, so ordena. Esta
        linha vai assinada pelo creator no grupo, e e a mesma classe de erro que
        mandou "De R$ 289 por R$ 183,91" para um grupo real em 03/09.
        """
        from apps.scrapers.ofertas import _linha_prova_do_deal

        produto = self._produto()
        curto = deals.DealCandidate(
            produto=produto, preco_vitrine=80.0, preco_final=80.0,
            historico={"n": 2, "mediana": 160.0, "minimo": 80.0},
        )
        self.assertEqual(_linha_prova_do_deal(curto), "")

        sustentado = deals.DealCandidate(
            produto=produto, preco_vitrine=80.0, preco_final=80.0,
            historico={"n": 9, "mediana": 160.0, "minimo": 80.0},
        )
        self.assertEqual(
            _linha_prova_do_deal(sustentado),
            "Menor preco que observamos em 90 dias".replace("preco", "preço"),
        )

    def test_a_prova_de_preco_chega_na_mensagem(self):
        """A funcao existia, era testada, e nenhuma mensagem a chamava.

        E a unica frase da mensagem que espelhador e formatador nao conseguem
        escrever: eles repassam o que a loja diz, sem serie propria de preco.
        """
        from apps.scrapers.ofertas import montar_mensagem_deal

        deal = self._deal()
        deal.historico = {"n": 9, "mediana": 160.0, "minimo": 80.0}
        texto = montar_mensagem_deal(deal, "https://meli.la/abc", usuario=self.user)
        self.assertIn("90 dias", texto)

    def test_o_preco_antigo_nao_e_anunciado_como_alta(self):
        """"chega a custar R$ X" le como se o preco fosse SUBIR."""
        from apps.scrapers.ofertas import montar_mensagem_deal

        deal = self._deal()
        deal.desconto_comprovado = True
        texto = montar_mensagem_deal(deal, "https://meli.la/abc", usuario=self.user)
        self.assertNotIn("chega a custar", texto)


class TextoIATests(TestCase):
    """O modelo pode vender; não pode inventar número nem alegação."""

    PERMITIDOS = {"80", "20", "510"}

    def _vendavel(self, texto, *, provas=()):
        from apps.scrapers.llm import _frase_vendavel

        return _frase_vendavel(
            texto, permitidos=self.PERMITIDOS, provas=set(provas))

    def test_numero_da_lista_passa(self):
        """O padrão do mercado põe o preço na frase. Isso é permitido."""
        frase = "São R$ 20 que o cupom tira no checkout, confira antes de fechar."
        self.assertEqual(self._vendavel(frase), frase)

    def test_numero_fora_da_lista_derruba_a_frase(self):
        """Preço inventado é o defeito mais caro do produto. Campo cai inteiro."""
        self.assertEqual(self._vendavel("Sai por R$ 59 hoje"), "")
        self.assertEqual(self._vendavel("Fica 45% mais barato"), "")

    def test_numero_do_nome_do_produto_e_liberado_pelo_chamador(self):
        from apps.scrapers.llm import numeros_do_texto

        self.assertEqual(
            numeros_do_texto("Fone JBL Tune 510BT"), {"510"})
        self.assertEqual(numeros_do_texto("R$ 1.299,00"), {"1299"})
        self.assertEqual(numeros_do_texto("R$ 80,50"), {"80.5"})

    def test_menor_preco_sem_prova_e_recusado(self):
        self.assertEqual(self._vendavel("O menor preço do ano nesse fone"), "")

    def test_menor_preco_com_prova_passa(self):
        frase = "O menor preço que a gente viu nele em 90 dias."
        self.assertEqual(
            self._vendavel(frase, provas=("minima",)),
            "",  # 90 não está na lista de números liberados
        )
        curta = "É o menor preço que a gente já viu nele."
        self.assertEqual(self._vendavel(curta, provas=("minima",)), curta)

    def test_urgencia_e_estoque_sem_prova_sao_recusados(self):
        self.assertEqual(self._vendavel("Última chance, acaba hoje"), "")
        self.assertEqual(self._vendavel("Correndo, últimas unidades"), "")
        self.assertEqual(self._vendavel("Ainda tem frete grátis"), "")

    def test_jargao_de_palhacada_continua_barrado(self):
        self.assertEqual(self._vendavel("Imperdível, corre que acaba"), "")
        self.assertEqual(self._vendavel("Clique aqui e garanta já"), "")

    def test_gancho_nomeia_produto_e_numero(self):
        from apps.scrapers.llm import _gancho_de_venda

        gancho = "JBL TUNE 510BT A R$ 80 COM CUPOM"
        self.assertEqual(
            _gancho_de_venda(gancho, self.PERMITIDOS, set()), gancho)

    def test_gancho_com_preco_inventado_cai(self):
        from apps.scrapers.llm import _gancho_de_venda

        self.assertEqual(
            _gancho_de_venda("FONE JBL POR R$ 59", self.PERMITIDOS, set()), "")

    def test_gancho_curto_demais_cai(self):
        from apps.scrapers.llm import _gancho_de_venda

        self.assertEqual(_gancho_de_venda("FONE JBL", self.PERMITIDOS, set()), "")


class FatosDoDealTests(BaseDeals):
    """A lista branca entregue ao modelo é a mesma que a mensagem imprime."""

    def _deal_com_minima(self):
        produto = self._produto(preco=100.0)
        self._observar(produto, 180.0, 175.0, 170.0)
        self._cupom(produto=produto, percentual=20.0, teto=100.0, checkout=True)
        return deals.gerar_deals(self._config(), limite=1)[0]

    def test_fatos_liberam_exatamente_os_numeros_da_mensagem(self):
        from apps.scrapers.ofertas import _fatos_do_deal

        fatos = self._deal_com_minima()
        dados = _fatos_do_deal(fatos)
        self.assertEqual(dados["preco_final"], 80.0)
        self.assertEqual(dados["beneficio_cupom"], 20.0)
        self.assertEqual(dados["economia"], 120.0)
        self.assertEqual(dados["percentual"], 60)

    def test_minima_nunca_e_autorizada_como_alegacao(self):
        """"Menor preço em 90 dias" saiu da mensagem — inclusive da frase da IA.

        Removê-la só da linha própria e deixar o modelo dizer o mesmo dentro do
        texto não é remover, é mudar de lugar.
        """
        from apps.scrapers.ofertas import _fatos_do_deal

        self.assertNotIn("minima", _fatos_do_deal(self._deal_com_minima())["provas"])

    def test_sem_historico_nao_autoriza_alegacao_nenhuma(self):
        from apps.scrapers.ofertas import _fatos_do_deal

        deal = deals.DealCandidate(
            produto=self._produto(), preco_vitrine=100.0, preco_final=100.0,
            historico=None,
        )
        dados = _fatos_do_deal(deal)
        self.assertEqual(dados["provas"], set())
        self.assertIsNone(dados["economia"])


class PoolReservaFatiaParaCupomTests(BaseDeals):
    """Produto com cupom provado nao pode ser cortado por ser menos recente.

    O pool ordenava so por observacao mais recente e cortava no teto. Produto com
    par confirmado costuma ser mais velho no catalogo, entao caia fora antes de
    ser avaliado. Medido em producao em 04/09/2026: 3.183 produtos com par
    confirmado e cupom ativo, pool de 400, interseccao de 20 — o sistema tinha a
    materia-prima do proprio produto e a descartava por recencia.
    """

    def _envelhecer(self, produto, horas):
        Produto.objects.filter(pk=produto.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=horas))

    @override_settings()
    def test_item_com_cupom_entra_mesmo_fora_do_teto_de_recencia(self):
        from apps.scrapers import ofertas

        # Dois produtos recentes ocupam um teto de dois lugares.
        for i in range(2):
            recente = self._produto(nome=f"Recente {i}", preco=100.0, de=200.0,
                                    link=f"https://produto.mercadolivre.com.br/R{i}")
            self._observar(recente, 190.0, 185.0, 180.0)
            self._envelhecer(recente, 1)

        antigo = self._produto(nome="Antigo com cupom", preco=100.0, de=200.0,
                               link="https://produto.mercadolivre.com.br/ANTIGO")
        self._observar(antigo, 190.0, 185.0, 180.0)
        self._envelhecer(antigo, 20)
        self._cupom(produto=antigo, codigo="COMCUPOM", percentual=20.0, teto=100.0)

        original = ofertas.TETO_CANDIDATOS
        try:
            ofertas.TETO_CANDIDATOS = 2   # os dois recentes enchem o teto
            ids = {p.pk for p in ofertas.pool_de_produtos_elegiveis(usuario=self.user)}
        finally:
            ofertas.TETO_CANDIDATOS = original

        self.assertIn(
            antigo.pk, ids,
            "Produto com par confirmado foi cortado do pool por ser menos recente.",
        )

    def test_item_sem_desconto_de_vitrine_entra_pelo_cupom(self):
        """O abatimento dele E o cupom; exigir vitrine elimina o deal por construcao.

        Medido em producao em 04/09/2026: dos 226 produtos com par confirmado,
        cupom ativo e ficha completa, exemplos reais sao "de R$ 76,95 por R$ 76,95"
        e "de R$ 229,57 por R$ 229,57" — vitrine sem desconto nenhum. O piso de 15%
        de vitrine rodava ANTES de olhar o cupom, entao cortava exatamente o deal
        que este produto existe para publicar.
        """
        from apps.scrapers import ofertas

        sem_vitrine = self._produto(
            nome="So o cupom desconta", preco=100.0, de=100.0,
            link="https://produto.mercadolivre.com.br/SOCUPOM")
        self._observar(sem_vitrine, 100.0, 100.0, 100.0)
        self._cupom(produto=sem_vitrine, codigo="SOCUPOM", percentual=20.0, teto=100.0)

        ids = {p.pk for p in ofertas.pool_de_produtos_elegiveis(usuario=self.user)}
        self.assertIn(
            sem_vitrine.pk, ids,
            "Item cujo desconto vem do cupom foi cortado pelo piso de vitrine.",
        )

    def test_origem_cupom_so_entra_quando_tem_par_confirmado(self):
        """Readmitir `origem=cupom` nao pode virar porta aberta para o resto."""
        from apps.scrapers import ofertas

        orfao = Produto.objects.create(
            owner=self.user, marketplace="mercadolivre", nome="Cupom sem par",
            origem="cupom", preco_sem_desconto=100.0, preco_com_cupom=100.0,
            confianca="alta",
            link_produto="https://produto.mercadolivre.com.br/ORFAO",
        )
        self._observar(orfao, 100.0, 100.0, 100.0)

        ids = {p.pk for p in ofertas.pool_de_produtos_elegiveis(usuario=self.user)}
        self.assertNotIn(orfao.pk, ids)

    def test_a_fatia_de_cupom_tem_teto_proprio(self):
        """Encher um lado nao pode esvaziar o outro."""
        from apps.scrapers import ofertas

        recente = self._produto(nome="So preco", preco=100.0, de=200.0,
                                link="https://produto.mercadolivre.com.br/SOPRECO")
        self._observar(recente, 190.0, 185.0, 180.0)
        self._envelhecer(recente, 1)

        for i in range(3):
            item = self._produto(nome=f"Com cupom {i}", preco=100.0, de=200.0,
                                 link=f"https://produto.mercadolivre.com.br/C{i}")
            self._observar(item, 190.0, 185.0, 180.0)
            self._envelhecer(item, 20 + i)
            self._cupom(produto=item, codigo=f"CUP{i}", percentual=20.0, teto=100.0)

        original_teto = ofertas.TETO_CANDIDATOS
        original_cupom = ofertas.TETO_CANDIDATOS_COM_CUPOM
        try:
            ofertas.TETO_CANDIDATOS = 1
            ofertas.TETO_CANDIDATOS_COM_CUPOM = 2
            ids = {p.pk for p in ofertas.pool_de_produtos_elegiveis(usuario=self.user)}
        finally:
            ofertas.TETO_CANDIDATOS = original_teto
            ofertas.TETO_CANDIDATOS_COM_CUPOM = original_cupom

        # O de preco puro continua no pool, e a fatia de cupom respeita o proprio teto.
        self.assertIn(recente.pk, ids)
        self.assertEqual(len(ids), 3)


class CupomSemProvaNaoEntraNaContaTests(BaseDeals):
    """O caso real de 07/09/2026 — o cooktop.

    A vitrine do Mercado Livre anunciava R$ 578,55 para um cooktop cujo carrinho
    cobrava R$ 609: a diferença exata de 5%, ou seja, o preço da vitrine JÁ era
    pós-cupom — e o produto não trazia prova nenhuma disso (`evidencia` só tinha
    `transport`). Em cima desse preço o sistema abateu um SEGUNDO cupom, o
    LIBERAESSA, de R$ 46,28, e publicou R$ 532,27. No checkout o LIBERAESSA estava
    esgotado e o cliente via R$ 609.

    Dois descontos empilhados, nenhum dos dois comprovado. O cupom continua na
    mensagem — é ele que vende — mas sem virar uma conta que ninguém conferiu.
    """

    def test_sem_prova_de_checkout_o_preco_nao_muda(self):
        produto = self._produto(preco=578.55, de=659.0)
        self._observar(produto, 700.0, 690.0, 680.0)
        self._cupom(produto=produto, codigo="LIBERAESSA", percentual=8.0, teto=100.0)

        deal = deals.gerar_deals(self._config(), limite=5)[0]

        self.assertTrue(deal.tem_cupom)
        self.assertEqual(deal.preco_final, 578.55)      # o preço medido, e só ele
        self.assertEqual(deal.beneficio_publicavel, 0.0)
        self.assertTrue(deal.coerente())

    def test_com_prova_de_checkout_a_conta_volta(self):
        produto = self._produto(preco=578.55, de=659.0)
        self._observar(produto, 700.0, 690.0, 680.0)
        self._cupom(produto=produto, codigo="LIBERAESSA", percentual=8.0, teto=100.0,
                    checkout=True)

        deal = deals.gerar_deals(self._config(), limite=5)[0]

        self.assertGreater(deal.beneficio_publicavel, 0.0)
        self.assertLess(deal.preco_final, 578.55)
        self.assertTrue(deal.coerente())

    def test_mensagem_sem_prova_nao_anuncia_abatimento(self):
        from apps.scrapers.ofertas import montar_mensagem_deal

        produto = self._produto(preco=578.55, de=659.0)
        self._observar(produto, 700.0, 690.0, 680.0)
        self._cupom(produto=produto, codigo="LIBERAESSA", percentual=8.0, teto=100.0)
        deal = deals.gerar_deals(self._config(), limite=5)[0]

        texto = montar_mensagem_deal(deal, "https://meli.la/x")

        self.assertIn("LIBERAESSA", texto)          # o cupom continua vendendo
        self.assertNotIn("abate R$", texto)         # sem conta não comprovada
        self.assertIn("578,55", texto)              # o número medido
        self.assertNotIn("532,27", texto)           # o número inventado

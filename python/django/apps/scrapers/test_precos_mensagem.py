"""Preço publicado x preço da vitrine.

A página oficial de cupons da Amazon mostra dois números: o da vitrine e o que o
cliente paga depois de ativar o cupom. A mensagem precisa anunciar o segundo —
anunciar o primeiro junto de "ative o cupom" promete um valor que a página não
cobra.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase

from apps.scrapers.models import Produto
from apps.scrapers.ofertas import (
    _preco_br, anotacao_preco_publicado, montar_mensagem, preco_publicavel,
)
from apps.scrapers.sources.amazon_coupons import _money


class PrecoPublicavelTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("preco-user")

    def _produto(self, **campos):
        base = {
            "owner": self.user, "marketplace": "amazon", "asin": "B012345678",
            "nome": "Cafeteira Expresso", "origem": "oferta",
            "link_produto": "https://www.amazon.com.br/dp/B012345678",
            "fonte": "amazon-public-coupons", "estado": "ativo",
            "preco_sem_desconto": 120.0, "preco_com_cupom": 100.0,
            "preco_efetivo": 90.0,
            "evidencia": {"promotion": {"present": True, "coupon_confirmed": True}},
        }
        base.update(campos)
        produto = Produto.objects.create(**base)
        # Observação NOSSA de que o item já custou o preço de lista. Sem ela o "DE"
        # não aparece — ver `_desconto_comprovado`: a mensagem não risca um preço
        # que nunca observamos. Estes testes medem QUAL número é anunciado, não a
        # prova, então a prova entra na fixture.
        from apps.scrapers.models import PrecoHistorico
        from apps.scrapers.precos import chave_produto

        PrecoHistorico.objects.create(
            marketplace=produto.marketplace, chave=chave_produto(produto),
            preco=float(base["preco_sem_desconto"]),
        )
        return produto

    def test_usa_preco_efetivo_quando_menor_que_a_vitrine(self):
        produto = self._produto()
        self.assertEqual(preco_publicavel(produto), 90.0)

    def test_cai_na_vitrine_quando_nao_ha_preco_efetivo(self):
        self.assertEqual(preco_publicavel(self._produto(preco_efetivo=0)), 100.0)

    def test_ignora_preco_efetivo_incoerente(self):
        """Efetivo maior que a vitrine é dado corrompido, não promoção."""
        self.assertEqual(preco_publicavel(self._produto(preco_efetivo=150.0)), 100.0)

    def test_mensagem_anuncia_o_preco_pago_junto_da_linha_de_cupom(self):
        produto = self._produto()
        texto = montar_mensagem(produto, "https://link", None)

        self.assertIn("POR 90", texto)
        self.assertNotIn("POR 100", texto)
        # O "DE" continua sendo a referência da vitrine riscada.
        self.assertIn("120", texto)
        self.assertIn("CUPOM: ative na Amazon — o preço já é com ele", texto)

    def test_produto_sem_cupom_mantem_o_preco_de_vitrine(self):
        produto = self._produto(
            preco_efetivo=0, evidencia={}, fonte="amazon-creators-api",
        )
        texto = montar_mensagem(produto, "https://link", None)
        self.assertIn("POR 100", texto)
        self.assertNotIn("CUPOM", texto)


class ParidadeTelaMensagemTests(TestCase):
    """A tela e a mensagem têm de dizer o MESMO número.

    A lista mostrava `preco_com_cupom` e a mensagem publicava `preco_publicavel()`:
    o item aparecia com um preço na tela e saía com outro no WhatsApp.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("paridade-user")

    def test_anotacao_sql_bate_com_a_funcao_python(self):
        casos = [
            ("sem efetivo", 0.0),
            ("efetivo menor (cupom de ativação)", 90.0),
            ("efetivo igual", 100.0),
            ("efetivo maior — dado corrompido", 150.0),
        ]
        for indice, (rotulo, efetivo) in enumerate(casos):
            with self.subTest(rotulo):
                # ASIN por caso: a chave natural de Produto não aceita duas
                # linhas do mesmo dono com o mesmo ASIN.
                produto = Produto.objects.create(
                    owner=self.user, marketplace="amazon",
                    asin=f"B0PARIDAD{indice}",
                    nome=f"Item {efetivo}", origem="oferta", estado="ativo",
                    link_produto=f"https://www.amazon.com.br/dp/{efetivo}",
                    preco_sem_desconto=120.0, preco_com_cupom=100.0,
                    preco_efetivo=efetivo,
                )
                anotado = (Produto.objects.filter(pk=produto.pk)
                           .annotate(preco_publicado=anotacao_preco_publicado())
                           .first())
                self.assertEqual(anotado.preco_publicado, preco_publicavel(produto))

    def test_preco_efetivo_nulo_cai_na_vitrine(self):
        produto = Produto.objects.create(
            owner=self.user, marketplace="mercadolivre", nome="Sem efetivo",
            origem="oferta", estado="ativo",
            link_produto="https://ml.com.br/x/p/MLB9",
            preco_sem_desconto=120.0, preco_com_cupom=100.0, preco_efetivo=None,
        )
        anotado = (Produto.objects.filter(pk=produto.pk)
                   .annotate(preco_publicado=anotacao_preco_publicado()).first())
        self.assertEqual(anotado.preco_publicado, 100.0)
        self.assertEqual(preco_publicavel(produto), 100.0)


class MensagemDeProdutoDeCupomMLTests(TestCase):
    """Produto de campanha do ML anuncia a VITRINE — o número que a página mostra.

    O cupom do ML só entra depois de ativar o link, então publicar o pós-cupom
    fazia a mensagem prometer um valor que a página não cobrava.
    """

    def test_anuncia_a_vitrine_e_manda_ativar_o_cupom_no_link(self):
        from apps.scrapers.models import Cupom

        produto = Produto.objects.create(
            owner=None, marketplace="mercadolivre", nome="Cafeteira Expresso",
            origem="cupom", campanha_id="99", fonte="mercadolivre-cupom",
            estado="ativo", link_produto="https://ml.com.br/cafeteira/p/MLB1",
            preco_sem_desconto=250.0, preco_com_cupom=100.0, preco_efetivo=100.0,
        )
        cupom = Cupom(campanha_id="99", titulo="20% OFF",
                      tipo_desconto="porcentagem", valor_desconto=20)
        # Prova de que o item já custou a vitrine. Sem observação nossa o "DE" não
        # é impresso (`_desconto_comprovado`), e o que este teste mede é QUAL preço
        # a campanha do ML anuncia — não a existência da prova.
        from apps.scrapers.models import PrecoHistorico
        from apps.scrapers.precos import chave_produto

        PrecoHistorico.objects.create(
            marketplace=produto.marketplace, chave=chave_produto(produto),
            preco=250.0,
        )

        texto = montar_mensagem(produto, "https://meli.la/abc", cupom)

        self.assertIn("POR 100", texto)
        self.assertIn("DE ", texto)          # 250, o preço de lista
        self.assertIn("250", texto)
        self.assertIn("CUPOM: ative no link", texto)
        # O antigo pós-cupom duplamente descontado (20% de 80) não pode aparecer.
        self.assertNotIn("64", texto)

    def test_oferta_com_preco_de_cupom_observado_anuncia_valor_pago(self):
        produto = Produto.objects.create(
            owner=None, marketplace="mercadolivre", nome="Câmera Wi-Fi 4K",
            origem="oferta", fonte="mercadolivre-web", estado="ativo",
            link_produto="https://ml.com.br/camera/p/MLB2",
            preco_sem_desconto=399.0, preco_com_cupom=113.74,
            preco_efetivo=98.77,
            evidencia={
                "promotion": {
                    "present": True, "coupon_confirmed": True,
                    "coupon_final_price": 98.77, "source": "pdp-live",
                },
            },
        )
        from apps.scrapers.models import PrecoHistorico
        from apps.scrapers.precos import chave_produto

        PrecoHistorico.objects.create(
            marketplace=produto.marketplace, chave=chave_produto(produto),
            preco=399.0,
        )

        texto = montar_mensagem(produto, "https://meli.la/camera", None)

        self.assertIn("POR 98,77", texto)
        self.assertNotIn("POR 113,74", texto)
        self.assertIn("CUPOM: ative no Mercado Livre — o preço já é com ele", texto)

    def test_preco_efetivo_ml_sem_prova_direta_nao_e_publicado(self):
        produto = Produto.objects.create(
            owner=None, marketplace="mercadolivre", nome="Câmera sem prova",
            origem="oferta", fonte="mercadolivre-web", estado="ativo",
            link_produto="https://ml.com.br/camera/p/MLB3",
            preco_sem_desconto=399.0, preco_com_cupom=113.74,
            preco_efetivo=98.77, evidencia={},
        )

        texto = montar_mensagem(produto, "https://meli.la/camera", None)

        self.assertIn("POR 113,74", texto)
        self.assertNotIn("CUPOM: ative no Mercado Livre — o preço já é com ele", texto)


class NormalizacaoDinheiroTests(TestCase):
    def test_money_aceita_ponto_decimal_e_ponto_de_milhar(self):
        casos = [
            ("R$ 64.99", 64.99),      # ponto como decimal — virava 6499.0
            ("R$ 1.299,90", 1299.90),  # ponto como milhar
            ("R$ 1 299,90", 1299.90),  # espaço como milhar
            ("R$ 1.299", 1299.0),      # milhar sem centavos
            ("R$ 89,90", 89.90),
            ("R$ 12.345.678,90", 12345678.90),
            ("", 0.0),
            ("sem preço", 0.0),
        ]
        for texto, esperado in casos:
            with self.subTest(texto=texto):
                self.assertEqual(_money(texto), esperado)

    def test_preco_br_usa_separador_de_milhar(self):
        casos = [
            (1299.9, "1.299,90"),
            (1299.0, "1.299"),
            (64.99, "64,99"),
            (89.5, "89,50"),
            (999.0, "999"),
            (12345678.9, "12.345.678,90"),
        ]
        for valor, esperado in casos:
            with self.subTest(valor=valor):
                self.assertEqual(_preco_br(valor), esperado)


class ParidadeMercadoLivreTests(TestCase):
    """No ML o pós-cupom só vale com evidência direta do card/PDP.

    A tela anotava `min(vitrine, efetivo)` para todo mundo, enquanto a mensagem
    exigia a evidência. Item com `preco_efetivo` velho aparecia na lista por
    R$ 93,60 e a mensagem publicava os R$ 117 da vitrine.
    """

    def _produto(self, indice, **campos):
        base = {
            "owner": None, "marketplace": "mercadolivre", "origem": "oferta",
            "estado": "ativo", "nome": "Chuveiro Loren Shower",
            "link_produto": f"https://www.mercadolivre.com.br/c/p/MLB{indice}",
            "preco_sem_desconto": 398.90, "preco_com_cupom": 117.0,
            "preco_efetivo": 93.60, "evidencia": {},
        }
        base.update(campos)
        return Produto.objects.create(**base)

    def _anotado(self, produto):
        return (Produto.objects.filter(pk=produto.pk)
                .annotate(preco_publicado=anotacao_preco_publicado())
                .first().preco_publicado)

    def test_efetivo_sem_evidencia_nao_vira_preco_de_tela(self):
        produto = self._produto(1)
        self.assertEqual(preco_publicavel(produto), 117.0)
        self.assertEqual(self._anotado(produto), 117.0)

    def test_cupom_so_do_card_nao_vira_preco(self):
        """Caso medido em 06/09/2026 (produto 80220, MLB63561701).

        O card de `/ofertas` anunciava "R$ 93,60 com cupom" e a PDP cobrava
        R$ 117. O card é indício; a página é quem cobra.
        """
        produto = self._produto(2, evidencia={"promotion": {
            "present": True, "coupon_confirmed": True,
            "coupon_final_price": 93.60, "source": "offer-card",
        }})
        self.assertEqual(preco_publicavel(produto), 117.0)
        self.assertEqual(self._anotado(produto), 117.0)

    def test_evidencia_de_outra_fonte_nao_libera_o_pos_cupom(self):
        produto = self._produto(3, evidencia={"promotion": {
            "present": True, "coupon_confirmed": True,
            "coupon_final_price": 93.60, "source": "campanha",
        }})
        self.assertEqual(preco_publicavel(produto), 117.0)
        self.assertEqual(self._anotado(produto), 117.0)

    def test_cupom_nao_confirmado_nao_libera_o_pos_cupom(self):
        produto = self._produto(4, evidencia={"promotion": {
            "present": True, "coupon_confirmed": False, "source": "offer-card",
        }})
        self.assertEqual(preco_publicavel(produto), 117.0)
        self.assertEqual(self._anotado(produto), 117.0)

    def test_badge_lido_na_pdp_vale_nas_duas_pontas(self):
        produto = self._produto(5, evidencia={"promotion": {
            "present": True, "coupon_confirmed": True,
            "coupon_final_price": 93.60, "source": "pdp-live",
        }})
        self.assertEqual(preco_publicavel(produto), 93.60)
        self.assertEqual(self._anotado(produto), 93.60)


class TelaOfertasMercadoLivreTests(TestCase):
    """A linha da vitrine tem de dizer o mesmo que a mensagem — e dizer de onde
    vem o número. Item com `preco_efetivo` de um cupom não comprovado aparecia
    por R$ 93,60 ao lado do rótulo "sem cupom", enquanto a página cobrava R$ 117.
    """

    def setUp(self):
        from django.urls import reverse

        self.user = get_user_model().objects.create_user("vitrine-ml", password="x")
        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        self.url = reverse("scraper-top")

    def _produto(self, indice, evidencia):
        from apps.scrapers.models import LinkAfiliadoUsuario

        produto = Produto.objects.create(
            owner=None, marketplace="mercadolivre", origem="oferta", estado="ativo",
            nome="Chuveiro Loren Shower", preco_sem_desconto=398.90,
            preco_com_cupom=117.0, preco_efetivo=93.60, evidencia=evidencia,
            link_produto=f"https://www.mercadolivre.com.br/c/p/MLB{indice}",
        )
        LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=produto, estado="pronto", afiliado_ok=True,
            link_afiliado=f"https://meli.la/{indice}", verificado_ok=True,
            url_canonica=f"https://meli.la/{indice}",
        )
        return produto

    def test_sem_evidencia_a_lista_mostra_a_vitrine(self):
        self._produto(11, {})

        resposta = self.client.get(self.url, {"loja": "mercadolivre"})

        listado = resposta.context["produtos"][0]
        self.assertEqual(listado.preco_publicado, 117.0)
        self.assertContains(resposta, "R$ 117,00")
        self.assertNotContains(resposta, "R$ 93,60")
        self.assertContains(resposta, "sem cupom")

    def test_cupom_so_do_card_nao_muda_a_lista(self):
        self._produto(13, {"promotion": {
            "present": True, "coupon_confirmed": True,
            "coupon_final_price": 93.60, "source": "offer-card",
        }})

        resposta = self.client.get(self.url, {"loja": "mercadolivre"})

        listado = resposta.context["produtos"][0]
        self.assertEqual(listado.preco_publicado, 117.0)
        self.assertContains(resposta, "R$ 117,00")
        self.assertContains(resposta, "sem cupom")

    def test_com_badge_da_pdp_a_lista_mostra_o_pos_cupom_e_diz_por_que(self):
        self._produto(12, {"promotion": {
            "present": True, "coupon_confirmed": True,
            "coupon_final_price": 93.60, "source": "pdp-live",
        }})

        resposta = self.client.get(self.url, {"loja": "mercadolivre"})

        listado = resposta.context["produtos"][0]
        self.assertEqual(listado.preco_publicado, 93.60)
        self.assertContains(resposta, "R$ 93,60")
        # O nome do teste cobra que a lista diga POR QUE mostra o pós-cupom.
        # "Cupom na página" não dizia: nem qual cupom, nem que página — e a
        # página não estava linkada em lugar nenhum da célula. O que a pessoa
        # precisa saber antes de mandar a oferta é que não há código para copiar
        # e que o desconto já está no número exibido.
        self.assertContains(resposta, "Cupom já no preço")
        self.assertContains(resposta, "sem código")
        self.assertNotContains(resposta, "sem cupom")

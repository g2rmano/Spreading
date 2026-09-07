"""O portão de "o anúncio ainda existe" — e por que ele estava cego.

`esta_vivo` decide se uma oferta pode ser publicada. Ele lia a PDP e concluía
"vivo" sempre que a resposta fosse 200 e não contivesse os termos de pausa. Desde
08/2026 o Mercado Livre responde 200 com um interstitial de verificação para o IP
da Fly: HTML curto, sem componente nenhum do ML, e obviamente sem a frase
"Anúncio pausado". O portão aprovava uma página que nunca foi vista.

Consertar só isso pararia o Mercado Livre inteiro — nenhuma PDP é legível daqui.
Por isso a prova de vida do ML vem primeiro da vitrine `/ofertas`, que é a porta
que responde, e a PDP virou segunda opinião.
"""
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from apps.scrapers.marketplaces.mercadolivre import MercadoLivre
from apps.scrapers.ofertas import esta_vivo

PDP_REAL = "<html><body class='andes-body ui-pdp-container'>" + ("x" * 400) + "</body></html>"
INTERSTICIAL = "<html><body>Verifique que você é uma pessoa</body></html>"


def _resposta(status=200, corpo=PDP_REAL, url="https://www.mercadolivre.com.br/p/MLB123"):
    return Mock(status_code=status, text=corpo, url=url)


class EstaVivoTests(SimpleTestCase):
    def _com(self, resposta):
        produto = Mock(id=1, link_produto="https://www.mercadolivre.com.br/p/MLB123")
        with patch("apps.scrapers.ofertas.requests.get", return_value=resposta):
            return esta_vivo(produto)

    def test_pdp_real_sem_termo_de_pausa_e_vivo(self):
        self.assertIs(self._com(_resposta()), True)

    def test_sumiu_de_verdade_e_morto(self):
        for status in (404, 410):
            with self.subTest(status=status):
                self.assertIs(self._com(_resposta(status=status)), False)

    def test_pausado_na_pdp_real_e_morto(self):
        corpo = PDP_REAL.replace("</body>", "Anúncio pausado</body>")
        self.assertIs(self._com(_resposta(corpo=corpo)), False)

    def test_interstitial_200_e_indeterminado_e_nao_vivo(self):
        # O caso que motivou tudo: 200, corpo curto, nenhum componente do ML e
        # nenhum termo de pausa. Antes: True. Publicava anúncio não verificado.
        self.assertIsNone(self._com(_resposta(corpo=INTERSTICIAL)))

    def test_verificacao_de_conta_e_indeterminada(self):
        self.assertIsNone(self._com(_resposta(
            url="https://www.mercadolivre.com.br/gz/account-verification?go=x",
        )))

    def test_bloqueio_explicito_e_indeterminado(self):
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.assertIsNone(self._com(_resposta(status=status, corpo=INTERSTICIAL)))

    def test_rede_caiu_e_indeterminado(self):
        produto = Mock(id=1, link_produto="https://www.mercadolivre.com.br/p/MLB123")
        with patch("apps.scrapers.ofertas.requests.get", side_effect=OSError("timeout")):
            self.assertIsNone(esta_vivo(produto))


class VivoPelaVitrineTests(TestCase):
    """Estar na varredura de `/ofertas` é prova positiva; não estar não prova nada."""

    def setUp(self):
        self.produto = Mock(
            id=7, marketplace="mercadolivre",
            link_produto="https://www.mercadolivre.com.br/p/MLB-3456789",
        )

    def test_item_na_vitrine_dispensa_a_pdp(self):
        with patch("apps.scrapers.preco_ao_vivo.varrer_ofertas_ml",
                   return_value={"MLB3456789": (93.60, 399.0)}), \
                patch("apps.scrapers.ofertas.requests.get") as pdp:
            self.assertIs(MercadoLivre().is_alive(self.produto), True)
        pdp.assert_not_called()

    def test_fora_da_vitrine_com_pdp_bloqueada_e_indeterminado(self):
        with patch("apps.scrapers.preco_ao_vivo.varrer_ofertas_ml", return_value={}), \
                patch("apps.scrapers.ofertas.requests.get",
                      return_value=_resposta(corpo=INTERSTICIAL)):
            self.assertIsNone(MercadoLivre().is_alive(self.produto))

    def test_varredura_quebrada_nao_vira_veredito(self):
        # A vitrine é oportunista. Se ela falha, quem decide continua sendo a PDP.
        with patch("apps.scrapers.preco_ao_vivo.varrer_ofertas_ml",
                   side_effect=RuntimeError("vitrine fora")), \
                patch("apps.scrapers.ofertas.requests.get", return_value=_resposta()):
            self.assertIs(MercadoLivre().is_alive(self.produto), True)

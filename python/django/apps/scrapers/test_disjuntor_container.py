"""O disjuntor do container do Mercado Livre.

Cada cupom já tinha backoff de 20 minutos — mas POR CUPOM. Com 400 cupons por
ciclo e o host respondendo 403 a todos, o backoff individual não impedia o lote
seguinte de repetir a rodada inteira: ~1.600 requisições recusadas por hora,
afogando o log e gastando a janela de preparo sem produzir um único vínculo.

Depois de uma sequência de falhas o passo HTTP para de tocar na rede por um
tempo. Uma resposta boa reabre na hora.
"""
from django.test import SimpleTestCase, override_settings

from apps.scrapers import coupon_products

_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "persistente": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "disjuntor-container-testes",
    },
}


@override_settings(CACHES=_CACHES)
class DisjuntorDoContainerTests(SimpleTestCase):
    def setUp(self):
        coupon_products._cache_circuito().clear()

    def test_comeca_fechado(self):
        self.assertFalse(coupon_products._circuito_aberto())

    def test_falhas_isoladas_nao_abrem(self):
        for _ in range(coupon_products.CONTAINER_FALHAS_ATE_ABRIR - 1):
            coupon_products._registrar_falha_de_transporte()
        self.assertFalse(coupon_products._circuito_aberto())

    def test_sequencia_no_teto_abre(self):
        for _ in range(coupon_products.CONTAINER_FALHAS_ATE_ABRIR):
            coupon_products._registrar_falha_de_transporte()
        self.assertTrue(coupon_products._circuito_aberto())

    def test_uma_resposta_boa_reabre_o_caminho(self):
        for _ in range(coupon_products.CONTAINER_FALHAS_ATE_ABRIR):
            coupon_products._registrar_falha_de_transporte()
        self.assertTrue(coupon_products._circuito_aberto())

        coupon_products._registrar_sucesso_de_transporte()

        self.assertFalse(coupon_products._circuito_aberto())

    def test_sucesso_tambem_zera_a_contagem(self):
        # Senão uma falha isolada meses depois herdaria a sequência antiga.
        for _ in range(coupon_products.CONTAINER_FALHAS_ATE_ABRIR - 1):
            coupon_products._registrar_falha_de_transporte()
        coupon_products._registrar_sucesso_de_transporte()
        coupon_products._registrar_falha_de_transporte()
        self.assertFalse(coupon_products._circuito_aberto())

    def test_sem_cache_o_comportamento_e_o_de_antes(self):
        # O disjuntor é proteção, não dependência: se o cache falhar, o passo
        # HTTP volta a tentar em vez de ficar preso fechado.
        from unittest.mock import patch
        with patch.object(coupon_products, "_cache_circuito",
                          side_effect=RuntimeError("cache fora")):
            self.assertFalse(coupon_products._circuito_aberto())
            coupon_products._registrar_falha_de_transporte()  # não levanta

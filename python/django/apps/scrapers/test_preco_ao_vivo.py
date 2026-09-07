"""Política de revalidação de preço antes do envio.

Aqui é onde um erro silencioso custa dinheiro: ou a mensagem anuncia preço
errado, ou o envio é bloqueado sem necessidade.
"""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.scrapers import preco_ao_vivo
from apps.scrapers.models import ConfiguracaoEnvio, PrecoHistorico, Produto


def _item_api(preco, preco_de):
    """Resposta da Creators API no formato que _mapear_item entende."""
    return [{
        "asin": "B012345678",
        "itemInfo": {"title": {"displayValue": "Cafeteira Expresso"}},
        "offersV2": {"listings": [{
            "price": {
                "money": {"amount": preco},
                "savingBasis": {"money": {"amount": preco_de}},
                "savings": {"percentage": round((preco_de - preco) / preco_de * 100)},
            },
        }]},
    }]


class RevalidacaoTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("preco-vivo")
        self.produto = Produto.objects.create(
            owner=self.user, marketplace="amazon", asin="B012345678",
            nome="Cafeteira Expresso", origem="oferta", estado="ativo",
            link_produto="https://www.amazon.com.br/dp/B012345678",
            preco_sem_desconto=200.0, preco_com_cupom=100.0, preco_efetivo=100.0,
            frase_llm="TÍTULO ANTIGO POR 100", nome_llm="Cafeteira",
        )

    def _revalidar(self, itens, configuracao=None):
        with patch("apps.scrapers.scraper_amazon.creators_api.get_items",
                   return_value=itens), \
             patch("apps.scrapers.scraper_amazon.creators_api.creds_de_usuario",
                   return_value=object()):
            return preco_ao_vivo.revalidar(
                self.produto, usuario=self.user, configuracao=configuracao)

    def test_variacao_dentro_da_tolerancia_nao_grava_nada(self):
        resultado = self._revalidar(_item_api(100.2, 200.0))

        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["mudou"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 100.0)
        # Texto da IA preservado: o preço não mudou.
        self.assertEqual(self.produto.frase_llm, "TÍTULO ANTIGO POR 100")

    def test_preco_que_caiu_atualiza_e_segue(self):
        resultado = self._revalidar(_item_api(80.0, 200.0))

        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["mudou"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 80.0)
        self.assertEqual(self.produto.preco_efetivo, 80.0)

    def test_preco_que_subiu_mantendo_desconto_segue(self):
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="123@g.us", min_desconto_percent=15.0)
        # 150 de 200 = 25% de desconto, acima do mínimo.
        resultado = self._revalidar(_item_api(150.0, 200.0), configuracao=config)

        self.assertTrue(resultado["ok"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 150.0)

    def test_preco_que_subiu_derrubando_o_desconto_aborta(self):
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="123@g.us", min_desconto_percent=15.0)
        # 195 de 200 = 2,5%, abaixo do mínimo -> não faz sentido publicar.
        resultado = self._revalidar(_item_api(195.0, 200.0), configuracao=config)

        self.assertFalse(resultado["ok"])
        self.assertIn("desconto caiu", resultado["motivo"])
        # Mesmo abortando, o preço fresco fica gravado para a próxima seleção.
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 195.0)

    def test_mudanca_de_preco_invalida_o_texto_da_ia(self):
        self._revalidar(_item_api(80.0, 200.0))
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.frase_llm, "")
        self.assertEqual(self.produto.nome_llm, "")

    def test_api_indisponivel_com_leitura_recente_nao_bloqueia(self):
        # A regra que já valia: uma janela de indisponibilidade não pode parar os
        # envios. Continua valendo — enquanto a última leitura for recente.
        with patch("apps.scrapers.scraper_amazon.creators_api.get_items",
                   side_effect=RuntimeError("503")), \
             patch("apps.scrapers.scraper_amazon.creators_api.creds_de_usuario",
                   return_value=object()):
            resultado = preco_ao_vivo.revalidar(self.produto, usuario=self.user)

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "ultima_leitura_recente")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 100.0)

    def test_api_indisponivel_com_leitura_vencida_bloqueia(self):
        # A metade que faltava. Sem medir agora e sem leitura recente, publicar é
        # afirmar um preço que ninguém conferiu — foi assim que saiu a air fryer a
        # R$ 199,90 cobrando R$ 249,50, com 1021 minutos de observação.
        Produto.objects.filter(pk=self.produto.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=20))
        self.produto.refresh_from_db()

        with patch("apps.scrapers.scraper_amazon.creators_api.get_items",
                   side_effect=RuntimeError("503")), \
             patch("apps.scrapers.scraper_amazon.creators_api.creds_de_usuario",
                   return_value=object()):
            resultado = preco_ao_vivo.revalidar(self.produto, usuario=self.user)

        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fonte"], "sem_medicao")
        self.assertIn("não confirmado", resultado["motivo"])

    def test_mudanca_registra_historico_de_preco(self):
        self._revalidar(_item_api(80.0, 200.0))
        self.assertTrue(
            PrecoHistorico.objects.filter(marketplace="amazon", preco=80.0).exists())

    def test_marketplace_sem_fonte_ao_vivo_segue_com_o_banco(self):
        self.produto.marketplace = "awin"
        self.produto.save(update_fields=["marketplace"])
        resultado = preco_ao_vivo.revalidar(self.produto, usuario=self.user)
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "nao_suportado")

    def test_cupom_de_ativacao_da_amazon_nao_e_revalidado(self):
        """O pior desfecho possível: achatar o pós-cupom contra a vitrine.

        A Creators API devolve a VITRINE. Se revalidássemos um item cujo preço
        anunciado é o `preco_efetivo` (cupom já garantido na página oficial), a
        mensagem passaria a prometer o valor maior logo abaixo de "ative o cupom".
        """
        self.produto.preco_efetivo = 70.0  # < preco_com_cupom (100)
        self.produto.save(update_fields=["preco_efetivo"])

        resultado = self._revalidar(_item_api(100.0, 200.0))

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "cupom_ativacao_nao_revalidavel")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_efetivo, 70.0)
        self.assertLess(self.produto.preco_efetivo, self.produto.preco_com_cupom)


def _relatorio(preco=0.0, preco_de=0.0, bloqueio="", morto=False,
               preco_cupom=0.0, cupom_detectado=False):
    return {"preco": preco, "preco_de": preco_de, "url_final": "",
            "preco_cupom": preco_cupom, "cupom_detectado": cupom_detectado,
            "bloqueio": bloqueio, "morto": morto}


class RevalidacaoMercadoLivreTests(TestCase):
    """O ML é o pool principal e não era revalidado — a causa do preço errado.

    Só o GET autenticado passa pelo anti-bot (ver link_http), então tudo aqui
    mocka `sessao_ml` + `relatorio_de_preco`.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("preco-ml")
        self.produto = Produto.objects.create(
            owner=None, marketplace="mercadolivre", nome="Smart TV 50",
            origem="oferta", estado="ativo",
            link_produto="https://www.mercadolivre.com.br/tv/p/MLB123",
            preco_sem_desconto=2499.0, preco_com_cupom=1799.0, preco_efetivo=1799.0,
            frase_llm="TÍTULO ANTIGO", nome_llm="Smart TV",
        )

    def _revalidar(self, relatorio, *, configuracao=None, url="", sessao=object()):
        with patch.object(preco_ao_vivo, "sessao_ml", return_value=sessao), \
             patch("apps.scrapers.scraper_mercadolivre.link_http.relatorio_de_preco",
                   return_value=relatorio) as chamada:
            resultado = preco_ao_vivo.revalidar(
                self.produto, usuario=self.user, configuracao=configuracao, url=url)
        return resultado, chamada

    def test_preco_igual_nao_grava_nada(self):
        resultado, _ = self._revalidar(_relatorio(1799.0, 2499.0))
        self.assertTrue(resultado["ok"])
        self.assertFalse(resultado["mudou"])
        self.assertEqual(resultado["fonte"], "ml-http-sessao")

    def test_preco_que_caiu_atualiza_e_segue(self):
        resultado, _ = self._revalidar(_relatorio(1499.0, 2499.0))
        self.assertTrue(resultado["ok"])
        self.assertTrue(resultado["mudou"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 1499.0)
        self.assertEqual(self.produto.preco_efetivo, 1499.0)

    def test_preco_com_cupom_da_pdp_vira_preco_efetivo(self):
        self.produto.preco_sem_desconto = 399.0
        self.produto.preco_com_cupom = 113.74
        self.produto.preco_efetivo = 113.74
        self.produto.save(update_fields=[
            "preco_sem_desconto", "preco_com_cupom", "preco_efetivo",
        ])

        resultado, _ = self._revalidar(_relatorio(
            113.74, 399.0, preco_cupom=98.77, cupom_detectado=True,
        ))

        self.assertTrue(resultado["ok"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 113.74)
        self.assertEqual(self.produto.preco_efetivo, 98.77)
        self.assertEqual(
            self.produto.evidencia["promotion"]["coupon_final_price"], 98.77,
        )
        self.assertEqual(
            self.produto.evidencia["promotion"]["source"], "pdp-live",
        )

    def test_pdp_sem_badge_remove_cupom_obsoleto(self):
        self.produto.preco_com_cupom = 113.74
        self.produto.preco_efetivo = 98.77
        self.produto.evidencia = {
            "promotion": {
                "present": True, "coupon_confirmed": True,
                "coupon_final_price": 98.77, "source": "offer-card",
            },
        }
        self.produto.save(update_fields=[
            "preco_com_cupom", "preco_efetivo", "evidencia",
        ])

        resultado, _ = self._revalidar(_relatorio(113.74, 399.0))

        self.assertTrue(resultado["ok"])
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_efetivo, 113.74)
        self.assertNotIn("promotion", self.produto.evidencia)

    def test_preco_que_subiu_derrubando_o_desconto_aborta(self):
        config = ConfiguracaoEnvio.objects.create(
            owner=self.user, grupo_id="123@g.us", min_desconto_percent=15.0)
        # 2450 de 2499 = 2%, abaixo do mínimo.
        resultado, _ = self._revalidar(
            _relatorio(2450.0, 2499.0), configuracao=config)
        self.assertFalse(resultado["ok"])
        self.assertIn("desconto caiu", resultado["motivo"])

    def test_confere_a_url_publicada_e_nao_o_link_do_produto(self):
        """Confere o publicado primeiro; a PDP completa o dado de cupom ausente."""
        _resultado, chamada = self._revalidar(
            _relatorio(1799.0, 2499.0), url="https://meli.la/abc")
        self.assertEqual(chamada.call_args_list[0].args[0], "https://meli.la/abc")
        self.assertEqual(
            chamada.call_args_list[1].args[0], self.produto.link_produto,
        )

    def test_sem_url_cai_no_link_do_produto(self):
        _resultado, chamada = self._revalidar(_relatorio(1799.0, 2499.0))
        self.assertEqual(chamada.call_args.args[0], self.produto.link_produto)

    def test_challenge_do_anti_bot_com_leitura_recente_nao_bloqueia(self):
        """A regra central: uma janela de bloqueio não pode parar os envios."""
        resultado, _ = self._revalidar(
            _relatorio(bloqueio="o Mercado Livre exigiu verificação"))
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "ultima_leitura_recente")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 1799.0)

    def test_challenge_com_leitura_vencida_tenta_a_vitrine_e_bloqueia(self):
        """Leitura velha + PDP bloqueada: resta a vitrine; sem ela, não publica.

        É o cenário real deste IP desde 08/2026. O bloqueio não pode parar os
        envios, mas também não pode virar licença para afirmar um preço que
        ninguém leu.
        """
        Produto.objects.filter(pk=self.produto.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=20))
        self.produto.refresh_from_db()

        # O `link_produto` deste fixture não tem um item id de verdade (MLB + 6
        # dígitos); sem ele a varredura nem é consultada. Fixar o id aqui mantém o
        # teste sobre a política, não sobre o formato da URL do fixture.
        with patch("apps.scrapers.preco_ao_vivo.varrer_ofertas_ml",
                   return_value={}) as vitrine, \
                patch("apps.scrapers.scraper_mercadolivre.link._extrair_item_id",
                      return_value="MLB123456"):
            resultado, _ = self._revalidar(
                _relatorio(bloqueio="o Mercado Livre exigiu verificação"))

        vitrine.assert_called()
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["fonte"], "sem_medicao")

    def test_leitura_vencida_com_item_na_vitrine_publica_o_preco_lido(self):
        Produto.objects.filter(pk=self.produto.pk).update(
            ultima_observacao=timezone.now() - timedelta(hours=20))
        self.produto.refresh_from_db()

        with patch("apps.scrapers.preco_ao_vivo.varrer_ofertas_ml",
                   return_value={"MLB123456": (1499.0, 2499.0)}), \
                patch("apps.scrapers.scraper_mercadolivre.link._extrair_item_id",
                      return_value="MLB123456"):
            resultado, _ = self._revalidar(
                _relatorio(bloqueio="o Mercado Livre exigiu verificação"))

        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "ml-ofertas-jit")

    def test_sem_sessao_do_ml_e_inconclusivo(self):
        resultado, _ = self._revalidar(_relatorio(1499.0), sessao=None)
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "ultima_leitura_recente")
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 1799.0)

    def test_historico_vai_com_o_marketplace_certo(self):
        """Gravar como "amazon" quebraria o selo de mínima de 30 dias do ML."""
        self._revalidar(_relatorio(1499.0, 2499.0))
        self.assertTrue(PrecoHistorico.objects.filter(
            marketplace="mercadolivre", preco=1499.0).exists())
        self.assertFalse(
            PrecoHistorico.objects.filter(marketplace="amazon").exists())

    def test_rls_negando_a_escrita_ainda_corrige_a_mensagem(self):
        """Sob o RLS do usuário o pool compartilhado não é gravável.

        O que a mensagem lê é o objeto em memória — o save é best-effort.
        """
        with patch.object(Produto, "save", side_effect=Exception("RLS: denied")):
            resultado, _ = self._revalidar(_relatorio(1499.0, 2499.0))

        self.assertTrue(resultado["ok"])
        self.assertEqual(self.produto.preco_com_cupom, 1499.0)  # em memória
        self.produto.refresh_from_db()
        self.assertEqual(self.produto.preco_com_cupom, 1799.0)  # banco intacto

    def test_flag_desligada_nao_toca_a_rede(self):
        with self.settings(PRECO_REVALIDA_ML=False), \
             patch.object(preco_ao_vivo, "sessao_ml") as sessao:
            resultado = preco_ao_vivo.revalidar(self.produto, usuario=self.user)
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["fonte"], "desligado")
        sessao.assert_not_called()

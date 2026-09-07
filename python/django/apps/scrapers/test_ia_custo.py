"""Contador de gasto de IA: sem ele, o teto de custo é palpite.

O teto do produto é R$350/mês incluindo Fly e IA, e o repositório não somava um
único token — a única resposta para "quanto a IA gastou" era abrir o console da
Anthropic. Contabilidade também não pode derrubar o funil: qualquer falha aqui é
log, nunca exceção que suba.
"""
from types import SimpleNamespace

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scrapers import ia_custo
from apps.scrapers.models import GastoIA


def _resposta(entrada=1000, saida=500, modelo="claude-haiku-4-5-20251001",
              cache_leitura=0):
    return SimpleNamespace(
        model=modelo,
        usage=SimpleNamespace(
            input_tokens=entrada, output_tokens=saida,
            cache_read_input_tokens=cache_leitura,
            cache_creation_input_tokens=0,
        ),
    )


class RegistroDeUsoTests(TestCase):
    def test_primeira_chamada_cria_a_linha_do_mes(self):
        ia_custo.registrar_uso(_resposta(), origem="cupom_extractor")
        linha = GastoIA.objects.get()
        self.assertEqual(linha.chamadas, 1)
        self.assertEqual(linha.tokens_entrada, 1000)
        self.assertEqual(linha.tokens_saida, 500)
        self.assertEqual(linha.competencia, timezone.localdate().replace(day=1))

    def test_chamadas_seguintes_acumulam_na_mesma_linha(self):
        for _ in range(3):
            ia_custo.registrar_uso(_resposta(), origem="cupom_extractor")
        linha = GastoIA.objects.get()
        self.assertEqual(linha.chamadas, 3)
        self.assertEqual(linha.tokens_entrada, 3000)

    def test_origens_diferentes_ficam_separadas(self):
        ia_custo.registrar_uso(_resposta(), origem="cupom_extractor")
        ia_custo.registrar_uso(_resposta(), origem="gerar_conteudo")
        self.assertEqual(GastoIA.objects.count(), 2)

    def test_cache_lido_conta_como_entrada(self):
        ia_custo.registrar_uso(_resposta(entrada=100, cache_leitura=900),
                               origem="cupom_extractor")
        self.assertEqual(GastoIA.objects.get().tokens_entrada, 1000)

    def test_resposta_sem_usage_nao_grava_nem_levanta(self):
        ia_custo.registrar_uso(SimpleNamespace(model="x"), origem="cupom_extractor")
        self.assertEqual(GastoIA.objects.count(), 0)

    def test_falha_de_contabilidade_nunca_sobe(self):
        # Contar gasto não pode derrubar um envio.
        ia_custo.registrar_uso(object(), origem="cupom_extractor")
        self.assertEqual(GastoIA.objects.count(), 0)


@override_settings(LLM_CAMBIO_BRL=5.0, LLM_TETO_BRL_MES=10.0)
class ResumoDoMesTests(TestCase):
    def test_converte_token_em_dinheiro_pela_tabela(self):
        # Haiku: US$1 por milhão de entrada, US$5 por milhão de saída.
        # 1M entrada + 1M saída = US$6 = R$30 a 5,00.
        ia_custo.registrar_uso(
            _resposta(entrada=1_000_000, saida=1_000_000), origem="cupom_extractor")
        resumo = ia_custo.resumo_do_mes()
        self.assertAlmostEqual(resumo["custo_brl"], 30.0, places=2)

    def test_avisa_quando_cruza_o_teto(self):
        ia_custo.registrar_uso(
            _resposta(entrada=1_000_000, saida=1_000_000), origem="cupom_extractor")
        self.assertTrue(ia_custo.resumo_do_mes()["estourou"])

    def test_abaixo_do_teto_nao_avisa(self):
        ia_custo.registrar_uso(_resposta(entrada=1000, saida=100),
                               origem="cupom_extractor")
        resumo = ia_custo.resumo_do_mes()
        self.assertFalse(resumo["estourou"])
        self.assertEqual(resumo["por_origem"][0]["origem"], "cupom_extractor")

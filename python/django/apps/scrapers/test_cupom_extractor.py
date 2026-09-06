"""A leitura é do modelo; a decisão de aceitar é nossa.

O extrator existe porque a expressão regular falhou de forma medida: 14 canais
varridos, zero cupons — não por falta de cupom, mas porque cada canal escreve de um
jeito. Uma mensagem real do `@cupombr` carrega sete cupons de Mercado Livre com
desconto, mínimo, teto e escopo, e nenhum casava com o padrão que o regex esperava.

Estes testes cobrem exatamente a parte que NÃO é o modelo: `_limpar`, que decide o
que entra. Ela roda sem rede e sem chave de API de propósito — a regra de negócio
não pode depender de uma chamada externa para ser verificável.
"""
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings

from apps.scrapers.cupom_extractor import (
    _limpar, codigo_plausivel, extrair, extrair_deterministico,
    parece_ter_cupom,
)
from apps.scrapers.coupon_rules import codigo_humano

# Transcrição fiel de uma mensagem real do @cupombr, medida em 19/08/2026.
MENSAGEM_REAL = """LISTÃO de Cupom Mercado Livre

10% OFF, Limite de R$ 20 OFF em todo site: TODOOSITE1308
15%OFF Limite de R$ 189: TVS1208CELULAR
R$50 OFF em R$399: CASA1508
25% OFF Acessórios para veículos: OMELHOR
✅ Ative aqui: https://mercadolivre.com.br/sec/31qRqvp"""

SAIDA_MODELO = {"cupons": [
    {"codigo": "TODOOSITE1308", "loja": "mercadolivre", "tipo": "porcentagem",
     "valor": 10, "minimo": 0, "teto": 20, "escopo": "todo site"},
    {"codigo": "CASA1508", "loja": "mercadolivre", "tipo": "fixo",
     "valor": 50, "minimo": 399, "teto": 0, "escopo": ""},
]}

MENSAGEM_AMBIGUA = (
    "Cupom Amazon CASA1508 com desconto especial para compras selecionadas"
)


class SinalDeCupomTests(TestCase):
    def test_mensagem_com_cupom_e_marcada(self):
        self.assertTrue(parece_ter_cupom(MENSAGEM_REAL))
        self.assertTrue(parece_ter_cupom("🎟️ AMIG4ASPROM0 30%"))
        self.assertTrue(parece_ter_cupom("20% OFF hoje"))

    def test_mensagem_sem_sinal_nao_vai_para_o_modelo(self):
        """Filtro de custo: mensagem sem cara de cupom não é lida."""
        self.assertFalse(parece_ter_cupom("Bom dia, pessoal!"))
        self.assertFalse(parece_ter_cupom("Monitor LG por R$ 569 à vista"))


class RegraDeAceitacaoTests(TestCase):
    """O que o modelo devolve passa por aqui antes de virar cupom."""

    def test_aceita_os_cupons_da_mensagem_real(self):
        aceitos = _limpar(SAIDA_MODELO)
        self.assertEqual([c["codigo"] for c in aceitos],
                         ["TODOOSITE1308", "CASA1508"])
        primeiro = aceitos[0]
        self.assertEqual(primeiro["tipo"], "porcentagem")
        self.assertEqual(primeiro["valor"], 10.0)
        self.assertEqual(primeiro["teto"], 20.0)
        self.assertEqual(primeiro["escopo"], "todo site")
        self.assertEqual(aceitos[1]["minimo"], 399.0)

    def test_codigo_com_espaco_e_recusado(self):
        """"MERCADO LIVRE" não é código; ninguém digita isso no checkout."""
        aceitos = _limpar({"cupons": [
            {"codigo": "MERCADO LIVRE", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 10},
        ]})
        self.assertEqual(aceitos, [])

    def test_palavra_operacional_nao_vira_codigo_via_modelo(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "ESTOQUE", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 10},
        ]})
        self.assertEqual(aceitos, [])

    def test_palavras_de_interface_e_texto_colado_nao_viram_codigo(self):
        for codigo in (
            "RESGATE", "RESGATAR", "EXCLUSIVO", "TECNOLOGIA",
            "ATUALIZADO", "MELICUPONS", "ATIVADO", "ESGOTANDO",
            "ESGOTANDOOO", "MOSTRAR", "UTILIZADO", "RESGATARAM", "VOLTANDO",
            "CORREEEEE", "CORREEEEEE", "ANUNCIO", "MAISCUPONS",
            "9.9CONSEGUEM",
        ):
            with self.subTest(codigo=codigo):
                self.assertFalse(codigo_plausivel(codigo))
                self.assertEqual(codigo_humano(codigo), "")
                self.assertEqual(_limpar({"cupons": [{
                    "codigo": codigo, "loja": "shopee",
                    "tipo": "fixo", "valor": 20,
                }]}), [])

    def test_fallback_nao_inventa_codigo_com_rotulo_de_categoria(self):
        texto = (
            "CUPOM SHOPEE TECNOLOGIA\n"
            "R$100 OFF em compras acima de R$999\n"
            "Resgate aqui: https://s.shopee.com.br/abc"
        )
        self.assertEqual(extrair_deterministico(texto), [])

    def test_placeholder_de_agregador_nao_e_codigo_publicavel(self):
        for codigo in ("RESGATENOLINK", "PEGUEAQUI", "CUPOMNOLINK"):
            with self.subTest(codigo=codigo):
                self.assertEqual(codigo_humano(codigo), "")

    def test_cem_por_cento_e_recusado(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "TUDO100", "loja": "mercadolivre",
             "tipo": "porcentagem", "valor": 100},
        ]})
        self.assertEqual(aceitos, [])

    def test_loja_que_nao_sabemos_afiliar_e_recusada(self):
        """Cupom de loja não afiliável é trabalho para o usuário e comissão de outro."""
        aceitos = _limpar({"cupons": [
            {"codigo": "MAGALU10", "loja": "magazineluiza",
             "tipo": "porcentagem", "valor": 10},
        ]})
        self.assertEqual(aceitos, [])

    def test_valor_zero_e_recusado(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "SEMVALOR", "loja": "amazon",
             "tipo": "porcentagem", "valor": 0},
        ]})
        self.assertEqual(aceitos, [])

    def test_loja_ausente_cai_no_padrao_do_link(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "SEMLOJA10", "loja": "", "tipo": "porcentagem", "valor": 10},
        ]}, loja_padrao="shopee")
        self.assertEqual(aceitos[0]["loja"], "shopee")

    def test_codigo_repetido_entra_uma_vez(self):
        aceitos = _limpar({"cupons": [
            {"codigo": "IGUAL10", "loja": "amazon", "tipo": "porcentagem", "valor": 10},
            {"codigo": "IGUAL10", "loja": "amazon", "tipo": "porcentagem", "valor": 15},
        ]})
        self.assertEqual(len(aceitos), 1)

    def test_resposta_ilegivel_do_modelo_nao_quebra(self):
        self.assertEqual(_limpar(None), [])
        self.assertEqual(_limpar({"cupons": "nao e lista"}), [])
        self.assertEqual(_limpar({"cupons": ["texto solto"]}), [])

    def test_fallback_local_le_lista_real_sem_chave(self):
        achados = extrair_deterministico(MENSAGEM_REAL)
        self.assertEqual(
            [c["codigo"] for c in achados],
            ["TODOOSITE1308", "TVS1208CELULAR", "CASA1508", "OMELHOR"],
        )
        self.assertEqual(achados[0]["teto"], 20.0)
        self.assertEqual(achados[2]["minimo"], 399.0)

    def test_fallback_nao_confunde_nome_de_produto_com_codigo(self):
        texto = (
            "🔥 Smartphone Motorola Moto G17 4G 128GB (Cupom 10% OFF)\n"
            "💰 R$ 728,19\nhttps://meli.la/abc"
        )
        self.assertEqual(extrair_deterministico(texto), [])

    def test_fallback_associa_codigo_na_linha_seguinte(self):
        texto = (
            "Cupom Shopee\nR$15 OFF nas compras acima de R$79\n"
            "Usem o Cupom: F1MD03SQU3NT4\nhttps://s.shopee.com.br/abc"
        )
        achados = extrair_deterministico(texto)
        self.assertEqual([c["codigo"] for c in achados], ["F1MD03SQU3NT4"])
        self.assertEqual(achados[0]["minimo"], 79.0)

    def test_fallback_nao_confunde_path_de_link_curto_com_codigo(self):
        texto = (
            "Cupom Shopee 50% OFF limite R$10: SP08T3AF "
            "Resgate: https://s.shopee.com.br/1qU7Zs67MB "
            "Carrinho: https://s.shopee.com.br/7plKiu5H62"
        )
        achados = extrair_deterministico(texto)
        self.assertEqual([item["codigo"] for item in achados], ["SP08T3AF"])

    def test_fallback_le_codigo_isolado_antes_das_regras(self):
        texto = (
            "Cupom Shopee\n🎟 99T3MTUD0N4SH0\n"
            "Regras: R$20 OFF para compras acima de R$60 na Shopee.\n"
            "https://s.shopee.com.br/9f2udPbK4n"
        )
        achados = extrair_deterministico(texto)
        self.assertEqual([item["codigo"] for item in achados], ["99T3MTUD0N4SH0"])
        self.assertEqual(achados[0]["minimo"], 60.0)

    def test_fallback_le_codigo_antes_do_desconto_na_mesma_linha(self):
        texto = (
            "Hora do cupom Shopee\n"
            "🎟️ 4F1L14D010: R$10 OFF em compras acima de R$59"
        )
        achados = extrair_deterministico(texto)

        self.assertEqual([item["codigo"] for item in achados], ["4F1L14D010"])
        self.assertEqual(achados[0]["valor"], 10.0)
        self.assertEqual(achados[0]["minimo"], 59.0)

    def test_fallback_nao_inventa_codigo_de_texto_operacional(self):
        casos = (
            "Cupom Mercado Livre de 20% OFF usando o cupom ESCRITO que será liberado",
            "Motorola por R$ 1.662 no Pix. Aplique R$100 OFF",
            "Regras: R$20 OFF para compras acima de R$60 na Shopee",
        )
        for texto in casos:
            with self.subTest(texto=texto):
                self.assertEqual(extrair_deterministico(texto), [])

    def test_fallback_nao_usa_cta_depois_de_link_como_codigo(self):
        casos = (
            (
                "Cupom Mercado Livre 30% OFF max R$1.000: PROMOMELI30 "
                "Lista: https://meli.la/abc Clique em Mostrar mais",
                ["PROMOMELI30"],
            ),
            (
                "Shopee R$20 OFF acima de R$79 Codigo: OFERTA20AF "
                "Carrinho: https://s.shopee.com.br/abc Acaba rapido! CORREEEEE",
                ["OFERTA20AF"],
            ),
            (
                "Shopee R$20 OFF acima de R$60 Codigo: F3L1Z3SS876 "
                "Carrinho: https://s.shopee.com.br/abc 75% UTILIZADO",
                ["F3L1Z3SS876"],
            ),
        )
        for texto, esperados in casos:
            with self.subTest(texto=texto):
                self.assertEqual(
                    [row["codigo"] for row in extrair_deterministico(texto)],
                    esperados,
                )

    def test_canal_de_loja_unica_nao_reclassifica_outra_loja(self):
        texto = (
            "Novo evento AliExpress Choice Day\n"
            "R$12 OFF em R$90: BRFS1\nR$25 OFF em R$180: BRFS2"
        )
        self.assertEqual(
            extrair_deterministico(texto, loja_padrao="mercadolivre"), [],
        )

    @override_settings(ANTHROPIC_API_KEY="chave-de-teste", CUPOM_LLM_ATIVO=True)
    def test_loja_nao_aceita_nem_chega_ao_modelo(self):
        texto = "AliExpress R$12 OFF em R$90: BRFS1"
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(extrair(texto, loja_padrao="mercadolivre"), [])
        cliente.assert_not_called()

    def test_dominio_transcrito_pelo_modelo_nao_e_codigo(self):
        self.assertEqual(_limpar({"cupons": [
            {"codigo": "S.SHOPEE.COM.BR", "loja": "shopee",
             "tipo": "porcentagem", "valor": 50},
        ]}), [])


@override_settings(ANTHROPIC_API_KEY="chave-de-teste", CUPOM_LLM_ATIVO=True)
class ExtracaoTests(TestCase):
    def setUp(self):
        cache.clear()

    def _resposta(self, payload):
        def _fake(*args, **kwargs):
            return payload
        return _fake

    def test_le_a_mensagem_e_devolve_os_cupons(self):
        with patch("apps.scrapers.llm._cliente") as cliente, \
                patch("apps.scrapers.llm._texto_resposta", return_value="{}"), \
                patch("apps.scrapers.llm._json_resposta", return_value=SAIDA_MODELO):
            achados = extrair(MENSAGEM_REAL)
        self.assertEqual(len(achados), 4)
        cliente.assert_not_called()

    def test_a_mesma_mensagem_e_lida_uma_vez_so(self):
        """Cache por hash: a mensagem do canal é imutável, pagar duas vezes é desperdício."""
        with patch("apps.scrapers.llm._cliente") as cliente, \
                patch("apps.scrapers.llm._texto_resposta", return_value="{}"), \
                patch("apps.scrapers.llm._json_resposta", return_value=SAIDA_MODELO):
            extrair(MENSAGEM_AMBIGUA)
            extrair(MENSAGEM_AMBIGUA)
        self.assertEqual(cliente.call_count, 1)

    def test_mensagem_sem_cupom_nem_chega_ao_modelo(self):
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(extrair("Bom dia, pessoal!"), [])
        cliente.assert_not_called()

    def test_banner_sem_codigo_nao_gasta_ia_para_confirmar_vazio(self):
        texto = "Cupom Amazon com 20% OFF para produtos selecionados"
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(extrair(texto), [])
        cliente.assert_not_called()

    def test_falha_do_modelo_devolve_vazio_e_nao_cacheia(self):
        """Erro não derruba a coleta nem congela a mensagem como vazia."""
        with patch("apps.scrapers.llm._cliente", side_effect=RuntimeError("api fora")):
            self.assertEqual(extrair(MENSAGEM_AMBIGUA), [])
        # Simula o TTL do circuito encerrado; a mensagem em si não foi cacheada.
        cache.delete("cupom-llm-circuit:anthropic")
        with patch("apps.scrapers.llm._cliente"), \
                patch("apps.scrapers.llm._texto_resposta", return_value="{}"), \
                patch("apps.scrapers.llm._json_resposta", return_value=SAIDA_MODELO):
            self.assertEqual(len(extrair(MENSAGEM_AMBIGUA)), 2)

    def test_erro_de_credito_abre_circuito_e_evita_tempestade_de_chamadas(self):
        outra = MENSAGEM_AMBIGUA.replace("CASA1508", "CASA1509")
        with patch(
            "apps.scrapers.llm._cliente",
            side_effect=RuntimeError("Your credit balance is too low; billing"),
        ) as cliente:
            self.assertEqual(extrair(MENSAGEM_AMBIGUA), [])
            self.assertEqual(extrair(outra), [])

        self.assertEqual(cliente.call_count, 1)
        self.assertEqual(cache.get("cupom-llm-circuit:anthropic"), "credential_or_credit")

    def test_resposta_truncada_salva_os_cupons_inteiros(self):
        """Erro real de produção: `Unterminated string` no meio da lista.

        A mensagem que estourou o orçamento foi justamente a mais valiosa — o
        "LISTÃO" com sete cupons. Perder a mensagem inteira por causa do último
        objeto cortado é o pior resultado possível.
        """
        truncada = (
            '{"cupons":[{"codigo":"TODOOSITE1308","loja":"mercadolivre",'
            '"tipo":"porcentagem","valor":10,"minimo":0,"teto":20,"escopo":"todo site"},'
            '{"codigo":"CASA1508","loja":"mercadolivre","tipo":"fixo","valor":50,'
            '"minimo":399,"teto":0,"escopo":""},'
            '{"codigo":"CORTADO","loja":"mercadoliv'
        )
        # NÃO mockar `_json_resposta`. Ele termina em `json.loads` e nunca devolve
        # None; um mock com `return_value=None` fazia este teste passar sobre um
        # caminho que produção não tem, e escondeu por completo o fato de o resgate
        # estar inalcançável atrás de `if dados is None`. Aqui o parser real recebe
        # o texto truncado e levanta, que é o que acontece de verdade.
        with patch("apps.scrapers.llm._cliente"), \
                patch("apps.scrapers.llm._texto_resposta", return_value=truncada):
            achados = extrair(MENSAGEM_AMBIGUA)
        self.assertEqual([c["codigo"] for c in achados],
                         ["TODOOSITE1308", "CASA1508"])

    @override_settings(ANTHROPIC_API_KEY="")
    def test_sem_chave_nao_chama_nada(self):
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(len(extrair(MENSAGEM_REAL)), 4)
        cliente.assert_not_called()

    @override_settings(CUPOM_LLM_ATIVO=False)
    def test_desligado_por_flag(self):
        with patch("apps.scrapers.llm._cliente") as cliente:
            self.assertEqual(len(extrair(MENSAGEM_REAL)), 4)
        cliente.assert_not_called()

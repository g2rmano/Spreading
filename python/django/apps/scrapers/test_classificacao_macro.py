"""Classificação por nicho — os títulos que a produção mostrou escapando.

Sem macro-categoria um produto não chega a nenhum grupo de nicho: ele some do
funil sem gerar erro nenhum. Em 07/09/2026 eram 692 de 2.527 produtos frescos —
mais de um quarto do catálogo — e a maior causa era boba: a lista de palavras
mistura termos com e sem acento e a comparação era literal, então "Garrafa
Térmica Stanley", que é exatamente como o Mercado Livre escreve, não casava com
"garrafa termica".

Os títulos abaixo são reais, colhidos da produção.
"""
from django.test import SimpleTestCase

from apps.scrapers.scraper_mercadolivre.ofertas_scraper import (
    classificar_oferta_por_nome,
)


class AcentoNaoPodeEsconderProdutoTests(SimpleTestCase):
    def test_termo_da_lista_sem_acento_casa_com_titulo_acentuado(self):
        self.assertEqual(
            classificar_oferta_por_nome(
                "Garrafa Térmica Stanley Aerolight Flip Straw Rose Quartz 473ml"),
            "Cozinha, Mesa e Bar",
        )

    def test_titulo_sem_acento_tambem_casa(self):
        self.assertEqual(
            classificar_oferta_por_nome("Garrafa Termica Stanley 473ml"),
            "Cozinha, Mesa e Bar",
        )


class FamiliasQueFaltavamTests(SimpleTestCase):
    """Perfumaria sozinha era boa parte dos 692."""

    def test_perfumaria(self):
        for titulo in (
            "Lattafa Yara Moi Eau de Parfum 100ml",
            "PHEBO - Deo Colônia Limão Siciliano 200ml",
            "Gabriela Sabatini Eau de Toilette 60Ml",
            "4711 Eau de Cologne 200Ml",
            "Body Splash Blue, GIOVANNA BABY",
            "NATURA UNA BLUSH DEO PARFUM 75ml",
        ):
            with self.subTest(titulo=titulo):
                self.assertEqual(
                    classificar_oferta_por_nome(titulo),
                    "Beleza e Cuidados Pessoais",
                )

    def test_eletrodomesticos_que_faltavam(self):
        for titulo, macro in (
            ("Lava Louças 8 Serviços Branco Touch Plus Midea", "Eletrodomésticos"),
            ("Cooktop Brastemp a Gás 5 Bocas com Grades de Ferro", "Eletrodomésticos"),
        ):
            with self.subTest(titulo=titulo):
                self.assertEqual(classificar_oferta_por_nome(titulo), macro)

    def test_casa_e_ferramentas_que_faltavam(self):
        casos = (
            ("Kit Colcha Cobre Leito Queen Matelado Liso 3 Peças",
             "Casa, Móveis e Decoração"),
            ("Lixeira Inteligente Automática Cinza Com Sensor",
             "Casa, Móveis e Decoração"),
            ("Plaina Elétrica Manual Para Madeira Bfp780 780w",
             "Ferramentas e Manutenção"),
            ("Interruptor Touch WI-FI 6 Teclas EIW 1006 Branco Intelbras",
             "Ferramentas e Manutenção"),
        )
        for titulo, macro in casos:
            with self.subTest(titulo=titulo):
                self.assertEqual(classificar_oferta_por_nome(titulo), macro)

    def test_suplemento_em_capsulas(self):
        self.assertEqual(
            classificar_oferta_por_nome("Colagentek Tipo II 613mg (60 caps), VitaFor"),
            "Saúde, Ortopedia e Equipamentos Médicos",
        )


class ProdutoDigitalFicaDeForaTests(SimpleTestCase):
    """Infoproduto não é achado de grupo de ofertas.

    Sem macro ele nunca é roteado para um nicho, e isso é o comportamento certo —
    não uma lacuna a preencher. Publicar "600 Projetos de Móveis em Metalon" num
    grupo de eletrodomésticos queima o grupo.
    """

    def test_pack_digital_nao_ganha_nicho(self):
        for titulo in (
            "600 Projetos Móveis Metalon E Arquivo Digital",
            "Mega Kit 935 Moldes De Roupas Digitais Completo",
            "Slides Profissionais Editáveis GraphicMix 32 Mil Para PowerPoint",
        ):
            with self.subTest(titulo=titulo):
                self.assertIsNone(classificar_oferta_por_nome(titulo))

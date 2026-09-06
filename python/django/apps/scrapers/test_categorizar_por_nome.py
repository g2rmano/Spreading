"""Classificar errado e pior que nao classificar.

Produto sem macro nao aparece em regra nenhuma — perde-se volume. Produto na macro
errada APARECE na regra errada, e o grupo recebe a "promocao de merda" que o filtro
de nicho existe para evitar. Por isso a bateria abaixo cobre tanto o acerto quanto
o silencio: empate nao pode virar palpite.
"""
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import ensure_personal_organization
from apps.scrapers.categorizar_por_nome import (
    macro_do_nome, popular_macro_por_nome,
)
from apps.scrapers.models import CupomNormalizado, FonteIngestao, Produto, ProdutoCupom


class MacroDoNomeTests(SimpleTestCase):
    def test_nomes_reais_de_producao(self):
        """Todos colhidos do catalogo em 04/09/2026."""
        casos = {
            "Airtag Para Coleira De Cachorro Suporte": "Pets e Animais",
            "10toalha Mesa 70x70 Cobre Mancha Tnt Festa": "Casa, Móveis e Decoração",
            "Cápsulas Vazias de Gelatina Incolor Nº 0": "Alimentos e Bebidas",
            "Macadâmia Natural Inteira Tipo 2 Graúda - 500g": "Alimentos e Bebidas",
            "Celular Samsung Galaxy A17 Com Ia, 128gb, 4gb Ram":
                "Celulares, Telefonia e Wearables",
            "Notebook Positivo Vision R15m Amd Ryzen 7 16gb":
                "Eletrônicos e Informática",
        }
        for nome, esperado in casos.items():
            self.assertEqual(macro_do_nome(nome), esperado, nome)

    def test_peso_resolve_o_termo_mais_especifico(self):
        """Cada um destes empatava 1x1 e saia sem macro."""
        # relogio(Joias) x smartwatch(Celulares)
        self.assertEqual(
            macro_do_nome("Relógio Smartwatch Forestory Inteligente"),
            "Celulares, Telefonia e Wearables")
        # aspirador(Eletrodomesticos) tem de ganhar do resto do nome
        self.assertEqual(
            macro_do_nome("Robô Aspirador S40 Pro Alexa E Google Branco"),
            "Eletrodomésticos")
        # compressor(Ferramentas) x calibrador de pneu(Automotivo)
        self.assertEqual(
            macro_do_nome("Compressor Portátil Digital Calibrador De Pneu"),
            "Automotivo")

    def test_qualificador_no_fim_nao_manda_no_resultado(self):
        """A cabeca do nome decide; o que vem depois e complemento.

        Auditoria de 25 nomes reais em 06/09/2026: 1 em cada 4 ia para a macro
        errada porque a ultima palavra vencia a primeira. Uma lanterna virava
        material eletrico por causa de "Bateria" no fim, e um oculos de natacao
        virava jardim por causa de "Piscina".
        """
        # "Anel de vedacao" e peca de manutencao; "processador" e complemento.
        self.assertEqual(macro_do_nome("2 Anéis De Vedação Para Processador"),
                         "Ferramentas e Manutenção")
        # "bateria" no fim nao transforma lanterna em material eletrico
        self.assertEqual(
            macro_do_nome("Lanterna Tática Voxo Militar T9 Zoom Potente Bateria"), "")
        # "piscina" no fim nao transforma oculos de natacao em jardim
        self.assertEqual(macro_do_nome("Óculos Natação Hero Band Mergulho Piscina"),
                         "Esportes e Fitness")
        # a quantidade que abre o anuncio nao gasta a janela
        self.assertEqual(macro_do_nome("50 Sacolas Plástica Premium Boca De Palhaço"),
                         "Embalagens e Descartáveis")

    def test_termo_composto_pode_terminar_fora_da_cabeca(self):
        """Comecar na cabeca basta; cortar no limite seco perdia a resposta certa."""
        self.assertEqual(
            macro_do_nome("Compressor Portátil Digital Calibrador De Pneu"),
            "Automotivo")

    def test_nome_sem_sinal_fica_sem_macro(self):
        self.assertEqual(macro_do_nome("2 Gh - Ghmuscle"), "")
        self.assertEqual(macro_do_nome("Produto Generico Qualquer Coisa"), "")
        self.assertEqual(macro_do_nome(""), "")
        self.assertEqual(macro_do_nome(None), "")

    def test_casa_palavra_inteira_e_ignora_acento(self):
        # "cama" nao pode casar dentro de "camarao"
        self.assertNotEqual(macro_do_nome("Camarão Descascado Congelado 1kg"),
                            "Casa, Móveis e Decoração")
        # e o acento nao pode impedir o acerto
        self.assertEqual(macro_do_nome("PANELA DE PRESSAO 4,5L"),
                         macro_do_nome("Panela de Pressão 4,5L"))

    def test_marca_que_atravessa_categoria_nao_classifica(self):
        """Xiaomi vende celular, aspirador, patinete e TV."""
        self.assertEqual(macro_do_nome("Xiaomi Original Lacrado"), "")


class PopularMacroPorNomeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("catnome", password="x")
        ensure_personal_organization(cls.user)
        cls.fonte, _ = FonteIngestao.objects.get_or_create(
            slug="ml-cupons-cat",
            defaults={"marketplace": "mercadolivre", "nome": "ML", "status": "ok"})

    def _produto(self, nome, *, macro="", link=None):
        return Produto.objects.create(
            owner=self.user, marketplace="mercadolivre", nome=nome, origem="cupom",
            preco_sem_desconto=100.0, preco_com_cupom=100.0, macro_categoria=macro,
            link_produto=link or f"https://produto.mercadolivre.com.br/{nome[:12]}",
        )

    def _com_cupom(self, produto, codigo):
        cupom = CupomNormalizado.objects.create(
            fonte=self.fonte, external_id=f"c:{codigo}", marketplace="mercadolivre",
            titulo=codigo, codigo=codigo, estado="ativo", scope_type="sitewide",
            redemption_mode="code",
        )
        ProdutoCupom.objects.create(produto=produto, cupom=cupom, status="confirmado")
        return cupom

    def test_preenche_o_vazio_e_nao_toca_no_que_ja_tem(self):
        vazio = self._produto("Furadeira De Impacto 750w")
        ja_tem = self._produto("Cafeteira Expressa Automatica",
                               macro="Casa, Móveis e Decoração",
                               link="https://produto.mercadolivre.com.br/JATEM")

        self.assertEqual(popular_macro_por_nome(), 1)

        vazio.refresh_from_db()
        ja_tem.refresh_from_db()
        self.assertEqual(vazio.macro_categoria, "Ferramentas e Manutenção")
        # Categoria vinda do marketplace e autoridade maior; nao se sobrescreve.
        self.assertEqual(ja_tem.macro_categoria, "Casa, Móveis e Decoração")

    def test_apenas_com_cupom_ignora_quem_nao_tem_par(self):
        com = self._produto("Panela De Pressao 4,5 Litros",
                            link="https://produto.mercadolivre.com.br/COM")
        self._com_cupom(com, "CUPOMPANELA")
        sem = self._produto("Tenis Esportivo Masculino Corrida",
                            link="https://produto.mercadolivre.com.br/SEM")

        self.assertEqual(popular_macro_por_nome(apenas_com_cupom=True), 1)

        com.refresh_from_db()
        sem.refresh_from_db()
        self.assertEqual(com.macro_categoria, "Cozinha, Mesa e Bar")
        self.assertEqual(sem.macro_categoria or "", "")

    def test_e_idempotente(self):
        self._produto("Geladeira Frost Free 400 Litros")
        self.assertEqual(popular_macro_por_nome(), 1)
        self.assertEqual(popular_macro_por_nome(), 0)

    def test_nome_sem_sinal_nao_recebe_palpite(self):
        indefinido = self._produto("2 Gh - Ghmuscle")
        self.assertEqual(popular_macro_por_nome(), 0)
        indefinido.refresh_from_db()
        self.assertEqual(indefinido.macro_categoria or "", "")

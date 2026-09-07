"""Telegram é canal de entrega, e até agora era o único sem defesa contra duplicata.

O caminho do WhatsApp ganhou quatro camadas para nunca publicar a mesma oferta
duas vezes: ledger no worker, resultado "incerto" que não repete, chave de
idempotência por operação e consulta ao ledger antes de rotular um timeout. O
Telegram não ganhou nenhuma — e é o canal que a operação usa como rede de
segurança quando o WhatsApp cai.
"""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from apps.scrapers.senders.telegram import TelegramSender, _sem_token

TOKEN = "123456789:AAF-segredo-do-bot-que-nao-pode-vazar"


class TokenNoErroTests(SimpleTestCase):
    def test_token_do_caminho_da_url_e_mascarado(self):
        # `requests` põe a URL inteira no texto do ConnectionError, e o token do
        # bot vive no CAMINHO — que a redação de log do projeto não cobre. Sem
        # isto o segredo ia para EventoOperacional.contexto e para o Sentry.
        texto = (f"HTTPSConnectionPool(host='api.telegram.org'): "
                 f"Max retries with url: /bot{TOKEN}/sendMessage")
        limpo = _sem_token(texto, TOKEN)
        self.assertNotIn(TOKEN, limpo)
        self.assertNotIn("segredo-do-bot", limpo)

    def test_token_de_outra_conta_tambem_e_mascarado(self):
        # Rede de segurança: mascara qualquer /bot<id>:<segredo>/, não só o token
        # desta chamada.
        limpo = _sem_token("erro em /bot99:OUTRO-SEGREDO/sendPhoto", "")
        self.assertNotIn("OUTRO-SEGREDO", limpo)


class DesfechoDeTimeoutTests(SimpleTestCase):
    def setUp(self):
        # Mesmo padrão do resto da suíte: o token é campo cifrado e o teste é sobre
        # o transporte, não sobre a criptografia.
        self.user = SimpleNamespace(perfil=SimpleNamespace(telegram_bot_token=TOKEN))
        self.sender = TelegramSender()

    def _enviar(self, excecao):
        with patch("apps.scrapers.senders.telegram.requests.post",
                   side_effect=excecao):
            return self.sender.enviar_oferta(
                "@canal_de_teste", "oferta", usuario=self.user,
                operation_id="op-123",
            )

    def test_timeout_de_leitura_e_incerto_e_nao_repete(self):
        # O pedido SAIU. O Bot API pode já ter publicado; repetir duplica a oferta
        # no canal. Antes isto voltava como transitório e `padronizar_resultado`
        # calculava `repetir=True`.
        r = self._enviar(requests.ReadTimeout("read timed out"))
        self.assertFalse(r["sucesso"])
        self.assertEqual(r["resultado"], "incerto")
        self.assertFalse(r["repetir"])

    def test_falha_ao_conectar_pode_repetir(self):
        # Não chegou a conectar: nada saiu, repetir é seguro e desejável.
        r = self._enviar(requests.ConnectTimeout("connect timed out"))
        self.assertFalse(r["sucesso"])
        self.assertNotEqual(r.get("resultado"), "incerto")
        self.assertTrue(r["repetir"])

    def test_erro_de_conexao_nao_vaza_o_token(self):
        r = self._enviar(requests.ConnectionError(
            f"Max retries with url: /bot{TOKEN}/sendMessage"))
        self.assertNotIn(TOKEN, r["erro"])

    def test_sucesso_devolve_o_id_da_mensagem(self):
        resposta = Mock(status_code=200)
        resposta.json.return_value = {"ok": True, "result": {"message_id": 42}}
        with patch("apps.scrapers.senders.telegram.requests.post",
                   return_value=resposta):
            r = self.sender.enviar_oferta("@canal_de_teste", "oferta",
                                          usuario=self.user)
        self.assertTrue(r["sucesso"])
        self.assertEqual(r["mensagem_id"], "42")

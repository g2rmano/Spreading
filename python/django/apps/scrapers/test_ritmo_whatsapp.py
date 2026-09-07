"""Ritmo do número de WhatsApp: nem rajada, nem relógio.

A detecção de automação do WhatsApp é heurística, não uma regra publicada, e o
sinal mais forte é cadência. O jitter de `ConfiguracaoEnvio.agendar_proximo` já
deixava CADA regra irregular — mas com dez grupos, dez regras vencendo no mesmo
tique disparavam em sequência, poucos segundos entre si, pelo mesmo número. Isso é
o padrão de robô que o jitter por regra não vê.

Vale só para WhatsApp: o Telegram é API oficial e não tem risco de banimento.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.scrapers import ofertas
from apps.scrapers.models import ConfiguracaoEnvio, Publicacao


@override_settings(WHATSAPP_ESPACO_MIN_S=0, WHATSAPP_ESPACO_MAX_S=0)
class RitmoDoNumeroTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("ritmo", password="x")
        self.user.perfil.marcar_verificado()
        self.configs = [
            ConfiguracaoEnvio.objects.create(
                owner=self.user, grupo_id=f"grupo{i}@g.us", canal="whatsapp",
                janela_inicio=0, janela_fim=0,  # 24h: o teste não depende da hora
            )
            for i in range(4)
        ]

    def _processar(self):
        with patch("apps.scrapers.whatsapp_client.status",
                   return_value={"conectado": True}), \
                patch("apps.scrapers.ofertas.selecionar_e_enviar",
                      return_value={"sucesso": True}) as enviar:
            ofertas.processar_configs_de_envio()
        return enviar

    @override_settings(WHATSAPP_ENVIOS_POR_TICK=2, WHATSAPP_ENVIOS_POR_HORA=100)
    def test_no_maximo_dois_envios_do_mesmo_numero_por_tique(self):
        # Quatro regras vencidas, um número. Sem o teto, as quatro saíam em
        # sequência. As outras duas não são perdidas: continuam vencidas e saem no
        # tique seguinte, que é justamente o espaçamento desejado.
        enviar = self._processar()
        self.assertEqual(enviar.call_count, 2)

    @override_settings(WHATSAPP_ENVIOS_POR_TICK=10, WHATSAPP_ENVIOS_POR_HORA=2)
    def test_teto_por_hora_conta_o_que_ja_saiu(self):
        agora = timezone.now()
        for _ in range(2):
            Publicacao.objects.create(
                usuario=self.user, canal="whatsapp", destino_id="grupo0@g.us",
                status="enviado", enviada_em=agora, criada_em=agora,
            )
        enviar = self._processar()
        self.assertEqual(enviar.call_count, 0)

    @override_settings(WHATSAPP_ENVIOS_POR_TICK=10, WHATSAPP_ENVIOS_POR_HORA=2)
    def test_publicacao_velha_nao_conta_para_o_teto_horario(self):
        antiga = timezone.now() - timezone.timedelta(hours=3)
        for _ in range(5):
            publicacao = Publicacao.objects.create(
                usuario=self.user, canal="whatsapp", destino_id="grupo0@g.us",
                status="enviado", enviada_em=antiga,
            )
            # `criada_em` é auto_now_add; a janela é sobre ela.
            Publicacao.objects.filter(pk=publicacao.pk).update(criada_em=antiga)
        enviar = self._processar()
        self.assertGreater(enviar.call_count, 0)

    @override_settings(WHATSAPP_ENVIOS_POR_TICK=1, WHATSAPP_ENVIOS_POR_HORA=100)
    def test_telegram_nao_entra_no_teto_do_whatsapp(self):
        # Sem risco de banimento e sem número para proteger: o teto do WhatsApp
        # não pode frear o canal oficial.
        ConfiguracaoEnvio.objects.update(canal="telegram")
        enviar = self._processar()
        self.assertEqual(enviar.call_count, 4)

from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from apps.scrapers import wa_supervisor

_SETTINGS = dict(
    WA_SUPERVISOR_ENABLED=True,
    WA_SUPERVISOR_FALHAS=3,
    WA_SUPERVISOR_COOLDOWN_MIN=15,
    WA_MACHINE_APP="spreading-wa",
    FLY_API_TOKEN="token-teste",
    WHATSAPP_API_URL="http://wa-interno:3000",
)


class DecidirAcaoTests(SimpleTestCase):
    """A decisão pura: sem token vira no-op, o limite é seguido e o cooldown
    segura o restart — é ele que impede o loop de restarts."""

    def test_sem_token_e_noop_mesmo_com_limite_estourado(self):
        self.assertEqual(
            wa_supervisor.decidir_acao(token="", falhas_seguidas=99,
                                       em_cooldown=False, falhas_limite=3),
            "noop",
        )

    def test_abaixo_do_limite_aguarda(self):
        for falhas in (1, 2):
            self.assertEqual(
                wa_supervisor.decidir_acao(token="t", falhas_seguidas=falhas,
                                           em_cooldown=False, falhas_limite=3),
                "aguardar",
            )

    def test_no_limite_reinicia(self):
        self.assertEqual(
            wa_supervisor.decidir_acao(token="t", falhas_seguidas=3,
                                       em_cooldown=False, falhas_limite=3),
            "reiniciar",
        )

    def test_no_limite_mas_em_cooldown_aguarda(self):
        self.assertEqual(
            wa_supervisor.decidir_acao(token="t", falhas_seguidas=99,
                                       em_cooldown=True, falhas_limite=3),
            "aguardar",
        )


class GestoParaEstadoTests(SimpleTestCase):
    """Máquina parada precisa de /start; máquina em transição não precisa de nada."""

    def test_parada_pede_start(self):
        for estado in ("stopped", "suspended", "STOPPED"):
            self.assertEqual(wa_supervisor.gesto_para_estado(estado), "start")

    def test_em_transicao_aguarda(self):
        for estado in ("starting", "stopping", "created", "replacing"):
            self.assertEqual(wa_supervisor.gesto_para_estado(estado), "aguardar")

    def test_viva_ou_desconhecida_pede_restart(self):
        for estado in ("started", "", "coisa-nova"):
            self.assertEqual(wa_supervisor.gesto_para_estado(estado), "restart")


@override_settings(**_SETTINGS)
class VerificarTests(SimpleTestCase):
    """O wrapper stateful: contador e cooldown no cache (falso, locmem), sonda e
    API do Fly mockadas — nenhum IO real."""

    def setUp(self):
        cache.clear()
        wa_supervisor._avisou_sem_token = False
        wa_supervisor._ULTIMO_CORPO = {}

    def _rodar_falhas(self, n, reiniciar, listar):
        with patch.object(wa_supervisor, "_sonda_saudavel", return_value=False), \
             patch.object(wa_supervisor.fly_infra, "_listar_maquinas", listar), \
             patch.object(wa_supervisor.fly_infra, "reiniciar_maquina", reiniciar), \
             patch.object(wa_supervisor, "log_event") as evento:
            return [wa_supervisor.verificar() for _ in range(n)], evento

    def test_contador_sobe_nas_falhas_e_reinicia_no_limite(self):
        listar = MagicMock(return_value=[{"id": "mach-123"}])
        reiniciar = MagicMock()
        acoes, evento = self._rodar_falhas(3, reiniciar, listar)
        self.assertEqual(acoes, ["aguardar", "aguardar", "reiniciado"])
        # O id veio da API (nada de id fixo no código) e o restart foi UMA vez.
        listar.assert_called_once_with("spreading-wa")
        reiniciar.assert_called_once_with("spreading-wa", "mach-123")
        # O restart aparece na Saúde em vez de ser silencioso.
        self.assertEqual(evento.call_args[0][0], "whatsapp")
        self.assertEqual(evento.call_args[0][1], "worker_reiniciado")
        self.assertEqual(evento.call_args[1]["level"], "error")

    def test_sonda_ok_zera_o_contador(self):
        cache.set("wa_supervisor_falhas", 2)
        with patch.object(wa_supervisor, "_sonda_saudavel", return_value=True), \
             patch.object(wa_supervisor.fly_infra, "reiniciar_maquina") as reiniciar:
            self.assertEqual(wa_supervisor.verificar(), "ok")
        self.assertEqual(cache.get("wa_supervisor_falhas"), 0)
        reiniciar.assert_not_called()

    def test_cooldown_impede_loop_de_restart(self):
        listar = MagicMock(return_value=[{"id": "mach-123"}])
        reiniciar = MagicMock()
        self._rodar_falhas(3, reiniciar, listar)  # atinge o limite e reinicia
        self.assertEqual(reiniciar.call_count, 1)
        # Falhas continuam (worker subindo) — sem cooldown cada passada reiniciaria.
        acoes, _ = self._rodar_falhas(6, reiniciar, listar)
        self.assertEqual(acoes, ["aguardar"] * 6)
        self.assertEqual(reiniciar.call_count, 1)

    def test_sem_token_nem_sonda(self):
        with override_settings(FLY_API_TOKEN=""):
            with patch.object(wa_supervisor, "_sonda_saudavel") as sonda, \
                 patch.object(wa_supervisor.fly_infra, "reiniciar_maquina") as reiniciar:
                self.assertEqual(wa_supervisor.verificar(), "sem_token")
                self.assertEqual(wa_supervisor.verificar(), "sem_token")
        sonda.assert_not_called()
        reiniciar.assert_not_called()

    def test_desligado_pela_flag_nao_faz_nada(self):
        with override_settings(WA_SUPERVISOR_ENABLED=False):
            with patch.object(wa_supervisor, "_sonda_saudavel") as sonda:
                self.assertEqual(wa_supervisor.verificar(), "desligado")
        sonda.assert_not_called()

    def test_maquina_parada_e_ligada_em_vez_de_reiniciada(self):
        """O bug de 18/08: com a VM em 'stopped' o /restart devolvia 409 e o
        worker ficava caído para sempre."""
        listar = MagicMock(return_value=[{"id": "mach-123", "estado": "stopped"}])
        with patch.object(wa_supervisor, "_sonda_saudavel", return_value=False), \
             patch.object(wa_supervisor.fly_infra, "_listar_maquinas", listar), \
             patch.object(wa_supervisor.fly_infra, "iniciar_maquina") as iniciar, \
             patch.object(wa_supervisor.fly_infra, "reiniciar_maquina") as reiniciar, \
             patch.object(wa_supervisor, "log_event"):
            acoes = [wa_supervisor.verificar() for _ in range(3)]
        self.assertEqual(acoes, ["aguardar", "aguardar", "reiniciado"])
        iniciar.assert_called_once_with("spreading-wa", "mach-123")
        reiniciar.assert_not_called()

    def test_maquina_subindo_nao_leva_gesto(self):
        listar = MagicMock(return_value=[{"id": "mach-123", "estado": "starting"}])
        with patch.object(wa_supervisor, "_sonda_saudavel", return_value=False), \
             patch.object(wa_supervisor.fly_infra, "_listar_maquinas", listar), \
             patch.object(wa_supervisor.fly_infra, "iniciar_maquina") as iniciar, \
             patch.object(wa_supervisor.fly_infra, "reiniciar_maquina") as reiniciar, \
             patch.object(wa_supervisor, "log_event"):
            acoes = [wa_supervisor.verificar() for _ in range(3)]
        self.assertEqual(acoes[-1], "aguardar")
        iniciar.assert_not_called()
        reiniciar.assert_not_called()

    def test_tentativa_que_falha_tambem_arma_cooldown(self):
        """Sem isto o vigia volta em 15s e mata o worker no meio do boot — foi
        assim que um 409 virou 8h de WhatsApp fora do ar."""
        listar = MagicMock(return_value=[{"id": "mach-123", "estado": "started"}])
        reiniciar = MagicMock(side_effect=RuntimeError("409 Conflict"))
        with patch.object(wa_supervisor, "_sonda_saudavel", return_value=False), \
             patch.object(wa_supervisor.fly_infra, "_listar_maquinas", listar), \
             patch.object(wa_supervisor.fly_infra, "reiniciar_maquina", reiniciar), \
             patch.object(wa_supervisor, "log_event"):
            acoes = [wa_supervisor.verificar() for _ in range(9)]
        self.assertEqual(acoes[2], "erro")
        # As 6 passadas seguintes ficam presas no cooldown: UMA tentativa só.
        self.assertEqual(reiniciar.call_count, 1)
        self.assertEqual(acoes[3:], ["aguardar"] * 6)

    def test_falha_da_api_fly_nao_derruba_o_monitor(self):
        listar = MagicMock(side_effect=RuntimeError("API Fly fora"))
        cache.set("wa_supervisor_falhas", 2)  # a próxima falha atinge o limite
        with patch.object(wa_supervisor, "_sonda_saudavel", return_value=False), \
             patch.object(wa_supervisor.fly_infra, "_listar_maquinas", listar):
            self.assertEqual(wa_supervisor.verificar(), "erro")


@override_settings(**_SETTINGS)
class SessaoTravadaTests(SimpleTestCase):
    """O buraco que o vigia tinha: /health só dizia se o PROCESSO estava mudo.

    Sessão em `expirado`, `falha_auth` ou `recuperacao_pausada` é um estado do
    qual ela não sai sozinha — o worker responde 200 e não envia nada. Antes isso
    só aparecia na tela de Saúde, que ninguém abre; foi assim que uma sessão
    passou horas anunciando presença sem entregar mensagem. Restart não resolve
    (a sessão volta no mesmo estado), então o gesto certo é abrir incidente, que
    é o que aciona o canal de alerta.
    """

    def setUp(self):
        cache.clear()
        wa_supervisor._avisou_sem_token = False
        wa_supervisor._ULTIMO_CORPO = {}

    def _verificar_com_corpo(self, corpo):
        def sonda():
            wa_supervisor._ULTIMO_CORPO = corpo
            return True

        with patch.object(wa_supervisor, "_sonda_saudavel", side_effect=sonda),              patch.object(wa_supervisor.fly_infra, "reiniciar_maquina") as reiniciar,              patch.object(wa_supervisor, "log_event") as evento:
            resultado = wa_supervisor.verificar()
        return resultado, evento, reiniciar

    def test_sessao_travada_abre_incidente_e_nao_reinicia(self):
        resultado, evento, reiniciar = self._verificar_com_corpo(
            {"sessions_total": 1, "sessions_ready": 0, "sessions_stuck": 1}
        )
        self.assertEqual(resultado, "ok")  # o processo está vivo; isso não muda
        reiniciar.assert_not_called()      # restart devolveria a sessão no mesmo estado
        self.assertEqual(evento.call_args[0][0], "whatsapp")
        self.assertEqual(evento.call_args[0][1], "sessao_travada")
        self.assertEqual(evento.call_args[1]["level"], "error")
        self.assertEqual(evento.call_args[1]["contexto"]["sessions_stuck"], 1)

    def test_sessao_que_caiu_e_voltou_ao_qr_tambem_avisa(self):
        """Nao e fase terminal, e por isso passou despercebida.

        06/09/2026: pareada em 04/09 12:31, de volta ao QR sem um unico evento de
        logout no historico. O sistema leu como instalacao nova esperando o
        primeiro scan e nao avisou ninguem. Instalacao nova pode esperar; sessao
        que caiu, nao.
        """
        resultado, evento, reiniciar = self._verificar_com_corpo(
            {"sessions_total": 1, "sessions_ready": 0, "sessions_stuck": 0,
             "sessions_repareamento": 1}
        )
        self.assertEqual(resultado, "ok")
        reiniciar.assert_not_called()
        self.assertEqual(evento.call_args[0][1], "sessao_travada")
        self.assertEqual(evento.call_args[1]["level"], "error")
        self.assertEqual(evento.call_args[1]["contexto"]["sessions_repareamento"], 1)
        self.assertIn("pedindo QR", evento.call_args[0][2])

    def test_qr_de_instalacao_nova_nao_avisa(self):
        """Sem `.paired`, o Node nao conta como repareamento — e nada dispara."""
        _, evento, _ = self._verificar_com_corpo(
            {"sessions_total": 1, "sessions_ready": 0, "sessions_stuck": 0,
             "sessions_repareamento": 0}
        )
        evento.assert_not_called()

    def test_sessao_saudavel_nao_avisa_nada(self):
        _, evento, _ = self._verificar_com_corpo(
            {"sessions_total": 1, "sessions_ready": 1, "sessions_stuck": 0}
        )
        evento.assert_not_called()

    def test_health_antigo_sem_os_contadores_nao_avisa(self):
        """Durante o deploy o Django novo conversa com o Node velho."""
        _, evento, _ = self._verificar_com_corpo({"ok": True, "capacity": {}})
        evento.assert_not_called()

    def test_avisa_uma_vez_e_cala_enquanto_ninguem_reconecta(self):
        corpo = {"sessions_total": 1, "sessions_ready": 0, "sessions_stuck": 1}
        _, evento, _ = self._verificar_com_corpo(corpo)
        self.assertEqual(evento.call_count, 1)
        for _ in range(5):
            _, evento, _ = self._verificar_com_corpo(corpo)
            evento.assert_not_called()

    def test_sessao_voltando_rearma_o_aviso(self):
        travado = {"sessions_total": 1, "sessions_ready": 0, "sessions_stuck": 1}
        saudavel = {"sessions_total": 1, "sessions_ready": 1, "sessions_stuck": 0}
        _, evento, _ = self._verificar_com_corpo(travado)
        self.assertEqual(evento.call_count, 1)
        self._verificar_com_corpo(saudavel)   # reconectou: o silêncio é liberado
        _, evento, _ = self._verificar_com_corpo(travado)
        self.assertEqual(evento.call_count, 1)

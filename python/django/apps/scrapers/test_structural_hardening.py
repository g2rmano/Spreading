import base64
import io
import json
import logging
import sys
from contextlib import contextmanager
from datetime import date, timedelta
from unittest.mock import Mock, patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import (
    Membership, MercadoLivreSession, OrganizationFeatureOverride,
    WhatsAppConnection, ensure_personal_organization, organization_for_user,
)
from apps.scrapers.models import (
    ConfiguracaoEnvio,
    CupomDisponibilidade, CupomDisponibilidadeEvento, CupomFonteObservacao,
    CupomNormalizado, CupomPreparacao,
    FonteIngestao,
    LinkAfiliadoCupomUsuario, LinkAfiliadoProdutoCupomUsuario,
    LinkAfiliadoUsuario, Produto, ProdutoCupom,
    Publicacao, ResourceLease, WorkerHeartbeat,
)


class WhatsAppOrganizationInvariantTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("wa-owner", password="x")
        self.member = get_user_model().objects.create_user("wa-member", password="x")
        self.organization = ensure_personal_organization(self.owner)
        Membership.objects.create(
            organization=self.organization, user=self.member, role="operator",
        )
        self.member.perfil.active_organization = self.organization
        self.member.perfil.wa_session = "legacy-must-not-win"
        self.member.perfil.save(update_fields=["active_organization", "wa_session"])

    def test_dois_usuarios_da_mesma_organizacao_usam_a_mesma_sessao(self):
        expected = self.organization.whatsapp_connection.instance_id

        self.assertEqual(organization_for_user(self.owner), self.organization)
        self.assertEqual(organization_for_user(self.member), self.organization)
        self.assertEqual(self.owner.perfil.sessao_whatsapp(), expected)
        self.assertEqual(self.member.perfil.sessao_whatsapp(), expected)
        self.assertNotEqual(self.member.perfil.sessao_whatsapp(), "legacy-must-not-win")

    def test_organizacao_ativa_exige_membership_ativa(self):
        outsider = get_user_model().objects.create_user("wa-outsider", password="x")
        outsider.perfil.active_organization = self.organization

        with self.assertRaises(ValidationError):
            outsider.perfil.save(update_fields=["active_organization"])

    def test_one_to_one_impede_segunda_sessao_na_organizacao(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            WhatsAppConnection.objects.create(
                organization=self.organization, instance_id="duplicate-session",
            )

    def test_telemetria_aceita_apenas_numero_mascarado(self):
        from apps.scrapers.whatsapp_client import _persistir_telemetria

        session = self.organization.whatsapp_connection.instance_id
        _persistir_telemetria(session, {
            "conectado": False,
            "fase": "recuperando",
            "numero_mascarado": "+55 11 99888-7766",
            "worker": "machine-safe",
            "capacidade": {"usadas": 2, "maximas": 2},
            "motivo_indisponibilidade": "global_capacity",
        })
        connection = WhatsAppConnection.objects.get(organization=self.organization)
        self.assertEqual(connection.masked_number, "")
        self.assertEqual(connection.capacity_used, 2)
        self.assertEqual(connection.capacity_max, 2)
        self.assertEqual(connection.unavailable_reason, "global_capacity")

        _persistir_telemetria(session, {"numero_mascarado": "***7766"})
        connection.refresh_from_db()
        self.assertEqual(connection.masked_number, "***7766")

        _persistir_telemetria(session, {
            "fase": "conectado", "consistency_status": "adopted",
        })
        connection.refresh_from_db()
        self.assertEqual(connection.status, "active")
        self.assertEqual(connection.consistency_status, "adopted")


class SendPipelineOrganizationRolloutTests(TestCase):
    def setUp(self):
        self.user_a = get_user_model().objects.create_user("send-v2-a", password="x")
        self.user_b = get_user_model().objects.create_user("send-v2-b", password="x")
        self.org_a = ensure_personal_organization(self.user_a)
        self.org_b = ensure_personal_organization(self.user_b)

    @override_settings(SEND_PIPELINE_V2_ENABLED=False, PILOT_ORGANIZATION_IDS=set())
    def test_kill_switch_global_vence_override_enabled(self):
        from apps.accounts.feature_flags import feature_decision

        OrganizationFeatureOverride.objects.create(
            organization=self.org_a, feature="SEND_PIPELINE_V2_ENABLED",
            state="enabled",
        )
        self.assertEqual(
            feature_decision("SEND_PIPELINE_V2_ENABLED", self.user_a),
            (False, "global_kill_switch"),
        )

    @override_settings(SEND_PIPELINE_V2_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_allowlist_vazia_nao_libera_todas_as_organizacoes(self):
        from apps.accounts.feature_flags import feature_decision

        self.assertEqual(
            feature_decision("SEND_PIPELINE_V2_ENABLED", self.user_a),
            (False, "pilot_required"),
        )
        self.assertEqual(
            feature_decision("SEND_PIPELINE_V2_ENABLED", self.user_b),
            (False, "pilot_required"),
        )

    @override_settings(SEND_PIPELINE_V2_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_override_e_isolado_por_organizacao(self):
        from apps.accounts.feature_flags import feature_decision

        OrganizationFeatureOverride.objects.create(
            organization=self.org_a, feature="SEND_PIPELINE_V2_ENABLED",
            state="enabled",
        )
        self.assertEqual(
            feature_decision("SEND_PIPELINE_V2_ENABLED", self.user_a),
            (True, "organization_enabled"),
        )
        self.assertEqual(
            feature_decision("SEND_PIPELINE_V2_ENABLED", self.user_b),
            (False, "pilot_required"),
        )

    @override_settings(SEND_PIPELINE_V2_ENABLED=True)
    def test_disabled_da_organizacao_vence_allowlist(self):
        from apps.accounts.feature_flags import feature_decision

        OrganizationFeatureOverride.objects.create(
            organization=self.org_a, feature="SEND_PIPELINE_V2_ENABLED",
            state="disabled",
        )
        with override_settings(PILOT_ORGANIZATION_IDS={str(self.org_a.pk)}):
            self.assertEqual(
                feature_decision("SEND_PIPELINE_V2_ENABLED", self.user_a),
                (False, "organization_disabled"),
            )


class CouponSourceObservationTenantTests(TestCase):
    def test_mesma_identidade_de_fonte_nao_colide_entre_organizacoes(self):
        from apps.scrapers.sources.persistence import record_coupon_observation

        user_a = get_user_model().objects.create_user("observation-a", password="x")
        user_b = get_user_model().objects.create_user("observation-b", password="x")
        org_a = ensure_personal_organization(user_a)
        org_b = ensure_personal_organization(user_b)
        source = FonteIngestao.objects.create(
            slug="tenant-observation", marketplace="mercadolivre", nome="Tenant",
        )
        shared = {
            "fonte": source, "external_id": "same-external-id",
            "marketplace": "mercadolivre", "titulo": "Cupom privado",
            "codigo": "MESMO20", "estado": "ativo",
        }
        coupon_a = CupomNormalizado.objects.create(owner=user_a, **shared)
        coupon_b = CupomNormalizado.objects.create(owner=user_b, **shared)

        record_coupon_observation(coupon_a, outcome="accepted")
        record_coupon_observation(coupon_b, outcome="accepted")

        rows = CupomFonteObservacao.objects.order_by("organization_id")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            set(rows.values_list("organization_id", flat=True)),
            {org_a.pk, org_b.pk},
        )
        record_coupon_observation(coupon_a, outcome="invalid", reason_code="changed")
        self.assertEqual(CupomFonteObservacao.objects.count(), 2)
        self.assertEqual(
            CupomFonteObservacao.objects.get(organization=org_a).outcome,
            "invalid",
        )
        self.assertEqual(
            CupomFonteObservacao.objects.get(organization=org_b).outcome,
            "accepted",
        )


class BrowserResourceContractTests(TestCase):
    def test_slot_da_maquina_inclui_processos_fora_do_contexto_atual(self):
        import os
        import tempfile
        import threading

        from apps.scrapers.resource_control import machine_resource_slot

        ready = threading.Event()
        release = threading.Event()
        holder_result = []

        with tempfile.TemporaryDirectory() as lock_dir, patch.dict(
            os.environ, {"SPREADING_RESOURCE_LOCK_DIR": lock_dir},
        ):
            def hold_slot():
                with machine_resource_slot("django_chromium") as acquired:
                    holder_result.append(acquired)
                    ready.set()
                    release.wait(timeout=2)

            thread = threading.Thread(target=hold_slot)
            thread.start()
            self.assertTrue(ready.wait(timeout=1))
            with machine_resource_slot("django_chromium") as acquired:
                self.assertFalse(acquired)
            release.set()
            thread.join(timeout=2)

            self.assertEqual(holder_result, [True])
            self.assertFalse(thread.is_alive())
            with machine_resource_slot("django_chromium") as acquired:
                self.assertTrue(acquired)
                # Reentrância evita autoconflito em helpers aninhados do mesmo job.
                with machine_resource_slot("django_chromium") as nested:
                    self.assertTrue(nested)

    def test_pedido_interativo_e_visivel_a_outro_processo_e_expira(self):
        """O flock diz "ocupado"; ele não diz "tem gente esperando".

        Sem este canal, o login interativo disputava contra LOTES inteiros (40
        links a ~5s cada) e a espera estourava — a tela abria e fechava sozinha
        enquanto a lane pesada rodava.
        """
        import os
        import tempfile

        from apps.scrapers import resource_control
        from apps.scrapers.resource_control import (
            interesse_interativo, interesse_interativo_pendente,
        )

        with tempfile.TemporaryDirectory() as lock_dir, patch.dict(
            os.environ, {"SPREADING_RESOURCE_LOCK_DIR": lock_dir},
        ):
            self.assertFalse(interesse_interativo_pendente("django_chromium"))
            with interesse_interativo("django_chromium"):
                self.assertTrue(interesse_interativo_pendente("django_chromium"))
                # Um marcador esquecido por um processo morto não pode calar os
                # workers para sempre.
                with patch.object(
                    resource_control, "INTERESSE_INTERATIVO_TTL_S", -1,
                ):
                    self.assertFalse(
                        interesse_interativo_pendente("django_chromium"))
            self.assertFalse(interesse_interativo_pendente("django_chromium"))

    def test_pedido_manual_tambem_faz_lote_automatico_ceder(self):
        import os
        import tempfile

        from apps.scrapers import resource_control
        from apps.scrapers.resource_control import (
            interesse_interativo_pendente, limpar_interesse_manual,
            sinalizar_interesse_manual,
        )

        with tempfile.TemporaryDirectory() as lock_dir, patch.dict(
            os.environ, {"SPREADING_RESOURCE_LOCK_DIR": lock_dir},
        ):
            self.assertTrue(sinalizar_interesse_manual("django_chromium"))
            self.assertTrue(interesse_interativo_pendente("django_chromium"))
            with patch.object(resource_control, "INTERESSE_MANUAL_TTL_S", -1):
                self.assertFalse(interesse_interativo_pendente("django_chromium"))
            limpar_interesse_manual("django_chromium")
            self.assertFalse(interesse_interativo_pendente("django_chromium"))

    def test_a_suite_nunca_le_os_marcadores_da_maquina(self):
        """Contrato de hermeticidade, e ele já foi violado.

        `_caminho_de_lock` caía no /tmp da máquina quando ninguém apontava um
        diretório — inclusive durante os testes. Um `spreading-django_chromium.
        manual-wanted` deixado por uma raspagem manual de verdade fazia
        `casar_cupons_container` ceder o navegador no primeiro cupom, e
        `test_limite_de_cupons_por_passada` falhava com `1 != 10` sem nenhuma
        relação com o código sob teste. Uma execução vermelha, cinco verdes, mesmo
        comando: portão que às vezes mente ensina a ignorar vermelho.
        """
        import os
        import tempfile

        from apps.scrapers.resource_control import _diretorio_de_lock

        os.environ.pop("SPREADING_RESOURCE_LOCK_DIR", None)
        self.assertNotEqual(_diretorio_de_lock(), tempfile.gettempdir())
        # E continua obedecendo a quem aponta um diretório de propósito.
        with tempfile.TemporaryDirectory() as escolhido, patch.dict(
            os.environ, {"SPREADING_RESOURCE_LOCK_DIR": escolhido},
        ):
            self.assertEqual(_diretorio_de_lock(), escolhido)

    def test_a_suite_nao_escreve_no_estado_real_dos_workers(self):
        """Mesmo contrato, outro diretório: `.automacao` guarda o heartbeat e o
        cursor de retomada da varredura. A suíte escrevendo lá significaria um teste
        de raspagem deixando a página 3 marcada para o teste seguinte encontrar — e,
        no ambiente de quem desenvolve, mexendo no estado do worker de verdade."""
        import os

        from apps.scrapers import automacao_state

        real = os.path.join(
            getattr(settings, "ML_AUTH_DIR", "") or settings.BASE_DIR, ".automacao")
        self.assertNotEqual(os.path.abspath(automacao_state._DIR),
                            os.path.abspath(real))

    def test_esteira_negada_entra_na_fila_e_sai_ao_conquistar(self):
        """O flock diz "ocupado"; a fila diz "tem uma ESTEIRA esperando".

        Até 20/08/2026 só uma pessoa podia interromper um lote longo. O resultado
        medido em produção: a varredura de 40 páginas segurava o Chromium por
        ~62min e links, verificação e envio perdiam a vez em todo ciclo — sem link
        nenhum cupom fica pronto, e sem cupom pronto o tick publica `0 enviada(s)`.
        """
        import os
        import tempfile

        from apps.scrapers import resource_control
        from apps.scrapers.resource_control import (
            esteiras_na_fila, interesse_pendente, limpar_interesse_de_esteira,
            sinalizar_interesse_de_esteira,
        )

        with tempfile.TemporaryDirectory() as lock_dir, patch.dict(
            os.environ, {"SPREADING_RESOURCE_LOCK_DIR": lock_dir},
        ):
            self.assertFalse(interesse_pendente("django_chromium"))

            self.assertTrue(sinalizar_interesse_de_esteira("links_generate"))
            self.assertEqual(esteiras_na_fila("django_chromium"), ["links_generate"])
            self.assertTrue(interesse_pendente("django_chromium"))

            # Quem pediu não cede para si mesmo — senão o lote não sairia do lugar.
            self.assertFalse(
                interesse_pendente("django_chromium", exceto="links_generate"))

            # Um processo morto no meio do ciclo não pode calar a varredura para
            # sempre: o pedido vale por uma janela, não indefinidamente.
            with patch.object(resource_control, "INTERESSE_DE_ESTEIRA_TTL_S", -1):
                self.assertFalse(interesse_pendente("django_chromium"))

            limpar_interesse_de_esteira("links_generate")
            self.assertFalse(interesse_pendente("django_chromium"))

    def test_as_duas_varreduras_longas_nao_ganham_prioridade(self):
        """Se `scrape` e `scrape_rapido` pudessem furar a fila, elas cederiam uma
        para a outra em looping e nenhuma terminaria uma passada."""
        import os
        import tempfile

        from apps.scrapers.resource_control import (
            esteiras_na_fila, sinalizar_interesse_de_esteira,
        )

        with tempfile.TemporaryDirectory() as lock_dir, patch.dict(
            os.environ, {"SPREADING_RESOURCE_LOCK_DIR": lock_dir},
        ):
            for lote_longo in ("scrape", "scrape_rapido", "ml_offers", "ml_search"):
                self.assertFalse(sinalizar_interesse_de_esteira(lote_longo))
            self.assertEqual(esteiras_na_fila("django_chromium"), [])

    def test_negativa_de_navegador_poe_a_esteira_na_fila_sozinha(self):
        """O registro mora no ponto da negativa: nenhum chamador precisa lembrar."""
        import os
        import tempfile
        from contextlib import contextmanager

        from apps.scrapers import carga
        from apps.scrapers.resource_control import esteiras_na_fila

        @contextmanager
        def _lease_negado(*_a, **_kw):
            yield (False, {})

        @contextmanager
        def _lease_dado(*_a, **_kw):
            yield ("token", {})

        with tempfile.TemporaryDirectory() as lock_dir, patch.dict(
            os.environ, {"SPREADING_RESOURCE_LOCK_DIR": lock_dir},
        ), patch.object(carga.connections["default"], "vendor", "postgresql"), \
                patch.object(carga, "in_system_context", return_value=True):
            with patch.object(carga, "leased_resource", _lease_negado):
                with carga.operacao_pesada(owner_kind="links_verify") as conseguiu:
                    self.assertFalse(conseguiu)
            self.assertEqual(esteiras_na_fila("django_chromium"), ["links_verify"])

            @contextmanager
            def _slot_dado(*_a, **_kw):
                yield True

            with patch.object(carga, "leased_resource", _lease_dado), \
                    patch.object(carga, "machine_resource_slot", _slot_dado):
                with carga.operacao_pesada(owner_kind="links_verify") as conseguiu:
                    self.assertTrue(conseguiu)
            # Conquistou: saiu da fila sem ninguém precisar limpar.
            self.assertEqual(esteiras_na_fila("django_chromium"), [])

    def test_lote_de_links_devolve_o_navegador_a_um_login_esperando(self):
        from apps.scrapers.scraper_mercadolivre import link as ml_link

        produtos = [
            Produto(
                id=indice, marketplace="mercadolivre", nome=f"Item {indice}",
                link_produto=f"https://produto.mercadolivre.com.br/MLB-{indice}",
                preco_sem_desconto=100, preco_com_cupom=90,
            )
            for indice in range(1, 5)
        ]
        afiliados = []

        @contextmanager
        def _sem_browser(**_kwargs):
            yield Mock(), Mock()

        with patch.object(ml_link, "coordinated_ml_browser",
                          lambda **_k: _sem_browser()), \
                patch.object(ml_link, "iniciar_browser", _sem_browser), \
                patch.object(ml_link, "_abrir_link_builder", Mock()), \
                patch.object(ml_link, "_salvar_link_global", Mock()), \
                patch.object(ml_link, "executar_no_tenant",
                             lambda fn, *a, **k: None), \
                patch.object(ml_link, "_afiliar_url_na_pagina",
                             lambda _p, url: afiliados.append(url) or "https://meli.la/x"), \
                patch.object(ml_link, "interesse_pendente",
                             lambda _r, exceto=None: len(afiliados) >= 2):
            gerados, _falhas = ml_link.gerar_links_em_lote(produtos)

        # Cede DEPOIS de terminar o item corrente: dois gerados, dois devolvidos
        # à fila — nada se perde, e o login não espera o lote inteiro.
        self.assertEqual(gerados, 2)
        self.assertEqual(len(afiliados), 2)

    def test_lease_aninhado_do_mesmo_recurso_reutiliza_token_sem_reaquisição(self):
        from apps.scrapers.resource_control import leased_resource

        with patch(
            "apps.scrapers.resource_control.acquire",
            return_value=("internal-token", {"resource": "django_chromium"}),
        ) as acquire_mock, patch(
            "apps.scrapers.resource_control.release", return_value=True,
        ) as release_mock:
            with leased_resource("django_chromium") as (outer_ok, outer):
                with leased_resource("django_chromium") as (inner_ok, inner):
                    self.assertTrue(outer_ok)
                    self.assertTrue(inner_ok)
                    self.assertEqual(inner["lease_token"], "internal-token")
                    self.assertTrue(inner["reentrant"])

        self.assertEqual(acquire_mock.call_count, 1)
        release_mock.assert_called_once_with("django_chromium", "internal-token")

    def test_browser_e_sessao_sempre_sao_adquiridos_em_ordem_estavel(self):
        from apps.scrapers.carga import browser_resource

        events = []

        @contextmanager
        def fake_operation(*, resource_key, **_kwargs):
            events.append(("acquire", resource_key))
            try:
                yield True
            finally:
                events.append(("release", resource_key))

        with patch("apps.scrapers.carga.operacao_pesada", fake_operation):
            with browser_resource(session_key="ml_site_session:org-a") as acquired:
                self.assertTrue(acquired)
                events.append(("work", "browser"))

        self.assertEqual(events, [
            ("acquire", "django_chromium"),
            ("acquire", "ml_site_session:org-a"),
            ("work", "browser"),
            ("release", "ml_site_session:org-a"),
            ("release", "django_chromium"),
        ])

    def test_dois_usuarios_da_mesma_org_compartilham_chave_de_sessao_ml(self):
        from apps.scrapers.carga import ml_site_browser_resource

        owner = get_user_model().objects.create_user("ml-lock-owner", password="x")
        member = get_user_model().objects.create_user("ml-lock-member", password="x")
        organization = ensure_personal_organization(owner)
        Membership.objects.create(
            organization=organization, user=member, role="operator",
        )
        member.perfil.active_organization = organization
        member.perfil.save(update_fields=["active_organization"])
        keys = []

        @contextmanager
        def fake_browser_resource(*, session_key, **_kwargs):
            keys.append(session_key)
            yield True

        with patch("apps.scrapers.carga.browser_resource", fake_browser_resource):
            with ml_site_browser_resource(owner) as owner_ok:
                self.assertTrue(owner_ok)
            with ml_site_browser_resource(member) as member_ok:
                self.assertTrue(member_ok)

        self.assertEqual(keys, [
            f"ml_site_session:{organization.pk}",
            f"ml_site_session:{organization.pk}",
        ])

    def test_ingestao_http_nao_consome_chromium_e_amazon_consumira(self):
        from apps.scrapers.sources.registry import _ingestion_guard

        resources = []

        @contextmanager
        def fake_lease(resource_key, **_kwargs):
            resources.append(resource_key)
            yield True, {"resource": resource_key}

        fake_connection = type("Connection", (), {"vendor": "postgresql"})()
        with patch("apps.scrapers.sources.registry.connection", fake_connection), \
                patch("apps.scrapers.resource_control.leased_resource", fake_lease):
            with _ingestion_guard("http-only", requires_chromium=False) as acquired:
                self.assertTrue(acquired)
            self.assertEqual(resources, ["source_ingest:http-only"])

            resources.clear()
            with _ingestion_guard(
                "amazon-public-coupons", requires_chromium=True,
            ) as acquired:
                self.assertTrue(acquired)
            self.assertEqual(resources, [
                "django_chromium", "source_ingest:amazon-public-coupons",
            ])

    def test_ingestao_sinaliza_fila_quando_chromium_esta_ocupado(self):
        from apps.scrapers.sources.registry import _ingestion_guard

        @contextmanager
        def unavailable_lease(_resource_key, **_kwargs):
            yield False, {"reason": "occupied"}

        fake_connection = type("Connection", (), {"vendor": "postgresql"})()
        with patch("apps.scrapers.sources.registry.connection", fake_connection), \
                patch("apps.scrapers.resource_control.leased_resource", unavailable_lease), \
                patch("apps.scrapers.resource_control.sinalizar_interesse_de_esteira") as signal:
            with _ingestion_guard(
                "shopee-public-coupons", requires_chromium=True,
            ) as acquired:
                self.assertEqual(acquired, (False, "capacity_deferred"))

        signal.assert_called_once_with("source_shopee-public-coupons")

    def test_ingestao_espera_holder_ceder_e_roda_no_mesmo_ciclo(self):
        from apps.scrapers.sources.registry import _ingestion_guard

        tentativas = iter((False, True, True))

        @contextmanager
        def lease_sequencial(_resource_key, **_kwargs):
            yield next(tentativas), {"resource": _resource_key}

        fake_connection = type("Connection", (), {"vendor": "postgresql"})()
        with patch("apps.scrapers.sources.registry.connection", fake_connection), \
                patch("apps.scrapers.resource_control.leased_resource", lease_sequencial), \
                patch("apps.scrapers.resource_control.sinalizar_interesse_de_esteira") as signal, \
                patch("apps.scrapers.sources.registry.time.sleep"):
            with _ingestion_guard(
                "shopee-public-coupons", requires_chromium=True, wait_seconds=1,
            ) as acquired:
                self.assertEqual(acquired, (True, ""))

        signal.assert_called_once_with("source_shopee-public-coupons")

    def test_scrape_e_flash_nao_seguram_chromium_o_ciclo_inteiro(self):
        import inspect
        from apps.scrapers.management.commands.automacao import Command

        scrape = inspect.getsource(Command._loop_scrape)
        flash = inspect.getsource(Command._loop_scrape_rapido)
        self.assertNotIn("operacao_pesada", scrape)
        self.assertNotIn("operacao_pesada", flash)
        self.assertIn("_rodar_scrape(", scrape)
        self.assertIn("_rodar_scrape_rapido()", flash)

    def test_boot_sobrevive_a_banco_fora_do_ar(self):
        """Postgres indisponível no boot não pode matar o processo.

        @system_job fazia SQL (checagem de role + set_config) antes do
        primeiro ciclo do loop; sem retry, honcho derrubava o grupo inteiro
        (8 workers, ou o gunicorn no Procfile.web) e a Fly reiniciava em
        crash-loop. handle() precisa absorver DatabaseError no boot e tentar
        de novo, sem propagar.
        """
        from django.db import DatabaseError
        from apps.scrapers.management.commands.automacao import Command

        cmd = Command()
        chamadas = {"n": 0}

        @contextmanager
        def system_context_instavel():
            chamadas["n"] += 1
            if chamadas["n"] < 3:
                raise DatabaseError("conexão fechada")
            yield

        with patch(
            "apps.scrapers.management.commands.automacao.system_context",
            system_context_instavel,
        ), patch(
            "apps.scrapers.management.commands.automacao.connections.close_all",
        ), patch(
            "apps.scrapers.management.commands.automacao.time.sleep",
        ) as sleep_mock, patch.object(
            Command, "_despachar", return_value="ok",
        ) as despachar_mock:
            resultado = cmd.handle(modo="scrape")

        self.assertEqual(resultado, "ok")
        self.assertEqual(chamadas["n"], 3)
        despachar_mock.assert_called_once()
        self.assertEqual(sleep_mock.call_count, 2)


class WhatsAppReconcileSafetyTests(SimpleTestCase):
    def tearDown(self):
        cache.clear()
        super().tearDown()

    @override_settings(WA_SESSION_RECONCILE_SECONDS=60)
    def test_reconciliacao_e_limitada_por_sessao(self):
        from apps.scrapers import whatsapp_client

        cache.clear()
        response = {
            "sucesso": True, "consistencia": "consistent", "runtime": "recuperando",
        }
        with patch.object(whatsapp_client, "_enabled", return_value=True), patch.object(
            whatsapp_client, "_headers", return_value={"Authorization": "redacted"},
        ), patch.object(
            whatsapp_client, "_request_json", return_value=response,
        ) as request, patch.object(whatsapp_client, "_persistir_telemetria"):
            self.assertEqual(whatsapp_client.reconciliar_sessao("safe-session"), response)
            self.assertEqual(whatsapp_client.reconciliar_sessao("safe-session"), response)
        request.assert_called_once()

    def test_erro_http_nao_devolve_detalhe_sensivel_da_excecao(self):
        from apps.scrapers import whatsapp_client

        raw_secret = "Bearer capability-secret cookie=session-secret"
        with patch.object(
            whatsapp_client.requests, "request", side_effect=RuntimeError(raw_secret),
        ):
            result = whatsapp_client._request_json(
                "GET", "/api/status", attempts=1,
            )
        serialized = json.dumps(result)
        self.assertNotIn("capability-secret", serialized)
        self.assertNotIn("session-secret", serialized)
        self.assertEqual(result["causa"], "RuntimeError")


class LogRedactionTests(SimpleTestCase):
    def test_formatter_remove_segredos_tambem_do_traceback(self):
        from core.logging import RedactingFormatter

        try:
            raise RuntimeError(
                "Bearer capability-secret cookie=session-secret "
                "https://portal.example/report?token=query-secret "
                "+55 11 99888-7766 " + ("A" * 300)
            )
        except RuntimeError:
            record = logging.LogRecord(
                "test", logging.ERROR, __file__, 1,
                "falha com storage_state=opaque-secret", (), exc_info=sys.exc_info(),
            )
        rendered = RedactingFormatter("%(message)s").format(record)
        for secret in (
            "capability-secret", "session-secret", "query-secret",
            "opaque-secret", "99888-7766", "A" * 256,
        ):
            self.assertNotIn(secret, rendered)
        self.assertIn("[redacted]", rendered)


class ManualQueueOperationalStateTests(TestCase):
    def setUp(self):
        self.user_a = get_user_model().objects.create_user("queue-a", password="x")
        self.user_b = get_user_model().objects.create_user("queue-b", password="x")
        self.org_a = ensure_personal_organization(self.user_a)
        self.org_b = ensure_personal_organization(self.user_b)

    def _job(self, user, organization):
        from apps.scrapers.models import ExecucaoRaspagem
        return ExecucaoRaspagem.objects.create(
            organization=organization, solicitada_por=user, tipo="ofertas",
            deadline_em=timezone.now() + timedelta(minutes=45),
        )

    @override_settings(MANUAL_QUEUE_NO_WORKER_TIMEOUT_SECONDS=60)
    def test_worker_ausente_encerra_fila_com_causa_acionavel(self):
        from apps.scrapers.manual_scraping import atualizar_diagnostico_fila

        job = self._job(self.user_a, self.org_a)
        type(job).objects.filter(pk=job.pk).update(
            criada_em=timezone.now() - timedelta(minutes=2),
        )
        atualizar_diagnostico_fila()
        job.refresh_from_db()

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.codigo_erro, "worker_unavailable")
        self.assertIn("processo manual", job.acao_recomendada)

    def test_posicao_worker_e_lock_owner_sao_duraveis(self):
        from apps.scrapers.manual_scraping import atualizar_diagnostico_fila

        first = self._job(self.user_a, self.org_a)
        second = self._job(self.user_b, self.org_b)
        # SQLite/Windows pode dar o mesmo instante aos dois auto_now_add; como a
        # PK é UUID aleatória, esse empate não prova ordem de submissão e deixava a
        # suíte integral intermitente. Este teste verifica posições distintas, não
        # a política para pedidos realmente simultâneos, então explicita a ordem.
        now = timezone.now()
        type(first).objects.filter(pk=first.pk).update(
            criada_em=now - timedelta(microseconds=1),
        )
        type(second).objects.filter(pk=second.pk).update(criada_em=now)
        WorkerHeartbeat.objects.create(
            worker_id="manual:test", worker_type="manual", state="idle",
        )
        ResourceLease.objects.create(
            resource_key="django_chromium", owner_token="occupied",
            owner_kind="reports", acquired_at=timezone.now(),
            heartbeat_at=timezone.now(), expires_at=timezone.now() + timedelta(seconds=90),
        )
        atualizar_diagnostico_fila()
        first.refresh_from_db()
        second.refresh_from_db()

        self.assertEqual(first.posicao_fila, 1)
        self.assertEqual(first.motivo_espera, "resource_busy")
        self.assertEqual(first.lock_owner_tipo, "reports")
        self.assertEqual(second.posicao_fila, 2)
        self.assertEqual(second.motivo_espera, "previous_job")
        self.assertIsNone(first.eta_min_em)
        self.assertEqual(first.eta_amostra, 0)

    def test_transicoes_produzem_metricas_reais_por_motivo(self):
        from apps.scrapers.manual_scraping import (
            atualizar_diagnostico_fila, criar_execucao, queue_wait_metrics,
        )

        now = timezone.now()
        job, created = criar_execucao(
            organization=self.org_a, usuario=self.user_a, tipo="ofertas",
        )
        self.assertTrue(created)
        type(job).objects.filter(pk=job.pk).update(
            criada_em=now - timedelta(minutes=2),
        )
        job.eventos.filter(etapa="Fila:worker_absent").update(
            criado_em=now - timedelta(minutes=2),
        )
        WorkerHeartbeat.objects.create(
            worker_id="manual:metrics", worker_type="manual", state="idle",
            heartbeat_at=now,
        )
        ResourceLease.objects.create(
            resource_key="django_chromium", owner_token="occupied",
            owner_kind="reports", acquired_at=now, heartbeat_at=now,
            expires_at=now + timedelta(seconds=90),
        )

        atualizar_diagnostico_fila()
        metrics = queue_wait_metrics(
            since=now - timedelta(minutes=10), now=timezone.now(),
            usuario=self.user_a,
        )
        rows = {row["reason"]: row for row in metrics["by_reason"]}

        self.assertGreaterEqual(rows["worker_absent"]["p50_seconds"], 119)
        self.assertEqual(rows["resource_busy"]["current"], 1)
        self.assertEqual(metrics["current_total"], 1)

    def test_execucao_estourada_nao_e_retomada(self):
        from apps.scrapers.manual_scraping import recuperar_jobs_abandonados

        job = self._job(self.user_a, self.org_a)
        type(job).objects.filter(pk=job.pk).update(
            status="running", tentativas=1,
            iniciada_em=timezone.now() - timedelta(minutes=46),
            heartbeat_em=timezone.now(),
        )
        self.assertEqual(recuperar_jobs_abandonados(), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertEqual(job.codigo_erro, "execution_timeout")

    def test_fairness_2_para_1_e_aging(self):
        from apps.scrapers.resource_control import acquire

        ResourceLease.objects.create(
            resource_key="fair", consecutive_manual=2,
            scheduled_waiting_since=timezone.now() - timedelta(seconds=10),
        )
        token, detail = acquire("fair", owner_kind="manual")
        self.assertIsNone(token)
        self.assertEqual(detail["owner_kind"], "scheduled_fairness")

        ResourceLease.objects.create(
            resource_key="aged",
            manual_waiting_since=timezone.now() - timedelta(minutes=11),
        )
        token, detail = acquire("aged", owner_kind="scheduled")
        self.assertIsNone(token)
        self.assertEqual(detail["owner_kind"], "manual_aged")

    def test_job_manual_na_fila_nao_perde_corrida_para_novo_ciclo_automatico(self):
        from apps.scrapers.resource_control import acquire, release

        self._job(self.user_a, self.org_a)
        # Mesmo depois de duas execuções manuais, este clique já está esperando:
        # a regra 2:1 não pode criar um abraço mortal em que o automático cede ao
        # queued e o manual cede ao automático ao mesmo tempo.
        ResourceLease.objects.create(
            resource_key="django_chromium", consecutive_manual=2,
            scheduled_waiting_since=timezone.now() - timedelta(seconds=10),
        )
        token, detail = acquire("django_chromium", owner_kind="scrape_rapido")

        self.assertIsNone(token)
        self.assertEqual(detail["owner_kind"], "manual_queued")

        token, detail = acquire("django_chromium", owner_kind="manual")
        self.assertTrue(token)
        self.assertEqual(detail["owner_kind"], "manual")
        self.assertTrue(release("django_chromium", token))

    def test_lease_expirado_nao_e_roubado_de_job_ainda_vivo(self):
        from apps.scrapers.resource_control import acquire

        job = self._job(self.user_a, self.org_a)
        type(job).objects.filter(pk=job.pk).update(
            status="running", lease_token="manual-live",
            heartbeat_em=timezone.now(), iniciada_em=timezone.now(),
        )
        ResourceLease.objects.create(
            resource_key="manual-live-resource", owner_token="manual-live",
            owner_kind="manual", expires_at=timezone.now() - timedelta(seconds=1),
        )

        token, detail = acquire("manual-live-resource", owner_kind="scheduled")
        self.assertIsNone(token)
        self.assertEqual(detail["owner_kind"], "manual")


class SendStateMachineTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("sender-v2", password="x")
        self.organization = ensure_personal_organization(self.user)

    def _publication(self, suffix="1"):
        return Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            canal="whatsapp", destino_id=f"group-{suffix}@g.us",
        )

    def test_transitorio_reagenda_e_incerto_nunca_reenvia(self):
        from apps.scrapers.send_pipeline import begin_transport, finish_transport

        publication = self._publication()
        self.assertEqual(publication.stage, "reserved")
        publication, attempt = begin_transport(publication)
        self.assertEqual(publication.stage, "transport_started")
        publication = finish_transport(
            publication, attempt,
            {"sucesso": False, "erro": "worker indisponível"},
            random_fn=lambda _a, maximum: maximum,
        )
        publication.refresh_from_db()
        self.assertEqual(publication.stage, "transport_queued")
        self.assertEqual(publication.status, "pendente")
        self.assertIsNotNone(publication.next_retry_at)
        self.assertEqual(publication.tentativas.get(numero=1).classification, "transient")

        publication, second = begin_transport(publication)
        finish_transport(publication, second, {
            "sucesso": False, "resultado": "incerto", "repetir": False,
        })
        publication.refresh_from_db()
        self.assertEqual(publication.stage, "uncertain")
        self.assertEqual(publication.status, "incerto")
        self.assertIsNone(publication.next_retry_at)
        with self.assertRaises(ValueError):
            begin_transport(publication)

    def test_somente_falha_explicitamente_permanente_pausa_retry(self):
        from apps.scrapers.send_pipeline import begin_transport, finish_transport

        publication, attempt = begin_transport(self._publication("permanent"))
        finish_transport(publication, attempt, {
            "sucesso": False, "erro": "destino inválido",
        })
        publication.refresh_from_db()
        self.assertEqual(publication.stage, "permanent_failed")
        self.assertEqual(publication.status, "falhou")

    def test_operation_key_e_isolada_por_publicacao_e_tenant(self):
        first = self._publication("same")
        other = get_user_model().objects.create_user("sender-other", password="x")
        other_org = ensure_personal_organization(other)
        second = Publicacao.objects.create(
            usuario=other, organization=other_org,
            canal="whatsapp", destino_id="group-same@g.us",
        )
        self.assertNotEqual(first.operation_key, second.operation_key)
        self.assertEqual(first.tentativas.count(), 0)
        self.assertTrue(first.eventos_estado.filter(stage="reserved").exists())


class SendPipelineV2QueueTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("queue-v2", password="x")
        self.organization = ensure_personal_organization(self.user)
        self.product = Produto.objects.create(
            marketplace="mercadolivre", nome="Oferta enfileirada", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-123-v2",
        )

    @override_settings(SEND_PIPELINE_V2_ENABLED=True)
    def test_request_reserva_sem_link_browser_ou_transporte(self):
        from apps.scrapers.ofertas import enviar_oferta_de_produto

        marketplace = type("Marketplace", (), {
            "build_affiliate_link": Mock(side_effect=AssertionError("browser no request")),
        })()
        sender = Mock()
        with patch("apps.scrapers.marketplaces.registry.get_marketplace",
                   return_value=marketplace), patch(
            "apps.scrapers.senders.registry.get_sender", return_value=sender,
        ):
            result = enviar_oferta_de_produto(
                self.product, "123@g.us", usuario=self.user, enqueue_only=True,
            )

        self.assertTrue(result["queued"])
        marketplace.build_affiliate_link.assert_not_called()
        sender.enviar_oferta.assert_not_called()
        publication = Publicacao.objects.get(pk=result["publicacao"].pk)
        self.assertEqual(publication.transport_state, "queued_v2")
        self.assertEqual(publication.stage, "reserved")

    def test_worker_confirma_e_remove_midia_privada(self):
        from PIL import Image
        from apps.scrapers.send_pipeline import (
            process_queued_publications, queue_publications,
        )

        publication = Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            origem="produto", produto=self.product, canal="whatsapp",
            destino_id="123@g.us",
        )
        output = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(output, format="JPEG")
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        queue_publications([publication], image_b64=encoded, mime="image/jpeg")

        def confirmed(*_args, **kwargs):
            reserved = kwargs["_reserved_publication"]
            Publicacao.objects.filter(pk=reserved.pk).update(
                status="enviado", stage="confirmed", transport_state="confirmed",
                enviada_em=timezone.now(),
            )
            return {"sucesso": True, "via": "test"}

        with patch(
            "apps.scrapers.ofertas.enviar_oferta_de_produto", side_effect=confirmed,
        ) as send:
            results = process_queued_publications(limit=1)

        self.assertEqual(results[0]["sucesso"], True)
        send.assert_called_once()
        publication.refresh_from_db()
        self.assertEqual(publication.stage, "confirmed")
        self.assertIsNone(publication.queued_media)

    @override_settings(SEND_RETRY_BASE_SECONDS=30, SEND_RETRY_MAX_SECONDS=900,
                       SEND_MAX_ATTEMPTS=5)
    def test_falha_antes_do_transporte_reagenda_sem_pausar_regra(self):
        from apps.scrapers.send_pipeline import (
            process_queued_publications, queue_publications,
        )

        publication = Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            origem="produto", produto=self.product, canal="whatsapp",
            destino_id="456@g.us",
        )
        queue_publications([publication])
        with patch(
            "apps.scrapers.ofertas.enviar_oferta_de_produto",
            return_value={
                "sucesso": False, "classe": "transitorio",
                "motivo": "worker indisponível", "causa": "worker_unavailable",
            },
        ):
            process_queued_publications(limit=1)

        publication.refresh_from_db()
        self.assertEqual(publication.status, "pendente")
        self.assertEqual(publication.stage, "transport_queued")
        self.assertEqual(publication.transport_state, "retry_wait")
        self.assertEqual(publication.attempt_count, 1)
        self.assertIsNotNone(publication.next_retry_at)
        self.assertEqual(
            publication.tentativas.get(numero=1).classification, "transient",
        )

    def test_midia_base64_invalida_e_rejeitada_antes_da_fila(self):
        from apps.scrapers.send_pipeline import queue_publications

        publication = Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            origem="produto", produto=self.product, canal="whatsapp",
            destino_id="789@g.us",
        )
        with self.assertRaisesRegex(ValueError, "base64"):
            queue_publications(
                [publication], image_b64="não-é-base64", mime="image/jpeg",
            )
        publication.refresh_from_db()
        self.assertEqual(publication.transport_state, "")

    def test_lote_misto_entre_organizacoes_e_rejeitado(self):
        from apps.scrapers.send_pipeline import queue_publications

        other = get_user_model().objects.create_user("queue-v2-other", password="x")
        other_organization = ensure_personal_organization(other)
        first = Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            origem="aviso_cupons", canal="whatsapp", destino_id="same@g.us",
        )
        second = Publicacao.objects.create(
            usuario=other, organization=other_organization,
            origem="aviso_cupons", canal="whatsapp", destino_id="same@g.us",
        )

        with self.assertRaisesRegex(ValueError, "\u00fanica organização"):
            queue_publications([first, second], batch_key="forced-collision")
        self.assertFalse(Publicacao.objects.filter(
            pk__in=[first.pk, second.pk], transport_state="queued_v2",
        ).exists())

    def test_colisao_de_chave_nao_agrega_publicacao_de_outro_tenant(self):
        from apps.scrapers.send_pipeline import _claim_next_batch, queue_publications

        other = get_user_model().objects.create_user("queue-v2-collision", password="x")
        other_organization = ensure_personal_organization(other)
        first = Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            origem="produto", produto=self.product, canal="whatsapp",
            destino_id="collision@g.us",
        )
        second = Publicacao.objects.create(
            usuario=other, organization=other_organization,
            origem="produto", produto=self.product, canal="whatsapp",
            destino_id="collision@g.us",
        )
        queue_publications([first], batch_key="forced-collision")
        queue_publications([second], batch_key="forced-collision")

        claimed = _claim_next_batch()
        self.assertEqual(len(claimed), 1)
        self.assertIn(claimed[0], {first.pk, second.pk})
        unclaimed = second if claimed[0] == first.pk else first
        unclaimed.refresh_from_db()
        self.assertEqual(unclaimed.transport_state, "queued_v2")

    def test_aviso_em_lote_usa_um_unico_transporte(self):
        from apps.scrapers.send_pipeline import (
            process_queued_publications, queue_publications,
        )

        source = FonteIngestao.objects.create(
            slug="batch-code-source", marketplace="mercadolivre", nome="Códigos",
        )
        coupons = [
            CupomNormalizado.objects.create(
                fonte=source, external_id=f"code:{code}", marketplace="mercadolivre",
                titulo=f"20% OFF {code}", codigo=code, estado="ativo",
                regras={"modo_resgate": "codigo", "valor_desconto": 20},
            )
            for code in ("LOTE20", "LOTE21")
        ]
        publications = [
            Publicacao.objects.create(
                usuario=self.user, organization=self.organization,
                origem="aviso_cupons", cupom_normalizado=coupon,
                canal="whatsapp", destino_id="batch@g.us",
            )
            for coupon in coupons
        ]
        queue_publications(publications)

        def confirmed(batch_coupons, *_args, **kwargs):
            reserved = kwargs["_reserved_publications"]
            Publicacao.objects.filter(pk__in=[row.pk for row in reserved]).update(
                status="enviado", stage="confirmed", transport_state="confirmed",
                enviada_em=timezone.now(),
            )
            self.assertEqual({c.pk for c in batch_coupons}, {c.pk for c in coupons})
            return {"sucesso": True, "via": "test", "cupons": len(batch_coupons)}

        with patch(
            "apps.scrapers.ofertas.enviar_aviso_cupons", side_effect=confirmed,
        ) as send:
            results = process_queued_publications(limit=1)

        self.assertTrue(results[0]["sucesso"])
        send.assert_called_once()
        self.assertEqual(
            Publicacao.objects.filter(stage="confirmed", destino_id="batch@g.us").count(),
            2,
        )


class OrphanPublicationDrainTests(TestCase):
    """A órfã só pode ser reagendada quando existe fila capaz de retomá-la.

    Quem drena a fila v2 é ``process_queued_publications``, e o loop de envio só o
    chama com ``SEND_PIPELINE_V2_ENABLED`` ligada. Reagendar sem consumidor deixava a
    linha 'pendente' para sempre, recasando com o reconciliador a cada ciclo e
    gravando um evento novo em cada passagem.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("orfa-drain", password="x")
        self.organization = ensure_personal_organization(self.user)
        self.product = Produto.objects.create(
            marketplace="mercadolivre", nome="Oferta órfã", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-orfa",
        )

    def _orfa(self, *, transport_state="queued_v2", stage="reserved", com_midia=False):
        publication = Publicacao.objects.create(
            usuario=self.user, organization=self.organization, origem="produto",
            produto=self.product, canal="whatsapp", destino_id="orfa@g.us",
            status="pendente", stage=stage, transport_state=transport_state,
        )
        atualizacoes = {"criada_em": timezone.now() - timedelta(hours=2)}
        if com_midia:
            from PIL import Image

            output = io.BytesIO()
            Image.new("RGB", (1, 1), "white").save(output, format="JPEG")
            atualizacoes |= {
                "queued_media": output.getvalue(), "queued_media_mime": "image/jpeg",
            }
        Publicacao.objects.filter(pk=publication.pk).update(**atualizacoes)
        return publication

    def _habilitar_rollout(self):
        OrganizationFeatureOverride.objects.create(
            organization=self.organization, feature="SEND_PIPELINE_V2_ENABLED",
            state="enabled",
        )

    @override_settings(SEND_PIPELINE_V2_ENABLED=False, PILOT_ORGANIZATION_IDS=set())
    def test_sem_consumidor_a_orfa_fecha_em_um_ciclo_e_nao_recicla(self):
        from apps.scrapers.maintenance import reconciliar_publicacoes_orfas
        from apps.scrapers.models import PublicacaoEvento

        publication = self._orfa()

        self.assertEqual(reconciliar_publicacoes_orfas(), 1)

        publication.refresh_from_db()
        self.assertEqual(publication.status, "falhou")
        self.assertEqual(publication.stage, "cancelled")
        self.assertEqual(publication.transport_state, "no_queue_consumer")
        self.assertIsNone(publication.next_retry_at)

        eventos = PublicacaoEvento.objects.filter(publicacao=publication).count()
        # O segundo ciclo não pode voltar a tocar na mesma linha nem gravar evento.
        self.assertEqual(reconciliar_publicacoes_orfas(), 0)
        self.assertEqual(
            PublicacaoEvento.objects.filter(publicacao=publication).count(), eventos,
        )
        self.assertEqual(
            PublicacaoEvento.objects.filter(
                publicacao=publication, reason_code="restart_without_queue_consumer",
            ).count(),
            1,
        )

    @override_settings(SEND_PIPELINE_V2_ENABLED=True, PILOT_ORGANIZATION_IDS=set(),
                       SEND_MAX_ATTEMPTS=3)
    def test_com_rollout_ligado_a_orfa_retoma_conta_tentativa_e_esgota(self):
        from apps.scrapers.maintenance import reconciliar_publicacoes_orfas

        self._habilitar_rollout()
        publication = self._orfa()

        for esperado in (1, 2, 3):
            self.assertEqual(reconciliar_publicacoes_orfas(), 1)
            publication.refresh_from_db()
            self.assertEqual(publication.status, "pendente")
            self.assertEqual(publication.stage, "transport_queued")
            self.assertEqual(publication.transport_state, "retry_after_restart")
            self.assertEqual(publication.attempt_count, esperado)
            self.assertIsNotNone(publication.next_retry_at)

        # A contagem de tentativas é o que torna o teto alcançável por este caminho:
        # sem incrementá-la, este ciclo nunca chegava ao ramo terminal.
        self.assertEqual(reconciliar_publicacoes_orfas(), 1)
        publication.refresh_from_db()
        self.assertEqual(publication.status, "falhou")
        self.assertEqual(publication.stage, "cancelled")
        self.assertEqual(publication.transport_state, "retry_exhausted")
        self.assertEqual(reconciliar_publicacoes_orfas(), 0)

    @override_settings(SEND_PIPELINE_V2_ENABLED=False, PILOT_ORGANIZATION_IDS=set())
    def test_desfecho_terminal_do_reconciliador_apaga_a_midia_enfileirada(self):
        from apps.scrapers.maintenance import reconciliar_publicacoes_orfas

        publication = self._orfa(com_midia=True)
        self.assertTrue(bytes(Publicacao.objects.get(pk=publication.pk).queued_media))

        reconciliar_publicacoes_orfas()

        publication.refresh_from_db()
        self.assertIn(publication.stage, {"cancelled", "uncertain"})
        self.assertFalse(bytes(publication.queued_media or b""))
        self.assertEqual(publication.queued_media_mime, "")

    @override_settings(SEND_PIPELINE_V2_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_transporte_iniciado_fica_incerto_e_tambem_libera_a_midia(self):
        from apps.scrapers.maintenance import reconciliar_publicacoes_orfas

        self._habilitar_rollout()
        publication = self._orfa(
            stage="transport_started", transport_state="processing_v2", com_midia=True,
        )

        self.assertEqual(reconciliar_publicacoes_orfas(), 1)

        publication.refresh_from_db()
        self.assertEqual(publication.status, "incerto")
        self.assertEqual(publication.stage, "uncertain")
        self.assertFalse(bytes(publication.queued_media or b""))


class UploadedMediaValidationTests(SimpleTestCase):
    def test_upload_corrompido_e_tamanho_excessivo_tem_erro_explicito(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from apps.scrapers.views import _imagem_upload_b64

        corrupted = SimpleUploadedFile("foto.jpg", b"nao-e-imagem", "image/jpeg")
        with self.assertRaisesRegex(ValueError, "imagem válida"):
            _imagem_upload_b64(corrupted)

        oversized = SimpleUploadedFile("grande.jpg", b"x" * 11, "image/jpeg")
        with self.assertRaisesRegex(ValueError, "5 MiB"):
            _imagem_upload_b64(oversized, max_bytes=10)


class CouponReadinessReasonTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("readiness", password="x")
        self.organization = ensure_personal_organization(self.user)
        self.source = FonteIngestao.objects.create(
            slug="ml-cupons-afiliados", marketplace="mercadolivre", nome="ML",
        )

    def _code(self):
        return CupomNormalizado.objects.create(
            fonte=self.source, external_id="code:scope", marketplace="mercadolivre",
            titulo="Código válido", codigo="VALIDO20", link="https://lista.mercadolivre.com.br/x",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 20},
        )

    def _ml_session(self):
        return MercadoLivreSession.objects.create(
            organization=self.organization, key_version="v1", wrapped_dek=b"wrapped",
            wrap_nonce=b"wrap", data_nonce=b"data", ciphertext=b"cipher",
            status="active", lb_readiness="ready",
        )

    @staticmethod
    @contextmanager
    def _ml(conectado=True, detalhe="", linkbuilder="ready"):
        """Instala o veredito de conexão que as TELAS renderizam.

        A projeção não pode ter uma leitura própria do estado do ML: era daí que
        vinha "tela verde, esteira parada em aguardando conexão".
        """
        from apps.scrapers.conexoes import Estado

        site = Estado(conectado, "Mercado Livre", "banco",
                      "" if conectado else "Reconecte sua conta.", detalhe, None)
        builder = Estado(linkbuilder == "ready", "Link Builder", "banco", "",
                         linkbuilder, None)
        with patch("apps.scrapers.conexoes.estado_ml", return_value=site), \
                patch("apps.scrapers.conexoes.estado_ml_linkbuilder",
                      return_value=builder):
            yield

    def test_codigo_sem_produto_e_visivel_mas_explica_sessao_e_link(self):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = self._code()
        with self._ml(conectado=False, detalhe="sem_sessao"):
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual((projection.stage, projection.category, projection.reason_code),
                         ("waiting_link", "no_session", "ml_session_missing"))

        self._ml_session()
        with self._ml():
            projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.reason_code, "affiliate_link_pending")
        LinkAfiliadoCupomUsuario.objects.create(
            usuario=self.user, cupom=coupon,
            url_origem=coupon.link, link_afiliado="https://meli.la/coupon-ready",
            afiliado_ok=True, estado="pronto", verificado_ok=True,
            verificado_em=timezone.now(), url_canonica="https://meli.la/coupon-ready",
        )
        with self._ml():
            projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.stage, "ready")

    def test_percentual_com_teto_irrisorio_e_rejeitado_como_lixo(self):
        """'50% OFF' com teto de R$1 é dado real do ML, não bug de parser — e é
        lixo: o comprador nunca leva mais que o teto. Achado em produção em
        03/09/2026 (cupom "Glamour.div", 50% OFF, desconto_maximo=1.0)."""
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        cupom = CupomNormalizado.objects.create(
            fonte=self.source, external_id="campanha:teto-irrisorio",
            marketplace="mercadolivre", titulo="50% OFF Em produtos de Glamour.div",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 50.0,
                    "desconto_maximo": 1.0, "valor_minimo": 1.0,
                    "modo_resgate": "ativacao"},
        )
        with self._ml():
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=cupom, usuario=self.user)
        self.assertEqual(
            (projection.stage, projection.category, projection.reason_code),
            ("discarded", "invalid", "desconto_irrelevante"),
        )

    def test_percentual_com_teto_relevante_nao_e_afetado(self):
        """Confirma que o piso não vira gatilho de falso positivo: um cupom com
        teto de verdade (acima do piso configurado) segue o caminho normal."""
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        cupom = CupomNormalizado.objects.create(
            fonte=self.source, external_id="campanha:teto-bom",
            marketplace="mercadolivre", titulo="30% OFF loja boa",
            regras={"tipo_desconto": "porcentagem", "valor_desconto": 30.0,
                    "desconto_maximo": 200.0, "valor_minimo": 100.0,
                    "modo_resgate": "ativacao"},
        )
        with self._ml():
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=cupom, usuario=self.user)
        self.assertNotEqual(projection.reason_code, "desconto_irrelevante")

    def test_reconciliacao_em_lote_nao_cresce_queries_por_cupom(self):
        """Quarenta alegações não podem virar quarenta EXISTS/transações."""
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        comunidade = FonteIngestao.objects.create(
            slug="telegram-publico", marketplace="mercadolivre", nome="Telegram",
        )
        CupomNormalizado.objects.bulk_create([
            CupomNormalizado(
                fonte=comunidade, external_id=f"tg:batch:{indice}",
                marketplace="mercadolivre", titulo=f"Cupom Telegram {indice}",
                codigo=f"TELE{indice:04d}",
                audience_scope=("organization" if indice >= 20 else "public"),
                organization=(self.organization if indice >= 20 else None),
                regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                        "valor_desconto": 10},
            )
            for indice in range(40)
        ])
        with self._ml(conectado=False, detalhe="sem_sessao"):
            projetar_disponibilidade_cupons(self.user)

        with self._ml(conectado=False, detalhe="sem_sessao"), \
                CaptureQueriesContext(connection) as queries:
            resultado = projetar_disponibilidade_cupons(self.user)

        self.assertEqual(resultado["total"], 40)
        self.assertLessEqual(len(queries), 25)
        self.assertEqual(
            CupomDisponibilidade.objects.filter(
                usuario=self.user, reason_code="community_uncorroborated",
            ).count(),
            40,
        )

    def test_reprojecao_identica_nao_regrava_disponibilidade(self):
        """Tick sem mudança não gera WAL nem mascara uma projeção parada."""
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = self._code()
        with self._ml(conectado=False, detalhe="sem_sessao"):
            projetar_disponibilidade_cupons(self.user)

        with self._ml(conectado=False, detalhe="sem_sessao"), \
                CaptureQueriesContext(connection) as queries:
            projetar_disponibilidade_cupons(self.user)

        availability_table = CupomDisponibilidade._meta.db_table
        writes = [
            query["sql"] for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith("UPDATE")
            and availability_table in query["sql"]
        ]
        self.assertEqual(writes, [])
        self.assertEqual(
            CupomDisponibilidade.objects.get(cupom=coupon).reason_code,
            "ml_session_missing",
        )

    def test_cache_verificado_continua_ready_sem_sessao_e_cache_vencido_nao(self):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = self._code()
        link = LinkAfiliadoCupomUsuario.objects.create(
            usuario=self.user, cupom=coupon, url_origem=coupon.link,
            link_afiliado="https://meli.la/cache", afiliado_ok=True,
            estado="pronto", verificado_ok=True, verificado_em=timezone.now(),
            url_canonica="https://meli.la/cache",
        )
        with self._ml(conectado=False, detalhe="sem_sessao"):
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(projection.stage, "ready")

        link.verificado_em = timezone.now() - timedelta(days=8)
        link.save(update_fields=["verificado_em"])
        with self._ml(conectado=False, detalhe="sem_sessao"):
            projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(
            (projection.stage, projection.reason_code),
            ("waiting_link", "affiliate_link_expired"),
        )

    @override_settings(ML_CUPONS_ATIVACAO_ENABLED=True)
    def test_ativacao_com_link_da_relacao_verificado_fica_ready_sem_sessao(self):
        from apps.scrapers.coupon_products import atualizar_chave_cupom
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        source, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-web",
            defaults={"marketplace": "mercadolivre", "nome": "ML web"},
        )
        coupon = CupomNormalizado.objects.create(
            fonte=source, external_id="campanha:CACHE1", marketplace="mercadolivre",
            titulo="15% em selecionados", redemption_mode="activation",
            scope_type="container",
            link="https://lista.mercadolivre.com.br/_Container_CACHE1",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 15,
                    "container_url":
                        "https://lista.mercadolivre.com.br/_Container_CACHE1"},
        )
        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Produto", origem="cupom",
            preco_sem_desconto=100, preco_com_cupom=90,
            link_produto="https://produto.mercadolivre.com.br/MLB-100000",
            imagem_url="https://img.example/item.jpg", estado="ativo",
        )
        relation = ProdutoCupom.objects.create(
            produto=product, cupom=coupon, status="confirmado",
            activation_key="CACHE1", verificado_em=timezone.now(),
            preco_original=100, preco_atual=90, preco_final=85,
        )
        CupomPreparacao.objects.create(
            cupom=coupon, usuario=None, status="pronto",
            produtos_chave=atualizar_chave_cupom(coupon),
            verificado_em=timezone.now(),
        )
        LinkAfiliadoProdutoCupomUsuario.objects.create(
            usuario=self.user, relacao=relation,
            url_isca=(product.link_produto + "?coupon_campaign_id=CACHE1"),
            link_afiliado="https://meli.la/relation-cache", estado="pronto",
            verificado_ok=True, verificado_em=timezone.now(),
            url_canonica="https://meli.la/relation-cache",
        )

        with self._ml(conectado=False, detalhe="sem_sessao"):
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(projection.stage, "ready")

    def test_estado_da_conexao_vem_da_mesma_fonte_que_as_telas(self):
        """Sessão instável (mas viva) não pode parar a esteira inteira.

        `MercadoLivreSession.status` guarda o veredito BRUTO da última sonda; o ML
        responde 302→login a IP de datacenter sem que a sessão tenha morrido, e é
        por isso que `conexoes.estado_ml` aplica a política de acúmulo antes de
        declarar desconexão. Enquanto a projeção lia a coluna crua, a tela de
        conexão ficava verde e todo o funil parava em "aguardando conexão".
        """
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = self._code()
        session = self._ml_session()
        session.last_probe_result = "suspeito"
        session.probe_failures = 1
        session.save(update_fields=["last_probe_result", "probe_failures"])

        with self._ml(conectado=True):
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(projection.reason_code, "affiliate_link_pending")

        # E o inverso: veredito de desconexão da MESMA fonte para o funil.
        with self._ml(conectado=False, detalhe="expirado"):
            projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual((projection.stage, projection.reason_code),
                         ("waiting_link", "ml_session_expired"))

    def test_sessao_do_catalogo_caida_pede_reconexao_em_vez_de_culpar_o_cupom(self):
        """A esteira compartilhada usa a sessão de sistema. Quando ela cai, o
        preparo não observa NADA — e a tela dizia "nenhum produto comprovado",
        que manda procurar defeito no cupom em vez de reconectar o ML."""
        from apps.scrapers.coupon_products import ERRO_SESSAO_ML
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = CupomNormalizado.objects.create(
            fonte=self.source, external_id="campanha:77", marketplace="mercadolivre",
            titulo="25% em selecionados", codigo="",
            link="https://lista.mercadolivre.com.br/_Container_77",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 25,
                    "container_url":
                        "https://lista.mercadolivre.com.br/_Container_77"},
        )
        CupomPreparacao.objects.create(
            cupom=coupon, usuario=None, status="erro", erro=ERRO_SESSAO_ML,
            verificado_em=timezone.now(),
        )
        self._ml_session()
        with self._ml(conectado=True):
            projetar_disponibilidade_cupons(self.user)

        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(
            (projection.stage, projection.category, projection.reason_code),
            ("eligible", "no_session", "ml_catalog_session_expired"),
        )
        self.assertIn("reconecte", projection.safe_detail.casefold())

    def test_link_builder_pedindo_login_bloqueia_com_motivo_proprio(self):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = self._code()
        self._ml_session()
        with self._ml(conectado=True, linkbuilder="login_required"):
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(projection.reason_code, "ml_linkbuilder_login_required")

    def test_not_found_so_muda_ausente_e_grava_transicao_uma_vez(self):
        from apps.scrapers.coupon_readiness import (
            marcar_ausentes_execucao_saudavel,
            projetar_disponibilidade_cupons,
        )
        from apps.scrapers.models import CupomDisponibilidadeEvento

        present = self._code()
        absent = CupomNormalizado.objects.create(
            fonte=self.source, external_id="code:absent",
            marketplace="mercadolivre", titulo="Código anterior",
            codigo="ANTIGO15", link="https://lista.mercadolivre.com.br/y",
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 15},
        )
        projetar_disponibilidade_cupons(self.user)
        absent_projection = CupomDisponibilidade.objects.get(
            cupom=absent, usuario=self.user,
        )
        events_before = CupomDisponibilidadeEvento.objects.filter(
            disponibilidade=absent_projection,
        ).count()

        changed = marcar_ausentes_execucao_saudavel(
            self.source, [present.external_id],
        )

        self.assertEqual(changed, 1)
        absent_projection.refresh_from_db()
        self.assertEqual(
            (
                absent_projection.stage, absent_projection.category,
                absent_projection.reason_code,
            ),
            ("discarded", "not_found", "not_found_healthy_run"),
        )
        self.assertNotEqual(
            CupomDisponibilidade.objects.get(cupom=present, usuario=self.user).category,
            "not_found",
        )
        event = CupomDisponibilidadeEvento.objects.filter(
            disponibilidade=absent_projection,
        ).latest("pk")
        self.assertEqual((event.to_stage, event.category), ("discarded", "not_found"))
        self.assertEqual(
            marcar_ausentes_execucao_saudavel(self.source, [present.external_id]),
            0,
        )
        self.assertEqual(
            CupomDisponibilidadeEvento.objects.filter(
                disponibilidade=absent_projection,
            ).count(),
            events_before + 1,
        )
        # O recálculo normal do fim do pipeline não pode apagar a ausência.
        projetar_disponibilidade_cupons(self.user)
        absent_projection.refresh_from_db()
        self.assertEqual(absent_projection.category, "not_found")

        # Quando a fonte voltar a observar o item, a mesma chave canônica é
        # reabilitada sem apagar catálogo ou histórico.
        from apps.scrapers.sources.persistence import record_coupon_observation
        record_coupon_observation(absent, outcome="accepted")
        projetar_disponibilidade_cupons(self.user)
        absent_projection.refresh_from_db()
        self.assertNotEqual(absent_projection.category, "not_found")

    def test_pipeline_so_materializa_ausencia_em_inventario_completo_saudavel(self):
        from apps.scrapers.coupon_pipeline import _coletar_adaptador, _metricas_vazias
        from apps.scrapers.sources.base import IngestedItem

        item = IngestedItem(
            external_id="new-code", marketplace="mercadolivre",
            source=self.source.slug, kind="coupon", canonical_url="",
            title="Novo", coupon_code="NOVO10",
        )
        healthy = {
            "status": "ok", "offers": [], "coupons": [item],
            "health": "healthy", "metrics": {"complete": True},
        }
        with patch("apps.scrapers.sources.run_source", return_value=healthy), \
                patch(
                    "apps.scrapers.sources.persistence.persist_items",
                    return_value={"offers": 0, "coupons": 1},
                ), patch(
                    "apps.scrapers.coupon_readiness.marcar_ausentes_execucao_saudavel",
                ) as mark_absent:
            _coletar_adaptador(self.source.slug, _metricas_vazias())
        mark_absent.assert_called_once_with(self.source, ["new-code"])

        partial = {**healthy, "health": "partial", "metrics": {"complete": False}}
        with patch("apps.scrapers.sources.run_source", return_value=partial), \
                patch(
                    "apps.scrapers.sources.persistence.persist_items",
                    return_value={"offers": 0, "coupons": 1},
                ), patch(
                    "apps.scrapers.coupon_readiness.marcar_ausentes_execucao_saudavel",
                ) as mark_absent:
            _coletar_adaptador(self.source.slug, _metricas_vazias())
        mark_absent.assert_not_called()

        # A Amazon coleta ofertas+cupons num snapshot e persiste fora deste helper.
        # A chamada intermediária ``items=()`` jamais representa inventário vazio.
        with patch("apps.scrapers.sources.run_source", return_value=healthy), \
                patch(
                    "apps.scrapers.coupon_readiness.marcar_ausentes_execucao_saudavel",
                ) as mark_absent:
            _coletar_adaptador(
                self.source.slug, _metricas_vazias(), items=(),
            )
        mark_absent.assert_not_called()

    def test_ausencia_de_item_com_vigencia_encerrada_e_expiracao_nao_not_found(self):
        from apps.scrapers.coupon_readiness import (
            marcar_ausentes_execucao_saudavel,
            projetar_disponibilidade_cupons,
        )

        present = self._code()
        expired = CupomNormalizado.objects.create(
            fonte=self.source, external_id="code:expired",
            marketplace="mercadolivre", titulo="Código com vigência",
            codigo="VENCEU10", link="https://lista.mercadolivre.com.br/expired",
            validade=timezone.now() + timedelta(days=1),
            regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                    "valor_desconto": 10},
        )
        projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(
            cupom=expired, usuario=self.user,
        )
        expired.validade = timezone.now() - timedelta(seconds=1)
        expired.save(update_fields=["validade"])

        self.assertEqual(
            marcar_ausentes_execucao_saudavel(
                self.source, [present.external_id],
            ),
            1,
        )
        projection.refresh_from_db()
        self.assertEqual(
            (projection.stage, projection.category, projection.reason_code),
            ("discarded", "rejected", "expired"),
        )

    @override_settings(ML_CUPONS_ATIVACAO_ENABLED=False)
    def test_ativacao_kill_switch_tem_motivo_exato(self):
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = CupomNormalizado.objects.create(
            fonte=self.source, external_id="campanha:off", marketplace="mercadolivre",
            titulo="Ativação", codigo="", link="https://lista.mercadolivre.com.br/_Container_1",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 15,
                    "container_url": "https://lista.mercadolivre.com.br/_Container_1"},
        )
        projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(projection.stage, "discarded")
        self.assertEqual(projection.reason_code, "feature_global_kill_switch")

    @override_settings(ML_CUPONS_ATIVACAO_ENABLED=True, PILOT_ORGANIZATION_IDS=set())
    def test_ativacao_exige_override_sessao_produto_preco_e_link(self):
        from apps.scrapers.coupon_products import atualizar_chave_cupom
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons
        from apps.scrapers.models import CupomPreparacao

        OrganizationFeatureOverride.objects.create(
            organization=self.organization, feature="ML_CUPONS_ATIVACAO_ENABLED",
            state="enabled",
        )
        coupon = CupomNormalizado.objects.create(
            fonte=self.source, external_id="campanha:ready", marketplace="mercadolivre",
            titulo="Ativação 15%", codigo="",
            link="https://lista.mercadolivre.com.br/_Container_2",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 15,
                    "container_url": "https://lista.mercadolivre.com.br/_Container_2"},
        )
        with self._ml(conectado=False, detalhe="sem_sessao"):
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(projection.reason_code, "ml_session_missing")

        self._ml_session()
        conectado = self._ml()
        conectado.__enter__()
        self.addCleanup(conectado.__exit__, None, None, None)
        projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.reason_code, "preparation_pending")

        product = Produto.objects.create(
            marketplace="mercadolivre", nome="Produto comprovado", campanha_id="ready",
            preco_sem_desconto=100, preco_com_cupom=90,
            link_produto="https://produto.mercadolivre.com.br/MLB-1",
            imagem_url="https://img.example/item.jpg",
        )
        ProdutoCupom.objects.create(
            produto=product, cupom=coupon, status="confirmado",
            verificado_em=timezone.now(), preco_original=100,
            preco_atual=90, preco_final=75,
        )
        key = atualizar_chave_cupom(coupon)
        CupomPreparacao.objects.create(
            cupom=coupon, usuario=None, status="pronto",
            produtos_chave=key, verificado_em=timezone.now(),
        )
        projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.stage, "prepared")

        link = LinkAfiliadoUsuario.objects.create(
            usuario=self.user, produto=product,
            url_isca=product.link_produto, link_afiliado="https://meli.la/item-ready",
            afiliado_ok=True, verificado_ok=None, estado="pronto",
        )
        projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.stage, "waiting_link")
        self.assertEqual(projection.reason_code, "link_verification_pending")

        link.verificado_ok = False
        link.verificacao_motivo = "Destino não corresponde ao produto."
        link.save(update_fields=["verificado_ok", "verificacao_motivo"])
        projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.stage, "waiting_link")
        self.assertEqual(projection.reason_code, "affiliate_link_rejected")

        link.verificado_ok = True
        link.url_canonica = "https://meli.la/item-ready"
        link.save(update_fields=["verificado_ok", "url_canonica"])
        projetar_disponibilidade_cupons(self.user)
        projection.refresh_from_db()
        self.assertEqual(projection.stage, "ready")

    def test_campanha_da_org_sistema_nao_vaza_pra_outra_conta(self):
        """Produção: só a org em ML_SYSTEM_ORGANIZATION_ID vê campanhas autenticadas.

        lules é essa org. teste1/luiza projetam ~140 cupons públicos; lules ~2800.
        """
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        fonte, _ = FonteIngestao.objects.get_or_create(
            slug="mercadolivre-campanhas",
            defaults={"marketplace": "mercadolivre", "nome": "Campanhas"},
        )
        coupon = CupomNormalizado.objects.create(
            fonte=fonte, external_id="campanha:lules-only",
            marketplace="mercadolivre", titulo="Só a org sistema",
            codigo="", audience_scope="organization",
            organization=self.organization, owner=None,
            link="https://lista.mercadolivre.com.br/_Container_sys",
            regras={"modo_resgate": "ativacao", "tipo_desconto": "porcentagem",
                    "valor_desconto": 10,
                    "container_url": "https://lista.mercadolivre.com.br/_Container_sys"},
        )
        outsider = get_user_model().objects.create_user("nao-lules", password="x")
        ensure_personal_organization(outsider)

        with self._ml():
            self.assertEqual(projetar_disponibilidade_cupons(self.user)["total"], 1)
            self.assertTrue(
                CupomDisponibilidade.objects.filter(
                    usuario=self.user, cupom=coupon,
                ).exists(),
            )
            self.assertEqual(projetar_disponibilidade_cupons(outsider)["total"], 0)
            self.assertFalse(
                CupomDisponibilidade.objects.filter(
                    usuario=outsider, cupom=coupon,
                ).exists(),
            )

    def test_mudanca_de_publico_para_organizacao_encerra_projecao_antiga(self):
        """Uma projeção criada antes da restrição não pode sobreviver entregável."""
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = self._code()
        outsider = get_user_model().objects.create_user("escopo-antigo", password="x")
        outsider_org = ensure_personal_organization(outsider)
        with self._ml():
            projetar_disponibilidade_cupons(outsider)
        projection = CupomDisponibilidade.objects.get(
            usuario=outsider, organization=outsider_org, cupom=coupon,
        )
        eventos_antes = CupomDisponibilidadeEvento.objects.filter(
            disponibilidade=projection,
        ).count()

        coupon.audience_scope = "organization"
        coupon.organization = self.organization
        coupon.save(update_fields=["audience_scope", "organization"])
        with self._ml():
            resultado = projetar_disponibilidade_cupons(outsider)

        projection.refresh_from_db()
        self.assertEqual(resultado["total"], 0)
        self.assertEqual(
            (projection.stage, projection.category, projection.reason_code),
            ("discarded", "rejected", "coupon_out_of_scope"),
        )
        self.assertEqual(
            CupomDisponibilidadeEvento.objects.filter(
                disponibilidade=projection,
            ).count(),
            eventos_antes + 1,
        )
        # Reconciliação idempotente: não reescreve nem duplica o evento.
        with self._ml():
            projetar_disponibilidade_cupons(outsider)
        self.assertEqual(
            CupomDisponibilidadeEvento.objects.filter(
                disponibilidade=projection,
            ).count(),
            eventos_antes + 1,
        )

    def test_cupom_de_codigo_do_ml_tem_quem_prepare_o_link(self):
        """Impasse fechado: o cupom aparecia na tela e nunca ficava disponível.

        `_codigo` só promove a `ready` com um LinkAfiliadoCupomUsuario verificado, e
        a única rotina que gravava essa linha era `enviar_cupom` — que a tela só
        oferece quando o cupom JÁ está `ready`. Nenhum worker fechava o ciclo, então
        todo cupom de código do ML ficava permanentemente "aguardando link".
        """
        from apps.scrapers.coupon_pipeline import afiliar_cupons_de_codigo
        from apps.scrapers.coupon_readiness import projetar_disponibilidade_cupons

        coupon = self._code()
        self._ml_session()

        def _resolver(cupom, usuario):
            LinkAfiliadoCupomUsuario.objects.create(
                usuario=usuario, cupom=cupom, url_origem=cupom.link,
                link_afiliado="https://meli.la/gerado", afiliado_ok=True,
                estado="pronto", verificado_ok=True,
                verificado_em=timezone.now(), url_canonica="https://meli.la/gerado",
            )
            return {"sucesso": True, "link": "https://meli.la/gerado"}

        with self._ml(), \
                patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom", _resolver):
            resultado = afiliar_cupons_de_codigo(self.user, [coupon])
        self.assertEqual(resultado["gerados"], 1)

        with self._ml():
            projetar_disponibilidade_cupons(self.user)
        projection = CupomDisponibilidade.objects.get(cupom=coupon, usuario=self.user)
        self.assertEqual(projection.stage, "ready")

    def test_afiliacao_de_codigo_para_o_lote_quando_a_sessao_cai(self):
        """Sessão morta não pode custar um Chromium por cupom restante."""
        from apps.scrapers.coupon_pipeline import afiliar_cupons_de_codigo

        cupons = [self._code()]
        for indice in range(3):
            cupons.append(CupomNormalizado.objects.create(
                fonte=self.source, external_id=f"code:{indice}",
                marketplace="mercadolivre", titulo=f"Código {indice}",
                codigo=f"CODIGO{indice}0",
                link="https://lista.mercadolivre.com.br/z",
                regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                        "valor_desconto": 10},
            ))
        chamadas = []

        def _resolver(cupom, _usuario):
            chamadas.append(cupom.pk)
            return {"sucesso": False, "motivo": "Sessão expirada.",
                    "precisa_login_ml": True}

        # Conectado no início: é o caso que este teste cobre — a sessão cai DURANTE
        # o lote. A conta já desconectada antes de começar tem portão próprio
        # (`ml_conectado`), coberto no teste seguinte.
        with patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True), \
                patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom", _resolver):
            resultado = afiliar_cupons_de_codigo(self.user, cupons)
        self.assertEqual(len(chamadas), 1)
        self.assertEqual(resultado["gerados"], 0)

    def test_link_builder_indisponivel_para_lote_sem_derrubar_sessao(self):
        from apps.scrapers.coupon_pipeline import afiliar_cupons_de_codigo

        cupons = [self._code()]
        for indice in range(2):
            cupons.append(CupomNormalizado.objects.create(
                fonte=self.source, external_id=f"temporario:{indice}",
                marketplace="mercadolivre", titulo=f"Temporário {indice}",
                codigo=f"TEMPORARIO{indice}0",
                link="https://lista.mercadolivre.com.br/z",
                regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                        "valor_desconto": 10},
            ))
        chamadas = []

        def _resolver(cupom, _usuario):
            chamadas.append(cupom.pk)
            return {
                "sucesso": False,
                "motivo": "Link Builder temporariamente indisponível.",
                "precisa_login_ml": False,
                "indisponivel_ml": True,
            }

        with patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=True), \
                patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom", _resolver):
            resultado = afiliar_cupons_de_codigo(self.user, cupons)

        self.assertEqual(len(chamadas), 1)
        self.assertEqual(resultado["falhas"], 1)

    def test_navegador_ocupado_e_fila_e_nao_falha_de_preparo(self):
        """Contenção de capacidade não pode virar avaria na tela.

        `BrowserResourceUnavailable` caía no `except Exception` de `preparar_cupom`
        e virava `status="erro"` + 30 min de castigo: em produção, 188 cupons
        marcados como `preparation_failed` — a tela mandava procurar um defeito que
        não existia, quando bastava esperar o Chromium.
        """
        from apps.scrapers.carga import BrowserResourceUnavailable
        from apps.scrapers.coupon_products import (
            ERRO_CAPACIDADE_BROWSER, preparar_cupom,
        )
        from apps.scrapers.models import CupomPreparacao

        cupom = self._code()
        with patch("apps.scrapers.coupon_products._base_produtos",
                   side_effect=BrowserResourceUnavailable("ocupado")):
            self.assertEqual(preparar_cupom(cupom, usuario=self.user), [])

        preparo = CupomPreparacao.objects.get(cupom=cupom)
        self.assertEqual(preparo.status, "pendente")
        self.assertEqual(preparo.erro, ERRO_CAPACIDADE_BROWSER)
        # Espera curta: nada foi julgado sobre o cupom.
        self.assertLess(
            preparo.proxima_tentativa - timezone.now(), timedelta(minutes=10))

    def test_afiliacao_de_codigo_nem_comeca_sem_sessao_ml(self):
        """Conta desconectada não entra na fila do Link Builder.

        Eram 3 das 4 contas de produção gastando um lugar na disputa pelo único
        Chromium a cada ciclo — na frente de quem estava conectado.
        """
        from apps.scrapers.coupon_pipeline import afiliar_cupons_de_codigo

        cupons = [self._code()]

        def _resolver(_cupom, _usuario):
            raise AssertionError("não deve tentar gerar link sem sessão")

        with patch("apps.scrapers.monitor_conexao.ml_conectado", return_value=False), \
                patch("apps.scrapers.ofertas.resolver_link_afiliado_cupom", _resolver):
            resultado = afiliar_cupons_de_codigo(self.user, cupons)
        self.assertEqual(resultado["gerados"], 0)
        self.assertEqual(resultado["pendentes"], 1)

    def test_tela_de_promocoes_nao_materializa_projecao(self):
        """A tela é somente leitura mesmo com a conta zerada.

        O backfill dentro da request não commitava: OrganizationContextMiddleware
        envolve a response inteira num atomic() para instalar o escopo RLS, então
        o atomic() por cupom virava savepoint, o laço de milhares de cupons batia
        no lock_timeout contra o worker e tudo voltava atrás -- 500 em loop. Quem
        materializa é o worker `cupons` e o comando backfill_disponibilidade_cupons.
        """
        self._code()
        self.assertFalse(CupomDisponibilidade.objects.filter(usuario=self.user).exists())

        self.user.perfil.marcar_verificado()
        self.client.force_login(self.user)
        response = self.client.get("/scrapers/top/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            CupomDisponibilidade.objects.filter(usuario=self.user).exists(),
            "A tela de Promoções voltou a escrever projeção dentro da request.",
        )

    def test_backfill_apaga_orfas_em_lotes_e_reprojeta(self):
        """DELETE único de órfãs+eventos estoura statement_timeout em produção.

        Com lote pequeno o comando termina e a conta viva continua projetada.
        """
        vivo = self._code()
        orfaos = [
            CupomNormalizado.objects.create(
                fonte=self.source, external_id=f"code:dead{i}",
                marketplace="mercadolivre", titulo="Expirado",
                codigo=f"DEAD{i}", estado="expirado",
                regras={"modo_resgate": "codigo", "tipo_desconto": "porcentagem",
                        "valor_desconto": 10},
            )
            for i in range(3)
        ]
        for cupom in [vivo, *orfaos]:
            proj = CupomDisponibilidade.objects.create(
                organization=self.organization, usuario=self.user, cupom=cupom,
                channel="whatsapp", use_mode="code_notice", stage="collected",
            )
            CupomDisponibilidadeEvento.objects.create(
                organization=self.organization, disponibilidade=proj,
                from_stage="", to_stage="collected",
                marketplace="mercadolivre", source=self.source.slug,
                use_mode="code_notice",
            )
        saida = io.StringIO()
        with patch(
            "apps.scrapers.management.commands.backfill_disponibilidade_cupons.ORPHAN_BATCH",
            1,
        ), self._ml():
            call_command(
                "backfill_disponibilidade_cupons", "--todas", stdout=saida,
            )
        self.assertFalse(
            CupomDisponibilidade.objects.filter(cupom__in=orfaos).exists(),
        )
        self.assertTrue(
            CupomDisponibilidade.objects.filter(
                cupom=vivo, usuario=self.user,
            ).exists(),
        )
        self.assertIn("órfãs lote=1", saida.getvalue())


class CupomLixoTests(SimpleTestCase):
    """`cupom_e_lixo` decide pelo benefício REAL, não pelo percentual anunciado."""

    def test_percentual_com_teto_baixo_e_lixo(self):
        from apps.scrapers.coupon_rules import cupom_e_lixo
        self.assertTrue(cupom_e_lixo(
            {"tipo_desconto": "porcentagem", "valor_desconto": 50.0, "desconto_maximo": 1.0}))

    def test_percentual_com_teto_alto_nao_e_lixo(self):
        from apps.scrapers.coupon_rules import cupom_e_lixo
        self.assertFalse(cupom_e_lixo(
            {"tipo_desconto": "porcentagem", "valor_desconto": 30.0, "desconto_maximo": 200.0}))

    def test_percentual_sem_teto_nao_e_julgavel_e_passa(self):
        """Sem teto conhecido, a incerteza não pode virar rejeição."""
        from apps.scrapers.coupon_rules import cupom_e_lixo
        self.assertFalse(cupom_e_lixo(
            {"tipo_desconto": "porcentagem", "valor_desconto": 50.0}))

    def test_fixo_baixo_e_lixo(self):
        from apps.scrapers.coupon_rules import cupom_e_lixo
        self.assertTrue(cupom_e_lixo({"tipo_desconto": "fixo", "valor_desconto": 2.0}))

    def test_fixo_alto_nao_e_lixo(self):
        from apps.scrapers.coupon_rules import cupom_e_lixo
        self.assertFalse(cupom_e_lixo({"tipo_desconto": "fixo", "valor_desconto": 50.0}))

    def test_piso_e_configuravel_por_setting(self):
        from apps.scrapers.coupon_rules import cupom_e_lixo
        regras = {"tipo_desconto": "fixo", "valor_desconto": 12.0}
        with override_settings(COUPON_VALOR_MINIMO_RELEVANTE_REAIS=5):
            self.assertFalse(cupom_e_lixo(regras))
        with override_settings(COUPON_VALOR_MINIMO_RELEVANTE_REAIS=20):
            self.assertTrue(cupom_e_lixo(regras))


class MarketplaceParserResilienceTests(SimpleTestCase):
    def test_ml_aceita_simbolo_renomeado_e_json_ssr_deslocado(self):
        from apps.scrapers.sources.ml_public_coupons import _extrair_cupons_html
        from apps.scrapers.scraper_mercadolivre.cupons_codigo_scraper import (
            _cupons_do_payload, _payload_nordic,
        )

        renamed = '<script>const availableVouchers = [{"nome":"NOVO20","dia_fim":"31/12/2099"}];</script>'
        coupons, diagnostic = _extrair_cupons_html(renamed)
        self.assertEqual(coupons[0]["nome"], "NOVO20")
        self.assertEqual(diagnostic["parser"], "schema-array")

        payload = {"moved": {"again": [{
            "campaign_id": "123", "title": {"text": "15% off"},
            "action": {"url": "https://lista.mercadolivre.com.br/_Container_1"},
        }]}}
        html = f'<script type="application/json">{json.dumps(payload)}</script>'
        parsed = _payload_nordic(html)
        self.assertEqual(_cupons_do_payload(parsed)[0]["campaign_id"], "123")

    def test_ml_zero_explicito_e_saudavel_schema_ilegivel_e_degradado(self):
        from apps.scrapers.sources.ml_public_coupons import _extrair_cupons_html

        coupons, diagnostic = _extrair_cupons_html("<script>const COUPONS = [];</script>")
        self.assertEqual(coupons, [])
        self.assertTrue(diagnostic["schema_ok"])
        self.assertTrue(diagnostic["explicit_empty"])

        coupons, diagnostic = _extrair_cupons_html("<html>layout alterado</html>")
        self.assertEqual(coupons, [])
        self.assertFalse(diagnostic["schema_ok"])

        coupons, diagnostic = _extrair_cupons_html(
            "<script>const banners = [];</script>"
        )
        self.assertEqual(coupons, [])
        self.assertFalse(diagnostic["schema_ok"])

        coupons, diagnostic = _extrair_cupons_html(
            "<script>const availableCoupons = [];</script>"
        )
        self.assertEqual(coupons, [])
        self.assertTrue(diagnostic["schema_ok"])
        self.assertTrue(diagnostic["explicit_empty"])

    @override_settings(
        ML_CUPONS_AFILIADOS_URL="https://public.example/complete-inventory",
    )
    def test_ml_so_autoriza_inventario_completo_com_schema_comprovado(self):
        from apps.scrapers.sources.ml_public_coupons import MLPublicCouponsSource

        response = Mock(
            status_code=200, headers={},
            text='<script>const availableCoupons = [];</script>',
        )
        response.raise_for_status = Mock()
        source = MLPublicCouponsSource()
        with patch(
            "apps.scrapers.sources.ml_public_coupons.requests.get",
            return_value=response,
        ):
            self.assertEqual(source._cupons_brutos(), [])
        self.assertEqual(source.last_health, "healthy_empty")
        self.assertTrue(source.last_metrics["complete"])
        self.assertEqual(len(source.last_metrics["schema_fingerprint"]), 64)

    @override_settings(
        ML_CUPONS_AFILIADOS_URL="https://public.example/cupons?token=nao-persistir#fragmento",
    )
    def test_ml_normaliza_aliases_booleano_e_remove_query_da_evidencia(self):
        from apps.scrapers.sources.ml_public_coupons import MLPublicCouponsSource

        source = MLPublicCouponsSource()
        source._cupons_brutos = lambda: [{
            "codigo": "ALT20",
            "validade": "2099-12-31",
            "inicio": "2099-01-01",
            "discount": "20%",
            "discount_value": 20,
            "minimum_purchase": 49,
            "max_discount": 30,
            "site_wide": "false",
            "scope_id": "container-alt",
            "scope_url": "https://lista.mercadolivre.com.br/_Container_alt",
        }]

        item = list(source.discover_coupons())[0]

        self.assertEqual(item.coupon_code, "ALT20")
        self.assertFalse(item.coupon_rules["is_mar_aberto"])
        self.assertEqual(item.coupon_rules["container_name"], "container-alt")
        self.assertEqual(item.valid_until.date(), date(2099, 12, 31))
        self.assertEqual(item.starts_at.date(), date(2099, 1, 1))
        self.assertEqual(item.evidence["url"], "https://public.example/cupons")

    def test_ml_rejeita_vigencia_ilegivel_e_container_nao_publico(self):
        from apps.scrapers.sources.ml_public_coupons import MLPublicCouponsSource

        source = MLPublicCouponsSource()
        source._cupons_brutos = lambda: [
            {"nome": "SEM-DATA", "dia_fim": "amanhã", "is_mar_aberto": True},
            {"nome": "URL-RUIM", "dia_fim": "31/12/2099",
             "container_url": "https://example.invalid/private", "container_name": "x"},
        ]
        self.assertEqual(list(source.discover_coupons()), [])
        self.assertEqual(source.last_metrics["accepted"], 0)
        self.assertEqual(source.last_metrics["rejected"], 2)
        self.assertEqual(source.last_metrics["rejections"], {
            "invalid_end_date": 1, "invalid_discount": 1,
        })

    def test_ml_aceita_codigo_geral_oficial_sem_container(self):
        from apps.scrapers.sources.ml_public_coupons import MLPublicCouponsSource

        source = MLPublicCouponsSource()
        source._cupons_brutos = lambda: [{
            "nome": "CUPOMDASEMANA", "dia_fim": "31/12/2099",
            "valor_desconto": "20%", "discount_num": 20,
            "min_compra": 19, "desconto_max": 150,
            "acao": "Construção e Indústria", "container_url": "-",
        }]
        coupons = list(source.discover_coupons())

        self.assertEqual([coupon.coupon_code for coupon in coupons], ["CUPOMDASEMANA"])
        self.assertEqual(coupons[0].canonical_url, "https://www.mercadolivre.com.br/")
        self.assertEqual(coupons[0].coupon_rules["valor_minimo"], 19)


class _FakeCardValue:
    def __init__(self, text="", attributes=None):
        self.text = text
        self.attributes = attributes or {}

    @property
    def first(self):
        return self

    def count(self):
        return int(bool(self.text or self.attributes))

    def inner_text(self, timeout=None):
        return self.text

    def get_attribute(self, name):
        return self.attributes.get(name)


class _FakeAmazonCard:
    def __init__(self, *, asin="", promo="", title="Produto", current="R$ 100,00",
                 final="Você paga R$ 80,00 com o cupom", variant=""):
        self.attributes = {
            "data-asin": asin,
            "data-csa-c-item-id": f"amzn1.coupon./promo/{promo}" if promo else "",
        }
        self.text = f"{title} {final} {variant}"
        self.title = title
        self.current = current

    def get_attribute(self, name):
        return self.attributes.get(name, "")

    def inner_text(self, timeout=None):
        return self.text

    def locator(self, selector):
        if "href" in selector or "product-card-link" in selector:
            asin = self.attributes.get("data-asin") or "B000000099"
            return _FakeCardValue(attributes={"href": f"https://www.amazon.com.br/dp/{asin}"})
        if "title" in selector or selector in {"h2", "h3", ".a-truncate-full"}:
            return _FakeCardValue(text=self.title)
        if selector == "img":
            return _FakeCardValue(attributes={
                "src": "https://images.example/item.jpg", "alt": self.title,
            })
        if "a-text-price" in selector:
            return _FakeCardValue(text="R$ 120,00")
        if "price" in selector or "a-price" in selector:
            return _FakeCardValue(text=self.current)
        return _FakeCardValue()


class _FakeAmazonCollection:
    def __init__(self, items):
        self.items = items

    @property
    def first(self):
        return self.items[0] if self.items else _FakeCardValue()

    def count(self):
        return len(self.items)

    def nth(self, index):
        return self.items[index]

    def is_visible(self):
        return False


class _FakeAmazonPage:
    def __init__(self, rounds, semantic=False, body="Ofertas"):
        self.rounds = rounds
        self.round = 0
        self.semantic = semantic
        self.body = body

    def goto(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, _milliseconds):
        return None

    def evaluate(self, _script):
        self.round = min(self.round + 1, len(self.rounds) - 1)

    def locator(self, selector):
        if selector == "body":
            return _FakeCardValue(text=self.body)
        if selector == "[data-testid='product-card']" and not self.semantic:
            return _FakeAmazonCollection(self.rounds[self.round])
        if selector == "li:has(a[href*='/dp/'])" and self.semantic:
            return _FakeAmazonCollection(self.rounds[self.round])
        return _FakeAmazonCollection([])


class AmazonProgressiveCollectionTests(SimpleTestCase):
    def test_card_invalido_e_preco_contraditorio_tem_motivo_exato(self):
        from apps.scrapers.sources.amazon_coupons import AmazonCouponsSource

        source = AmazonCouponsSource()
        _row, reason = source._parse_card(_FakeAmazonCard(
            asin="B000000010", promo="promo-10", current="R$ 100,00",
            final="Você paga R$ 100,00 com o cupom",
        ))
        self.assertEqual(reason, "final_not_lower")
        _row, reason = source._parse_card(_FakeAmazonCard(
            asin="B000000011", promo="", current="R$ 100,00",
        ))
        self.assertEqual(reason, "missing_promo")

    @override_settings(
        AMAZON_COUPON_MAX_PAGES=5, AMAZON_COUPON_MAX_ITEMS=20,
        AMAZON_COUPON_MAX_SECONDS=30,
    )
    def test_lazy_load_fallback_dedup_e_balanco_exato(self):
        from apps.scrapers.sources.amazon_coupons import AmazonCouponsSource

        first = _FakeAmazonCard(asin="B000000001", promo="promo-1")
        invalid = _FakeAmazonCard(asin="BAD", promo="promo-invalid")
        duplicate = _FakeAmazonCard(
            asin="B000000001", promo="promo-1", variant="render alternativo",
        )
        second = _FakeAmazonCard(asin="B000000002", promo="promo-2", title="Segundo")
        page = _FakeAmazonPage([[first, invalid], [first, invalid, duplicate, second]], semantic=True)

        @contextmanager
        def browser(**_kwargs):
            yield page, object()

        source = AmazonCouponsSource()
        with patch("apps.scrapers.sources.amazon_coupons.iniciar_browser", browser):
            rows = source._snapshot()

        self.assertEqual({row["asin"] for row in rows}, {"B000000001", "B000000002"})
        metrics = source.last_metrics
        self.assertEqual(metrics["duplicates"], 1)
        self.assertEqual(metrics["rejected_by_reason"]["missing_asin"], 1)
        self.assertEqual(
            metrics["cards_seen"],
            metrics["accepted"] + metrics["duplicates"] + metrics["rejected"],
        )
        self.assertGreaterEqual(metrics["pages_processed"], 2)
        self.assertTrue(metrics["complete"])
        self.assertEqual(metrics["stop_reason"], "no_new_items")
        self.assertEqual(len(metrics["schema_fingerprint"]), 64)
        self.assertEqual(
            set(metrics["duration_by_stage_ms"]),
            {"navigation", "parsing", "pagination_wait"},
        )
        self.assertTrue(all(
            value >= 0 for value in metrics["duration_by_stage_ms"].values()
        ))

    @override_settings(
        AMAZON_COUPON_MAX_PAGES=1, AMAZON_COUPON_MAX_ITEMS=20,
        AMAZON_COUPON_MAX_SECONDS=30,
    )
    def test_teto_de_paginas_e_parcial_e_nao_autoriza_not_found(self):
        from apps.scrapers.sources.amazon_coupons import AmazonCouponsSource

        page = _FakeAmazonPage([[
            _FakeAmazonCard(asin="B000000020", promo="promo-20"),
        ]])

        @contextmanager
        def browser(**_kwargs):
            yield page, object()

        source = AmazonCouponsSource()
        with patch("apps.scrapers.sources.amazon_coupons.iniciar_browser", browser):
            self.assertEqual(len(source._snapshot()), 1)
        self.assertEqual(source.last_health_status, "partial")
        self.assertFalse(source.last_metrics["complete"])
        self.assertEqual(source.last_metrics["stop_reason"], "max_pages")

    def test_captcha_e_bloqueio_nao_viram_coleta_vazia_saudavel(self):
        from apps.scrapers.sources.amazon_coupons import AmazonCouponsSource

        page = _FakeAmazonPage([[]], body="Digite os caracteres: não é um robô")

        @contextmanager
        def browser(**_kwargs):
            yield page, object()

        source = AmazonCouponsSource()
        with patch("apps.scrapers.sources.amazon_coupons.iniciar_browser", browser), \
                patch("apps.scrapers.sources.amazon_coupons.capture_public_diagnostic"):
            with self.assertRaisesRegex(RuntimeError, "captcha"):
                source._snapshot()
        self.assertEqual(source.last_health_status, "blocked")
        self.assertEqual(source.last_metrics["blocked"], 1)


class ReportAdapterFixtureTests(SimpleTestCase):
    HEADERS = "Data;Etiqueta;Produto;Cliques;Conversões;Pedidos;Receita;Comissão\n"

    def test_csv_tsv_xlsx_e_zero_legitimo(self):
        from openpyxl import Workbook
        from apps.scrapers.relatorios import _parse_delimited_report, _parse_xlsx_report

        csv_bytes = (self.HEADERS +
                     "01/08/2026;tag-a;Item;0;0;0;R$ 0,00;R$ 0,00\n").encode()
        csv_rows = _parse_delimited_report(
            csv_bytes, "amazon", date(2026, 8, 1), date(2026, 8, 9),
        )
        self.assertEqual(len(csv_rows), 1)
        self.assertEqual(csv_rows[0].comissao, 0)
        self.assertEqual((csv_rows.seen, csv_rows.rejected), (1, 0))

        tsv = (self.HEADERS.replace(";", "\t") +
               "02/08/2026\ttag-b\tItem B\t10\t1\t1\t100,00\t10,00\n").encode()
        self.assertEqual(len(_parse_delimited_report(
            tsv, "mercadolivre", date(2026, 8, 1), date(2026, 8, 9),
        )), 1)

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Data", "Etiqueta", "Produto", "Cliques", "Conversões",
                      "Pedidos", "Receita", "Comissão"])
        sheet.append([date(2026, 8, 3), "tag-c", "Item C", 20, 2, 2, 200, 20])
        output = io.BytesIO()
        workbook.save(output)
        rows = _parse_xlsx_report(
            output.getvalue(), "amazon", date(2026, 8, 1), date(2026, 8, 9),
        )
        self.assertEqual(rows[0].data, date(2026, 8, 3))
        self.assertTrue(rows.schema_fingerprint)

    def test_celula_ilegivel_e_coluna_alterada_nao_persistem_zero(self):
        from apps.scrapers.relatorios import ReportSyncError, _parse_delimited_report

        invalid = (self.HEADERS +
                   "01/08/2026;tag;Item;n/d;n/d;n/d;n/d;n/d\n").encode()
        with self.assertRaises(ReportSyncError):
            _parse_delimited_report(
                invalid, "amazon", date(2026, 8, 1), date(2026, 8, 9),
            )
        changed = (
            "Data;Etiqueta;Produto;Cliques;Pedidos;Receita;Comissão\n"
            "01/08/2026;tag;Item;1;1;10;1\n"
        ).encode()
        with self.assertRaisesRegex(ReportSyncError, "conversoes"):
            _parse_delimited_report(
                changed, "amazon", date(2026, 8, 1), date(2026, 8, 9),
            )

    def test_contador_invalido_e_periodo_nao_comprovado_abortam_parser(self):
        from apps.scrapers.relatorios import (
            ReportPeriodMismatch, ReportSyncError, _parse_delimited_report,
        )

        fractional = (self.HEADERS +
                      "01/08/2026;tag;Item;1,5;0;0;0;0\n").encode()
        with self.assertRaises(ReportSyncError):
            _parse_delimited_report(
                fractional, "amazon", date(2026, 8, 1), date(2026, 8, 9),
            )

        outside = (self.HEADERS +
                   "31/07/2026;tag;Item;1;0;0;0;0\n").encode()
        with self.assertRaises(ReportPeriodMismatch):
            _parse_delimited_report(
                outside, "amazon", date(2026, 8, 1), date(2026, 8, 9),
            )

    def test_tabela_html_e_paginacao_repetida(self):
        from apps.scrapers.relatorios import (
            ReportSyncError, _extract_paginated_table_rows, _extract_table_rows,
        )

        class Cell:
            def __init__(self, value):
                self.value = value

            def inner_text(self, timeout=None):
                return self.value

        class Locator:
            def __init__(self, items):
                self.items = items

            @property
            def first(self):
                return self.items[0] if self.items else self

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class Row:
            def __init__(self, values):
                self.cells = [Cell(value) for value in values]

            def locator(self, selector):
                return Locator(self.cells) if selector == "td" else Locator([])

        class Next:
            def get_attribute(self, _name):
                return None

            def click(self, timeout=None):
                return None

        class Page:
            headers = [
                "Data", "Etiqueta", "Produto", "Cliques", "Conversões",
                "Pedidos", "Receita", "Comissão",
            ]
            values = ["03/08/2026", "tag", "Item", "0", "0", "0", "0", "0"]

            def __init__(self, has_next=False):
                self.has_next = has_next

            def locator(self, selector):
                if "password" in selector:
                    return Locator([])
                if selector == "table thead th":
                    return Locator([Cell(value) for value in self.headers])
                if selector == "table tbody tr":
                    return Locator([Row(self.values)])
                if selector == "a[rel='next']" and self.has_next:
                    return Locator([Next()])
                return Locator([])

            def wait_for_load_state(self, *args, **kwargs):
                return None

        rows = _extract_table_rows(
            Page(), "mercadolivre", date(2026, 8, 1), date(2026, 8, 9),
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].receita, 0)
        with self.assertRaisesRegex(ReportSyncError, "repetiu"):
            _extract_paginated_table_rows(
                Page(has_next=True), "mercadolivre",
                date(2026, 8, 1), date(2026, 8, 9), max_pages=3,
            )

    @override_settings(
        AMAZON_BROWSER_REPORTS_ENABLED=True, AMAZON_ASSOCIATES_REPORT_URL="",
    )
    def test_pre_requisito_distingue_url_e_sessao(self):
        from apps.scrapers.relatorios import report_prerequisites

        user = type("User", (), {"is_authenticated": True, "pk": 1})()
        with patch("apps.accounts.feature_flags.enabled_for_user", return_value=True):
            result = report_prerequisites(user, "amazon")
        self.assertEqual(result["code"], "url_missing")
        self.assertIn("AMAZON_ASSOCIATES_REPORT_URL", result["instruction"])

        with override_settings(AMAZON_ASSOCIATES_REPORT_URL="https://associados.example/report"), \
                patch("apps.scrapers.report_sessions.has_report_session", return_value=False), \
                patch("apps.accounts.feature_flags.enabled_for_user", return_value=True):
            result = report_prerequisites(user, "amazon")
        self.assertEqual(result["code"], "session_missing")


class DiagnosticRedactionTests(SimpleTestCase):
    def test_texto_publico_remove_query_e_segredos(self):
        from apps.scrapers.source_diagnostics import _safe_text

        value = _safe_text(
            "https://public.example/path?token=secret "
            "Authorization: bearer-secret cookie=session-secret password=hunter2"
        )
        for secret in ("token=secret", "bearer-secret", "session-secret", "hunter2"):
            self.assertNotIn(secret, value)
        self.assertIn("[redacted]", value)


class CouponAudienceAndConfigurationTests(TestCase):
    def setUp(self):
        self.user_a = get_user_model().objects.create_user("coupon-org-a")
        self.user_b = get_user_model().objects.create_user("coupon-org-b")
        self.org_a = ensure_personal_organization(self.user_a)
        self.org_b = ensure_personal_organization(self.user_b)
        self.source = FonteIngestao.objects.create(
            slug="scoped-coupons", marketplace="mercadolivre", nome="ML autenticado",
        )

    def test_cupom_organizacional_nao_aparece_para_outra_organizacao(self):
        from apps.scrapers.coupon_rules import cupons_visiveis_q

        coupon = CupomNormalizado.objects.create(
            fonte=self.source, organization=self.org_a,
            data_scope="organization", audience_scope="organization",
            external_id="campanha:ORG-A", marketplace="mercadolivre",
            titulo="Campanha privada", codigo="", redemption_mode="activation",
            scope_type="container", regras={"modo_resgate": "ativacao"},
        )
        self.assertTrue(CupomNormalizado.objects.filter(
            cupons_visiveis_q(self.user_a), pk=coupon.pk,
        ).exists())
        self.assertFalse(CupomNormalizado.objects.filter(
            cupons_visiveis_q(self.user_b), pk=coupon.pk,
        ).exists())

    def test_aviso_ativo_exige_marketplace_especifico(self):
        config = ConfiguracaoEnvio(
            owner=self.user_a, organization=self.org_a,
            grupo_id="grupo@g.us", tipo="aviso_cupons", marketplace="", ativo=True,
        )
        with self.assertRaises(ValidationError) as error:
            config.full_clean()
        self.assertIn("marketplace", error.exception.message_dict)

        with self.assertRaises(IntegrityError), transaction.atomic():
            ConfiguracaoEnvio.objects.create(
                owner=self.user_a, organization=self.org_a,
                grupo_id="grupo@g.us", tipo="aviso_cupons",
                marketplace="", ativo=True,
            )


class ValvulaDeSegurancaDaFilaTests(TestCase):
    """A regra tem de pausar quando o destino está errado — também na fila v2.

    No caminho síncrono, uma falha permanente incrementa `falhas_consecutivas` e
    `pausar_apos_falhas` acaba freando a regra. Sob a fila, o resultado imediato é
    `sucesso=True, queued=True`: nenhum dos ramos que contam falha era alcançado, e
    o desfecho real acontecia minutos depois, dentro do worker, longe de qualquer
    regra. Uma regra com destino inválido re-enfileirava para sempre.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("valvula", password="x")
        self.organization = ensure_personal_organization(self.user)
        self.config = ConfiguracaoEnvio.objects.create(
            owner=self.user, organization=self.organization,
            grupo_id="destino-invalido", pausar_apos_falhas=2,
        )
        self.product = Produto.objects.create(
            marketplace="mercadolivre", nome="Oferta", origem="oferta",
            preco_sem_desconto=100, preco_com_cupom=80,
            link_produto="https://produto.mercadolivre.com.br/MLB-9-valvula",
        )

    def _publicacao(self):
        return Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            configuracao=self.config, origem="produto", produto=self.product,
            canal="whatsapp", destino_id="destino-invalido",
        )

    def _falhar(self, publicacao, classe="permanente"):
        from apps.scrapers.send_pipeline import (
            _record_pretransport_failure, queue_publications,
        )
        queue_publications([publicacao])
        publicacao.refresh_from_db()
        _record_pretransport_failure([publicacao], {
            "sucesso": False, "classe": classe,
            "motivo": "Destino do WhatsApp inválido.",
        })

    def test_falha_permanente_conta_para_a_regra(self):
        self._falhar(self._publicacao())
        self.config.refresh_from_db()
        self.assertEqual(self.config.falhas_consecutivas, 1)
        self.assertIsNone(self.config.pausada_ate)

    def test_ao_bater_o_teto_a_regra_freia(self):
        for _ in range(2):
            self._falhar(self._publicacao())
        self.config.refresh_from_db()
        self.assertEqual(self.config.falhas_consecutivas, 2)
        self.assertIsNotNone(self.config.pausada_ate)
        self.assertIn("inválido", self.config.motivo_pausa)

    def test_falha_transitoria_nao_conta(self):
        # Era exatamente isto que desligava a automação de quem não tinha defeito
        # nenhum na regra: worker piscou, 429, timeout.
        self._falhar(self._publicacao(), classe="transitorio")
        self.config.refresh_from_db()
        self.assertEqual(self.config.falhas_consecutivas, 0)
        self.assertIsNone(self.config.pausada_ate)

    def test_envio_manual_sem_regra_nao_quebra(self):
        publicacao = Publicacao.objects.create(
            usuario=self.user, organization=self.organization,
            origem="produto", produto=self.product, canal="whatsapp",
            destino_id="destino-invalido",
        )
        self._falhar(publicacao)  # configuracao=None: não há regra para pausar
        publicacao.refresh_from_db()
        self.assertEqual(publicacao.stage, "permanent_failed")

import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
from datetime import timedelta
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from django.contrib.auth import get_user_model
from django.core.exceptions import (
    ImproperlyConfigured,
    PermissionDenied,
    ValidationError,
)
from django.test import (
    SimpleTestCase, TestCase, TransactionTestCase, override_settings,
)
from django.db import connection
from django.urls import reverse
from django.utils import timezone

from apps.accounts.ml_session_crypto import MLSessionCryptoError
from apps.accounts.ml_sessions import (
    has_storage_state, load_storage_state, save_storage_state,
)
from apps.accounts.models import (
    Membership,
    MercadoLivreSession,
    WhatsAppConnection,
)
from apps.accounts.rls import (
    MIXED_TENANT_TABLES, STRICT_TENANT_TABLES, SYSTEM_ONLY_TABLES,
    policy_statements,
)
from apps.accounts.tenant import (
    _context_signature, current_organization_id, executar_no_tenant,
    organization_context,
)
from apps.accounts.wa_capabilities import issue_capability, public_key_base64url
from apps.scrapers.models import Produto


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


ML_KEYS = json.dumps({
    "v1": _b64(bytes(range(32))),
    "v2": _b64(bytes(reversed(range(32)))),
})
WA_PRIVATE = _b64(bytes(range(32)))
TENANT_CONTEXT_KEY = _b64(bytes(range(48)))


class OrganizationBoundaryTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("tenant-alice", password="test")
        self.bob = User.objects.create_user("tenant-bob", password="test")

    def test_user_creation_provisions_boundary_and_connection(self):
        organization = self.alice.personal_organization
        self.assertEqual(self.alice.perfil.organization, organization)
        membership = Membership.objects.get(
            organization=organization, user=self.alice,
        )
        connection = WhatsAppConnection.objects.get(organization=organization)

        self.assertEqual(membership.role, "owner")
        self.assertTrue(membership.is_active)
        self.assertEqual(connection.instance_id, str(self.alice.pk))
        self.assertNotEqual(
            organization.pk, self.bob.personal_organization.pk,
        )

    def test_private_model_derives_organization_from_owner(self):
        product = Produto.objects.create(
            owner=self.alice,
            nome="Privado",
            preco_sem_desconto=100,
            preco_com_cupom=80,
            link_produto="https://example.com/item",
        )
        self.assertEqual(product.organization, self.alice.personal_organization)
        self.assertEqual(product.data_scope, "organization")

    def test_cross_tenant_write_is_rejected_before_database(self):
        with organization_context(self.alice.personal_organization):
            with self.assertRaises(PermissionDenied):
                Produto.objects.create(
                    owner=self.bob,
                    nome="Tentativa",
                    preco_sem_desconto=100,
                    preco_com_cupom=80,
                    link_produto="https://example.com/item",
                )

    def test_owner_and_explicit_organization_must_match(self):
        with self.assertRaises(ValidationError):
            Produto.objects.create(
                owner=self.alice,
                organization=self.bob.personal_organization,
                nome="Tentativa",
                preco_sem_desconto=100,
                preco_com_cupom=80,
                link_produto="https://example.com/item",
            )


@override_settings(
    ML_SESSION_KEKS_JSON=ML_KEYS,
    ML_SESSION_CURRENT_KEY_VERSION="v1",
    ML_LEGACY_SESSION_READ_ENABLED=False,
)
class MercadoLivreEncryptionTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "ml-secure", password="test",
        )
        self.state = {
            "cookies": [{"name": "ssid", "value": "segredo-cookie"}],
            "origins": [{"origin": "https://mercadolivre.com.br"}],
        }

    def test_roundtrip_does_not_store_plaintext(self):
        record = save_storage_state(self.user, self.state)
        self.assertEqual(load_storage_state(self.user), self.state)
        self.assertNotIn(b"segredo-cookie", bytes(record.ciphertext))
        self.assertNotIn(b"ssid", bytes(record.ciphertext))

    def test_reconexao_antecipa_preparo_bloqueado_por_sessao(self):
        from apps.scrapers.coupon_products import ERRO_SESSAO_ML
        from apps.scrapers.models import (
            CupomNormalizado, CupomPreparacao, FonteIngestao,
        )

        fonte = FonteIngestao.objects.create(
            slug="ml-reconnect-test", marketplace="mercadolivre", nome="ML",
        )
        cupom = CupomNormalizado.objects.create(
            fonte=fonte, external_id="campanha:reconnect",
            marketplace="mercadolivre", titulo="Cupom", codigo="",
        )
        preparo = CupomPreparacao.objects.create(
            cupom=cupom, status="erro", erro=ERRO_SESSAO_ML,
            proxima_tentativa=timezone.now() + timedelta(hours=1),
        )

        save_storage_state(self.user, self.state)

        preparo.refresh_from_db()
        self.assertIsNone(preparo.proxima_tentativa)
        self.assertEqual(preparo.erro, ERRO_SESSAO_ML)

    def test_ciphertext_tampering_is_detected_and_quarantined(self):
        record = save_storage_state(self.user, self.state)
        tampered = bytearray(record.ciphertext)
        tampered[-1] ^= 1
        MercadoLivreSession.objects.filter(pk=record.pk).update(
            ciphertext=bytes(tampered),
        )

        with self.assertRaises(MLSessionCryptoError):
            load_storage_state(self.user)
        record.refresh_from_db()
        self.assertEqual(record.status, "decrypt_error")

    def test_aad_prevents_moving_session_to_another_tenant(self):
        other = get_user_model().objects.create_user("ml-other", password="test")
        record = save_storage_state(self.user, self.state)
        MercadoLivreSession.objects.filter(pk=record.pk).update(
            organization=other.personal_organization,
        )

        with self.assertRaises(MLSessionCryptoError):
            load_storage_state(other)

    def test_key_rotation_reencrypts_with_current_version(self):
        record = save_storage_state(self.user, self.state)
        self.assertEqual(record.key_version, "v1")
        with override_settings(ML_SESSION_CURRENT_KEY_VERSION="v2"):
            record = save_storage_state(self.user, self.state)
            self.assertEqual(record.key_version, "v2")
            self.assertEqual(load_storage_state(self.user), self.state)

    def test_exact_legacy_file_migrates_then_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, f"auth_{self.user.pk}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle)
            with override_settings(
                ML_AUTH_DIR=directory,
                ML_LEGACY_SESSION_READ_ENABLED=True,
            ):
                self.assertEqual(load_storage_state(self.user), self.state)
            self.assertFalse(os.path.exists(path))
            self.assertTrue(MercadoLivreSession.objects.filter(
                organization=self.user.personal_organization,
            ).exists())


@override_settings(
    ML_SESSION_KEKS_JSON=ML_KEYS,
    ML_SESSION_CURRENT_KEY_VERSION="v1",
    ML_LEGACY_SESSION_READ_ENABLED=False,
)
class VereditoDaSondaTests(TestCase):
    """A sonda nunca apaga a credencial — e uma suspeita isolada não desconecta.

    Antes, `conexoes.estado_ml` chamava `delete_storage_state` no primeiro veredito
    "expirado". Só que quem produzia esse veredito era um GET a partir do IP de
    datacenter da Fly, onde o gateway anti-bot do ML responde 302→login/403 a
    requisições autenticadas. O usuário conectava, via a tela verde, e um worker o
    desconectava minutos depois — apagando a sessão da organização inteira.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("ml-probe", password="test")
        self.org = self.user.personal_organization
        self.state = {"cookies": [{"name": "ssid", "value": "x"}], "origins": []}
        save_storage_state(self.user, self.state)

    def _suspeitar(self, vezes, *, espacadas=True):
        from apps.accounts.ml_sessions import PROBE_JANELA_S, registrar_veredito

        for _ in range(vezes):
            registrar_veredito(self.org, "suspeito", "o ML redirecionou para o login")
            if espacadas:
                MercadoLivreSession.objects.filter(organization=self.org).update(
                    last_probe_at=timezone.now() - timedelta(seconds=PROBE_JANELA_S + 1),
                )
        return MercadoLivreSession.objects.get(organization=self.org)

    def test_uma_suspeita_nao_apaga_nem_desconecta(self):
        record = self._suspeitar(1)
        self.assertEqual(record.status, "suspect")
        self.assertEqual(record.probe_failures, 1)
        self.assertEqual(load_storage_state(self.user), self.state)

    def test_suspeitas_repetidas_pedem_reconexao_sem_apagar(self):
        from apps.accounts.ml_sessions import PROBE_FALHAS_PARA_DESCONECTAR

        record = self._suspeitar(PROBE_FALHAS_PARA_DESCONECTAR)
        self.assertEqual(record.status, "expired")
        self.assertFalse(has_storage_state(self.user))
        # Os bytes continuam lá: a sonda segue rodando e um "conectado" reabilita.
        self.assertEqual(load_storage_state(self.user), self.state)

    def test_rajada_simultanea_conta_como_uma_suspeita(self):
        """Nove processos sondam em paralelo (ver python/Procfile). Sem a janela,
        uma única rajada valeria pelos três ciclos independentes que a política
        exige e desconectaria o usuário em segundos."""
        record = self._suspeitar(5, espacadas=False)
        self.assertEqual(record.probe_failures, 1)
        self.assertEqual(record.status, "suspect")

    def test_conectado_reabilita_a_sessao_sozinho(self):
        from apps.accounts.ml_sessions import (
            PROBE_FALHAS_PARA_DESCONECTAR, registrar_veredito,
        )

        self._suspeitar(PROBE_FALHAS_PARA_DESCONECTAR)
        registrar_veredito(self.org, "conectado")
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.status, "active")
        self.assertEqual(record.probe_failures, 0)
        self.assertTrue(has_storage_state(self.user))

    def test_inconclusivo_nao_conta_falha(self):
        from apps.accounts.ml_sessions import registrar_veredito

        registrar_veredito(self.org, "inconclusivo", "timeout")
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.probe_failures, 0)
        self.assertEqual(record.status, "active")

    def test_reconectar_zera_o_historico_da_sonda(self):
        self._suspeitar(2)
        save_storage_state(self.user, self.state)
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.probe_failures, 0)
        self.assertEqual(record.last_probe_result, "")
        self.assertEqual(record.status, "active")

    def test_leitura_de_sonda_nao_marca_uso(self):
        MercadoLivreSession.objects.filter(organization=self.org).update(
            last_used_at=None,
        )
        load_storage_state(self.user, touch=False)
        self.assertIsNone(
            MercadoLivreSession.objects.get(organization=self.org).last_used_at,
        )


class VereditoDoLinkBuilderTests(TestCase):
    """O veredito do PORTAL DE AFILIADOS, isolado do veredito do site.

    A separação é o ponto: o portal recusar o cookie não pode bloquear a raspagem
    do site, que usa a MESMA credencial e continua funcionando. Se este veredito
    alimentasse `status`, três suspeitas do Link Builder derrubariam o catálogo
    inteiro da organização.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("lb-probe", password="test")
        self.org = self.user.personal_organization
        self.state = {"cookies": [{"name": "ssid", "value": "x"}], "origins": []}
        save_storage_state(self.user, self.state)

    def _suspeitar(self, vezes, *, espacadas=True):
        from apps.accounts.ml_sessions import (
            PROBE_JANELA_S, registrar_veredito_linkbuilder,
        )

        for _ in range(vezes):
            registrar_veredito_linkbuilder(self.org, "suspeito", "portal pediu login")
            if espacadas:
                MercadoLivreSession.objects.filter(organization=self.org).update(
                    lb_last_probe_at=timezone.now() - timedelta(seconds=PROBE_JANELA_S + 1),
                )
        return MercadoLivreSession.objects.get(organization=self.org)

    def test_suspeitas_do_portal_nunca_bloqueiam_a_sessao_do_site(self):
        from apps.accounts.ml_sessions import PROBE_FALHAS_PARA_DESCONECTAR

        record = self._suspeitar(PROBE_FALHAS_PARA_DESCONECTAR + 2)
        self.assertGreaterEqual(record.lb_probe_failures, PROBE_FALHAS_PARA_DESCONECTAR)
        # `status` intocado: a raspagem segue autorizada.
        self.assertEqual(record.status, "active")
        self.assertTrue(has_storage_state(self.user))
        self.assertEqual(load_storage_state(self.user), self.state)

    def test_os_dois_vereditos_nao_se_misturam(self):
        from apps.accounts.ml_sessions import registrar_veredito

        registrar_veredito(self.org, "suspeito", "site")
        self._suspeitar(1)
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.probe_failures, 1)
        self.assertEqual(record.lb_probe_failures, 1)
        registrar_veredito(self.org, "conectado")
        record.refresh_from_db()
        # Site voltou; o portal segue suspeito, que é exatamente o caso real.
        self.assertEqual(record.probe_failures, 0)
        self.assertEqual(record.lb_probe_failures, 1)

    def test_rajada_simultanea_conta_como_uma_suspeita(self):
        record = self._suspeitar(5, espacadas=False)
        self.assertEqual(record.lb_probe_failures, 1)

    def test_conectado_zera_o_contador(self):
        from apps.accounts.ml_sessions import registrar_veredito_linkbuilder

        self._suspeitar(2)
        registrar_veredito_linkbuilder(self.org, "conectado")
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.lb_probe_failures, 0)

    def test_inconclusivo_nao_conta_falha(self):
        from apps.accounts.ml_sessions import registrar_veredito_linkbuilder

        registrar_veredito_linkbuilder(self.org, "inconclusivo", "anti-bot")
        self.assertEqual(
            MercadoLivreSession.objects.get(organization=self.org).lb_probe_failures, 0)

    def test_reconectar_zera_o_historico_do_portal(self):
        """O login novo atravessa o SSO do portal: as suspeitas eram sobre os
        cookies antigos."""
        self._suspeitar(2)
        save_storage_state(self.user, self.state)
        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.lb_probe_failures, 0)
        self.assertEqual(record.lb_last_probe_result, "")
        self.assertIsNone(record.lb_last_probe_at)

    def test_snapshot_devolve_chaves_sem_prefixo(self):
        """conexoes._estado_do_registro traduz os dois escopos com o mesmo código."""
        from apps.accounts.ml_sessions import linkbuilder_snapshot

        self._suspeitar(1)
        snap = linkbuilder_snapshot(self.org)
        self.assertEqual(snap["probe_failures"], 1)
        self.assertEqual(snap["last_probe_result"], "suspeito")
        # "" e não "active": este escopo não tem status próprio, de propósito.
        self.assertEqual(snap["status"], "")

    def test_renovacao_de_cookies_preserva_prontidao_e_status(self):
        from apps.accounts.ml_sessions import (
            registrar_prontidao_linkbuilder,
            renew_storage_state,
        )

        registrar_prontidao_linkbuilder(
            self.org, "login_required", "portal pediu login",
        )
        MercadoLivreSession.objects.filter(organization=self.org).update(
            status="suspect",
        )
        renew_storage_state(
            self.user,
            {"cookies": [{"name": "ssid", "value": "renovado"}], "origins": []},
        )

        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.lb_readiness, "login_required")
        self.assertEqual(record.lb_readiness_reason, "portal pediu login")
        self.assertEqual(record.status, "suspect")

    def test_nova_autenticacao_retorna_prontidao_para_unknown(self):
        from apps.accounts.ml_sessions import registrar_prontidao_linkbuilder

        registrar_prontidao_linkbuilder(self.org, "ready", "controles visíveis")
        save_storage_state(self.user, self.state)

        record = MercadoLivreSession.objects.get(organization=self.org)
        self.assertEqual(record.lb_readiness, "unknown")
        self.assertIsNone(record.lb_readiness_checked_at)


@override_settings(
    WA_CAPABILITY_PRIVATE_KEY=WA_PRIVATE,
    WA_CAPABILITY_KEY_ID="test-ed25519",
    WA_CAPABILITY_ISSUER="spreading-web",
    WA_CAPABILITY_AUDIENCE="spreading-wa",
    WA_CAPABILITY_TTL_SECONDS=30,
)
class WhatsAppCapabilityTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "wa-secure", password="test",
        )
        self.connection = self.user.personal_organization.whatsapp_connection

    def test_capability_is_tenant_session_action_and_time_bound(self):
        token = issue_capability(
            self.connection.instance_id, ["send"], single_use=True,
        )
        public_raw = base64.urlsafe_b64decode(
            public_key_base64url() + "==",
        )
        payload = jwt.decode(
            token,
            Ed25519PublicKey.from_public_bytes(public_raw),
            algorithms=["EdDSA"],
            issuer="spreading-web",
            audience="spreading-wa",
        )
        self.assertEqual(payload["sub"], str(self.user.personal_organization.pk))
        self.assertEqual(payload["organization_id"], payload["sub"])
        self.assertEqual(payload["session_id"], self.connection.instance_id)
        self.assertEqual(payload["actions"], ["send"])
        self.assertTrue(payload["single_use"])
        self.assertLessEqual(payload["exp"] - payload["iat"], 30)

    def test_unknown_session_is_denied(self):
        with self.assertRaises(PermissionDenied):
            issue_capability("tenant-inexistente", ["send"])


class FailClosedConfigurationTests(TestCase):
    @override_settings(
        SECURITY_FREEZE_NEW_TENANTS=True,
        PERMITIR_CADASTRO_PUBLICO=True,
    )
    def test_signup_freeze_wins_over_public_signup_setting(self):
        response = self.client.get(reverse("signup"))
        self.assertRedirects(response, reverse("login"))


@override_settings(WHATSAPP_WEB_ENABLED=False)
class MembershipRoleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "role-user", password="test", is_staff=True,
        )
        self.user.perfil.marcar_verificado()
        self.membership = Membership.objects.get(
            user=self.user,
            organization=self.user.personal_organization,
        )
        self.client.force_login(self.user)

    def test_viewer_can_read_but_cannot_mutate_or_start_sse_job(self):
        self.membership.role = "viewer"
        self.membership.save(update_fields=["role"])

        self.assertEqual(self.client.get(reverse("scraper-dashboard")).status_code, 200)
        self.assertEqual(self.client.post(reverse("scraper-automacao")).status_code, 403)
        self.assertEqual(self.client.get(reverse("scraper-gerar-links")).status_code, 403)

    def test_operator_cannot_manage_credentials_or_connections(self):
        self.membership.role = "operator"
        self.membership.save(update_fields=["role"])

        self.assertEqual(self.client.get(reverse("scraper-dashboard")).status_code, 200)
        self.assertEqual(self.client.get(reverse("scraper-whatsapp")).status_code, 403)


class RLSPolicyTests(SimpleTestCase):
    def test_session_tables_are_protected_and_force_rls_is_emitted(self):
        self.assertIn("accounts_mercadolivresession", STRICT_TENANT_TABLES)
        self.assertIn("accounts_browsersession", STRICT_TENANT_TABLES)
        self.assertIn("accounts_whatsappconnection", STRICT_TENANT_TABLES)
        self.assertIn("accounts_perfil", STRICT_TENANT_TABLES)
        statements = policy_statements(
            "accounts_mercadolivresession", mixed=False,
        )
        self.assertTrue(any("CREATE POLICY tenant_select" in sql for sql in statements))
        self.assertTrue(any("WITH CHECK" in sql for sql in statements))
        self.assertTrue(any("current_user IN" in sql for sql in statements))
        self.assertTrue(any("app.organization_signature" in sql for sql in statements))
        self.assertTrue(any("app.system_signature" in sql for sql in statements))
        self.assertTrue(any(
            "tenant_security.context_valid" in sql for sql in statements
        ))
        self.assertIn(
            'ALTER TABLE "accounts_mercadolivresession" FORCE ROW LEVEL SECURITY',
            statements,
        )

    def test_signature_is_checked_once_per_query_without_weakening_hmac(self):
        strict = " ".join(policy_statements(
            "accounts_mercadolivresession", mixed=False,
        ))
        mixed = " ".join(policy_statements(
            "scrapers_produto", mixed=True,
        ))

        # A subconsulta não correlacionada vira InitPlan no PostgreSQL. Sem ela,
        # context_valid consultava o segredo e calculava HMAC uma vez por linha.
        self.assertIn("SELECT tenant_security.context_valid('system'", strict)
        self.assertIn("SELECT tenant_security.context_valid('organization'", strict)
        self.assertIn("organization_id IS NULL OR", mixed)
        self.assertNotIn(
            "AND tenant_security.context_valid('organization'", strict,
        )

    def test_perfil_pessoal_continua_gravavel_pelo_proprio_actor_em_org_compartilhada(self):
        statements = policy_statements("accounts_perfil", mixed=False)
        update_policy = next(
            sql for sql in statements if "CREATE POLICY tenant_update" in sql
        )
        self.assertIn("app.actor_id", update_policy)
        self.assertIn("tenant_security.context_valid('actor'", update_policy)

    def test_novas_projecoes_e_controles_estao_na_classe_rls_correta(self):
        strict = {
            "accounts_organizationfeatureoverride",
            "scrapers_execucaoraspagem",
            "scrapers_eventoraspagem",
            "scrapers_cupomdisponibilidade",
            "scrapers_cupomdisponibilidadeevento",
            "scrapers_linkafiliadoprodutocupomusuario",
            "scrapers_publicacaotentativa",
            "scrapers_publicacaoevento",
            "scrapers_relatoriosync",
        }
        self.assertTrue(strict.issubset(set(STRICT_TENANT_TABLES)))
        self.assertIn("scrapers_execucaoingestao", MIXED_TENANT_TABLES)
        self.assertEqual(
            set(SYSTEM_ONLY_TABLES),
            {"scrapers_workerheartbeat", "scrapers_resourcelease"},
        )

        system_sql = " ".join(policy_statements(
            "scrapers_resourcelease", mixed=False, system_only=True,
        ))
        self.assertIn("app.system_signature", system_sql)
        self.assertNotIn("app.organization_id", system_sql)


class TenantContextSigningTests(SimpleTestCase):
    @override_settings(
        APP_ENV="production",
        TENANT_CONTEXT_SIGNING_KEY=TENANT_CONTEXT_KEY,
    )
    def test_context_signature_is_hmac_sha256_and_tenant_bound(self):
        organization_id = "62df844f-824b-42bd-82c0-a25076c67ab4"
        expected = hmac.new(
            TENANT_CONTEXT_KEY.encode("utf-8"),
            f"organization:{organization_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(
            _context_signature("organization", organization_id),
            expected,
        )
        self.assertNotEqual(
            _context_signature(
                "organization",
                "dfc593bc-c7ea-483f-ab20-2eb335e81bd4",
            ),
            expected,
        )

    @override_settings(APP_ENV="production", TENANT_CONTEXT_SIGNING_KEY="")
    def test_production_context_without_key_fails_closed(self):
        with self.assertRaises(ImproperlyConfigured):
            _context_signature("system")


class PonteORMForaDoLoopTests(TransactionTestCase):
    """`executar_no_tenant`: a ponte que substituiu DJANGO_ALLOW_ASYNC_UNSAFE.

    A API sync do Playwright deixa um event loop rodando num greenlet desta mesma
    thread, e o @async_unsafe do Django derruba qualquer query enquanto isso durar.
    O bypass antigo era uma variável de ambiente GLOBAL AO PROCESSO: com 8 threads no
    gunicorn, o `finally` de um fluxo removia a permissão no meio de outro. Aqui a
    query é desviada para uma thread sem loop — e o tenant precisa ser reinstalado
    lá, porque contextvars não cruzam threads de executor.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user("ponte-orm", password="test")
        self.organization = self.user.personal_organization

    def test_fora_do_playwright_roda_na_mesma_thread(self):
        # É isto que mantém `TestCase` (transação revertida no fim) enxergando as
        # escritas do resto da suíte: sem loop rodando, não há desvio nenhum.
        with organization_context(self.organization):
            thread = executar_no_tenant(threading.get_ident)
        self.assertEqual(thread, threading.get_ident())

    def test_dentro_do_playwright_desvia_para_outra_thread_e_persiste(self):
        with organization_context(self.organization), \
             patch("apps.accounts.tenant._dentro_de_loop", return_value=True):
            thread = executar_no_tenant(threading.get_ident)
            executar_no_tenant(
                Produto.objects.create, owner=self.user, nome="Gravado pela ponte",
                preco_sem_desconto=100, preco_com_cupom=80,
                marketplace="mercadolivre", origem="oferta",
            )

        self.assertNotEqual(thread, threading.get_ident())
        self.assertTrue(
            Produto.objects.filter(nome="Gravado pela ponte").exists(),
            "a escrita feita na thread da ponte tem de estar commitada",
        )

    def test_o_escopo_de_organizacao_e_reinstalado_na_thread(self):
        # Sem reinstalar, a organização da chamada anterior sobreviveria na conexão
        # persistente do executor e vazaria para o tenant seguinte.
        vistos = []
        with organization_context(self.organization), \
             patch("apps.accounts.tenant._dentro_de_loop", return_value=True):
            executar_no_tenant(lambda: vistos.append(current_organization_id()))
        self.assertEqual(vistos, [str(self.organization.pk)])

    def test_excecao_propaga_em_vez_de_ficar_presa_no_future(self):
        def explode():
            raise ZeroDivisionError("falha real")

        with organization_context(self.organization), \
             patch("apps.accounts.tenant._dentro_de_loop", return_value=True):
            with self.assertRaises(ZeroDivisionError):
                executar_no_tenant(explode)

    def test_sem_tenant_falha_fechado(self):
        # Falhar aqui é melhor que falhar na RLS minutos depois, com o browser já
        # fechado e o dado capturado perdido.
        with self.assertRaises(ValueError):
            executar_no_tenant(lambda: None)

    def test_tenant_suspenso_bloqueia_orm_direto_antes_da_rls(self):
        from apps.accounts.tenant import TenantSuspensoORMError, tenant_suspenso

        with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
            with self.assertRaisesRegex(TenantSuspensoORMError, "executar_no_tenant"):
                Produto.objects.filter(owner=self.user).exists()

    def test_tenant_suspenso_permite_orm_pela_ponte(self):
        from apps.accounts.tenant import tenant_suspenso

        with tenant_suspenso(self.organization.pk, actor_id=self.user.pk):
            produto = executar_no_tenant(
                Produto.objects.create,
                owner=self.user,
                nome="Gravado sob tenant suspenso",
                preco_sem_desconto=100,
                preco_com_cupom=80,
                marketplace="mercadolivre",
                origem="oferta",
            )
        self.assertEqual(produto.organization_id, self.organization.pk)


class EscopoAninhadoRestauraContextoTests(TestCase):
    """Sair de um escopo aninhado não pode apagar o escopo do chamador.

    Reproduz o incidente de produção: o worker de envio roda inteiro sob
    `@system_job`; ao gerar um link ele chama `executar_no_tenant`, que fora do
    Playwright abre um `organization_context` NA MESMA CONEXÃO. Ao sair, esse
    contexto zerava `app.system_context`, e como o ContextVar do worker continuava
    True nada nunca o reinstalava — o `cfg.save()` seguinte caía na RLS como sessão
    anônima e o tick inteiro morria em ValidationError, todo ciclo.

    Os GUCs são espionados em vez de exercitados de verdade porque o RLS é
    exclusivo do Postgres e a suíte roda em SQLite; o que importa aqui é a ORDEM e
    o CONTEÚDO das reinstalações, que são os mesmos nos dois backends.
    """

    def setUp(self):
        from apps.accounts import tenant
        self.tenant = tenant
        self.chamadas = []

        def _espiao(*, organization_id=None, system=False, local=True):
            self.chamadas.append(
                {"organization_id": str(organization_id or ""),
                 "system": bool(system), "local": bool(local)})

        remendo_ctx = patch.object(tenant, "_set_postgres_context", _espiao)
        # `_reinstalar_escopo_ambiente` sai cedo fora do Postgres; forçamos o corpo.
        remendo_vendor = patch.object(
            tenant.connection, "vendor", "postgresql", create=False)
        remendo_role = patch.object(tenant, "_assert_privileged_database_role")
        for remendo in (remendo_ctx, remendo_vendor, remendo_role):
            remendo.start()
            self.addCleanup(remendo.stop)

    def test_organization_aninhada_devolve_o_system_do_worker(self):
        org = "11111111-1111-1111-1111-111111111111"
        with self.tenant.system_context():
            self.assertTrue(self.tenant.in_system_context())
            with self.tenant.organization_context(org):
                self.assertFalse(self.tenant.in_system_context())
            # O ponto do incidente: aqui o worker precisa continuar em system.
            self.assertTrue(self.tenant.in_system_context())

        # Última reinstalação de dentro do `with`: system de volta, não 'off'.
        reinstalacao = self.chamadas[-2]
        self.assertTrue(reinstalacao["system"])
        self.assertFalse(reinstalacao["local"])
        # E ao fechar o system_context externo, aí sim o escopo é liberado.
        self.assertFalse(self.chamadas[-1]["system"])
        self.assertEqual(self.chamadas[-1]["organization_id"], "")

    def test_organization_aninhada_devolve_a_organizacao_de_fora(self):
        fora = "22222222-2222-2222-2222-222222222222"
        dentro = "33333333-3333-3333-3333-333333333333"
        with self.tenant.organization_context(fora):
            with self.tenant.organization_context(dentro):
                pass
            self.assertEqual(self.tenant.current_organization_id(), fora)

        reinstalacao = self.chamadas[-2]
        self.assertEqual(reinstalacao["organization_id"], fora)
        self.assertFalse(reinstalacao["system"])

    def test_sem_escopo_por_baixo_continua_zerando(self):
        # A garantia oposta: fora de qualquer aninhamento, sair LIMPA a conexão.
        # Sem isto uma organização vazaria na conexão persistente do executor.
        with self.tenant.organization_context(
                "44444444-4444-4444-4444-444444444444"):
            pass

        self.assertEqual(self.chamadas[-1],
                         {"organization_id": "", "system": False, "local": False})

    def test_excecao_no_corpo_tambem_restaura(self):
        with self.assertRaises(ZeroDivisionError):
            with self.tenant.system_context():
                with self.tenant.organization_context(
                        "55555555-5555-5555-5555-555555555555"):
                    raise ZeroDivisionError("falha no meio do job")
        self.assertFalse(self.tenant.in_system_context())

    def test_actor_aninhado_devolve_o_actor_de_fora(self):
        atores = []
        with patch.object(self.tenant, "_set_postgres_actor",
                          lambda actor_id=None, *, local=True: atores.append(
                              str(actor_id or ""))):
            with self.tenant.actor_context(7):
                with self.tenant.actor_context(9):
                    pass
                self.assertEqual(self.tenant.current_actor_id(), "7")

        self.assertEqual(atores[-2], "7")
        self.assertEqual(atores[-1], "")


class ContextoDeOrganizacaoCacheadoTests(TestCase):
    """Resolver o tenant custava três consultas em TODA request autenticada.

    No live view das conexões cada clique e cada tecla é um POST próprio, e um POST
    que nem toca no ORM estava pagando essas três idas ao Postgres antes de a view
    começar — no mesmo processo que hospeda o Chromium do login. O cache só é
    aceitável porque a invalidação é imediata: uma decisão de RBAC não pode ser
    servida de uma foto velha.
    """

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.user = get_user_model().objects.create_user("ctx-cache", password="test")
        self.user.perfil.marcar_verificado()
        self.organization = self.user.personal_organization

    def _resolver(self):
        from apps.accounts import organization_middleware

        return organization_middleware._resolver(self.user)

    def test_segunda_resolucao_nao_volta_ao_banco(self):
        self._resolver()
        with self.assertNumQueries(0):
            organization, membership = self._resolver()
        self.assertEqual(organization.pk, self.organization.pk)
        self.assertEqual(membership.role, "owner")

    def test_revogar_vinculo_vale_na_request_seguinte(self):
        self._resolver()
        membership = Membership.objects.get(
            organization=self.organization, user=self.user,
        )
        membership.is_active = False
        membership.save()
        # Sem o signal, o acesso revogado continuaria valendo até o TTL vencer.
        _organization, membership_apos = self._resolver()
        self.assertIsNone(membership_apos)

    def test_pk_reciclada_nao_herda_a_organizacao_anterior(self):
        # O rollback de cada TestCase devolve a sequência: o usuário 1 de um teste
        # não é o usuário 1 do seguinte. A entrada guarda o date_joined justamente
        # para que uma PK reaproveitada não sirva a resolução da conta antiga.
        from apps.accounts import organization_middleware

        self._resolver()
        outro = get_user_model().objects.create_user("ctx-outro", password="test")
        # Mesma PK, conta diferente: é o cenário que o TestCase produz sozinho.
        outro.pk = self.user.pk
        _organization, _membership = organization_middleware._resolver(outro)
        cached = organization_middleware.cache.get(
            organization_middleware._cache_key(outro.pk),
        )
        self.assertEqual(cached[0], outro.date_joined)

    def test_middleware_instala_organizacao_e_vinculo_na_request(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("scraper-ml-conexao"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.organization.pk, self.organization.pk)
        self.assertEqual(
            response.wsgi_request.organization_membership.role, "owner",
        )
        # Helpers executados pela view reutilizam o objeto que o middleware acabou
        # de autorizar; não repetem Perfil + Organization + Membership.
        from apps.accounts.models import organization_for_user

        with self.assertNumQueries(0):
            organization = organization_for_user(response.wsgi_request.user)
        self.assertEqual(organization.pk, self.organization.pk)


class DebugFechaEmProducaoTests(SimpleTestCase):
    """DEBUG ligado com APP_ENV de produção tem de impedir o processo de subir.

    O default de DEBUG olha `FLY_APP_NAME`, e essa é exatamente a variável que
    some quando a MESMA imagem roda fora da Fly. Sem esta trava, um host qualquer
    subia com DEBUG=1 e, junto, o `DevAutoLoginMiddleware`, que cria e autentica
    um SUPERUSUÁRIO para requisição anônima.
    """

    def _importar_settings(self, **ambiente):
        import subprocess
        import sys

        from django.conf import settings as _settings

        env = dict(os.environ)
        env.pop("FLY_APP_NAME", None)
        env.update({
            "DJANGO_SECRET_KEY": "chave-de-teste-suficientemente-longa-para-passar",
            "PYTHONIOENCODING": "utf-8",
        })
        env.update(ambiente)
        return subprocess.run(
            [sys.executable, "-c", "import core.settings"],
            cwd=str(_settings.BASE_DIR),
            env=env, capture_output=True, text=True,
        )

    def test_debug_com_app_env_de_producao_nao_sobe(self):
        for app_env in ("production", "staging"):
            with self.subTest(app_env=app_env):
                r = self._importar_settings(DJANGO_DEBUG="1", APP_ENV=app_env)
                self.assertNotEqual(r.returncode, 0, r.stdout)
                # Sem acento na asserção: o stderr do subprocesso volta na
                # codificação do console (cp1252 no Windows) e "está" chega
                # mojibake, quebrando um teste que na verdade passou.
                self.assertIn("ImproperlyConfigured", r.stderr)
                self.assertIn(f"APP_ENV={app_env}", r.stderr)

    def test_desenvolvimento_com_debug_continua_subindo(self):
        r = self._importar_settings(DJANGO_DEBUG="1", APP_ENV="development")
        self.assertEqual(r.returncode, 0, r.stderr)


class LinkPublicoRLSTests(SimpleTestCase):
    """A única leitura anônima do produto: o link que já foi publicado.

    Sem esta porta, `redirect_curto` responde 404 para TODO link enviado e nenhum
    clique é registrado — a receita morre em silêncio, e a suíte em SQLite não vê
    nada porque lá não existe RLS.
    """

    def test_publicacao_abre_apenas_a_linha_do_identificador_apresentado(self):
        sql = " ".join(policy_statements("scrapers_publicacao", mixed=False))
        self.assertIn("app.public_link", sql)
        self.assertIn("app.public_link_signature", sql)
        # Assinada pelo mesmo HMAC das outras: um GUC plantado sem a chave não vale.
        self.assertIn("tenant_security.context_valid", sql)
        # Só o que já foi publicado, e só a linha cujo identificador veio na URL.
        self.assertIn("status = 'enviado'", sql)
        self.assertIn("slug_curto", sql)
        self.assertIn("id_publico", sql)

    def test_escrita_da_publicacao_continua_exigindo_tenant(self):
        statements = policy_statements("scrapers_publicacao", mixed=False)
        for sql in statements:
            if "FOR SELECT" in sql:
                continue
            self.assertNotIn(
                "app.public_link", sql,
                "o contexto público não pode liberar escrita em publicação",
            )

    def test_clique_so_entra_amarrado_a_publicacao_liberada(self):
        statements = policy_statements("scrapers_cliquepublicacao", mixed=False)
        insert = next(s for s in statements if "FOR INSERT" in s)
        select = next(s for s in statements if "FOR SELECT" in s)
        # O clique nasce da request anônima...
        self.assertIn("app.public_link", insert)
        self.assertIn("scrapers_publicacao", insert)
        self.assertIn("pub.id = publicacao_id", insert)
        # ...mas lê-lo continua sendo privilégio do dono.
        self.assertNotIn("app.public_link", select)


class ContextoLinkPublicoTests(TestCase):
    def test_instala_e_derruba_os_gucs_do_link_publico(self):
        from apps.accounts.tenant import public_link_context

        if connection.vendor != "postgresql":
            self.skipTest("GUC assinado só existe no PostgreSQL")

        with public_link_context("abc123"):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_setting('app.public_link', true), "
                    "current_setting('app.public_link_signature', true)"
                )
                valor, assinatura = cursor.fetchone()
        self.assertEqual(valor, "abc123")
        self.assertEqual(len(assinatura or ""), 64)

        # `local=TRUE` dentro de atomic: com CONN_MAX_AGE=600 a conexão volta para
        # o pool, e um GUC de sessão sobreviveria para a request de outra pessoa.
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_setting('app.public_link', true)")
            self.assertIn(cursor.fetchone()[0], ("", None))

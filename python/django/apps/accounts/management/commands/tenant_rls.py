import hashlib
import re
import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import OperationalError, connection, transaction

from apps.accounts.rls import (
    ALL_TENANT_TABLES,
    CONTROL_TENANT_TABLES,
    MIXED_TENANT_TABLES,
    SYSTEM_ONLY_TABLES,
    policy_statements,
)


# O --enable roda no release_command de TODO deploy, com o release ANTERIOR ainda
# servindo tráfego. Cada ALTER TABLE pede AccessExclusiveLock e o lote inteiro vive
# numa transação só, então basta uma transação do app tocar duas dessas tabelas na
# ordem inversa para fechar o ciclo: em 18/08 dois deploys seguidos morreram com
# "deadlock detected" entre scrapers_produtocupom e scrapers_produto. Ordenar as
# tabelas não resolve — quem escolhe a ordem do outro lado é o tráfego. A saída é o
# DDL desistir rápido e tentar de novo: com lock_timeout ele nunca fica na fila
# atrás de uma transação viva (o que congelaria o app inteiro atrás dele), e tanto o
# timeout quanto o deadlock viram uma tentativa perdida em vez de um deploy abortado.
#
# Faltava, porém, a parte que torna o retry suficiente. Duas coisas, ambas medidas
# em 06/09/2026, quando DOIS deploys seguidos abortaram no release com "8 tentativas
# disputaram lock com o tráfego vivo":
#
#   1. O lote inteiro vivia numa transação só. Um conflito em qualquer uma das 34
#      tabelas desfazia as 34 e a tentativa seguinte recomeçava do zero — contra um
#      worker que roda oito loops sem parar, a chance de 34 locks exclusivos caberem
#      na mesma janela de 3s é pequena, e cai a cada tabela nova. Por tabela, cada
#      transação precisa de UM lock por alguns milissegundos.
#
#   2. Quase todo esse DDL era no-op. As políticas já estavam aplicadas desde o
#      primeiro deploy; ainda assim, todo release reescrevia as quatro políticas e
#      pedia AccessExclusiveLock em cada tabela para reafirmar um ENABLE que já
#      valia. Uma impressão digital do SQL gravada como COMMENT da tabela (que pega
#      ShareUpdateExclusiveLock, e não bloqueia leitura nem escrita) deixa o caso
#      comum — nada mudou — custar zero lock exclusivo.
#
# A rede de segurança é a verificação final: RLS é fronteira de segurança, então
# ninguém sai daqui sem `relrowsecurity` E `relforcerowsecurity` em todas as tabelas.
# O que deixou de abortar o deploy foi reaplicar política já correta; tabela
# desprotegida continua sendo falha dura.
# Prefixo do COMMENT que guarda a impressão digital do DDL desta tabela.
_MARCA_PREFIXO = "spreading-rls:"
_LOCK_TIMEOUT = "3s"
_TENTATIVAS = 8
_ESPERA_S = 5


class Command(BaseCommand):
    help = "Habilita, desabilita ou inspeciona o RLS multi-tenant."

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--enable", action="store_true")
        action.add_argument("--disable", action="store_true")
        action.add_argument("--status", action="store_true")
        # Instala SÓ o schema `tenant_security` (segredo + verificador HMAC) e
        # sai. Existe porque um banco vazio não consegue migrar: a 0061 cria
        # policies que chamam `tenant_security.context_valid`, e quem cria esse
        # schema é o --enable, que só roda DEPOIS do migrate. Em produção isso
        # nunca apareceu porque o schema já existia de um deploy anterior — mas
        # provisionar do zero, ou restaurar um backup num banco novo, quebra em
        # `schema "tenant_security" does not exist`.
        action.add_argument("--only-context", action="store_true")
        parser.add_argument(
            "--system-role", default=settings.TENANT_SYSTEM_DB_ROLE,
        )
        parser.add_argument(
            "--migration-role", default=settings.TENANT_MIGRATION_DB_ROLE,
        )

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            if options["status"]:
                self.stdout.write("RLS não aplicável: backend não é PostgreSQL.")
                return
            raise CommandError("RLS só pode ser alterado no PostgreSQL.")

        if options["status"]:
            self._status()
            return

        system_role = options["system_role"]
        migration_role = options["migration_role"]
        for role in (system_role, migration_role):
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
                raise CommandError(f"Nome de role inválido: {role!r}")

        if options["enable"] or options["only_context"]:
            self._com_retry(
                "contexto assinado",
                lambda: self._instalar_contexto(system_role, migration_role),
            )
        if options["only_context"]:
            self.stdout.write(
                "Contexto assinado instalado; nenhuma policy tocada."
            )
            return

        aplicadas = puladas = 0
        for table in ALL_TENANT_TABLES:
            mudou = self._com_retry(
                f'tabela "{table}"',
                lambda t=table: self._aplicar_tabela(
                    t, options, system_role, migration_role),
            )
            if mudou:
                aplicadas += 1
            else:
                puladas += 1

        if options["enable"]:
            self._exigir_protecao_total()

        self.stdout.write(self.style.SUCCESS(
            f"RLS {'habilitado e forçado' if options['enable'] else 'desabilitado'}: "
            f"{aplicadas} tabela(s) alterada(s), {puladas} já em dia, "
            f"{len(ALL_TENANT_TABLES)} no total."
        ))

    def _com_retry(self, alvo, funcao):
        """Executa uma unidade de DDL, cedendo a vez ao tráfego e voltando depois."""
        for tentativa in range(1, _TENTATIVAS + 1):
            try:
                return funcao()
            except OperationalError as e:
                # Deadlock e lock_timeout são a MESMA situação vista de dois
                # ângulos: uma transação viva estava no caminho. Qualquer outro
                # OperationalError (conexão caída, permissão) sobe e aborta.
                texto = str(e).lower()
                if not ("deadlock" in texto or "lock timeout" in texto
                        or "canceling statement due to lock" in texto):
                    raise
                if tentativa == _TENTATIVAS:
                    raise CommandError(
                        f"RLS não aplicado em {alvo}: {_TENTATIVAS} tentativas "
                        f"disputaram lock com o tráfego vivo. Último erro: {e}"
                    ) from e
                self.stdout.write(
                    f"Lock disputado em {alvo} (tentativa {tentativa}/{_TENTATIVAS}); "
                    f"nova tentativa em {_ESPERA_S}s."
                )
                time.sleep(_ESPERA_S)
        return False

    def _exigir_protecao_total(self):
        """RLS é fronteira de segurança: ninguém passa daqui com tabela aberta."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relname FROM pg_class WHERE relname = ANY(%s) "
                "AND NOT (relrowsecurity AND relforcerowsecurity)",
                [list(ALL_TENANT_TABLES)],
            )
            desprotegidas = sorted(row[0] for row in cursor.fetchall())
        if desprotegidas:
            raise CommandError(
                "RLS ausente ou não forçado em: " + ", ".join(desprotegidas)
            )

    def _instalar_contexto(self, system_role, migration_role):
        """Verificador HMAC e checagem de roles. Não toca em tabela de dados."""
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
            cursor.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)",
                [[system_role, migration_role]],
            )
            found = {row[0] for row in cursor.fetchall()}
            faltando = {system_role, migration_role} - found
            if faltando:
                raise CommandError(
                    "Roles exigidas pelo RLS não existem: " + ", ".join(sorted(faltando))
                )
            self._install_signed_context(
                cursor, system_role=system_role, migration_role=migration_role)
        return True

    def _aplicar_tabela(self, table, options, system_role, migration_role):
        """DDL de UMA tabela, na própria transação. Devolve se houve mudança.

        Por tabela, e não em lote, porque o lote todo-ou-nada desfazia 34 tabelas
        por causa de um conflito em uma — ver o cabeçalho do módulo.
        """
        if not options["enable"]:
            with transaction.atomic(), connection.cursor() as cursor:
                cursor.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
                cursor.execute(f'ALTER TABLE "{table}" NO FORCE ROW LEVEL SECURITY')
                cursor.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
                cursor.execute(f'COMMENT ON TABLE "{table}" IS NULL')
            return True

        sqls = policy_statements(
            table,
            mixed=table in MIXED_TENANT_TABLES,
            system_only=table in SYSTEM_ONLY_TABLES,
            system_role=system_role,
            migration_role=migration_role,
        )
        digital = _MARCA_PREFIXO + hashlib.sha256(
            "|".join(sqls).encode("utf-8")).hexdigest()[:32]

        # Caso comum: nada mudou desde o último deploy. Ler o COMMENT não pega lock
        # nenhum, e sair aqui evita o AccessExclusiveLock que reafirmaria o que já
        # vale. É isto que faz o release parar de brigar com o tráfego.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT obj_description(%s::regclass, 'pg_class'), "
                "       relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE oid = %s::regclass",
                [table, table],
            )
            linha = cursor.fetchone()
        if linha and linha[0] == digital and linha[1] and linha[2]:
            return False

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
            for sql in sqls:
                cursor.execute(sql)
            # A marca entra na MESMA transação do DDL: ou os dois valem, ou nenhum.
            # Gravada separada do SQL, ela poderia sobreviver a um rollback e fazer
            # o próximo deploy pular uma tabela que ficou sem política.
            cursor.execute(f'COMMENT ON TABLE "{table}" IS %s', [digital])
        return True

    def _install_signed_context(self, cursor, *, system_role, migration_role):
        """Instala o verificador HMAC sem conceder acesso ao segredo."""
        secret = settings.TENANT_CONTEXT_SIGNING_KEY
        if not secret:
            raise CommandError(
                "TENANT_CONTEXT_SIGNING_KEY é obrigatória para habilitar RLS."
            )

        runtime_role = settings.TENANT_RUNTIME_DB_ROLE
        for role in (runtime_role, system_role, migration_role):
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", role):
                raise CommandError(f"Nome de role inválido: {role!r}")
        qn = connection.ops.quote_name

        cursor.execute("SELECT current_user")
        if cursor.fetchone()[0] != migration_role:
            raise CommandError(
                "O contexto RLS assinado só pode ser instalado pela role de migração."
            )

        # public deixa de ser gravável por roles não confiáveis antes de carregar
        # pgcrypto, conforme a recomendação de segurança de extensions/functions.
        cursor.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        cursor.execute(
            f"REVOKE CREATE ON SCHEMA public FROM "
            f"{qn(runtime_role)}, {qn(system_role)}"
        )
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto WITH SCHEMA public")
        cursor.execute(
            """
            SELECT n.nspname
              FROM pg_extension e
              JOIN pg_namespace n ON n.oid = e.extnamespace
             WHERE e.extname = 'pgcrypto'
            """
        )
        row = cursor.fetchone()
        if not row or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", row[0]):
            raise CommandError("Schema do pgcrypto ausente ou inválido.")
        crypto_schema = row[0]
        cursor.execute(
            """
            SELECT has_schema_privilege(%s, %s, 'CREATE'),
                   has_schema_privilege(%s, %s, 'CREATE')
            """,
            [runtime_role, crypto_schema, system_role, crypto_schema],
        )
        if any(cursor.fetchone()):
            raise CommandError(
                "O schema do pgcrypto é gravável por uma role não confiável."
            )

        cursor.execute(
            f"CREATE SCHEMA IF NOT EXISTS tenant_security "
            f"AUTHORIZATION {qn(migration_role)}"
        )
        cursor.execute("REVOKE ALL ON SCHEMA tenant_security FROM PUBLIC")
        cursor.execute(
            f"GRANT USAGE ON SCHEMA tenant_security TO "
            f"{qn(runtime_role)}, {qn(system_role)}"
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tenant_security.context_secret (
                singleton boolean PRIMARY KEY DEFAULT TRUE
                    CHECK (singleton),
                secret text NOT NULL CHECK (length(secret) >= 43)
            )
            """
        )
        cursor.execute(
            """
            INSERT INTO tenant_security.context_secret (singleton, secret)
            VALUES (TRUE, %s)
            ON CONFLICT (singleton)
            DO UPDATE SET secret = EXCLUDED.secret
            """,
            [secret],
        )
        cursor.execute(
            f"REVOKE ALL ON tenant_security.context_secret FROM PUBLIC, "
            f"{qn(runtime_role)}, {qn(system_role)}"
        )

        # O segredo permanece em tabela sem grants. A policy chama somente este
        # verificador SECURITY DEFINER, com search_path fixo e HMAC qualificado.
        cursor.execute(
            f"""
            CREATE OR REPLACE FUNCTION tenant_security.context_valid(
                context_kind text,
                context_value text,
                supplied_signature text
            )
            RETURNS boolean
            LANGUAGE sql
            STABLE
            PARALLEL UNSAFE
            SECURITY DEFINER
            SET search_path = pg_catalog, pg_temp
            AS $tenant_context$
                SELECT CASE
                    WHEN supplied_signature ~ '^[0-9a-f]{{64}}$' THEN
                        pg_catalog.decode(supplied_signature, 'hex') =
                        {qn(crypto_schema)}.hmac(
                            pg_catalog.convert_to(
                                context_kind || ':' || context_value,
                                'UTF8'
                            ),
                            pg_catalog.convert_to(secret, 'UTF8'),
                            'sha256'
                        )
                    ELSE FALSE
                END
                FROM tenant_security.context_secret
                WHERE singleton = TRUE
            $tenant_context$
            """
        )
        cursor.execute(
            """
            REVOKE ALL ON FUNCTION
                tenant_security.context_valid(text, text, text)
            FROM PUBLIC
            """
        )
        cursor.execute(
            f"""
            GRANT EXECUTE ON FUNCTION
                tenant_security.context_valid(text, text, text)
            TO {qn(runtime_role)}, {qn(system_role)}, {qn(migration_role)}
            """
        )

    def _status(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema()
                   AND c.relname = ANY(%s)
                 ORDER BY c.relname
                """,
                [list(ALL_TENANT_TABLES)],
            )
            rows = cursor.fetchall()
            cursor.execute(
                """
                SELECT tablename, policyname, cmd,
                       COALESCE(qual, ''), COALESCE(with_check, '')
                  FROM pg_policies
                 WHERE schemaname = current_schema()
                   AND tablename = ANY(%s)
                 ORDER BY tablename, policyname
                """,
                [list(ALL_TENANT_TABLES)],
            )
            policies = cursor.fetchall()
            cursor.execute(
                """
                SELECT p.prosecdef,
                       owner.rolname,
                       COALESCE(array_to_string(p.proconfig, ','), ''),
                       has_function_privilege(%s, p.oid, 'EXECUTE'),
                       has_function_privilege(%s, p.oid, 'EXECUTE'),
                       NOT EXISTS (
                           SELECT 1
                             FROM aclexplode(
                                 COALESCE(p.proacl, acldefault('f', p.proowner))
                             ) acl
                            WHERE acl.grantee = 0
                              AND acl.privilege_type = 'EXECUTE'
                       ),
                       pg_get_functiondef(p.oid)
                  FROM pg_proc p
                  JOIN pg_namespace n ON n.oid = p.pronamespace
                  JOIN pg_roles owner ON owner.oid = p.proowner
                 WHERE n.nspname = 'tenant_security'
                   AND p.proname = 'context_valid'
                   AND p.pronargs = 3
                """,
                [
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_SYSTEM_DB_ROLE,
                ],
            )
            context_function = cursor.fetchone()
            cursor.execute(
                """
                SELECT has_schema_privilege(%s, 'tenant_security', 'USAGE'),
                       has_schema_privilege(%s, 'tenant_security', 'CREATE'),
                       has_table_privilege(
                           %s,
                           'tenant_security.context_secret',
                           'SELECT'
                       ),
                       has_schema_privilege(%s, 'tenant_security', 'USAGE'),
                       has_table_privilege(
                           %s,
                           'tenant_security.context_secret',
                           'SELECT'
                       )
                """,
                [
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_RUNTIME_DB_ROLE,
                    settings.TENANT_SYSTEM_DB_ROLE,
                    settings.TENANT_SYSTEM_DB_ROLE,
                ],
            )
            context_privileges = cursor.fetchone()
        found = {name for name, _, _ in rows}
        for name, enabled, forced in rows:
            self.stdout.write(
                f"{name}: enabled={str(enabled).lower()} forced={str(forced).lower()}"
            )
        missing = sorted(set(ALL_TENANT_TABLES) - found)
        if missing:
            raise CommandError(f"Tabelas ausentes: {', '.join(missing)}")
        if any(not enabled or not forced for _, enabled, forced in rows):
            raise CommandError("RLS ainda não está ENABLE + FORCE em todas as tabelas.")

        by_table = {}
        for table, name, command, using, with_check in policies:
            by_table.setdefault(table, {})[name] = (
                command,
                using,
                with_check,
            )
        expected = {
            "tenant_select": "SELECT",
            "tenant_insert": "INSERT",
            "tenant_update": "UPDATE",
            "tenant_delete": "DELETE",
        }
        policy_errors = []
        for table in ALL_TENANT_TABLES:
            table_policies = by_table.get(table, {})
            if set(table_policies) != set(expected):
                policy_errors.append(f"{table}: conjunto de policies divergente")
                continue
            # Tabela SYSTEM_ONLY não tem — e não pode ter — cláusula de
            # organização: o acesso dela é só contexto de sistema assinado, que
            # é MAIS restrito que o das tabelas tenant. Exigir
            # `app.organization_id` aqui reprovava uma política correta e
            # deixava `--status` permanentemente vermelho, o que é pior que não
            # checar: uma regressão de verdade nessas duas tabelas ficaria
            # indistinguível do falso positivo.
            somente_sistema = table in SYSTEM_ONLY_TABLES
            exigidas = (
                ["app.system_context", "app.system_signature",
                 "tenant_security.context_valid"]
                if somente_sistema
                else ["app.organization_id", "app.organization_signature",
                      "app.system_context", "app.system_signature",
                      "tenant_security.context_valid"]
            )
            for name, command in expected.items():
                actual_command, using, with_check = table_policies[name]
                expression = f"{using} {with_check}"
                if actual_command != command:
                    policy_errors.append(f"{table}.{name}: comando divergente")
                if (
                    any(termo not in expression for termo in exigidas)
                    or "CURRENT_USER" not in expression.upper()
                ):
                    policy_errors.append(
                        f"{table}.{name}: expressão fail-closed ausente"
                    )
                if somente_sistema and "app.organization_id" in expression:
                    policy_errors.append(
                        f"{table}.{name}: tabela de sistema não pode abrir por organização"
                    )
            select_expression = " ".join(table_policies["tenant_select"][1:])
            if table in CONTROL_TENANT_TABLES and (
                "app.actor_id" not in select_expression
                or "app.actor_signature" not in select_expression
            ):
                policy_errors.append(
                    f"{table}.tenant_select: contexto de ator assinado ausente"
                )
            public_visible = "organization_id IS NULL" in select_expression
            if public_visible != (table in MIXED_TENANT_TABLES):
                policy_errors.append(
                    f"{table}.tenant_select: visibilidade pública divergente"
                )
        if policy_errors:
            raise CommandError(
                "Policies RLS inválidas: " + "; ".join(policy_errors[:8])
            )
        if not context_function:
            raise CommandError("Função de contexto RLS assinado ausente.")
        (
            security_definer,
            function_owner,
            function_config,
            runtime_execute,
            system_execute,
            public_revoked,
            function_definition,
        ) = context_function
        if (
            not security_definer
            or function_owner != settings.TENANT_MIGRATION_DB_ROLE
            or "search_path=pg_catalog, pg_temp" not in function_config
            or not runtime_execute
            or not system_execute
            or not public_revoked
            or ".hmac(" not in function_definition
            or "tenant_security.context_secret" not in function_definition
        ):
            raise CommandError("Função de contexto RLS assinado insegura.")
        (
            runtime_usage,
            runtime_create,
            runtime_secret_read,
            system_usage,
            system_secret_read,
        ) = context_privileges
        if (
            not runtime_usage
            or runtime_create
            or runtime_secret_read
            or not system_usage
            or system_secret_read
        ):
            raise CommandError(
                "Privilégios do segredo de contexto RLS estão inseguros."
            )
        self.stdout.write(self.style.SUCCESS(
            "Contexto tenant assinado por HMAC e segredo inacessível às roles "
            "runtime/system."
        ))

"""Definição central das tabelas protegidas por RLS."""

import re

STRICT_TENANT_TABLES = (
    "accounts_perfil",
    "accounts_mercadolivresession",
    "accounts_browsersession",
    "accounts_whatsappconnection",
    "accounts_organizationfeatureoverride",
    "scrapers_integracaoafiliado",
    "scrapers_programaafiliado",
    "scrapers_historicoenvio",
    "scrapers_publicacao",
    "scrapers_cliquepublicacao",
    "scrapers_linkafiliadocupomusuario",
    "scrapers_linkafiliadoprodutocupomusuario",
    "scrapers_receitaafiliado",
    "scrapers_relatoriosync",
    "scrapers_linkafiliadousuario",
    "scrapers_canalmonitorado",
    "scrapers_enviocanal",
    "scrapers_configuracaoenvio",
    "scrapers_execucaoraspagem",
    "scrapers_eventoraspagem",
    "scrapers_cupomdisponibilidade",
    "scrapers_cupomdisponibilidadeevento",
    "scrapers_cupomvalidacao",
    "scrapers_publicacaotentativa",
    "scrapers_publicacaoevento",
)

MIXED_TENANT_TABLES = (
    "scrapers_produto",
    "scrapers_cupomnormalizado",
    "scrapers_cupompreparacao",
    "scrapers_produtocupom",
    "scrapers_execucaoingestao",
    "scrapers_eventooperacional",
    "scrapers_incidentesaude",
    "scrapers_cupomfonteobservacao",
)

SYSTEM_ONLY_TABLES = (
    "scrapers_workerheartbeat",
    "scrapers_resourcelease",
)

CONTROL_TENANT_TABLES = (
    "accounts_organization",
    "accounts_membership",
)

ALL_TENANT_TABLES = (
    STRICT_TENANT_TABLES + MIXED_TENANT_TABLES + CONTROL_TENANT_TABLES
    + SYSTEM_ONLY_TABLES
)


def organization_expr(column: str = "organization_id") -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_.]{0,127}", column):
        raise ValueError(f"Coluna de organização inválida: {column!r}")
    return (
        f"({column} = "
        "NULLIF(current_setting('app.organization_id', true), '')::uuid "
        "AND (SELECT tenant_security.context_valid("
        "'organization', "
        "current_setting('app.organization_id', true), "
        "current_setting('app.organization_signature', true)"
        ")))"
    )


ORG_EXPR = organization_expr()

ACTOR_EXPR = (
    "(user_id = "
    "NULLIF(current_setting('app.actor_id', true), '')::bigint "
    "AND (SELECT tenant_security.context_valid("
    "'actor', "
    "current_setting('app.actor_id', true), "
    "current_setting('app.actor_signature', true)"
    ")))"
)

ORGANIZATION_ACTOR_EXPR = (
    "((accounts_organization.personal_owner_id = "
    "NULLIF(current_setting('app.actor_id', true), '')::bigint "
    "OR EXISTS ("
    "SELECT 1 FROM accounts_membership tenant_actor_membership "
    "WHERE tenant_actor_membership.organization_id = accounts_organization.id "
    "AND tenant_actor_membership.user_id = "
    "NULLIF(current_setting('app.actor_id', true), '')::bigint "
    "AND tenant_actor_membership.is_active"
    ")) "
    "AND (SELECT tenant_security.context_valid("
    "'actor', "
    "current_setting('app.actor_id', true), "
    "current_setting('app.actor_signature', true)"
    ")))"
)


# Link publicado: a única leitura anônima que o produto precisa ter.
#
# `redirect_curto` e `redirect_rastreado` respondem a quem clicou numa mensagem e
# não tem sessão, logo não passam pelo OrganizationContextMiddleware e não têm
# `app.organization_id`. Com as duas tabelas em STRICT, a linha ficava invisível e
# TODO link publicado respondia 404 — a receita morria em silêncio, e a suíte não
# via nada porque roda em SQLite, onde não há RLS.
#
# A saída não é afrouxar a policy nem dar contexto de sistema ao gunicorn (a role
# de runtime não pode abrir contexto cross-tenant, e isso é proteção, não
# descuido). É um contexto próprio, assinado pelo mesmo HMAC dos outros, que
# libera EXATAMENTE a linha cujo identificador o visitante já apresentou na URL —
# e só se ela já foi publicada.
def public_link_expr(prefix: str = "") -> str:
    if prefix and not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", prefix):
        raise ValueError(f"Prefixo de tabela inválido: {prefix!r}")
    col = f"{prefix}." if prefix else ""
    valor = "NULLIF(current_setting('app.public_link', true), '')"
    return (
        "("
        f"{col}status = 'enviado' "
        f"AND {valor} IS NOT NULL "
        f"AND ({col}slug_curto = {valor} OR {col}id_publico::text = {valor}) "
        "AND (SELECT tenant_security.context_valid("
        f"'public_link', {valor}, "
        "current_setting('app.public_link_signature', true)"
        "))"
        ")"
    )


PUBLIC_LINK_EXPR = public_link_expr()

# O clique é escrito pela mesma request anônima. Não basta o contexto ser válido:
# a linha inserida tem de apontar para a publicação que o contexto libera.
CLIQUE_PUBLIC_LINK_EXPR = (
    "(EXISTS (SELECT 1 FROM scrapers_publicacao pub "
    f"WHERE pub.id = publicacao_id AND {public_link_expr('pub')}))"
)


def _role_literal(role: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", str(role or "")):
        raise ValueError(f"Nome de role PostgreSQL inválido: {role!r}")
    return f"'{role}'"


def system_expr(system_role: str, migration_role: str) -> str:
    roles = ", ".join(map(_role_literal, (system_role, migration_role)))
    return (
        "current_setting('app.system_context', true) = 'on' "
        f"AND current_user IN ({roles}) "
        "AND (SELECT tenant_security.context_valid("
        "'system', '', current_setting('app.system_signature', true)"
        "))"
    )


def policy_statements(
    table: str,
    *,
    mixed: bool,
    system_only: bool = False,
    system_role: str = "spreading_system",
    migration_role: str = "spreading_migration",
) -> list[str]:
    system = system_expr(system_role, migration_role)
    if system_only:
        visible = writable = f"({system})"
    elif table == "accounts_organization":
        visible = (
            f"(({system}) OR "
            f"{organization_expr('accounts_organization.id')} OR "
            f"{ORGANIZATION_ACTOR_EXPR})"
        )
        writable = (
            f"(({system}) OR "
            f"{organization_expr('accounts_organization.id')})"
        )
    elif table == "accounts_membership":
        visible = f"(({system}) OR {ORG_EXPR} OR {ACTOR_EXPR})"
        writable = f"(({system}) OR {ORG_EXPR})"
    elif table == "accounts_perfil":
        visible = f"(({system}) OR {ORG_EXPR} OR {ACTOR_EXPR})"
        # Perfil continua fisicamente ligado à organização pessoal durante a
        # compatibilidade, mesmo quando o usuário atua em uma organização
        # compartilhada. O actor assinado pode ler/escrever somente a própria linha;
        # a validação de active_organization ainda exige Membership ativa.
        writable = f"(({system}) OR {ORG_EXPR} OR {ACTOR_EXPR})"
    elif table == "scrapers_publicacao":
        # Leitura anônima do link publicado; escrita segue exigindo tenant.
        visible = f"(({system}) OR {ORG_EXPR} OR {PUBLIC_LINK_EXPR})"
        writable = f"(({system}) OR {ORG_EXPR})"
    elif table == "scrapers_cliquepublicacao":
        # O clique nasce da mesma request anônima, e a leitura precisa da MESMA
        # porta — não por escolha de produto, e sim por como o Postgres funciona: o
        # `create()` do Django emite `INSERT ... RETURNING id`, e `RETURNING` exige
        # que a linha nova passe também pela policy de SELECT. Sem isto o INSERT
        # era aceito e o RETURNING recusado, com a mensagem genérica de violação de
        # RLS — provado em produção em 07/09/2026, onde o mesmo INSERT sem
        # `RETURNING` passava.
        #
        # O alcance continua estreito: a cláusula é a mesma do INSERT, então quem
        # tem o link enxerga apenas os cliques daquela publicação. A tabela não
        # guarda IP, cookie nem identificador pessoal (ver `CliquePublicacao`).
        visible = f"(({system}) OR {ORG_EXPR} OR {CLIQUE_PUBLIC_LINK_EXPR})"
        writable = f"(({system}) OR {ORG_EXPR} OR {CLIQUE_PUBLIC_LINK_EXPR})"
    else:
        # O catálogo compartilhado é quase todo público. Colocá-lo primeiro
        # evita até o InitPlan de assinatura para essas linhas; para as privadas,
        # cada assinatura continua obrigatória e validada pelo mesmo HMAC.
        visible = "(organization_id IS NULL OR " if mixed else "("
        visible += f"({system}) OR {ORG_EXPR}"
        visible += ")"
        writable = f"(({system}) OR {ORG_EXPR})"
    return [
        f'DROP POLICY IF EXISTS tenant_select ON "{table}"',
        f'DROP POLICY IF EXISTS tenant_insert ON "{table}"',
        f'DROP POLICY IF EXISTS tenant_update ON "{table}"',
        f'DROP POLICY IF EXISTS tenant_delete ON "{table}"',
        (
            f'CREATE POLICY tenant_select ON "{table}" FOR SELECT '
            f"USING {visible}"
        ),
        (
            f'CREATE POLICY tenant_insert ON "{table}" FOR INSERT '
            f"WITH CHECK {writable}"
        ),
        (
            f'CREATE POLICY tenant_update ON "{table}" FOR UPDATE '
            f"USING {writable} WITH CHECK {writable}"
        ),
        (
            f'CREATE POLICY tenant_delete ON "{table}" FOR DELETE '
            f"USING {writable}"
        ),
        f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
    ]

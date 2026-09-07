"""Prova que o link publicado abre para quem clicou — e nada além dele.

    python manage.py link_publico_probe --slug abc123

Roda com a role de runtime (a mesma do gunicorn) porque é essa a role que atende
o visitante anônimo. Sem contexto nenhum a linha tem de estar invisível; com o
contexto de link público assinado, exatamente aquela linha aparece e o clique
entra. As duas metades importam: a primeira é a proteção multi-tenant, a segunda
é a receita.

Existe porque a suíte roda em SQLite, onde não há RLS: os testes do redirect
passavam verdes enquanto, em produção, todo `/r/<slug>/` respondia 404 e nenhum
clique era gravado. Este comando é o que se pode apontar para o banco de verdade.

Nada é gravado: o clique é inserido dentro de uma transação que sempre desfaz.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.accounts.tenant import public_link_context


class Command(BaseCommand):
    help = "Prova, com a role runtime, que o link publicado abre para o visitante anônimo."

    def add_arguments(self, parser):
        parser.add_argument(
            "--slug", required=True,
            help="slug_curto de uma Publicacao com status 'enviado'.",
        )
        parser.add_argument(
            "--sem-clique", action="store_true",
            help="Só prova a leitura; pula a inserção do clique.",
        )

    def handle(self, *args, **options):
        from apps.scrapers.models import CliquePublicacao, Publicacao

        if connection.vendor != "postgresql":
            raise CommandError("O probe exige PostgreSQL: em SQLite não há RLS para provar.")

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_user, rolsuper, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
            role, superuser, bypass_rls = cursor.fetchone()
        if role != settings.TENANT_RUNTIME_DB_ROLE or superuser or bypass_rls:
            raise CommandError(
                f"O probe tem de rodar com {settings.TENANT_RUNTIME_DB_ROLE} sem "
                f"SUPERUSER/BYPASSRLS; veio {role} (super={superuser}, bypass={bypass_rls}). "
                "Com outra role o resultado não prova nada."
            )

        slug = options["slug"]

        # 1. Sem contexto: a linha não existe para esta role.
        if Publicacao.objects.filter(slug_curto=slug).exists():
            raise CommandError(
                "FALHA: a publicação está visível SEM contexto de link público. "
                "A policy de tenant não está valendo para esta tabela."
            )
        self.stdout.write("ok  sem contexto: invisível (RLS ativo)")

        # 2. Com o contexto assinado: exatamente aquela linha.
        with public_link_context(slug):
            publicacao = Publicacao.objects.filter(
                slug_curto=slug, status="enviado",
            ).first()
            if publicacao is None:
                raise CommandError(
                    f"FALHA: com o contexto de link público, o slug {slug!r} continua "
                    "invisível. Ou a publicação não existe / não está 'enviado', ou a "
                    "policy não foi reaplicada (o release roda tenant_rls --enable)."
                )
            visiveis = Publicacao.objects.count()
            if visiveis != 1:
                raise CommandError(
                    f"FALHA: o contexto abriu {visiveis} publicações; tem de abrir 1. "
                    "A cláusula está larga demais."
                )
            self.stdout.write(
                f"ok  com contexto: 1 publicação visível (id={publicacao.pk})"
            )

            if options["sem_clique"]:
                return

            # 3. O clique da mesma request anônima entra. Desfeito sempre: o probe
            #    prova a permissão, não polui a métrica de cliques do cliente.
            try:
                with transaction.atomic():
                    clique = CliquePublicacao.objects.create(publicacao=publicacao)
                    self.stdout.write(f"ok  clique registrado (id={clique.pk}, desfeito)")
                    transaction.set_rollback(True)
            except Exception as exc:
                raise CommandError(
                    f"FALHA ao registrar o clique: {type(exc).__name__}: {exc}. "
                    "A policy de INSERT de scrapers_cliquepublicacao não está "
                    "amarrada ao contexto de link público."
                ) from exc

        self.stdout.write(self.style.SUCCESS("Link público funcionando de ponta a ponta."))

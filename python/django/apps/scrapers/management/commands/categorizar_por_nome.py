"""Backfill da macro-categoria a partir do nome do produto.

O ciclo de cupons já classifica o que tem par confirmado a cada 15 minutos. Este
comando existe para a primeira passada sobre o que já está no banco, e para rodar
o catálogo inteiro quando se quiser — o ciclo, de propósito, só olha o que muda o
funil hoje.

    python manage.py categorizar_por_nome --dry-run
    python manage.py categorizar_por_nome --apenas-com-cupom
    python manage.py categorizar_por_nome
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.accounts.tenant import system_context
from apps.scrapers.categorizar_por_nome import (
    macro_do_nome, popular_macro_por_nome,
)
from apps.scrapers.models import Produto


class Command(BaseCommand):
    help = "Preenche Produto.macro_categoria vazia a partir do nome."

    def add_arguments(self, parser):
        parser.add_argument("--limite", type=int, default=None)
        parser.add_argument(
            "--apenas-com-cupom", action="store_true",
            help="Só produtos com par confirmado e cupom ativo.")
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Mostra o que seria classificado, sem gravar.")

    def handle(self, *args, **opts):
        with system_context():
            if opts["dry_run"]:
                self._prever(opts)
                return
            n = popular_macro_por_nome(
                limite=opts["limite"],
                apenas_com_cupom=opts["apenas_com_cupom"],
            )
            self.stdout.write(self.style.SUCCESS(
                f"{n} produto(s) classificado(s) pelo nome."))

    def _prever(self, opts):
        qs = Produto.objects.filter(
            Q(macro_categoria__isnull=True) | Q(macro_categoria="")
        ).exclude(nome="")
        if opts["apenas_com_cupom"]:
            qs = qs.filter(
                cupons_normalizados__status="confirmado",
                cupons_normalizados__cupom__estado="ativo",
            ).distinct()
        qs = qs.order_by("-ultima_observacao", "-id")
        if opts["limite"]:
            qs = qs[:opts["limite"]]

        total = classificados = 0
        por_macro = {}
        exemplos = []
        for produto in qs.iterator(chunk_size=500):
            total += 1
            macro = macro_do_nome(produto.nome)
            if not macro:
                continue
            classificados += 1
            por_macro[macro] = por_macro.get(macro, 0) + 1
            if len(exemplos) < 12:
                exemplos.append((macro, produto.nome[:52]))

        self.stdout.write(f"sem macro: {total} | classificáveis: {classificados}")
        for macro, n in sorted(por_macro.items(), key=lambda kv: -kv[1]):
            self.stdout.write(f"  {n:>5}  {macro}")
        if exemplos:
            self.stdout.write("exemplos:")
            for macro, nome in exemplos:
                self.stdout.write(f"  {macro[:34]:<34} <- {nome}")

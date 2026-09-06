"""Servidor HTTP mínimo que expõe a saúde das esteiras do process group `worker`.

    python manage.py worker_health --port 8081

Existe porque o `worker` não serve HTTP e, sem serviço, o Fly não tinha check
nenhum sobre ele: os oito loops podiam estar todos parados e `fly status` seguia
verde, porque o único check do app é o `/healthz` do gunicorn, que roda na OUTRA
máquina. `deploy/fly-nightly-power.sh` herda o mesmo engano — `checks_are_passing`
aprovava o religamento noturno com o check da `web` sozinho.

O sinal não é inventado aqui: cada loop já grava heartbeat a cada ciclo em
`automacao_state`, e `worker_alive()` é a mesma leitura que a tela de Saúde usa.
Este comando só publica isso numa porta.

Um check reprovado NÃO reinicia a máquina — o Fly não reinicia por health check
crítico (é a razão de `wa_supervisor` existir). O que ele faz é parar de mentir:
`fly status` fica vermelho e o religamento noturno enxerga a esteira morta.
"""
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from django.core.management.base import BaseCommand

from apps.scrapers import automacao_state as st

logger = logging.getLogger(__name__)

# Só as esteiras que rodam NESTE process group. `relatorios` mora com o gunicorn
# (monta o volume /data), então cobrá-la aqui daria vermelho permanente.
ESTEIRAS = ("scrape", "envio", "links")


def _diagnostico() -> tuple[bool, dict]:
    """(saudável, corpo). Uma esteira sem heartbeat recente reprova o check."""
    esteiras = {}
    for job in ESTEIRAS:
        try:
            viva = st.worker_alive(job)
        except Exception as exc:  # banco fora do ar é indisponibilidade, não crash
            logger.warning("worker_health: falha ao ler heartbeat de %s: %s", job, exc)
            esteiras[job] = {"viva": False, "erro": str(exc)}
            continue
        esteiras[job] = {"viva": viva}
    ok = all(e.get("viva") for e in esteiras.values())
    return ok, {"ok": ok, "esteiras": esteiras, "stale_s": st.HEARTBEAT_STALE}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok, corpo = _diagnostico()
        payload = json.dumps(corpo).encode("utf-8")
        self.send_response(200 if ok else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        """Silencia o log por request: o Fly bate a cada 15s, para sempre."""


class Command(BaseCommand):
    help = "Publica a saúde das esteiras do worker numa porta HTTP, para o check do Fly."

    def add_arguments(self, parser):
        parser.add_argument("--port", type=int, default=8081)
        parser.add_argument("--bind", default="0.0.0.0")

    def handle(self, *args, **opts):
        servidor = ThreadingHTTPServer((opts["bind"], opts["port"]), _Handler)
        # daemon_threads: uma request pendurada não pode impedir o desligamento
        # do processo e travar o SIGTERM do honcho.
        servidor.daemon_threads = True
        logger.info(
            "worker_health no ar em %s:%s — esteiras %s",
            opts["bind"], opts["port"], ", ".join(ESTEIRAS),
        )
        try:
            servidor.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            servidor.server_close()

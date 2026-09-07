"""Worker que lê canais curados (Telegram) e re-divulga com a tag do dono (B4).

Usa Telethon (userbot MTProto): uma CONTA de usuário entra nos canais-fonte e lê as
mensagens novas. Para cada mensagem com link de produto, troca a URL pela versão
afiliada do dono do CanalMonitorado e envia ao grupo de destino (WhatsApp/Telegram),
com dedup por URL-fonte (EnvioCanal).

Requer settings.TELEGRAM_API_ID/API_HASH/SESSION. Sem eles, o worker fica ocioso.
Rode:  python manage.py monitorar_canais --tick 60
"""
import logging
import time
import traceback

from django.conf import settings
from django.core.management.base import BaseCommand
from apps.accounts.tenant import system_job

from apps.scrapers import automacao_state as st

logger = logging.getLogger(__name__)

# Esta lane PUBLICA em grupo e, até agora, não gravava batimento nenhum: não
# aparecia na tela de Saúde nem no check das esteiras. A única lane que fala com o
# mundo lá fora era também a única invisível.
JOB = "canais"

# Menor que o HEARTBEAT_STALE de 90s: um batimento a cada 300s apareceria morto
# entre uma escrita e outra.
_INTERVALO_OCIOSO = 30


def _dormir_batendo(segundos: int):
    """Dorme em fatias, renovando o batimento — o `--tick` pode passar de 90s.

    As outras lanes têm POLL curto e batem naturalmente. Esta espera o tick
    inteiro entre varreduras; dormir de uma vez faria o check acusar morte de um
    worker perfeitamente vivo.
    """
    restante = max(0, int(segundos))
    while restante > 0:
        fatia = min(_INTERVALO_OCIOSO, restante)
        time.sleep(fatia)
        restante -= fatia
        if restante > 0:
            st.write_state(JOB, fase="aguardando")


class Command(BaseCommand):
    help = "Lê canais curados no Telegram e re-divulga com a tag de afiliado do dono."

    def add_arguments(self, parser):
        parser.add_argument("--tick", type=int, default=60,
                            help="Segundos entre varreduras dos canais.")

    @system_job
    def handle(self, *args, **opts):
        if not settings.TELETHON_RELINK_ENABLED:
            logger.info("Telethon/relink desativado por política; worker ocioso")
            while True:
                # Ocioso por política é estado saudável, e o batimento sai mesmo
                # assim — senão "desligado de propósito" e "morto" ficam
                # indistinguíveis para quem olha de fora.
                st.write_state(JOB, fase="desligado",
                               ultima_msg="Relink de canais desativado por política.")
                time.sleep(_INTERVALO_OCIOSO)
        if not (settings.TELEGRAM_API_ID and settings.TELEGRAM_API_HASH
                and settings.TELEGRAM_SESSION):
            logger.info("Telegram userbot nao configurado; worker ocioso")
            # Fica vivo mas ocioso (honcho reinicia se sair); evita crash-loop.
            while True:
                st.write_state(JOB, fase="desligado",
                               ultima_msg="Telegram userbot não configurado.")
                time.sleep(_INTERVALO_OCIOSO)

        # Import tardio: só exige telethon quando de fato configurado.
        from telethon.sync import TelegramClient
        from telethon.sessions import StringSession

        tick = max(10, opts["tick"])
        logger.info("Worker de canais no ar; varre a cada %ss", tick)
        client = TelegramClient(
            StringSession(settings.TELEGRAM_SESSION),
            settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH,
        )
        client.start()
        try:
            while True:
                st.write_state(JOB, fase="varrendo", erro="")
                try:
                    self._varrer(client)
                    st.write_state(JOB, fase="aguardando", erro="")
                except Exception:
                    logger.error("Erro na varredura de canais:\n%s", traceback.format_exc())
                    st.write_state(JOB, fase="aguardando",
                                   erro="falha na varredura de canais")
                _dormir_batendo(tick)
        finally:
            client.disconnect()

    def _varrer(self, client):
        from apps.scrapers.models import CanalMonitorado, EnvioCanal
        from apps.scrapers.canais.relink import (
            extrair_urls, reescrever_mensagem_detalhada,
        )
        from apps.scrapers.senders.registry import get_sender

        for canal in CanalMonitorado.objects.filter(ativo=True).select_related("owner"):
            try:
                self._processar_canal(client, canal, EnvioCanal,
                                      reescrever_mensagem_detalhada,
                                      get_sender, extrair_urls)
            except Exception as e:
                logger.warning("Falha no canal %s: %s", canal.handle, e)

    def _processar_canal(self, client, canal, EnvioCanal, reescrever_mensagem,
                         get_sender, extrair_urls):
        from django.db.models import Q
        from django.utils import timezone
        from apps.scrapers.canais.validacao import INCERTO, mensagem_liberada
        from apps.scrapers.models import Publicacao
        from apps.scrapers.eventos import log_event

        sender = get_sender(canal.destino_canal)
        maior_id = canal.ultimo_id
        # reverse=True: da mais antiga p/ a mais nova entre as não vistas (min_id).
        for msg in client.iter_messages(canal.handle, min_id=canal.ultimo_id,
                                        reverse=True, limit=50):
            texto = msg.message or ""
            if not texto:
                maior_id = max(maior_id, msg.id)
                continue
            novo_texto, chaves, pares = reescrever_mensagem(texto, canal.owner)
            if extrair_urls(texto) and not chaves:
                raise RuntimeError("Mensagem contém oferta, mas nenhum link afiliado foi gerado")
            if not chaves:
                maior_id = max(maior_id, msg.id)
                continue  # nenhuma URL de produto re-linkada

            # PORTÃO DE REPUTAÇÃO. O texto é de um estranho e a assinatura é do
            # usuário: nada sai sem que o destino tenha sido aberto e aprovado agora.
            liberada, veredito, motivo_verificacao = mensagem_liberada(
                pares, usuario=canal.owner,
            )
            if not liberada:
                if veredito == INCERTO:
                    # Incerto NÃO avança `ultimo_id`: a mensagem volta no próximo
                    # ciclo, quando o destino talvez responda. Avançar aqui perderia
                    # ofertas boas sempre que o marketplace apertasse o anti-bot.
                    logger.info(
                        "Canal %s: mensagem %s adiada — %s",
                        canal.handle, msg.id, motivo_verificacao,
                    )
                    break
                logger.info(
                    "Canal %s: mensagem %s descartada — %s",
                    canal.handle, msg.id, motivo_verificacao,
                )
                log_event(
                    "publicacao", "canal_oferta_reprovada",
                    f"Oferta de @{canal.handle} não passou na conferência: "
                    f"{motivo_verificacao}",
                    level="info", usuario=canal.owner,
                    contexto={"canal": canal.handle, "mensagem": msg.id},
                )
                maior_id = max(maior_id, msg.id)
                continue
            maior_id = max(maior_id, msg.id)
            # Dedup: já divulgou alguma dessas ofertas p/ este dono?
            ja = set(EnvioCanal.objects.filter(owner=canal.owner, chave__in=chaves)
                     .values_list("chave", flat=True))
            novas = [c for c in chaves if c not in ja]
            if not novas:
                continue
            perfil = getattr(canal.owner, "perfil", None)
            if perfil and perfil.bloqueado:
                logger.info("Canal %s pulado: conta bloqueada", canal.handle)
                continue
            limite = perfil.cota_max_envios_dia() if perfil else 0
            inicio_dia = timezone.localtime().replace(hour=0, minute=0, second=0,
                                                       microsecond=0)
            if limite and Publicacao.objects.filter(
                usuario=canal.owner, criada_em__gte=inicio_dia,
                status__in=("enviado", "incerto", "pendente"),
            ).count() >= limite:
                logger.info("Canal %s pulado: cota diaria atingida", canal.handle)
                continue
            session = perfil.sessao_whatsapp() if perfil else str(canal.owner_id)
            publicacao = Publicacao.objects.filter(
                usuario=canal.owner, origem="canal_monitorado",
                canal=canal.destino_canal, destino_id=canal.destino_grupo_id,
                mensagem=novo_texto, status="pendente",
            ).filter(
                Q(next_retry_at__isnull=True)
                | Q(next_retry_at__lte=timezone.now())
            ).order_by("criada_em").first()
            if publicacao is None:
                publicacao = Publicacao.objects.create(
                    usuario=canal.owner, origem="canal_monitorado",
                    canal=canal.destino_canal,
                    destino_id=canal.destino_grupo_id, destino_nome=canal.handle,
                    mensagem=novo_texto, categoria="Canal monitorado",
                )
            from apps.scrapers.send_pipeline import begin_transport, finish_transport
            publicacao_transporte, tentativa = begin_transport(publicacao)
            resultado = sender.enviar_oferta(
                canal.destino_grupo_id, novo_texto, legenda=novo_texto,
                usuario=canal.owner, session=session,
                operation_id=publicacao_transporte.operation_key)
            finish_transport(
                publicacao_transporte, tentativa, resultado,
                duration_ms=resultado.get("duracao_ms", 0),
            )
            if resultado.get("sucesso"):
                EnvioCanal.objects.bulk_create(
                    [EnvioCanal(owner=canal.owner, chave=c) for c in novas],
                    ignore_conflicts=True,
                )
                log_event("publicacao", "send_ok", "Canal monitorado divulgado.",
                          usuario=canal.owner,
                          contexto={"publicacao_id": publicacao.id,
                                    "canal_monitorado_id": canal.id,
                                    "destino": canal.destino_grupo_id})
                logger.info("Canal %s -> %s divulgado", canal.handle, canal.destino_grupo_id)
            elif resultado.get("resultado") == "incerto":
                # Não retentar: o transporte pode ter entregue antes de perder a confirmação.
                EnvioCanal.objects.bulk_create(
                    [EnvioCanal(owner=canal.owner, chave=c) for c in novas],
                    ignore_conflicts=True,
                )
                log_event("publicacao", "send_failed", "Entrega do canal não confirmada.",
                          level="warning", usuario=canal.owner,
                          contexto={"publicacao_id": publicacao.id,
                                    "resultado": "incerto", "repetir": False})
            else:
                raise RuntimeError(resultado.get("erro") or "Falha no envio do canal")
        # Avança o cursor mesmo sem envio (não reprocessa msgs antigas no restart).
        if maior_id > canal.ultimo_id:
            canal.ultimo_id = maior_id
            canal.save(update_fields=["ultimo_id"])

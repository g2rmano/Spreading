"""Máquina de estados auditável do envio e política única de retry."""
from __future__ import annotations

import hashlib
import base64
import binascii
import io
import logging
import random
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.scrapers.models import Publicacao, PublicacaoEvento, PublicacaoTentativa

logger = logging.getLogger(__name__)


STAGES = (
    "selected", "reserved", "composing", "price_revalidation",
    "media_preparation", "transport_queued", "transport_started",
    "confirmation_pending", "confirmed",
)
TERMINALS = {"rejected", "permanent_failed", "cancelled", "uncertain"}
QUEUE_STATES = {"queued_v2", "retry_wait", "retry_after_restart", "processing_v2"}
_NEXT = {
    "selected": {"reserved", "rejected", "cancelled"},
    "reserved": {"composing", "rejected", "cancelled"},
    "composing": {"price_revalidation", "rejected", "cancelled"},
    "price_revalidation": {"media_preparation", "rejected", "cancelled"},
    "media_preparation": {"transport_queued", "rejected", "cancelled"},
    "transport_queued": {"transport_started", "permanent_failed", "cancelled"},
    "transport_started": {"confirmation_pending", "confirmed", "permanent_failed", "uncertain"},
    "confirmation_pending": {"confirmed", "uncertain"},
    # "incerto" é o desfecho quando o worker estoura o próprio orçamento de 55s no
    # `sendMessage`. Ele diz "não sei", não "não foi" — e o ledger do Node muitas
    # vezes sabe, porque `confirmarMensagem` conclui depois que a nossa leitura já
    # desistiu. Sem esta aresta o registro ficava congelado: em produção, 13 de 129
    # tentativas em dois dias em limbo permanente, sem ninguém para desempatar.
    #
    # A aresta é de mão única e só sobe com PROVA (ledger em `confirmed` com id de
    # mensagem). "incerto" nunca vira "falhou" por aqui, e nada é reenviado: o veto
    # a repetir continua sendo o que impede a oferta de sair duas vezes no grupo.
    "uncertain": {"confirmed"},
}


def operation_key(publicacao):
    raw = (
        f"{publicacao.organization_id}:{publicacao.canal}:"
        f"{publicacao.destino_id}:{publicacao.id_publico}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def initialize_publication(publicacao):
    """Inicialização idempotente usada pelo signal e por backfills."""
    key = publicacao.operation_key or operation_key(publicacao)
    stage = publicacao.stage or "selected"
    if publicacao.status == "enviado":
        stage = "confirmed"
    elif publicacao.status == "incerto":
        stage = "uncertain"
    elif publicacao.status == "falhou" and stage == "selected":
        stage = "permanent_failed"
    elif stage == "selected":
        stage = "reserved"
    Publicacao.objects.filter(pk=publicacao.pk).update(operation_key=key, stage=stage)
    PublicacaoEvento.objects.get_or_create(
        organization_id=publicacao.organization_id, publicacao=publicacao,
        stage=stage, reason_code="publication_created",
        defaults={"safe_detail": "Publicação reservada para processamento."},
    )
    publicacao.operation_key, publicacao.stage = key, stage
    return publicacao


def transition(publicacao, stage, *, reason_code="", safe_detail="", **updates):
    if stage not in STAGES and stage not in TERMINALS:
        raise ValueError(f"Etapa de envio inválida: {stage}")
    with transaction.atomic():
        current = Publicacao.objects.select_for_update().get(pk=publicacao.pk)
        if current.stage == stage:
            return current
        permitido = _NEXT.get(current.stage, set())
        # `confirmed` é final sem exceção. Os demais terminais também são, a menos
        # que exista uma aresta declarada saindo deles — hoje só `uncertain ->
        # confirmed`, a reconciliação pelo ledger.
        if current.stage == "confirmed" or (
                current.stage in TERMINALS and stage not in permitido):
            raise ValueError(f"Publicação terminal em {current.stage}")
        if stage not in permitido:
            raise ValueError(f"Transição inválida: {current.stage} -> {stage}")
        current.stage = stage
        for field, value in updates.items():
            setattr(current, field, value)
        current.save(update_fields=["stage", *updates.keys()])
        PublicacaoEvento.objects.create(
            organization_id=current.organization_id, publicacao=current, stage=stage,
            reason_code=str(reason_code or "")[:64],
            safe_detail=str(safe_detail or "")[:255],
        )
        return current


def _advance(publicacao, target):
    order = list(STAGES)
    current = Publicacao.objects.get(pk=publicacao.pk)
    while current.stage != target:
        current_index = order.index(current.stage)
        target_index = order.index(target)
        if current_index >= target_index:
            break
        current = transition(current, order[current_index + 1])
    return current


def begin_transport(publicacao):
    current = _advance(publicacao, "transport_started")
    numero = current.attempt_count + 1
    attempt, _ = PublicacaoTentativa.objects.get_or_create(
        organization_id=current.organization_id, publicacao=current, numero=numero,
        defaults={"stage": "transport_started"},
    )
    Publicacao.objects.filter(pk=current.pk).update(
        attempt_count=numero, transport_state="started",
    )
    current.attempt_count = numero
    return current, attempt


def classify_result(result):
    result = result or {}
    if result.get("sucesso"):
        return "confirmed"
    if result.get("resultado") == "incerto" or result.get("repetir") is False:
        return "uncertain"
    if result.get("classe") in {"permanente", "permanent"}:
        return "permanent"
    error = str(result.get("erro") or result.get("motivo") or "").casefold()
    explicit_permanent = any(marker in error for marker in (
        "destino inválido", "destino invalido", "payload inválido", "payload invalido",
        "credencial revogada", "credential revoked", "base64 inválido", "base64 invalido",
        "destino não encontrado", "destino nao encontrado", "grupo de destino",
    ))
    return "permanent" if explicit_permanent else "transient"


def retry_delay(attempt_number, random_fn=None):
    base = max(1, int(settings.SEND_RETRY_BASE_SECONDS))
    maximum = max(base, int(settings.SEND_RETRY_MAX_SECONDS))
    cap = min(maximum, base * (2 ** max(0, attempt_number - 1)))
    random_fn = random_fn or random.uniform
    return max(0.0, float(random_fn(0, cap)))


def _validated_media_bytes(image_b64, mime):
    if not image_b64:
        return None, ""
    try:
        raw = base64.b64decode(str(image_b64), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("Mídia base64 inválida.") from exc
    if not raw or len(raw) > 16 * 1024 * 1024:
        raise ValueError("Mídia vazia ou maior que 16 MiB.")
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            image.verify()
            detected = str(image.format or "").upper()
    except Exception as exc:
        raise ValueError("Mídia não é uma imagem decodificável.") from exc
    allowed = {
        "JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp",
    }
    detected_mime = allowed.get(detected)
    requested = str(mime or detected_mime or "").lower()
    if not detected_mime or requested != detected_mime:
        raise ValueError("MIME da mídia não corresponde ao conteúdo.")
    return raw, detected_mime


def queue_publications(publications, *, image_b64=None, mime="image/jpeg",
                       batch_key=""):
    """Marca reservas para consumo assíncrono sem persistir base64 ou mensagem em log."""
    publications = [publication for publication in publications if publication]
    if not publications:
        raise ValueError("Nenhuma publicação reservada para enfileirar.")
    raw, detected_mime = _validated_media_bytes(image_b64, mime)
    batch_key = str(batch_key or uuid.uuid4().hex)[:64]
    now = timezone.now()
    ids = [publication.pk for publication in publications]
    with transaction.atomic():
        locked = list(Publicacao.objects.select_for_update().filter(
            pk__in=ids, status="pendente",
        ))
        if len(locked) != len(ids):
            raise ValueError("Uma das publicações já possui resultado terminal.")
        batch_scopes = {
            (
                publication.organization_id, publication.usuario_id,
                publication.canal, publication.destino_id, publication.origem,
            )
            for publication in locked
        }
        if len(batch_scopes) != 1:
            raise ValueError(
                "Um lote de envio deve pertencer a uma única organização, "
                "usuário, canal, destino e origem."
            )
        Publicacao.objects.filter(pk__in=ids).update(
            transport_state="queued_v2", next_retry_at=now,
            queued_media=raw, queued_media_mime=detected_mime,
            delivery_batch_key=batch_key,
        )
        PublicacaoEvento.objects.bulk_create([
            PublicacaoEvento(
                organization_id=publication.organization_id,
                publicacao_id=publication.pk,
                stage=publication.stage,
                reason_code="queued_for_worker",
                safe_detail="Publicação reservada e aguardando o worker de envio.",
            )
            for publication in locked
        ])
    for publication in publications:
        publication.transport_state = "queued_v2"
        publication.next_retry_at = now
        publication.delivery_batch_key = batch_key
    return batch_key


def queued_media_b64(publication):
    raw = bytes(publication.queued_media or b"")
    return base64.b64encode(raw).decode("ascii") if raw else None


def _contar_falha_na_regra(publication, classification, reason_code=""):
    """Devolve a falha terminal para a regra que a originou.

    Sem isto a válvula de segurança não existe no caminho enfileirado. O núcleo só
    incrementa `falhas_consecutivas` quando o envio termina no mesmo tique; sob a
    fila v2 o resultado imediato é `sucesso=True, queued=True`, e o desfecho real
    acontece aqui, minutos depois, longe de qualquer regra. Uma regra com destino
    inválido re-enfileirava para sempre — `pausar_apos_falhas` e `frear()` nunca
    eram alcançados.

    Só falha TERMINAL conta. Retry ainda em curso não é veredito, e transitório
    nunca contou nem no caminho antigo: é o que desligava a automação de quem não
    tinha defeito nenhum na regra.
    """
    del classification  # o estágio já diz o desfecho; a classificação não basta
    # `uncertain` também é terminal e NÃO é veredito de falha: pode ter chegado ao
    # grupo. Só os dois desfechos que afirmam fracasso contam.
    if publication.stage not in {"permanent_failed", "cancelled"}:
        return
    configuracao = getattr(publication, "configuracao", None)
    if configuracao is None:
        return  # envio manual da tela: não há regra para pausar
    from apps.scrapers.eventos import log_event

    configuracao.falhas_consecutivas = (configuracao.falhas_consecutivas or 0) + 1
    campos = ["falhas_consecutivas"]
    limite = configuracao.pausar_apos_falhas
    if limite and configuracao.falhas_consecutivas >= limite:
        # `frear` só preenche os campos; quem grava é quem chama.
        configuracao.frear(timezone.now(), publication.erro or reason_code)
        campos = ["falhas_consecutivas", "motivo_pausa", "pausada_ate"]
        log_event(
            "publicacao", "config_pausada",
            f"Automação pausada após {configuracao.falhas_consecutivas} falhas na "
            f"fila de envio: {configuracao.motivo_pausa}",
            level="error", usuario=configuracao.owner,
            contexto={"configuracao": configuracao.id,
                      "destino": configuracao.grupo_id,
                      "publicacao_id": publication.pk,
                      "motivo_tecnico": reason_code},
        )
    configuracao.save(update_fields=campos)


def _record_pretransport_failure(publications, result):
    """Classifica uma falha ocorrida antes de ``begin_transport`` e agenda retry."""
    classification = classify_result(result)
    now = timezone.now()
    for publication in publications:
        with transaction.atomic():
            current = Publicacao.objects.select_for_update().get(pk=publication.pk)
            if current.stage in TERMINALS or current.stage == "confirmed":
                continue
            # O núcleo já registrou a tentativa de transporte; não conte a mesma
            # falha de novo. O retry_at produzido por finish_transport é autoritativo.
            if current.transport_state == "retry_wait":
                continue
            numero = current.attempt_count + 1
            attempt = PublicacaoTentativa.objects.create(
                organization_id=current.organization_id,
                publicacao=current,
                numero=numero,
                stage=current.stage,
                classification=classification,
                result=str(result.get("resultado") or classification)[:32],
                reason_code=str(result.get("causa") or result.get("etapa") or "")[:64],
                finished_at=now,
            )
            current.attempt_count = numero
            if classification == "uncertain":
                current.status = "incerto"
                current.stage = "uncertain"
                current.transport_state = "uncertain"
                current.next_retry_at = None
                reason = "pretransport_uncertain"
            elif classification == "permanent":
                current.status = "falhou"
                current.stage = "permanent_failed"
                current.transport_state = "failed"
                current.next_retry_at = None
                reason = "pretransport_permanent"
            elif numero >= int(settings.SEND_MAX_ATTEMPTS):
                current.status = "falhou"
                current.stage = "cancelled"
                current.transport_state = "retry_exhausted"
                current.next_retry_at = None
                reason = "pretransport_retry_exhausted"
            else:
                delay = retry_delay(numero)
                current.status = "pendente"
                current.stage = "transport_queued"
                current.transport_state = "retry_wait"
                current.next_retry_at = now + timedelta(seconds=delay)
                attempt.next_retry_at = current.next_retry_at
                reason = "pretransport_retry"
            current.erro = str(result.get("motivo") or "Falha antes do transporte.")[:500]
            current.save(update_fields=[
                "attempt_count", "status", "stage", "transport_state",
                "next_retry_at", "erro",
            ])
            if current.stage in TERMINALS:
                _contar_falha_na_regra(current, classification, reason)
            attempt.save(update_fields=["next_retry_at"])
            PublicacaoEvento.objects.create(
                organization_id=current.organization_id, publicacao=current,
                stage=current.stage, reason_code=reason,
                safe_detail="Falha classificada antes do início do transporte.",
            )


def _claim_next_batch(now=None):
    now = now or timezone.now()
    pretransport_stages = {
        "reserved", "composing", "price_revalidation", "media_preparation",
        "transport_queued",
    }
    with transaction.atomic():
        candidate = (Publicacao.objects.select_for_update(skip_locked=True)
                     .filter(status="pendente", stage__in=pretransport_stages,
                             transport_state__in=QUEUE_STATES,
                             next_retry_at__lte=now)
                     .order_by("next_retry_at", "criada_em", "pk").first())
        if candidate is None:
            return []
        batch_key = candidate.delivery_batch_key
        batch = list(Publicacao.objects.select_for_update().filter(
            delivery_batch_key=batch_key,
            status="pendente", organization_id=candidate.organization_id,
            usuario_id=candidate.usuario_id, canal=candidate.canal,
            destino_id=candidate.destino_id, origem=candidate.origem,
        ).order_by("pk")) if batch_key else [candidate]
        lease_until = now + timedelta(minutes=5)
        Publicacao.objects.filter(pk__in=[row.pk for row in batch]).update(
            transport_state="processing_v2", next_retry_at=lease_until,
        )
        return [row.pk for row in batch]


def process_queued_publications(limit=20):
    """Consome reservas v2 no worker ``envio`` com escopo explícito por tenant."""
    from apps.accounts.tenant import organization_context
    from apps.scrapers.ofertas import (
        enviar_aviso_cupons, enviar_cupom, enviar_oferta_de_produto,
    )

    outcomes = []
    for _index in range(max(1, int(limit))):
        ids = _claim_next_batch()
        if not ids:
            break
        anchor = Publicacao.objects.select_related("organization").get(pk=ids[0])
        try:
            with organization_context(anchor.organization_id):
                publications = list(Publicacao.objects.select_related(
                    "usuario", "produto", "cupom_normalizado", "configuracao",
                ).filter(pk__in=ids).order_by("pk"))
                first = publications[0]
                image_b64 = queued_media_b64(first)
                if first.origem == "produto" and first.produto_id:
                    result = enviar_oferta_de_produto(
                        first.produto, first.destino_id, verificar=True,
                        canal=first.canal, usuario=first.usuario,
                        configuracao=first.configuracao,
                        destino_nome=first.destino_nome,
                        imagem_b64_custom=image_b64,
                        _reserved_publication=first,
                    )
                elif first.origem == "cupom" and first.cupom_normalizado_id:
                    result = enviar_cupom(
                        first.cupom_normalizado, first.destino_id,
                        canal=first.canal, usuario=first.usuario,
                        configuracao=first.configuracao,
                        destino_nome=first.destino_nome,
                        imagem_b64_custom=image_b64,
                        _reserved_publication=first,
                    )
                elif first.origem == "aviso_cupons":
                    result = enviar_aviso_cupons(
                        [row.cupom_normalizado for row in publications
                         if row.cupom_normalizado_id],
                        first.destino_id, canal=first.canal, usuario=first.usuario,
                        configuracao=first.configuracao,
                        destino_nome=first.destino_nome,
                        _reserved_publications=publications,
                    )
                else:
                    result = {
                        "sucesso": False, "classe": "permanente",
                        "motivo": "Reserva de envio sem conteúdo válido.",
                        "causa": "invalid_queued_publication",
                    }
                if result.get("sucesso") and first.configuracao_id:
                    type(first.configuracao).objects.filter(
                        pk=first.configuracao_id,
                    ).update(
                        ultimo_envio=timezone.now(), falhas_consecutivas=0,
                        motivo_pausa="", pausada_ate=None,
                    )
                elif not result.get("sucesso"):
                    _record_pretransport_failure(publications, result)
                outcomes.append(result)
        except Exception as exc:  # noqa: BLE001 — fila deve sobreviver ao item ruim
            result = {
                "sucesso": False, "classe": "transitorio",
                "motivo": "Falha operacional antes do transporte.",
                "causa": type(exc).__name__,
            }
            with organization_context(anchor.organization_id):
                publications = list(Publicacao.objects.filter(pk__in=ids))
                _record_pretransport_failure(publications, result)
            outcomes.append(result)
        finally:
            # Mídia já não é necessária depois de terminal. Em retry ela permanece
            # para que o mesmo conteúdo possa ser retomado sem tocar no request web.
            terminal = Publicacao.objects.filter(pk__in=ids).exclude(
                stage__in=TERMINALS | {"confirmed"},
            ).exists()
            if not terminal:
                Publicacao.objects.filter(pk__in=ids).update(
                    queued_media=None, queued_media_mime="",
                )
    return outcomes


def finish_transport(publicacao, attempt, result, *, duration_ms=0, random_fn=None):
    classification = classify_result(result)
    now = timezone.now()
    attempt.finished_at = now
    attempt.duration_ms = max(0, int(duration_ms or 0))
    attempt.classification = classification
    attempt.result = str(result.get("resultado") or classification)[:32]
    attempt.reason_code = str(result.get("causa") or result.get("etapa") or "")[:64]

    current = Publicacao.objects.get(pk=publicacao.pk)
    if classification == "confirmed":
        current = transition(
            current, "confirmed", reason_code="transport_confirmed",
            status="enviado", enviada_em=now, transport_state="confirmed",
            duration_ms=attempt.duration_ms, erro="", next_retry_at=None,
        )
    elif classification == "uncertain":
        if current.stage == "transport_started":
            current = transition(current, "confirmation_pending",
                                 reason_code="confirmation_missing")
        current = transition(
            current, "uncertain", reason_code="transport_uncertain",
            safe_detail="Entrega não confirmada; reenvio automático bloqueado.",
            status="incerto", transport_state="uncertain",
            duration_ms=attempt.duration_ms, next_retry_at=None,
        )
    elif classification == "permanent":
        current = transition(
            current, "permanent_failed", reason_code="permanent_transport_failure",
            safe_detail="Destino, payload ou credencial foi rejeitado permanentemente.",
            status="falhou", transport_state="failed",
            duration_ms=attempt.duration_ms, next_retry_at=None,
        )
    elif current.attempt_count >= int(settings.SEND_MAX_ATTEMPTS):
        current = transition(
            current, "cancelled", reason_code="retry_exhausted",
            safe_detail="Tentativas transitórias esgotadas.", status="falhou",
            transport_state="retry_exhausted", duration_ms=attempt.duration_ms,
            next_retry_at=None,
        )
    else:
        delay = retry_delay(current.attempt_count, random_fn=random_fn)
        retry_at = now + timedelta(seconds=delay)
        Publicacao.objects.filter(pk=current.pk).update(
            stage="transport_queued", status="pendente", transport_state="retry_wait",
            duration_ms=attempt.duration_ms, next_retry_at=retry_at,
        )
        PublicacaoEvento.objects.create(
            organization_id=current.organization_id, publicacao=current,
            stage="transport_queued", reason_code="transient_retry",
            safe_detail="Falha transitória; nova tentativa agendada.",
        )
        current.stage = "transport_queued"
        current.next_retry_at = retry_at
        attempt.next_retry_at = retry_at
    attempt.save(update_fields=[
        "finished_at", "duration_ms", "classification", "result", "reason_code",
        "next_retry_at",
    ])
    return current


# Janela de reconciliação. O ledger do Node guarda a operação por tempo limitado e
# uma publicação antiga já foi lida por quem acompanha o grupo: reabrir o assunto
# depois disso não muda decisão nenhuma.
RECONCILIACAO_JANELA = timedelta(hours=6)


def reconciliar_incertos(limit=20, agora=None, consulta=None):
    """Pergunta ao ledger do worker o que houve com os envios 'incerto'.

    O worker WhatsApp tem orçamento próprio de 55s para o `sendMessage`. Quando ele
    estoura, devolve "incerto" — que quer dizer "não sei", não "não foi". A
    confirmação nativa costuma chegar logo depois, e o ledger do Node registra;
    ninguém consultava. Em produção, 13 de 129 tentativas em dois dias ficaram em
    limbo permanente, e quem publica não tinha como saber se a oferta chegou.

    Aqui NADA é reenviado. A função só troca "não sei" por "chegou" quando o ledger
    apresenta a prova (fase ``confirmed`` com id de mensagem). Sem prova, a linha
    fica exatamente como está — o veto a repetir continua sendo o que impede a
    mesma oferta de sair duas vezes no grupo.
    """
    from apps.scrapers import whatsapp_client
    from apps.scrapers.ofertas import wa_session_de

    consultar = consulta or whatsapp_client.consultar_operacao
    agora = agora or timezone.now()
    alvos = Publicacao.objects.filter(
        stage="uncertain", status="incerto",
        criada_em__gte=agora - RECONCILIACAO_JANELA,
    ).exclude(operation_key=None).exclude(operation_key="").select_related(
        "usuario",
    ).order_by("-criada_em")[:max(1, int(limit))]

    confirmadas = consultadas = 0
    for publicacao in alvos:
        consultadas += 1
        try:
            resposta = consultar(
                wa_session_de(publicacao.usuario), publicacao.operation_key)
        except Exception:
            logger.debug("Ledger não respondeu sobre a publicação %s",
                         publicacao.pk, exc_info=True)
            continue
        if not resposta.get("encontrado") or resposta.get("fase") != "confirmed":
            continue
        resultado = resposta.get("resultado") or {}
        if not (resultado.get("sucesso") and resultado.get("mensagem_id")):
            continue
        try:
            transition(
                publicacao, "confirmed", reason_code="ledger_reconciled",
                safe_detail="O worker confirmou a entrega depois do nosso prazo.",
                status="enviado", enviada_em=publicacao.enviada_em or agora,
                transport_state="confirmed", erro="",
            )
        except ValueError:
            # Outro caminho já resolveu esta linha entre a consulta e o update.
            continue
        confirmadas += 1
    if confirmadas:
        logger.info("Reconciliação de envios: %s de %s incerto(s) confirmado(s) "
                    "pelo ledger.", confirmadas, consultadas)
    return {"consultadas": consultadas, "confirmadas": confirmadas}

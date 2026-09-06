"""Contexto fail-closed de organização para HTTP, serviços e workers."""

import asyncio
import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import close_old_connections, connection, connections, transaction
from django.db.backends.signals import connection_created
from django.dispatch import receiver


_current_organization_id = ContextVar("current_organization_id", default=None)
_current_actor_id = ContextVar("current_actor_id", default=None)
_system_context = ContextVar("tenant_system_context", default=False)

# Tenant "guardado": vale para jobs longos que NÃO podem segurar transação (o live
# view do login prende o Chromium por até 10 min). Deliberadamente NÃO é
# _current_organization_id: assim o signal restore_context_on_reconnect ignora este
# estado, e um ORM acidental no meio do job roda sem escopo — a RLS barra — em vez de
# vazar GUC numa conexão do pool que voltará para outro tenant.
_tenant_suspenso = ContextVar("tenant_suspenso", default=None)  # (org_id, actor_id)


class TenantSuspensoORMError(RuntimeError):
    """ORM usado sem reinstalar o tenant de um job longo."""


def _bloquear_orm_com_tenant_apenas_suspenso(execute, sql, params, many, context):
    """Transforma uma futura violação de RLS num erro local e determinístico.

    ``tenant_suspenso`` existe justamente para jobs que não podem manter uma
    transação aberta. Portanto, enquanto ele estiver ativo, SQL de negócio só é
    seguro dentro de ``organization_context``/``system_context`` — normalmente
    instalados por ``executar_no_tenant``. Antes desta guarda, o SQLite dos testes
    aceitava um acesso acidental e apenas o PostgreSQL de produção o recusava.

    As instruções assinadas de infraestrutura que instalam/restauram os próprios
    GUCs são permitidas; elas não leem nem alteram tabelas de negócio.
    """
    sql_normalizado = " ".join(str(sql or "").lower().split())
    instalando_contexto = sql_normalizado.startswith("select set_config(")
    controle_de_transacao = sql_normalizado.startswith((
        "begin", "commit", "rollback", "savepoint", "release savepoint",
    ))
    if (
        _tenant_suspenso.get()
        and not current_organization_id()
        and not in_system_context()
        and not instalando_contexto
        and not controle_de_transacao
    ):
        raise TenantSuspensoORMError(
            "ORM executado com tenant apenas suspenso; use executar_no_tenant()."
        )
    return execute(sql, params, many, context)


def current_organization_id():
    return _current_organization_id.get()


def current_actor_id():
    return _current_actor_id.get()


def in_system_context() -> bool:
    return bool(_system_context.get())


def _context_signature(kind: str, value: str = "") -> str:
    key = settings.TENANT_CONTEXT_SIGNING_KEY
    if not key:
        if settings.APP_ENV in {"staging", "production"}:
            raise ImproperlyConfigured(
                "TENANT_CONTEXT_SIGNING_KEY é obrigatória para o RLS assinado."
            )
        return ""
    message = f"{kind}:{value}".encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _set_postgres_context(*, organization_id=None, system=False, local=True):
    if connection.vendor != "postgresql":
        return
    organization_value = str(organization_id or "")
    organization_signature = (
        _context_signature("organization", organization_value)
        if organization_value and not system
        else ""
    )
    system_signature = _context_signature("system") if system else ""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT set_config('app.organization_id', %s, %s),
                   set_config('app.organization_signature', %s, %s),
                   set_config('app.system_context', %s, %s),
                   set_config('app.system_signature', %s, %s)
            """,
            [
                organization_value,
                local,
                organization_signature,
                local,
                "on" if system else "off",
                local,
                system_signature,
                local,
            ],
        )


def _set_postgres_actor(actor_id=None, *, local=True):
    if connection.vendor != "postgresql":
        return
    actor_value = str(actor_id or "")
    signature = (
        _context_signature("actor", actor_value) if actor_value else ""
    )
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT set_config('app.actor_id', %s, %s),
                   set_config('app.actor_signature', %s, %s)
            """,
            [actor_value, local, signature, local],
        )


def _assert_privileged_database_role():
    if connection.vendor != "postgresql":
        return
    from django.conf import settings
    from django.core.exceptions import PermissionDenied

    allowed = {
        settings.TENANT_SYSTEM_DB_ROLE,
        settings.TENANT_MIGRATION_DB_ROLE,
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT current_user, r.rolsuper, r.rolbypassrls
              FROM pg_roles r
             WHERE r.rolname = current_user
            """
        )
        current, is_superuser, bypass_rls = cursor.fetchone()
    if current not in allowed:
        raise PermissionDenied(
            "A role de banco desta máquina não pode abrir contexto cross-tenant."
        )
    if is_superuser or bypass_rls:
        raise PermissionDenied(
            "A role de sistema não pode ter SUPERUSER ou BYPASSRLS."
        )
    if current == settings.TENANT_SYSTEM_DB_ROLE:
        from .rls import ALL_TENANT_TABLES
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT count(*)
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = current_schema()
                   AND c.relname = ANY(%s)
                   AND c.relowner = (
                       SELECT oid FROM pg_roles WHERE rolname = current_user
                   )
                """,
                [list(ALL_TENANT_TABLES)],
            )
            if cursor.fetchone()[0]:
                raise PermissionDenied(
                    "A role de workers não pode ser dona de tabela tenant."
                )


def _reinstalar_escopo_ambiente():
    """Reaplica nos GUCs o escopo que os ContextVars descrevem NESTE instante.

    Sair de um escopo aninhado zerando os GUCs (`system_context='off'`,
    `organization_id=''`) só está certo quando não havia escopo por baixo. Quando
    havia, o reset apagava o do chamador — e como o ContextVar dele continuava
    intacto, nada nunca o reinstalava: a conexão seguia anônima até o processo
    morrer.

    Era esse o defeito que parava a automação em produção. O worker de envio roda
    inteiro sob `@system_job`; a geração de link chama `executar_no_tenant`, que
    FORA do Playwright roda em linha, na mesma conexão, e abre um
    `organization_context`. Ao sair, ele desligava o `system_context` do worker.
    O `cfg.save()` seguinte caía na RLS como sessão anônima, `organization_for_user`
    devolvia None e o tick inteiro morria em ValidationError — sem reagendar, sem
    contar falha e sem enviar nada. Todo tick, das 20h de um dia em diante.

    Restaura em vez de zerar: nunca concede mais do que o chamador já tinha. E o
    `system_expr` da RLS continua exigindo `current_user IN (system, migration)`,
    então nem um 'on' indevido aqui abriria dado para uma role de aplicação.
    """
    if connection.vendor != "postgresql":
        return
    if in_system_context():
        _set_postgres_context(system=True, local=False)
        return
    _set_postgres_context(
        organization_id=current_organization_id(), system=False, local=False,
    )


def _reinstalar_actor_ambiente():
    """Versão de `_reinstalar_escopo_ambiente` para `app.actor_id`."""
    if connection.vendor != "postgresql":
        return
    _set_postgres_actor(current_actor_id(), local=False)


@contextmanager
def actor_context(actor):
    """Autoriza somente a descoberta das organizações do usuário autenticado."""
    actor_id = getattr(actor, "pk", actor)
    if not actor_id:
        raise ValueError("actor é obrigatório para resolver o tenant")
    token = _current_actor_id.set(str(actor_id))
    try:
        with transaction.atomic():
            _set_postgres_actor(actor_id, local=True)
            yield
    finally:
        # Ordem importa: o ContextVar volta ao valor de fora ANTES de reinstalar,
        # senão reinstalaríamos o escopo que está sendo desmontado.
        _current_actor_id.reset(token)
        try:
            _reinstalar_actor_ambiente()
        except Exception:
            pass


@contextmanager
def organization_context(organization):
    """Abre uma transação cujo RLS só enxerga uma organização."""
    organization_id = getattr(organization, "pk", organization)
    if not organization_id:
        raise ValueError("organization é obrigatória para contexto tenant")
    token_org = _current_organization_id.set(str(organization_id))
    token_system = _system_context.set(False)
    try:
        with transaction.atomic():
            _set_postgres_context(
                organization_id=organization_id, system=False, local=True,
            )
            yield
    finally:
        _system_context.reset(token_system)
        _current_organization_id.reset(token_org)
        try:
            _reinstalar_escopo_ambiente()
        except Exception:
            pass


@contextmanager
def system_context():
    """Contexto explícito para catálogo global, migrações e suporte auditado."""
    if in_system_context():
        yield
        return
    token_org = _current_organization_id.set(None)
    token_system = _system_context.set(True)
    try:
        _assert_privileged_database_role()
        _set_postgres_context(system=True, local=False)
        yield
    finally:
        _system_context.reset(token_system)
        _current_organization_id.reset(token_org)
        try:
            _reinstalar_escopo_ambiente()
        except Exception:
            # A conexão pode ter caído; a próxima nasce sem contexto por default.
            pass


@contextmanager
def public_link_context(identificador):
    """Libera a leitura de UMA publicação já enviada para um visitante anônimo.

    Existe porque o clique num link publicado chega sem sessão: o
    OrganizationContextMiddleware devolve cedo, nenhum GUC de organização é
    instalado, e as duas tabelas envolvidas são STRICT — a linha some e o
    redirect responde 404 para todo link que o sistema já mandou.

    Não é `system_context` disfarçado. A role de runtime do gunicorn não pode
    abrir contexto cross-tenant (`_assert_privileged_database_role`), e não é isso
    que se quer: o visitante não deve enxergar o tenant, só a linha cujo
    identificador ele já apresentou na URL. A policy correspondente está em
    `rls.py` (`public_link_expr`) e exige `status='enviado'`.

    `local=TRUE` dentro de `atomic` é obrigatório, não estilo: `CONN_MAX_AGE` é
    600s, a conexão volta ao pool e um GUC de sessão sobreviveria para a próxima
    request — que pode ser de outra pessoa.
    """
    valor = str(identificador or "")
    if not valor or connection.vendor != "postgresql":
        yield
        return
    assinatura = _context_signature("public_link", valor)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT set_config('app.public_link', %s, TRUE),
                       set_config('app.public_link_signature', %s, TRUE)
                """,
                [valor, assinatura],
            )
        yield


def system_job(func):
    """Marca um management command inteiro como operação global explícita."""
    @wraps(func)
    def wrapped(*args, **kwargs):
        with system_context():
            return func(*args, **kwargs)
    return wrapped


def organization_job(func):
    """Instala o tenant em threads/jobs cujo primeiro argumento é User ou user_id."""
    @wraps(func)
    def wrapped(user_or_id, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from .models import organization_for_user

        close_old_connections()
        user = user_or_id
        if not getattr(user, "is_authenticated", False):
            user = get_user_model().objects.get(pk=user_or_id)
        try:
            with actor_context(user.pk):
                organization = organization_for_user(user)
                if organization is None:
                    raise ValueError("Job privado iniciado sem organização ativa.")
                with organization_context(organization):
                    return func(user_or_id, *args, **kwargs)
        finally:
            close_old_connections()
    return wrapped


# ───────────────── ponte de ORM para dentro do Playwright ─────────────────
#
# A API *sync* do Playwright roda o event loop do asyncio num greenlet da MESMA
# thread. Greenlets compartilham thread-locals, então `asyncio.get_running_loop()`
# tem sucesso enquanto o código está dentro de `with sync_playwright():` — e o
# decorator @async_unsafe do Django levanta SynchronousOnlyOperation em QUALQUER
# operação do ORM (connect/cursor/commit/close).
#
# A regra do repo é tirar o ORM de dentro do bloco (ver ofertas_scraper.py e os
# workers de conexão). Onde isso não dá — ORM intercalado com navegação, como o
# lote do Link Builder, em que bufferizar perderia o progresso parcial de dezenas
# de itens — a chamada vai para uma thread dedicada: thread nova = sem loop
# rodando = @async_unsafe não dispara. O que NÃO se usa é DJANGO_ALLOW_ASYNC_UNSAFE:
# os.environ é global ao processo (8 threads no gunicorn) e o `finally` de um fluxo
# removia a permissão debaixo de outro, o que era a origem da intermitência.

_executor_orm: ThreadPoolExecutor | None = None
_executor_orm_lock = threading.Lock()


def _dentro_de_loop() -> bool:
    """True quando há event loop rodando NESTA thread (Playwright sync = True)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _obter_executor_orm() -> ThreadPoolExecutor:
    """Executor de uma thread só, persistente pelo processo.

    max_workers=1 de propósito: serializa as gravações num ponto único e limita a
    +1 conexão Postgres no processo (já são 8 threads de gunicorn com
    CONN_MAX_AGE=600). Persistente, e não uma thread por chamada, porque abrir
    conexão nova ao Postgres da Fly (TLS) por item de lote custaria dezenas de ms
    cada.
    """
    global _executor_orm
    if _executor_orm is None:
        with _executor_orm_lock:
            if _executor_orm is None:
                _executor_orm = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="orm-fora-do-loop",
                )
    return _executor_orm


@contextmanager
def tenant_suspenso(organization_id, actor_id=None):
    """Guarda o tenant sem abrir transação, conexão nem GUC.

    Quem grava reinstala o escopo de verdade em `executar_no_tenant`.
    """
    if not organization_id:
        raise ValueError("organização é obrigatória para suspender o tenant")
    token = _tenant_suspenso.set((str(organization_id), str(actor_id or "") or None))
    try:
        # A guarda torna o contrato observável também no SQLite da suíte: uma
        # query direta não pode mais parecer correta localmente e falhar só na RLS
        # do PostgreSQL em produção.
        with connection.execute_wrapper(_bloquear_orm_com_tenant_apenas_suspenso):
            yield
    finally:
        _tenant_suspenso.reset(token)


def executar_no_tenant(fn, *args, organization_id=None, actor_id=None, **kwargs):
    """Executa `fn` com o ORM válido e o tenant instalado, mesmo dentro do Playwright.

    FORA do Playwright chama direto: mesma thread, mesma conexão, mesma transação.
    É isso que mantém `TestCase` (que roda tudo numa transação revertida no fim)
    enxergando as escritas — e não custa thread nem conexão nenhuma.

    DENTRO do `with sync_playwright()` a chamada vai para a thread dedicada.
    O tenant NÃO é herdado por ela: contextvars não cruzam threads de executor e
    `django.db.connections` é thread-local, então o escopo é reinstalado
    explicitamente com actor_context + organization_context — que também resetam
    os GUCs na saída. Sem esse reset, a organização de uma chamada sobreviveria na
    conexão persistente do executor e vazaria para o tenant seguinte.
    """
    guardado = _tenant_suspenso.get() or (None, None)
    organization_id = organization_id or current_organization_id() or guardado[0]
    actor_id = actor_id or current_actor_id() or guardado[1]
    # O catálogo ML é um pool global gravado pelos workers de sistema (usuario=None):
    # ali não há organização, e o escopo correto é o de sistema, não o tenant.
    sistema = not organization_id and in_system_context()
    if not organization_id and not sistema:
        # Fail-closed: sem escopo resolvido a gravação iria parar na RLS de qualquer
        # jeito, e mais tarde — com o browser já fechado e o dado perdido.
        raise ValueError(
            "executar_no_tenant exige organização; nenhum contexto tenant ativo."
        )

    def _no_escopo():
        if sistema:
            with system_context():
                return fn(*args, **kwargs)
        with actor_context(actor_id) if actor_id else nullcontext():
            with organization_context(organization_id):
                return fn(*args, **kwargs)

    if not _dentro_de_loop():
        return _no_escopo()

    def _alvo():
        # Minutos de browser matam o socket ocioso do Postgres dos dois lados.
        close_old_connections()
        try:
            return _no_escopo()
        finally:
            close_old_connections()

    # .result() propaga a exceção para o chamador em vez de deixá-la presa no Future.
    return _obter_executor_orm().submit(_alvo).result()


def executar_orm_ou_direto(fn, *args, **kwargs):
    """`executar_no_tenant` com saída direta para processos sem escopo.

    Jobs SSE rodam com o tenant apenas ANOTADO (`tenant_suspenso`): cada ida ao
    banco precisa reinstalar o escopo numa transação curta, senão a RLS devolve
    zero linhas. Workers de sistema (role com BYPASSRLS) não têm escopo nenhum —
    ali `executar_no_tenant` levanta ValueError e a query segue direta, como
    sempre funcionou. Use este wrapper em código compartilhado pelos dois mundos.
    """
    try:
        return executar_no_tenant(fn, *args, **kwargs)
    except ValueError as exc:
        if "exige organização" not in str(exc):
            raise
        return fn(*args, **kwargs)


def organization_job_sem_transacao(func):
    """Como @organization_job, mas sem segurar transação/conexão durante o job.

    `organization_job` envolve a função inteira num transaction.atomic(). Para um
    worker de live view isso é uma transação `idle in transaction` por até 10
    minutos, prendendo uma conexão do pool enquanto o usuário digita a senha — e o
    commit da saída é @async_unsafe. Aqui o tenant é resolvido numa transação de
    milissegundos, as conexões são fechadas, e o escopo fica apenas anotado; quem
    toca o banco o reinstala via `executar_no_tenant`.
    """
    @wraps(func)
    def wrapped(user_or_id, *args, **kwargs):
        from django.contrib.auth import get_user_model
        from .models import organization_for_user

        close_old_connections()
        user = user_or_id
        if not getattr(user, "is_authenticated", False):
            user = get_user_model().objects.get(pk=user_or_id)
        with actor_context(user.pk):
            organization = organization_for_user(user)
            if organization is None:
                raise ValueError("Job privado iniciado sem organização ativa.")
            organization_id = str(organization.pk)
        # Nada aberto enquanto o browser roda.
        close_old_connections()
        try:
            with tenant_suspenso(organization_id, actor_id=str(user.pk)):
                return func(user_or_id, *args, **kwargs)
        finally:
            close_old_connections()
    return wrapped


def organization_callable(organization, func, *, segurar_transacao=True):
    """Adapta um callable sem argumentos para execução segura em nova thread.

    `segurar_transacao=False` é o modo para jobs LONGOS (SSE de raspagem, geração
    de links): o escopo fica apenas anotado, sem transação nem conexão presas, e
    quem grava reinstala o tenant via `executar_no_tenant` — mesmo desenho de
    `organization_job_sem_transacao`.

    Por que isso importa: `organization_context` mantém um `transaction.atomic()`
    aberto durante TODO o job. Um lote de links passa minutos no Link Builder sem
    tocar no banco, e essa transação fica `idle in transaction` até o proxy da Fly
    derrubar o socket. Pior: dentro de uma transação o Django NÃO fecha a conexão —
    ele só marca `closed_in_transaction` e a mantém quebrada até o fim — então
    nenhuma tentativa de renovar (`close_all`, `close_old_connections`) tem efeito
    ali dentro, e toda query seguinte estoura "the connection is closed".
    """
    organization_id = getattr(organization, "pk", organization)

    @wraps(func)
    def wrapped():
        close_old_connections()
        try:
            if segurar_transacao:
                with organization_context(organization_id):
                    return func()
            with tenant_suspenso(organization_id):
                return func()
        finally:
            close_old_connections()
    return wrapped


def organization_thread_target(organization, func, *, segurar_transacao=True):
    """`organization_callable` para uso como alvo de `threading.Thread`.

    `django.db.connections` é thread-local: quando a thread morre, a referência
    para a conexão vai embora mas a conexão NÃO é fechada. E `close_old_connections`
    não resolve — com `CONN_MAX_AGE=600` em produção ela não considera velha uma
    conexão recém-aberta, apenas órfã. Cada requisição SSE (raspagem, envio, busca
    nas lojas, geração de links) deixava assim uma conexão pendurada no Postgres
    até o processo reiniciar.

    O fechamento fica FORA de `organization_callable` porque ele também é chamado
    em linha, dentro da transação do chamador, onde fechar a conexão a quebraria.
    """
    alvo = organization_callable(organization, func,
                                 segurar_transacao=segurar_transacao)

    @wraps(func)
    def executar():
        try:
            return alvo()
        finally:
            connections.close_all()
    return executar


@receiver(connection_created)
def restore_context_on_reconnect(sender, connection, **kwargs):
    """Reinstala o escopo após reconnect de um worker longo."""
    if connection.vendor != "postgresql":
        return
    organization_id = current_organization_id()
    actor_id = current_actor_id()
    system = in_system_context()
    if not organization_id and not actor_id and not system:
        return
    organization_value = str(organization_id or "")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT set_config('app.organization_id', %s, false),
                   set_config('app.organization_signature', %s, false),
                   set_config('app.system_context', %s, false),
                   set_config('app.system_signature', %s, false)
            """,
            [
                organization_value,
                (
                    _context_signature("organization", organization_value)
                    if organization_value and not system
                    else ""
                ),
                "on" if system else "off",
                _context_signature("system") if system else "",
            ],
        )
        cursor.execute(
            """
            SELECT set_config('app.actor_id', %s, false),
                   set_config('app.actor_signature', %s, false)
            """,
            [
                str(actor_id or ""),
                (
                    _context_signature("actor", str(actor_id))
                    if actor_id
                    else ""
                ),
            ],
        )

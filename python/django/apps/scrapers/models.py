from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone
import secrets
import unicodedata
import uuid

from apps.accounts.fields import EncryptedCharField

# Alfabeto base62 do slug curto de publicação. 7 caracteres dão 62^7 (~3,5
# trilhões) de combinações: colisão é estatisticamente irrelevante e, se
# acontecer, o unique do banco barra e o envio seguinte gera outro slug.
_ALFABETO_SLUG = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def gerar_slug_curto():
    return "".join(secrets.choice(_ALFABETO_SLUG) for _ in range(7))


def normalizar_busca(texto) -> str:
    """Minúsculas e sem acento, para 'robo' encontrar 'Robô Aspirador'.

    `icontains` vira ILIKE no Postgres, que é sensível a acento: quem digitava
    "robo" não achava nenhum dos itens cujo título traz "robô", e a tela parecia
    não ter o produto. A alternativa seria a extensão `unaccent`, mas dev e CI
    rodam SQLite — o caminho de produção ficaria sem cobertura de teste. Uma
    coluna normalizada casa igual nos dois bancos.
    """
    if not texto:
        return ""
    decomposto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in decomposto if not unicodedata.combining(c)).casefold()

class Cupom(models.Model):
    campanha_id = models.CharField(max_length=100, unique=True)
    titulo = models.CharField(max_length=255)
    tipo_desconto = models.CharField(max_length=20) # 'fixo' ou 'porcentagem'
    valor_desconto = models.FloatField()
    valor_minimo = models.FloatField(default=0.0)  # compra mínima para o cupom ser válido
    # Limite efetivo informado pelo marketplace (ex.: "10% limitado a R$ 50").
    desconto_maximo = models.FloatField(null=True, blank=True)
    # Condição de público/pagamento que precisa acompanhar a publicação.
    restrito = models.BooleanField(default=False)
    link_original = models.URLField(max_length=1000)
    codigo = models.CharField(max_length=512, blank=True, default="")
    data_criacao = models.DateTimeField(auto_now_add=True)
    fonte = models.CharField(max_length=80, blank=True, default="")
    validade = models.DateTimeField(null=True, blank=True)
    ultima_verificacao = models.DateTimeField(null=True, blank=True, db_index=True)
    estado = models.CharField(max_length=20, default="ativo", db_index=True)

class Produto(models.Model):
    # Marketplace de origem ('mercadolivre' | 'amazon' | 'shopee'). Permite que a
    # seleção/envio sejam agnósticos: o link de afiliado certo é resolvido via registry.
    marketplace = models.CharField(max_length=20, default="mercadolivre", db_index=True)
    # Dono do item (multi-tenant). null = pool COMPARTILHADO (ML raspado p/ todos).
    # set = item privado daquele usuário (Amazon, raspado com a conta Creators dele).
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, db_index=True,
                              related_name="produtos")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="produtos",
    )
    data_scope = models.CharField(
        max_length=16, choices=[("public", "Público"), ("organization", "Organização")],
        default="public", db_index=True,
    )
    # ASIN da Amazon (vazio p/ outros marketplaces). Usado p/ link canônico /dp/{ASIN},
    # dedup por (marketplace, asin) e refresh de preço/liveness via getItems.
    asin = models.CharField(max_length=20, blank=True, default="", db_index=True)
    campanha_id = models.CharField(max_length=100, db_index=True, blank=True, default="")
    origem = models.CharField(max_length=20, default="cupom", db_index=True)  # 'cupom' | 'oferta'
    nome = models.CharField(max_length=255)
    # Espelho de `nome` sem acento e em minúsculas, mantido por save() e pelo único
    # bulk_create de Produto (scraper_mercadolivre/scraper.py). É contra ele que a
    # busca da tela de Promoções casa — ver normalizar_busca() acima.
    #
    # Sem db_index de propósito: a busca é `icontains`, ou seja LIKE '%termo%', e
    # curinga à esquerda não usa índice btree. Um índice aqui só custaria escrita.
    # Folga no tamanho porque casefold() pode alongar (ß -> ss).
    nome_norm = models.CharField(max_length=300, blank=True, default="")
    # SEMÂNTICA DOS TRÊS CAMPOS DE PREÇO — leia antes de gravar em qualquer um.
    #   preco_sem_desconto: preço de lista, o "DE" riscado do card/PDP.
    #   preco_com_cupom:    a VITRINE, ou seja, o "POR" que a página mostra ao abrir
    #                       o link. O nome é legado e engana: NÃO é o preço depois
    #                       de aplicar cupom nenhum.
    #   preco_efetivo:      o que o cliente realmente paga. Só difere da vitrine
    #                       quando a fonte observou o terceiro preço pós-cupom:
    #                       cupons oficiais da Amazon ou badge "com Cupom" do ML.
    # Havia dois produtores gravando significados diferentes em preco_com_cupom: o
    # caminho de cupom do ML salvava aqui o preço JÁ descontado, e coupon_products
    # .calcular_precos descontava o cupom de novo em cima — a mensagem anunciava um
    # valor que loja nenhuma cobrava. Quem for escrever o terceiro produtor: a
    # vitrine vai em preco_com_cupom, e o desconto de cupom é calculado na hora de
    # publicar, nunca persistido aqui.
    preco_sem_desconto = models.FloatField()
    preco_com_cupom = models.FloatField()
    link_produto = models.URLField(max_length=1000)
    categoria = models.CharField(max_length=100, null=True, blank=True, db_index=True) # Lembra do domain_id?
    macro_categoria = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    # Cache do link de afiliado pré-gerado (evita abrir Playwright na hora do envio)
    url_isca = models.URLField(max_length=1000, blank=True, default="")
    link_afiliado = models.URLField(max_length=1000, blank=True, default="")
    imagem_url = models.URLField(max_length=1000, blank=True, default="")
    frete_full = models.BooleanField(default=False)
    # Marcado quando o card do ML traz o selo "Oferta relâmpago". Feedback da cliente:
    # ofertas relâmpago vendem muito; o ranking dá boost a elas (selecionar_item_para_grupo).
    relampago = models.BooleanField(default=False, db_index=True)
    # Código digitável no checkout, quando o item vem de um cupom de código (ex: CASINHA)
    codigo_checkout = models.CharField(max_length=60, blank=True, default="")
    # True quando o link_afiliado foi verificado e carrega a tag de afiliado (A3).
    # False = link sem atribuição -> não enviar (perda de comissão silenciosa).
    afiliado_ok = models.BooleanField(default=False)
    # Frase de marketing gerada por LLM, cacheada na raspagem (evita bloquear o envio).
    frase_llm = models.CharField(max_length=255, blank=True, default="")
    # Nome enxuto para a mensagem, sem a cauda de especificações do marketplace.
    nome_llm = models.CharField(max_length=120, blank=True, default="")
    # Proveniência e confiança: a UI e o seletor nunca precisam adivinhar se o
    # dado ainda é publicável.
    fonte = models.CharField(max_length=80, blank=True, default="", db_index=True)
    primeira_observacao = models.DateTimeField(auto_now_add=True, null=True)
    ultima_observacao = models.DateTimeField(auto_now=True, null=True, db_index=True)
    ultima_verificacao = models.DateTimeField(null=True, blank=True, db_index=True)
    estado = models.CharField(max_length=20, default="ativo", db_index=True)
    falha_verificacao = models.CharField(max_length=255, blank=True, default="")
    preco_fonte = models.FloatField(null=True, blank=True)
    preco_efetivo = models.FloatField(null=True, blank=True)
    confianca = models.CharField(max_length=20, default="media", db_index=True)
    evidencia = models.JSONField(default=dict, blank=True)
    valido_ate = models.DateTimeField(null=True, blank=True, db_index=True)
    falhas_consecutivas = models.PositiveIntegerField(default=0)

    def save(self, *args, **kwargs):
        self.nome_norm = normalizar_busca(self.nome)[:300]
        # `update_fields` restrito não gravaria a coluna: quem atualiza só o nome
        # (a raspagem faz isso quando o título do anúncio muda) deixaria a busca
        # casando com o nome antigo para sempre.
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "nome" in set(update_fields):
            kwargs["update_fields"] = list(update_fields) + ["nome_norm"]
        return super().save(*args, **kwargs)

    class Meta:
        indexes = [
            # Chave exata de todos os upserts de coleta. Sem este índice, cada card
            # raspado fazia uma varredura do catálogo acumulado e monopolizava o
            # PostgreSQL enquanto o painel tentava navegar.
            models.Index(
                fields=["marketplace", "owner", "link_produto"],
                name="scrapers_prod_lookup_idx",
            ),
        ]
        # Identidade do produto, finalmente no schema. Até 09/2026 não havia
        # NENHUMA constraint aqui, e o mesmo anúncio entrava várias vezes porque
        # writers diferentes gravavam formas diferentes da mesma URL: 5.527
        # grupos duplicados em 68 mil linhas quando isto foi medido.
        #
        # São quatro constraints parciais, e não `nulls_distinct=False`, por dois
        # motivos: `owner` NULL significa "catálogo compartilhado" (NULL = NULL
        # aqui, semanticamente), e índice parcial funciona no SQLite da suíte,
        # enquanto `nulls_distinct` só existe no PostgreSQL — o Django pularia a
        # constraint nos testes e a invariante ficaria sem cobertura.
        #
        # O par asin/link espelha a regra que os writers já aplicam: ASIN ganha
        # da URL quando existe (ver identidade_produto.chave_natural).
        constraints = [
            models.UniqueConstraint(
                fields=["marketplace", "asin"],
                condition=models.Q(owner__isnull=True) & ~models.Q(asin=""),
                name="uniq_produto_publico_asin",
            ),
            models.UniqueConstraint(
                fields=["marketplace", "owner", "asin"],
                condition=models.Q(owner__isnull=False) & ~models.Q(asin=""),
                name="uniq_produto_privado_asin",
            ),
            models.UniqueConstraint(
                fields=["marketplace", "link_produto"],
                condition=models.Q(owner__isnull=True) & models.Q(asin=""),
                name="uniq_produto_publico_link",
            ),
            models.UniqueConstraint(
                fields=["marketplace", "owner", "link_produto"],
                condition=models.Q(owner__isnull=False) & models.Q(asin=""),
                name="uniq_produto_privado_link",
            ),
        ]


class FonteIngestao(models.Model):
    """Estado durável de um conector. Nunca contém credenciais."""
    STATUS = [(s, s) for s in ("ok", "degraded", "blocked", "disabled")]
    slug = models.CharField(max_length=80, unique=True)
    marketplace = models.CharField(max_length=20, db_index=True)
    nome = models.CharField(max_length=120)
    habilitada = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS, default="degraded")
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    ultimo_total = models.PositiveIntegerField(default=0)
    erro_publico = models.CharField(max_length=255, blank=True, default="")
    falhas_consecutivas = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.nome


class IntegracaoAfiliado(models.Model):
    """Conta de uma rede de afiliacao pertencente a um usuario.

    Mercado Livre e Amazon ainda usam os fluxos legados do Perfil. Este modelo e o
    contrato extensivel das redes com API, com Awin como primeiro provedor.
    """

    STATUS = [
        ("pendente", "Pendente"), ("conectada", "Conectada"),
        ("degradada", "Degradada"), ("reconectar", "Reconectar"),
        ("desativada", "Desativada"),
    ]
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="integracoes_afiliado")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="integracoes_afiliado",
    )
    provedor = models.CharField(max_length=30, default="awin", db_index=True)
    identificador_conta = models.CharField(max_length=120, blank=True, default="")
    nome_conta = models.CharField(max_length=160, blank=True, default="")
    token = EncryptedCharField(max_length=4096, blank=True, default="")
    habilitada = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pendente",
                              db_index=True)
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    proxima_sincronizacao = models.DateTimeField(null=True, blank=True, db_index=True)
    programas_sincronizados_em = models.DateTimeField(null=True, blank=True)
    erro_publico = models.CharField(max_length=255, blank=True, default="")
    falhas_consecutivas = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "provedor"],
                                    name="uniq_integracao_provedor_usuario"),
        ]

    def __str__(self):
        return f"{self.provedor}:{self.nome_conta or self.identificador_conta}"


class ProgramaAfiliado(models.Model):
    """Anunciante/programa habilitado dentro de uma integracao do usuario."""

    integracao = models.ForeignKey(IntegracaoAfiliado, on_delete=models.CASCADE,
                                   related_name="programas")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="programas_afiliado",
    )
    external_id = models.CharField(max_length=80)
    nome = models.CharField(max_length=180)
    dominio = models.CharField(max_length=255, blank=True, default="")
    dominios_validos = models.JSONField(default=list, blank=True)
    logo_url = models.URLField(max_length=1000, blank=True, default="")
    status_vinculo = models.CharField(max_length=30, default="joined", db_index=True)
    link_status = models.CharField(max_length=30, default="online", db_index=True)
    deeplink_habilitado = models.BooleanField(default=True)
    habilitado = models.BooleanField(default=True)
    comissao_min = models.FloatField(null=True, blank=True)
    comissao_max = models.FloatField(null=True, blank=True)
    comissao_tipo = models.CharField(max_length=20, blank=True, default="")
    comissao_sincronizada_em = models.DateTimeField(null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["integracao", "external_id"],
                                    name="uniq_programa_por_integracao"),
        ]

    def __str__(self):
        return self.nome


class ExecucaoIngestao(models.Model):
    STATUS = [(s, s) for s in ("running", "ok", "empty", "error", "blocked")]
    fonte = models.ForeignKey(FonteIngestao, on_delete=models.CASCADE,
                              related_name="execucoes")
    integracao = models.ForeignKey(IntegracaoAfiliado, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="execucoes")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="execucoes_ingestao",
    )
    iniciada_em = models.DateTimeField(auto_now_add=True, db_index=True)
    finalizada_em = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS, default="running")
    total_ofertas = models.PositiveIntegerField(default=0)
    total_cupons = models.PositiveIntegerField(default=0)
    erro_publico = models.CharField(max_length=255, blank=True, default="")
    metricas = models.JSONField(default=dict, blank=True)
    rejeicoes = models.JSONField(default=dict, blank=True)
    duracoes = models.JSONField(default=dict, blank=True)
    paginas_processadas = models.PositiveIntegerField(default=0)
    schema_fingerprint = models.CharField(max_length=64, blank=True, default="")
    health_status = models.CharField(max_length=24, blank=True, default="unknown",
                                     db_index=True)


class ExecucaoRaspagem(models.Model):
    """Pedido manual durável, executado pelo worker de raspagem.

    Diferente de ``ExecucaoIngestao`` (uma execução técnica de uma fonte), este
    registro representa a ação iniciada na interface e sobrevive a refresh,
    queda do EventSource, deploy e reinício do processo web.
    """

    TIPOS = [("ofertas", "Promoções"), ("cupons", "Cupons")]
    STATUS = [
        ("queued", "Na fila"),
        ("running", "Executando"),
        ("succeeded", "Concluída"),
        ("partial", "Concluída parcialmente"),
        ("failed", "Falhou"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE,
        related_name="execucoes_raspagem",
    )
    solicitada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name="raspagens_solicitadas",
    )
    tipo = models.CharField(max_length=16, choices=TIPOS, db_index=True)
    status = models.CharField(
        max_length=16, choices=STATUS, default="queued", db_index=True,
    )
    etapa = models.CharField(max_length=80, blank=True, default="")
    progresso = models.PositiveSmallIntegerField(default=0)
    contagens = models.JSONField(default=dict, blank=True)
    codigo_erro = models.CharField(max_length=40, blank=True, default="")
    erro_publico = models.CharField(max_length=255, blank=True, default="")
    acao_recomendada = models.CharField(max_length=255, blank=True, default="")
    tentativas = models.PositiveSmallIntegerField(default=0)
    criada_em = models.DateTimeField(auto_now_add=True, db_index=True)
    iniciada_em = models.DateTimeField(null=True, blank=True)
    heartbeat_em = models.DateTimeField(null=True, blank=True, db_index=True)
    finalizada_em = models.DateTimeField(null=True, blank=True, db_index=True)
    posicao_fila = models.PositiveIntegerField(null=True, blank=True)
    motivo_espera = models.CharField(max_length=40, blank=True, default="", db_index=True)
    espera_iniciada_em = models.DateTimeField(null=True, blank=True)
    worker_id = models.CharField(max_length=120, blank=True, default="", db_index=True)
    recurso_esperado = models.CharField(max_length=160, blank=True, default="")
    lock_owner_tipo = models.CharField(max_length=40, blank=True, default="")
    lease_token = models.CharField(max_length=64, blank=True, default="")
    deadline_em = models.DateTimeField(null=True, blank=True, db_index=True)
    eta_min_em = models.DateTimeField(null=True, blank=True)
    eta_max_em = models.DateTimeField(null=True, blank=True)
    eta_amostra = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-criada_em",)
        constraints = [
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(status__in=("queued", "running")),
                name="uniq_raspagem_manual_ativa_por_org",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "criada_em"],
                         name="scrape_job_status_created"),
        ]


class EventoRaspagem(models.Model):
    """Linha de progresso persistida e paginável por cursor numérico."""

    NIVEIS = [
        ("info", "Informação"),
        ("warning", "Aviso"),
        ("error", "Erro"),
        ("success", "Sucesso"),
    ]

    execucao = models.ForeignKey(
        ExecucaoRaspagem, on_delete=models.CASCADE, related_name="eventos",
    )
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE,
        related_name="eventos_raspagem",
    )
    nivel = models.CharField(max_length=12, choices=NIVEIS, default="info")
    etapa = models.CharField(max_length=80, blank=True, default="")
    mensagem = models.CharField(max_length=500)
    progresso = models.PositiveSmallIntegerField(null=True, blank=True)
    criado_em = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ("id",)
        indexes = [
            models.Index(fields=["execucao", "id"],
                         name="scrape_event_job_cursor"),
        ]


class CupomNormalizado(models.Model):
    """Cupom independente de produto.

    Códigos digitáveis podem ser publicados sem produto. Cupons de ativação só
    ficam prontos depois de uma associação ``ProdutoCupom`` comprovada.
    """
    REDEMPTION_MODES = [
        ("", "Legado/indefinido"), ("code", "Código"),
        ("activation", "Ativação"),
    ]
    SCOPE_TYPES = [
        ("", "Legado/indefinido"), ("sitewide", "Site inteiro"),
        ("category", "Categoria"), ("container", "Container"),
        ("product", "Produto"),
    ]
    AUDIENCE_SCOPES = [
        ("public", "Público"), ("organization", "Organização"),
    ]
    fonte = models.ForeignKey(FonteIngestao, on_delete=models.CASCADE,
                              related_name="cupons")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, db_index=True,
                              related_name="cupons_normalizados")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="cupons_normalizados",
    )
    data_scope = models.CharField(
        max_length=16, choices=[("public", "Público"), ("organization", "Organização")],
        default="public", db_index=True,
    )
    integracao = models.ForeignKey(IntegracaoAfiliado, on_delete=models.SET_NULL,
                                   null=True, blank=True, related_name="cupons")
    programa = models.ForeignKey(ProgramaAfiliado, on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name="cupons")
    external_id = models.CharField(max_length=160)
    marketplace = models.CharField(max_length=20, db_index=True)
    tipo_conteudo = models.CharField(max_length=20, default="voucher", db_index=True)
    # Campos tipados espelham as regras históricas do JSON e tornam o funil
    # consultável sem inferências diferentes em cada consumidor. O valor vazio
    # é aceito apenas para compatibilidade durante o backfill de linhas legadas.
    redemption_mode = models.CharField(
        max_length=16, choices=REDEMPTION_MODES, blank=True, default="", db_index=True,
    )
    scope_type = models.CharField(
        max_length=16, choices=SCOPE_TYPES, blank=True, default="", db_index=True,
    )
    audience_scope = models.CharField(
        max_length=16, choices=AUDIENCE_SCOPES, default="public", db_index=True,
    )
    anunciante_nome = models.CharField(max_length=180, blank=True, default="")
    titulo = models.CharField(max_length=255)
    codigo = models.CharField(max_length=120, blank=True, default="")
    # Categoria/ação da fonte (Sellers, Fashion, "site inteiro", ...). Vem do
    # `escopo` das regras normalizadas; alimenta o filtro por categoria dos cupons.
    categoria = models.CharField(max_length=100, blank=True, default="", db_index=True)
    regras = models.JSONField(default=dict, blank=True)
    link = models.URLField(max_length=1000, blank=True, default="")
    inicio = models.DateTimeField(null=True, blank=True, db_index=True)
    validade = models.DateTimeField(null=True, blank=True, db_index=True)
    restrito = models.BooleanField(default=False, db_index=True)
    relampago = models.BooleanField(default=False, db_index=True)
    estado = models.CharField(max_length=20, default="ativo", db_index=True)
    confianca = models.CharField(max_length=20, default="baixa", db_index=True)
    evidencia = models.JSONField(default=dict, blank=True)
    # Fingerprint apenas dos dados que mudam a aplicabilidade/preco dos produtos.
    # A preparacao guarda a mesma chave; divergencia invalida o cache sem escrever
    # durante o GET da tela de cupons.
    produtos_chave = models.CharField(max_length=64, blank=True, default="", db_index=True)
    primeira_observacao = models.DateTimeField(auto_now_add=True)
    ultima_observacao = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["fonte", "external_id"], condition=models.Q(owner__isnull=True),
                name="uniq_cupom_compartilhado_fonte_external"),
            models.UniqueConstraint(
                fields=["owner", "fonte", "external_id"],
                condition=models.Q(owner__isnull=False),
                name="uniq_cupom_privado_owner_fonte_external"),
        ]


class CupomFonteObservacao(models.Model):
    """Evidência normalizada por fonte; nunca contém HTML autenticado ou segredo."""

    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="observacoes_fontes_cupom",
    )
    fonte = models.ForeignKey(
        FonteIngestao, on_delete=models.CASCADE, related_name="observacoes_cupons",
    )
    cupom = models.ForeignKey(
        CupomNormalizado, on_delete=models.CASCADE, null=True, blank=True,
        related_name="observacoes_fontes",
    )
    canonical_key = models.CharField(max_length=220, db_index=True)
    source_external_id = models.CharField(max_length=180, blank=True, default="")
    precedence = models.PositiveSmallIntegerField(default=100)
    health_status = models.CharField(max_length=24, default="unknown", db_index=True)
    outcome = models.CharField(max_length=32, default="accepted", db_index=True)
    reason_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    evidence = models.JSONField(default=dict, blank=True)
    observed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["fonte", "canonical_key", "source_external_id"],
                condition=models.Q(organization__isnull=True),
                name="uniq_public_coupon_source_observation",
            ),
            models.UniqueConstraint(
                fields=[
                    "organization", "fonte", "canonical_key", "source_external_id",
                ],
                condition=models.Q(organization__isnull=False),
                name="uniq_tenant_coupon_source_observation",
            ),
        ]


class CupomDisponibilidade(models.Model):
    STAGES = [
        ("collected", "Coletado"),
        ("eligible", "Elegível"),
        ("prepared", "Preparado"),
        ("waiting_link", "Aguardando link"),
        ("ready", "Pronto"),
        ("discarded", "Descartado"),
    ]
    CATEGORIES = [
        ("", "Sem bloqueio"),
        ("not_found", "Não encontrado"),
        ("rejected", "Rejeitado"),
        ("waiting", "Aguardando"),
        ("no_session", "Sem sessão"),
        ("no_link", "Sem link"),
        ("invalid", "Inválido"),
        ("operational_failure", "Falha operacional"),
    ]
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE,
        related_name="disponibilidades_cupons",
    )
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="disponibilidades_cupons",
    )
    cupom = models.ForeignKey(
        CupomNormalizado, on_delete=models.CASCADE, related_name="disponibilidades",
    )
    channel = models.CharField(max_length=20, default="whatsapp")
    use_mode = models.CharField(max_length=24, default="product_activation")
    stage = models.CharField(max_length=24, choices=STAGES, default="collected",
                             db_index=True)
    category = models.CharField(max_length=32, choices=CATEGORIES, blank=True,
                                default="", db_index=True)
    reason_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    safe_detail = models.CharField(max_length=255, blank=True, default="")
    retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "usuario", "cupom", "channel", "use_mode"],
                name="uniq_coupon_readiness_projection",
            ),
        ]


class CupomDisponibilidadeEvento(models.Model):
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE,
        related_name="eventos_disponibilidade_cupom",
    )
    disponibilidade = models.ForeignKey(
        CupomDisponibilidade, on_delete=models.CASCADE, related_name="eventos",
    )
    from_stage = models.CharField(max_length=24, blank=True, default="")
    to_stage = models.CharField(max_length=24)
    category = models.CharField(max_length=32, blank=True, default="")
    reason_code = models.CharField(max_length=64, blank=True, default="")
    marketplace = models.CharField(max_length=20, blank=True, default="", db_index=True)
    source = models.CharField(max_length=80, blank=True, default="", db_index=True)
    use_mode = models.CharField(max_length=24, blank=True, default="", db_index=True)
    evidence_strength = models.CharField(max_length=32, blank=True, default="")
    attempt = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    source_run_id = models.CharField(max_length=80, blank=True, default="", db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class CupomValidacao(models.Model):
    """Tentativa auditável de aplicar um código, sempre sem concluir a compra."""

    STATUS = [
        ("pending", "Pendente"), ("running", "Em execução"),
        ("accepted", "Aceito"), ("rejected", "Rejeitado"),
        ("inconclusive", "Inconclusivo"),
    ]
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE,
        related_name="validacoes_cupons",
    )
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="validacoes_cupons",
    )
    cupom = models.ForeignKey(
        CupomNormalizado, on_delete=models.CASCADE, related_name="validacoes",
    )
    marketplace = models.CharField(max_length=20, db_index=True)
    product_key = models.CharField(max_length=160, blank=True, default="", db_index=True)
    product_url = models.URLField(max_length=1500, blank=True, default="")
    cart_fingerprint = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS, default="pending",
                              db_index=True)
    reason_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    safe_detail = models.CharField(max_length=255, blank=True, default="")
    subtotal_before = models.DecimalField(max_digits=12, decimal_places=2,
                                          null=True, blank=True)
    subtotal_after = models.DecimalField(max_digits=12, decimal_places=2,
                                         null=True, blank=True)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2,
                                          null=True, blank=True)
    evidence = models.JSONField(default=dict, blank=True)
    no_purchase = models.BooleanField(default=True)
    attempts = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True, db_index=True)
    retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["usuario", "cupom", "cart_fingerprint"],
                name="uniq_coupon_cart_validation",
            ),
            models.CheckConstraint(
                condition=models.Q(no_purchase=True),
                name="coupon_validation_never_purchases",
            ),
        ]
        indexes = [
            models.Index(
                fields=["usuario", "status", "verified_at"],
                name="coupon_validation_user_status",
            ),
            models.Index(
                fields=["marketplace", "verified_at"],
                name="coupon_validation_market_time",
            ),
        ]


class ProdutoCupom(models.Model):
    STATUS = [
        ("confirmado", "Confirmado"), ("provavel", "Provável"),
        ("nao_aplicavel", "Não aplicável"), ("expirado", "Expirado"),
    ]
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE,
                                related_name="cupons_normalizados")
    cupom = models.ForeignKey(CupomNormalizado, on_delete=models.CASCADE,
                              related_name="produtos")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="produto_cupons",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="provavel")
    verificado_em = models.DateTimeField(null=True, blank=True)
    evidencia = models.JSONField(default=dict, blank=True)
    # A campanha pertence à associação, não ao Produto. Um mesmo anúncio pode
    # participar de várias campanhas simultaneamente.
    activation_key = models.CharField(max_length=160, blank=True, default="", db_index=True)
    # Snapshot monetario especifico deste cupom. O mesmo Produto pode participar de
    # campanhas diferentes; por isso o preco final nao pode morar apenas em Produto.
    preco_original = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    preco_atual = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    preco_final = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = ("produto", "cupom")


class LinkAfiliadoProdutoCupomUsuario(models.Model):
    """Link afiliado verificado para uma associação produto–cupom específica."""
    usuario = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="links_produto_cupom",
    )
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="links_produto_cupom",
    )
    relacao = models.ForeignKey(
        ProdutoCupom, on_delete=models.CASCADE, related_name="links_usuarios",
    )
    url_isca = models.URLField(max_length=1000, blank=True, default="")
    link_afiliado = models.URLField(max_length=1500, blank=True, default="")
    estado = models.CharField(
        max_length=20,
        choices=[("pendente", "Na fila"), ("pronto", "Pronto"),
                 ("erro", "Falhou"), ("nao_afiliavel", "Não afiliável")],
        default="pendente", db_index=True,
    )
    verificado_ok = models.BooleanField(null=True, blank=True, default=None,
                                        db_index=True)
    verificado_em = models.DateTimeField(null=True, blank=True, db_index=True)
    url_canonica = models.URLField(max_length=1500, blank=True, default="")
    verificacao_motivo = models.CharField(max_length=300, blank=True, default="")
    tentativas = models.PositiveIntegerField(default=0)
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    proxima_tentativa = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["usuario", "relacao"], name="uniq_link_produto_cupom_usuario",
            ),
        ]


class CupomPreparacao(models.Model):
    STATUS = [
        ("pendente", "Pendente"), ("pronto", "Pronto"),
        ("vazio", "Sem produtos"), ("erro", "Erro"),
    ]
    cupom = models.ForeignKey(CupomNormalizado, on_delete=models.CASCADE,
                              related_name="preparacoes")
    # null = preparacao compartilhada (catalogo publico do Mercado Livre).
    # Demais lojas e cupons privados sao preparados no contexto do usuario.
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                null=True, blank=True, related_name="cupons_preparados")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="cupons_preparados",
    )
    status = models.CharField(max_length=20, choices=STATUS, default="pendente",
                              db_index=True)
    produtos_chave = models.CharField(max_length=64, blank=True, default="", db_index=True)
    verificado_em = models.DateTimeField(null=True, blank=True, db_index=True)
    proxima_tentativa = models.DateTimeField(null=True, blank=True, db_index=True)
    erro = models.CharField(max_length=500, blank=True, default="")
    reason_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    safe_detail = models.CharField(max_length=255, blank=True, default="")
    tentativas = models.PositiveIntegerField(default=0)
    duracao_ms = models.PositiveIntegerField(default=0)
    source_run_id = models.CharField(max_length=80, blank=True, default="", db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cupom"], condition=models.Q(usuario__isnull=True),
                name="uniq_preparo_cupom_compartilhado"),
            models.UniqueConstraint(
                fields=["cupom", "usuario"], condition=models.Q(usuario__isnull=False),
                name="uniq_preparo_cupom_usuario"),
        ]

class PrecoHistorico(models.Model):
    """Uma observação de preço por raspagem — base p/ detectar QUEDA REAL e derrubar
    'de/por' inflado (o preço "de" do ML costuma ser fictício). Chave por identidade
    do produto (asin na Amazon; URL normalizada no ML), não pelo id do Produto (que
    é recriado a cada raspagem)."""
    marketplace = models.CharField(max_length=20, db_index=True)
    chave = models.CharField(max_length=300, db_index=True)
    preco = models.FloatField()
    data = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=["marketplace", "chave", "data"]),
            # O ranking lê preço junto das três chaves. Cobrir o valor evita um
            # acesso aleatório à tabela para cada observação histórica.
            models.Index(
                fields=["marketplace", "chave", "data"], include=["preco"],
                name="scrape_price_stats_cover",
            ),
        ]


class HistoricoEnvio(models.Model):
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE)
    # Dono do envio (multi-tenant): dedup "nunca repetir" passa a ser POR usuário.
    # null = envios legados (single-tenant) — tratados como do owner default na migração.
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                null=True, blank=True, db_index=True,
                                related_name="envios")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="envios",
    )
    data_envio = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.produto.nome} enviado em {self.data_envio}"


class Publicacao(models.Model):
    """Registro imutável da decisão e do resultado de uma publicação."""
    STATUS = [
        ("pendente", "Pendente"), ("enviado", "Enviado"),
        ("falhou", "Falhou"), ("incerto", "Confirmação pendente"),
        ("ignorado", "Ignorado"),
    ]
    id_publico = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    origem = models.CharField(max_length=30, default="produto", db_index=True)
    # Slug do link curto publicado na mensagem (/r/<slug>/). null p/ as linhas
    # anteriores ao campo — o token assinado antigo continua funcionando p/ elas.
    slug_curto = models.CharField(max_length=12, unique=True, null=True, blank=True,
                                  default=gerar_slug_curto, editable=False)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="publicacoes")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="publicacoes",
    )
    produto = models.ForeignKey(Produto, on_delete=models.SET_NULL, null=True,
                                related_name="publicacoes")
    cupom_normalizado = models.ForeignKey(
        "CupomNormalizado", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="publicacoes",
    )
    configuracao = models.ForeignKey("ConfiguracaoEnvio", on_delete=models.SET_NULL,
                                     null=True, blank=True, related_name="publicacoes")
    canal = models.CharField(max_length=20)
    destino_id = models.CharField(max_length=100)
    destino_nome = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS, default="pendente", db_index=True)
    erro = models.CharField(max_length=500, blank=True, default="")
    variante = models.CharField(max_length=1, default="A")
    mensagem = models.TextField(blank=True, default="")
    link_afiliado = models.URLField(max_length=1500, blank=True, default="")
    link_rastreado = models.URLField(max_length=1500, blank=True, default="")
    preco_original = models.FloatField(default=0)
    preco_final = models.FloatField(default=0)
    cupom = models.CharField(max_length=255, blank=True, default="")
    categoria = models.CharField(max_length=100, blank=True, default="")
    score = models.FloatField(default=0)
    motivos_score = models.JSONField(default=list, blank=True)
    criada_em = models.DateTimeField(auto_now_add=True, db_index=True)
    enviada_em = models.DateTimeField(null=True, blank=True, db_index=True)
    operation_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    stage = models.CharField(max_length=32, default="selected", db_index=True)
    attempt_count = models.PositiveSmallIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True, db_index=True)
    duration_ms = models.PositiveIntegerField(default=0)
    transport_state = models.CharField(max_length=32, blank=True, default="")
    # Payload binário privado usado apenas entre a reserva web e o worker v2. Nunca
    # entra em logs/eventos/admin e é apagado ao atingir estado terminal.
    queued_media = models.BinaryField(null=True, blank=True, editable=False)
    queued_media_mime = models.CharField(max_length=80, blank=True, default="")
    delivery_batch_key = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
    )

    class Meta:
        # O dashboard filtra sempre por (usuario, janela de data): os índices de
        # coluna única obrigavam o banco a escolher um e filtrar o resto na mão.
        indexes = [models.Index(fields=["usuario", "criada_em"])]


class PublicacaoTentativa(models.Model):
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE,
        related_name="tentativas_publicacao",
    )
    publicacao = models.ForeignKey(
        Publicacao, on_delete=models.CASCADE, related_name="tentativas",
    )
    numero = models.PositiveSmallIntegerField()
    stage = models.CharField(max_length=32, db_index=True)
    classification = models.CharField(max_length=24, blank=True, default="")
    result = models.CharField(max_length=32, blank=True, default="")
    reason_code = models.CharField(max_length=64, blank=True, default="")
    duration_ms = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["publicacao", "numero"], name="uniq_publication_attempt",
            ),
        ]


class PublicacaoEvento(models.Model):
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE,
        related_name="eventos_publicacao",
    )
    publicacao = models.ForeignKey(
        Publicacao, on_delete=models.CASCADE, related_name="eventos_estado",
    )
    stage = models.CharField(max_length=32, db_index=True)
    reason_code = models.CharField(max_length=64, blank=True, default="")
    safe_detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)


class CliquePublicacao(models.Model):
    """Clique sem IP, cookie ou identificador pessoal."""
    publicacao = models.ForeignKey(Publicacao, on_delete=models.CASCADE, related_name="cliques")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="cliques_publicacao",
    )
    clicado_em = models.DateTimeField(auto_now_add=True, db_index=True)


class LinkAfiliadoCupomUsuario(models.Model):
    """Cache de link afiliado de um cupom por usuario e URL de origem."""
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="links_cupons")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="links_cupons",
    )
    cupom = models.ForeignKey(CupomNormalizado, on_delete=models.CASCADE,
                              related_name="links_usuarios")
    url_origem = models.URLField(max_length=1000)
    link_afiliado = models.URLField(max_length=1500)
    afiliado_ok = models.BooleanField(default=False)
    ESTADOS = [
        ("pendente", "Na fila"), ("pronto", "Link gerado"),
        ("nao_afiliavel", "Não afiliável"), ("erro", "Falhou"),
    ]
    estado = models.CharField(max_length=20, choices=ESTADOS, default="pendente",
                              db_index=True)
    verificado_ok = models.BooleanField(null=True, blank=True, default=None,
                                        db_index=True)
    verificado_em = models.DateTimeField(null=True, blank=True, db_index=True)
    url_canonica = models.URLField(max_length=1500, blank=True, default="")
    verificacao_motivo = models.CharField(max_length=300, blank=True, default="")
    tentativas = models.PositiveIntegerField(default=0)
    ultimo_erro = models.CharField(max_length=300, blank=True, default="")
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    proxima_tentativa = models.DateTimeField(null=True, blank=True, db_index=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["usuario", "cupom"],
                                    name="uniq_link_cupom_usuario"),
        ]


class ReceitaAfiliado(models.Model):
    """Linha normalizada de relatório sincronizado do marketplace."""
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="receitas_afiliado")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="receitas_afiliado",
    )
    marketplace = models.CharField(max_length=20, db_index=True)
    data = models.DateField(db_index=True)
    etiqueta = models.CharField(max_length=120, blank=True, default="")
    produto_nome = models.CharField(max_length=255, blank=True, default="")
    cliques = models.PositiveIntegerField(default=0)
    conversoes = models.PositiveIntegerField(default=0)
    pedidos = models.PositiveIntegerField(default=0)
    receita = models.FloatField(default=0)
    comissao = models.FloatField(default=0)
    periodo_inicio = models.DateField(null=True, blank=True)
    periodo_fim = models.DateField(null=True, blank=True)
    origem = models.CharField(max_length=20, default="auto")
    granularidade = models.CharField(max_length=20, default="dia")
    hash_origem = models.CharField(max_length=64, unique=True)
    importada_em = models.DateTimeField(auto_now_add=True)

    class Meta:
        # resumo_financeiro busca o snapshot mais recente por (usuario, marketplace).
        indexes = [models.Index(fields=["usuario", "marketplace", "data"])]


class RelatorioSync(models.Model):
    STATUS = [
        ("nunca", "Nunca sincronizado"),
        ("rodando", "Sincronizando"),
        ("ok", "Sincronizado"),
        ("erro", "Erro"),
        ("acao", "Precisa de ação"),
        # Distinto de "acao": não há nada que o usuário possa fazer. A leitura
        # automática daquele portal ainda não existe/não está configurada, e mandar
        # ele "reconectar" uma conta que já está conectada é um loop sem saída.
        ("nao_configurado", "Sincronização automática indisponível"),
    ]
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="syncs_relatorio")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="syncs_relatorio",
    )
    marketplace = models.CharField(max_length=20, db_index=True)
    status = models.CharField(max_length=20, choices=STATUS, default="nunca", db_index=True)
    ultimo_inicio = models.DateTimeField(null=True, blank=True)
    ultimo_fim = models.DateTimeField(null=True, blank=True)
    ultimo_sucesso = models.DateTimeField(null=True, blank=True)
    proxima_execucao = models.DateTimeField(null=True, blank=True, db_index=True)
    erro = models.CharField(max_length=500, blank=True, default="")
    registros_criados = models.PositiveIntegerField(default=0)
    registros_atualizados = models.PositiveIntegerField(default=0)
    prerequisite_code = models.CharField(max_length=64, blank=True, default="", db_index=True)
    schema_fingerprint = models.CharField(max_length=64, blank=True, default="")
    linhas_vistas = models.PositiveIntegerField(default=0)
    linhas_aceitas = models.PositiveIntegerField(default=0)
    linhas_rejeitadas = models.PositiveIntegerField(default=0)
    periodo_aplicado_inicio = models.DateField(null=True, blank=True)
    periodo_aplicado_fim = models.DateField(null=True, blank=True)
    atualizado_em = models.DateTimeField(auto_now=True)

    @property
    def erro_publico(self):
        """Texto para a UI. `erro` guarda a exceção crua (admin/logs), que não
        pode vazar para o usuário; a home monta instâncias não salvas, então
        isto não pode tocar o banco."""
        if self.status == "acao":
            return "Reconecte o portal de afiliados para voltar a sincronizar."
        if self.status == "erro":
            return "Falha temporária na leitura dos relatórios — tentaremos de novo."
        if self.status == "nao_configurado":
            return "Esta loja ainda não tem leitura automática de relatórios."
        return ""

    class Meta:
        unique_together = ("usuario", "marketplace")


class GastoIA(models.Model):
    """Tokens de IA consumidos por mês, modelo e origem.

    Sem `organization`: o teto de custo é do produto inteiro, não de um inquilino,
    e atribuir por organização exigiria decidir o rateio de uma chamada que serve
    ao catálogo compartilhado. Fica fora do RLS pelo mesmo motivo — não é dado de
    ninguém.
    """
    competencia = models.DateField(db_index=True)   # sempre o dia 1 do mês
    modelo = models.CharField(max_length=80)
    origem = models.CharField(max_length=40)        # que parte do funil gastou
    chamadas = models.PositiveIntegerField(default=0)
    tokens_entrada = models.BigIntegerField(default=0)
    tokens_saida = models.BigIntegerField(default=0)
    atualizado_em = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["competencia", "modelo", "origem"],
                name="gastoia_unico_por_mes_modelo_origem",
            ),
        ]

    def __str__(self):
        return f"{self.competencia:%Y-%m} {self.origem} ({self.modelo})"


class WorkerHeartbeat(models.Model):
    worker_id = models.CharField(max_length=120, unique=True)
    worker_type = models.CharField(max_length=40, db_index=True)
    state = models.CharField(max_length=24, default="idle", db_index=True)
    task_type = models.CharField(max_length=40, blank=True, default="")
    heartbeat_at = models.DateTimeField(default=timezone.now, db_index=True)
    details = models.JSONField(default=dict, blank=True)


class AutomacaoEstado(models.Model):
    """Chave e heartbeat globais dos loops, compartilhados entre process groups."""

    job = models.CharField(max_length=32, unique=True)
    enabled = models.BooleanField(default=False)
    configured = models.BooleanField(default=False)
    state = models.JSONField(default=dict, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class ResourceLease(models.Model):
    resource_key = models.CharField(max_length=180, unique=True)
    owner_token = models.CharField(max_length=64, blank=True, default="", db_index=True)
    owner_kind = models.CharField(max_length=40, blank=True, default="")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="resource_leases",
    )
    acquired_at = models.DateTimeField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    consecutive_manual = models.PositiveSmallIntegerField(default=0)
    manual_waiting_since = models.DateTimeField(null=True, blank=True)
    scheduled_waiting_since = models.DateTimeField(null=True, blank=True)


class EventoOperacional(models.Model):
    """Log estruturado para depuração de pipelines e suporte."""
    LEVELS = [
        ("debug", "Debug"),
        ("info", "Info"),
        ("warning", "Warning"),
        ("error", "Error"),
    ]
    PIPELINES = [
        ("onboarding", "Onboarding"),
        ("scraper", "Scraper"),
        ("ranking", "Ranking"),
        ("publicacao", "Publicação"),
        ("conexao", "Conexão"),
        ("whatsapp", "WhatsApp"),
        ("telegram", "Telegram"),
        ("relatorios", "Relatórios"),
        ("redirect", "Redirect"),
        ("sistema", "Sistema"),
    ]
    criado_em = models.DateTimeField(auto_now_add=True, db_index=True)
    level = models.CharField(max_length=10, choices=LEVELS, default="info", db_index=True)
    pipeline = models.CharField(max_length=30, choices=PIPELINES, db_index=True)
    evento = models.CharField(max_length=80, db_index=True)
    mensagem = models.CharField(max_length=500)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="eventos_operacionais")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        db_index=True, related_name="eventos_operacionais",
    )
    contexto = models.JSONField(default=dict, blank=True)
    erro = models.TextField(blank=True, default="")
    # Evita que a leitura do painel reprocese o mesmo log histórico como uma
    # nova ocorrência do incidente.
    incidente_processado = models.BooleanField(default=False, db_index=True)

    class Meta:
        indexes = [models.Index(fields=["pipeline", "level", "criado_em"])]


class IncidenteSaude(models.Model):
    """Problema operacional agregado e seu último diagnóstico confirmado."""
    STATUS = [("aberto", "Aberto"), ("concluido", "Ajuste concluído")]
    chave = models.CharField(max_length=64, unique=True)
    causa = models.CharField(max_length=80, db_index=True)
    pipeline = models.CharField(max_length=30, db_index=True)
    escopo = models.CharField(max_length=255, default="sistema", db_index=True)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="incidentes_saude")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.SET_NULL, null=True, blank=True,
        db_index=True, related_name="incidentes_saude",
    )
    level = models.CharField(max_length=10, default="warning")
    status = models.CharField(max_length=12, choices=STATUS, default="aberto", db_index=True)
    ocorrencias = models.PositiveIntegerField(default=1)
    primeira_ocorrencia = models.DateTimeField(default=timezone.now)
    ultima_ocorrencia = models.DateTimeField(default=timezone.now, db_index=True)
    ultima_mensagem = models.CharField(max_length=500, blank=True, default="")
    contexto = models.JSONField(default=dict, blank=True)
    evento_origem = models.ForeignKey(EventoOperacional, on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="incidentes")
    confirmado_em = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmacao = models.CharField(max_length=255, blank=True, default="")
    # Janela de silêncio do alerta, movida do cache (LocMem por processo, sem
    # Redis em produção — 10 processos deduplicavam localmente e mandavam até
    # 10 mensagens do mesmo incidente) para a própria linha. `alertado_em` é a
    # entrega confirmada; `alerta_tentado_em` é a reivindicação em andamento —
    # duas colunas, não uma, porque cache.add fundia as duas coisas e o
    # cache.delete de liberação (na falha de entrega) virava remendo.
    alertado_em = models.DateTimeField(null=True, blank=True, db_index=True)
    alerta_tentado_em = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status", "ultima_ocorrencia"])]


class LinkAfiliadoUsuario(models.Model):
    """Cache de link de afiliado POR usuário — cada um tem a própria tag/comissão.

    O `Produto.link_afiliado` global não serve mais: o link precisa carregar a tag
    do usuário que envia. Amazon é trivial (monta na hora); ML é caro (Link Builder
    via Playwright), então cacheamos por (usuario, produto).
    """
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="links_afiliado")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="links_afiliado",
    )
    produto = models.ForeignKey(Produto, on_delete=models.CASCADE,
                                related_name="links_usuario")
    url_isca = models.URLField(max_length=1000, blank=True, default="")
    link_afiliado = models.URLField(max_length=1000, blank=True, default="")
    afiliado_ok = models.BooleanField(default=False)
    criado_em = models.DateTimeField(auto_now_add=True)

    # ── Veredito de verificação do DESTINO (fonte única: link_validacao) ──
    # Separa "o Link Builder devolveu uma URL" (afiliado_ok) de "o link abre mesmo
    # o anúncio certo" (verificado_ok). A listagem só oferece envio quando
    # verificado_ok is True; o envio confia neste veredito e usa a url_canonica,
    # em vez de reconferir com uma segunda implementação que poderia divergir.
    #   None  = ainda não verificado (não enviável até verificar)
    #   True  = destino aprovado (enviável)
    #   False = reprovado (link inválido; mostra motivo, não oferece envio)
    verificado_ok = models.BooleanField(null=True, blank=True, default=None,
                                        db_index=True)
    verificado_em = models.DateTimeField(null=True, blank=True)
    # URL exata aprovada que o envio deve usar (o próprio link de afiliado que
    # passou na verificação) — para não reconstruir o link em outra camada.
    url_canonica = models.URLField(max_length=1000, blank=True, default="")
    verificacao_motivo = models.CharField(max_length=300, blank=True, default="")

    # ── Por que este item ainda não tem link ──
    # Sem isto, um produto que nunca afilia fica "pendente" para sempre e não há um
    # único registro do motivo: o gerador contava a falha e seguia (falhas += 1;
    # continue). A linha passa a existir mesmo sem link, carregando a explicação.
    ESTADOS = [
        ("pendente", "Na fila"),
        ("pronto", "Link gerado"),
        # Terminal: a URL não é afiliável pelo Programa (catálogo /up/, perfil,
        # /social/). Retentar não muda o resultado — e retentar para sempre era o
        # que consumia o lote e impedia os outros produtos de avançarem.
        ("nao_afiliavel", "Não afiliável"),
        ("erro", "Falhou"),
    ]
    estado = models.CharField(max_length=20, choices=ESTADOS, default="pendente",
                              db_index=True)
    tentativas = models.PositiveIntegerField(default=0)
    ultimo_erro = models.CharField(max_length=300, blank=True, default="")
    ultima_tentativa = models.DateTimeField(null=True, blank=True)
    # Quando tentar de novo. None + estado terminal = nunca mais.
    proxima_tentativa = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        unique_together = ("usuario", "produto")
        indexes = [
            models.Index(
                fields=["usuario", "verificado_ok", "-verificado_em"],
                name="linkusr_ready_recent_idx",
            ),
        ]


class CupomCodigo(models.Model):
    """Cupom de CÓDIGO digitável no checkout (ex: SOUMELIMAIS). Curado manualmente."""
    codigo = models.CharField(max_length=60, unique=True)
    descricao = models.CharField(max_length=255, blank=True, default="")
    tipo_desconto = models.CharField(max_length=20, default="porcentagem")  # 'porcentagem' | 'fixo'
    valor_desconto = models.FloatField(default=0.0)
    valor_minimo = models.FloatField(default=0.0)
    validade = models.DateField(null=True, blank=True)
    ativo = models.BooleanField(default=True)
    # Códigos descobertos automaticamente não têm associação comprovada com um
    # produto. A marca não depende da descrição, que pode ser editada no painel.
    automatico = models.BooleanField(default=False, db_index=True)
    # macro_categorias em que o cupom é válido, separadas por vírgula. Vazio = vale p/ todas.
    # Usado para NÃO sugerir um código que não se aplica ao item (cupons não acumulam).
    categorias = models.CharField(max_length=255, blank=True, default="")

    def aplica_em(self, produto) -> bool:
        """True se este código de checkout é válido para o produto (categoria + mínimo + validade)."""
        from django.utils import timezone
        if not self.ativo:
            return False
        if self.validade and self.validade < timezone.now().date():
            return False
        if self.valor_minimo and produto.preco_com_cupom < self.valor_minimo:
            return False
        cats = [c.strip().lower() for c in self.categorias.split(",") if c.strip()]
        if cats:
            alvo = (produto.macro_categoria or "").strip().lower()
            if alvo not in cats:
                return False
        return True

    def __str__(self):
        return f"{self.codigo} ({self.valor_desconto}{'%' if self.tipo_desconto=='porcentagem' else ' R$'})"


class CanalMonitorado(models.Model):
    """Fonte curada (canal público de ofertas no Telegram) que o worker lê e
    RE-DIVULGA trocando os links pela tag de afiliado do dono (B4). É como
    BlueBot/Pro Afiliados operam: alto volume, baixa manutenção.

    Cuidado (ético/ToS): re-divulgar deals curados de terceiros é área cinzenta.
    Opt-in por usuário; trocar tag de afiliado é padrão no nicho."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="canais_monitorados")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="canais_monitorados",
    )
    # Canal-fonte no Telegram: @username público ou id numérico (-100...).
    handle = models.CharField(max_length=120)
    # Destino da re-divulgação (grupo do próprio usuário).
    destino_canal = models.CharField(max_length=20, default="whatsapp")  # whatsapp | telegram
    destino_grupo_id = models.CharField(max_length=100)
    ativo = models.BooleanField(default=True)
    # Último id de mensagem já processado (evita reprocessar no restart do worker).
    ultimo_id = models.BigIntegerField(default=0)

    def __str__(self):
        return f"{self.handle} → {self.destino_grupo_id} ({self.destino_canal})"


class EnvioCanal(models.Model):
    """Dedup do fluxo de canais curados: não re-divulga a MESMA oferta 2x por usuário.
    Chave = hash da URL-fonte do produto (HistoricoEnvio exige Produto; aqui não há)."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="envios_canal")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="envios_canal",
    )
    chave = models.CharField(max_length=64, db_index=True)  # sha1 da url-fonte
    data = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        unique_together = ("owner", "chave")


class ConfiguracaoEnvio(models.Model):
    """Regra de divulgação: qual nicho vai para qual grupo, com que frequência."""
    # Dono da regra (multi-tenant). null = regras legadas, migradas p/ owner default.
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              null=True, blank=True, db_index=True,
                              related_name="configuracoes")
    organization = models.ForeignKey(
        "accounts.Organization", on_delete=models.CASCADE, null=True, blank=True,
        db_index=True, related_name="configuracoes",
    )
    macro_categoria = models.CharField(max_length=100, blank=True, default="")
    # Sub-nicho opcional: só envia itens cujo nome casa com algum destes termos
    # (separados por vírgula). Ex: "aspirador robo, robot vacuum, robô aspirador".
    #
    # TextField e não CharField(255): a semântica sempre foi "vários sub-nichos em
    # OU" — todo consumidor faz split(",") (content_ranking.py, o scraper da Amazon
    # e a busca por termo) — mas o teto de 255 obrigava a criar uma regra por
    # sub-nicho para o MESMO
    # grupo. Um macro-nicho grande sozinho já passava do limite: Eletrodomésticos
    # soma 395 caracteres de termos. Não há índice nem unicidade sobre esta coluna,
    # então soltar o tamanho não custa nada no banco.
    termo_busca = models.TextField(blank=True, default="")
    # Termos que ELIMINAM o item, mesma semântica de vírgula do `termo_busca`.
    # Sem isto o nicho só sabia incluir: "fone" trazia capinha de fone, película de
    # fone e suporte de fone, e a regra não tinha como dizer que não.
    termos_negativos = models.TextField(blank=True, default="")
    # Faixa de preço do nicho, avaliada sobre o preço FINAL (já com cupom), não
    # sobre a vitrine — é o valor que o comprador vê no checkout que define se o
    # item pertence àquele grupo.
    preco_min = models.FloatField(null=True, blank=True)
    preco_max = models.FloatField(null=True, blank=True)
    # Canal de envio: 'whatsapp' (grupo @g.us) | 'telegram' (chat/channel id).
    canal = models.CharField(max_length=20, default="whatsapp")
    # Filtro opcional de marketplace ('' = qualquer). Ex: só 'mercadolivre'.
    marketplace = models.CharField(max_length=20, blank=True, default="")
    programas = models.ManyToManyField(ProgramaAfiliado, blank=True,
                                       related_name="configuracoes")
    incluir_restritos = models.BooleanField(default=True)
    incluir_sem_desconto = models.BooleanField(default=True)
    grupo_id = models.CharField(max_length=100)          # ex '12345@g.us' (WA) ou '@canal'/-100... (TG)
    grupo_nome = models.CharField(max_length=255, blank=True, default="")
    # O que esta regra publica. 'ofertas' é tudo que existia: produto ou cupom com
    # produtos comprovados, um item por envio. 'aviso_cupons' é o broadcast de
    # códigos novos, sem produto — ver ofertas.enviar_aviso_cupons.
    TIPO_OFERTAS = "ofertas"
    TIPO_AVISO_CUPONS = "aviso_cupons"
    TIPOS = [(TIPO_OFERTAS, "Ofertas e cupons com produto"),
             (TIPO_AVISO_CUPONS, "Aviso de cupons novos")]
    tipo = models.CharField(max_length=20, choices=TIPOS, default=TIPO_OFERTAS)
    intervalo_minutos = models.PositiveIntegerField(default=60)
    # Janela de envio (hora local 0-23). Só envia dentro de [inicio, fim).
    # Se fim <= inicio, a janela cruza a meia-noite (ex: 20→6).
    janela_inicio = models.PositiveSmallIntegerField(default=8)
    janela_fim = models.PositiveSmallIntegerField(default=20)
    # Dias da semana permitidos, em ISO (1=segunda … 7=domingo), separados por
    # vírgula. VAZIO = todos os dias, que é o comportamento de sempre — assim toda
    # regra já existente continua igual sem precisar preencher nada.
    dias_semana = models.CharField(max_length=20, blank=True, default="")
    min_desconto_percent = models.FloatField(default=15.0)
    # Anti-repetição do MESMO produto p/ este grupo (não é o ritmo de envio). Oculto na UI.
    horas_cooldown = models.PositiveIntegerField(default=24)
    ativo = models.BooleanField(default=True)
    ultimo_envio = models.DateTimeField(null=True, blank=True)
    # Próximo envio agendado com jitter já aplicado (anti-robótico). None = envia já.
    proximo_envio = models.DateTimeField(null=True, blank=True)
    max_envios_dia = models.PositiveIntegerField(default=20)
    falhas_consecutivas = models.PositiveIntegerField(default=0)
    pausar_apos_falhas = models.PositiveIntegerField(default=5)
    motivo_pausa = models.CharField(max_length=255, blank=True, default="")
    # Freio temporário depois de falhas seguidas. Antes o freio era `ativo=False`, que
    # só ação humana desfaz: a automação morria em silêncio e o usuário descobria dias
    # depois que não recebia mais nada ("programei e durou só ontem"). `ativo` volta a
    # ser exclusivamente a chave do usuário; o freio automático mora aqui e expira.
    pausada_ate = models.DateTimeField(null=True, blank=True)
    variante_template = models.CharField(max_length=10, default="alternar")
    nome_marca = models.CharField(max_length=80, blank=True, default="")
    tom_marca = models.CharField(max_length=20, blank=True, default="")
    nivel_emoji = models.PositiveSmallIntegerField(null=True, blank=True)
    chamada_acao = models.CharField(max_length=120, blank=True, default="")
    divulgacao_afiliado = models.CharField(max_length=180, blank=True, default="")
    template_a = models.TextField(blank=True, default="")
    template_b = models.TextField(blank=True, default="")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(tipo="aviso_cupons")
                    | ~models.Q(marketplace="")
                    | models.Q(ativo=False)
                ),
                name="aviso_cupom_ativo_exige_marketplace",
            ),
        ]

    def clean(self):
        super().clean()
        if self.tipo == self.TIPO_AVISO_CUPONS and self.ativo and not self.marketplace:
            from django.core.exceptions import ValidationError
            raise ValidationError({
                "marketplace": "Escolha Mercado Livre, Amazon ou Shopee para o aviso de cupons.",
            })

    def dentro_da_janela(self, agora) -> bool:
        """True se a hora local de `agora` está na janela de envio."""
        h = timezone.localtime(agora).hour
        i, f = self.janela_inicio, self.janela_fim
        if i == f:
            return True                      # janela 24h
        if i < f:
            return i <= h < f                # mesma data: 8..20
        return h >= i or h < f               # cruza meia-noite: 20..6

    def dias_permitidos(self) -> set:
        """Dias ISO habilitados. Conjunto vazio significa 'todos os dias'."""
        dias = set()
        for parte in str(self.dias_semana or "").split(","):
            parte = parte.strip()
            if parte.isdigit() and 1 <= int(parte) <= 7:
                dias.add(int(parte))
        return dias

    def dia_permitido(self, agora) -> bool:
        """True se o dia da semana LOCAL de `agora` está habilitado nesta regra.

        Local, e não UTC, pelo mesmo motivo de `dentro_da_janela`: às 22h de sábado
        em São Paulo já é domingo em UTC, e a regra pararia (ou começaria) um dia
        antes do que o usuário marcou na tela.
        """
        dias = self.dias_permitidos()
        return not dias or timezone.localtime(agora).isoweekday() in dias

    def proximo_dia_habilitado(self, agora):
        """Início do próximo dia local em que esta regra pode enviar.

        Usado pelo freio automático: reagendar para "amanhã" cru faria a regra
        acordar num dia que o usuário desmarcou, gastar um tick e dormir de novo.
        """
        local = timezone.localtime(agora)
        for adiante in range(1, 9):
            candidato = (local + timedelta(days=adiante)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            if self.dia_permitido(candidato):
                return candidato
        return local + timedelta(days=1)

    def agendar_proximo(self, agora):
        """Define proximo_envio = agora + intervalo ± jitter(1-10min). Anti-padrão robótico."""
        import random
        jitter = random.randint(1, 10) * random.choice((-1, 1))
        minutos = max(1, self.intervalo_minutos + jitter)
        self.proximo_envio = agora + timedelta(minutes=minutos)

    def frear(self, agora, motivo: str):
        """Aplica o freio automático até o próximo dia habilitado."""
        self.motivo_pausa = str(motivo or "Falhas consecutivas")[:255]
        self.pausada_ate = self.proximo_dia_habilitado(agora)

    def freio_ativo(self, agora) -> bool:
        """True enquanto o freio automático vale. Ao expirar, limpa-se sozinho."""
        if self.pausada_ate is None:
            return False
        if agora < self.pausada_ate:
            return True
        self.pausada_ate = None
        self.falhas_consecutivas = 0
        return False

    def __str__(self):
        return f"{self.macro_categoria} → {self.grupo_nome or self.grupo_id} (a cada {self.intervalo_minutos}min)"

"""Quanto a IA custou este mês — medido, não estimado.

O teto do produto é R$350/mês incluindo Fly e IA, e até agora o repositório não
contava um único token: não havia acumulação de `usage` em lugar nenhum, então a
única resposta possível para "quanto a IA gastou" era abrir o console da
Anthropic. Um teto que ninguém consegue observar não é um teto.

O que este módulo faz é pequeno de propósito: soma tokens por mês e por modelo, e
converte para dinheiro com a tabela de preços em `settings`. Nada aqui bloqueia
chamada — a decisão do usuário foi contador com alerta, não interruptor.

Preços em dólar por milhão de tokens; câmbio em `LLM_CAMBIO_BRL`. Contar tokens é
exato; o dinheiro é uma conversão, e a tela diz isso.
"""
import logging

from django.conf import settings
from django.db.models import F
from django.utils import timezone

logger = logging.getLogger(__name__)


def _preco(modelo: str) -> tuple:
    """(US$/MTok entrada, US$/MTok saída) do modelo, com fallback do Haiku."""
    tabela = getattr(settings, "LLM_PRECOS_USD_MTOK", {}) or {}
    for chave, valores in tabela.items():
        if chave and chave in modelo:
            return float(valores[0]), float(valores[1])
    return 1.0, 5.0  # Haiku 4.5: US$1 entrada / US$5 saída por milhão


def custo_brl(modelo: str, entrada: int, saida: int) -> float:
    entrada_usd, saida_usd = _preco(modelo)
    cambio = float(getattr(settings, "LLM_CAMBIO_BRL", 5.12) or 5.12)
    usd = (entrada / 1_000_000) * entrada_usd + (saida / 1_000_000) * saida_usd
    return usd * cambio


def registrar_uso(resposta, *, origem: str, modelo: str = "") -> None:
    """Acumula o `usage` de uma resposta da Anthropic. Nunca levanta.

    Contabilidade não pode derrubar o funil: qualquer falha aqui vira log e o
    envio segue. O acumulado é por (mês, modelo, origem) para responder tanto
    "quanto gastei" quanto "com o quê".
    """
    try:
        uso = getattr(resposta, "usage", None)
        if uso is None:
            return
        entrada = int(getattr(uso, "input_tokens", 0) or 0)
        saida = int(getattr(uso, "output_tokens", 0) or 0)
        # Cache lido custa uma fração da entrada, mas ainda é entrada: somar aqui
        # mantém o total honesto sem fingir precisão de centavo.
        entrada += int(getattr(uso, "cache_read_input_tokens", 0) or 0)
        entrada += int(getattr(uso, "cache_creation_input_tokens", 0) or 0)
        if not (entrada or saida):
            return

        from apps.scrapers.models import GastoIA

        competencia = timezone.localdate().replace(day=1)
        nome_modelo = (modelo or getattr(resposta, "model", "")
                       or getattr(settings, "LLM_MODELO", ""))[:80]
        linha, criada = GastoIA.objects.get_or_create(
            competencia=competencia, modelo=nome_modelo, origem=origem[:40],
        )
        if criada:
            GastoIA.objects.filter(pk=linha.pk).update(
                chamadas=1, tokens_entrada=entrada, tokens_saida=saida)
            return
        GastoIA.objects.filter(pk=linha.pk).update(
            chamadas=F("chamadas") + 1,
            tokens_entrada=F("tokens_entrada") + entrada,
            tokens_saida=F("tokens_saida") + saida,
        )
    except Exception:
        logger.exception("Falha ao contabilizar uso de IA (origem=%s)", origem)


def resumo_do_mes(competencia=None) -> dict:
    """Total do mês: chamadas, tokens e o custo convertido, por origem."""
    from apps.scrapers.models import GastoIA

    competencia = competencia or timezone.localdate().replace(day=1)
    linhas = list(GastoIA.objects.filter(competencia=competencia))
    por_origem = []
    total = 0.0
    for linha in linhas:
        valor = custo_brl(linha.modelo, linha.tokens_entrada, linha.tokens_saida)
        total += valor
        por_origem.append({
            "origem": linha.origem, "modelo": linha.modelo,
            "chamadas": linha.chamadas,
            "tokens_entrada": linha.tokens_entrada,
            "tokens_saida": linha.tokens_saida,
            "custo_brl": round(valor, 2),
        })
    por_origem.sort(key=lambda item: item["custo_brl"], reverse=True)
    limite = float(getattr(settings, "LLM_TETO_BRL_MES", 0) or 0)
    return {
        "competencia": competencia,
        "disponivel": True,
        "custo_brl": round(total, 2),
        "teto_brl": limite,
        "estourou": bool(limite) and total >= limite,
        "por_origem": por_origem,
    }

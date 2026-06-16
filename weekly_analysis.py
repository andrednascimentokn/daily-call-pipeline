"""Weekly overview synthesis via DeepSeek v4-pro.

Aggregates a week of already-distilled daily analyses (analysis.json) plus weekly
metrics and the agent's running context.json, and produces a HIGH-LEVEL weekly
overview — NOT a call-by-call breakdown (that lives in the daily reports).

Reuses the same OpenAI-compatible DeepSeek client config / retry strategy as
deepseek_analysis.py. If there are no daily analyses for the week (agent had no
transcribed calls), returns a deterministic metrics-only overview without calling
the model — honest and cheap.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI

from deepseek_analysis import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_FALLBACK_MODEL,
    REAL_ESTATE_RULE,
)

log = logging.getLogger("pipeline")

WEEKLY_SYSTEM_PROMPT = """És um senior sales coach a escrever um RESUMO SEMANAL de coaching para um agente comercial em Portugal. Recebes as análises diárias já produzidas ao longo da semana (cada uma já destila forças, fraquezas e ações de um dia) mais as métricas agregadas da semana e o histórico do agente.

O teu trabalho NÃO é repetir o detalhe chamada-a-chamada (isso já está nos relatórios diários). O teu trabalho é dar uma VISÃO GERAL da semana: os padrões que se repetiram, a evolução, e onde focar a próxima semana. Sê específico mas de alto nível — agrega, não enumeres cada chamada.

ESCREVE TODO O CONTEÚDO EM PORTUGUÊS DE PORTUGAL (PT-PT). Sem platitudes.

O OUTPUT DEVE SER UM ÚNICO OBJETO JSON VÁLIDO com EXATAMENTE estas seis chaves:

{
  "week_overview": "<2-4 frases. A grande história da semana: principal força consolidada E principal gap. Usa **negrito** no achado-chave. Refere o volume (X chamadas na semana, Y transcritas).>",
  "metrics_commentary": "<1-2 frases sobre o volume e a consistência ao longo da semana, e tendência face a semanas anteriores se o histórico permitir.>",
  "recurring_strengths": [
    {"title": "<força que apareceu repetidamente na semana>", "description": "<porque é consistente, com referência aos dias/contactos quando útil>"}
  ],
  "recurring_weaknesses": [
    {"title": "<fraqueza recorrente na semana>", "description": "<o padrão observado vários dias>", "action": "<recomendação concreta com *frase exemplo em itálico* em PT-PT>"}
  ],
  "weekly_trend": "<1-3 frases: a semana melhorou, estabilizou ou piorou face ao histórico? Porquê?>",
  "focus_next_week": [
    {"title": "<prioridade para a próxima semana>", "description": "<porquê e como, 1-2 frases>", "example_phrase": "<frase exemplo em PT-PT que o agente deve usar>"}
  ]
}

REGRAS:
- 3 a 4 itens em recurring_strengths
- 4 a 6 itens em recurring_weaknesses
- 3 a 5 itens em focus_next_week
- Nunca incluir markdown fences (```), nada além do JSON
- Frases exemplo em PT-PT envolvidas em asteriscos (*frase*)
- Visão de SEMANA, agregada. Não listes cada chamada individual.
"""


def _summarize_daily(analysis: Dict[str, Any]) -> str:
    """Compact a single day's analysis.json into a short text block for the prompt."""
    parts: List[str] = []
    oa = analysis.get("overall_assessment", "")
    if oa:
        parts.append(f"Avaliação: {oa}")
    strengths = analysis.get("what_agent_does_well", [])
    if strengths:
        titles = [s.get("title", "") for s in strengths if isinstance(s, dict)]
        parts.append("Forças: " + "; ".join(t for t in titles if t))
    weaknesses = analysis.get("critical_weaknesses", [])
    if weaknesses:
        titles = [w.get("title", "") for w in weaknesses if isinstance(w, dict)]
        parts.append("Fraquezas: " + "; ".join(t for t in titles if t))
    actions = analysis.get("priority_coaching_actions", [])
    if actions:
        titles = [a.get("title", "") for a in actions if isinstance(a, dict)]
        parts.append("Ações prioritárias: " + "; ".join(t for t in titles if t))
    return "\n".join(parts)


def _build_user_prompt(
    agent: Dict[str, Any],
    week_label: str,
    daily_analyses: List[Dict[str, Any]],
    weekly_metrics: Dict[str, Any],
    context: Dict[str, Any],
) -> str:
    sections: List[str] = []
    sections.append(f"AGENTE: {agent['display']}")
    sections.append(
        "CONTEXTO DO NEGÓCIO: Real Estate Invest vende imóveis em desenvolvimentos "
        "portugueses específicos. O agente faz contacto, agenda visitas, negoceia "
        "condições e acompanha o pipeline até à reserva/escritura."
    )
    sections.append(f"SEMANA: {week_label}")
    sections.append(REAL_ESTATE_RULE)

    # Aggregate weekly metrics + per-day breakdown
    sections.append("\nMÉTRICAS DA SEMANA:")
    sections.append(
        f"  Total: {weekly_metrics.get('total_calls', 0)} chamadas, "
        f"{weekly_metrics.get('completed', 0)} completadas, "
        f"{weekly_metrics.get('transcribed', 0)} transcritas, "
        f"em {weekly_metrics.get('days_active', 0)} dias com atividade."
    )
    for d in weekly_metrics.get("per_day", []):
        sections.append(
            f"  - {d['date']}: {d['total']} chamadas, {d['completed']} completadas, "
            f"{d['transcribed']} transcritas"
        )

    # Historical trend from context.json
    insights = context.get("coaching_insights", {}) if context else {}
    history = context.get("metrics_history", []) if context else []
    if history:
        sections.append("\nHISTÓRICO (métricas recentes, para tendência):")
        for h in history[-14:]:
            sections.append(
                f"  - {h.get('date')}: {h.get('total_calls', 0)} chamadas, "
                f"{h.get('completed_calls', 0)} completadas, {h.get('transcribed', 0)} transcritas"
            )
    if insights:
        sections.append(f"\nForças acumuladas: {insights.get('strengths', [])}")
        sections.append(f"Fraquezas acumuladas: {insights.get('weaknesses', [])}")
        sections.append(f"Padrões acumulados: {insights.get('patterns', [])}")
        sections.append(f"Evolução até agora: {insights.get('evolution', '')}")

    # The week's daily analyses (already distilled)
    sections.append("\nANÁLISES DIÁRIAS DA SEMANA (já destiladas, para sintetizares):")
    if not daily_analyses:
        sections.append("(nenhuma — sem chamadas transcritas esta semana)")
    for item in daily_analyses:
        sections.append(f"\n=== {item['date']} ===")
        sections.append(_summarize_daily(item["analysis"]))

    sections.append(
        "\nDevolve UM ÚNICO objeto JSON com as 6 chaves do system prompt. "
        "Visão agregada da SEMANA, sem markdown fences, tudo em PT-PT."
    )
    return "\n".join(sections)


def _extract_json(raw: str) -> Dict[str, Any]:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _metrics_only_overview(agent: Dict[str, Any], weekly_metrics: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic overview when there are no daily analyses (no transcribed calls)."""
    total = weekly_metrics.get("total_calls", 0)
    completed = weekly_metrics.get("completed", 0)
    days = weekly_metrics.get("days_active", 0)
    return {
        "week_overview": (
            f"**Sem chamadas gravadas/transcritas esta semana** para {agent['display']}, "
            f"por isso não há base para análise de coaching. Registaram-se {total} chamadas "
            f"({completed} completadas) em {days} dias com atividade."
        ),
        "metrics_commentary": (
            f"{total} chamadas na semana, {completed} completadas. "
            "Sem gravações, não é possível avaliar a qualidade das conversas."
        ),
        "recurring_strengths": [],
        "recurring_weaknesses": [
            {
                "title": "Ausência de gravações para análise",
                "description": (
                    "Nenhuma chamada da semana foi gravada/transcrita, o que impede o "
                    "coaching baseado em conversas reais."
                ),
                "action": (
                    "Garantir que as chamadas relevantes ficam gravadas no HubSpot para "
                    "permitir análise. *Confirma que a gravação está ativa antes de iniciar a chamada.*"
                ),
            }
        ],
        "weekly_trend": "Sem dados de conversa suficientes para avaliar tendência esta semana.",
        "focus_next_week": [
            {
                "title": "Ativar gravação das chamadas",
                "description": "Sem gravações não há matéria-prima para coaching semanal.",
                "example_phrase": "Vou rever as minhas chamadas gravadas para identificar melhorias.",
            }
        ],
    }


def analyze_weekly(
    agent: Dict[str, Any],
    week_label: str,
    daily_analyses: List[Dict[str, Any]],
    weekly_metrics: Dict[str, Any],
    context: Dict[str, Any],
    *,
    api_key: Optional[str] = None,
    max_retries: int = 5,
) -> Dict[str, Any]:
    """Return a weekly-overview dict matching the build_weekly_report schema.

    If there are no daily analyses for the week, returns a deterministic
    metrics-only overview (no model call).
    """
    if not daily_analyses:
        log.info(f"[{agent['display']}] no daily analyses for week — metrics-only overview")
        return _metrics_only_overview(agent, weekly_metrics)

    key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL, timeout=120.0)
    user_prompt = _build_user_prompt(agent, week_label, daily_analyses, weekly_metrics, context)

    required = {
        "week_overview", "metrics_commentary", "recurring_strengths",
        "recurring_weaknesses", "weekly_trend", "focus_next_week",
    }

    last_exc: Optional[BaseException] = None
    temperature = 0.4
    fallback_threshold = max(1, max_retries // 2)
    for attempt in range(max_retries):
        model = DEEPSEEK_MODEL if attempt < fallback_threshold else DEEPSEEK_FALLBACK_MODEL
        try:
            log.info(f"[{agent['display']}] DeepSeek weekly attempt {attempt+1}/{max_retries} model={model} temp={temperature}")
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": WEEKLY_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=12000,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            if not raw.strip():
                raise ValueError(f"empty content from {model} (usage={resp.usage})")
            obj = _extract_json(raw)
            missing = required - set(obj.keys())
            if missing:
                raise ValueError(f"missing keys in {model} output: {missing}")
            log.info(f"[{agent['display']}] DeepSeek weekly OK (model={model}, usage={resp.usage})")
            return obj
        except Exception as exc:
            last_exc = exc
            log.warning(f"[{agent['display']}] DeepSeek weekly attempt {attempt+1} ({model}) failed: {exc}")
            temperature = max(0.1, temperature - 0.05)
            time.sleep(min(60, 5 * (2 ** attempt)))
    assert last_exc is not None
    raise last_exc

"""Single-shot coaching analysis via DeepSeek v4-pro.

Receives all of a day's transcripts + historical context.json for an agent
and returns:
  - analysis: the 5 CLAUDE.md keys (overall_assessment, what_agent_does_well,
    critical_weaknesses, call_summary, priority_coaching_actions)
  - coaching_insights_update: a dict to merge into context.json's coaching_insights
    field (strengths, weaknesses, patterns, evolution)

Uses the OpenAI-compatible DeepSeek endpoint. JSON mode is enabled via
`response_format={"type": "json_object"}`. The static parts of the prompt
(system + agent background) come FIRST in the conversation so DeepSeek's
automatic prompt cache hits on subsequent days (cache hit price is ~120× cheaper).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from openai import OpenAI

log = logging.getLogger(__name__)

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
# deepseek-v4-pro is a reasoning model (burns reasoning_tokens internally) — higher
# quality but slower and needs a generous max_tokens budget. deepseek-v4-flash is
# the non-reasoning sibling, cheaper/faster, used as fallback when pro is overloaded.
DEEPSEEK_MODEL = "deepseek-v4-pro"
DEEPSEEK_FALLBACK_MODEL = "deepseek-v4-flash"

# Max chars of transcript to send per call (~ a few thousand tokens). Long calls
# get truncated at the tail; the head usually contains the discovery + objection
# handling, which is what matters for coaching.
TRANSCRIPT_CHAR_CAP = 18000

REAL_ESTATE_RULE = (
    "REGRA CRÍTICA — IMOBILIÁRIO: Este agente vende imóveis num único development "
    "(Conde da Ribeira, Copa, etc.). Cross-sell entre developments NÃO existe nesta "
    "operação. NUNCA sugerir como ponto fraco ou ação de coaching que o agente "
    "'devia ter oferecido outro empreendimento' (ex: Santo Amaro 154). Foca-te em: "
    "negociação, gestão de objeções (preço, financiamento, tipologia), agendamento "
    "de visitas, qualificação de leads, follow-up disciplinado, inteligência "
    "emocional, soluções criativas dentro do produto que o agente vende. Quando o "
    "orçamento do prospect não bate certo, analisa como o agente redirecionou para "
    "tipologias disponíveis no MESMO development, negociou condições, ou qualificou "
    "graciosamente — não recomendes redirecionar para outros developments."
)

SYSTEM_PROMPT = """És um senior sales coach a escrever um relatório diário de coaching para um agente comercial em Portugal. O teu trabalho é analisar TODAS as chamadas do dia de uma vez e produzir feedback brutalmente honesto, específico, com referências a contactos, momentos e quotes concretas das transcrições.

ESCREVE TODO O CONTEÚDO EM PORTUGUÊS DE PORTUGAL (PT-PT). Sem platitudes, sem generalidades. Cada afirmação tem de poder ser ligada a um momento concreto de uma chamada concreta.

O OUTPUT DEVE SER UM ÚNICO OBJETO JSON VÁLIDO com EXATAMENTE estas seis chaves:

{
  "overall_assessment": "<2-3 frases. Destaca a principal força E o principal gap do dia. Usa **negrito** para o achado-chave. Refere métricas (X chamadas, Y completadas, Z transcritas).>",
  "what_agent_does_well": [
    {"title": "<força específica observada em várias chamadas>", "description": "<descrição com referências a contactos e momentos concretos>"}
  ],
  "critical_weaknesses": [
    {
      "title": "<descrição direta da fraqueza>",
      "description": "<o que aconteceu, com nomes de contactos e contexto>",
      "key_missed_moments": ["<contacto (duração): citação ou momento — o que deveria ter sido feito>", "..."],
      "action": "<recomendação concreta com FRASE EXEMPLO em itálico (envolver em *...*) em PT-PT>"
    }
  ],
  "call_summary": [
    {"contact": "<nome>", "duration": "<X min>", "outcome": "<committed/warm/near_commit/soft_hold/lost/no_contact/blocked/visit_scheduled/etc>", "key_issue": "<ponto-chave em 1 frase>"}
  ],
  "priority_coaching_actions": [
    {"title": "<ação prioritária>", "description": "<1-2 frases a explicar porquê e como>", "example_phrase": "<frase exemplo em PT-PT que o agente deve usar>"}
  ],
  "coaching_insights_update": {
    "strengths": ["<força confirmada hoje (lista de strings curtas, max 6)>"],
    "weaknesses": ["<fraqueza observada hoje (lista de strings curtas, max 6)>"],
    "patterns": ["<padrão recorrente (ex: 'aceita vou pensar sem follow-up agendado em 5+/8 chamadas')>"],
    "evolution": "<1-2 frases: o que mudou em relação ao histórico passado — melhorou? piorou? igual?>"
  }
}

REGRAS:
- 3 a 4 itens em what_agent_does_well
- 5 a 7 itens em critical_weaknesses
- exatamente 5 itens em priority_coaching_actions
- call_summary tem 1 item por chamada transcrita
- Nunca incluir markdown fences (```), nada além do JSON
- Frases exemplo em **PT-PT** envolvidas em asteriscos (*frase*)
- Se uma chamada não pode ser avaliada (transcrição < 50 palavras), omite-a do call_summary
"""


def _build_user_prompt(
    agent: Dict[str, Any],
    date_str: str,
    calls_meta: List[Dict[str, Any]],
    transcripts: Dict[str, str],
    context: Dict[str, Any],
) -> str:
    business = agent.get("business", "")
    business_blurb = {
        "nova_digital": (
            "NOVA Digital vende formações IEFP financiadas em IA/tech a profissionais portugueses, "
            "via telefone. O agente liga a prospects que mostraram interesse."
        ),
        "real_estate": (
            "Real Estate Invest vende imóveis em desenvolvimentos portugueses específicos "
            "(Conde da Ribeira, Copa, etc.). O agente faz primeiro contacto, agenda visitas, "
            "negoceia condições e acompanha o pipeline até à reserva/escritura."
        ),
    }.get(business, "")

    sections: List[str] = []
    sections.append(f"AGENTE: {agent['display']}")
    sections.append(f"CONTEXTO DO NEGÓCIO: {business_blurb}")
    sections.append(f"DATA: {date_str}")

    if business == "real_estate":
        sections.append(REAL_ESTATE_RULE)

    # Historical context block — short but informative
    insights = context.get("coaching_insights", {}) if context else {}
    history = context.get("metrics_history", []) if context else []
    recent = history[-7:] if history else []
    sections.append("\nCONTEXTO HISTÓRICO (últimos dias + insights acumulados):")
    sections.append("Métricas recentes:")
    for h in recent:
        sections.append(
            f"  - {h.get('date')}: {h.get('total_calls', 0)} chamadas, "
            f"{h.get('completed_calls', 0)} completadas, {h.get('transcribed', 0)} transcritas"
        )
    if insights:
        sections.append(f"Forças anteriormente identificadas: {insights.get('strengths', [])}")
        sections.append(f"Fraquezas anteriormente identificadas: {insights.get('weaknesses', [])}")
        sections.append(f"Padrões anteriormente identificados: {insights.get('patterns', [])}")
        sections.append(f"Evolução até agora: {insights.get('evolution', '')}")

    # Day-level stats
    total = len(calls_meta)
    completed = sum(1 for c in calls_meta if c.get("status") == "COMPLETED")
    with_transcript = sum(1 for c in calls_meta if c.get("has_transcript"))
    sections.append(f"\nESTATÍSTICAS DO DIA: {total} chamadas, {completed} completadas, {with_transcript} transcritas")

    # Per-call payload
    sections.append("\nCHAMADAS COM TRANSCRIÇÃO:")
    transcribed_calls = [c for c in calls_meta if c.get("has_transcript") and transcripts.get(c["call_id"])]
    if not transcribed_calls:
        sections.append("(nenhuma)")
    for c in transcribed_calls:
        cid = c["call_id"]
        contact = c.get("contact_name", f"Call {cid}")
        duration = c.get("duration_str", f"{c.get('duration_sec', 0)//60} min")
        direction = c.get("direction", "")
        text = transcripts[cid][:TRANSCRIPT_CHAR_CAP]
        if len(transcripts[cid]) > TRANSCRIPT_CHAR_CAP:
            text += "\n[...transcrição truncada...]"
        sections.append(f"\n--- {contact} | {duration} | {direction} | id={cid} ---")
        sections.append(text)

    sections.append(
        "\nDevolve UM ÚNICO objeto JSON com as 6 chaves descritas no system prompt. "
        "Sem markdown fences. Tudo em PT-PT."
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


def analyze(
    agent: Dict[str, Any],
    date_str: str,
    calls_meta: List[Dict[str, Any]],
    transcripts: Dict[str, str],
    context: Dict[str, Any],
    *,
    api_key: Optional[str] = None,
    max_retries: int = 5,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run single-shot analysis. Returns (analysis_dict, coaching_insights_update_dict).

    The first dict matches the analysis.json schema expected by build_report.py.
    The second dict should be merged into context.json's `coaching_insights`.

    Retries with exponential backoff (max 60s/attempt). After half the retries on the
    primary (reasoning) model, falls back to deepseek-v4-flash to survive 503 storms.
    """
    key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")

    client = OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL, timeout=120.0)
    user_prompt = _build_user_prompt(agent, date_str, calls_meta, transcripts, context)

    required = {
        "overall_assessment", "what_agent_does_well", "critical_weaknesses",
        "call_summary", "priority_coaching_actions",
    }

    last_exc: Optional[BaseException] = None
    temperature = 0.4
    fallback_threshold = max(1, max_retries // 2)  # switch model after this many failures
    for attempt in range(max_retries):
        model = DEEPSEEK_MODEL if attempt < fallback_threshold else DEEPSEEK_FALLBACK_MODEL
        try:
            log.info(f"[{agent['display']}] DeepSeek analyze attempt {attempt+1}/{max_retries} model={model} temp={temperature}")
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=16000,
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content or ""
            if not raw.strip():
                raise ValueError(f"empty content from {model} (usage={resp.usage})")
            obj = _extract_json(raw)
            missing = required - set(obj.keys())
            if missing:
                raise ValueError(f"missing keys in {model} output: {missing}")
            insights_update = obj.pop("coaching_insights_update", {}) or {}
            log.info(f"[{agent['display']}] DeepSeek OK (model={model}, usage={resp.usage})")
            return obj, insights_update
        except Exception as exc:
            last_exc = exc
            log.warning(f"[{agent['display']}] DeepSeek attempt {attempt+1} ({model}) failed: {exc}")
            temperature = max(0.1, temperature - 0.05)
            time.sleep(min(60, 5 * (2 ** attempt)))  # 5s, 10s, 20s, 40s, 60s
    assert last_exc is not None
    raise last_exc


def merge_coaching_insights(existing: Dict[str, Any], update: Dict[str, Any], date_label: str) -> Dict[str, Any]:
    """Merge today's insights into the running `coaching_insights` block.

    Strategy: replace strengths/weaknesses/patterns with today's view (they are
    point-in-time observations the model already deduplicates against history).
    Append today's `evolution` note to a dated log so prior context is preserved.
    """
    out = dict(existing) if existing else {}
    if "strengths" in update:
        out["strengths"] = update["strengths"]
    if "weaknesses" in update:
        out["weaknesses"] = update["weaknesses"]
    if "patterns" in update:
        out["patterns"] = update["patterns"]
    if update.get("evolution"):
        evo_log = out.get("evolution_log", [])
        if not isinstance(evo_log, list):
            evo_log = []
        evo_log.append({"date": date_label, "note": update["evolution"]})
        out["evolution_log"] = evo_log[-30:]  # cap at last 30 days
        out["evolution"] = update["evolution"]
    return out

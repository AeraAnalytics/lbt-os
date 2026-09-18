"""
AI Audit Engine — core intelligence layer of LBT OS.

Design:
  1. Pull raw metrics + segment analysis from the metrics service.
  2. Build a rich, root-cause-oriented prompt with full business context.
  3. Call the LLM appropriate for the org's plan.
  4. Persist the audit report to Supabase.
  5. Return the report (truncated for free-tier orgs).

Plan → LLM routing:
  basic   → Ollama (local, $0 cost — one audit/month allowed)
  pro     → Ollama or OpenAI mini (configurable via settings)
  premium → OpenAI GPT-4o  (best reasoning, reserved for premium tier)
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from openai import OpenAI
from postgrest.exceptions import APIError
from supabase import Client
from tenacity import retry, stop_after_attempt, wait_exponential

from ..config import settings
from .metrics import (
    _DEFAULT_BENCHMARK,
    INDUSTRY_BENCHMARKS,
    get_dashboard_metrics,
    get_segment_analysis,
)

# ---------------------------------------------------------------------------
# Per-plan LLM client cache
# ---------------------------------------------------------------------------
_clients: dict[str, OpenAI] = {}

# Monthly audit caps per plan.  None = unlimited.
# Starter: 3  |  Growth: 20  |  Scale: unlimited  |  Enterprise: unlimited
FREE_MONTHLY_AUDIT_LIMIT = 3
PLAN_AUDIT_LIMITS: dict[str, int | None] = {
    "basic":      3,
    "pro":        20,
    "premium":    None,
    "enterprise": None,
}

OPTIONAL_AUDIT_COLUMNS = {
    "biggest_leverage_point",
    "health_rationale",
    "segment_analysis",
}


def _ollama_client() -> OpenAI:
    if "ollama" not in _clients:
        _clients["ollama"] = OpenAI(base_url=f"{settings.ollama_base_url}/v1", api_key="ollama")
    return _clients["ollama"]


def _openai_client() -> OpenAI:
    if "openai" not in _clients:
        _clients["openai"] = OpenAI(api_key=settings.openai_api_key)
    return _clients["openai"]


def _get_llm_client_for_plan(plan: str) -> tuple[OpenAI, str, bool]:
    if plan == "premium":
        return _openai_client(), settings.openai_premium_model, True
    if plan == "pro":
        if settings.llm_provider == "openai":
            return _openai_client(), settings.openai_model, True
        return _ollama_client(), settings.ollama_model, False
    return _ollama_client(), settings.ollama_model, False


# ---------------------------------------------------------------------------
# Monthly audit counter
# ---------------------------------------------------------------------------

def count_audits_this_month(db: Client, org_id: str) -> int:
    first_of_month = date.today().replace(day=1).isoformat()
    result = (
        db.table("audit_reports").select("id", count="exact")
        .eq("org_id", org_id).gte("generated_at", first_of_month).execute()
    )
    return result.count or 0


# ---------------------------------------------------------------------------
# Prompts — root-cause, confidence-aware, interdependency-conscious
# ---------------------------------------------------------------------------

AUDIT_SYSTEM_PROMPT = """
You are a sharp business analyst and operator coach specializing in small local businesses (trades, service, gig economy).

Your job is NOT to describe what the numbers show — the owner can read those. Your job is to:
1. Diagnose WHY the numbers are what they are (root cause, not surface symptom).
2. Quantify the financial impact as specifically as the data allows.
3. Rank recommendations by expected ROI, not by severity alone.
4. Flag interdependencies — some actions MUST happen before others are useful.
5. Calibrate your confidence: fewer data points means wider uncertainty, say so.

RULES:
- Never say "consider improving" — say exactly what to do and why it moves money.
- If a metric is bad, say what's driving it (which source, which cost bucket, which behavior).
- If you can't calculate a dollar impact precisely, give a realistic range with your reasoning.
- If two findings are related, link them explicitly ("fixing X first will make Y easier").
- Severity must reflect financial magnitude, not just deviation from benchmarks.
- Strength findings are only worth flagging if they're genuinely actionable (e.g., "double down on this channel").

Respond ONLY with valid JSON in this exact structure:
{
  "health_score": <0-100 integer>,
  "health_rationale": "<2-3 sentences explaining why this score, referencing specific numbers>",
  "insights": [
    {
      "type": "revenue_leak" | "missed_opportunity" | "inefficiency" | "strength",
      "title": "<specific, diagnostic — not generic>",
      "root_cause": "<what is actually causing this — not just what it is>",
      "detail": "<plain English explanation with specific numbers from the data>",
      "estimated_impact": "<dollar amount or range with methodology, e.g. '$X–$Y based on N leads × avg deal'>",
      "severity": "high" | "medium" | "low",
      "confidence": "high" | "medium" | "low",
      "confidence_note": "<why confidence is high/medium/low — usually sample size>"
    }
  ],
  "recommendations": [
    {
      "priority": <1-based integer>,
      "action": "<specific, executable action — not vague advice>",
      "why": "<exactly how this moves revenue, margin, or retention — with numbers>",
      "expected_impact": "<realistic dollar or % range>",
      "timeframe": "this week" | "this month" | "next quarter",
      "depends_on": "<action that must happen first, or null>",
      "effort": "low" | "medium" | "high"
    }
  ],
  "biggest_leverage_point": "<the single highest-ROI action the owner should do first and why>"
}
""".strip()


def _build_metrics_prompt(
    metrics: dict,
    segments: dict,
    org_name: str,
    industry: str | None,
    benchmark: dict,
) -> str:
    industry_label = industry or "general service business"
    m  = metrics
    sl = segments.get("segments", [])

    # Build segment table
    seg_lines = []
    for s in sl[:8]:
        seg_lines.append(
            f"  {s['source']:20s}  leads={s['leads']:3d}  won={s['won']:3d}  "
            f"conv={s['conversion_rate_pct']:5.1f}%  rev=${s['revenue']:>10,.2f}  "
            f"avg_deal=${s['avg_deal_size']:>8,.2f}  rev/lead=${s['revenue_per_lead']:>7,.2f}"
        )
    segment_table = "\n".join(seg_lines) if seg_lines else "  (no source data available)"

    channel_insight = segments.get("channel_insight") or "(insufficient data for channel comparison)"

    won_leads  = m["leads"]["won"]
    avg_deal   = m["revenue"]["avg_deal_size"]
    net_income = m["revenue"]["profit"] - m["expenses"]["total"]

    return f"""
Business:          {org_name}
Industry:          {industry_label}
Analysis period:   last {m['period_days']} days

=== INDUSTRY BENCHMARKS (target thresholds for this business type) ===
  Conversion rate benchmark:  {benchmark['conversion']:.1f}%
  Gross margin benchmark:     {benchmark['margin']:.1f}%
  Repeat customer benchmark:  {benchmark['repeat']:.1f}%
  Benchmark note: {benchmark['note']}

=== REVENUE ===
  Total Revenue:    ${m['revenue']['total']:,.2f}
  Total Cost:       ${m['revenue']['cost']:,.2f}
  Gross Profit:     ${m['revenue']['profit']:,.2f}
  Profit Margin:    {m['revenue']['margin_pct']}%
  Avg Deal Size:    ${avg_deal:,.2f}  (based on {won_leads} won deals)

=== LEADS & CONVERSION ===
  Total Leads:        {m['leads']['total']}
  Won (converted):    {m['leads']['won']}
  Lost:               {m['leads']['lost']}
  In-progress:        {m['leads']['total'] - m['leads']['won'] - m['leads']['lost']}
  Conversion Rate:    {m['leads']['conversion_rate_pct']}%  (benchmark: {benchmark['conversion']:.1f}%)
  Missed Follow-Ups:  {m['leads']['missed_follow_ups']} leads with a scheduled follow-up and no contact logged

=== SOURCE ANALYSIS (conversion + revenue by acquisition channel) ===
{segment_table}

Channel insight: {channel_insight}

=== CUSTOMERS ===
  Total Customers:  {m['customers']['total']}
  Repeat:           {m['customers']['repeat']} ({m['customers']['repeat_pct']}%)  (benchmark: {benchmark['repeat']:.1f}%)

=== EXPENSES ===
  Total Expenses:   ${m['expenses']['total']:,.2f}
  By category:      {json.dumps(m['expenses']['by_category'])}

=== OPERATING PICTURE ===
  Net Operating Income (est.): ${net_income:,.2f}
  Expense-to-Revenue ratio:    {(m['expenses']['total'] / m['revenue']['total'] * 100):.1f}% {'(expenses exceed revenue — critical)' if m['expenses']['total'] > m['revenue']['total'] and m['revenue']['total'] > 0 else ''}

Data confidence note: This analysis covers {m['leads']['total']} leads and {m['customers']['total']} customers.
{'Sample size is thin — treat conversion and retention findings as directional, not definitive.' if m['leads']['total'] < 10 else 'Sample size is adequate for reliable pattern detection.'}
""".strip()


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
def _call_llm(prompt: str, plan: str) -> dict[str, Any]:
    client, model, json_mode = _get_llm_client_for_plan(plan)

    kwargs: dict[str, Any] = dict(
        model=model,
        messages=[
            {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        temperature=0.25,
        max_tokens=2500 if plan == "premium" else 2000,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)
    content  = response.choices[0].message.content.strip()

    # Ollama sometimes wraps JSON in markdown code fences — strip them
    if content.startswith("```"):
        content = content.split("```")[1]
        content = content.removeprefix("json")

    return json.loads(content)


# ---------------------------------------------------------------------------
# Free-tier truncation
# ---------------------------------------------------------------------------

def truncate_for_free_tier(report: dict[str, Any]) -> dict[str, Any]:
    truncated = dict(report)
    truncated["insights"]             = report.get("insights", [])[:3]
    truncated["recommendations"]      = []
    truncated["biggest_leverage_point"] = None
    truncated["is_truncated"]         = True
    return truncated


def _insert_audit_report(db: Client, report: dict[str, Any]) -> dict[str, Any]:
    try:
        result = db.table("audit_reports").insert(report).execute()
        return result.data[0]
    except APIError as exc:
        if "Could not find the" not in str(exc):
            raise
        compatible_report = {
            key: value
            for key, value in report.items()
            if key not in OPTIONAL_AUDIT_COLUMNS
        }
        result = db.table("audit_reports").insert(compatible_report).execute()
        saved = result.data[0]
        for key in OPTIONAL_AUDIT_COLUMNS:
            if key in report:
                saved[key] = report[key]
        return saved


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _should_truncate(plan: str) -> bool:
    """Only the free Starter tier gets a truncated report."""
    return plan == "basic"


def run_audit(
    db: Client,
    org_id: str,
    org_name: str,
    industry: str | None,
    plan: str = "basic",
) -> dict[str, Any]:
    period_end   = date.today()
    period_start = period_end - timedelta(days=30)

    metrics  = get_dashboard_metrics(db, org_id, days=30)
    segments = get_segment_analysis(db, org_id, days=30)
    benchmark = INDUSTRY_BENCHMARKS.get(industry or "", _DEFAULT_BENCHMARK)

    prompt    = _build_metrics_prompt(metrics, segments, org_name, industry, benchmark)
    ai_result = _call_llm(prompt, plan)

    _, model, _ = _get_llm_client_for_plan(plan)

    report = {
        "org_id":                 org_id,
        "period_start":           period_start.isoformat(),
        "period_end":             period_end.isoformat(),
        "insights":               ai_result.get("insights", []),
        "recommendations":        ai_result.get("recommendations", []),
        "biggest_leverage_point": ai_result.get("biggest_leverage_point"),
        "health_rationale":       ai_result.get("health_rationale"),
        "raw_metrics":            metrics,
        "segment_analysis":       segments,
        "health_score":           ai_result.get("health_score", 50),
        "model_used":             model,
    }

    saved = _insert_audit_report(db, report)

    if _should_truncate(plan):
        return truncate_for_free_tier(saved)
    return saved


def get_latest_audit(db: Client, org_id: str, plan: str = "pro") -> dict[str, Any] | None:
    result = (
        db.table("audit_reports").select("*")
        .eq("org_id", org_id).order("generated_at", desc=True).limit(1).execute()
    )
    if not result.data:
        return None
    report = result.data[0]
    if _should_truncate(plan):
        return truncate_for_free_tier(report)
    return report

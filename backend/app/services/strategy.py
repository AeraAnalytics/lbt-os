"""
Strategy Intelligence Service — AI strategist + competitor web intelligence.

Gives small business owners a proactive data team that:
1. Answers strategic questions using their own business data.
2. Searches the web for competitors and extracts pricing/service signals.
3. Runs comparative analysis to position the business against the market.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx
from supabase import Client

from .ai_audit import _get_llm_client_for_plan
from .metrics import (
    _DEFAULT_BENCHMARK,
    INDUSTRY_BENCHMARKS,
    get_dashboard_metrics,
    get_segment_analysis,
)

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

STRATEGIST_SYSTEM_PROMPT = """
You are a senior business strategist embedded full-time with a small business owner.
You have complete access to their live business data — revenue, leads, margins, customer retention, and channel performance.

Your role: be their proactive data team, not a generic AI assistant.

RULES:
- Use their SPECIFIC numbers. Say "$47,200 in revenue" not "your revenue."
- Connect data to decisions: "Your referral channel converts at 38% — 12 points above your average — which means adding one more referral per week is worth ~$X in closed revenue."
- Give executable recommendations. Never "consider" or "explore" — say exactly what to do.
- Quantify impact with a range and your reasoning: "$800–$1,400/month based on your avg deal size of $X."
- Prioritize by ROI and owner time: a busy operator needs the highest-leverage action first.
- If they ask about pricing, anchoring, or competitive positioning, use their margin data to reason through it.
- Sound like a senior operator who has seen this business type before, not a consultant padding a report.
""".strip()

COMPETITOR_ANALYSIS_SYSTEM_PROMPT = """
You are a competitive intelligence analyst for a small business.
You have their business metrics AND raw information scraped from competitor websites.

Your job:
1. Extract pricing signals from the competitor pages (ranges, package names, per-hour rates, etc.)
2. Extract service offerings and positioning language
3. Compare the user's implied pricing/positioning against competitors
4. Identify gaps, advantages, and strategic moves
5. Give specific, ranked recommendations

Be direct. The owner wants to know: Am I priced right? What are they doing that I'm not? What should I change?
Format output as structured JSON for programmatic rendering.
""".strip()


# ---------------------------------------------------------------------------
# SSRF protection — fix #1
# ---------------------------------------------------------------------------

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local / AWS metadata service
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),   # carrier-grade NAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _assert_safe_url(url: str) -> None:
    """Raise ValueError if the URL points to a private/internal network address."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("Only HTTP/HTTPS URLs are allowed.")
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("URL is missing a hostname.")
        ip_str = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(ip_str)
        if any(ip in net for net in _PRIVATE_NETWORKS):
            raise ValueError("URL resolves to a private/internal network address.")
    except socket.gaierror:
        raise ValueError("Cannot resolve the URL hostname.")
    except UnicodeError:
        raise ValueError("URL hostname is invalid.")


# ---------------------------------------------------------------------------
# Web search + page fetch
# ---------------------------------------------------------------------------

async def _ddg_search(query: str, max_results: int = 8) -> list[dict]:
    """DuckDuckGo HTML search — no API key required."""
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "b": ""},
                headers={
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "text/html",
                },
            )
        html = resp.text
        results = []

        # Extract result blocks — DDG wraps each result in a <div class="result ...">
        block_pattern = re.compile(
            r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</span>',
            re.DOTALL,
        )
        for m in block_pattern.finditer(html):
            url  = m.group(1).strip()
            title = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            snippet = re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if url and title and not url.startswith("//duckduckgo"):
                results.append({"title": title, "url": url, "snippet": snippet})
            if len(results) >= max_results:
                break

        # Fallback: try simpler pattern if no results
        if not results:
            link_pattern = re.compile(r'href="(https?://[^"]+)"[^>]*class="[^"]*result[^"]*"')
            for m in link_pattern.finditer(html):
                url = m.group(1)
                if "duckduckgo" not in url:
                    results.append({"title": url, "url": url, "snippet": ""})
                if len(results) >= max_results:
                    break

        return results[:max_results]
    except Exception:
        return []


async def _fetch_page_text(url: str, max_chars: int = 4000) -> str:
    """Fetch a webpage and extract human-readable text, focusing on pricing/service content."""
    # fix #1: SSRF guard — reject internal/private addresses before any network call
    try:
        _assert_safe_url(url)
    except ValueError as exc:
        return f"(Skipped: {exc})"

    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            resp = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; LBT-Research/1.0)",
                    "Accept": "text/html",
                },
            )
        html = resp.text

        # Strip script/style/nav
        html = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<nav[^>]*>.*?</nav>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<footer[^>]*>.*?</footer>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<header[^>]*>.*?</header>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<!--.*?-->', ' ', html, flags=re.DOTALL)

        # Strip remaining tags
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'&[a-z]+;', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()

        # Prioritize pricing/service-relevant sentences
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', text) if len(s.strip()) > 20]
        priority_keywords = [
            'price', 'pricing', 'cost', r'\$', 'rate', 'fee', 'package', 'plan',
            'service', 'per hour', 'per month', 'starting', 'quote', 'estimate',
            'starting at', 'from $', 'affordable', 'competitive',
        ]
        priority_pattern = re.compile('|'.join(priority_keywords), re.IGNORECASE)

        high_priority = [s for s in sentences if priority_pattern.search(s)]
        normal = [s for s in sentences if not priority_pattern.search(s)]

        # Return pricing-focused content first
        combined = high_priority[:60] + normal[:40]
        return ' '.join(combined)[:max_chars]
    except Exception:
        # fix #15: never echo the URL or internal error details back to the caller
        return "(Could not fetch competitor page.)"


async def search_competitors(
    industry: str,
    location: str,
    org_name: str,
    extra_query: str = "",
    max_results: int = 8,
) -> list[dict]:
    """
    Search DuckDuckGo for local competitors and return raw result data.
    Does NOT fetch individual pages yet — that happens in the analysis step.
    """
    industry_clean = industry.replace("_", " ")
    location_clean = location.strip() if location else ""
    base_query = f'{industry_clean} business {location_clean} pricing services'.strip()
    if extra_query:
        base_query = f'{extra_query} {industry_clean} {location_clean}'

    results = await _ddg_search(base_query, max_results=max_results)

    # Filter out news, directories, and the user's own business
    filtered = []
    skip_patterns = re.compile(
        r'(yellowpages|yelp\.com/biz|angi\.com|thumbtack|homeadvisor|bark\.com'
        r'|bbb\.org|facebook\.com|linkedin\.com|wikipedia|reddit|news|blog)',
        re.IGNORECASE,
    )
    for r in results:
        url = r.get("url", "")
        if skip_patterns.search(url):
            continue
        if org_name.lower().split()[0] in url.lower():
            continue  # skip own site
        filtered.append(r)

    return filtered[:6]


async def fetch_competitor_details(urls: list[str]) -> list[dict]:
    """Fetch and extract content from a list of competitor URLs concurrently."""
    tasks = [_fetch_page_text(url) for url in urls[:4]]
    texts = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for url, text in zip(urls, texts):
        results.append({
            "url": url,
            "content": text if isinstance(text, str) else "(Error fetching page)",
        })
    return results


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

def _call_llm_sync(system_prompt: str, user_prompt: str, plan: str = "pro", json_mode: bool = True) -> dict | str:
    """Synchronous LLM call — used by strategy endpoints."""
    client, model, supports_json = _get_llm_client_for_plan(plan)

    kwargs: dict = dict(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=2500 if plan in ("pro", "premium") else 1800,
    )
    if json_mode and supports_json:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(**kwargs)
    content = response.choices[0].message.content.strip()

    if content.startswith("```"):
        content = content.split("```")[1]
        content = content.removeprefix("json")

    if json_mode:
        try:
            return json.loads(content)
        except Exception:
            return {"raw": content}
    return content


# ---------------------------------------------------------------------------
# Strategy session — answer strategic questions with business data
# ---------------------------------------------------------------------------

def _build_business_context(metrics: dict, segments: dict, org_name: str, industry: str, benchmark: dict) -> str:
    m = metrics
    sl = segments.get("segments", [])

    seg_lines = []
    for s in sl[:6]:
        seg_lines.append(
            f"  {s['source']:20s}  leads={s['leads']:3d}  conv={s['conversion_rate_pct']:5.1f}%  "
            f"rev=${s['revenue']:>10,.2f}  avg_deal=${s['avg_deal_size']:>8,.2f}"
        )
    segment_section = "\n".join(seg_lines) if seg_lines else "  (no source breakdown available)"

    brief = m.get("analyst_brief", {})
    return f"""
Business: {org_name}
Industry: {industry.replace('_', ' ')}

=== LIVE BUSINESS DATA (last 30 days) ===
Revenue:         ${m['revenue']['total']:,.2f}
Gross Profit:    ${m['revenue']['profit']:,.2f}
Margin:          {m['revenue']['margin_pct']}%
Avg Deal Size:   ${m['revenue']['avg_deal_size']:,.2f}
Leads:           {m['leads']['total']}  (won: {m['leads']['won']}, conv: {m['leads']['conversion_rate_pct']}%)
Missed follow-ups: {m['leads']['missed_follow_ups']}
Customers total: {m['customers']['total']}  (repeat: {m['customers']['repeat_pct']}%)
Expenses:        ${m['expenses']['total']:,.2f}
Net income est.: ${m['revenue']['profit'] - m['expenses']['total']:,.2f}

=== INDUSTRY BENCHMARKS ===
Conversion benchmark: {benchmark.get('conversion', 22)}%   (you: {m['leads']['conversion_rate_pct']}%)
Margin benchmark:     {benchmark.get('margin', 24)}%        (you: {m['revenue']['margin_pct']}%)
Repeat benchmark:     {benchmark.get('repeat', 18)}%        (you: {m['customers']['repeat_pct']}%)

=== CHANNEL BREAKDOWN (conversion + revenue by source) ===
{segment_section}

=== ANALYST ASSESSMENT ===
Health score: {brief.get('health_score', 'N/A')} / 100 ({brief.get('health_label', '')})
Top risk:     {brief.get('top_risks', [{}])[0].get('title', 'N/A') if brief.get('top_risks') else 'N/A'}
Best opportunity: {brief.get('top_opportunities', [{}])[0].get('title', 'N/A') if brief.get('top_opportunities') else 'N/A'}
""".strip()


def run_strategy_session(
    db: Client,
    org_id: str,
    org_name: str,
    industry: str,
    question: str,
    plan: str = "pro",
    conversation_history: list[dict] | None = None,
) -> dict[str, Any]:
    """Ask the AI strategist a question. Returns structured strategic response."""
    metrics  = get_dashboard_metrics(db, org_id, days=30)
    segments = get_segment_analysis(db, org_id, days=30)
    benchmark = INDUSTRY_BENCHMARKS.get(industry, _DEFAULT_BENCHMARK)

    context = _build_business_context(metrics, segments, org_name, industry, benchmark)

    history_text = ""
    if conversation_history:
        lines = []
        for msg in conversation_history[-6:]:
            role = "Owner" if msg["role"] == "user" else "Strategist"
            lines.append(f"{role}: {msg['content']}")
        history_text = "\n\nPrevious conversation:\n" + "\n".join(lines)

    user_prompt = f"""
{context}
{history_text}

Owner's question: {question}

Respond with a JSON object:
{{
  "answer": "<direct strategic response — 2-4 paragraphs, data-specific, actionable>",
  "key_insight": "<the single most important thing they need to hear — 1-2 sentences>",
  "actions": [
    {{
      "action": "<specific executable step>",
      "impact": "<estimated dollar or % impact>",
      "timeframe": "this week | this month | next quarter",
      "effort": "low | medium | high"
    }}
  ],
  "follow_up_questions": ["<question they should ask next>", "<another good question>"]
}}
""".strip()

    result = _call_llm_sync(STRATEGIST_SYSTEM_PROMPT, user_prompt, plan=plan)

    if isinstance(result, str):
        result = {"answer": result, "key_insight": "", "actions": [], "follow_up_questions": []}

    result["metrics_snapshot"] = {
        "revenue":        metrics["revenue"]["total"],
        "margin_pct":     metrics["revenue"]["margin_pct"],
        "conversion_pct": metrics["leads"]["conversion_rate_pct"],
        "health_score":   metrics.get("analyst_brief", {}).get("health_score"),
    }
    return result


# ---------------------------------------------------------------------------
# Competitive analysis
# ---------------------------------------------------------------------------

async def run_competitive_analysis(
    db: Client,
    org_id: str,
    org_name: str,
    industry: str,
    location: str,
    competitor_urls: list[str],
    plan: str = "pro",
) -> dict[str, Any]:
    """
    Fetch competitor pages and run AI comparative analysis.
    Returns pricing position, service gaps, and strategic recommendations.
    """
    metrics  = get_dashboard_metrics(db, org_id, days=30)
    segments = get_segment_analysis(db, org_id, days=30)
    benchmark = INDUSTRY_BENCHMARKS.get(industry, _DEFAULT_BENCHMARK)

    context = _build_business_context(metrics, segments, org_name, industry, benchmark)

    # Fetch competitor content concurrently
    competitor_details = await fetch_competitor_details(competitor_urls)

    comp_section_lines = []
    for i, c in enumerate(competitor_details):
        comp_section_lines.append(
            f"\n--- Competitor {i+1}: {c['url']} ---\n{c['content'][:1500]}"
        )
    competitors_text = "\n".join(comp_section_lines) if comp_section_lines else "(no competitor data fetched)"

    user_prompt = f"""
=== YOUR BUSINESS DATA ===
{context}

=== COMPETITOR PAGES SCRAPED ===
{competitors_text}

Analyze this competitively and respond with JSON:
{{
  "pricing_position": {{
    "assessment": "above_market | at_market | below_market | unclear",
    "explanation": "<what the data shows about their pricing vs. competitors>",
    "competitor_price_signals": ["<price signal found>", ...]
  }},
  "service_gaps": [
    {{
      "gap": "<service/offering competitors have that this business may lack>",
      "opportunity": "<why adding this could matter>",
      "priority": "high | medium | low"
    }}
  ],
  "competitive_advantages": [
    "<what this business does or could do better than competitors>"
  ],
  "strategic_moves": [
    {{
      "move": "<specific strategic action>",
      "reasoning": "<why this wins against the competitive landscape>",
      "estimated_impact": "<dollar or % range>",
      "timeframe": "this week | this month | next quarter"
    }}
  ],
  "market_summary": "<2-3 sentence overview of where this business sits in the market and what to do about it>",
  "biggest_threat": "<the one competitor move or market condition most likely to hurt them>",
  "biggest_opportunity": "<the one market gap they could capture fastest>"
}}
""".strip()

    result = _call_llm_sync(COMPETITOR_ANALYSIS_SYSTEM_PROMPT, user_prompt, plan=plan)

    if isinstance(result, str):
        result = {"market_summary": result}

    result["competitor_urls"]    = competitor_urls
    result["competitors_fetched"] = len(competitor_details)
    result["org_metrics_snapshot"] = {
        "revenue":        metrics["revenue"]["total"],
        "margin_pct":     metrics["revenue"]["margin_pct"],
        "avg_deal_size":  metrics["revenue"]["avg_deal_size"],
        "conversion_pct": metrics["leads"]["conversion_rate_pct"],
    }
    return result


# ---------------------------------------------------------------------------
# Proactive briefing — surface insights without being asked
# ---------------------------------------------------------------------------

def get_proactive_briefing(
    db: Client,
    org_id: str,
    org_name: str,
    industry: str,
    plan: str = "basic",
) -> dict[str, Any]:
    """
    Generate a short proactive briefing the owner should read before strategizing.
    Surfaces the most urgent items from their current data.
    """
    metrics  = get_dashboard_metrics(db, org_id, days=30)
    segments = get_segment_analysis(db, org_id, days=30)
    brief    = metrics.get("analyst_brief", {})

    # Build 3-5 proactive signals
    signals = []

    # Follow-up urgency
    missed = metrics["leads"]["missed_follow_ups"]
    if missed > 0:
        signals.append({
            "type": "urgent",
            "icon": "⚠",
            "title": f"{missed} follow-up{'s' if missed > 1 else ''} overdue right now",
            "detail": "These leads already expressed interest. Every day without contact reduces close probability by ~15-20%.",
            "action": "Clear the follow-up queue today.",
        })

    # Best channel signal
    best = segments.get("best_by_conversion")
    if best and best.get("leads", 0) >= 2:
        signals.append({
            "type": "opportunity",
            "icon": "✦",
            "title": f"'{best['source'].replace('_', ' ').title()}' is converting at {best['conversion_rate_pct']:.1f}%",
            "detail": f"That's likely {best['conversion_rate_pct'] - metrics['leads']['conversion_rate_pct']:.1f} points above your blended average. Every additional lead here is worth ~${best['avg_deal_size']:,.0f}.",
            "action": "Route more volume through this channel before optimizing others.",
        })

    # Revenue trend signal
    variance = brief.get("variance_breakdown", {})
    if variance.get("total_revenue_change", 0) < -500:
        signals.append({
            "type": "warning",
            "icon": "↓",
            "title": f"Revenue is down ${abs(variance['total_revenue_change']):,.0f} vs. last period",
            "detail": variance.get("explanation", "Review lead volume and conversion to find the cause."),
            "action": "Open the variance breakdown to diagnose which factor drove it.",
        })
    elif variance.get("total_revenue_change", 0) > 500:
        signals.append({
            "type": "positive",
            "icon": "↑",
            "title": f"Revenue is up ${variance['total_revenue_change']:,.0f} vs. last period",
            "detail": variance.get("explanation", "Identify what drove the growth and double down."),
            "action": "Identify the driver and protect it.",
        })

    # Margin risk
    if metrics["revenue"]["margin_pct"] < 20 and metrics["revenue"]["total"] > 0:
        top_exp = max(metrics["expenses"]["by_category"].items(), key=lambda x: x[1]) if metrics["expenses"]["by_category"] else None
        detail = f"Gross margin is {metrics['revenue']['margin_pct']:.1f}% — thin for sustainable growth."
        if top_exp:
            detail += f" Biggest cost bucket: {top_exp[0].replace('_', ' ')} at ${top_exp[1]:,.0f}."
        signals.append({
            "type": "warning",
            "icon": "◐",
            "title": "Margin is below a healthy operating range",
            "detail": detail,
            "action": "Review your largest expense category before adding more volume.",
        })

    # Retention opportunity
    if metrics["customers"]["total"] >= 5 and metrics["customers"]["repeat_pct"] < 25:
        signals.append({
            "type": "opportunity",
            "icon": "◉",
            "title": f"Only {metrics['customers']['repeat_pct']:.1f}% of customers are coming back",
            "detail": "Existing customers cost nothing to reactivate vs. acquiring new ones. A simple outreach campaign can move this fast.",
            "action": "Run a reactivation offer to the customer list this week.",
        })

    return {
        "signals":            signals[:4],
        "health_score":       brief.get("health_score"),
        "health_label":       brief.get("health_label"),
        "executive_summary":  brief.get("executive_summary"),
        "suggested_questions": [
            "What should my pricing be to maximize margin without losing deals?",
            "Which service should I push hardest this month?",
            "What's the fastest way to add $5,000/month in revenue?",
            "How do I compete with larger competitors who can undercut me on price?",
            "Am I growing or am I just busy?",
        ],
    }

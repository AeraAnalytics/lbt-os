"""
Metrics aggregation service.

All metrics are computed from raw Supabase data. This service is the
single source of truth for dashboard numbers, analyst brief, segment
analysis, and revenue forecasting.
"""
from datetime import date, timedelta
from typing import Any

from supabase import Client

INDUSTRY_BENCHMARKS: dict[str, dict[str, float | str]] = {
    "hvac":            {"conversion": 28.0, "margin": 32.0, "repeat": 24.0, "note": "HVAC operators usually win by tightening follow-up speed and seasonal repeat work."},
    "plumbing":        {"conversion": 30.0, "margin": 34.0, "repeat": 26.0, "note": "Strong plumbing shops usually defend margin through pricing discipline and repeat service calls."},
    "electrician":     {"conversion": 27.0, "margin": 33.0, "repeat": 22.0, "note": "Electricians usually improve profit by separating emergency jobs from scheduled work and protecting labor margin."},
    "landscaping":     {"conversion": 25.0, "margin": 29.0, "repeat": 44.0, "note": "Landscaping businesses usually win on recurring contracts, route efficiency, and seasonal upsells."},
    "cleaning_service":{"conversion": 31.0, "margin": 27.0, "repeat": 52.0, "note": "Cleaning businesses usually perform best when repeat schedules and crew utilization are tightly managed."},
    "gig_worker":      {"conversion": 36.0, "margin": 41.0, "repeat": 34.0, "note": "Solo operators usually grow fastest by protecting take-home margin and doubling down on the highest-paying service mix."},
    "salon_spa":       {"conversion": 34.0, "margin": 30.0, "repeat": 58.0, "note": "Salon and spa operators usually rely on rebooking rate, service mix, and recurring client value."},
    "restaurant":      {"conversion":  0.0, "margin": 14.0, "repeat": 38.0, "note": "Restaurants typically rely on retention, table turns, and tight cost control more than lead conversion."},
    "gym":             {"conversion": 24.0, "margin": 26.0, "repeat": 55.0, "note": "Fitness businesses win on retention, recurring revenue, and member reactivation."},
    "real_estate":     {"conversion": 18.0, "margin": 42.0, "repeat": 12.0, "note": "Real estate teams usually care most about lead quality, close rate, and referral momentum."},
}

_DEFAULT_BENCHMARK = {
    "conversion": 22.0, "margin": 24.0, "repeat": 18.0,
    "note": "Healthy local operators usually balance lead volume, margin discipline, and repeat business.",
}


def _sum(rows: list[dict], key: str) -> float:
    return round(sum(float(r.get(key) or 0) for r in rows), 2)


def _pct_change(current: float, previous: float) -> float | None:
    if previous == 0:
        return None if current == 0 else 100.0
    return round(((current - previous) / previous) * 100, 1)


def _label_change(current: float, previous: float, unit: str = "%") -> str:
    delta = round(current - previous, 1)
    if unit == "%":
        direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
        return f"{direction} {abs(delta):.1f} pts" if delta else "flat"
    direction = "up" if delta > 0 else "down" if delta < 0 else "flat"
    return f"{direction} {abs(delta):,.0f}{unit}" if delta else "flat"


def _health_label(score: int) -> str:
    if score >= 80:
        return "Strong"
    if score >= 60:
        return "Stable"
    if score >= 40:
        return "Needs Attention"
    return "At Risk"


def _window_metrics(db: Client, org_id: str, start_day: date, end_day: date) -> dict[str, Any]:
    start_iso = start_day.isoformat()
    end_iso   = end_day.isoformat()

    sales = (db.table("sales").select("amount, cost, profit, source, payment_status, sold_at")
             .eq("org_id", org_id).gte("sold_at", start_iso).lt("sold_at", end_iso).execute()).data or []
    paid_sales = [s for s in sales if s.get("payment_status") == "paid"]

    leads = (db.table("leads").select("status, source, created_at, follow_up_at, contacted_at")
             .eq("org_id", org_id).gte("created_at", start_iso).lt("created_at", end_iso).execute()).data or []

    expenses = (db.table("expenses").select("amount, category, expense_date")
                .eq("org_id", org_id).gte("expense_date", start_iso).lt("expense_date", end_iso).execute()).data or []

    customers = (db.table("customers").select("id, total_orders, created_at")
                 .eq("org_id", org_id).gte("created_at", start_iso).lt("created_at", end_iso).execute()).data or []

    revenue_by_source: dict[str, float] = {}
    for sale in paid_sales:
        src = sale.get("source") or "unknown"
        revenue_by_source[src] = revenue_by_source.get(src, 0) + float(sale.get("amount") or 0)

    leads_by_source: dict[str, int] = {}
    won_by_source: dict[str, int] = {}
    for lead in leads:
        src = lead.get("source") or "unknown"
        leads_by_source[src] = leads_by_source.get(src, 0) + 1
        if lead.get("status") == "won":
            won_by_source[src] = won_by_source.get(src, 0) + 1

    expenses_by_category: dict[str, float] = {}
    for expense in expenses:
        cat = expense.get("category") or "uncategorized"
        expenses_by_category[cat] = expenses_by_category.get(cat, 0) + float(expense.get("amount") or 0)

    won  = [lead for lead in leads if lead.get("status") == "won"]
    lost = [lead for lead in leads if lead.get("status") == "lost"]
    missed_follow_ups = [
        lead for lead in leads
        if lead.get("follow_up_at") and not lead.get("contacted_at") and lead.get("status") not in ("won", "lost")
    ]

    total_revenue  = _sum(paid_sales, "amount")
    total_cost     = _sum(paid_sales, "cost")
    total_profit   = _sum(paid_sales, "profit")
    total_expenses = _sum(expenses, "amount")
    conversion_rate = (len(won) / len(leads) * 100) if leads else 0
    won_count = len(won)
    avg_deal_size = (total_revenue / won_count) if won_count > 0 else 0

    return {
        "revenue_total":     total_revenue,
        "cost_total":        total_cost,
        "profit_total":      total_profit,
        "margin_pct":        round((total_profit / total_revenue * 100), 1) if total_revenue else 0,
        "expense_total":     total_expenses,
        "lead_total":        len(leads),
        "won_total":         won_count,
        "lost_total":        len(lost),
        "conversion_rate_pct": round(conversion_rate, 1),
        "avg_deal_size":     round(avg_deal_size, 2),
        "missed_follow_ups": len(missed_follow_ups),
        "new_customers":     len(customers),
        "revenue_by_source": {k: round(v, 2) for k, v in revenue_by_source.items()},
        "leads_by_source":   leads_by_source,
        "won_by_source":     won_by_source,
        "expenses_by_category": {k: round(v, 2) for k, v in expenses_by_category.items()},
    }


# ---------------------------------------------------------------------------
# Dashboard metrics
# ---------------------------------------------------------------------------

def get_dashboard_metrics(db: Client, org_id: str, days: int = 30) -> dict[str, Any]:
    """Return all dashboard metrics for an org over the last N days."""
    since = (date.today() - timedelta(days=days)).isoformat()

    sales = (db.table("sales").select("amount, cost, profit, source, payment_status, sold_at, customer_id")
             .eq("org_id", org_id).gte("sold_at", since).execute()).data or []
    paid_sales = [s for s in sales if s["payment_status"] == "paid"]

    total_revenue = _sum(paid_sales, "amount")
    total_cost    = _sum(paid_sales, "cost")
    total_profit  = _sum(paid_sales, "profit")
    profit_margin = (total_profit / total_revenue * 100) if total_revenue else 0

    revenue_by_source: dict[str, float] = {}
    for s in paid_sales:
        src = s.get("source") or "unknown"
        revenue_by_source[src] = revenue_by_source.get(src, 0) + float(s.get("amount") or 0)

    leads = (db.table("leads").select("status, source, created_at, follow_up_at, contacted_at")
             .eq("org_id", org_id).gte("created_at", since).execute()).data or []
    total_leads  = len(leads)
    won_leads    = [lead for lead in leads if lead["status"] == "won"]
    lost_leads   = [lead for lead in leads if lead["status"] == "lost"]
    conversion_rate = (len(won_leads) / total_leads * 100) if total_leads else 0
    missed_follow_ups = [
        lead for lead in leads
        if lead.get("follow_up_at") and not lead.get("contacted_at") and lead["status"] not in ("won", "lost")
    ]
    avg_deal_size = (total_revenue / len(won_leads)) if won_leads else 0

    leads_by_source: dict[str, int] = {}
    won_by_source: dict[str, int] = {}
    for lead in leads:
        src = lead.get("source") or "unknown"
        leads_by_source[src] = leads_by_source.get(src, 0) + 1
        if lead["status"] == "won":
            won_by_source[src] = won_by_source.get(src, 0) + 1

    customers = (db.table("customers").select("id, total_orders, last_purchase_at, created_at")
                 .eq("org_id", org_id).execute()).data or []
    total_customers  = len(customers)
    repeat_customers = [c for c in customers if c["total_orders"] > 1]
    repeat_pct = (len(repeat_customers) / total_customers * 100) if total_customers else 0

    expenses = (db.table("expenses").select("amount, category, expense_date")
                .eq("org_id", org_id).gte("expense_date", since).execute()).data or []
    total_expenses = _sum(expenses, "amount")
    expenses_by_category: dict[str, float] = {}
    for e in expenses:
        cat = e["category"]
        expenses_by_category[cat] = expenses_by_category.get(cat, 0) + float(e.get("amount") or 0)

    metrics = {
        "period_days": days,
        "revenue": {
            "total":         round(total_revenue, 2),
            "cost":          round(total_cost, 2),
            "profit":        round(total_profit, 2),
            "margin_pct":    round(profit_margin, 1),
            "avg_deal_size": round(avg_deal_size, 2),
            "by_source":     {k: round(v, 2) for k, v in revenue_by_source.items()},
        },
        "leads": {
            "total":               total_leads,
            "won":                 len(won_leads),
            "lost":                len(lost_leads),
            "conversion_rate_pct": round(conversion_rate, 1),
            "missed_follow_ups":   len(missed_follow_ups),
            "by_source":           leads_by_source,
            "won_by_source":       won_by_source,
        },
        "customers": {
            "total":      total_customers,
            "repeat":     len(repeat_customers),
            "repeat_pct": round(repeat_pct, 1),
        },
        "expenses": {
            "total":       round(total_expenses, 2),
            "by_category": {k: round(v, 2) for k, v in expenses_by_category.items()},
        },
    }

    metrics["analyst_brief"] = get_analyst_brief(db, org_id, metrics=metrics, days=days)
    return metrics


# ---------------------------------------------------------------------------
# Analyst brief
# ---------------------------------------------------------------------------

def get_analyst_brief(
    db: Client,
    org_id: str,
    metrics: dict[str, Any] | None = None,
    days: int = 30,
) -> dict[str, Any]:
    metrics = metrics or get_dashboard_metrics(db, org_id, days=days)
    org = (db.table("organizations").select("name, industry, plan")
           .eq("id", org_id).single().execute())
    org_data  = org.data or {}
    industry  = org_data.get("industry") or "general"
    benchmark = INDUSTRY_BENCHMARKS.get(industry, _DEFAULT_BENCHMARK)

    today          = date.today()
    current_start  = today - timedelta(days=days)
    previous_start = current_start - timedelta(days=days)

    current_window  = _window_metrics(db, org_id, current_start, today + timedelta(days=1))
    previous_window = _window_metrics(db, org_id, previous_start, current_start)

    revenue_total  = metrics["revenue"]["total"]
    margin_pct     = metrics["revenue"]["margin_pct"]
    lead_total     = metrics["leads"]["total"]
    conversion_pct = metrics["leads"]["conversion_rate_pct"]
    missed_follow_ups = metrics["leads"]["missed_follow_ups"]
    repeat_pct     = metrics["customers"]["repeat_pct"]
    expense_total  = metrics["expenses"]["total"]

    bm_conversion  = float(benchmark["conversion"])
    bm_margin      = float(benchmark["margin"])
    bm_repeat      = float(benchmark["repeat"])

    # ----- Calibrated health score -----
    # Scale confidence by data volume — thin data means smaller deductions
    data_confidence = min(1.0, lead_total / 10)  # full confidence at 10+ leads

    score = 100

    if revenue_total == 0:
        score -= 18
    if lead_total == 0:
        score -= 18
    elif bm_conversion > 0:
        if conversion_pct < bm_conversion * 0.6:
            score -= int(16 * max(0.5, data_confidence))
        elif conversion_pct < bm_conversion:
            score -= int(8 * max(0.5, data_confidence))

    if bm_margin > 0:
        if margin_pct < bm_margin * 0.6:
            score -= 16
        elif margin_pct < bm_margin:
            score -= 8

    if missed_follow_ups > 0:
        score -= min(18, missed_follow_ups * 3)

    if repeat_pct < bm_repeat and metrics["customers"]["total"] >= 5:
        score -= 10

    if expense_total > revenue_total > 0:
        score -= 10

    # Bonus points for overperforming benchmarks
    if conversion_pct >= bm_conversion * 1.1 and lead_total >= 5:
        score = min(98, score + 5)
    if margin_pct >= bm_margin * 1.1 and revenue_total > 0:
        score = min(98, score + 5)

    score = max(18, min(98, score))

    # ----- Variance decomposition -----
    # When revenue changes, break down: leads vs conversion vs deal size
    prev_leads     = previous_window["lead_total"]
    prev_conv      = previous_window["conversion_rate_pct"] / 100
    prev_deal      = previous_window["avg_deal_size"]
    curr_leads     = current_window["lead_total"]
    curr_conv      = current_window["conversion_rate_pct"] / 100
    curr_deal      = current_window["avg_deal_size"]

    variance_breakdown: dict[str, Any] = {}
    if prev_leads > 0 and prev_conv > 0 and prev_deal > 0:
        # Shapley decomposition of revenue change
        lead_effect  = (curr_leads - prev_leads) * prev_conv * prev_deal
        conv_effect  = prev_leads * (curr_conv - prev_conv) * prev_deal
        deal_effect  = prev_leads * prev_conv * (curr_deal - prev_deal)
        total_effect = lead_effect + conv_effect + deal_effect

        def _fmt_effect(v: float) -> str:
            sign = "+" if v >= 0 else "-"
            return f"{sign}${abs(v):,.0f}"

        variance_breakdown = {
            "total_revenue_change": round(current_window["revenue_total"] - previous_window["revenue_total"], 2),
            "lead_volume_effect":   round(lead_effect, 2),
            "conversion_effect":    round(conv_effect, 2),
            "deal_size_effect":     round(deal_effect, 2),
            "explanation": (
                f"Revenue shifted by ${abs(total_effect):,.0f}. "
                f"Lead volume contributed {_fmt_effect(lead_effect)}, "
                f"conversion rate contributed {_fmt_effect(conv_effect)}, "
                f"and average deal size contributed {_fmt_effect(deal_effect)}."
            ) if total_effect != 0 else "Revenue was flat across both periods.",
        }

    # ----- Risks -----
    top_risks: list[dict[str, Any]] = []
    if missed_follow_ups > 0:
        top_risks.append({
            "title": "Follow-up queue is leaking revenue",
            "detail": f"{missed_follow_ups} lead{'s' if missed_follow_ups != 1 else ''} already have follow-up dates but still have no contact logged. That usually means warm demand is stalling before a quote or close.",
            "severity": "high" if missed_follow_ups >= 4 else "medium",
        })
    if lead_total >= 5 and bm_conversion > 0 and conversion_pct < bm_conversion:
        # Find the weakest source for more precise diagnosis
        leads_by_src = metrics["leads"]["by_source"]
        won_by_src   = metrics["leads"].get("won_by_source", {})
        worst_source = None
        worst_conv   = 100.0
        for src, cnt in leads_by_src.items():
            if cnt >= 3:
                src_conv = (won_by_src.get(src, 0) / cnt * 100)
                if src_conv < worst_conv:
                    worst_conv, worst_source = src_conv, src
        detail = (
            f"Conversion is {conversion_pct:.1f}% against a benchmark near {bm_conversion:.0f}% for this industry."
        )
        if worst_source:
            detail += f" The weakest channel is '{worst_source.replace('_', ' ')}' at {worst_conv:.1f}% — start the diagnosis there."
        top_risks.append({"title": "Lead conversion is below a healthy operating range", "detail": detail,
                          "severity": "high" if conversion_pct < bm_conversion * 0.6 else "medium"})
    if revenue_total > 0 and bm_margin > 0 and margin_pct < bm_margin:
        # Find top expense category contributing to margin pressure
        top_exp_cat = None
        if metrics["expenses"]["by_category"]:
            top_exp_cat = max(metrics["expenses"]["by_category"].items(), key=lambda x: x[1])
        detail = f"Gross margin is {margin_pct:.1f}% while the business should be defending closer to {bm_margin:.0f}%."
        if top_exp_cat:
            detail += f" The biggest cost driver is '{top_exp_cat[0].replace('_', ' ')}' at ${top_exp_cat[1]:,.0f}."
        top_risks.append({"title": "Margin is too thin for comfortable growth", "detail": detail,
                          "severity": "high" if margin_pct < bm_margin * 0.6 else "medium"})
    if metrics["customers"]["total"] >= 5 and repeat_pct < bm_repeat:
        top_risks.append({"title": "Retention is not compounding yet",
                          "detail": f"Repeat customer rate is {repeat_pct:.1f}%, below the benchmark near {bm_repeat:.0f}%. The business is not capturing follow-on value from customers it already paid to acquire.",
                          "severity": "medium"})
    if revenue_total == 0 and lead_total == 0:
        top_risks.append({"title": "The pipeline is too quiet to audit deeply",
                          "detail": "There is not enough lead or revenue activity in the current window to spot true operating patterns.",
                          "severity": "medium"})

    # ----- Opportunities -----
    best_revenue_source = max(metrics["revenue"]["by_source"].items(), key=lambda x: x[1]) if metrics["revenue"]["by_source"] else None
    top_expense_cat     = max(metrics["expenses"]["by_category"].items(), key=lambda x: x[1]) if metrics["expenses"]["by_category"] else None

    # Find best-converting source (min 2 leads)
    best_converting_source = None
    leads_by_src = metrics["leads"]["by_source"]
    won_by_src   = metrics["leads"].get("won_by_source", {})
    best_conv_rate = 0.0
    for src, cnt in leads_by_src.items():
        if cnt >= 2:
            src_conv = won_by_src.get(src, 0) / cnt * 100
            if src_conv > best_conv_rate:
                best_conv_rate, best_converting_source = src_conv, src

    top_opportunities: list[dict[str, Any]] = []
    if best_converting_source and best_converting_source != (best_revenue_source[0] if best_revenue_source else None):
        top_opportunities.append({
            "title": f"'{best_converting_source.replace('_', ' ').title()}' converts best — it deserves more volume",
            "detail": f"This channel is closing at {best_conv_rate:.1f}%, likely above your blended average. Directing more leads here should lift overall conversion without changing the sales process.",
            "impact_hint": "High-leverage",
        })
    if best_revenue_source and best_revenue_source[0] != "unknown":
        top_opportunities.append({
            "title": f"{best_revenue_source[0].replace('_', ' ').title()} is already your strongest revenue source",
            "detail": f"It generated ${best_revenue_source[1]:,.0f} in tracked revenue this period. Doubling down on a channel that already converts into cash is usually safer than testing cold channels.",
            "impact_hint": "High-leverage",
        })
    if missed_follow_ups > 0:
        top_opportunities.append({
            "title": "Overdue follow-ups are the fastest recoverable win",
            "detail": "These leads already raised their hands. Clearing the queue is usually the quickest path to more quotes, more closed work, and better conversion.",
            "impact_hint": "This week",
        })
    if metrics["customers"]["total"] >= 3 and repeat_pct < 35:
        top_opportunities.append({
            "title": "Existing customers are the cheapest growth channel",
            "detail": "A simple reactivation or maintenance offer can often lift repeat revenue faster than acquiring brand-new leads from scratch.",
            "impact_hint": "Retention",
        })
    if top_expense_cat:
        top_opportunities.append({
            "title": f"{top_expense_cat[0].replace('_', ' ').title()} is the first cost bucket to inspect",
            "detail": f"It represents ${top_expense_cat[1]:,.0f} in spend this period. Tightening the largest cost center tends to improve margin faster than broad cost-cutting.",
            "impact_hint": "Margin",
        })
    if margin_pct >= bm_margin:
        top_opportunities.append({
            "title": "Unit economics are strong enough to press growth",
            "detail": "Margin is already holding at a healthy level, which means new demand should add profit rather than just workload.",
            "impact_hint": "Scale",
        })

    # ----- What changed -----
    what_changed = [
        {
            "label": "Revenue",
            "value": _pct_change(current_window["revenue_total"], previous_window["revenue_total"]),
            "direction": "up" if current_window["revenue_total"] > previous_window["revenue_total"] else "down" if current_window["revenue_total"] < previous_window["revenue_total"] else "flat",
            "detail": f"${current_window['revenue_total']:,.0f} this period vs ${previous_window['revenue_total']:,.0f} previously",
        },
        {
            "label": "Lead volume",
            "value": _pct_change(current_window["lead_total"], previous_window["lead_total"]),
            "direction": "up" if current_window["lead_total"] > previous_window["lead_total"] else "down" if current_window["lead_total"] < previous_window["lead_total"] else "flat",
            "detail": f"{current_window['lead_total']} leads this period vs {previous_window['lead_total']} previously",
        },
        {
            "label": "Profit margin",
            "value": round(current_window["margin_pct"] - previous_window["margin_pct"], 1),
            "direction": "up" if current_window["margin_pct"] > previous_window["margin_pct"] else "down" if current_window["margin_pct"] < previous_window["margin_pct"] else "flat",
            "detail": f"{_label_change(current_window['margin_pct'], previous_window['margin_pct'])} from the prior period",
        },
        {
            "label": "Avg deal size",
            "value": _pct_change(current_window["avg_deal_size"], previous_window["avg_deal_size"]),
            "direction": "up" if current_window["avg_deal_size"] > previous_window["avg_deal_size"] else "down" if current_window["avg_deal_size"] < previous_window["avg_deal_size"] else "flat",
            "detail": f"${current_window['avg_deal_size']:,.0f} avg vs ${previous_window['avg_deal_size']:,.0f} previously",
        },
    ]

    # ----- Focus actions -----
    focus_this_week: list[dict[str, Any]] = []
    if missed_follow_ups > 0:
        focus_this_week.append({
            "priority": 1,
            "title": f"Clear the {missed_follow_ups}-lead follow-up queue",
            "detail": "Start with the oldest overdue leads and push each one to contacted, won, or lost so the pipeline becomes truthful again.",
        })
    if top_expense_cat:
        focus_this_week.append({
            "priority": len(focus_this_week) + 1,
            "title": f"Review {top_expense_cat[0].replace('_', ' ')} spend",
            "detail": "Check whether this cost bucket is supporting closed revenue or simply dragging margin down.",
        })
    if best_converting_source:
        focus_this_week.append({
            "priority": len(focus_this_week) + 1,
            "title": f"Route more demand through '{best_converting_source.replace('_', ' ')}'",
            "detail": f"This channel closes at {best_conv_rate:.1f}%. Prioritizing it this week should move conversion numbers faster than any other single action.",
        })
    elif best_revenue_source and best_revenue_source[0] != "unknown":
        focus_this_week.append({
            "priority": len(focus_this_week) + 1,
            "title": f"Feed more demand into {best_revenue_source[0].replace('_', ' ')}",
            "detail": "The best current source has already shown it can turn into paid work. Put this week's effort where the business is already seeing traction.",
        })
    if not focus_this_week:
        focus_this_week.append({
            "priority": 1,
            "title": "Start logging leads, sales, and expenses consistently",
            "detail": "Once operating data is flowing, the dashboard can identify where growth is real and where margin is leaking.",
        })

    # ----- Executive summary -----
    revenue_change = _pct_change(current_window["revenue_total"], previous_window["revenue_total"])
    revenue_change_text = "holding flat"
    if revenue_change is not None:
        revenue_change_text = ("up sharply" if revenue_change >= 15 else "up modestly" if revenue_change > 0
                               else "down" if revenue_change < 0 else "holding flat")

    if revenue_total == 0 and lead_total == 0:
        executive_summary = "The business does not have enough current activity for a deep operating read yet. The immediate priority is creating consistent pipeline and transaction visibility so the dashboard can separate demand problems from execution problems."
    else:
        executive_summary = (
            f"Revenue is {revenue_change_text}, with gross margin at {margin_pct:.1f}% and lead conversion at {conversion_pct:.1f}%. "
            f"The biggest operating pressure right now is {'follow-up discipline' if missed_follow_ups > 0 else 'turning activity into cleaner profit'}, "
            f"while the clearest upside is {'capturing overdue demand and doubling down on the best current source' if best_revenue_source else 'tightening execution around the existing pipeline'}."
        )
        if variance_breakdown.get("explanation"):
            executive_summary += f" {variance_breakdown['explanation']}"

    return {
        "health_score":      score,
        "health_label":      _health_label(score),
        "executive_summary": executive_summary,
        "top_risks":         top_risks[:3],
        "top_opportunities": top_opportunities[:3],
        "what_changed":      what_changed,
        "focus_this_week":   focus_this_week[:3],
        "benchmark_note":    str(benchmark["note"]),
        "variance_breakdown": variance_breakdown,
        "data_confidence":   round(data_confidence, 2),
        "pro_positioning": {
            "headline": "Pro gives you an analyst team on demand",
            "detail":   "Free gives you the operating brief. Pro adds deeper AI drill-down, forecasting, recurring board reports, and continuous monitoring.",
            "features": [
                "Root-cause analysis across revenue, margin, and retention",
                "30/60/90 day forecasting and scenario modeling",
                "Source-level conversion analysis: which channels actually pay off",
            ],
        },
    }


# ---------------------------------------------------------------------------
# Segment analysis (conversion + revenue by lead source)
# ---------------------------------------------------------------------------

def get_segment_analysis(db: Client, org_id: str, days: int = 30) -> dict[str, Any]:
    """
    Cross-tabulate lead source against conversion rate, revenue, volume,
    and average deal size. Surfaces which acquisition channels actually pay off.
    """
    since = (date.today() - timedelta(days=days)).isoformat()

    leads = (db.table("leads").select("status, source, created_at")
             .eq("org_id", org_id).gte("created_at", since).execute()).data or []
    sales = (db.table("sales").select("amount, source, payment_status, sold_at")
             .eq("org_id", org_id).gte("sold_at", since).execute()).data or []
    paid_sales = [s for s in sales if s.get("payment_status") == "paid"]

    # Aggregate per source
    source_data: dict[str, dict] = {}

    def _src(row: dict) -> str:
        return (row.get("source") or "unknown").strip() or "unknown"

    for lead in leads:
        src = _src(lead)
        bucket = source_data.setdefault(src, {"leads": 0, "won": 0, "lost": 0, "revenue": 0.0, "sales_count": 0})
        bucket["leads"] += 1
        if lead.get("status") == "won":
            bucket["won"] += 1
        elif lead.get("status") == "lost":
            bucket["lost"] += 1

    for sale in paid_sales:
        src = _src(sale)
        bucket = source_data.setdefault(src, {"leads": 0, "won": 0, "lost": 0, "revenue": 0.0, "sales_count": 0})
        bucket["revenue"] += float(sale.get("amount") or 0)
        bucket["sales_count"] += 1

    segments = []
    for src, d in source_data.items():
        conv    = (d["won"] / d["leads"] * 100) if d["leads"] > 0 else 0
        avg_deal = (d["revenue"] / d["sales_count"]) if d["sales_count"] > 0 else 0
        # Revenue efficiency = revenue per lead (ROI proxy)
        rev_per_lead = (d["revenue"] / d["leads"]) if d["leads"] > 0 else 0
        segments.append({
            "source":               src,
            "leads":                d["leads"],
            "won":                  d["won"],
            "lost":                 d["lost"],
            "in_progress":          d["leads"] - d["won"] - d["lost"],
            "conversion_rate_pct":  round(conv, 1),
            "revenue":              round(d["revenue"], 2),
            "avg_deal_size":        round(avg_deal, 2),
            "revenue_per_lead":     round(rev_per_lead, 2),
        })

    segments.sort(key=lambda x: x["revenue"], reverse=True)
    for i, seg in enumerate(segments):
        seg["rank_by_revenue"] = i + 1

    conv_ranked = sorted(
        [s for s in segments if s["leads"] >= 2],
        key=lambda x: x["conversion_rate_pct"], reverse=True,
    )
    for i, seg in enumerate(conv_ranked):
        seg["rank_by_conversion"] = i + 1

    total_revenue = sum(s["revenue"] for s in segments)
    for seg in segments:
        seg["revenue_share_pct"] = round((seg["revenue"] / total_revenue * 100) if total_revenue else 0, 1)

    best_by_conv    = conv_ranked[0] if conv_ranked else None
    best_by_revenue = segments[0] if segments else None
    worst_by_conv   = conv_ranked[-1] if len(conv_ranked) > 1 else None

    insight = None
    if best_by_conv and best_by_revenue and best_by_conv["source"] != best_by_revenue["source"]:
        insight = (
            f"'{best_by_conv['source'].replace('_', ' ').title()}' converts best at {best_by_conv['conversion_rate_pct']:.1f}% "
            f"but '{best_by_revenue['source'].replace('_', ' ').title()}' brings in more raw revenue. "
            f"These two channels have different strengths — optimizing for conversion vs. volume requires a deliberate choice."
        )
    elif best_by_conv:
        insight = f"'{best_by_conv['source'].replace('_', ' ').title()}' is your highest-converting channel at {best_by_conv['conversion_rate_pct']:.1f}%."

    return {
        "period_days":      days,
        "segments":         segments,
        "best_by_conversion": best_by_conv,
        "best_by_revenue":    best_by_revenue,
        "worst_by_conversion": worst_by_conv,
        "channel_insight":    insight,
        "total_revenue":      round(total_revenue, 2),
    }


# ---------------------------------------------------------------------------
# Revenue forecast (linear trend + confidence bands)
# ---------------------------------------------------------------------------

def get_revenue_forecast(db: Client, org_id: str, lookback_weeks: int = 16) -> dict[str, Any]:
    """
    Extrapolate weekly revenue forward 12 weeks using linear regression on
    the last N weeks of paid sales data. Returns per-week predictions with
    confidence intervals and 30/60/90-day rollup summaries.
    """
    since = (date.today() - timedelta(weeks=lookback_weeks)).isoformat()
    sales = (db.table("sales").select("amount, sold_at")
             .eq("org_id", org_id).eq("payment_status", "paid")
             .gte("sold_at", since).execute()).data or []

    weekly: dict[str, float] = {}
    for s in sales:
        if not s.get("sold_at"):
            continue
        d = date.fromisoformat(s["sold_at"][:10])
        # Snap to ISO week start (Monday)
        week_start = (d - timedelta(days=d.weekday())).isoformat()
        weekly[week_start] = weekly.get(week_start, 0) + float(s.get("amount") or 0)

    sorted_weeks = sorted(weekly.items())

    if len(sorted_weeks) < 3:
        return {"status": "insufficient_data", "historical": [], "forecast_weekly": [], "summary": {}}

    n      = len(sorted_weeks)
    x_vals = list(range(n))
    y_vals = [v for _, v in sorted_weeks]

    x_mean = sum(x_vals) / n
    y_mean = sum(y_vals) / n
    ss_xy  = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    ss_xx  = sum((x - x_mean) ** 2 for x in x_vals)
    slope  = ss_xy / ss_xx if ss_xx != 0 else 0
    intercept = y_mean - slope * x_mean

    residuals     = [y - (slope * x + intercept) for x, y in zip(x_vals, y_vals)]
    std_residual  = (sum(r ** 2 for r in residuals) / max(n - 2, 1)) ** 0.5

    trend_direction = "growing" if slope > 0.5 else "declining" if slope < -0.5 else "flat"
    weekly_change_pct = (slope / y_mean * 100) if y_mean > 0 else 0

    last_week_date = date.fromisoformat(sorted_weeks[-1][0])
    forecast = []
    for i in range(1, 13):
        x         = n - 1 + i
        predicted = max(0.0, slope * x + intercept)
        week_date = (last_week_date + timedelta(weeks=i)).isoformat()
        forecast.append({
            "week":      week_date,
            "predicted": round(predicted, 2),
            "low":       round(max(0.0, predicted - std_residual * 1.5), 2),
            "high":      round(predicted + std_residual * 1.5, 2),
        })

    next_30  = round(sum(f["predicted"] for f in forecast[:4]), 2)
    next_60  = round(sum(f["predicted"] for f in forecast[:8]), 2)
    next_90  = round(sum(f["predicted"] for f in forecast[:12]), 2)

    # Narrative
    if trend_direction == "growing":
        narrative = f"Revenue has been trending up ~${slope:,.0f}/week. If the trend holds, the next 30 days should bring in around ${next_30:,.0f}."
    elif trend_direction == "declining":
        narrative = f"Revenue has been declining ~${abs(slope):,.0f}/week. Without a change, the next 30 days may total only ${next_30:,.0f}."
    else:
        narrative = f"Revenue is relatively flat week-over-week. The 30-day forecast is approximately ${next_30:,.0f}."

    return {
        "status":             "ok",
        "trend_direction":    trend_direction,
        "weekly_slope":       round(slope, 2),
        "weekly_change_pct":  round(weekly_change_pct, 1),
        "historical":         [{"week": k, "revenue": round(v, 2)} for k, v in sorted_weeks],
        "forecast_weekly":    forecast,
        "summary": {
            "next_30_days": next_30,
            "next_60_days": next_60,
            "next_90_days": next_90,
        },
        "narrative": narrative,
    }


# ---------------------------------------------------------------------------
# Revenue trend (chart)
# ---------------------------------------------------------------------------

def get_revenue_trend(db: Client, org_id: str, weeks: int = 12) -> list[dict]:
    """Weekly revenue trend for the last N weeks (for chart)."""
    since = (date.today() - timedelta(weeks=weeks)).isoformat()
    sales = (db.table("sales").select("amount, sold_at")
             .eq("org_id", org_id).eq("payment_status", "paid")
             .gte("sold_at", since).order("sold_at").execute()).data or []

    weekly: dict[str, float] = {}
    for s in sales:
        week_label = s["sold_at"][:10]
        weekly[week_label] = weekly.get(week_label, 0) + float(s.get("amount") or 0)

    return [{"date": k, "revenue": round(v, 2)} for k, v in sorted(weekly.items())]

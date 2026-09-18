"""
Transactional email service — powered by Resend.

All send functions are fire-and-forget safe: if RESEND_API_KEY is not set,
or if the API call fails, the error is logged and swallowed.  Nothing in the
request path should block on email.

Emails sent:
  - audit_complete     → after AI audit runs
  - sync_complete      → after QuickBooks / HubSpot sync
  - weekly_brief       → Monday morning operating summary (called by scheduler)
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enabled() -> bool:
    return bool(settings.resend_api_key)


def _send(*, to: str, subject: str, html: str) -> None:
    """Low-level send via Resend SDK. Silently skips if not configured."""
    if not _enabled():
        log.debug("Email skipped (RESEND_API_KEY not set): %s → %s", subject, to)
        return
    try:
        import resend
        resend.api_key = settings.resend_api_key
        resend.Emails.send({
            "from": settings.from_email,
            "to": [to],
            "subject": subject,
            "html": html,
        })
        log.info("Email sent: %s → %s", subject, to)
    except Exception as exc:
        log.warning("Email failed (%s → %s): %s", subject, to, exc)


def _base(*, title: str, preheader: str, body_html: str, cta_url: str, cta_label: str) -> str:
    """
    Minimal responsive HTML shell.
    Uses inline styles only — email clients strip <style> tags.
    """
    app_url = settings.frontend_url
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title}</title>
</head>
<body style="margin:0;padding:0;background:#f8fafc;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
  <!-- preheader (hidden preview text) -->
  <span style="display:none;max-height:0;overflow:hidden;mso-hide:all;">{preheader}</span>

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f8fafc;padding:40px 16px;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

        <!-- Header -->
        <tr>
          <td style="background:#0f172a;border-radius:16px 16px 0 0;padding:24px 32px;">
            <span style="color:#ffffff;font-size:18px;font-weight:700;letter-spacing:-0.3px;">LBT OS</span>
            <span style="color:#94a3b8;font-size:12px;margin-left:8px;">Lean Business Tracker</span>
          </td>
        </tr>

        <!-- Body -->
        <tr>
          <td style="background:#ffffff;padding:32px;border-left:1px solid #e2e8f0;border-right:1px solid #e2e8f0;">
            {body_html}

            <!-- CTA -->
            <div style="margin-top:32px;text-align:center;">
              <a href="{cta_url}"
                 style="display:inline-block;background:#2563eb;color:#ffffff;font-weight:600;
                        font-size:14px;padding:13px 28px;border-radius:50px;text-decoration:none;
                        letter-spacing:0.01em;">
                {cta_label}
              </a>
            </div>
          </td>
        </tr>

        <!-- Footer -->
        <tr>
          <td style="background:#f1f5f9;border-radius:0 0 16px 16px;border:1px solid #e2e8f0;
                     border-top:none;padding:20px 32px;text-align:center;">
            <p style="margin:0;font-size:12px;color:#94a3b8;line-height:1.6;">
              Sent by <a href="{app_url}" style="color:#64748b;text-decoration:none;">LBT OS</a> ·
              Aera Analytics · Denver, CO<br/>
              AI-generated insights should be reviewed before major business decisions.
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Severity colour helper (used in audit email)
# ---------------------------------------------------------------------------

_SEV_COLOURS: dict[str, tuple[str, str]] = {
    "high":   ("#fff1f2", "#b91c1c"),
    "medium": ("#fffbeb", "#b45309"),
    "low":    ("#eff6ff", "#1d4ed8"),
}

def _severity_pill(severity: str) -> str:
    bg, text = _SEV_COLOURS.get(severity, ("#f1f5f9", "#64748b"))
    return (
        f'<span style="display:inline-block;background:{bg};color:{text};'
        f'font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.5px;'
        f'padding:3px 10px;border-radius:50px;">{severity}</span>'
    )


# ---------------------------------------------------------------------------
# Public send functions
# ---------------------------------------------------------------------------

def send_audit_complete(
    *,
    to: str,
    org_name: str,
    report: dict[str, Any],
) -> None:
    """
    Email sent immediately after an AI audit run.
    Shows health score + top 3 insights with a link back to the full report.
    """
    score  = report.get("health_score", 0)
    score_color = "#16a34a" if score >= 70 else "#d97706" if score >= 45 else "#dc2626"
    label  = "Healthy" if score >= 70 else "Needs Attention" if score >= 45 else "Critical"

    insights = (report.get("insights") or [])[:3]
    insight_rows = ""
    for ins in insights:
        sev = ins.get("severity", "low")
        insight_rows += f"""
        <div style="border:1px solid #e2e8f0;border-radius:12px;padding:16px;margin-bottom:12px;">
          <div style="margin-bottom:8px;">
            {_severity_pill(sev)}
            <span style="font-size:13px;font-weight:600;color:#0f172a;margin-left:8px;">
              {ins.get('title', '')}
            </span>
          </div>
          <p style="margin:0;font-size:13px;color:#475569;line-height:1.6;">
            {ins.get('detail', '')}
          </p>
          {"<p style='margin:8px 0 0;font-size:12px;font-weight:600;color:#16a34a;'>" + ins['estimated_impact'] + "</p>" if ins.get('estimated_impact') else ""}
        </div>"""

    is_truncated = report.get("is_truncated", False)
    upsell_block = ""
    if is_truncated:
        upsell_block = """
        <div style="margin-top:20px;background:#eff6ff;border:1px solid #bfdbfe;border-radius:12px;padding:16px;text-align:center;">
          <p style="margin:0 0 8px;font-size:13px;font-weight:600;color:#1e40af;">
            Upgrade to Pro for full recommendations
          </p>
          <p style="margin:0;font-size:12px;color:#3b82f6;line-height:1.5;">
            Pro unlocks the complete action plan with dollar-value estimates,
            unlimited monthly audits, and audit history.
          </p>
        </div>"""

    body = f"""
      <h1 style="margin:0 0 4px;font-size:22px;font-weight:700;color:#0f172a;letter-spacing:-0.4px;">
        Your business audit is ready
      </h1>
      <p style="margin:0 0 24px;font-size:14px;color:#64748b;">{org_name}</p>

      <!-- Health score -->
      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;
                  padding:20px 24px;display:flex;align-items:center;margin-bottom:28px;">
        <div style="text-align:center;min-width:72px;">
          <div style="font-size:36px;font-weight:800;color:{score_color};line-height:1;">{score}</div>
          <div style="font-size:10px;font-weight:700;text-transform:uppercase;
                      letter-spacing:1.4px;color:{score_color};margin-top:4px;">{label}</div>
        </div>
        <div style="margin-left:20px;flex:1;">
          <div style="font-size:13px;font-weight:600;color:#0f172a;">Business Health Score</div>
          <div style="font-size:12px;color:#64748b;margin-top:4px;line-height:1.5;">
            Computed from lead conversion rate, profit margin, customer retention,
            and expense efficiency over the last 30 days.
          </div>
        </div>
      </div>

      <h2 style="margin:0 0 14px;font-size:15px;font-weight:700;color:#0f172a;">
        What the AI found
      </h2>
      {insight_rows}
      {upsell_block}
    """

    _send(
        to=to,
        subject=f"Audit complete — health score {score} ({label}) · {org_name}",
        html=_base(
            title="AI Audit Complete",
            preheader=f"Your business health score is {score}. {insights[0]['title'] if insights else 'See your full report inside.'}",
            body_html=body,
            cta_url=f"{settings.frontend_url}/app/insights",
            cta_label="View full report →",
        ),
    )


def send_sync_complete(
    *,
    to: str,
    org_name: str,
    provider_label: str,
    stats: dict[str, Any],
    status: str,
) -> None:
    """Email sent after a QuickBooks or HubSpot sync completes."""
    status_colour = "#16a34a" if status == "success" else "#d97706" if status == "partial" else "#dc2626"
    status_label  = {"success": "Sync complete", "partial": "Sync partial", "failed": "Sync failed"}.get(status, status)

    stat_rows = ""
    for entity, count in (stats or {}).items():
        stat_rows += f"""
        <div style="display:flex;justify-content:space-between;align-items:center;
                    padding:10px 0;border-bottom:1px solid #f1f5f9;">
          <span style="font-size:13px;color:#475569;text-transform:capitalize;">{entity}</span>
          <span style="font-size:14px;font-weight:700;color:#0f172a;">{count} records</span>
        </div>"""

    body = f"""
      <h1 style="margin:0 0 4px;font-size:22px;font-weight:700;color:#0f172a;letter-spacing:-0.4px;">
        {provider_label} sync finished
      </h1>
      <p style="margin:0 0 24px;font-size:14px;color:#64748b;">{org_name}</p>

      <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;padding:20px 24px;margin-bottom:24px;">
        <div style="font-size:12px;font-weight:700;text-transform:uppercase;
                    letter-spacing:1.4px;color:{status_colour};margin-bottom:14px;">
          {status_label}
        </div>
        {stat_rows or '<p style="margin:0;font-size:13px;color:#94a3b8;">No new records were imported.</p>'}
      </div>

      <p style="margin:0;font-size:13px;color:#64748b;line-height:1.6;">
        Your dashboard and AI audit layer have been updated with the latest data from {provider_label}.
        Run a new audit to get fresh recommendations based on this import.
      </p>
    """

    _send(
        to=to,
        subject=f"{provider_label} sync {status} · {org_name}",
        html=_base(
            title=f"{provider_label} Sync Complete",
            preheader=f"{provider_label} sync finished with status {status}. {sum((stats or {}).values())} total records processed.",
            body_html=body,
            cta_url=f"{settings.frontend_url}/app/connections",
            cta_label="View sync details →",
        ),
    )


def send_weekly_brief(
    *,
    to: str,
    org_name: str,
    metrics: dict[str, Any],
) -> None:
    """
    Weekly Monday morning operating brief.
    Called by the recurring scheduler — not triggered by user actions.
    """
    r = metrics.get("revenue", {})
    leads_m = metrics.get("leads", {})
    c = metrics.get("customers", {})
    e = metrics.get("expenses", {})

    def _d(v: Any) -> str:
        return f"${(v or 0):,.0f}"

    kpi_cells = "".join(
        f"""<td style="width:50%;padding:0 6px 12px;">
          <div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:12px;padding:16px;">
            <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1.4px;color:#94a3b8;margin-bottom:8px;">{lbl}</div>
            <div style="font-size:20px;font-weight:700;color:#0f172a;">{val}</div>
            <div style="font-size:11px;color:#64748b;margin-top:4px;">{sub}</div>
          </div>
        </td>"""
        for lbl, val, sub in [
            ("Revenue",          _d(r.get("total")),  f"{r.get('margin_pct', 0):.1f}% margin"),
            ("Gross Profit",     _d(r.get("profit")), f"{_d(e.get('total'))} expenses"),
            ("Lead Conversion",  f"{leads_m.get('conversion_rate_pct', 0):.1f}%", f"{leads_m.get('won', 0)} won of {leads_m.get('total', 0)}"),
            ("Repeat Customers", f"{c.get('repeat_pct', 0):.1f}%", f"{c.get('repeat', 0)} returning"),
        ]
    )

    followup_alert = ""
    missed = leads_m.get("missed_follow_ups", 0)
    if missed > 0:
        followup_alert = (
            f"<div style='background:#fff7ed;border:1px solid #fed7aa;border-radius:12px;"
            f"padding:16px;margin-bottom:12px;'>"
            f"<p style='margin:0;font-size:13px;color:#9a3412;'>"
            f"<strong>Follow-up alert:</strong> {missed} lead{'s' if missed != 1 else ''} "
            f"have a follow-up scheduled but haven't been contacted.</p></div>"
        )

    revenue_str  = _d(r.get("total"))
    conv_str     = f"{leads_m.get('conversion_rate_pct', 0):.0f}"

    body = f"""
      <h1 style="margin:0 0 4px;font-size:22px;font-weight:700;color:#0f172a;letter-spacing:-0.4px;">
        Your weekly operating brief
      </h1>
      <p style="margin:0 0 24px;font-size:14px;color:#64748b;">{org_name} · Last 30 days</p>

      <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:12px;">
        <tr>{kpi_cells[:len(kpi_cells)//2*2 // 2 * 2]}</tr>
      </table>

      {followup_alert}

      <p style="margin:0;font-size:13px;color:#64748b;line-height:1.6;">
        Open your dashboard for the full analyst brief, revenue trend chart, and this week's action board.
      </p>
    """

    _send(
        to=to,
        subject=f"Weekly brief · {revenue_str} revenue, {conv_str}% conversion · {org_name}",
        html=_base(
            title="Weekly Operating Brief",
            preheader=f"{org_name}: {revenue_str} revenue, {conv_str}% conversion last 30 days.",
            body_html=body,
            cta_url=f"{settings.frontend_url}/app",
            cta_label="Open dashboard →",
        ),
    )

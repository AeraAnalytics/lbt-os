"""
Audit PDF Generator — renders an LBT OS audit report as a branded PDF.

Uses reportlab (pure Python, no system dependencies).
Designed to look like a real analyst deliverable, not a printout.
"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ---------------------------------------------------------------------------
# Brand colours
# ---------------------------------------------------------------------------
BRAND_DARK   = colors.HexColor("#0f172a")   # slate-950
BRAND_BLUE   = colors.HexColor("#2563eb")   # blue-600
BRAND_LIGHT  = colors.HexColor("#f8fafc")   # slate-50
SLATE_400    = colors.HexColor("#94a3b8")
SLATE_600    = colors.HexColor("#475569")
RED_600      = colors.HexColor("#dc2626")
AMBER_500    = colors.HexColor("#f59e0b")
GREEN_600    = colors.HexColor("#16a34a")
BLUE_50      = colors.HexColor("#eff6ff")
ROSE_50      = colors.HexColor("#fff1f2")
AMBER_50     = colors.HexColor("#fffbeb")
GREEN_50     = colors.HexColor("#f0fdf4")


def _styles() -> dict[str, ParagraphStyle]:
    return {
        "kicker": ParagraphStyle("kicker", fontSize=8,  fontName="Helvetica-Bold",
                                 textColor=BRAND_BLUE, spaceAfter=4, leading=10,
                                 letterSpacing=1.5),
        "h1":     ParagraphStyle("h1",     fontSize=22, fontName="Helvetica-Bold",
                                 textColor=BRAND_DARK, spaceAfter=4, leading=28),
        "h2":     ParagraphStyle("h2",     fontSize=13, fontName="Helvetica-Bold",
                                 textColor=BRAND_DARK, spaceAfter=6, spaceBefore=18, leading=18),
        "body":   ParagraphStyle("body",   fontSize=10, leading=15, textColor=SLATE_600),
        "small":  ParagraphStyle("small",  fontSize=8,  leading=11, textColor=SLATE_400),
        "badge":  ParagraphStyle("badge",  fontSize=8,  fontName="Helvetica-Bold",
                                 textColor=BRAND_BLUE,  leading=10),
        "impact": ParagraphStyle("impact", fontSize=9,  fontName="Helvetica-Bold",
                                 textColor=GREEN_600, leading=12),
        "score":  ParagraphStyle("score",  fontSize=42, fontName="Helvetica-Bold",
                                 textColor=BRAND_DARK, alignment=TA_CENTER, leading=50),
        "rec_num":ParagraphStyle("rec_num", fontSize=11, fontName="Helvetica-Bold",
                                 textColor=colors.white, alignment=TA_CENTER, leading=14),
        "tag":    ParagraphStyle("tag", fontSize=8, fontName="Helvetica-Bold",
                                 textColor=SLATE_600, leading=10),
    }


def _severity_colour(severity: str) -> tuple[colors.Color, colors.Color]:
    """Returns (bg, text) colour pair for severity badge."""
    return {
        "high":   (ROSE_50,  RED_600),
        "medium": (AMBER_50, AMBER_500),
        "low":    (BLUE_50,  BRAND_BLUE),
    }.get(severity, (BRAND_LIGHT, SLATE_600))


def _type_label(insight_type: str) -> str:
    return {
        "revenue_leak":       "Revenue Leak",
        "missed_opportunity": "Missed Opportunity",
        "inefficiency":       "Inefficiency",
        "strength":           "Strength",
    }.get(insight_type, insight_type.replace("_", " ").title())


def _score_label(score: int) -> tuple[str, colors.Color]:
    if score >= 70:
        return "Healthy", GREEN_600
    if score >= 45:
        return "Needs Attention", AMBER_500
    return "Critical", RED_600


def generate_audit_pdf(report: dict[str, Any], org_name: str) -> bytes:
    """
    Render a full audit report as a PDF and return the raw bytes.
    """
    buf  = io.BytesIO()
    doc  = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        rightMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    S    = _styles()
    W    = doc.width
    elements: list = []

    # ------------------------------------------------------------------
    # HEADER
    # ------------------------------------------------------------------
    generated_at = report.get("generated_at", "")
    try:
        dt_label = datetime.fromisoformat(generated_at.replace("Z", "+00:00")).strftime("%B %-d, %Y")
    except Exception:
        dt_label = generated_at[:10] if generated_at else "—"

    header_data = [[
        Paragraph("LBT OS", ParagraphStyle("logo", fontSize=14, fontName="Helvetica-Bold",
                                            textColor=colors.white, leading=18)),
        Paragraph(
            f"AI Business Audit<br/>"
            f"<font size='9' color='#94a3b8'>{org_name}</font>",
            ParagraphStyle("hdr_title", fontSize=13, fontName="Helvetica-Bold",
                           textColor=colors.white, alignment=TA_RIGHT, leading=18),
        ),
    ]]
    header_table = Table(header_data, colWidths=[W * 0.5, W * 0.5])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), BRAND_DARK),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 14),
        ("RIGHTPADDING", (0, 0), (-1, -1), 14),
        ("TOPPADDING",   (0, 0), (-1, -1), 14),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 14),
        ("ROUNDEDCORNERS", [8]),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 0.2 * inch))

    # Period row
    period_start = report.get("period_start", "")
    period_end   = report.get("period_end", "")
    elements.append(Paragraph(
        f"Period: {period_start} — {period_end}  ·  Generated: {dt_label}  ·  "
        f"Model: {report.get('model_used', '—')}",
        S["small"],
    ))
    elements.append(Spacer(1, 0.15 * inch))
    elements.append(HRFlowable(width=W, thickness=1, color=colors.HexColor("#e2e8f0")))
    elements.append(Spacer(1, 0.2 * inch))

    # ------------------------------------------------------------------
    # HEALTH SCORE
    # ------------------------------------------------------------------
    score = int(report.get("health_score") or 50)
    label, score_color = _score_label(score)

    score_data = [[
        Table(
            [[Paragraph(str(score), S["score"])]],
            colWidths=[1.4 * inch],
            style=TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), BRAND_LIGHT),
                ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING",    (0, 0), (-1, -1), 14),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
                ("ROUNDEDCORNERS", [8]),
                ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#e2e8f0")),
            ]),
        ),
        Table(
            [
                [Paragraph("Business Health Score", ParagraphStyle(
                    "hs_label", fontSize=10, textColor=SLATE_400, leading=14))],
                [Paragraph(label, ParagraphStyle(
                    "hs_val", fontSize=20, fontName="Helvetica-Bold",
                    textColor=score_color, leading=26))],
                [Paragraph(
                    "Score computed from lead conversion rate, profit margin, "
                    "customer retention, and expense efficiency.",
                    S["small"],
                )],
            ],
            colWidths=[W - 1.7 * inch],
            style=TableStyle([
                ("VALIGN",  (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 16),
            ]),
        ),
    ]]
    elements.append(Table(score_data, colWidths=[1.7 * inch, W - 1.7 * inch]))
    elements.append(Spacer(1, 0.25 * inch))

    # ------------------------------------------------------------------
    # INSIGHTS
    # ------------------------------------------------------------------
    insights = report.get("insights") or []
    if insights:
        elements.append(Paragraph("WHAT THE AI FOUND", S["kicker"]))
        elements.append(Paragraph("Key findings from your 30-day operating data", S["h2"]))

        for ins in insights:
            bg, text_c = _severity_colour(ins.get("severity", "low"))
            type_label  = _type_label(ins.get("type", ""))
            sev_label   = (ins.get("severity") or "").upper()
            impact      = ins.get("estimated_impact") or ""

            badge_cell = Table(
                [[Paragraph(f"{type_label}  ·  {sev_label}", S["badge"])]],
                style=TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), bg),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 8),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 8),
                    ("TOPPADDING",    (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("ROUNDEDCORNERS", [4]),
                ]),
            )

            title_para = Paragraph(f"<b>{ins.get('title', '')}</b>", ParagraphStyle(
                "ins_title", fontSize=11, fontName="Helvetica-Bold",
                textColor=BRAND_DARK, leading=15, spaceBefore=2,
            ))
            detail_para = Paragraph(ins.get("detail", ""), S["body"])
            impact_para = Paragraph(f"Estimated impact: {impact}", S["impact"]) if impact else Spacer(1, 0)

            card_data = [[badge_cell], [title_para], [detail_para], [impact_para]]
            card = Table(
                card_data,
                colWidths=[W],
                style=TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), colors.white),
                    ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#e2e8f0")),
                    ("LEFTPADDING",   (0, 0), (-1, -1), 14),
                    ("RIGHTPADDING",  (0, 0), (-1, -1), 14),
                    ("TOPPADDING",    (0, 0), (0, 0),   10),
                    ("TOPPADDING",    (0, 1), (-1, -1),  6),
                    ("BOTTOMPADDING", (0, -1), (-1, -1), 12),
                    ("ROUNDEDCORNERS", [8]),
                ]),
            )
            elements.append(card)
            elements.append(Spacer(1, 0.12 * inch))

    # ------------------------------------------------------------------
    # RECOMMENDATIONS
    # ------------------------------------------------------------------
    recs = report.get("recommendations") or []
    if recs:
        elements.append(Spacer(1, 0.1 * inch))
        elements.append(Paragraph("RECOMMENDED ACTIONS", S["kicker"]))
        elements.append(Paragraph("Prioritised next steps to move the needle", S["h2"]))

        for rec in recs:
            priority   = rec.get("priority", "")
            action     = rec.get("action", "")
            why        = rec.get("why", "")
            timeframe  = rec.get("timeframe", "")

            num_cell = Table(
                [[Paragraph(str(priority), S["rec_num"])]],
                colWidths=[0.45 * inch],
                rowHeights=[0.45 * inch],
                style=TableStyle([
                    ("BACKGROUND",    (0, 0), (-1, -1), BRAND_BLUE),
                    ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
                    ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
                    ("ROUNDEDCORNERS", [22]),
                ]),
            )
            content_data = [
                [Paragraph(f"<b>{action}</b>", ParagraphStyle(
                    "rec_action", fontSize=10, fontName="Helvetica-Bold",
                    textColor=BRAND_DARK, leading=14))],
                [Paragraph(why, S["body"])],
                [Paragraph(f"⏱  {timeframe}", ParagraphStyle(
                    "timeframe", fontSize=8, fontName="Helvetica-Bold",
                    textColor=BRAND_BLUE, leading=12, spaceBefore=2))],
            ]
            content_table = Table(content_data, colWidths=[W - 0.75 * inch],
                                  style=TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 12),
                                                    ("TOPPADDING",  (0, 0), (-1, -1), 2)]))

            row_data = [[num_cell, content_table]]
            row = Table(
                row_data,
                colWidths=[0.6 * inch, W - 0.6 * inch],
                style=TableStyle([
                    ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                    ("BACKGROUND",    (0, 0), (-1, -1), colors.white),
                    ("BOX",           (0, 0), (-1, -1), 1, colors.HexColor("#e2e8f0")),
                    ("LEFTPADDING",   (0, 0), (0, 0),  12),
                    ("TOPPADDING",    (0, 0), (-1, -1), 12),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING",  (-1, 0), (-1, -1), 14),
                    ("ROUNDEDCORNERS", [8]),
                ]),
            )
            elements.append(row)
            elements.append(Spacer(1, 0.12 * inch))

    # ------------------------------------------------------------------
    # FOOTER
    # ------------------------------------------------------------------
    elements.append(Spacer(1, 0.3 * inch))
    elements.append(HRFlowable(width=W, thickness=1, color=colors.HexColor("#e2e8f0")))
    elements.append(Spacer(1, 0.1 * inch))
    elements.append(Paragraph(
        f"Generated by LBT OS · Aera Analytics · {dt_label} · "
        "This report is AI-generated and should be reviewed by a business advisor before major decisions.",
        ParagraphStyle("footer", fontSize=7, textColor=SLATE_400, alignment=TA_CENTER, leading=10),
    ))

    doc.build(elements)
    return buf.getvalue()

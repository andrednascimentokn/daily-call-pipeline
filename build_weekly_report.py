#!/usr/bin/env python3
"""Weekly overview PDF builder.

Renders the weekly-overview analysis (from weekly_analysis.analyze_weekly) into a
PDF that mirrors the daily report's visual identity but frames everything at the
WEEK level (no call-by-call table). Reuses styles/helpers from build_report.py.
"""
import locale
from datetime import datetime
from pathlib import Path

try:
    locale.setlocale(locale.LC_TIME, "pt_PT.UTF-8")
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, "pt_PT")
    except locale.Error:
        pass

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, NextPageTemplate,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.flowables import KeepTogether
from reportlab.lib import colors

# Reuse the daily report's visual identity + helpers (no duplication).
from build_report import (
    build_styles, md_to_html, header_page, normal_page,
    DARK_BLUE, ORANGE, LIGHT_GRAY, MED_GRAY, WHITE,
    GREEN_BG, GREEN_TEXT, TEXT_DARK, TEXT_LIGHT,
)

_PT_MONTHS = {
    1: "janeiro", 2: "fevereiro", 3: "março", 4: "abril", 5: "maio", 6: "junho",
    7: "julho", 8: "agosto", 9: "setembro", 10: "outubro", 11: "novembro", 12: "dezembro",
}


def _fmt_range(week_start: datetime, week_end: datetime) -> str:
    if week_start.month == week_end.month:
        return f"{week_start.day} a {week_end.day} de {_PT_MONTHS[week_end.month]} de {week_end.year}"
    return (
        f"{week_start.day} de {_PT_MONTHS[week_start.month]} a "
        f"{week_end.day} de {_PT_MONTHS[week_end.month]} de {week_end.year}"
    )


def generate_weekly_pdf(weekly, week_start, week_end, weekly_metrics,
                        agent_display, agent_name, output_path):
    """Generate the weekly overview PDF. Returns the path written."""
    styles = build_styles()
    aw = A4[0] - 4 * cm
    range_str = _fmt_range(week_start, week_end)

    total = weekly_metrics.get("total_calls", 0)
    completed = weekly_metrics.get("completed", 0)
    transcribed = weekly_metrics.get("transcribed", 0)
    days_active = weekly_metrics.get("days_active", 0)
    num_days = weekly_metrics.get("num_days", 5) or 5
    avg_per_day = round(total / num_days, 1)
    connect_rate = int(completed / total * 100) if total else 0

    elements = []

    # HEADER
    elements.append(Spacer(1, 10))
    elements.append(Paragraph("Resumo Semanal de Chamadas", styles["ReportTitle"]))
    elements.append(Paragraph(f"{agent_display} | Semana de {range_str}", styles["ReportSubtitle"]))
    meta = (
        f"{total} chamadas na semana • {transcribed} transcritas • "
        f"{days_active} dias com atividade • Gerado a {datetime.now().strftime('%d %B %Y')}"
    )
    elements.append(Paragraph(meta, styles["ReportMeta"]))
    elements.append(Spacer(1, 20))

    # STATS BAR (week-level)
    stats = [
        [
            Paragraph(f"<b>{total}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{completed}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{transcribed}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{avg_per_day}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{days_active}</b>", styles["StatNumber"]),
        ],
        [
            Paragraph("Total da Semana", styles["StatLabel"]),
            Paragraph("Completadas", styles["StatLabel"]),
            Paragraph("Transcritas", styles["StatLabel"]),
            Paragraph("Média por Dia", styles["StatLabel"]),
            Paragraph("Dias com Atividade", styles["StatLabel"]),
        ],
        [
            Paragraph("semana inteira", styles["StatSublabel"]),
            Paragraph(f"{connect_rate}% taxa de contacto", styles["StatSublabel"]),
            Paragraph("analisadas no coaching", styles["StatSublabel"]),
            Paragraph(f"em {num_days} dias úteis", styles["StatSublabel"]),
            Paragraph("dias com chamadas", styles["StatSublabel"]),
        ],
    ]
    col_w = (A4[0] - 4 * cm) / 5
    st = Table(stats, colWidths=[col_w] * 5)
    st.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, MED_GRAY),
        ("TOPPADDING", (0, 0), (-1, 0), 10),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
    ]))
    elements.append(st)
    elements.append(Spacer(1, 20))

    # 1. VISÃO GERAL DA SEMANA
    elements.append(Paragraph("1. Visão Geral da Semana", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    overview = md_to_html(weekly.get("week_overview", "Visão geral não disponível."))
    commentary = weekly.get("metrics_commentary", "")
    if commentary:
        overview += f"<br/><br/>{md_to_html(commentary)}"
    at = Table([[Paragraph(overview, styles["BodyText2"])]], colWidths=[aw])
    at.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(at)
    elements.append(Spacer(1, 16))

    # 2. PONTOS FORTES RECORRENTES
    elements.append(Paragraph(f"2. Pontos Fortes Recorrentes de {agent_name}", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    strengths = weekly.get("recurring_strengths", [])
    if not strengths:
        elements.append(Paragraph("Sem forças recorrentes identificadas esta semana.", styles["BodyText2"]))
    for item in strengths:
        title = item.get("title", "") if isinstance(item, dict) else str(item)
        desc = item.get("description", "") if isinstance(item, dict) else ""
        text = f'<font color="#388E3C"><b>&#10003;</b></font> <b>{md_to_html(title)}</b>'
        if desc:
            text += f'<br/>{md_to_html(desc)}'
        bt = Table([[Paragraph(text, styles["BulletItem"])]], colWidths=[aw])
        bt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), GREEN_BG),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("LINEAFTER", (0, 0), (0, -1), 3, GREEN_TEXT),
        ]))
        elements.append(bt)
        elements.append(Spacer(1, 4))
    elements.append(Spacer(1, 12))

    # 3. PONTOS FRACOS RECORRENTES
    elements.append(Paragraph("3. Pontos Fracos Recorrentes &amp; Padrões", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    for i, w in enumerate(weekly.get("recurring_weaknesses", []), 1):
        title = w.get("title", "") if isinstance(w, dict) else str(w)
        desc = w.get("description", "") if isinstance(w, dict) else ""
        action = w.get("action", "") if isinstance(w, dict) else ""
        inner = []
        inner.append(Paragraph(f"<b>#{i} — {md_to_html(title)}</b>", styles["WeaknessTitle"]))
        if desc:
            inner.append(Paragraph(md_to_html(desc), styles["WeaknessBody"]))
        if action:
            inner.append(Spacer(1, 4))
            ab = Table(
                [[Paragraph(f"<b>Ação:</b> {md_to_html(action)}", styles["WeaknessBody"])]],
                colWidths=[aw - cm],
            )
            ab.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF3E0")),
                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]))
            inner.append(ab)
        wt = Table([[inner]], colWidths=[aw])
        wt.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("LINEBEFORE", (0, 0), (0, -1), 3, ORANGE),
        ]))
        elements.append(KeepTogether([wt, Spacer(1, 6)]))
    elements.append(Spacer(1, 8))

    # 4. TENDÊNCIA DA SEMANA
    elements.append(Paragraph("4. Tendência da Semana", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    tt = Table(
        [[Paragraph(md_to_html(weekly.get("weekly_trend", "Sem dados de tendência.")), styles["BodyText2"])]],
        colWidths=[aw],
    )
    tt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(tt)
    elements.append(Spacer(1, 16))

    # 5. FOCO PARA A PRÓXIMA SEMANA
    elements.append(Paragraph("5. Foco para a Próxima Semana", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    badge_s = ParagraphStyle("Badge", fontSize=14, fontName="Helvetica-Bold", textColor=WHITE, alignment=TA_CENTER)
    for i, act in enumerate(weekly.get("focus_next_week", []), 1):
        title = act.get("title", "") if isinstance(act, dict) else str(act)
        desc = act.get("description", "") if isinstance(act, dict) else ""
        example = act.get("example_phrase", "") if isinstance(act, dict) else ""
        inner = []
        inner.append(Paragraph(f"<b>{md_to_html(title)}</b>", styles["CoachingTitle"]))
        if desc:
            inner.append(Paragraph(md_to_html(desc), styles["CoachingBody"]))
        if example:
            example = example.strip("*\"'“”")
            inner.append(Paragraph(f'<i>“{md_to_html(example)}”</i>', styles["CoachingBody"]))
        badge = Table([[Paragraph(str(i), badge_s)]], colWidths=[28], rowHeights=[28])
        badge.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ORANGE),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        row = Table([[badge, inner]], colWidths=[36, A4[0] - 4 * cm - 36])
        row.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (0, -1), 0), ("LEFTPADDING", (1, 0), (1, -1), 8),
        ]))
        elements.append(KeepTogether([row, Spacer(1, 6)]))

    # FOOTER
    elements.append(Spacer(1, 20))
    elements.append(Paragraph(
        f"Confidencial — Coaching Comercial Interno • Gerado a {datetime.now().strftime('%d %B %Y')}",
        styles["FooterStyle"],
    ))

    filepath = Path(output_path)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    doc = BaseDocTemplate(str(filepath), pagesize=A4, topMargin=2*cm, bottomMargin=2*cm, leftMargin=2*cm, rightMargin=2*cm)
    f1 = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height - 60, id="first")
    f2 = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="rest")
    doc.addPageTemplates([
        PageTemplate(id="First", frames=f1, onPage=header_page),
        PageTemplate(id="Later", frames=f2, onPage=normal_page),
    ])
    elements.insert(0, NextPageTemplate("Later"))
    doc.build(elements)
    print(f"Weekly PDF generated: {filepath}")
    return str(filepath)

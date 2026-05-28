#!/usr/bin/env python3
"""Generic PDF report builder — works for any agent.

Two ways to invoke:

(1) By agent name + date (legacy Mac flow):
    python3 build_report.py --agent ruben --date 2026-05-08 --total-calls 22 --completed-calls 14
    Reads analysis.json from agents/<Agent>_Call_Report/data/<dd-mm-yyyy>/ and
    writes the PDF to agents/<Agent>_Call_Report/reports/.

(2) By absolute paths (VPS flow — workdir is somewhere in /tmp/pipeline/):
    python3 build_report.py \\
        --analysis-path /tmp/pipeline/2026-05-27/Ruben_Call_Report/analysis.json \\
        --output-path /tmp/pipeline/2026-05-27/Ruben_Call_Report/Ruben_Call_Report_27-05-2026.pdf \\
        --date 2026-05-27 --total-calls 71 --completed-calls 37 \\
        --agent-display "Rúben @ NOVA Digital" --agent-name "Rúben"
"""
import argparse
import json
import locale
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    locale.setlocale(locale.LC_TIME, "pt_PT.UTF-8")
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, "pt_PT")
    except locale.Error:
        pass  # fallback to system default

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, NextPageTemplate,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)
from reportlab.platypus.flowables import KeepTogether

from agents_config import AGENTS

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
DARK_BLUE = colors.HexColor("#2C3E6B")
ORANGE = colors.HexColor("#E8731A")
LIGHT_GRAY = colors.HexColor("#F5F5F5")
MED_GRAY = colors.HexColor("#E0E0E0")
WHITE = colors.white
TEXT_DARK = colors.HexColor("#333333")
TEXT_LIGHT = colors.HexColor("#666666")
GREEN_BG = colors.HexColor("#E8F5E9")
GREEN_TEXT = colors.HexColor("#388E3C")


def md_to_html(text):
    if not text:
        return ""
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*(?!\*)", r'<i>\1</i>', text)
    text = re.sub(r"(?<![a-zA-Z])_(.+?)_(?![a-zA-Z])", r'<i>\1</i>', text)
    text = text.replace("&", "&amp;").replace("&amp;amp;", "&amp;")
    return text


def build_styles():
    s = getSampleStyleSheet()
    defs = {
        "ReportTitle": dict(parent=s["Title"], fontSize=24, textColor=WHITE, alignment=TA_LEFT, spaceAfter=4, fontName="Helvetica-Bold"),
        "ReportSubtitle": dict(parent=s["Normal"], fontSize=11, textColor=colors.HexColor("#CCCCCC"), alignment=TA_LEFT, spaceAfter=2),
        "ReportMeta": dict(parent=s["Normal"], fontSize=9, textColor=colors.HexColor("#AAAAAA"), alignment=TA_LEFT),
        "SectionTitle": dict(parent=s["Heading1"], fontSize=16, textColor=DARK_BLUE, spaceBefore=16, spaceAfter=4, fontName="Helvetica-Bold"),
        "BodyText2": dict(parent=s["Normal"], fontSize=10, textColor=TEXT_DARK, alignment=TA_JUSTIFY, leading=14, spaceAfter=6),
        "BulletItem": dict(parent=s["Normal"], fontSize=10, textColor=TEXT_DARK, leftIndent=20, leading=13, spaceAfter=4),
        "WeaknessTitle": dict(parent=s["Normal"], fontSize=11, textColor=TEXT_DARK, fontName="Helvetica-Bold", spaceAfter=2),
        "WeaknessBody": dict(parent=s["Normal"], fontSize=10, textColor=TEXT_DARK, leftIndent=10, leading=13, spaceAfter=2),
        "CoachingTitle": dict(parent=s["Normal"], fontSize=11, textColor=TEXT_DARK, fontName="Helvetica-Bold", spaceAfter=2),
        "CoachingBody": dict(parent=s["Normal"], fontSize=10, textColor=TEXT_DARK, leftIndent=10, leading=13, spaceAfter=2),
        "FooterStyle": dict(parent=s["Normal"], fontSize=8, textColor=TEXT_LIGHT, alignment=TA_CENTER),
        "StatNumber": dict(parent=s["Normal"], fontSize=22, fontName="Helvetica-Bold", textColor=DARK_BLUE, alignment=TA_CENTER),
        "StatLabel": dict(parent=s["Normal"], fontSize=8, textColor=TEXT_LIGHT, alignment=TA_CENTER),
        "StatSublabel": dict(parent=s["Normal"], fontSize=7, textColor=TEXT_LIGHT, alignment=TA_CENTER),
    }
    for name, kw in defs.items():
        s.add(ParagraphStyle(name, **kw))
    return s


def header_page(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(DARK_BLUE)
    canvas.rect(0, A4[1] - 130, A4[0], 130, fill=1, stroke=0)
    canvas.restoreState()


def normal_page(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(TEXT_LIGHT)
    canvas.drawCentredString(
        A4[0] / 2, 15 * mm,
        f"Confidencial — Coaching Comercial Interno • Gerado a {datetime.now().strftime('%d %B %Y')}",
    )
    canvas.restoreState()


def outcome_color(outcome):
    ol = (outcome or "").lower()
    if any(w in ol for w in ["commit", "paid"]):
        return "#388E3C"
    elif any(w in ol for w in ["warm", "near", "active"]):
        return "#F57C00"
    elif any(w in ol for w in ["soft", "hold"]):
        return "#F57C00"
    elif any(w in ol for w in ["lost", "no_contact", "no contact", "blocked"]):
        return "#D32F2F"
    return "#333333"


def generate_pdf(analysis, report_date, total_calls, completed_calls, agent_display, reports_dir, agent_name, output_path=None):
    """Generate a PDF report from analysis.json data.

    If `output_path` is given, the PDF is written there directly (and `reports_dir`
    is ignored except for ensuring the parent exists). Otherwise the PDF lands in
    `reports_dir/<agent_name>_Call_Report_<dd-mm-yyyy>.pdf`.

    Returns the path to the generated PDF.
    """
    styles = build_styles()
    weekday = report_date.strftime("%A")
    date_str = report_date.strftime("%d %B %Y")
    date_label = report_date.strftime("%d-%m-%Y")

    call_summary = analysis.get("call_summary", [])
    recordings_reviewed = len(call_summary)
    warm_count = sum(
        1 for c in call_summary
        if any(w in c.get("outcome", "").lower() for w in ["commit", "warm", "near", "paid"])
    )

    top_talk_name = "N/A"
    top_duration_str = "N/A"
    top_duration = 0.0
    for c in call_summary:
        dur_str = c.get("duration", "0")
        try:
            mins = float(re.sub(r"[^\d.]", "", dur_str))
        except ValueError:
            mins = 0.0
        if mins > top_duration:
            top_duration = mins
            top_talk_name = f"{c.get('contact', 'N/A')} — {c.get('outcome', '')}"
            top_duration_str = f"{int(mins)} min"

    elements = []

    # HEADER
    elements.append(Spacer(1, 10))
    elements.append(Paragraph("Relatório de Chamadas", styles["ReportTitle"]))
    elements.append(Paragraph(f"{agent_display} | {weekday}, {date_str}", styles["ReportSubtitle"]))
    meta = (
        f"{total_calls} chamadas registadas \u2022 {recordings_reviewed} com gravação \u2022 "
        f"{recordings_reviewed} gravações analisadas \u2022 Gerado a {datetime.now().strftime('%d %B %Y')}"
    )
    elements.append(Paragraph(meta, styles["ReportMeta"]))
    elements.append(Spacer(1, 20))

    # STATS BAR
    connect_rate = int(completed_calls / total_calls * 100) if total_calls else 0
    stats = [
        [
            Paragraph(f"<b>{total_calls}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{completed_calls}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{recordings_reviewed}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{warm_count}</b>", styles["StatNumber"]),
            Paragraph(f"<b>{top_duration_str}</b>", styles["StatNumber"]),
        ],
        [
            Paragraph("Total de Chamadas", styles["StatLabel"]),
            Paragraph("Completadas", styles["StatLabel"]),
            Paragraph("Gravações Analisadas", styles["StatLabel"]),
            Paragraph("Quentes/Perto de Fechar", styles["StatLabel"]),
            Paragraph("Maior Duração", styles["StatLabel"]),
        ],
        [
            Paragraph("1 dia", styles["StatSublabel"]),
            Paragraph(f"{connect_rate}% taxa de contacto", styles["StatSublabel"]),
            Paragraph("chamadas substantivas", styles["StatSublabel"]),
            Paragraph("das chamadas analisadas", styles["StatSublabel"]),
            Paragraph(top_talk_name, styles["StatSublabel"]),
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

    # 1. OVERALL ASSESSMENT
    elements.append(Paragraph("1. Avaliação Geral", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    at = Table(
        [[Paragraph(md_to_html(analysis.get("overall_assessment", "Avaliação não disponível.")), styles["BodyText2"])]],
        colWidths=[A4[0] - 4 * cm],
    )
    at.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT_GRAY),
        ("TOPPADDING", (0, 0), (-1, -1), 12), ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
        ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
    ]))
    elements.append(at)
    elements.append(Spacer(1, 16))

    # 2. WHAT AGENT DOES WELL
    elements.append(Paragraph(f"2. O Que {agent_name} Faz Bem", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    for item in analysis.get("what_agent_does_well", []):
        title = item.get("title", "") if isinstance(item, dict) else str(item)
        desc = item.get("description", "") if isinstance(item, dict) else ""
        text = f'<font color="#388E3C"><b>&#10003;</b></font> <b>{md_to_html(title)}</b>'
        if desc:
            text += f'<br/>{md_to_html(desc)}'
        bt = Table([[Paragraph(text, styles["BulletItem"])]], colWidths=[A4[0] - 4 * cm])
        bt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), GREEN_BG),
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 12), ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("LINEAFTER", (0, 0), (0, -1), 3, GREEN_TEXT),
        ]))
        elements.append(bt)
        elements.append(Spacer(1, 4))
    elements.append(Spacer(1, 12))

    # 3. CRITICAL WEAKNESSES
    elements.append(Paragraph("3. Pontos Fracos Críticos &amp; Oportunidades Perdidas", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))
    for i, w in enumerate(analysis.get("critical_weaknesses", []), 1):
        title = w.get("title", "") if isinstance(w, dict) else str(w)
        desc = w.get("description", "") if isinstance(w, dict) else ""
        moments = w.get("key_missed_moments", []) if isinstance(w, dict) else []
        action = w.get("action", "") if isinstance(w, dict) else ""

        inner = []
        inner.append(Paragraph(f"<b>#{i} \u2014 {md_to_html(title)}</b>", styles["WeaknessTitle"]))
        if desc:
            inner.append(Paragraph(md_to_html(desc), styles["WeaknessBody"]))
        if moments:
            inner.append(Paragraph("<b>Momentos-chave perdidos:</b>", styles["WeaknessBody"]))
            for m in moments:
                inner.append(Paragraph(f"\u2022 {md_to_html(str(m))}", styles["WeaknessBody"]))
        if action:
            inner.append(Spacer(1, 4))
            ab = Table(
                [[Paragraph(f"<b>Ação:</b> {md_to_html(action)}", styles["WeaknessBody"])]],
                colWidths=[A4[0] - 5 * cm],
            )
            ab.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF3E0")),
                ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]))
            inner.append(ab)

        wt = Table([[inner]], colWidths=[A4[0] - 4 * cm])
        wt.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (0, 0), (-1, -1), 14), ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("LINEBEFORE", (0, 0), (0, -1), 3, ORANGE),
        ]))
        elements.append(KeepTogether([wt, Spacer(1, 6)]))

    # 4. CALL-BY-CALL SUMMARY
    elements.append(Paragraph("4. Resumo Chamada a Chamada (Gravações Analisadas)", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))

    hs = ParagraphStyle("TH", fontSize=9, fontName="Helvetica-Bold", textColor=WHITE)
    cs = ParagraphStyle("TC", fontSize=9, textColor=TEXT_DARK, leading=11)

    td = [[
        Paragraph("Contacto", hs), Paragraph("Duração", hs),
        Paragraph("Resultado", hs), Paragraph("Ponto-Chave", hs),
    ]]
    for c in call_summary:
        oc = outcome_color(c.get("outcome", ""))
        td.append([
            Paragraph(c.get("contact", ""), cs),
            Paragraph(c.get("duration", ""), cs),
            Paragraph(f'<font color="{oc}"><b>{c.get("outcome", "")}</b></font>', cs),
            Paragraph(c.get("key_issue", ""), cs),
        ])

    aw = A4[0] - 4 * cm
    summary_table = Table(td, colWidths=[aw * 0.20, aw * 0.10, aw * 0.14, aw * 0.56])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), ORANGE),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, MED_GRAY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 20))

    # 5. PRIORITY COACHING ACTIONS
    elements.append(Paragraph("5. Top 5 Ações Prioritárias de Coaching", styles["SectionTitle"]))
    elements.append(HRFlowable(width="100%", thickness=2, color=ORANGE, spaceAfter=8))

    badge_s = ParagraphStyle("Badge", fontSize=14, fontName="Helvetica-Bold", textColor=WHITE, alignment=TA_CENTER)

    for i, act in enumerate(analysis.get("priority_coaching_actions", []), 1):
        title = act.get("title", "") if isinstance(act, dict) else str(act)
        desc = act.get("description", "") if isinstance(act, dict) else ""
        example = act.get("example_phrase", "") if isinstance(act, dict) else ""

        inner = []
        inner.append(Paragraph(f"<b>{md_to_html(title)}</b>", styles["CoachingTitle"]))
        if desc:
            inner.append(Paragraph(md_to_html(desc), styles["CoachingBody"]))
        if example:
            example = example.strip("*\"'\u201c\u201d")
            inner.append(Paragraph(f'<i>\u201c{md_to_html(example)}\u201d</i>', styles["CoachingBody"]))

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
        f"Confidencial \u2014 Coaching Comercial Interno \u2022 Gerado a {datetime.now().strftime('%d %B %Y')}",
        styles["FooterStyle"],
    ))

    # Build PDF
    if output_path is not None:
        filepath = Path(output_path)
        filepath.parent.mkdir(parents=True, exist_ok=True)
    else:
        reports_dir.mkdir(parents=True, exist_ok=True)
        filepath = reports_dir / f"{agent_name}_Call_Report_{date_label}.pdf"
    doc = BaseDocTemplate(str(filepath), pagesize=A4, topMargin=2*cm, bottomMargin=2*cm, leftMargin=2*cm, rightMargin=2*cm)
    f1 = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height - 60, id="first")
    f2 = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="rest")
    doc.addPageTemplates([
        PageTemplate(id="First", frames=f1, onPage=header_page),
        PageTemplate(id="Later", frames=f2, onPage=normal_page),
    ])
    elements.insert(0, NextPageTemplate("Later"))
    doc.build(elements)
    print(f"PDF generated: {filepath}")
    return str(filepath)


def main():
    parser = argparse.ArgumentParser(description="Generate PDF call report for any agent")
    parser.add_argument("--agent", help="Agent name (legacy flow): ruben, david, sandra, emanuel, ze, joana")
    parser.add_argument("--date", required=True, help="Date in YYYY-MM-DD format")
    parser.add_argument("--total-calls", type=int, required=True)
    parser.add_argument("--completed-calls", type=int, required=True)
    parser.add_argument("--analysis-path", help="Absolute path to analysis.json (overrides --agent lookup)")
    parser.add_argument("--output-path", help="Absolute path for the PDF output (overrides --agent lookup)")
    parser.add_argument("--agent-display", help="Override report header (e.g. 'Rúben @ NOVA Digital')")
    parser.add_argument("--agent-name", help="Override agent name used in filename + section titles")
    args = parser.parse_args()

    report_date = datetime.strptime(args.date, "%Y-%m-%d")
    date_label = report_date.strftime("%d-%m-%Y")

    # Resolve analysis path + display fields, with --agent providing defaults
    agent_cfg = None
    if args.agent:
        agent_cfg = next((a for a in AGENTS if a["name"] == args.agent), None)
        if not agent_cfg:
            print(f"Error: unknown agent '{args.agent}'. Available: {[a['name'] for a in AGENTS]}")
            sys.exit(1)

    if args.analysis_path:
        analysis_path = Path(args.analysis_path)
    elif agent_cfg:
        analysis_path = agent_cfg["data_dir"] / date_label / "analysis.json"
    else:
        print("Error: must pass --agent or --analysis-path")
        sys.exit(1)

    if not analysis_path.exists():
        print(f"Error: analysis.json not found at {analysis_path}")
        sys.exit(1)

    agent_display = args.agent_display or (agent_cfg["report_display"] if agent_cfg else "")
    agent_name = args.agent_name or (agent_cfg["display"] if agent_cfg else "")
    reports_dir = agent_cfg["report_dir"] if agent_cfg else (Path(args.output_path).parent if args.output_path else Path("."))

    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
    generate_pdf(
        analysis,
        report_date,
        args.total_calls,
        args.completed_calls,
        agent_display=agent_display,
        reports_dir=reports_dir,
        agent_name=agent_name,
        output_path=args.output_path,
    )


if __name__ == "__main__":
    main()

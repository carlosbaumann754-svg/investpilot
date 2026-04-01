"""
InvestPilot - PDF Report Generator
Erstellt professionelle PDF-Berichte aus Weekly Report Daten.
Speichert im Bericht/-Ordner und stellt ueber API bereit.
"""

import logging
from pathlib import Path
from datetime import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm, cm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT

from app.config_manager import get_data_path

log = logging.getLogger("ReportPDF")

# Farben im InvestPilot Stil
DARK_BG = colors.HexColor("#1a1d2e")
CARD_BG = colors.HexColor("#252839")
TEXT_PRIMARY = colors.HexColor("#e2e8f0")
TEXT_SECONDARY = colors.HexColor("#94a3b8")
ACCENT_BLUE = colors.HexColor("#60a5fa")
ACCENT_GREEN = colors.HexColor("#10b981")
ACCENT_RED = colors.HexColor("#ef4444")
ACCENT_YELLOW = colors.HexColor("#f59e0b")
WHITE = colors.white
BLACK = colors.black


def _get_styles():
    """Erstelle PDF-Styles."""
    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        "Title_IP",
        parent=styles["Title"],
        fontSize=22,
        textColor=ACCENT_BLUE,
        spaceAfter=6,
        alignment=TA_CENTER,
    ))
    styles.add(ParagraphStyle(
        "Subtitle_IP",
        parent=styles["Normal"],
        fontSize=11,
        textColor=TEXT_SECONDARY,
        alignment=TA_CENTER,
        spaceAfter=20,
    ))
    styles.add(ParagraphStyle(
        "Section_IP",
        parent=styles["Heading2"],
        fontSize=14,
        textColor=ACCENT_BLUE,
        spaceBefore=16,
        spaceAfter=8,
        borderWidth=0,
        borderPadding=0,
    ))
    styles.add(ParagraphStyle(
        "Body_IP",
        parent=styles["Normal"],
        fontSize=10,
        textColor=BLACK,
        spaceAfter=4,
        leading=14,
    ))
    styles.add(ParagraphStyle(
        "Small_IP",
        parent=styles["Normal"],
        fontSize=8,
        textColor=TEXT_SECONDARY,
        spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        "Good_IP",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#059669"),
        spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        "Bad_IP",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#dc2626"),
        spaceAfter=3,
    ))
    styles.add(ParagraphStyle(
        "Warn_IP",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#d97706"),
        spaceAfter=3,
    ))
    return styles


def _make_kpi_table(perf):
    """Erstelle KPI-Uebersicht als Tabelle."""
    return_pct = perf.get("total_return_pct", 0)
    return_color = ACCENT_GREEN if return_pct >= 0 else ACCENT_RED

    data = [
        ["Gesamt-Rendite", "Win Rate", "Sharpe Ratio", "Marktregime"],
        [
            f"{return_pct:+.2f}%",
            f"{perf.get('win_rate', 0):.1f}%",
            f"{perf.get('sharpe_estimate', 0):.2f}",
            perf.get("market_regime", "?").upper(),
        ],
        [
            f"${perf.get('total_return_usd', 0):+,.2f}",
            f"{perf.get('win_days', 0)}/{perf.get('win_days', 0) + perf.get('lose_days', 0)} Tage",
            "",
            f"{perf.get('total_runs', 0)} Zyklen",
        ],
    ]

    t = Table(data, colWidths=[120, 100, 100, 120])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 1), (-1, 1), 16),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 1), (0, 1), return_color),
        ("FONTSIZE", (0, 2), (-1, 2), 8),
        ("TEXTCOLOR", (0, 2), (-1, 2), TEXT_SECONDARY),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#4b5563")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    return t


def _make_trades_table(trades):
    """Erstelle Trade-Uebersicht."""
    data = [
        ["Kennzahl", "Wert"],
        ["Trades gesamt", str(trades.get("total_trades", 0))],
        ["Kaeufe", str(trades.get("buys", 0))],
        ["Verkaeufe", str(trades.get("sells", 0))],
        ["Scanner-Trades", str(trades.get("scanner_trades", 0))],
        ["Stop-Loss Ausloeser", str(trades.get("sl_closes", 0))],
        ["Take-Profit Ausloeser", str(trades.get("tp_closes", 0))],
        ["Handelsvolumen", f"${trades.get('total_volume_usd', 0):,.0f}"],
    ]

    t = Table(data, colWidths=[200, 160])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, colors.HexColor("#f9fafb")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _make_top_assets_table(trades):
    """Erstelle Top-Assets Tabelle."""
    top = trades.get("top_symbols", [])
    if not top:
        return None

    data = [["Symbol", "Trades"]]
    for sym, count in top[:10]:
        data.append([sym, str(count)])

    t = Table(data, colWidths=[200, 100])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, colors.HexColor("#f9fafb")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))
    return t


def _make_suggestions_table(suggestions):
    """Erstelle Verbesserungsvorschlaege-Tabelle."""
    if not suggestions:
        return None

    data = [["Prio", "Bereich", "Vorschlag", "Aktion"]]
    for s in suggestions:
        data.append([
            s.get("prioritaet", ""),
            s.get("bereich", ""),
            s.get("vorschlag", ""),
            s.get("aktion", ""),
        ])

    t = Table(data, colWidths=[40, 60, 220, 150])
    prio_colors = {"HOCH": ACCENT_RED, "MITTEL": ACCENT_YELLOW, "NIEDRIG": ACCENT_GREEN}

    style_cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
        ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, colors.HexColor("#f9fafb")]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
    ]

    # Prio-Zellen einfaerben
    for i, s in enumerate(suggestions, start=1):
        color = prio_colors.get(s.get("prioritaet", ""), BLACK)
        style_cmds.append(("TEXTCOLOR", (0, i), (0, i), color))
        style_cmds.append(("FONTNAME", (0, i), (0, i), "Helvetica-Bold"))

    t.setStyle(TableStyle(style_cmds))
    return t


def generate_pdf(report, output_dir=None):
    """Erstelle PDF aus Report-Daten.

    Args:
        report: Dictionary mit Report-Daten aus generate_weekly_report()
        output_dir: Zielordner. Default: Bericht/ im Projektverzeichnis

    Returns:
        Path zum erstellten PDF
    """
    if output_dir is None:
        # Standard: Bericht/ Ordner im Projektverzeichnis
        output_dir = Path(__file__).parent.parent / "Bericht"

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Dateiname mit Datum und KW
    now = datetime.now()
    kw = now.isocalendar()[1]
    filename = f"InvestPilot_Report_KW{kw:02d}_{now.strftime('%Y-%m-%d')}.pdf"
    filepath = output_dir / filename

    styles = _get_styles()
    elements = []

    # --- Header ---
    elements.append(Paragraph("InvestPilot", styles["Title_IP"]))
    elements.append(Paragraph("Weekly Performance Report", styles["Subtitle_IP"]))
    elements.append(Paragraph(
        f"Kalenderwoche {kw} | {report.get('week_ending', now.strftime('%d.%m.%Y'))}",
        styles["Subtitle_IP"]
    ))
    elements.append(HRFlowable(width="100%", thickness=1, color=ACCENT_BLUE))
    elements.append(Spacer(1, 12))

    # --- Performance KPIs ---
    perf = report.get("performance", {})
    elements.append(Paragraph("Performance-Uebersicht", styles["Section_IP"]))
    elements.append(_make_kpi_table(perf))
    elements.append(Spacer(1, 6))

    # Zusaetzliche Performance-Details
    elements.append(Paragraph(
        f"Max. Tagesgewinn: {perf.get('max_daily_gain', 0):+.2f}% | "
        f"Max. Tagesverlust: {perf.get('max_daily_loss', 0):+.2f}% | "
        f"Volatilitaet: {perf.get('daily_volatility', 0):.2f}%",
        styles["Small_IP"]
    ))
    elements.append(Spacer(1, 8))

    # --- Trading Aktivitaet ---
    trades = report.get("weekly_trades", {})
    elements.append(Paragraph("Trading Aktivitaet (diese Woche)", styles["Section_IP"]))
    elements.append(_make_trades_table(trades))
    elements.append(Spacer(1, 8))

    # --- Top Assets ---
    top_table = _make_top_assets_table(trades)
    if top_table:
        elements.append(Paragraph("Meistgehandelte Assets", styles["Section_IP"]))
        elements.append(top_table)
        elements.append(Spacer(1, 8))

    # Asset-Klassen Verteilung
    classes = trades.get("asset_class_breakdown", {})
    if classes:
        class_text = " | ".join(f"{cls}: {count}" for cls, count in classes.items())
        elements.append(Paragraph(f"Asset-Klassen: {class_text}", styles["Body_IP"]))
        elements.append(Spacer(1, 8))

    # --- Brain & Learnings ---
    elements.append(Paragraph("Brain & Learnings", styles["Section_IP"]))

    strengths = report.get("brain_strengths", [])
    issues = report.get("brain_issues", [])

    if strengths:
        elements.append(Paragraph("Staerken:", styles["Body_IP"]))
        for s in strengths:
            elements.append(Paragraph(f"  + {s}", styles["Good_IP"]))

    if issues:
        elements.append(Paragraph("Verbesserungsbedarf:", styles["Body_IP"]))
        for i in issues:
            elements.append(Paragraph(f"  ! {i}", styles["Warn_IP"]))

    elements.append(Paragraph(
        f"Brain-Zyklen: {perf.get('total_runs', 0)} | "
        f"Gelernte Regeln: {perf.get('active_rules', 0)} | "
        f"Optimierungen: {perf.get('optimization_count', 0)}",
        styles["Small_IP"]
    ))
    elements.append(Spacer(1, 8))

    # --- Technischer Status ---
    elements.append(Paragraph("Technischer Status", styles["Section_IP"]))

    tech_ok = report.get("tech_ok", [])
    tech_warn = report.get("tech_warnings", [])

    for c in tech_ok:
        elements.append(Paragraph(f"  OK: {c}", styles["Good_IP"]))
    for w in tech_warn:
        elements.append(Paragraph(f"  WARNUNG: {w}", styles["Bad_IP"]))

    if not tech_warn:
        elements.append(Paragraph("  Keine Warnungen - System laeuft stabil", styles["Good_IP"]))
    elements.append(Spacer(1, 8))

    # --- Verbesserungsvorschlaege ---
    suggestions = report.get("suggestions", [])
    elements.append(Paragraph("Verbesserungsvorschlaege", styles["Section_IP"]))

    if suggestions:
        suggestion_table = _make_suggestions_table(suggestions)
        if suggestion_table:
            elements.append(suggestion_table)
    else:
        elements.append(Paragraph(
            "Keine Verbesserungen noetig - System laeuft rund!",
            styles["Good_IP"]
        ))
    elements.append(Spacer(1, 12))

    # --- Discovery Ergebnisse (falls vorhanden) ---
    from app.config_manager import load_json
    discovery = load_json("discovery_result.json")
    if discovery and discovery.get("new_found", 0) > 0:
        elements.append(Paragraph("Asset Discovery (diese Woche)", styles["Section_IP"]))
        elements.append(Paragraph(
            f"Neue Assets gefunden: {discovery['new_found']} | "
            f"Bewertet: {discovery['evaluated']} | "
            f"Zum Scanner hinzugefuegt: {discovery['added']}",
            styles["Body_IP"]
        ))

        top_10 = discovery.get("top_10", [])
        if top_10:
            disc_data = [["Symbol", "Name", "Klasse", "Score"]]
            for a in top_10:
                disc_data.append([
                    a.get("symbol", ""),
                    a.get("name", ""),
                    a.get("class", ""),
                    f"{a.get('score', 0):.1f}",
                ])
            disc_table = Table(disc_data, colWidths=[80, 180, 80, 60])
            disc_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#374151")),
                ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, colors.HexColor("#f9fafb")]),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            elements.append(disc_table)

        elements.append(Spacer(1, 8))

    # --- Footer ---
    elements.append(HRFlowable(width="100%", thickness=0.5, color=TEXT_SECONDARY))
    elements.append(Spacer(1, 6))
    elements.append(Paragraph(
        f"InvestPilot Weekly Report | Generiert: {now.strftime('%d.%m.%Y %H:%M')} | "
        f"Naechster Bericht: naechsten Freitag 18:00",
        styles["Small_IP"]
    ))

    # --- PDF erstellen ---
    doc = SimpleDocTemplate(
        str(filepath),
        pagesize=A4,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        rightMargin=1.5 * cm,
        title=f"InvestPilot Report KW{kw}",
        author="InvestPilot Trading Bot",
    )
    doc.build(elements)

    log.info(f"PDF Report erstellt: {filepath}")
    return filepath

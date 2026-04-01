"""
InvestPilot - Weekly Report Generator
Erstellt jeden Freitag Abend einen vollstaendigen Bericht:
- Trading Performance (Gewinne, Verluste, Win Rate)
- Portfolio-Zusammensetzung und Diversifikation
- Brain-Learnings und Strategie-Anpassungen
- Scanner-Effizienz (welche Signale waren gut/schlecht)
- Technischer Health-Check (Fehler, Uptime, API-Zuverlaessigkeit)
- Konkrete Verbesserungsvorschlaege
"""

import os
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.config_manager import load_json, load_config
from app.brain import load_brain, generate_performance_report
from app.persistence import backup_to_cloud

log = logging.getLogger("WeeklyReport")

SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ALERT_RECIPIENT = os.environ.get("ALERT_RECIPIENT", "")


def _get_weekly_trades():
    """Hole Trades der letzten 7 Tage."""
    history = load_json("trade_history.json") or []
    cutoff = (datetime.now() - timedelta(days=7)).isoformat()
    return [t for t in history if t.get("timestamp", "") >= cutoff]


def _analyze_trade_performance(trades):
    """Analysiere Trade-Performance der Woche."""
    if not trades:
        return {
            "total_trades": 0,
            "buys": 0, "sells": 0,
            "scanner_trades": 0,
            "sl_closes": 0, "tp_closes": 0,
            "total_volume_usd": 0,
            "top_symbols": [],
            "asset_class_breakdown": {},
        }

    buys = [t for t in trades if "BUY" in t.get("action", "")]
    sells = [t for t in trades if "SELL" in t.get("action", "") or "CLOSE" in t.get("action", "")]
    scanner = [t for t in trades if "SCANNER" in t.get("action", "")]
    sl = [t for t in trades if t.get("action") == "STOP_LOSS_CLOSE"]
    tp = [t for t in trades if t.get("action") == "TAKE_PROFIT_CLOSE"]

    volume = sum(t.get("amount_usd", 0) for t in trades)

    # Meistgehandelte Symbole
    symbol_counts = {}
    for t in trades:
        sym = t.get("symbol", "?")
        symbol_counts[sym] = symbol_counts.get(sym, 0) + 1
    top_symbols = sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # Asset-Klassen Breakdown
    class_counts = {}
    for t in trades:
        cls = t.get("asset_class", "unknown")
        class_counts[cls] = class_counts.get(cls, 0) + 1

    return {
        "total_trades": len(trades),
        "buys": len(buys),
        "sells": len(sells),
        "scanner_trades": len(scanner),
        "sl_closes": len(sl),
        "tp_closes": len(tp),
        "total_volume_usd": volume,
        "top_symbols": top_symbols,
        "asset_class_breakdown": class_counts,
    }


def _assess_brain_health(brain):
    """Bewerte Brain-Gesundheit und Lernfortschritt."""
    issues = []
    strengths = []

    total_runs = brain.get("total_runs", 0)
    if total_runs < 50:
        issues.append(f"Erst {total_runs} Zyklen - Brain braucht mehr Daten (min. 200 fuer zuverlaessige Muster)")
    elif total_runs > 500:
        strengths.append(f"{total_runs} Zyklen - Brain hat solide Datenbasis")

    win_rate = brain.get("win_rate", 0)
    if win_rate > 60:
        strengths.append(f"Win Rate {win_rate:.1f}% - ueberdurchschnittlich")
    elif win_rate < 40:
        issues.append(f"Win Rate nur {win_rate:.1f}% - Strategie-Anpassung noetig")

    regime = brain.get("market_regime", "unknown")
    if regime == "unknown":
        issues.append("Marktregime nicht erkannt - zu wenig Daten")
    else:
        strengths.append(f"Marktregime erkannt: {regime}")

    rules = brain.get("learned_rules", [])
    if len(rules) > 5:
        strengths.append(f"{len(rules)} gelernte Regeln aktiv")
    elif len(rules) == 0:
        issues.append("Noch keine Regeln gelernt - Brain braucht mehr Zyklen")

    sharpe = brain.get("sharpe_estimate", 0)
    if sharpe > 1.0:
        strengths.append(f"Sharpe Ratio {sharpe:.2f} - gutes Risiko/Rendite-Verhaeltnis")
    elif sharpe < 0:
        issues.append(f"Sharpe Ratio {sharpe:.2f} - negativ, Risiko ueberwiegt Rendite")

    return strengths, issues


def _tech_health_check():
    """Technischer Gesundheits-Check des Bots."""
    checks = []
    warnings = []

    # Config vorhanden?
    config = load_config()
    if config:
        checks.append("Config geladen")
    else:
        warnings.append("Config konnte nicht geladen werden")

    # Brain State vorhanden?
    brain = load_brain()
    if brain and brain.get("total_runs", 0) > 0:
        checks.append(f"Brain aktiv ({brain['total_runs']} Zyklen)")
    else:
        warnings.append("Brain leer oder nicht initialisiert")

    # Trade History vorhanden?
    history = load_json("trade_history.json")
    if history and len(history) > 0:
        checks.append(f"Trade History: {len(history)} Eintraege")
    else:
        warnings.append("Keine Trade History vorhanden")

    # Scanner State?
    scanner = load_json("scanner_state.json")
    if scanner:
        checks.append("Scanner State vorhanden")
        last_scan = scanner.get("last_scan", "")
        if last_scan:
            checks.append(f"Letzter Scan: {last_scan}")
    else:
        warnings.append("Kein Scanner State - Scanner noch nie gelaufen?")

    # Cloud Persistence?
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        checks.append("Cloud-Backup konfiguriert (GITHUB_TOKEN)")
    else:
        warnings.append("GITHUB_TOKEN fehlt - kein Cloud-Backup!")

    # eToro API Keys?
    if os.environ.get("ETORO_PUBLIC_KEY") and os.environ.get("ETORO_DEMO_PRIVATE_KEY"):
        checks.append("eToro API Keys konfiguriert")
    else:
        warnings.append("eToro API Keys fehlen")

    return checks, warnings


def _generate_improvement_suggestions(brain, trade_stats, tech_checks, tech_warnings):
    """Generiere konkrete Verbesserungsvorschlaege."""
    suggestions = []

    # Trading-basierte Vorschlaege
    if trade_stats["sl_closes"] > trade_stats["tp_closes"] * 2:
        suggestions.append({
            "bereich": "Trading",
            "prioritaet": "HOCH",
            "vorschlag": "Zu viele Stop-Loss Ausloeser vs. Take-Profit. Stop-Loss weiter setzen oder Take-Profit enger.",
            "aktion": "stop_loss_pct und take_profit_pct in config.json anpassen",
        })

    if trade_stats["total_trades"] == 0:
        suggestions.append({
            "bereich": "Trading",
            "prioritaet": "HOCH",
            "vorschlag": "Keine Trades diese Woche - pruefen ob Trading aktiviert ist und API funktioniert.",
            "aktion": "Dashboard pruefen, Logs checken",
        })

    if trade_stats["scanner_trades"] == 0 and trade_stats["total_trades"] > 0:
        suggestions.append({
            "bereich": "Scanner",
            "prioritaet": "MITTEL",
            "vorschlag": "Scanner hat keine Trades generiert. Min-Score evtl. zu hoch.",
            "aktion": "min_scanner_score in config.json senken (aktuell empfohlen: 12-15)",
        })

    # Brain-basierte Vorschlaege
    scores = brain.get("instrument_scores", {})
    if scores:
        worst = sorted(scores.items(), key=lambda x: x[1].get("score", 0))[:3]
        for iid, data in worst:
            if data.get("score", 0) < -10:
                suggestions.append({
                    "bereich": "Portfolio",
                    "prioritaet": "MITTEL",
                    "vorschlag": f"Instrument {iid} hat Score {data['score']:.1f} - dauerhaft schwach.",
                    "aktion": "Aus Portfolio-Targets entfernen oder Allokation reduzieren",
                })

    # Tech-basierte Vorschlaege
    if len(tech_warnings) > 2:
        suggestions.append({
            "bereich": "Technik",
            "prioritaet": "HOCH",
            "vorschlag": f"{len(tech_warnings)} technische Warnungen - System-Stabilitaet pruefen.",
            "aktion": "Warnings oben im Bericht pruefen und beheben",
        })

    # Diversifikation
    classes = trade_stats.get("asset_class_breakdown", {})
    if len(classes) <= 2 and trade_stats["total_trades"] > 10:
        suggestions.append({
            "bereich": "Diversifikation",
            "prioritaet": "MITTEL",
            "vorschlag": f"Nur {len(classes)} Asset-Klassen gehandelt. Mehr diversifizieren.",
            "aktion": "enabled_asset_classes in config.json erweitern",
        })

    return suggestions


def generate_weekly_report():
    """Erstelle den vollstaendigen Wochen-Bericht."""
    log.info("Generiere Weekly Report...")

    brain = load_brain()
    perf = generate_performance_report()
    weekly_trades = _get_weekly_trades()
    trade_stats = _analyze_trade_performance(weekly_trades)
    brain_strengths, brain_issues = _assess_brain_health(brain)
    tech_checks, tech_warnings = _tech_health_check()
    suggestions = _generate_improvement_suggestions(brain, trade_stats, tech_checks, tech_warnings)

    report = {
        "generated": datetime.now().isoformat(),
        "week_ending": datetime.now().strftime("%d.%m.%Y"),
        "performance": perf,
        "weekly_trades": trade_stats,
        "brain_strengths": brain_strengths,
        "brain_issues": brain_issues,
        "tech_ok": tech_checks,
        "tech_warnings": tech_warnings,
        "suggestions": suggestions,
    }

    log.info(f"Weekly Report generiert: {trade_stats['total_trades']} Trades, "
             f"{len(suggestions)} Verbesserungsvorschlaege")
    return report


def _render_html_report(report):
    """Erstelle HTML E-Mail aus Report-Daten."""
    perf = report["performance"]
    trades = report["weekly_trades"]
    suggestions = report["suggestions"]

    # Farbe basierend auf Performance
    return_pct = perf.get("total_return_pct", 0)
    color = "#10b981" if return_pct >= 0 else "#ef4444"

    # Top Symbole Tabelle
    top_rows = ""
    for sym, count in trades.get("top_symbols", []):
        top_rows += f"<tr><td style='padding:6px 12px;color:#e2e8f0;'>{sym}</td><td style='padding:6px 12px;color:#94a3b8;'>{count}x</td></tr>"

    # Asset-Klassen
    class_rows = ""
    for cls, count in trades.get("asset_class_breakdown", {}).items():
        class_rows += f"<tr><td style='padding:6px 12px;color:#e2e8f0;'>{cls}</td><td style='padding:6px 12px;color:#94a3b8;'>{count}</td></tr>"

    # Strengths
    strength_items = "".join(f"<li style='color:#10b981;margin:4px 0;'>{s}</li>" for s in report["brain_strengths"])
    issue_items = "".join(f"<li style='color:#f59e0b;margin:4px 0;'>{i}</li>" for i in report["brain_issues"])

    # Tech
    tech_ok_items = "".join(f"<li style='color:#10b981;margin:4px 0;'>{c}</li>" for c in report["tech_ok"])
    tech_warn_items = "".join(f"<li style='color:#ef4444;margin:4px 0;'>{w}</li>" for w in report["tech_warnings"])

    # Suggestions
    suggestion_rows = ""
    for s in suggestions:
        prio_color = {"HOCH": "#ef4444", "MITTEL": "#f59e0b", "NIEDRIG": "#10b981"}.get(s["prioritaet"], "#94a3b8")
        suggestion_rows += f"""
        <tr>
            <td style='padding:8px 12px;'><span style='color:{prio_color};font-weight:bold;'>{s['prioritaet']}</span></td>
            <td style='padding:8px 12px;color:#94a3b8;'>{s['bereich']}</td>
            <td style='padding:8px 12px;color:#e2e8f0;'>{s['vorschlag']}</td>
            <td style='padding:8px 12px;color:#60a5fa;font-size:12px;'>{s['aktion']}</td>
        </tr>"""

    html = f"""
    <html>
    <body style="font-family:'Segoe UI',sans-serif;background:#0f1117;color:#e2e8f0;padding:20px;margin:0;">
      <div style="max-width:700px;margin:0 auto;">

        <div style="text-align:center;padding:24px 0;border-bottom:1px solid #252839;">
          <h1 style="margin:0;color:#60a5fa;font-size:24px;">InvestPilot Weekly Report</h1>
          <p style="color:#94a3b8;margin:8px 0 0;">Woche bis {report['week_ending']}</p>
        </div>

        <!-- Performance Summary -->
        <div style="background:#252839;border-radius:12px;padding:24px;margin:20px 0;border-left:4px solid {color};">
          <h2 style="margin:0 0 16px;color:#e2e8f0;font-size:18px;">Performance</h2>
          <div style="display:flex;gap:20px;flex-wrap:wrap;">
            <div style="flex:1;min-width:120px;">
              <div style="color:#94a3b8;font-size:12px;">Gesamt-Rendite</div>
              <div style="color:{color};font-size:28px;font-weight:bold;">{return_pct:+.2f}%</div>
              <div style="color:#94a3b8;font-size:12px;">${perf.get('total_return_usd', 0):+,.2f}</div>
            </div>
            <div style="flex:1;min-width:120px;">
              <div style="color:#94a3b8;font-size:12px;">Win Rate</div>
              <div style="color:#e2e8f0;font-size:28px;font-weight:bold;">{perf.get('win_rate', 0):.1f}%</div>
            </div>
            <div style="flex:1;min-width:120px;">
              <div style="color:#94a3b8;font-size:12px;">Sharpe Ratio</div>
              <div style="color:#e2e8f0;font-size:28px;font-weight:bold;">{perf.get('sharpe_estimate', 0):.2f}</div>
            </div>
            <div style="flex:1;min-width:120px;">
              <div style="color:#94a3b8;font-size:12px;">Marktregime</div>
              <div style="color:#e2e8f0;font-size:28px;font-weight:bold;">{perf.get('market_regime', '?')}</div>
            </div>
          </div>
        </div>

        <!-- Weekly Trading Activity -->
        <div style="background:#252839;border-radius:12px;padding:24px;margin:20px 0;">
          <h2 style="margin:0 0 16px;color:#e2e8f0;font-size:18px;">Trading Aktivitaet (diese Woche)</h2>
          <table style="width:100%;border-collapse:collapse;">
            <tr><td style="padding:6px 0;color:#94a3b8;">Trades gesamt</td><td style="padding:6px 0;color:#e2e8f0;font-weight:bold;">{trades['total_trades']}</td></tr>
            <tr><td style="padding:6px 0;color:#94a3b8;">Kaeufe / Verkaeufe</td><td style="padding:6px 0;color:#e2e8f0;">{trades['buys']} / {trades['sells']}</td></tr>
            <tr><td style="padding:6px 0;color:#94a3b8;">Scanner-Trades</td><td style="padding:6px 0;color:#60a5fa;">{trades['scanner_trades']}</td></tr>
            <tr><td style="padding:6px 0;color:#94a3b8;">Stop-Loss / Take-Profit</td><td style="padding:6px 0;color:#e2e8f0;">{trades['sl_closes']} / {trades['tp_closes']}</td></tr>
            <tr><td style="padding:6px 0;color:#94a3b8;">Handelsvolumen</td><td style="padding:6px 0;color:#e2e8f0;">${trades['total_volume_usd']:,.0f}</td></tr>
          </table>
        </div>

        <!-- Top Traded Symbols -->
        <div style="background:#252839;border-radius:12px;padding:24px;margin:20px 0;">
          <h2 style="margin:0 0 16px;color:#e2e8f0;font-size:18px;">Top Assets & Diversifikation</h2>
          <div style="display:flex;gap:20px;flex-wrap:wrap;">
            <div style="flex:1;min-width:200px;">
              <h3 style="color:#94a3b8;font-size:14px;margin:0 0 8px;">Meistgehandelt</h3>
              <table>{top_rows or '<tr><td style="color:#94a3b8;">Keine Trades</td></tr>'}</table>
            </div>
            <div style="flex:1;min-width:200px;">
              <h3 style="color:#94a3b8;font-size:14px;margin:0 0 8px;">Asset-Klassen</h3>
              <table>{class_rows or '<tr><td style="color:#94a3b8;">Keine Daten</td></tr>'}</table>
            </div>
          </div>
        </div>

        <!-- Brain Health -->
        <div style="background:#252839;border-radius:12px;padding:24px;margin:20px 0;">
          <h2 style="margin:0 0 16px;color:#e2e8f0;font-size:18px;">Brain & Learnings</h2>
          <div style="margin-bottom:12px;">
            <strong style="color:#10b981;">Staerken:</strong>
            <ul style="margin:4px 0;padding-left:20px;">{strength_items or '<li style="color:#94a3b8;">Noch keine Daten</li>'}</ul>
          </div>
          <div>
            <strong style="color:#f59e0b;">Verbesserungsbedarf:</strong>
            <ul style="margin:4px 0;padding-left:20px;">{issue_items or '<li style="color:#94a3b8;">Alles OK</li>'}</ul>
          </div>
          <p style="color:#94a3b8;font-size:12px;margin:12px 0 0;">
            Brain Zyklen: {perf.get('total_runs', 0)} |
            Gelernte Regeln: {perf.get('active_rules', 0)} |
            Optimierungen: {perf.get('optimization_count', 0)}
          </p>
        </div>

        <!-- Technical Health -->
        <div style="background:#252839;border-radius:12px;padding:24px;margin:20px 0;">
          <h2 style="margin:0 0 16px;color:#e2e8f0;font-size:18px;">Technischer Status</h2>
          <div style="margin-bottom:12px;">
            <strong style="color:#10b981;">OK:</strong>
            <ul style="margin:4px 0;padding-left:20px;">{tech_ok_items}</ul>
          </div>
          <div>
            <strong style="color:#ef4444;">Warnungen:</strong>
            <ul style="margin:4px 0;padding-left:20px;">{tech_warn_items or '<li style="color:#10b981;">Keine Warnungen</li>'}</ul>
          </div>
        </div>

        <!-- Improvement Suggestions -->
        <div style="background:#252839;border-radius:12px;padding:24px;margin:20px 0;border-left:4px solid #60a5fa;">
          <h2 style="margin:0 0 16px;color:#60a5fa;font-size:18px;">Verbesserungsvorschlaege</h2>
          {'<table style="width:100%;border-collapse:collapse;"><tr style="border-bottom:1px solid #374151;"><th style="padding:8px 12px;text-align:left;color:#94a3b8;font-size:12px;">Prio</th><th style="padding:8px 12px;text-align:left;color:#94a3b8;font-size:12px;">Bereich</th><th style="padding:8px 12px;text-align:left;color:#94a3b8;font-size:12px;">Vorschlag</th><th style="padding:8px 12px;text-align:left;color:#94a3b8;font-size:12px;">Aktion</th></tr>' + suggestion_rows + '</table>' if suggestions else '<p style="color:#10b981;">Keine Verbesserungen noetig - laeuft rund!</p>'}
        </div>

        <div style="text-align:center;padding:20px 0;border-top:1px solid #252839;">
          <p style="color:#94a3b8;font-size:12px;margin:0;">
            InvestPilot Weekly Report | Generiert: {datetime.now().strftime('%d.%m.%Y %H:%M')} |
            Naechster Bericht: naechsten Freitag
          </p>
        </div>
      </div>
    </body>
    </html>
    """
    return html


def send_weekly_report():
    """Generiere und sende den woechentlichen Bericht."""
    report = generate_weekly_report()
    html = _render_html_report(report)

    # Speichere Report als JSON
    from app.config_manager import save_json
    save_json("weekly_report.json", report)
    log.info("Weekly Report JSON gespeichert")

    # PDF erstellen und im Bericht/-Ordner ablegen
    pdf_path = None
    try:
        from app.report_pdf import generate_pdf
        pdf_path = generate_pdf(report)
        log.info(f"PDF Report erstellt: {pdf_path}")
    except Exception as e:
        log.error(f"PDF-Erstellung fehlgeschlagen: {e}")

    # Per E-Mail senden
    if not SMTP_EMAIL or not SMTP_PASSWORD or not ALERT_RECIPIENT:
        log.info("E-Mail nicht konfiguriert - Report nur lokal gespeichert und geloggt")
        log.info(f"  Performance: {report['performance'].get('total_return_pct', 0):+.2f}%")
        log.info(f"  Trades diese Woche: {report['weekly_trades']['total_trades']}")
        log.info(f"  Vorschlaege: {len(report['suggestions'])}")
        return report

    try:
        msg = MIMEMultipart()
        msg["From"] = SMTP_EMAIL
        msg["To"] = ALERT_RECIPIENT
        msg["Subject"] = (
            f"[InvestPilot] Weekly Report - "
            f"{report['performance'].get('total_return_pct', 0):+.2f}% - "
            f"KW {datetime.now().isocalendar()[1]}"
        )
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)

        log.info("Weekly Report per E-Mail gesendet!")
        return report

    except Exception as e:
        log.error(f"Weekly Report E-Mail fehlgeschlagen: {e}")
        return report


def is_friday_evening():
    """Pruefe ob es Freitag zwischen 18:00-18:05 ist (innerhalb eines 5-Min Zyklus)."""
    now = datetime.now()
    return now.weekday() == 4 and now.hour == 18 and now.minute < 5

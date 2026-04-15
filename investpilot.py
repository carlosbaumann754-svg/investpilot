"""
InvestPilot v2 - Taegliches Investment-Briefing mit eToro API
Liest Portfolio direkt aus eToro, generiert PDF, versendet per E-Mail.

Module:
  1. eToro Portfolio Live-Daten (Positionen, P/L, Cash)
  2. Marktindizes (S&P 500, NASDAQ, SMI, DAX, EURO STOXX 50)
  3. Kurs-Alerts (Schwellenwert-basiert)
  4. News zu Portfolio-Positionen
  5. Rebalancing-Check (Soll vs. Ist)

eToro API Docs: https://api-portal.etoro.com
"""

import json
import sys
import smtplib
import logging
import uuid
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Fehler: pip install requests")
    sys.exit(1)

try:
    import yfinance as yf
except ImportError:
    print("Fehler: pip install yfinance")
    sys.exit(1)

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import cm
    from reportlab.lib.colors import HexColor
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    )
    from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
except ImportError:
    print("Fehler: pip install reportlab")
    sys.exit(1)


# --- Pfade ---
SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_PATH = SCRIPT_DIR / "investpilot.log"
OUTPUT_DIR = SCRIPT_DIR / "briefings"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("InvestPilot")


def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    log.error(f"config.json nicht gefunden: {CONFIG_PATH}")
    sys.exit(1)


def fmt_chf(v):
    if v is None: return "CHF --"
    return f"CHF {v:,.2f}".replace(",", "'")

def fmt_pct(v):
    if v is None: return "--"
    return f"{'+' if v >= 0 else ''}{v:.2f}%"

def clr(v):
    if v is None: return "#94A3B8"
    return "#22C55E" if v >= 0 else "#EF4444"


# ============================================================
# eTORO API CLIENT (Public API - kein Login noetig)
# Docs: https://api-portal.etoro.com
# Base: https://public-api.etoro.com/api/v1
# Auth: x-api-key + x-user-key (kein Passwort!)
# ============================================================

class EtoroClient:
    """Client fuer die eToro Public API (key-basiert, kein Login)."""

    def __init__(self, config):
        etoro_cfg = config.get("etoro", {})
        self.base_url = etoro_cfg.get("base_url", "https://public-api.etoro.com/api/v1")
        self.public_key = etoro_cfg.get("public_key", "")
        self.private_key = etoro_cfg.get("private_key", "")
        self.username = etoro_cfg.get("username", "")
        self.env = etoro_cfg.get("environment", "real")

        if not self.public_key or not self.private_key:
            log.warning("eToro API Keys nicht konfiguriert!")
            self.configured = False
        else:
            self.configured = True

        # Wir testen beide Key-Zuordnungen (A und B)
        self._key_order = None  # wird beim ersten erfolgreichen Call gesetzt

    def _headers_a(self):
        """Variante A: x-api-key=public, x-user-key=private."""
        return {
            "x-api-key": self.public_key,
            "x-user-key": self.private_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _headers_b(self):
        """Variante B: x-api-key=private, x-user-key=public."""
        return {
            "x-api-key": self.private_key,
            "x-user-key": self.public_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _get(self, endpoint):
        """GET mit automatischer Key-Erkennung."""
        url = f"{self.base_url}{endpoint}"

        # Falls Key-Reihenfolge schon bekannt
        if self._key_order == "A":
            return self._try_get(url, self._headers_a())
        elif self._key_order == "B":
            return self._try_get(url, self._headers_b())

        # Erste Anfrage: beide Varianten testen
        log.info(f"  Teste Key-Variante A (public=x-api-key)...")
        result = self._try_get(url, self._headers_a())
        if result is not None:
            self._key_order = "A"
            log.info(f"  Key-Variante A funktioniert!")
            return result

        log.info(f"  Teste Key-Variante B (private=x-api-key)...")
        result = self._try_get(url, self._headers_b())
        if result is not None:
            self._key_order = "B"
            log.info(f"  Key-Variante B funktioniert!")
            return result

        log.error(f"  Beide Key-Varianten fehlgeschlagen fuer {endpoint}")
        return None

    def _try_get(self, url, headers):
        """Einzelner GET-Versuch."""
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json() if resp.text else {}
            log.warning(f"  HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        except Exception as e:
            log.error(f"  Request Fehler: {e}")
            return None

    def get_pnl(self):
        """Portfolio P/L und Positionen."""
        return self._get(f"/trading/info/{self.env}/pnl")

    def get_equity(self):
        """Equity-Wert."""
        return self._get(f"/trading/info/{self.env}/equity")

    def get_available_cash(self):
        """Verfuegbares Cash."""
        return self._get(f"/trading/info/{self.env}/available-cash")

    def get_total_invested(self):
        """Total investiert."""
        return self._get(f"/trading/info/{self.env}/total-invested")

    def get_instruments(self, instrument_ids=None):
        """Instrument-Metadaten."""
        endpoint = "/instruments"
        if instrument_ids:
            ids_str = ",".join(str(i) for i in instrument_ids)
            endpoint += f"?InstrumentIds={ids_str}"
        return self._get(endpoint)

    def search_instruments(self, query):
        """Instrumente suchen."""
        return self._get(f"/instruments/search?query={query}")


# ============================================================
# MODUL 1: PORTFOLIO AUS eTORO
# ============================================================

def fetch_etoro_portfolio(client):
    """Hole echtes Portfolio direkt aus eToro (kein Login noetig)."""
    log.info("Modul 1: eToro Portfolio wird geladen...")

    empty = {
        "positions": [], "total_invested": 0, "total_current": 0,
        "total_pl": 0, "total_pl_pct": 0, "cash": 0, "source": "none"
    }

    if not client.configured:
        log.warning("eToro nicht konfiguriert - ueberspringe")
        return empty

    # PnL-Daten laden (testet automatisch beide Key-Varianten)
    log.info("  Lade eToro PnL-Daten...")
    pnl_data = client.get_pnl()
    cash_data = client.get_available_cash()
    invested_data = client.get_total_invested()

    # Debug
    log.info(f"  PnL Response: {str(pnl_data)[:300] if pnl_data else 'None'}")
    log.info(f"  Cash Response: {str(cash_data)[:200] if cash_data else 'None'}")
    log.info(f"  Invested Response: {str(invested_data)[:200] if invested_data else 'None'}")

    if not pnl_data:
        log.error("Konnte eToro P/L Daten nicht laden")
        empty["source"] = "etoro_error"
        return empty

    positions = []
    total_pl = 0

    # clientPortfolio-Wrapper entpacken falls vorhanden
    portfolio = pnl_data.get("clientPortfolio", pnl_data.get("ClientPortfolio", pnl_data))

    # Direkte Positionen
    for pos in portfolio.get("positions", portfolio.get("Positions", [])):
        pnl = pos.get("unrealizedPnL", pos.get("UnrealizedPnL", {}))
        pnl_val = pnl.get("pnL", pnl.get("PnL", 0)) if isinstance(pnl, dict) else 0
        total_pl += pnl_val

        invested = pos.get("investedAmount", pos.get("InvestedAmount", 0))
        current_val = invested + pnl_val
        pnl_pct = (pnl_val / invested * 100) if invested > 0 else 0

        positions.append({
            "symbol": str(pos.get("instrumentId", pos.get("InstrumentId", "?"))),
            "name": pos.get("instrumentName", pos.get("InstrumentName",
                    str(pos.get("instrumentId", pos.get("InstrumentId", "Unbekannt"))))),
            "shares": pos.get("units", pos.get("Units", 0)),
            "avg_price": pos.get("openRate", pos.get("OpenRate", 0)),
            "current_price": pos.get("currentRate", pos.get("CurrentRate", 0)),
            "invested": round(invested, 2),
            "current_value": round(current_val, 2),
            "pl": round(pnl_val, 2),
            "pl_pct": round(pnl_pct, 2),
            "daily_pct": None,
            "strategy": "core",
            "leverage": pos.get("leverage", pos.get("Leverage", 1)),
            "is_buy": pos.get("isBuy", pos.get("IsBuy", True)),
        })

    # Mirror/Copy-Positionen
    for mirror in portfolio.get("mirrors", portfolio.get("Mirrors", [])):
        closed_profit = mirror.get("closedPositionsNetProfit",
                        mirror.get("ClosedPositionsNetProfit", 0))
        total_pl += closed_profit
        for mpos in mirror.get("positions", mirror.get("Positions", [])):
            mpnl = mpos.get("unrealizedPnL", mpos.get("UnrealizedPnL", {}))
            mpnl_val = mpnl.get("pnL", mpnl.get("PnL", 0)) if isinstance(mpnl, dict) else 0
            total_pl += mpnl_val
            invested = mpos.get("investedAmount", mpos.get("InvestedAmount", 0))

            positions.append({
                "symbol": str(mpos.get("instrumentId", mpos.get("InstrumentId", "?"))),
                "name": f"[Copy] {mpos.get('instrumentName', mpos.get('InstrumentName', '?'))}",
                "shares": mpos.get("units", mpos.get("Units", 0)),
                "avg_price": mpos.get("openRate", mpos.get("OpenRate", 0)),
                "current_price": mpos.get("currentRate", mpos.get("CurrentRate", 0)),
                "invested": round(invested, 2),
                "current_value": round(invested + mpnl_val, 2),
                "pl": round(mpnl_val, 2),
                "pl_pct": round((mpnl_val / invested * 100) if invested > 0 else 0, 2),
                "daily_pct": None,
                "strategy": "tactical",
                "leverage": mpos.get("leverage", mpos.get("Leverage", 1)),
                "is_buy": mpos.get("isBuy", mpos.get("IsBuy", True)),
            })

    # Gesamtwerte
    total_invested = 0
    if invested_data:
        total_invested = invested_data.get("totalInvested",
                         invested_data.get("TotalInvested",
                         invested_data if isinstance(invested_data, (int, float)) else 0))
    if not total_invested:
        total_invested = sum(p["invested"] for p in positions)

    total_current = total_invested + total_pl

    cash = 0
    if cash_data:
        cash = cash_data.get("availableCash",
               cash_data.get("AvailableCash",
               cash_data if isinstance(cash_data, (int, float)) else 0))
    # Fallback: Credit aus PnL-Response
    if not cash and pnl_data:
        cp = pnl_data.get("clientPortfolio", pnl_data.get("ClientPortfolio", {}))
        if isinstance(cp, dict):
            cash = cp.get("credit", cp.get("Credit", 0))

    total_pl_pct = (total_pl / total_invested * 100) if total_invested > 0 else 0

    log.info(f"  eToro: {len(positions)} Positionen, P/L: {fmt_chf(total_pl)} ({fmt_pct(total_pl_pct)})")
    log.info(f"  Cash: {fmt_chf(cash)}, Investiert: {fmt_chf(total_invested)}")

    return {
        "positions": positions,
        "total_invested": round(total_invested, 2),
        "total_current": round(total_current, 2),
        "total_pl": round(total_pl, 2),
        "total_pl_pct": round(total_pl_pct, 2),
        "cash": round(cash, 2),
        "source": "etoro"
    }


# ============================================================
# MODUL 2: MARKTINDIZES (via Yahoo Finance)
# ============================================================

def fetch_indices(config):
    log.info("Modul 2: Marktindizes...")
    indices = config.get("market_indices", [])
    results = []
    for idx in indices:
        try:
            h = yf.Ticker(idx["symbol"]).history(period="5d")
            if h.empty: continue
            cur = h["Close"].iloc[-1]
            prev = h["Close"].iloc[-2] if len(h) >= 2 else cur
            wk = h["Close"].iloc[0] if len(h) >= 5 else prev
            results.append({
                "name": idx["name"], "price": round(cur, 2),
                "daily_pct": round((cur - prev) / prev * 100, 2),
                "weekly_pct": round((cur - wk) / wk * 100, 2)
            })
        except Exception as e:
            log.error(f"Index {idx['name']}: {e}")
    log.info(f"  {len(results)} Indizes geladen")
    return results


# ============================================================
# MODUL 3: KURS-ALERTS
# ============================================================

def check_alerts(perf, config):
    log.info("Modul 3: Kurs-Alerts...")
    ac = config.get("alerts", {})
    alerts = []

    for p in perf.get("positions", []):
        pl_pct = p.get("pl_pct", 0)
        if pl_pct is None: continue

        if pl_pct <= ac.get("daily_loss_alert_percent", -2.0):
            alerts.append({"type": "WARNUNG", "sev": "high", "sym": p["symbol"],
                           "name": p["name"], "msg": f"Position im Minus: {fmt_pct(pl_pct)}",
                           "action": "Stop-Loss pruefen"})
        elif pl_pct >= ac.get("daily_gain_alert_percent", 5.0):
            alerts.append({"type": "CHANCE", "sev": "med", "sym": p["symbol"],
                           "name": p["name"], "msg": f"Position im Plus: {fmt_pct(pl_pct)}",
                           "action": "Teilgewinnmitnahme pruefen"})

    if perf.get("total_pl_pct", 0) <= -5:
        alerts.append({"type": "WARNUNG", "sev": "high", "sym": "GESAMT",
                       "name": "Portfolio", "msg": f"Portfolio: {fmt_pct(perf['total_pl_pct'])}",
                       "action": "Strategie-Review empfohlen"})

    log.info(f"  {len(alerts)} Alerts")
    return alerts


# ============================================================
# MODUL 4: NEWS (via Yahoo Finance)
# ============================================================

def fetch_news(perf):
    log.info("Modul 4: News...")
    all_news = []
    seen = set()

    known_tickers = ["AAPL", "MSFT", "NVDA", "NESN.SW", "ROG.SW", "NOVN.SW",
                     "GOOGL", "AMZN", "TSLA", "META"]

    for sym in known_tickers[:6]:
        try:
            news = yf.Ticker(sym).news
            if not news: continue
            for art in news[:2]:
                content = art.get("content", {})
                title = content.get("title", art.get("title", ""))
                if not title or title in seen: continue
                seen.add(title)
                provider = content.get("provider", {})
                src = provider.get("displayName", "") if isinstance(provider, dict) else ""
                pub = content.get("pubDate", "")
                if pub:
                    try:
                        pub = datetime.fromisoformat(pub.replace("Z", "+00:00")).strftime("%d.%m.%Y")
                    except Exception as e:
                        log.warning(f"News pubDate parse failed fuer {sym}: {e}")
                all_news.append({"title": title, "symbol": sym, "source": src, "date": pub})
        except Exception as e:
            log.warning(f"News fetch fehlgeschlagen fuer {sym}: {e}", exc_info=True)

    log.info(f"  {len(all_news)} Artikel")
    return all_news[:10]


# ============================================================
# MODUL 5: REBALANCING
# ============================================================

def check_rebalancing(perf, config):
    log.info("Modul 5: Rebalancing...")
    strategies = config.get("strategies", {})
    total = perf.get("total_current", 0) + perf.get("cash", 0)
    if total <= 0:
        return {"strategies": [], "needs_rebalancing": False, "total": 0, "budget": 1500}

    strat_vals = {}
    for p in perf.get("positions", []):
        s = p.get("strategy", "core")
        v = p.get("current_value") or p.get("invested", 0)
        strat_vals[s] = strat_vals.get(s, 0) + v

    results = []
    needs = False
    budget = config.get("monthly_budget_chf", 1500)

    for sid, sc in strategies.items():
        target = sc["target_allocation"]
        cur_val = strat_vals.get(sid, 0)
        cur_pct = (cur_val / total * 100) if total > 0 else 0
        dev = cur_pct - target
        if abs(dev) > 5: needs = True
        results.append({
            "name": sc["name"], "target": target, "current": round(cur_pct, 1),
            "deviation": round(dev, 1), "value": round(cur_val, 2),
            "monthly": round(budget * target / 100, 2),
            "status": "OK" if abs(dev) <= 5 else ("UEBER" if dev > 0 else "UNTER")
        })

    log.info(f"  Rebalancing noetig: {'JA' if needs else 'NEIN'}")
    return {"strategies": results, "needs_rebalancing": needs,
            "total": round(total, 2), "budget": budget}


# ============================================================
# PDF GENERIERUNG
# ============================================================

def build_pdf(perf, indices, alerts, news, rebalancing, config):
    log.info("PDF wird generiert...")

    filename = f"briefing_{datetime.now().strftime('%Y-%m-%d')}.pdf"
    filepath = OUTPUT_DIR / filename

    doc = SimpleDocTemplate(str(filepath), pagesize=A4,
        topMargin=1.5*cm, bottomMargin=1.5*cm, leftMargin=2*cm, rightMargin=2*cm)

    C_BG = HexColor("#0B1120")
    C_CARD = HexColor("#1E293B")
    C_BORDER = HexColor("#334155")
    C_TEXT = HexColor("#E2E8F0")
    C_MUTED = HexColor("#94A3B8")
    C_TEAL = HexColor("#0F766E")
    C_GREEN = HexColor("#22C55E")
    C_RED = HexColor("#EF4444")
    C_PURPLE = HexColor("#7C3AED")
    C_AMBER = HexColor("#F59E0B")
    C_BLUE = HexColor("#3B82F6")
    C_ORANGE = HexColor("#F97316")
    C_WHITE = HexColor("#FFFFFF")

    styles = getSampleStyleSheet()
    s_title = ParagraphStyle("T", parent=styles["Title"], fontSize=22, textColor=C_WHITE, fontName="Helvetica-Bold", alignment=TA_LEFT)
    s_sub = ParagraphStyle("S", parent=styles["Normal"], fontSize=11, textColor=C_MUTED, fontName="Helvetica", spaceAfter=12)
    s_h2 = ParagraphStyle("H", parent=styles["Heading2"], fontSize=14, textColor=C_TEAL, fontName="Helvetica-Bold", spaceBefore=16, spaceAfter=8)
    s_body = ParagraphStyle("B", parent=styles["Normal"], fontSize=10, textColor=C_TEXT, fontName="Helvetica", leading=14)
    s_small = ParagraphStyle("SM", parent=styles["Normal"], fontSize=8, textColor=C_MUTED, fontName="Helvetica")
    s_alert_h = ParagraphStyle("AH", parent=s_body, textColor=C_RED, fontName="Helvetica-Bold")
    s_alert_m = ParagraphStyle("AM", parent=s_body, textColor=C_AMBER, fontName="Helvetica-Bold")
    s_center = ParagraphStyle("C", parent=s_small, alignment=TA_CENTER)

    story = []
    now = datetime.now().strftime("%d.%m.%Y - %H:%M Uhr")
    source_label = "eToro Live" if perf.get("source") == "etoro" else "Offline"

    # HEADER
    story.append(Paragraph("InvestPilot Briefing", s_title))
    story.append(Paragraph(f"{now}  |  {source_label}  |  CHF 1'500/Mt", s_sub))
    story.append(HRFlowable(width="100%", thickness=1, color=C_TEAL, spaceAfter=12))

    # PORTFOLIO UEBERSICHT
    story.append(Paragraph("Portfolio-Uebersicht (eToro)", s_h2))
    tbl_style = TableStyle([
        ("BACKGROUND", (0,0), (-1,0), C_TEAL), ("TEXTCOLOR", (0,0), (-1,0), C_WHITE),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 10),
        ("TEXTCOLOR", (0,1), (-1,-1), C_TEXT), ("BACKGROUND", (0,1), (-1,-1), C_CARD),
        ("GRID", (0,0), (-1,-1), 0.5, C_BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 6), ("BOTTOMPADDING", (0,0), (-1,-1), 6),
        ("LEFTPADDING", (0,0), (-1,-1), 10), ("ALIGN", (1,0), (1,-1), "RIGHT"),
    ])
    overview = [["Kennzahl", "Wert"],
                ["Investiert", fmt_chf(perf["total_invested"])],
                ["Aktueller Wert", fmt_chf(perf["total_current"])],
                ["Gewinn / Verlust", f"{fmt_chf(perf['total_pl'])}  ({fmt_pct(perf['total_pl_pct'])})"],
                ["Cash verfuegbar", fmt_chf(perf["cash"])],
                ["Gesamtwert (inkl. Cash)", fmt_chf(perf["total_current"] + perf["cash"])]]
    t = Table(overview, colWidths=[200, 280])
    t.setStyle(tbl_style)
    story.append(t)
    story.append(Spacer(1, 8))

    # POSITIONEN
    if perf["positions"]:
        story.append(Paragraph("Positionen", s_h2))
        pos_tbl = [["Instrument", "Invest.", "Aktuell", "P/L", "P/L %", "Hebel"]]
        for p in perf["positions"]:
            pos_tbl.append([
                p["name"][:25], fmt_chf(p["invested"]), fmt_chf(p["current_value"]),
                fmt_chf(p["pl"]), fmt_pct(p["pl_pct"]),
                f"{p.get('leverage', 1)}x"
            ])
        t = Table(pos_tbl, colWidths=[130, 70, 70, 70, 55, 40])
        cmds = [
            ("BACKGROUND", (0,0), (-1,0), C_PURPLE), ("TEXTCOLOR", (0,0), (-1,0), C_WHITE),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 8),
            ("TEXTCOLOR", (0,1), (-1,-1), C_TEXT), ("BACKGROUND", (0,1), (-1,-1), C_CARD),
            ("GRID", (0,0), (-1,-1), 0.5, C_BORDER),
            ("TOPPADDING", (0,0), (-1,-1), 4), ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("LEFTPADDING", (0,0), (-1,-1), 5), ("ALIGN", (1,0), (-1,-1), "RIGHT"),
        ]
        for i, p in enumerate(perf["positions"], 1):
            c = C_GREEN if (p["pl"] or 0) >= 0 else C_RED
            cmds.append(("TEXTCOLOR", (3, i), (4, i), c))
        t.setStyle(TableStyle(cmds))
        story.append(t)
        story.append(Spacer(1, 8))

    # ALERTS
    story.append(Paragraph("Kurs-Alerts", s_h2))
    if alerts:
        for a in alerts:
            sty = s_alert_h if a["sev"] == "high" else s_alert_m if a["sev"] == "med" else s_body
            story.append(Paragraph(f"<b>{a['type']}</b> {a['sym']}: {a['msg']}", sty))
            story.append(Paragraph(f"  Empfehlung: {a['action']}", s_small))
            story.append(Spacer(1, 3))
    else:
        story.append(Paragraph("Keine auffaelligen Bewegungen.", s_body))

    # MARKTINDIZES
    story.append(Paragraph("Marktindizes", s_h2))
    if indices:
        idx_tbl = [["Index", "Stand", "Tag", "Woche"]]
        for ix in indices:
            idx_tbl.append([ix["name"], f"{ix['price']:,.0f}", fmt_pct(ix["daily_pct"]), fmt_pct(ix["weekly_pct"])])
        t = Table(idx_tbl, colWidths=[150, 100, 100, 100])
        cmds = [
            ("BACKGROUND", (0,0), (-1,0), C_BLUE), ("TEXTCOLOR", (0,0), (-1,0), C_WHITE),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 10),
            ("TEXTCOLOR", (0,1), (-1,-1), C_TEXT), ("BACKGROUND", (0,1), (-1,-1), C_CARD),
            ("GRID", (0,0), (-1,-1), 0.5, C_BORDER),
            ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING", (0,0), (-1,-1), 8), ("ALIGN", (1,0), (-1,-1), "RIGHT"),
        ]
        for i, ix in enumerate(indices, 1):
            cmds.append(("TEXTCOLOR", (2,i), (2,i), C_GREEN if ix["daily_pct"] >= 0 else C_RED))
            cmds.append(("TEXTCOLOR", (3,i), (3,i), C_GREEN if ix["weekly_pct"] >= 0 else C_RED))
        t.setStyle(TableStyle(cmds))
        story.append(t)
    story.append(Spacer(1, 8))

    # REBALANCING
    story.append(Paragraph("Rebalancing-Check", s_h2))
    if rebalancing["needs_rebalancing"]:
        story.append(Paragraph("HANDLUNGSBEDARF: Allokation weicht vom Ziel ab!", s_alert_h))
    else:
        story.append(Paragraph("Allokation im Zielbereich.", s_body))
    if rebalancing["strategies"]:
        reb_tbl = [["Strategie", "Soll", "Ist", "Abw.", "Monatl.", "Status"]]
        for s in rebalancing["strategies"]:
            reb_tbl.append([s["name"], f"{s['target']}%", f"{s['current']}%",
                            f"{s['deviation']:+.1f}%", fmt_chf(s["monthly"]), s["status"]])
        t = Table(reb_tbl, colWidths=[100, 55, 55, 55, 85, 80])
        cmds = [
            ("BACKGROUND", (0,0), (-1,0), C_ORANGE), ("TEXTCOLOR", (0,0), (-1,0), C_WHITE),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"), ("FONTSIZE", (0,0), (-1,-1), 9),
            ("TEXTCOLOR", (0,1), (-1,-1), C_TEXT), ("BACKGROUND", (0,1), (-1,-1), C_CARD),
            ("GRID", (0,0), (-1,-1), 0.5, C_BORDER),
            ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING", (0,0), (-1,-1), 6), ("ALIGN", (1,0), (-1,-1), "RIGHT"),
            ("ALIGN", (5,0), (5,-1), "CENTER"),
        ]
        for i, s in enumerate(rebalancing["strategies"], 1):
            cmds.append(("TEXTCOLOR", (5,i), (5,i), C_GREEN if s["status"] == "OK" else C_RED))
        t.setStyle(TableStyle(cmds))
        story.append(t)
    story.append(Spacer(1, 8))

    # NEWS
    if news:
        story.append(Paragraph("Relevante News", s_h2))
        for n in news[:8]:
            story.append(Paragraph(f"<b>{n['symbol']}</b>  {n['title']}", s_body))
            story.append(Paragraph(f"{n['source']}  |  {n['date']}", s_small))
            story.append(Spacer(1, 3))

    # FOOTER
    story.append(Spacer(1, 20))
    story.append(HRFlowable(width="100%", thickness=0.5, color=C_BORDER, spaceAfter=8))
    story.append(Paragraph(f"InvestPilot v2 | eToro API | {now}", s_center))
    story.append(Paragraph("Keine Anlageberatung. Alle Daten ohne Gewaehr.", s_center))

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, A4[0], A4[1], fill=True, stroke=False)
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    log.info(f"PDF: {filepath}")
    return filepath


# ============================================================
# E-MAIL
# ============================================================

def send_email(pdf_path, config):
    ec = config.get("email", {})
    if not ec.get("enabled"):
        log.info("E-Mail deaktiviert")
        return False
    sender = ec.get("sender_email", "")
    pw = ec.get("sender_password", "")
    to = ec.get("recipient_email", "")
    if "HIER_DEIN" in sender or "HIER_DEIN" in pw:
        log.warning("E-Mail nicht konfiguriert!")
        return False
    try:
        msg = MIMEMultipart()
        msg["Subject"] = f"InvestPilot Briefing - {datetime.now().strftime('%d.%m.%Y')}"
        msg["From"] = f"InvestPilot <{sender}>"
        msg["To"] = to
        msg.attach(MIMEText("Dein InvestPilot Briefing ist im Anhang.\n\nKeine Anlageberatung.", "plain", "utf-8"))
        with open(pdf_path, "rb") as f:
            att = MIMEApplication(f.read(), _subtype="pdf")
            att.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
            msg.attach(att)
        with smtplib.SMTP(ec.get("smtp_server", "smtp.gmail.com"), ec.get("smtp_port", 587)) as srv:
            srv.starttls()
            srv.login(sender, pw)
            srv.sendmail(sender, to, msg.as_string())
        log.info(f"E-Mail an {to}")
        return True
    except Exception as e:
        log.error(f"E-Mail Fehler: {e}")
        return False


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("=" * 55)
    log.info("InvestPilot v2 startet (eToro API)...")
    log.info("=" * 55)

    config = load_config()
    client = EtoroClient(config)

    perf = fetch_etoro_portfolio(client)
    indices = fetch_indices(config)
    alerts = check_alerts(perf, config)
    news = fetch_news(perf)
    rebalancing = check_rebalancing(perf, config)

    pdf = build_pdf(perf, indices, alerts, news, rebalancing, config)
    email = send_email(pdf, config)

    log.info("=" * 55)
    log.info("ZUSAMMENFASSUNG")
    log.info(f"  Quelle:      {perf.get('source', '?')}")
    log.info(f"  Positionen:  {len(perf['positions'])}")
    log.info(f"  Investiert:  {fmt_chf(perf['total_invested'])}")
    log.info(f"  P/L:         {fmt_chf(perf['total_pl'])} ({fmt_pct(perf['total_pl_pct'])})")
    log.info(f"  Cash:        {fmt_chf(perf['cash'])}")
    log.info(f"  Alerts:      {len(alerts)}")
    log.info(f"  Indizes:     {len(indices)}")
    log.info(f"  News:        {len(news)}")
    log.info(f"  Rebalancing: {'JA' if rebalancing['needs_rebalancing'] else 'NEIN'}")
    log.info(f"  PDF:         {pdf}")
    log.info(f"  E-Mail:      {'Ja' if email else 'Nein'}")
    log.info("=" * 55)


if __name__ == "__main__":
    main()

"""Fundamental-Signal-Stack — die 5 validierten Signale.

Berechnet pro Asset die 5 unkorrelierten Signale (point-in-time, aus EDGAR-
Fundamentaldaten + Preis). REINE Funktionen: bekommen edgar_rec + Preise, geben
Roh-Signalwerte. Die cross-sektionale Rangbildung + Kombination zum Auswahl-
Score macht app/signal_stack.py.

Die 5 Signale (jedes schwach IC 0.03-0.06, aber UNKORRELIERT -> Aggregat stark,
Medallion-Prinzip). Validierung siehe docs/UNIVERSE_CHANGE_PLAN.md (Sharpe 1.02
vs Benchmark 0.77 ueber 15J, positiv in beiden Halbperioden):
  value      = Eigenkapital / Market-Cap (Book-to-Market) — hoch = guenstig
  quality    = GrossProfit / Assets (Novy-Marx)           — hoch = profitabel
  reversal   = -(1-Monats-Rendite)                          — hoch = juengster Verlierer (bounce)
  lev        = Eigenkapital / Assets                        — hoch = finanziell stark
  earngrowth = YoY-Gewinnwachstum (geclippt)                — hoch = verbessernde Fundamentaldaten

WICHTIG: der alte TA-Composite (market_scanner.score_asset) wird hierdurch
ersetzt — er hatte IC ~0 und war kombiniert sogar kontraproduktiv (-0.88 Korr.
zu reversal, Alpha-Einbruch +4.33%->+0.92% beim Hinzufuegen).
"""
from __future__ import annotations

from typing import Optional

from app import edgar_client as ec

AUDIT_METADATA = {
    "purpose": "Fundamental-Signal-Stack: 5 validierte Signale (Value/Quality/Reversal/Lev/EarnGrowth) point-in-time aus EDGAR + Preis (ersetzt edgelosen TA-Score)",
    "config_section": "signal_stack",
    "state_files": [],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v38 (Fundamental-Signal-Stack)",
}

SIGNAL_NAMES = ["value", "quality", "reversal", "lev", "earngrowth"]

# YoY-Gewinnwachstum wird geclippt (Ausreisser/Mini-Basis daempfen).
_EARN_CLIP = 2.0


def value_signal(rec: dict, asof: str, market_cap: Optional[float]) -> Optional[float]:
    """Book-to-Market = Eigenkapital / Market-Cap. Hoch = guenstig (Value)."""
    if not market_cap or market_cap <= 0:
        return None
    eq = ec.latest_stock(rec.get("StockholdersEquity", []), asof)
    if eq is None:
        return None
    return eq / market_cap


def quality_signal(rec: dict, asof: str) -> Optional[float]:
    """Gross-Profitability = GrossProfit / Assets (Novy-Marx). Hoch = profitabel."""
    gp = ec.annual_latest(rec.get("GrossProfit", []), asof)
    a = ec.latest_stock(rec.get("Assets", []), asof)
    if gp is None or not a or a <= 0:
        return None
    return gp / a


def reversal_signal(price_now: Optional[float], price_ref: Optional[float]) -> Optional[float]:
    """Short-Term-Reversal = -(1-Monats-Rendite). Hoch = juengster Verlierer.

    ``price_ref`` = Schlusskurs ~21 Handelstage vor ``price_now``.
    """
    if not price_ref or price_ref <= 0 or price_now is None:
        return None
    return -(price_now / price_ref - 1.0)


def leverage_signal(rec: dict, asof: str) -> Optional[float]:
    """Eigenkapitalquote = Eigenkapital / Assets. Hoch = finanziell stark."""
    eq = ec.latest_stock(rec.get("StockholdersEquity", []), asof)
    a = ec.latest_stock(rec.get("Assets", []), asof)
    if eq is None or not a or a <= 0:
        return None
    return eq / a


def earngrowth_signal(rec: dict, asof: str) -> Optional[float]:
    """YoY-Gewinnwachstum (geclippt auf +-2.0). Hoch = verbessernde Gewinne.

    Nur bei positivem Vorjahresgewinn definiert (sonst ist die Wachstumsrate
    nicht sinnvoll interpretierbar).
    """
    now, prev = ec.annual_two(rec.get("NetIncomeLoss", []), asof)
    if now is None or prev is None or prev <= 0:
        return None
    g = (now - prev) / prev
    return max(-_EARN_CLIP, min(_EARN_CLIP, g))


def compute_signals(rec: dict, asof: str, price_now: Optional[float],
                    price_ref: Optional[float]) -> dict:
    """Alle 5 Roh-Signale fuer EIN Asset. None-Wert = Daten fehlen.

    ``rec``       : EDGAR-Record (aus edgar_client.load_facts()[symbol])
    ``asof``      : Stichtag "YYYY-MM-DD" (point-in-time)
    ``price_now`` : aktueller Schlusskurs
    ``price_ref`` : Schlusskurs ~21 Handelstage zuvor (fuer reversal)
    """
    shares = ec.shares_outstanding(rec, asof)
    market_cap = shares * price_now if (shares and price_now) else None
    return {
        "value": value_signal(rec, asof, market_cap),
        "quality": quality_signal(rec, asof),
        "reversal": reversal_signal(price_now, price_ref),
        "lev": leverage_signal(rec, asof),
        "earngrowth": earngrowth_signal(rec, asof),
    }

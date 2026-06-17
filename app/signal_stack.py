"""Signal-Stack — kombiniert die 5 Fundamental-Signale zum Auswahl-Score.

Cross-sektionale Rangbildung: pro Signal werden alle Universe-Assets in Perzentile
(0..1) gerankt, fehlende Werte neutral auf 0.5 gesetzt; der Composite-Score ist
der Mittelwert der 5 Perzentile (0..100). Hoeher = besser (kaufen).

Das ersetzt den edgelosen TA-Composite (market_scanner.score_asset). Begruendung:
der 5-Signal-Stack schlug den Equal-Weight-Benchmark risiko-adjustiert (Sharpe
1.02 vs 0.77 ueber 15J, positiv in beiden Halbperioden); der TA-Score hatte IC ~0
und war kombiniert kontraproduktiv. Siehe docs/UNIVERSE_CHANGE_PLAN.md.

Gleichgewichtung der 5 Signale ist BEWUSST (kein Optimieren der Gewichte ->
weniger Overfitting; die monotone Sharpe-Verbesserung beim Stapeln galt fuer
Gleichgewicht).
"""
from __future__ import annotations

from typing import Optional

from app import fundamental_signals as fsig
from app.fundamental_signals import SIGNAL_NAMES

AUDIT_METADATA = {
    "purpose": "Signal-Stack: kombiniert die 5 Fundamental-Signale cross-sektional zu einem Perzentil-Composite-Auswahl-Score (ersetzt market_scanner.score_asset)",
    "config_section": None,  # v37dr: equal-weighted, config-los
    "state_files": [],  # v37dr: reine Scoring-Logik, schreibt nichts (Runner schreibt signal_stack_shadow.json)
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v38 (Fundamental-Signal-Stack)",
}


def _avg_ranks(values: list) -> list:
    """Durchschnitts-Raenge (0-basiert) mit Tie-Handling."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def percentiles(values: list) -> list:
    """Liste (mit moeglichen None) -> Perzentile [0..1]. None -> 0.5 (neutral).

    Weniger als 2 gueltige Werte -> alle 0.5 (nicht rankbar).
    """
    valid_idx = [i for i, v in enumerate(values) if v is not None]
    if len(valid_idx) < 2:
        return [0.5] * len(values)
    valid_vals = [values[i] for i in valid_idx]
    ranks = _avg_ranks(valid_vals)
    n = len(valid_vals)
    pmap = {valid_idx[k]: ranks[k] / (n - 1) for k in range(n)}
    return [pmap.get(i, 0.5) for i in range(len(values))]


# v37dt Audit#4: Mindest-Coverage fuer Kaufbarkeit. Fehlende Roh-Signale werden
# in percentiles() neutral auf 0.5 gemappt — ein Symbol mit nur 1 vorhandenen
# (Top-)Signal + 4x neutral-0.5 kommt so auf Composite (1.0+0.5*4)/5 = 0.6 -> Score
# 60 -> BUY, OBWOHL fast keine Fundamentaldaten da sind (genau der edgelose
# Price-only-Fall, dem das Fundamental-Universum entkommen sollte). Ein Symbol mit
# < MIN_SIGNAL_COVERAGE vorhandenen Roh-Signalen wird daher als NICHT kaufbar
# markiert (eligible=False); Score bleibt fuer Transparenz/Shadow-Log erhalten.
MIN_SIGNAL_COVERAGE = 3


def compute_composite_scores(raw_by_symbol: dict) -> dict:
    """{symbol: {signal: raw_or_None}} -> {symbol: {score, coverage, eligible, percentiles}}.

    score = Mittelwert der 5 Signal-Perzentile * 100 (0..100, hoeher = besser).
    coverage = Anzahl tatsaechlich vorhandener (non-None) Roh-Signale (0..5).
    eligible = coverage >= MIN_SIGNAL_COVERAGE (sonst nicht kaufbar, s.o. Audit#4).
    """
    symbols = list(raw_by_symbol.keys())
    if not symbols:
        return {}
    pct_by_signal = {}
    for sig in SIGNAL_NAMES:
        vals = [raw_by_symbol[s].get(sig) for s in symbols]
        pct_by_signal[sig] = percentiles(vals)
    out = {}
    for j, s in enumerate(symbols):
        pcts = {sig: pct_by_signal[sig][j] for sig in SIGNAL_NAMES}
        score = sum(pcts.values()) / len(pcts) * 100.0
        coverage = sum(1 for sig in SIGNAL_NAMES
                       if raw_by_symbol[s].get(sig) is not None)
        out[s] = {
            "score": round(score, 2),
            "coverage": coverage,
            "eligible": coverage >= MIN_SIGNAL_COVERAGE,
            "percentiles": {k: round(v, 4) for k, v in pcts.items()},
        }
    return out


def score_universe(symbols: list, facts: dict, prices: dict, asof: str) -> dict:
    """Voll-Orchestrierung: Roh-Signale -> Composite-Scores fuer ein Universum.

    ``facts``  : {SYMBOL: edgar_rec}  (aus edgar_client.load_facts())
    ``prices`` : {symbol: (price_now, price_ref_21d)}
    ``asof``   : Stichtag "YYYY-MM-DD"

    Entkoppelt von yfinance (Preise werden hineingereicht) -> voll testbar.
    """
    raw = {}
    for s in symbols:
        rec = facts.get(s.upper(), {})
        price_now, price_ref = prices.get(s, (None, None))
        raw[s] = fsig.compute_signals(rec, asof, price_now, price_ref)
    return compute_composite_scores(raw)


def ranked_symbols(scores: dict, top_fraction: Optional[float] = None) -> list:
    """Symbole nach Score absteigend. Optional nur das Top-``top_fraction`` (0..1).

    v37dt Audit#4: nur kaufbare Symbole (eligible != False, score != None). Bei
    Altdaten ohne 'eligible'-Feld wird wie bisher True angenommen (rueckwaerts-kompat).
    """
    eligible = [s for s in scores
                if scores[s].get("eligible", True)
                and scores[s].get("score") is not None]
    ordered = sorted(eligible, key=lambda s: scores[s]["score"], reverse=True)
    if top_fraction is not None and 0 < top_fraction < 1:
        k = max(1, int(len(ordered) * top_fraction))
        return ordered[:k]
    return ordered

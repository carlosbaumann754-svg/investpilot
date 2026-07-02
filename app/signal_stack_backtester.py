"""Signal-Stack-Backtester (v37e+, Rekalibrierungs-Fundament).

Backtestet den AKTIVEN Fundamental-Signal-Stack-Motor (nicht den Alt-TA-Score):
monatliches Point-in-Time-Ranking aus EDGAR-Fakten (via signal_stack.score_universe)
-> Top-N halten -> Exit-Simulation (SL/Trailing/TP/Time-Stop) -> Portfolio-Kurve
+ IWM-Benchmark. Parametrierbar fuer SL-/Deployment-Sweeps ueber mehrere Regimes.

Zweck: (1) validiert die Post-Soak-Rekalibrierung (SL-Breite + Cash-Quote) motor-
korrekt statt auf Alt-TA-Daten; (2) Fundament fuer die WFO-Re-Baseline (Task #4).

RECYCELT aus backtester.py bewusst NICHTS Motor-Spezifisches (dessen _score_at_bar
ist Alt-TA). Preise via yfinance-Bulk; Facts via edgar_client.load_facts (Point-in-
Time ueber 'asof', filed<=asof — by-design im signal_stack).

CAVEATS (Lean-MVP, bewusst): (a) Survivorship — sp600-Liste = HEUTIGE Member ->
Vorwaerts-Bias (Symbole vor IPO fehlen einfach; keine delisteten dabei). (b) Exits
auf Tages-Close (kein Intraday-Fill). (c) Monats-Rebalance statt kontinuierlich.
Fuer die relative SL-/Deployment-Aussage robust; absolute Renditen leicht optimistisch.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

log = logging.getLogger("StackBacktester")

AUDIT_METADATA = {
    "purpose": "Signal-Stack-Backtester: monatliches Point-in-Time-Ranking (EDGAR) + Exit-/Deployment-Simulation + IWM-Benchmark. Validiert Rekalibrierung (SL/Cash-Quote) motor-korrekt; Fundament fuer WFO-Re-Baseline",
    "config_section": None,
    "state_files": [],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v37e+ (Post-Soak Rekalibrierungs-Fundament)",
}

_REF_OFFSET = 21          # Handelstage fuer das Reversal-Signal
_ROUND_TRIP_COST_PCT = 0.4  # Spread+Slippage+Fee je Trade (Ein+Aus), konservativ
_IWM = "IWM"


# ============================================================
# DATEN
# ============================================================
def load_price_history(symbols: list, start: str, end: str) -> dict:
    """{symbol: [(date, close), ...] aufsteigend} via yfinance-Bulk (auto-adjust)."""
    import yfinance as yf
    out = {}
    data = yf.download(symbols + [_IWM], start=start, end=end, interval="1d",
                       group_by="ticker", auto_adjust=True, threads=True, progress=False)
    multi = (len(symbols) + 1) > 1
    for s in symbols + [_IWM]:
        try:
            df = data[s] if multi else data
            ser = df["Close"].dropna()
            out[s] = [(d.date(), float(c)) for d, c in zip(ser.index, ser.values)]
        except Exception:
            continue
    return out


def _prices_asof(price_hist: dict, asof: date, ref_offset: int = _REF_OFFSET) -> dict:
    """{symbol: (price_now, price_ref)} fuer den Score-Zeitpunkt asof."""
    prices = {}
    for s, ser in price_hist.items():
        if s == _IWM:
            continue
        upto = [c for (d, c) in ser if d <= asof]
        if len(upto) >= ref_offset + 1:
            prices[s] = (upto[-1], upto[-1 - ref_offset])
    return prices


def _forward_closes(price_hist: dict, sym: str, entry_day: date, until_day: date) -> list:
    """Closes von >entry_day bis <=until_day (die Tage NACH dem Einstieg)."""
    return [c for (d, c) in price_hist.get(sym, []) if entry_day < d <= until_day]


def _month_starts(start: str, end: str) -> list:
    """Erster Kalendertag jedes Monats im Bereich (Rebalance-Termine)."""
    y, m, _ = (int(x) for x in start.split("-"))
    ey, em, _ = (int(x) for x in end.split("-"))
    out = []
    while (y, m) <= (ey, em):
        out.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1; y += 1
    return out


# ============================================================
# POSITION-SIMULATION (Exits)
# ============================================================
def _sim_position(entry: float, fwd: list, sl_pct, tp_pct, trail_act_pct,
                  trail_pct) -> tuple:
    """Simuliert eine Position ueber die Forward-Closes bis Rebalance/Exit.

    Prioritaet pro Tag: SL -> Trailing -> TP. Sonst Halten bis Rebalance
    (letzter fwd-Close). Liefert (return_pct, reason, days_held).
    """
    if not fwd:
        return 0.0, "flat", 0
    high = entry
    trail_armed = False
    for i, c in enumerate(fwd, 1):
        # Hard-SL
        if sl_pct is not None and c <= entry * (1 + sl_pct / 100.0):
            return float(sl_pct), "SL", i
        high = max(high, c)
        # Trailing (erst scharf ab activation ueber Entry)
        if trail_pct is not None and trail_act_pct is not None:
            if not trail_armed and c >= entry * (1 + trail_act_pct / 100.0):
                trail_armed = True
            if trail_armed and c <= high * (1 - trail_pct / 100.0):
                return (c / entry - 1) * 100, "TRAIL", i
        # Take-Profit
        if tp_pct is not None and c >= entry * (1 + tp_pct / 100.0):
            return float(tp_pct), "TP", i
    # Rebalance-Exit am letzten Close
    return (fwd[-1] / entry - 1) * 100, "REBAL", len(fwd)


# ============================================================
# PICK-VORBERECHNUNG (langsam, einmal pro Zeitraum -> Sweep wird schnell)
# ============================================================
def precompute_monthly_picks(price_hist: dict, facts: dict, start: str, end: str) -> dict:
    """{rebalance_date: [ranked eligible Symbole]} — die Selektion ist der teure
    Teil (EDGAR-Scoring je Monat). Einmal berechnen, dann beliebig viele
    SL-/Deployment-/top_n-Configs schnell simulieren."""
    from app import signal_stack
    picks = {}
    for rb in _month_starts(start, end):
        prices = _prices_asof(price_hist, rb)
        if not prices:
            picks[rb] = []; continue
        scores = signal_stack.score_universe(list(prices.keys()), facts, prices, rb.isoformat())
        picks[rb] = signal_stack.ranked_symbols(scores)   # volles Ranking, top_n erst in der Sim
    return picks


# ============================================================
# BACKTEST
# ============================================================
def run_backtest(price_hist: dict, facts: dict, symbols: list, start: str, end: str,
                 top_n: int = 15, deployment: float = 0.90,
                 sl_pct=-5, tp_pct=None, trail_act_pct=None, trail_pct=None,
                 cost_pct: float = _ROUND_TRIP_COST_PCT, picks_by_month: dict = None) -> dict:
    """Monatlich rebalancierter Top-N-Signal-Stack-Backtest mit Exit-Sim.

    deployment: Anteil des Equity, der pro Monat auf die Top-N verteilt wird
    (Rest = Cash, Rendite 0). Modelliert die Cash-Quote direkt.
    picks_by_month: vorberechnete Rankings (precompute_monthly_picks) -> ueberspringt
    das teure Scoring (fuer Sweeps). None -> wird inline berechnet.
    """
    from app import signal_stack
    rebals = _month_starts(start, end)
    equity = 1.0
    monthly = []       # Monats-Portfolio-Renditen
    trades = []        # einzelne Positions-Ergebnisse
    eq_curve = [(rebals[0], 1.0)]
    for i, rb in enumerate(rebals):
        nxt = rebals[i + 1] if i + 1 < len(rebals) else date.fromisoformat(end)
        prices = _prices_asof(price_hist, rb)
        if not prices:
            monthly.append(0.0); eq_curve.append((rb, equity)); continue
        if picks_by_month is not None:
            picks = picks_by_month.get(rb, [])[:top_n]
        else:
            scores = signal_stack.score_universe(list(prices.keys()), facts, prices, rb.isoformat())
            picks = signal_stack.ranked_symbols(scores)[:top_n]
        picks = [p for p in picks if p in prices]   # nur mit Preis as-of
        if not picks:
            monthly.append(0.0); eq_curve.append((rb, equity)); continue
        per_pos = deployment / len(picks)   # Gewicht je Position (vom Equity)
        port_ret = 0.0
        for sym in picks:
            entry = prices[sym][0]
            fwd = _forward_closes(price_hist, sym, rb, nxt)
            r, reason, days = _sim_position(entry, fwd, sl_pct, tp_pct, trail_act_pct, trail_pct)
            r_net = r - cost_pct           # Round-Trip-Kosten
            port_ret += per_pos * (r_net / 100.0)
            trades.append({"month": rb.isoformat(), "sym": sym, "ret": r,
                           "ret_net": r_net, "reason": reason, "days": days})
        equity *= (1 + port_ret)
        monthly.append(port_ret * 100)
        eq_curve.append((nxt, equity))
    return {
        "config": {"top_n": top_n, "deployment": deployment, "sl_pct": sl_pct,
                   "tp_pct": tp_pct, "trail_act_pct": trail_act_pct, "trail_pct": trail_pct},
        "monthly_pct": monthly, "trades": trades, "equity_final": equity,
        "eq_curve": eq_curve, "start": start, "end": end,
    }


# ============================================================
# METRIKEN
# ============================================================
def _metrics(monthly_pct: list, eq_final: float, trades: list) -> dict:
    import statistics
    n = len(monthly_pct)
    total_ret = (eq_final - 1) * 100
    mean = statistics.mean(monthly_pct) if monthly_pct else 0
    sd = statistics.pstdev(monthly_pct) if n > 1 else 0
    sharpe = (mean / sd * (12 ** 0.5)) if sd > 0 else 0
    # Max-Drawdown aus der kumulierten Monatskurve
    eq = 1.0; peak = 1.0; mdd = 0.0
    for m in monthly_pct:
        eq *= (1 + m / 100.0); peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    trs = [t["ret_net"] for t in trades]
    wins = sum(1 for r in trs if r > 0)
    reasons = {}
    for t in trades:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    return {
        "total_return_pct": round(total_ret, 2),
        "sharpe_ann": round(sharpe, 2),
        "max_drawdown_pct": round(mdd * 100, 2),
        "months": n,
        "trades": len(trades),
        "win_rate_pct": round(wins / len(trs) * 100, 1) if trs else 0,
        "avg_trade_pct": round(statistics.mean(trs), 2) if trs else 0,
        "exit_reasons": reasons,
    }


def benchmark_iwm(price_hist: dict, start: str, end: str) -> dict:
    """Buy&Hold IWM ueber den Zeitraum (Total-Return + Sharpe aus Monats-Returns)."""
    ser = price_hist.get(_IWM, [])
    if not ser:
        return {}
    s = date.fromisoformat(start); e = date.fromisoformat(end)
    win = [(d, c) for (d, c) in ser if s <= d <= e]
    if len(win) < 2:
        return {}
    total = (win[-1][1] / win[0][1] - 1) * 100
    # Monats-Returns fuer Sharpe
    rebals = _month_starts(start, end)
    mrets = []
    for i in range(len(rebals) - 1):
        a = [c for (d, c) in win if d <= rebals[i]]
        b = [c for (d, c) in win if d <= rebals[i + 1]]
        if a and b and a[-1] > 0:
            mrets.append((b[-1] / a[-1] - 1) * 100)
    import statistics
    sd = statistics.pstdev(mrets) if len(mrets) > 1 else 0
    sh = (statistics.mean(mrets) / sd * (12 ** 0.5)) if sd > 0 else 0
    return {"iwm_total_return_pct": round(total, 2), "iwm_sharpe_ann": round(sh, 2)}

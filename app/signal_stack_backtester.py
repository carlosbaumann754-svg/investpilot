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


def _forward_series(price_hist: dict, sym: str, entry_day: date, until_day: date) -> list:
    """Wie _forward_closes, aber MIT Datum: [(date, close), ...].

    Der Hold-Modus braucht den Ausstiegs-ZEITPUNKT, nicht nur die Rendite —
    sonst weiss er nicht, wann ein Depot-Slot wieder frei wird.
    """
    return [(d, c) for (d, c) in price_hist.get(sym, []) if entry_day < d <= until_day]


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
                  trail_pct, tranches=None) -> tuple:
    """Simuliert eine Position ueber die Forward-Closes bis Rebalance/Exit.

    Prioritaet pro Tag: SL -> Tranchen -> Trailing -> TP. Sonst Halten bis
    Rebalance (letzter fwd-Close). Liefert (return_pct, reason, days_held).

    R-B12 (20.07.2026): TRANCHEN-UNTERSTUETZUNG nachgeruestet.
    Der Live-Trader nimmt via leverage.tp_tranches Teilgewinne mit (aktuell
    30 %% bei +8, 30 %% bei +16, 40 %% bei +30) — der Backtester kannte das
    NICHT und hat die Live-Exits damit systematisch zu MILD modelliert. Der
    Exit-Sweep vom 20.07. lief ohne diesen Mechanismus; live feuerten seit
    Soak-Start 10 von 10 Partials bei +8 %% (keine einzige je bei +16 %%),
    d. h. die Tranche ist real der wirksamste Gewinner-Deckel.

    tranches: Liste von {"pct_of_position": p, "profit_target_pct": t}.
    Erreicht der Kurs entry*(1+t/100), wird p %% der URSPRUNGSposition zu
    diesem Kurs realisiert; der Rest laeuft unter denselben SL-/Trail-Regeln
    weiter. Rueckgabe ist der positionsgewichtete Gesamt-Return.

    tranches=None -> exakt das vorherige Verhalten (rueckwaertskompatibel).
    """
    if not fwd:
        return 0.0, "flat", 0

    # Tranchen aufsteigend nach Ziel; defensiv gegen kaputte Config-Eintraege
    pending: list = []
    for t in (tranches or []):
        try:
            frac = float(t.get("pct_of_position", 0)) / 100.0
            tgt = float(t.get("profit_target_pct"))
        except (TypeError, ValueError, AttributeError):
            continue
        if frac > 0:
            pending.append((tgt, frac))
    pending.sort(key=lambda x: x[0])

    remaining = 1.0     # noch offener Anteil der Position
    booked = 0.0        # bereits realisierter, anteilsgewichteter Return in %
    fired = False       # hat mind. eine Tranche gegriffen?
    high = entry
    trail_armed = False

    def _out(rest_ret, reason, day):
        return booked + remaining * rest_ret, (f"TRANCHE+{reason}" if fired else reason), day

    for i, c in enumerate(fwd, 1):
        # Hard-SL (trifft den verbleibenden Anteil)
        if sl_pct is not None and c <= entry * (1 + sl_pct / 100.0):
            return _out(float(sl_pct), "SL", i)
        high = max(high, c)
        # Tranchen: alle an diesem Tag erreichten Ziele abarbeiten
        while pending and remaining > 1e-9 and c >= entry * (1 + pending[0][0] / 100.0):
            tgt, frac = pending.pop(0)
            take = min(frac, remaining)
            booked += take * tgt
            remaining -= take
            fired = True
        if remaining <= 1e-9:
            return booked, "TRANCHE_ALL", i
        # Trailing (erst scharf ab activation ueber Entry)
        if trail_pct is not None and trail_act_pct is not None:
            if not trail_armed and c >= entry * (1 + trail_act_pct / 100.0):
                trail_armed = True
            if trail_armed and c <= high * (1 - trail_pct / 100.0):
                return _out((c / entry - 1) * 100, "TRAIL", i)
        # Take-Profit
        if tp_pct is not None and c >= entry * (1 + tp_pct / 100.0):
            return _out(float(tp_pct), "TP", i)
    # Rebalance-Exit am letzten Close
    return _out((fwd[-1] / entry - 1) * 100, "REBAL", len(fwd))


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
                 tranches=None,
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
            r, reason, days = _sim_position(entry, fwd, sl_pct, tp_pct, trail_act_pct,
                                            trail_pct, tranches)
            r_net = r - cost_pct           # Round-Trip-Kosten
            port_ret += per_pos * (r_net / 100.0)
            trades.append({"month": rb.isoformat(), "sym": sym, "ret": r,
                           "ret_net": r_net, "reason": reason, "days": days})
        equity *= (1 + port_ret)
        monthly.append(port_ret * 100)
        eq_curve.append((nxt, equity))
    return {
        "config": {"top_n": top_n, "deployment": deployment, "sl_pct": sl_pct,
                   "tp_pct": tp_pct, "trail_act_pct": trail_act_pct, "trail_pct": trail_pct,
                   "tranches": tranches},
        "monthly_pct": monthly, "trades": trades, "equity_final": equity,
        "eq_curve": eq_curve, "start": start, "end": end,
    }


def run_backtest_hold(price_hist: dict, facts: dict, symbols: list, start: str, end: str,
                      top_n: int = 15, deployment: float = 0.70,
                      sl_pct=-8, tp_pct=None, trail_act_pct=None, trail_pct=None,
                      tranches=None,
                      cost_pct: float = _ROUND_TRIP_COST_PCT,
                      picks_by_month: dict = None) -> dict:
    """R-B16 (21.07.2026) — simuliert das TATSAECHLICHE Verhalten des Live-Bots.

    UNTERSCHIED ZU run_backtest (und warum das gebraucht wird)
    ----------------------------------------------------------
    run_backtest verkauft JEDE Position am Monatsende zwangsweise ("REBAL") und
    besetzt das Depot komplett neu. Der LIVE-Bot macht das NICHT: er rotiert
    nicht aus dem Ranking heraus (rebalance_portfolio gleicht nur Ziel-Gewichte
    ab), sondern haelt eine Position, bis ein Exit feuert.

    Bei der Exit-Analyse am 20.07.2026 fuehrte dieser Unterschied dazu, dass bis
    zu 100 %% des Backtest-Gewinns aus einem Mechanismus stammten, den es live gar
    nicht gibt — die daraus abgeleitete Empfehlung musste am selben Abend
    zurueckgedreht werden. Dieser Modus schliesst die Luecke: hier kaufen wir
    monatlich nur in FREIE Slots nach, Positionen laufen bis zu ihrem Exit.

    Damit werden die beiden Designs zum ersten Mal vergleichbar:
      run_backtest      = "mit monatlicher Rotation"   (Bot hat das NICHT)
      run_backtest_hold = "halten bis Exit"            (Bot-Realitaet)

    Kapital-Modell: fixes Gewicht deployment/top_n je Slot, Verzinsung beim
    Ausstieg (chronologisch). Bewusst simpel — die Aussage liegt in den
    Trade-Kennzahlen (PF/Payoff/Trefferquote), nicht in der Equity-Kurve.

    BEKANNTE MODELL-GRENZEN (bitte bei der Interpretation mitdenken):
      * Kein buy_cooldown. Der Live-Bot sperrt ein Symbol nach einem Stop eine
        Weile; hier kann derselbe Titel am naechsten Monatsersten sofort wieder
        gekauft werden -> Stop-und-sofort-zurueck wird zu guenstig abgebildet.
      * Kaeufe nur zu Monatsanfang. Live kauft der Bot nach, sobald ein Slot
        frei wird — hier wartet freies Kapital bis zum naechsten Rebalance.
      * Keine Ranking-Rotation (bewusst — das IST ja der Unterschied): ein Titel
        wird nicht verkauft, weil er aus den Top-N faellt.
      * "OPEN_AT_END" = am Testende noch offen. Diese Positionen sind NICHT
        realisiert; ihr Ergebnis ist ein Buchwert und faellt bei Trade-Metriken
        wie PF/Payoff mit ins Gewicht.
    """
    from app import signal_stack
    rebals = _month_starts(start, end)
    end_d = date.fromisoformat(end)
    w = deployment / max(top_n, 1)      # fixes Gewicht je Slot

    open_until: dict = {}               # symbol -> Ausstiegsdatum (Slot belegt bis)
    trades = []

    for rb in rebals:
        # 1) Abgelaufene Positionen geben ihren Slot frei
        for s, ed in list(open_until.items()):
            if ed <= rb:
                del open_until[s]

        free = top_n - len(open_until)
        if free <= 0:
            continue                    # Depot voll -> kein Nachkauf (wie live)

        prices = _prices_asof(price_hist, rb)
        if not prices:
            continue
        if picks_by_month is not None:
            ranked = picks_by_month.get(rb, [])
        else:
            scores = signal_stack.score_universe(list(prices.keys()), facts, prices,
                                                 rb.isoformat())
            ranked = signal_stack.ranked_symbols(scores)
        # Nur was einen Preis hat und noch nicht im Depot liegt
        cand = [p for p in ranked if p in prices and p not in open_until][:free]

        for sym in cand:
            entry = prices[sym][0]
            ser = _forward_series(price_hist, sym, rb, end_d)   # LANGES Fenster
            if not ser:
                continue
            fwd = [c for (_, c) in ser]
            r, reason, days = _sim_position(entry, fwd, sl_pct, tp_pct,
                                            trail_act_pct, trail_pct, tranches)
            # Ausstiegsdatum = Datum des Tages, an dem der Exit gefeuert hat
            idx = min(max(days - 1, 0), len(ser) - 1)
            exit_d = ser[idx][0]
            # "REBAL" heisst hier NICHT Rotation, sondern: kein Exit hat je
            # gefeuert -> Position am Testende noch offen. Klar benennen, sonst
            # verwechselt man es mit dem Rotations-Exit des anderen Modus.
            if reason == "REBAL":
                reason = "OPEN_AT_END"
            open_until[sym] = exit_d
            trades.append({"month": rb.isoformat(), "sym": sym, "ret": r,
                           "ret_net": r - cost_pct, "reason": reason,
                           "days": days, "entry": rb.isoformat(),
                           "exit": exit_d.isoformat()})

    # 2) Equity chronologisch nach Ausstieg verzinsen
    equity = 1.0
    by_month: dict = {}
    for t in sorted(trades, key=lambda x: x["exit"]):
        before = equity
        equity *= (1 + w * t["ret_net"] / 100.0)
        key = t["exit"][:7]
        by_month[key] = by_month.get(key, 0.0) + (equity - before) / before * 100.0

    monthly = [by_month.get(f"{d.year:04d}-{d.month:02d}", 0.0) for d in rebals]
    eq_curve = [(rebals[0], 1.0), (end_d, equity)]
    return {
        "config": {"mode": "hold", "top_n": top_n, "deployment": deployment,
                   "sl_pct": sl_pct, "tp_pct": tp_pct,
                   "trail_act_pct": trail_act_pct, "trail_pct": trail_pct,
                   "tranches": tranches},
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

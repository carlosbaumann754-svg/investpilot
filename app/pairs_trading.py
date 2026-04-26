"""
Pairs Trading Bot (Bot Nr. 5) — SKELETON
=========================================

Status: SKELETON / Architektur-Stub. NICHT live-tradefaehig.
Geplant fuer Phase 3 (Roadmap #14) — voller Implementations-Aufwand
ca. 3-5 Tage. Diese Datei ist die Basis-Architektur damit der Code-Pfad
klar ist und spaetere Iterationen incremental gebaut werden koennen.

==========================================
Was ist Pairs Trading?
==========================================

Markt-neutrale Strategie: Kaufe Asset A long, verkaufe Asset B short
(in gleichem Dollar-Volumen). Profit kommt aus der relativen Bewegung
A vs B, nicht aus der absoluten Marktrichtung.

Funktioniert wenn:
- A und B sind kointegriert (long-term Korrelation)
- Spread (A - beta*B) ist mean-reverting
- Aktueller Spread weicht > 2 Sigma vom Mittel ab -> Trade

Warum IBKR-only:
- eToro CFDs sind kein echtes Shorting (synthetische Position via
  Counterparty-Risk + hoehere Gebuehren)
- IBKR hat echtes Stock-Borrowing -> echte Short-Position mit
  realistischem PnL-Verhalten

==========================================
Architektur (modular, isoliert vom Bot Nr. 1)
==========================================

[Pair-Universe (kointegrierte Paare)]
        │
        ▼
[PairScreener]    --- statisticher Engle-Granger Test, ADF, Half-Life
        │
        ▼
[Spread-Calculator]  --- z-score = (current - mean) / stddev
        │
        ▼
[Signal-Generator]   --- z > +2 -> SHORT A / LONG B
                         z < -2 -> LONG A / SHORT B
                         |z| < 0.5 -> Close
        │
        ▼
[PairsBot.run_cycle()]  --- nutzt BrokerBase (IbkrBroker) fuer Orders
        │
        ▼
[Trade-Execution]   --- 2 Orders pro Signal (Long-Leg + Short-Leg)
        │              gleichzeitig submitten, gleiche Dollar-Volumen
        ▼
[Risk-Manager-Hook] --- max % Portfolio in Pairs, max # gleichzeitige Pairs

==========================================
Geplante Phasen
==========================================

W1 (1 Tag): Modul-Skelett (DIESE DATEI) + Config-Schema + Tests-Stubs
W2 (1 Tag): PairScreener mit yfinance-Daten — Universe-Building
            (Top 20 kointegrierte Paare aus S&P-500 Sektoren)
W3 (1 Tag): Live-Spread-Calculator + Z-Score-Signale + Backtester
W4 (1-2 Tage): IBKR-Integration: 2-Leg-Orders, Position-Tracking,
               Closing-Logic. Risk-Manager-Hooks
W5 (Optional): Multi-Strategy-Router (Bot 1 + Bot 5 parallel)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Config-Schema (zukuenftig in config.json unter "pairs_trading")
# ----------------------------------------------------------------------

DEFAULT_CONFIG = {
    "enabled": False,                 # Master-Switch (default OFF)
    "max_portfolio_pct": 20,          # max 20% Portfolio in Pairs
    "max_concurrent_pairs": 3,        # max 3 offene Pairs gleichzeitig
    "z_score_entry": 2.0,             # |z| > 2 -> Trade
    "z_score_exit": 0.5,              # |z| < 0.5 -> Close
    "lookback_days": 60,              # Spread-Stats-Window
    "min_half_life_days": 3,          # min Mean-Reversion-Speed
    "max_half_life_days": 30,         # max (sonst nicht tradeable)
    "rebalance_intraday": False,      # heute nur EOD-Signale
}


# ----------------------------------------------------------------------
# Datenstrukturen
# ----------------------------------------------------------------------

@dataclass
class Pair:
    """Definiert ein kointegriertes Asset-Paar fuer Trading."""
    symbol_a: str             # z.B. "KO"
    symbol_b: str             # z.B. "PEP"
    beta: float               # Hedge-Ratio aus OLS-Regression
    half_life_days: float     # Mean-Reversion-Geschwindigkeit
    mean_spread: float        # historischer Mittelwert von (A - beta*B)
    std_spread: float         # historische Std von (A - beta*B)
    last_updated: str = ""    # ISO-Timestamp letzter Re-Calibration

    @property
    def name(self) -> str:
        return f"{self.symbol_a}-{self.symbol_b}"


@dataclass
class PairsSignal:
    """Ein konkretes Trade-Signal fuer ein Paar."""
    pair: Pair
    direction: str            # "LONG_A_SHORT_B" oder "SHORT_A_LONG_B" oder "CLOSE"
    z_score: float
    suggested_amount_usd: float
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ----------------------------------------------------------------------
# Hauptklasse PairsBot — Skeleton-Methoden
# ----------------------------------------------------------------------

class PairsBot:
    """
    Pairs Trading Bot — Implementation-Skeleton.

    Verwendung (zukuenftig):
        from app.broker_base import get_broker
        from app.pairs_trading import PairsBot

        broker = get_broker(config)  # muss IbkrBroker sein!
        bot = PairsBot(broker, config)
        bot.run_cycle()
    """

    def __init__(self, broker, config: Optional[dict] = None):
        self.broker = broker
        cfg_pairs = (config or {}).get("pairs_trading", {})
        self.config = {**DEFAULT_CONFIG, **cfg_pairs}
        if broker.broker_name != "ibkr":
            log.warning(
                "PairsBot benoetigt IBKR-Broker (echtes Shorting). "
                "Aktuell '%s' — Bot wird im Read-Only-Mode laufen.",
                broker.broker_name,
            )

    # ------------------------------------------------------------------
    # Pair-Discovery (W2 LIVE)
    # ------------------------------------------------------------------

    def discover_pairs(
        self,
        candidates: Optional[list[str]] = None,
        years: int = 2,
        max_pairs: int = 20,
        p_threshold: float = 0.05,
    ) -> list[Pair]:
        """
        Findet kointegrierte Paare via Engle-Granger 2-Step Test.

        Workflow:
        1. yfinance: Daily-Closes fuer alle candidates ueber `years` Jahre
        2. Fuer jede Kombination (a,b): statsmodels coint() -> p-value
        3. Filter: p-value < p_threshold AND half-life in [min,max]
        4. Berechne beta (OLS), mean/std vom Spread
        5. Sortiere nach |z-Spread-Sharpe| descending
        6. Top max_pairs zurueckgeben

        Args:
            candidates: Symbole zum Pair-Test (None = ASSET_UNIVERSE-Stocks)
            years: yfinance-Lookback (default 2y)
            max_pairs: max # Paare zurueckgeben
            p_threshold: Engle-Granger p-Wert-Schwelle (95% = 0.05)

        Returns:
            Liste sortierter Pair-Objekte (best first).
        """
        import itertools
        import yfinance as yf
        import numpy as np
        from datetime import datetime as _dt

        try:
            from statsmodels.tsa.stattools import coint
            from statsmodels.regression.linear_model import OLS
            from statsmodels.tools import add_constant
        except ImportError as e:
            raise ImportError(
                "statsmodels fehlt — pip install statsmodels>=0.14"
            ) from e

        # Default candidates: alle Stocks aus ASSET_UNIVERSE
        if candidates is None:
            try:
                from app.market_scanner import ASSET_UNIVERSE
                candidates = [
                    sym for sym, meta in ASSET_UNIVERSE.items()
                    if (meta.get("class") or "").lower() in ("stocks", "stock", "etf")
                ]
            except Exception:
                candidates = ["AAPL", "MSFT", "GOOGL", "META", "AMZN"]

        log.info("PairScreener: Lade %d Symbole, %dy History...", len(candidates), years)

        # 1. Bulk-Download via yfinance
        try:
            df = yf.download(candidates, period=f"{years}y", progress=False, auto_adjust=True)["Close"]
            df = df.dropna(axis=1, how="all")  # leere Symbole raus
            df = df.dropna()                    # Tage mit gaps raus
        except Exception as e:
            log.error("yfinance bulk-download failed: %s", e)
            return []

        if df.shape[1] < 2 or df.shape[0] < 60:
            log.warning("Zu wenig Daten (cols=%d, rows=%d) — discover abgebrochen",
                        df.shape[1], df.shape[0])
            return []

        usable_symbols = list(df.columns)
        log.info("PairScreener: %d nutzbare Symbole (Shape %s)", len(usable_symbols), df.shape)

        # 2. Coint-Test pro Paar
        candidates_pairs: list[tuple[str, str, float]] = []
        for sym_a, sym_b in itertools.combinations(usable_symbols, 2):
            series_a = df[sym_a].values
            series_b = df[sym_b].values
            try:
                _t_stat, p_value, _crit = coint(series_a, series_b)
            except Exception:
                continue
            if p_value < p_threshold:
                candidates_pairs.append((sym_a, sym_b, float(p_value)))

        log.info("PairScreener: %d kointegrierte Paare (p < %.3f)",
                 len(candidates_pairs), p_threshold)

        # 3. Pro Paar: beta + spread-stats berechnen
        pairs: list[Pair] = []
        for sym_a, sym_b, p_value in candidates_pairs:
            series_a = df[sym_a].values
            series_b = df[sym_b].values
            try:
                # Beta via OLS: a ~ alpha + beta*b
                X = add_constant(series_b)
                model = OLS(series_a, X).fit()
                beta = float(model.params[1])
                spread = series_a - beta * series_b
                mean_spread = float(np.mean(spread))
                std_spread = float(np.std(spread))
                if std_spread <= 0:
                    continue
                # Half-Life via OU: dlnX = lambda*(mu - X)*dt -> OLS auf dX vs X_lag
                spread_lag = spread[:-1]
                d_spread = np.diff(spread)
                X_hl = add_constant(spread_lag)
                hl_model = OLS(d_spread, X_hl).fit()
                lam = -float(hl_model.params[1])
                if lam <= 0:
                    continue  # nicht mean-reverting
                half_life = float(np.log(2) / lam)
                # Filter half-life zwischen 3 und 30 Tagen (config)
                if not (self.config["min_half_life_days"] <= half_life
                        <= self.config["max_half_life_days"]):
                    continue
                pairs.append(Pair(
                    symbol_a=sym_a,
                    symbol_b=sym_b,
                    beta=beta,
                    half_life_days=half_life,
                    mean_spread=mean_spread,
                    std_spread=std_spread,
                    last_updated=_dt.now().isoformat(timespec="seconds"),
                ))
            except Exception as e:
                log.debug("Pair %s/%s skip: %s", sym_a, sym_b, e)
                continue

        # 4. Sortieren nach inverse half-life (kuerzer = besser tradebar)
        pairs.sort(key=lambda p: p.half_life_days)
        log.info("PairScreener: %d valid Paare nach Filter (half-life %d-%d Tage)",
                 len(pairs), self.config["min_half_life_days"],
                 self.config["max_half_life_days"])

        return pairs[:max_pairs]

    # ------------------------------------------------------------------
    # Signal-Generation (TODO W3)
    # ------------------------------------------------------------------

    def calculate_signals(
        self,
        pairs: list[Pair],
        portfolio_value_usd: float,
        open_pair_positions: Optional[list[str]] = None,
    ) -> list[PairsSignal]:
        """
        Pro Paar: aktueller Spread vs. historisches Mittel -> z-score -> Signal.

        Workflow:
        1. yfinance: aktuelle Closes (oder broker.live-quote falls verfuegbar)
        2. spread_now = price_a - beta * price_b
        3. z = (spread_now - mean_spread) / std_spread
        4. Signal-Logic:
           - z > +entry  -> SHORT_A_LONG_B (Spread sollte kleiner werden)
           - z < -entry  -> LONG_A_SHORT_B (Spread sollte groesser werden)
           - |z| < exit AND open position -> CLOSE
           - sonst -> kein Signal (skip)
        5. Sizing: portfolio_value * max_portfolio_pct / max_concurrent_pairs

        Args:
            pairs: Liste von Pair-Objekten aus discover_pairs()
            portfolio_value_usd: aktuelles Equity (fuer Sizing)
            open_pair_positions: Liste von pair.name strings die schon offen sind

        Returns:
            Liste von PairsSignal-Objekten (kann leer sein wenn nichts triggert).
        """
        if not pairs:
            return []
        open_pair_positions = set(open_pair_positions or [])

        # Live-Closes via yfinance (kompakt: 1 Tag, 1 Wert pro Symbol)
        try:
            import yfinance as yf
        except ImportError:
            log.error("yfinance fehlt — calculate_signals abgebrochen")
            return []

        # Sammle alle einzigartigen Symbole
        all_syms = sorted({s for p in pairs for s in (p.symbol_a, p.symbol_b)})
        try:
            df = yf.download(all_syms, period="5d", progress=False, auto_adjust=True)["Close"]
            df = df.dropna(how="all")
            if df.empty:
                log.warning("Keine Live-Closes verfuegbar")
                return []
            # Letzter verfuegbarer Close pro Symbol
            last_closes = df.iloc[-1].to_dict()
        except Exception as e:
            log.error("yfinance live-fetch failed: %s", e)
            return []

        # Sizing: gleichmaessig auf max_concurrent_pairs verteilt
        max_pairs_open = int(self.config["max_concurrent_pairs"])
        portfolio_pct = float(self.config["max_portfolio_pct"]) / 100.0
        per_pair_usd = portfolio_value_usd * portfolio_pct / max(1, max_pairs_open)
        # Pro Pair: halbe Sume LONG, halbe SHORT (gleiche Dollar-Exposure)
        per_leg_usd = per_pair_usd / 2.0

        z_entry = float(self.config["z_score_entry"])
        z_exit = float(self.config["z_score_exit"])

        signals: list[PairsSignal] = []
        for pair in pairs:
            price_a = last_closes.get(pair.symbol_a)
            price_b = last_closes.get(pair.symbol_b)
            if price_a is None or price_b is None or pair.std_spread <= 0:
                continue
            try:
                price_a, price_b = float(price_a), float(price_b)
            except (TypeError, ValueError):
                continue

            spread_now = price_a - pair.beta * price_b
            z = (spread_now - pair.mean_spread) / pair.std_spread

            is_open = pair.name in open_pair_positions
            direction = None
            reason = None

            # Close-Logic priorisiert (offene Position auflösen wenn z neutral)
            if is_open and abs(z) < z_exit:
                direction = "CLOSE"
                reason = f"z={z:+.2f} < exit={z_exit} (mean-reverted)"
            # Entry-Logic nur wenn nicht schon offen
            elif not is_open and z > z_entry:
                direction = "SHORT_A_LONG_B"
                reason = f"z={z:+.2f} > +{z_entry} (Spread zu hoch -> wird sinken)"
            elif not is_open and z < -z_entry:
                direction = "LONG_A_SHORT_B"
                reason = f"z={z:+.2f} < -{z_entry} (Spread zu tief -> wird steigen)"

            if direction:
                signals.append(PairsSignal(
                    pair=pair,
                    direction=direction,
                    z_score=float(z),
                    suggested_amount_usd=per_leg_usd,
                    reason=reason or "",
                ))

        log.info("PairsBot: %d Signale aus %d Paaren (z_entry=%.1f, z_exit=%.1f)",
                 len(signals), len(pairs), z_entry, z_exit)
        return signals

    # ------------------------------------------------------------------
    # Order-Submission (TODO W4)
    # ------------------------------------------------------------------

    def execute_signal(self, signal: PairsSignal) -> dict:
        """
        Submittet 2-Leg-Order (Long-Leg + Short-Leg gleichzeitig).

        TODO W4:
            - Symbol -> instrument_id Mapping fuer beide Legs
            - broker.buy(long_id, amount_usd) + broker.sell(short_id, amount_usd)
            - Beide Orders gemeinsam tracken (state pro Pair-Position)
            - Bei Partial-Fill: alle Legs auf gleiche Dollar-Exposure rebalancen
            - Failed-Leg-Recovery (wenn Long klappt aber Short nicht ->
              Long sofort wieder schliessen)
        """
        raise NotImplementedError("execute_signal ist W4-TODO")

    # ------------------------------------------------------------------
    # Cycle-Hauptschleife (TODO W4)
    # ------------------------------------------------------------------

    def run_cycle(self) -> dict:
        """
        Ein Trading-Zyklus. Wird von app/scheduler.py aufgerufen wenn
        config.pairs_trading.enabled = true.

        TODO W4: orchestriert discover + signals + execute.
        """
        if not self.config["enabled"]:
            log.info("PairsBot disabled (config.pairs_trading.enabled = false)")
            return {"status": "disabled"}
        raise NotImplementedError("run_cycle ist W4-TODO")


# ----------------------------------------------------------------------
# Module-Level Helper (TODO W3)
# ----------------------------------------------------------------------

def calculate_half_life(spread_series) -> Optional[float]:
    """Mean-Reversion Half-Life via OU-Prozess (Ornstein-Uhlenbeck) Fit.

    Modell: dX_t = lambda * (mu - X_t) * dt + sigma * dW
        -> half-life = ln(2) / lambda
    Schaetzung via OLS: dX = a + b*X_lag + e -> lambda = -b

    Returns:
        Half-Life in der Einheit der Zeitschritte (Tage bei Daily-Data),
        oder None wenn nicht mean-reverting (lambda <= 0).
    """
    import numpy as np
    try:
        from statsmodels.regression.linear_model import OLS
        from statsmodels.tools import add_constant
    except ImportError:
        return None
    arr = np.asarray(spread_series, dtype=float)
    if len(arr) < 30:
        return None
    spread_lag = arr[:-1]
    d_spread = np.diff(arr)
    X = add_constant(spread_lag)
    try:
        model = OLS(d_spread, X).fit()
        lam = -float(model.params[1])
        if lam <= 0:
            return None
        return float(np.log(2) / lam)
    except Exception:
        return None


def is_cointegrated(series_a, series_b, p_threshold: float = 0.05) -> bool:
    """Engle-Granger 2-Step Test fuer Kointegration.

    Returns True wenn p-Wert < p_threshold (95% Konfidenz default = 0.05).
    """
    try:
        from statsmodels.tsa.stattools import coint
    except ImportError:
        return False
    try:
        _t, p_value, _crit = coint(series_a, series_b)
        return float(p_value) < p_threshold
    except Exception:
        return False

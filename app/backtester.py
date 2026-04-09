"""
InvestPilot - Backtesting Engine
Replayed die Scanner-Scoring-Logik auf 5+ Jahren historischen Daten.
Simuliert Trades mit realistischen Transaktionskosten (Spread, Overnight).
Walk-Forward-Validierung: Train 80% / Test 20%.

Realistic Filters (v2):
  - VIX Regime Filter: blocks/reduces trades when VIX is elevated
  - Earnings Blackout: skips trades near earnings dates
  - Sector Concentration: limits positions per sector
  - Improved Trailing Stop-Loss: uses intraday highs
"""

import logging
import math
from datetime import datetime, timedelta
from collections import defaultdict

log = logging.getLogger("Backtester")

try:
    import yfinance as yf
    import numpy as np
except ImportError:
    yf = None
    np = None

from app.config_manager import load_config, load_json, save_json
from app.market_scanner import (
    ASSET_UNIVERSE,
    calc_rsi,
    calc_macd,
    calc_bollinger_position,
)

# ============================================================
# TRANSACTION COST MODEL (eToro realistic)
# ============================================================
SPREAD_PCT = 0.0015          # 0.15% per trade (spread)
OVERNIGHT_FEE_PCT = 0.0001   # 0.01% per night (leveraged)
SLIPPAGE_PCT = 0.0005        # 0.05% estimated slippage


# ============================================================
# HISTORY DOWNLOAD
# ============================================================

def download_history(symbols=None, years=5):
    """Download historical OHLCV data via yfinance.

    Args:
        symbols: list of ASSET_UNIVERSE keys, or None for a representative subset
        years: how many years of history

    Returns:
        dict {symbol: DataFrame with columns [Open, High, Low, Close, Volume]}
    """
    if yf is None:
        log.error("yfinance nicht installiert")
        return {}

    if symbols is None:
        # Full ASSET_UNIVERSE for realistic backtesting
        symbols = list(ASSET_UNIVERSE.keys())

    # Apply config-based universe filter (disabled_symbols)
    try:
        _cfg = load_config()
        _disabled = set(_cfg.get("disabled_symbols", []) or [])
        if _disabled:
            before = len(symbols)
            symbols = [s for s in symbols if s not in _disabled]
            log.info(f"Universe-Filter: {before - len(symbols)} disabled_symbols ausgefiltert")
    except Exception as _e:
        log.debug(f"Universe-Filter skipped: {_e}")

    period = f"{years}y"
    histories = {}
    errors = 0
    batch_size = 10

    log.info(f"Downloading {len(symbols)} assets, period={period}...")

    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        for sym in batch:
            info = ASSET_UNIVERSE.get(sym)
            if not info:
                continue
            yf_sym = info["yf"]
            try:
                ticker = yf.Ticker(yf_sym)
                hist = ticker.history(period=period, interval="1d")
                if hist.empty or len(hist) < 100:
                    log.debug(f"  {sym}: zu wenig Daten ({len(hist)} Tage)")
                    errors += 1
                    continue
                histories[sym] = hist
                log.debug(f"  {sym}: {len(hist)} Tage geladen")
            except Exception as e:
                log.debug(f"  {sym} Download-Fehler: {e}")
                errors += 1
        # Rate limiting between batches
        if i + batch_size < len(symbols):
            import time
            time.sleep(2)

    log.info(f"Download fertig: {len(histories)} OK, {errors} Fehler")
    return histories


# ============================================================
# VIX HISTORY DOWNLOAD (for regime filter in backtest)
# ============================================================

def download_vix_history(years=5):
    """Download VIX history for backtesting regime filter.

    Returns:
        dict {date -> vix_close} mapping trading dates to VIX closing values,
        or empty dict if download fails.
    """
    if yf is None:
        log.warning("yfinance nicht installiert — VIX-History nicht verfuegbar")
        return {}

    try:
        period = f"{years}y"
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period=period, interval="1d")
        if hist.empty:
            log.warning("VIX-History leer")
            return {}

        vix_data = {}
        for date, row in hist.iterrows():
            # Normalize date to match symbol history dates
            vix_data[date] = float(row["Close"])

        log.info(f"VIX-History geladen: {len(vix_data)} Tage")
        return vix_data
    except Exception as e:
        log.warning(f"VIX-History Download fehlgeschlagen: {e}")
        return {}


# ============================================================
# EARNINGS HISTORY (for backtest earnings blackout)
# ============================================================

def _fetch_historical_earnings_dates(symbol):
    """Fetch historical earnings dates for a symbol via yfinance.

    Returns list of datetime objects (earnings dates), or empty list.
    Uses the earnings_dates attribute which provides historical data.
    """
    if yf is None:
        return []

    try:
        info = ASSET_UNIVERSE.get(symbol)
        if not info:
            return []
        yf_sym = info["yf"]

        # Only stocks have earnings — skip crypto, forex, commodities, indices
        asset_class = info.get("class", "")
        if asset_class in ("crypto", "forex", "commodities", "indices"):
            return []

        ticker = yf.Ticker(yf_sym)

        # yfinance >= 0.2: ticker.earnings_dates returns a DataFrame with index = date
        earnings_dates_df = getattr(ticker, "earnings_dates", None)
        if earnings_dates_df is not None and hasattr(earnings_dates_df, "index") and len(earnings_dates_df) > 0:
            dates = []
            for dt in earnings_dates_df.index:
                if hasattr(dt, "to_pydatetime"):
                    dates.append(dt.to_pydatetime().replace(tzinfo=None))
                elif isinstance(dt, datetime):
                    dates.append(dt.replace(tzinfo=None))
            return dates

        # Fallback: quarterly earnings
        qe = getattr(ticker, "quarterly_earnings", None)
        if qe is not None and hasattr(qe, "index") and len(qe) > 0:
            dates = []
            for dt in qe.index:
                if hasattr(dt, "to_pydatetime"):
                    dates.append(dt.to_pydatetime().replace(tzinfo=None))
            return dates

        return []
    except Exception as e:
        log.debug(f"Earnings-History fuer {symbol} nicht abrufbar: {e}")
        return []


def _build_earnings_blackout_set(symbol, earnings_dates, buffer_before=3, buffer_after=1):
    """Build a set of dates that are in the earnings blackout window.

    Args:
        symbol: ticker symbol (for logging)
        earnings_dates: list of datetime objects
        buffer_before: days before earnings to block
        buffer_after: days after earnings to block

    Returns:
        set of date objects (date only, no time) that are blacked out
    """
    blackout_dates = set()
    for ed in earnings_dates:
        ed_date = ed.date() if hasattr(ed, "date") else ed
        for offset in range(-buffer_after, buffer_before + 1):
            blackout_dates.add(ed_date + timedelta(days=offset))
    return blackout_dates


# ============================================================
# SCORING ON HISTORICAL DATA
# ============================================================

def _features_at_bar(closes, volumes, idx, lookback=60):
    """Extrahiert v12-Backtest-Features fuer einen Bar.

    Returns:
        dict mit score, volatility, rsi, momentum_5d, momentum_20d, mr_strength
        oder None wenn nicht genug Daten.
    """
    if idx < lookback:
        return None
    window = closes[max(0, idx - lookback):idx + 1]
    vol_window = volumes[max(0, idx - lookback):idx + 1]
    if len(window) < 20:
        return None

    rsi = calc_rsi(window)
    boll_pos = calc_bollinger_position(window)

    volatility = 5.0
    if len(window) >= 20:
        returns = [(window[i] - window[i - 1]) / window[i - 1]
                   for i in range(max(1, len(window) - 20), len(window))]
        volatility = (sum(r ** 2 for r in returns) / len(returns)) ** 0.5 * 100

    momentum_5d = 0
    if len(window) >= 5:
        momentum_5d = (window[-1] - window[-5]) / window[-5] * 100
    momentum_20d = 0
    if len(window) >= 20:
        momentum_20d = (window[-1] - window[-20]) / window[-20] * 100

    # Mean-Reversion-Staerke (analog zu market_scanner.apply_regime_strategy_modifier)
    mr_strength = 0
    if rsi < 35:
        mr_strength += (35 - rsi) * 0.5
    if boll_pos < 0.25:
        mr_strength += (0.25 - boll_pos) * 20

    score = _score_at_bar(closes, volumes, idx, lookback)
    return {
        "score": score,
        "volatility": volatility,
        "rsi": rsi,
        "momentum_5d": momentum_5d,
        "momentum_20d": momentum_20d,
        "mr_strength": mr_strength,
    }


def _score_at_bar(closes, volumes, idx, lookback=60):
    """Score an asset at a specific bar index using scanner logic.

    Replicates the same indicators as market_scanner.score_asset().
    """
    if idx < lookback:
        return 0

    window = closes[max(0, idx - lookback):idx + 1]
    vol_window = volumes[max(0, idx - lookback):idx + 1]

    if len(window) < 20:
        return 0

    rsi = calc_rsi(window)
    macd_val, signal_val, macd_hist = calc_macd(window)
    boll_pos = calc_bollinger_position(window)

    current = window[-1]

    # Momentum
    momentum_5d = 0
    if len(window) >= 5:
        momentum_5d = (window[-1] - window[-5]) / window[-5] * 100

    momentum_20d = 0
    sma_20 = current
    if len(window) >= 20:
        momentum_20d = (window[-1] - window[-20]) / window[-20] * 100
        sma_20 = sum(window[-20:]) / 20

    sma_50 = sma_20
    if len(window) >= 50:
        sma_50 = sum(window[-50:]) / 50

    # Volume trend
    vol_trend = 1.0
    if len(vol_window) >= 10 and sum(vol_window[-10:-5]) > 0:
        vol_trend = sum(vol_window[-5:]) / sum(vol_window[-10:-5])

    # Volatility
    volatility = 5.0
    if len(window) >= 20:
        returns = [(window[i] - window[i - 1]) / window[i - 1]
                   for i in range(max(1, len(window) - 20), len(window))]
        volatility = (sum(r ** 2 for r in returns) / len(returns)) ** 0.5 * 100

    above_sma20 = current > sma_20
    above_sma50 = current > sma_50
    golden_cross = sma_20 > sma_50

    # --- Scoring (exact copy of market_scanner.score_asset) ---
    score = 0

    if rsi < 30:
        score += 20
    elif rsi < 40:
        score += 10
    elif rsi > 70:
        score -= 20
    elif rsi > 60:
        score -= 5

    if macd_hist > 0:
        score += 10
        if macd_val > signal_val:
            score += 5
    else:
        score -= 10
        if macd_val < signal_val:
            score -= 5

    score += max(-10, min(10, momentum_5d * 2))
    score += max(-10, min(10, momentum_20d * 0.5))

    if golden_cross:
        score += 10
    if above_sma20:
        score += 5
    else:
        score -= 5
    if above_sma50:
        score += 5
    else:
        score -= 5

    if boll_pos < 0.2:
        score += 10
    elif boll_pos > 0.8:
        score -= 10

    if vol_trend > 1.2 and score > 0:
        score += 5
    elif vol_trend > 1.2 and score < 0:
        score -= 5

    if volatility > 5:
        score *= 0.9

    return round(score, 1)


# ============================================================
# GRID-SEARCH PRECOMPUTE (v10 Performance Pack)
# ============================================================
#
# Diese Helpers berechnen einmalig alle Daten, die zwischen Grid-Combos
# IDENTISCH sind: Symbol-Arrays, Score-Matrix pro (sym, day), normalisierte
# VIX-Lookups, Datums-Indices. Spart 30-50x Laufzeit im Optimizer.
#
# Wichtig: Die hier produzierten Daten sind READ-ONLY — die Simulation darf
# nicht in das precomputed-Dict schreiben (sonst killt es Multiprocessing).

def precompute_grid_data(histories, vix_history=None):
    """Pre-compute alle Daten die zwischen Grid-Combos identisch sind.

    Returns:
        dict mit:
          - symbol_data: {sym: {closes, highs, volumes, dates, dates_to_idx, scores, sector}}
          - sorted_dates: sortierte Liste aller Daten ueber alle Symbole
          - vix_by_date_norm: {date_obj.date() -> vix_value} fuer O(1) lookup
    """
    symbol_data = {}
    all_dates_set = set()

    for sym, hist in histories.items():
        closes = hist["Close"].values.tolist()
        highs = hist["High"].values.tolist() if "High" in hist.columns else closes[:]
        volumes = hist["Volume"].values.tolist()
        dates = hist.index.tolist()

        # O(1) date -> idx lookup (statt list.index() O(N))
        dates_to_idx = {d: i for i, d in enumerate(dates)}

        # v12: Score + Features-Matrix - pro Bar einmal berechnen.
        scores = []
        volatilities = []
        mr_strengths = []
        for i in range(len(closes)):
            feats = _features_at_bar(closes, volumes, i)
            if feats is None:
                scores.append(0)
                volatilities.append(5.0)
                mr_strengths.append(0)
            else:
                scores.append(feats["score"])
                volatilities.append(feats["volatility"])
                mr_strengths.append(feats["mr_strength"])

        # Sektor cachen (wird sonst in jeder Combo neu nachgeschlagen)
        info = ASSET_UNIVERSE.get(sym, {})
        sector = info.get("sector", "unknown")

        symbol_data[sym] = {
            "closes": closes,
            "highs": highs,
            "volumes": volumes,
            "dates": dates,
            "dates_to_idx": dates_to_idx,
            "scores": scores,
            "volatilities": volatilities,
            "mr_strengths": mr_strengths,
            "sector": sector,
        }
        all_dates_set.update(dates)

    sorted_dates = sorted(all_dates_set)

    # VIX normalisieren: einmal date()-Strip, danach O(1) lookup
    vix_by_date_norm = {}
    if vix_history:
        for vix_date, vix_val in vix_history.items():
            try:
                key = vix_date.date() if hasattr(vix_date, "date") else vix_date
            except Exception:
                key = vix_date
            vix_by_date_norm[key] = vix_val

    # v12: Regime-Lookup pro Tag aus VIX abgeleitet
    # bull: VIX < 18 (low fear)
    # sideways: VIX 18..25 (normal)
    # bear: VIX > 25 (elevated/high fear)
    regime_by_date = {}
    for date_key, vix_val in vix_by_date_norm.items():
        if vix_val is None:
            regime_by_date[date_key] = "unknown"
        elif vix_val < 18:
            regime_by_date[date_key] = "bull"
        elif vix_val < 25:
            regime_by_date[date_key] = "sideways"
        else:
            regime_by_date[date_key] = "bear"

    return {
        "symbol_data": symbol_data,
        "sorted_dates": sorted_dates,
        "vix_by_date_norm": vix_by_date_norm,
        "regime_by_date": regime_by_date,
    }


def simulate_trades_fast(precomputed, config=None, earnings_blackouts=None,
                         use_realistic_filters=True):
    """Fast simulate_trades using precomputed data structures.

    Bit-identisch zu simulate_trades(), aber:
      - Keine pandas->list Konvertierung (precomputed)
      - O(1) statt O(N) date->idx lookups
      - Score-Matrix statt _score_at_bar() pro Combo
      - Normalisierte VIX-Lookups

    Wird ausschliesslich von run_grid_search() im Optimizer aufgerufen.
    Die Ergebnisse sind identisch zu simulate_trades() (siehe Regression-Test).
    """
    if config is None:
        config = load_config()

    dt = config.get("demo_trading", {})
    sl_pct = dt.get("stop_loss_pct", -3) / 100
    tp_pct = dt.get("take_profit_pct", 5) / 100
    min_score = dt.get("min_scanner_score", 15)
    max_positions = dt.get("max_positions", 20)

    lev_cfg = config.get("leverage", {})
    trailing_sl_pct = lev_cfg.get("trailing_sl_pct", 2.0) / 100
    trailing_activation_pct = lev_cfg.get("trailing_sl_activation_pct", 1.0) / 100

    rf_cfg = config.get("regime_filter", {})
    vix_crisis_threshold = rf_cfg.get("vix_crisis_threshold", 35)
    vix_caution_threshold = rf_cfg.get("vix_caution_threshold", 25)

    risk_cfg = config.get("risk_management", {})
    max_positions_per_sector = risk_cfg.get("max_positions_per_sector", 4)
    max_sector_allocation_pct = risk_cfg.get("max_sector_allocation_pct", 35)

    # v12 Feature-Flags aus Config
    ts_cfg = config.get("time_stop", {})
    ts_enabled = ts_cfg.get("enabled", False)
    ts_max_days = ts_cfg.get("max_days_stale", 10)
    ts_pnl_thresh = ts_cfg.get("stale_pnl_threshold_pct", 0.5) / 100
    ts_min_days = ts_cfg.get("min_days_open", 2)

    rs_cfg = config.get("regime_strategies", {})
    rs_enabled = rs_cfg.get("enabled", False)
    rs_bull_boost = rs_cfg.get("bull_momentum_boost", 0.5)
    rs_sideways_boost = rs_cfg.get("sideways_mr_boost", 0.6)
    rs_bear_penalty = rs_cfg.get("bear_non_defensive_penalty", -10)
    _DEFENSIVE = {"health", "consumer", "bonds", "commodities", "real_estate"}

    ml_cfg = config.get("meta_labeling", {})
    ml_enabled = ml_cfg.get("enabled", False) and not ml_cfg.get("shadow_mode", True)
    ml_min_score = ml_cfg.get("backtest_min_score", 50)
    ml_max_vol = ml_cfg.get("backtest_max_volatility", 4.5)

    if earnings_blackouts is None:
        earnings_blackouts = {}

    symbol_data = precomputed["symbol_data"]
    sorted_dates = precomputed["sorted_dates"]
    vix_by_date_norm = precomputed["vix_by_date_norm"]
    regime_by_date = precomputed.get("regime_by_date", {})

    filter_stats = {
        "vix_blocked": 0,
        "vix_reduced": 0,
        "earnings_blocked": 0,
        "sector_blocked": 0,
    }

    tp_tranches_cfg = config.get("leverage", {}).get("tp_tranches", [])

    trades = []
    open_positions = {}
    trailing_highs = {}
    trailing_sl = {}
    partial_triggered = {}

    start_idx = 60

    def _get_vix_fast(dt_val):
        if not vix_by_date_norm:
            return None
        try:
            key = dt_val.date() if hasattr(dt_val, "date") else dt_val
        except Exception:
            key = dt_val
        return vix_by_date_norm.get(key)

    def _is_earnings_blackout_fast(sym, current_date):
        if sym not in earnings_blackouts:
            return False
        blackout_set = earnings_blackouts[sym]
        if not blackout_set:
            return False
        try:
            check_date = current_date.date() if hasattr(current_date, "date") else current_date
        except Exception:
            check_date = current_date
        return check_date in blackout_set

    def _check_sector_concentration_fast(new_sym):
        new_sector = symbol_data.get(new_sym, {}).get("sector", "unknown")
        if not new_sector or new_sector == "unknown":
            return True

        sector_count = defaultdict(int)
        sector_value = defaultdict(float)
        total_value = 0.0

        for sym, pos in open_positions.items():
            sec = pos.get("sector", "unknown")
            sector_count[sec] += 1
            val = pos.get("entry_price", 1.0)
            sector_value[sec] += val
            total_value += val

        if sector_count[new_sector] >= max_positions_per_sector:
            return False

        if total_value > 0:
            avg_val = total_value / max(len(open_positions), 1)
            new_sector_val = sector_value[new_sector] + avg_val
            new_total = total_value + avg_val
            if new_total > 0 and (new_sector_val / new_total * 100) > max_sector_allocation_pct:
                return False

        return True

    for day_i in range(start_idx, len(sorted_dates)):
        current_date = sorted_dates[day_i]

        # 1. SL/TP/Trailing fuer offene Positionen
        for sym in list(open_positions.keys()):
            sd = symbol_data.get(sym)
            if not sd:
                continue
            sym_idx = sd["dates_to_idx"].get(current_date, -1)
            if sym_idx < 0:
                continue

            pos = open_positions[sym]
            current_price = sd["closes"][sym_idx]
            intraday_high = sd["highs"][sym_idx]
            entry_price = pos["entry_price"]
            pnl_pct = (current_price - entry_price) / entry_price

            high_pnl_pct = (intraday_high - entry_price) / entry_price
            if high_pnl_pct >= trailing_activation_pct:
                if sym not in trailing_highs or intraday_high > trailing_highs[sym]:
                    trailing_highs[sym] = intraday_high
                trail_level = trailing_highs[sym] * (1 - trailing_sl_pct)
                if sym not in trailing_sl or trail_level > trailing_sl[sym]:
                    trailing_sl[sym] = trail_level

            if sym in trailing_sl and current_price <= trailing_sl[sym]:
                days_held = (current_date - pos["entry_date"]).days
                cost = _calc_costs(entry_price, days_held)
                trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                    "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(current_price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                    "cost_pct": round(cost * 100, 3),
                    "days_held": days_held,
                    "exit_reason": "TRAILING_SL",
                    "entry_score": pos["score"],
                })
                del open_positions[sym]
                trailing_highs.pop(sym, None)
                del trailing_sl[sym]
                partial_triggered.pop(sym, None)
                continue

            if tp_tranches_cfg and pnl_pct > 0:
                triggered_set = partial_triggered.get(sym, set())
                for t_idx, t_cfg in enumerate(tp_tranches_cfg):
                    if t_idx in triggered_set:
                        continue
                    if pnl_pct * 100 >= t_cfg.get("profit_target_pct", 0):
                        days_held = (current_date - pos["entry_date"]).days
                        cost = _calc_costs(entry_price, days_held)
                        trades.append({
                            "symbol": sym,
                            "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                            "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                            "entry_price": round(entry_price, 4),
                            "exit_price": round(current_price, 4),
                            "pnl_pct": round(pnl_pct * 100, 2),
                            "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                            "cost_pct": round(cost * 100, 3),
                            "days_held": days_held,
                            "exit_reason": "PARTIAL_CLOSE",
                            "entry_score": pos["score"],
                            "partial_close_pct": t_cfg.get("pct_of_position", 0),
                        })
                        triggered_set.add(t_idx)
                partial_triggered[sym] = triggered_set

            if pnl_pct <= sl_pct:
                days_held = (current_date - pos["entry_date"]).days
                cost = _calc_costs(entry_price, days_held)
                trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                    "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(current_price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                    "cost_pct": round(cost * 100, 3),
                    "days_held": days_held,
                    "exit_reason": "STOP_LOSS",
                    "entry_score": pos["score"],
                })
                del open_positions[sym]
                trailing_highs.pop(sym, None)
                if sym in trailing_sl:
                    del trailing_sl[sym]
                partial_triggered.pop(sym, None)
                continue

            if pnl_pct >= tp_pct:
                days_held = (current_date - pos["entry_date"]).days
                cost = _calc_costs(entry_price, days_held)
                trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                    "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(current_price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                    "cost_pct": round(cost * 100, 3),
                    "days_held": days_held,
                    "exit_reason": "TAKE_PROFIT",
                    "entry_score": pos["score"],
                })
                del open_positions[sym]
                trailing_highs.pop(sym, None)
                if sym in trailing_sl:
                    del trailing_sl[sym]
                partial_triggered.pop(sym, None)
                continue

            # v12: Time-Stop Exit (stale Position raus)
            if ts_enabled:
                days_held = (current_date - pos["entry_date"]).days
                if days_held >= ts_max_days and days_held >= ts_min_days \
                        and abs(pnl_pct) < ts_pnl_thresh:
                    cost = _calc_costs(entry_price, days_held)
                    trades.append({
                        "symbol": sym,
                        "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                        "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                        "entry_price": round(entry_price, 4),
                        "exit_price": round(current_price, 4),
                        "pnl_pct": round(pnl_pct * 100, 2),
                        "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                        "cost_pct": round(cost * 100, 3),
                        "days_held": days_held,
                        "exit_reason": "TIME_STOP",
                        "entry_score": pos["score"],
                    })
                    del open_positions[sym]
                    trailing_highs.pop(sym, None)
                    if sym in trailing_sl:
                        del trailing_sl[sym]
                    partial_triggered.pop(sym, None)
                    continue

        # 2. Score & neue Entries
        if len(open_positions) >= max_positions:
            continue

        vix_block = False
        vix_reduce_size = False
        if use_realistic_filters and vix_by_date_norm:
            vix_val = _get_vix_fast(current_date)
            if vix_val is not None:
                if vix_val > vix_crisis_threshold:
                    vix_block = True
                    filter_stats["vix_blocked"] += 1
                elif vix_val > vix_caution_threshold:
                    vix_reduce_size = True
                    filter_stats["vix_reduced"] += 1

        if vix_block:
            continue

        # v12: Tages-Regime aus VIX (bull/sideways/bear/unknown)
        cur_regime = "unknown"
        if rs_enabled and regime_by_date:
            try:
                date_key = current_date.date() if hasattr(current_date, "date") else current_date
                cur_regime = regime_by_date.get(date_key, "unknown")
            except Exception:
                cur_regime = "unknown"

        scored = []
        for sym, sd in symbol_data.items():
            if sym in open_positions:
                continue
            sym_idx = sd["dates_to_idx"].get(current_date, -1)
            if sym_idx < 0:
                continue
            score = sd["scores"][sym_idx]
            volatility = sd.get("volatilities", [5.0] * (sym_idx + 1))[sym_idx] if "volatilities" in sd else 5.0
            mr_strength = sd.get("mr_strengths", [0] * (sym_idx + 1))[sym_idx] if "mr_strengths" in sd else 0

            # v12 Regime-Strategien: Score-Modifier
            if rs_enabled and cur_regime != "unknown":
                sector = sd.get("sector", "unknown")
                if cur_regime == "bull":
                    # Momentum-Boost wenn 5d-Momentum positiv (approx via mom_5d aus closes)
                    if sym_idx >= 5:
                        mom5 = (sd["closes"][sym_idx] - sd["closes"][sym_idx - 5]) / max(sd["closes"][sym_idx - 5], 0.01) * 100
                        if mom5 > 0:
                            score += min(10, mom5 * rs_bull_boost)
                elif cur_regime == "sideways":
                    if mr_strength > 0:
                        score += mr_strength * rs_sideways_boost
                elif cur_regime == "bear":
                    if sector not in _DEFENSIVE:
                        score += rs_bear_penalty
                    if mr_strength > 10:
                        score += 3

            # v12 Meta-Label-Approximation: filtere Low-Score + High-Vol Setups
            if ml_enabled:
                if score < ml_min_score and volatility > ml_max_vol:
                    continue

            if score >= min_score:
                scored.append((sym, score, sd["closes"][sym_idx], current_date))

        scored.sort(key=lambda x: x[1], reverse=True)

        if vix_reduce_size:
            slots = max(1, (max_positions - len(open_positions)) // 2)
        else:
            slots = max_positions - len(open_positions)

        opened_today = 0
        for sym, score, price, date in scored:
            if opened_today >= slots:
                break

            if use_realistic_filters and earnings_blackouts:
                if _is_earnings_blackout_fast(sym, current_date):
                    filter_stats["earnings_blocked"] += 1
                    continue

            if use_realistic_filters:
                if not _check_sector_concentration_fast(sym):
                    filter_stats["sector_blocked"] += 1
                    continue

            open_positions[sym] = {
                "entry_price": price,
                "entry_date": date,
                "score": score,
                "sector": symbol_data[sym].get("sector", "unknown"),
            }
            opened_today += 1

    # Restliche Positionen am Ende schliessen
    for sym, pos in open_positions.items():
        sd = symbol_data.get(sym)
        if not sd or not sd["closes"]:
            continue
        last_price = sd["closes"][-1]
        last_date = sd["dates"][-1]
        pnl_pct = (last_price - pos["entry_price"]) / pos["entry_price"]
        days_held = (last_date - pos["entry_date"]).days if hasattr(last_date, "__sub__") else 0
        cost = _calc_costs(pos["entry_price"], max(days_held, 0))
        trades.append({
            "symbol": sym,
            "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
            "exit_date": last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)[:10],
            "entry_price": round(pos["entry_price"], 4),
            "exit_price": round(last_price, 4),
            "pnl_pct": round(pnl_pct * 100, 2),
            "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
            "cost_pct": round(cost * 100, 3),
            "days_held": max(days_held, 0),
            "exit_reason": "END_OF_DATA",
            "entry_score": pos["score"],
        })

    return trades


# ============================================================
# TRADE SIMULATION
# ============================================================

def simulate_trades(histories, config=None, use_realistic_filters=True,
                    vix_history=None, earnings_blackouts=None):
    """Simulate trades on historical data using scanner scoring logic.

    For each day, scores all assets. Opens BUY positions when score >= threshold.
    Manages SL/TP. Closes positions based on signals.

    Args:
        histories: dict {symbol: DataFrame} with OHLCV data
        config: strategy config dict
        use_realistic_filters: if True, apply VIX regime, earnings blackout,
                               and sector concentration filters (default True)
        vix_history: dict {date -> vix_close}, pre-downloaded VIX data.
                     If None and use_realistic_filters=True, filters that need
                     VIX are skipped gracefully.
        earnings_blackouts: dict {symbol -> set of blackout dates}.
                            If None and use_realistic_filters=True, earnings
                            filter is skipped gracefully.

    Returns:
        list of trade dicts with entry/exit prices, pnl, costs, etc.
    """
    if config is None:
        config = load_config()

    dt = config.get("demo_trading", {})
    sl_pct = dt.get("stop_loss_pct", -3) / 100       # e.g., -0.03
    tp_pct = dt.get("take_profit_pct", 5) / 100       # e.g., 0.05
    min_score = dt.get("min_scanner_score", 15)
    max_positions = dt.get("max_positions", 20)

    # Trailing SL Parameter
    lev_cfg = config.get("leverage", {})
    trailing_sl_pct = lev_cfg.get("trailing_sl_pct", 2.0) / 100
    trailing_activation_pct = lev_cfg.get("trailing_sl_activation_pct", 1.0) / 100

    # --- Realistic filter parameters ---
    rf_cfg = config.get("regime_filter", {})
    vix_crisis_threshold = rf_cfg.get("vix_crisis_threshold", 35)
    vix_caution_threshold = rf_cfg.get("vix_caution_threshold", 25)

    mc_cfg = config.get("market_context", {})
    earnings_buffer_before = mc_cfg.get("earnings_buffer_days_before", 3)
    earnings_buffer_after = mc_cfg.get("earnings_buffer_days_after", 1)

    risk_cfg = config.get("risk_management", {})
    max_positions_per_sector = risk_cfg.get("max_positions_per_sector", 4)
    max_sector_allocation_pct = risk_cfg.get("max_sector_allocation_pct", 35)

    # Fallback: if no data provided, disable individual filters gracefully
    if vix_history is None:
        vix_history = {}
    if earnings_blackouts is None:
        earnings_blackouts = {}

    # Stats for filter reporting
    filter_stats = {
        "vix_blocked": 0,
        "vix_reduced": 0,
        "earnings_blocked": 0,
        "sector_blocked": 0,
    }

    # Partial Close (TP Tranchen) Parameter
    tp_tranches_cfg = config.get("leverage", {}).get("tp_tranches", [])

    trades = []          # completed trades
    open_positions = {}  # symbol -> {entry_price, entry_date, score, sector}
    trailing_highs = {}  # symbol -> highest price since entry (uses intraday highs)
    trailing_sl = {}     # symbol -> trailing SL price level
    partial_triggered = {}  # symbol -> set of triggered tranche indices

    # We need all symbols to have aligned date indices
    # Get the common date range
    all_dates = None
    symbol_data = {}

    for sym, hist in histories.items():
        closes = hist["Close"].values.tolist()
        highs = hist["High"].values.tolist() if "High" in hist.columns else closes[:]
        volumes = hist["Volume"].values.tolist()
        dates = hist.index.tolist()
        symbol_data[sym] = {
            "closes": closes,
            "highs": highs,
            "volumes": volumes,
            "dates": dates,
        }

        if all_dates is None:
            all_dates = set(hist.index)
        else:
            all_dates = all_dates.union(set(hist.index))

    if not all_dates:
        return []

    sorted_dates = sorted(all_dates)
    start_idx = 60  # need lookback

    # Pre-build a date -> VIX lookup using normalized dates
    vix_by_date = {}
    if use_realistic_filters and vix_history:
        for vix_date, vix_val in vix_history.items():
            # Normalize: strip time/tz to compare with symbol dates
            if hasattr(vix_date, "normalize"):
                vix_by_date[vix_date.normalize()] = vix_val
            elif hasattr(vix_date, "date"):
                vix_by_date[vix_date] = vix_val
            else:
                vix_by_date[vix_date] = vix_val

    def _get_vix_for_date(dt_val):
        """Look up VIX for a given date, trying multiple key formats."""
        if not vix_by_date:
            return None
        # Direct lookup
        if dt_val in vix_by_date:
            return vix_by_date[dt_val]
        # Try normalized
        if hasattr(dt_val, "normalize"):
            norm = dt_val.normalize()
            if norm in vix_by_date:
                return vix_by_date[norm]
        # Try matching by date() part
        dt_date = dt_val.date() if hasattr(dt_val, "date") else dt_val
        for k, v in vix_by_date.items():
            k_date = k.date() if hasattr(k, "date") else k
            if k_date == dt_date:
                return v
        return None

    def _get_sector(sym):
        """Get sector for a symbol from ASSET_UNIVERSE."""
        info = ASSET_UNIVERSE.get(sym, {})
        return info.get("sector", "unknown")

    def _check_sector_concentration(new_sym):
        """Check if adding new_sym would violate sector limits.

        Returns True if allowed, False if blocked.
        """
        new_sector = _get_sector(new_sym)
        if not new_sector or new_sector == "unknown":
            return True

        sector_count = defaultdict(int)
        sector_value = defaultdict(float)
        total_value = 0.0

        for sym, pos in open_positions.items():
            sec = pos.get("sector", "unknown")
            sector_count[sec] += 1
            # Use entry price as proxy for position value (equal weight assumed)
            val = pos.get("entry_price", 1.0)
            sector_value[sec] += val
            total_value += val

        # Check count limit
        if sector_count[new_sector] >= max_positions_per_sector:
            return False

        # Check allocation limit
        if total_value > 0:
            # Estimate new position value as average of existing
            avg_val = total_value / max(len(open_positions), 1)
            new_sector_val = sector_value[new_sector] + avg_val
            new_total = total_value + avg_val
            if new_total > 0 and (new_sector_val / new_total * 100) > max_sector_allocation_pct:
                return False

        return True

    def _is_earnings_blackout(sym, current_date):
        """Check if symbol is in earnings blackout on current_date."""
        if sym not in earnings_blackouts:
            return False
        blackout_set = earnings_blackouts[sym]
        if not blackout_set:
            return False
        check_date = current_date.date() if hasattr(current_date, "date") else current_date
        return check_date in blackout_set

    for day_i in range(start_idx, len(sorted_dates)):
        current_date = sorted_dates[day_i]

        # 1. Check SL/TP for open positions
        for sym in list(open_positions.keys()):
            sd = symbol_data.get(sym)
            if not sd:
                continue
            # Find this date in the symbol's data
            try:
                sym_idx = sd["dates"].index(current_date)
            except ValueError:
                continue

            pos = open_positions[sym]
            current_price = sd["closes"][sym_idx]
            intraday_high = sd["highs"][sym_idx]
            entry_price = pos["entry_price"]
            pnl_pct = (current_price - entry_price) / entry_price

            # Improved Trailing SL: track intraday highs, not just close
            high_pnl_pct = (intraday_high - entry_price) / entry_price
            if high_pnl_pct >= trailing_activation_pct:
                # Update trailing high using intraday high
                if sym not in trailing_highs or intraday_high > trailing_highs[sym]:
                    trailing_highs[sym] = intraday_high
                # Compute trailing SL level from the tracked high
                trail_level = trailing_highs[sym] * (1 - trailing_sl_pct)
                if sym not in trailing_sl or trail_level > trailing_sl[sym]:
                    trailing_sl[sym] = trail_level

            if sym in trailing_sl and current_price <= trailing_sl[sym]:
                days_held = (current_date - pos["entry_date"]).days
                cost = _calc_costs(entry_price, days_held)
                trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                    "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(current_price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                    "cost_pct": round(cost * 100, 3),
                    "days_held": days_held,
                    "exit_reason": "TRAILING_SL",
                    "entry_score": pos["score"],
                })
                del open_positions[sym]
                trailing_highs.pop(sym, None)
                del trailing_sl[sym]
                partial_triggered.pop(sym, None)
                continue

            # Partial Close (TP Tranchen) Simulation
            if tp_tranches_cfg and pnl_pct > 0:
                triggered_set = partial_triggered.get(sym, set())
                for t_idx, t_cfg in enumerate(tp_tranches_cfg):
                    if t_idx in triggered_set:
                        continue
                    if pnl_pct * 100 >= t_cfg.get("profit_target_pct", 0):
                        close_pct_of_pos = t_cfg.get("pct_of_position", 0) / 100
                        days_held = (current_date - pos["entry_date"]).days
                        cost = _calc_costs(entry_price, days_held)
                        trades.append({
                            "symbol": sym,
                            "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                            "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                            "entry_price": round(entry_price, 4),
                            "exit_price": round(current_price, 4),
                            "pnl_pct": round(pnl_pct * 100, 2),
                            "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                            "cost_pct": round(cost * 100, 3),
                            "days_held": days_held,
                            "exit_reason": "PARTIAL_CLOSE",
                            "entry_score": pos["score"],
                            "partial_close_pct": t_cfg.get("pct_of_position", 0),
                        })
                        triggered_set.add(t_idx)
                partial_triggered[sym] = triggered_set

            # Stop Loss
            if pnl_pct <= sl_pct:
                days_held = (current_date - pos["entry_date"]).days
                cost = _calc_costs(entry_price, days_held)
                trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                    "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(current_price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                    "cost_pct": round(cost * 100, 3),
                    "days_held": days_held,
                    "exit_reason": "STOP_LOSS",
                    "entry_score": pos["score"],
                })
                del open_positions[sym]
                trailing_highs.pop(sym, None)
                if sym in trailing_sl:
                    del trailing_sl[sym]
                partial_triggered.pop(sym, None)
                continue

            # Take Profit
            if pnl_pct >= tp_pct:
                days_held = (current_date - pos["entry_date"]).days
                cost = _calc_costs(entry_price, days_held)
                trades.append({
                    "symbol": sym,
                    "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
                    "exit_date": current_date.strftime("%Y-%m-%d") if hasattr(current_date, "strftime") else str(current_date)[:10],
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(current_price, 4),
                    "pnl_pct": round(pnl_pct * 100, 2),
                    "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
                    "cost_pct": round(cost * 100, 3),
                    "days_held": days_held,
                    "exit_reason": "TAKE_PROFIT",
                    "entry_score": pos["score"],
                })
                del open_positions[sym]
                trailing_highs.pop(sym, None)
                if sym in trailing_sl:
                    del trailing_sl[sym]
                partial_triggered.pop(sym, None)
                continue

        # 2. Score all assets and look for new entries
        if len(open_positions) >= max_positions:
            continue

        # --- VIX Regime Filter ---
        vix_block = False
        vix_reduce_size = False
        if use_realistic_filters and vix_by_date:
            vix_val = _get_vix_for_date(current_date)
            if vix_val is not None:
                if vix_val > vix_crisis_threshold:
                    vix_block = True
                    filter_stats["vix_blocked"] += 1
                elif vix_val > vix_caution_threshold:
                    vix_reduce_size = True
                    filter_stats["vix_reduced"] += 1

        if vix_block:
            continue  # Skip all new entries on this day

        scored = []
        for sym, sd in symbol_data.items():
            if sym in open_positions:
                continue
            try:
                sym_idx = sd["dates"].index(current_date)
            except ValueError:
                continue
            score = _score_at_bar(sd["closes"], sd["volumes"], sym_idx)
            if score >= min_score:
                scored.append((sym, score, sd["closes"][sym_idx], current_date))

        # Sort by score, take best
        scored.sort(key=lambda x: x[1], reverse=True)

        # VIX caution: reduce available slots by 50%
        if vix_reduce_size:
            slots = max(1, (max_positions - len(open_positions)) // 2)
        else:
            slots = max_positions - len(open_positions)

        opened_today = 0
        for sym, score, price, date in scored:
            if opened_today >= slots:
                break

            # --- Earnings Blackout Filter ---
            if use_realistic_filters and earnings_blackouts:
                if _is_earnings_blackout(sym, current_date):
                    filter_stats["earnings_blocked"] += 1
                    continue

            # --- Sector Concentration Filter ---
            if use_realistic_filters:
                if not _check_sector_concentration(sym):
                    filter_stats["sector_blocked"] += 1
                    continue

            open_positions[sym] = {
                "entry_price": price,
                "entry_date": date,
                "score": score,
                "sector": _get_sector(sym),
            }
            opened_today += 1

    # Close remaining open positions at last available price
    for sym, pos in open_positions.items():
        sd = symbol_data.get(sym)
        if not sd or not sd["closes"]:
            continue
        last_price = sd["closes"][-1]
        last_date = sd["dates"][-1]
        pnl_pct = (last_price - pos["entry_price"]) / pos["entry_price"]
        days_held = (last_date - pos["entry_date"]).days if hasattr(last_date, "__sub__") else 0
        cost = _calc_costs(pos["entry_price"], max(days_held, 0))
        trades.append({
            "symbol": sym,
            "entry_date": pos["entry_date"].strftime("%Y-%m-%d") if hasattr(pos["entry_date"], "strftime") else str(pos["entry_date"])[:10],
            "exit_date": last_date.strftime("%Y-%m-%d") if hasattr(last_date, "strftime") else str(last_date)[:10],
            "entry_price": round(pos["entry_price"], 4),
            "exit_price": round(last_price, 4),
            "pnl_pct": round(pnl_pct * 100, 2),
            "pnl_net_pct": round((pnl_pct - cost) * 100, 2),
            "cost_pct": round(cost * 100, 3),
            "days_held": max(days_held, 0),
            "exit_reason": "END_OF_DATA",
            "entry_score": pos["score"],
        })

    if use_realistic_filters:
        log.info(f"Simulation fertig: {len(trades)} Trades | "
                 f"Filter-Stats: VIX blocked={filter_stats['vix_blocked']} days, "
                 f"VIX reduced={filter_stats['vix_reduced']} days, "
                 f"Earnings blocked={filter_stats['earnings_blocked']}, "
                 f"Sector blocked={filter_stats['sector_blocked']}")
    else:
        log.info(f"Simulation fertig: {len(trades)} Trades (no realistic filters)")
    return trades


def _calc_costs(entry_price, days_held):
    """Calculate total transaction cost as fraction of entry price."""
    spread = SPREAD_PCT * 2  # entry + exit
    overnight = OVERNIGHT_FEE_PCT * max(days_held, 0)
    slippage = SLIPPAGE_PCT * 2
    return spread + overnight + slippage


# ============================================================
# METRICS CALCULATION
# ============================================================

def calculate_metrics(trades, position_sizing=None):
    """Calculate performance metrics from a list of trades.

    Args:
        trades: list of trade dicts
        position_sizing: optional dict with kelly sizing config:
            {"kelly_fraction": 0.01, "max_concurrent": 20}
            Wenn gesetzt: Equity-Curve wird mit Position-Sizing gerechnet
            statt jeder Trade zu 100%. Realistischer fuer Live-Vergleich.

    Returns dict with: total_return, annual_return, sharpe_ratio,
    max_drawdown, win_rate, profit_factor, avg_trade_duration, total_trades
    """
    if not trades:
        return _empty_metrics()

    # v12: Bei Position-Sizing rechnen wir die Trade-Returns runter
    # auf Equity-Anteil. Beispiel: Trade gewinnt 10%, Position war 1%
    # der Equity → realer Equity-Return = 0.1%.
    if position_sizing:
        kelly_frac = position_sizing.get("kelly_fraction", 0.01)
        # Trades nach exit_date sortieren fuer sequentielle Equity-Update
        try:
            sorted_trades = sorted(trades, key=lambda t: t.get("exit_date", ""))
        except Exception:
            sorted_trades = trades
        net_returns = [(t["pnl_net_pct"] / 100) * kelly_frac for t in sorted_trades]
        gross_returns = [(t["pnl_pct"] / 100) * kelly_frac for t in sorted_trades]
        trades = sorted_trades
    else:
        net_returns = [t["pnl_net_pct"] / 100 for t in trades]
        gross_returns = [t["pnl_pct"] / 100 for t in trades]

    wins = [r for r in net_returns if r > 0]
    losses = [r for r in net_returns if r <= 0]

    total_return = 1.0
    for r in net_returns:
        total_return *= (1 + r)
    total_return -= 1

    # Time span
    dates = []
    for t in trades:
        try:
            dates.append(datetime.strptime(t["entry_date"], "%Y-%m-%d"))
            dates.append(datetime.strptime(t["exit_date"], "%Y-%m-%d"))
        except (ValueError, KeyError):
            pass

    if len(dates) >= 2:
        span_days = (max(dates) - min(dates)).days
        years = max(span_days / 365.25, 0.1)
    else:
        years = 1.0

    annual_return = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1

    # Sharpe Ratio (annualized, risk-free = 0)
    if len(net_returns) >= 2:
        mean_r = sum(net_returns) / len(net_returns)
        std_r = (sum((r - mean_r) ** 2 for r in net_returns) / (len(net_returns) - 1)) ** 0.5
        trades_per_year = len(net_returns) / years
        sharpe = (mean_r * trades_per_year) / (std_r * (trades_per_year ** 0.5)) if std_r > 0 else 0
    else:
        sharpe = 0

    # Max Drawdown
    equity = [1.0]
    for r in net_returns:
        equity.append(equity[-1] * (1 + r))
    peak = equity[0]
    max_dd = 0
    for val in equity:
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Win rate
    win_rate = len(wins) / len(net_returns) * 100 if net_returns else 0

    # Profit factor
    gross_profit = sum(wins) if wins else 0
    gross_loss = abs(sum(losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

    # Average duration
    avg_duration = sum(t.get("days_held", 0) for t in trades) / len(trades)

    # Total costs
    total_costs = sum(t.get("cost_pct", 0) for t in trades)

    return {
        "total_return_pct": round(total_return * 100, 2),
        "annual_return_pct": round(annual_return * 100, 2),
        "sharpe_ratio": round(sharpe, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "avg_trade_days": round(avg_duration, 1),
        "total_trades": len(trades),
        "total_costs_pct": round(total_costs, 2),
        "best_trade_pct": round(max(net_returns) * 100, 2) if net_returns else 0,
        "worst_trade_pct": round(min(net_returns) * 100, 2) if net_returns else 0,
        "avg_win_pct": round(sum(wins) / len(wins) * 100, 2) if wins else 0,
        "avg_loss_pct": round(sum(losses) / len(losses) * 100, 2) if losses else 0,
    }


def _empty_metrics():
    return {
        "total_return_pct": 0, "annual_return_pct": 0,
        "sharpe_ratio": 0, "max_drawdown_pct": 0,
        "win_rate_pct": 0, "profit_factor": 0,
        "avg_trade_days": 0, "total_trades": 0,
        "total_costs_pct": 0, "best_trade_pct": 0,
        "worst_trade_pct": 0, "avg_win_pct": 0, "avg_loss_pct": 0,
    }


# ============================================================
# EQUITY CURVE
# ============================================================

def build_equity_curve(trades):
    """Build daily equity curve from trade list.

    Returns list of [date_str, equity_value] pairs starting at 10000.
    """
    if not trades:
        return []

    sorted_trades = sorted(trades, key=lambda t: t.get("exit_date", ""))

    equity = 10000.0
    curve = []

    for t in sorted_trades:
        r = t["pnl_net_pct"] / 100
        equity *= (1 + r)
        curve.append([t["exit_date"], round(equity, 2)])

    return curve


# ============================================================
# MONTHLY RETURNS
# ============================================================

def calc_monthly_returns(trades):
    """Calculate monthly return percentages.

    Returns dict {"2021-01": 2.3, "2021-02": -1.1, ...}
    """
    monthly = defaultdict(list)

    for t in trades:
        exit_date = t.get("exit_date", "")
        if len(exit_date) >= 7:
            month_key = exit_date[:7]  # "YYYY-MM"
            monthly[month_key].append(t["pnl_net_pct"] / 100)

    result = {}
    for month, returns in sorted(monthly.items()):
        compound = 1.0
        for r in returns:
            compound *= (1 + r)
        result[month] = round((compound - 1) * 100, 2)

    return result


# ============================================================
# WALK-FORWARD VALIDATION
# ============================================================

def walk_forward_validate(histories, config=None, train_pct=0.8,
                          use_realistic_filters=True,
                          vix_history=None, earnings_blackouts=None):
    """Split history into train/test, run backtest on each.

    Args:
        histories: dict of DataFrames
        config: trading config
        train_pct: fraction for in-sample (default 0.80)
        use_realistic_filters: pass through to simulate_trades
        vix_history: pass through to simulate_trades
        earnings_blackouts: pass through to simulate_trades

    Returns:
        dict with in_sample and out_of_sample results
    """
    # Split each symbol's history
    train_histories = {}
    test_histories = {}

    for sym, hist in histories.items():
        n = len(hist)
        split = int(n * train_pct)
        if split < 100 or (n - split) < 30:
            continue
        train_histories[sym] = hist.iloc[:split]
        test_histories[sym] = hist.iloc[split:]

    if not train_histories or not test_histories:
        log.warning("Nicht genug Daten fuer Walk-Forward Validation")
        return None

    # Get date ranges
    train_dates = []
    test_dates = []
    for hist in train_histories.values():
        train_dates.extend([hist.index[0], hist.index[-1]])
    for hist in test_histories.values():
        test_dates.extend([hist.index[0], hist.index[-1]])

    train_start = min(train_dates).strftime("%Y-%m-%d") if train_dates else "?"
    train_end = max(train_dates).strftime("%Y-%m-%d") if train_dates else "?"
    test_start = min(test_dates).strftime("%Y-%m-%d") if test_dates else "?"
    test_end = max(test_dates).strftime("%Y-%m-%d") if test_dates else "?"

    log.info(f"Walk-Forward: Train {train_start} - {train_end}, Test {test_start} - {test_end}")

    # Run backtest on each
    sim_kwargs = {
        "use_realistic_filters": use_realistic_filters,
        "vix_history": vix_history,
        "earnings_blackouts": earnings_blackouts,
    }
    train_trades = simulate_trades(train_histories, config, **sim_kwargs)
    test_trades = simulate_trades(test_histories, config, **sim_kwargs)

    train_metrics = calculate_metrics(train_trades)
    test_metrics = calculate_metrics(test_trades)

    train_curve = build_equity_curve(train_trades)
    test_curve = build_equity_curve(test_trades)

    return {
        "in_sample": {
            "period": f"{train_start} - {train_end}",
            "metrics": train_metrics,
            "equity_curve": train_curve,
            "trades_count": len(train_trades),
        },
        "out_of_sample": {
            "period": f"{test_start} - {test_end}",
            "metrics": test_metrics,
            "equity_curve": test_curve,
            "trades_count": len(test_trades),
        },
    }


# ============================================================
# ORCHESTRATOR
# ============================================================

def quick_walk_forward(histories, config, use_realistic_filters=True,
                       vix_history=None, earnings_blackouts=None):
    """Schneller Walk-Forward nur fuer Optimizer Grid-Search (ohne Equity Curve/Monthly)."""
    train_histories = {}
    test_histories = {}

    for sym, hist in histories.items():
        n = len(hist)
        split = int(n * 0.8)
        if split < 100 or (n - split) < 30:
            continue
        train_histories[sym] = hist.iloc[:split]
        test_histories[sym] = hist.iloc[split:]

    if not train_histories or not test_histories:
        return None

    sim_kwargs = {
        "use_realistic_filters": use_realistic_filters,
        "vix_history": vix_history,
        "earnings_blackouts": earnings_blackouts,
    }
    test_trades = simulate_trades(test_histories, config, **sim_kwargs)
    test_metrics = calculate_metrics(test_trades)

    train_trades = simulate_trades(train_histories, config, **sim_kwargs)
    train_metrics = calculate_metrics(train_trades)

    return {
        "in_sample": {"metrics": train_metrics, "trades_count": len(train_trades)},
        "out_of_sample": {"metrics": test_metrics, "trades_count": len(test_trades)},
    }


def run_full_backtest(config=None, symbols=None, years=5,
                      use_realistic_filters=True):
    """Full backtest pipeline: download -> simulate -> metrics -> walk-forward.

    Saves results to backtest_results.json.

    Args:
        config: strategy config dict
        symbols: list of symbols to test
        years: years of history to download
        use_realistic_filters: apply VIX regime, earnings blackout,
                               and sector concentration filters

    Returns:
        dict with all results
    """
    log.info("=" * 55)
    log.info("FULL BACKTEST START")
    log.info(f"  Realistic Filters: {'ON' if use_realistic_filters else 'OFF'}")
    log.info("=" * 55)

    if config is None:
        config = load_config()

    # 1. Download history
    histories = download_history(symbols=symbols, years=years)
    if not histories:
        log.error("Keine historischen Daten verfuegbar")
        return {"error": "Keine historischen Daten"}

    # 1b. Download VIX history for regime filter
    vix_history = {}
    earnings_blackouts = {}
    if use_realistic_filters:
        log.info("Downloading realistic filter data...")
        vix_history = download_vix_history(years=years)

        # Fetch earnings dates for all stock/ETF symbols
        mc_cfg = config.get("market_context", {})
        buf_before = mc_cfg.get("earnings_buffer_days_before", 3)
        buf_after = mc_cfg.get("earnings_buffer_days_after", 1)

        for sym in histories.keys():
            info = ASSET_UNIVERSE.get(sym, {})
            asset_class = info.get("class", "")
            if asset_class in ("crypto", "forex", "commodities", "indices"):
                continue  # No earnings for these
            edates = _fetch_historical_earnings_dates(sym)
            if edates:
                earnings_blackouts[sym] = _build_earnings_blackout_set(
                    sym, edates, buf_before, buf_after)
                log.debug(f"  {sym}: {len(edates)} earnings dates, "
                          f"{len(earnings_blackouts[sym])} blackout days")

        log.info(f"Filter data ready: VIX={len(vix_history)} days, "
                 f"Earnings blackouts for {len(earnings_blackouts)} symbols")

    # Common kwargs for simulate_trades
    sim_kwargs = {
        "use_realistic_filters": use_realistic_filters,
        "vix_history": vix_history,
        "earnings_blackouts": earnings_blackouts,
    }

    # 2. Full simulation
    all_trades = simulate_trades(histories, config, **sim_kwargs)
    all_metrics = calculate_metrics(all_trades)
    equity_curve = build_equity_curve(all_trades)
    monthly_returns = calc_monthly_returns(all_trades)

    # 3. Walk-forward validation
    wf = walk_forward_validate(histories, config, **sim_kwargs)

    # 4. Best/Worst trades
    sorted_by_pnl = sorted(all_trades, key=lambda t: t["pnl_net_pct"], reverse=True)
    best_trades = sorted_by_pnl[:5]
    worst_trades = sorted_by_pnl[-5:]

    # 5. Compile results
    results = {
        "timestamp": datetime.now().isoformat(),
        "config_used": {
            "strategy": config.get("demo_trading", {}).get("strategy", "unknown"),
            "stop_loss_pct": config.get("demo_trading", {}).get("stop_loss_pct", -3),
            "take_profit_pct": config.get("demo_trading", {}).get("take_profit_pct", 5),
            "min_scanner_score": config.get("demo_trading", {}).get("min_scanner_score", 15),
            "max_positions": config.get("demo_trading", {}).get("max_positions", 20),
            "use_realistic_filters": use_realistic_filters,
        },
        "symbols_tested": list(histories.keys()),
        "years": years,
        "full_period": {
            "metrics": all_metrics,
            "equity_curve": equity_curve,
            "total_trades": len(all_trades),
        },
        "in_sample": wf["in_sample"] if wf else None,
        "out_of_sample": wf["out_of_sample"] if wf else None,
        "best_trades": best_trades,
        "worst_trades": worst_trades,
        "monthly_returns": monthly_returns,
        "cost_model": {
            "spread_pct": SPREAD_PCT * 100,
            "overnight_fee_pct": OVERNIGHT_FEE_PCT * 100,
            "slippage_pct": SLIPPAGE_PCT * 100,
        },
        "realistic_filters": {
            "enabled": use_realistic_filters,
            "vix_data_points": len(vix_history),
            "earnings_symbols_covered": len(earnings_blackouts),
        },
    }

    # 6. Save
    save_json("backtest_results.json", results)
    log.info(f"Backtest gespeichert: {all_metrics['total_trades']} Trades, "
             f"Return={all_metrics['total_return_pct']:+.2f}%, "
             f"Sharpe={all_metrics['sharpe_ratio']:.2f}, "
             f"MaxDD={all_metrics['max_drawdown_pct']:.1f}%")

    return results

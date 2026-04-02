"""
InvestPilot - Backtesting Engine
Replayed die Scanner-Scoring-Logik auf 5+ Jahren historischen Daten.
Simuliert Trades mit realistischen Transaktionskosten (Spread, Overnight).
Walk-Forward-Validierung: Train 80% / Test 20%.
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
# SCORING ON HISTORICAL DATA
# ============================================================

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
# TRADE SIMULATION
# ============================================================

def simulate_trades(histories, config=None):
    """Simulate trades on historical data using scanner scoring logic.

    For each day, scores all assets. Opens BUY positions when score >= threshold.
    Manages SL/TP. Closes positions based on signals.

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

    trades = []          # completed trades
    open_positions = {}  # symbol -> {entry_price, entry_date, entry_idx, score}
    trailing_sl = {}     # symbol -> highest trailing SL level

    # We need all symbols to have aligned date indices
    # Get the common date range
    all_dates = None
    symbol_data = {}

    for sym, hist in histories.items():
        closes = hist["Close"].values.tolist()
        volumes = hist["Volume"].values.tolist()
        dates = hist.index.tolist()
        symbol_data[sym] = {"closes": closes, "volumes": volumes, "dates": dates}

        if all_dates is None:
            all_dates = set(hist.index)
        else:
            all_dates = all_dates.union(set(hist.index))

    if not all_dates:
        return []

    sorted_dates = sorted(all_dates)
    start_idx = 60  # need lookback

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
            entry_price = pos["entry_price"]
            pnl_pct = (current_price - entry_price) / entry_price

            # Trailing SL Update + Check
            if pnl_pct >= trailing_activation_pct:
                trail_level = current_price * (1 - trailing_sl_pct)
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
                del trailing_sl[sym]
                continue

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
                if sym in trailing_sl:
                    del trailing_sl[sym]
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
                if sym in trailing_sl:
                    del trailing_sl[sym]
                continue

        # 2. Score all assets and look for new entries
        if len(open_positions) >= max_positions:
            continue

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
        slots = max_positions - len(open_positions)

        for sym, score, price, date in scored[:slots]:
            open_positions[sym] = {
                "entry_price": price,
                "entry_date": date,
                "score": score,
            }

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

    log.info(f"Simulation fertig: {len(trades)} Trades")
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

def calculate_metrics(trades):
    """Calculate performance metrics from a list of trades.

    Returns dict with: total_return, annual_return, sharpe_ratio,
    max_drawdown, win_rate, profit_factor, avg_trade_duration, total_trades
    """
    if not trades:
        return _empty_metrics()

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

def walk_forward_validate(histories, config=None, train_pct=0.8):
    """Split history into train/test, run backtest on each.

    Args:
        histories: dict of DataFrames
        config: trading config
        train_pct: fraction for in-sample (default 0.80)

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
    train_trades = simulate_trades(train_histories, config)
    test_trades = simulate_trades(test_histories, config)

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

def quick_walk_forward(histories, config):
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

    test_trades = simulate_trades(test_histories, config)
    test_metrics = calculate_metrics(test_trades)

    train_trades = simulate_trades(train_histories, config)
    train_metrics = calculate_metrics(train_trades)

    return {
        "in_sample": {"metrics": train_metrics, "trades_count": len(train_trades)},
        "out_of_sample": {"metrics": test_metrics, "trades_count": len(test_trades)},
    }


def run_full_backtest(config=None, symbols=None, years=5):
    """Full backtest pipeline: download -> simulate -> metrics -> walk-forward.

    Saves results to backtest_results.json.

    Returns:
        dict with all results
    """
    log.info("=" * 55)
    log.info("FULL BACKTEST START")
    log.info("=" * 55)

    if config is None:
        config = load_config()

    # 1. Download history
    histories = download_history(symbols=symbols, years=years)
    if not histories:
        log.error("Keine historischen Daten verfuegbar")
        return {"error": "Keine historischen Daten"}

    # 2. Full simulation
    all_trades = simulate_trades(histories, config)
    all_metrics = calculate_metrics(all_trades)
    equity_curve = build_equity_curve(all_trades)
    monthly_returns = calc_monthly_returns(all_trades)

    # 3. Walk-forward validation
    wf = walk_forward_validate(histories, config)

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
    }

    # 6. Save
    save_json("backtest_results.json", results)
    log.info(f"Backtest gespeichert: {all_metrics['total_trades']} Trades, "
             f"Return={all_metrics['total_return_pct']:+.2f}%, "
             f"Sharpe={all_metrics['sharpe_ratio']:.2f}, "
             f"MaxDD={all_metrics['max_drawdown_pct']:.1f}%")

    return results

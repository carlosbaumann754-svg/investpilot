# InvestPilot v5 Upgrade - Detailed Code Analysis

## Executive Summary

This document provides a comprehensive technical analysis of the six key InvestPilot modules for v5 planning. Each module is dissected to show function signatures, data flows, and optimal integration points for new features like regime filtering, multi-timeframe confirmation, trailing SL, dynamic position sizing, and drawdown recovery.

---

# 1. MARKET SCANNER (market_scanner.py)

## Overview
Scans all 80+ assets, calculates technical indicators, scores them on -100 to +100 scale, and identifies trade opportunities.

## Key Function Signatures

### `calc_rsi(prices, period=14)` — Lines 106-121
```python
def calc_rsi(prices, period=14):
    """Relative Strength Index berechnen."""
```
**Returns:** Float (0-100)  
**Data Flow:** Takes list of close prices → calculates deltas → accumulates gains/losses → computes RSI
**Current Logic:** Uses SMA (not EMA) for averaging. Conservative implementation.
**v5 Integration Point:** Already well-structured; can feed into regime-filtered signals.

### `calc_macd(prices, fast=12, slow=26, signal=9)` — Lines 124-139
```python
def calc_macd(prices, fast=12, slow=26, signal=9):
    """MACD berechnen (vereinfacht mit EMA)."""
```
**Returns:** Tuple (macd_line, signal_line, histogram)  
**Data Flow:** Calculates EMA(fast) and EMA(slow) → subtracts to get MACD → applies signal line EMA
**Current Logic:** Returns last values only; good for daily scans.
**v5 Integration Point:** Multi-timeframe requires storing MACD arrays, not just last values.

### `calc_bollinger_position(prices, period=20)` — Lines 142-156
```python
def calc_bollinger_position(prices, period=20):
    """Position relativ zu Bollinger Bands (0=unteres Band, 1=oberes Band)."""
```
**Returns:** Float (0.0-1.0) normalized position
**Data Flow:** Calculates SMA → StdDev → returns relative position
**Current Logic:** Simple but effective for mean-reversion signals.
**v5 Integration Point:** Can be enhanced with squeeze detection.

### `analyze_single_asset(symbol, asset_info)` — Lines 159-222
**Key Statistics Calculated:**
- RSI (Overbought/Oversold)
- MACD (Trend momentum)
- Bollinger Bands position (0-1)
- 5-day & 20-day momentum %
- Volume trend ratio
- Volatility (20-day historical)
- SMA 20 & SMA 50 (trend)
- Golden cross detection

**Returns:** Dict with all indicators
```python
{
    "symbol": str,
    "price": float,
    "rsi": float,
    "macd": float,
    "macd_signal": float,
    "macd_histogram": float,
    "bollinger_pos": float (0-1),
    "momentum_5d": float (%),
    "momentum_20d": float (%),
    "volatility": float (% annualized),
    "volume_trend": float (ratio),
    "above_sma20": bool,
    "above_sma50": bool,
    "golden_cross": bool,
}
```

**v5 Integration Points:**
1. **Regime Filter:** Add market_regime to skip bearish assets in bull markets
2. **VIX Check:** Add vix_level param to reduce volatility scores in high VIX
3. **Earnings Window:** Check if stock in earnings blackout period
4. **Data Reuse:** Cache 3-month history to avoid repeated yfinance calls

## Core Scoring Engine

### `score_asset(analysis, use_ml=False)` — Lines 225-284
**Current Logic Flow:**
```
Base Score = 0

1. RSI Signal (-20 to +20)
   < 30 (oversold)    → +20
   < 40              → +10
   > 70 (overbought) → -20
   > 60              → -5

2. MACD Signal (-15 to +15)
   histogram > 0     → +10
   macd > signal     → +5 (bullish cross)
   histogram < 0     → -10
   macd < signal     → -5 (bearish cross)

3. Momentum (-20 to +20)
   5d momentum * 2   → max(±10)
   20d momentum * 0.5 → max(±10)

4. Trend / SMA (-15 to +15)
   golden_cross      → +10
   above_sma20       → +5
   above_sma50       → +5
   (opposite = negative)

5. Bollinger (-10 to +10)
   < 0.2 (lower band) → +10 (buy)
   > 0.8 (upper band) → -10 (sell)

6. Volume Confirmation (-5 to +5)
   vol_trend > 1.2 && score > 0 → +5
   vol_trend > 1.2 && score < 0 → -5

7. Volatility Damper
   volatility > 5%   → score *= 0.9

Final Score Range: -100 to +100
```

**ML Path:** If use_ml=True, uses ml_scorer.score_asset_ml() instead
- Returns 0-100 probability
- Converted to -100 to +100 range: `(ml_score - 50) * 2`

**Signal Mapping:**
```python
score >=  25 → STRONG_BUY
score >=  10 → BUY
score <= -25 → STRONG_SELL
score <= -10 → SELL
else         → HOLD
```

## v5 Upgrade Point: WHERE REGIME FILTER PLUGS IN

**Proposed Addition After Line 225:**
```python
def score_asset(analysis, use_ml=False, regime_filter=None):
    # NEW: Regime-based pre-filtering
    if regime_filter and regime_filter.get("enabled"):
        regime = regime_filter.get("market_regime", "unknown")
        
        # Skip oversold assets in strong bull markets
        if regime == "strong_bull" and analysis["rsi"] < 40:
            return -50  # Weak signal in this regime
        
        # Skip overbought assets in bear markets
        if regime == "bear" and analysis["rsi"] > 60:
            return 50   # Weak signal
```

## Multi-Timeframe Analysis

### `analyze_multi_timeframe(symbol, asset_info)` — Lines 311-389
**Currently Exists but Limited:**

Uses three timeframes via yfinance:
- **1H Chart:** Last 1 month, trend direction via SMA crossover, RSI
- **15M Chart:** Last 5 days, entry signal via RSI + MACD
- **5M Chart:** Last 1 day, suggested SL based on recent low

**Returns:**
```python
{
    "trend_1h": "up"|"down"|"neutral",
    "rsi_1h": float,
    "entry_15m": "buy"|"sell"|"neutral",
    "rsi_15m": float,
    "suggested_sl_pct": float,
    "mtf_aligned": bool,  # All timeframes agree
}
```

**v5 Improvement Points:**
1. **Expand Timeframes:** Add daily (1D) for macro trend
2. **Store Array Data:** Currently only uses last values; MTF should keep arrays
3. **Weighted Scoring:** Give higher weight to aligned timeframes
4. **Strength Scoring:** Measure how strong alignment is (0-100%)

### `enrich_with_mtf(scan_results, top_n=20)` — Lines 392-423

**Current Flow:**
1. Takes top 20 scan results
2. Calls analyze_multi_timeframe() on each
3. If `mtf_aligned=true`, adds 15% bonus to score
4. Sleeps 0.5s between requests (rate limiting)

**v5 Enhancement:** Can be expanded to score strength of alignment (weak/medium/strong) instead of binary.

---

## Integration Architecture for v5

```
market_scanner.py
├── scan_all_assets()
│   ├── Loop through assets
│   ├── analyze_single_asset()        ← Gets raw indicators
│   │   └── calc_rsi(), calc_macd(), calc_bollinger_position()
│   ├── score_asset()                 ← Scores -100 to +100
│   │   ├── [NEW] regime_filter()     ← Pre-filter by market regime
│   │   └── [NEW] vix_adjustment()    ← Dampen volatile markets
│   └── enrich_with_mtf()             ← Multi-timeframe confirmation
│       └── analyze_multi_timeframe() ← 1H/15M/5M analysis
│
├── ASSET_UNIVERSE dict
│   ├── yfinance symbol mapping
│   └── eToro instrument IDs
│
└── get_top_opportunities()           ← Export to trader.py
```

---

# 2. TRADER (trader.py) — TRADE EXECUTION ENGINE

## Architecture Overview

The trader module orchestrates:
1. **Risk validation** (via risk_manager)
2. **Leverage selection** (via leverage_manager)
3. **Execution** (via execution module)
4. **Order management** (eToro API client)
5. **Alert notifications** (via alerts)

## Critical Integration Point: Where Things Flow

```
trader.py (lines 1-50+)
├── execute_trade()           [INCOMPLETE - partially shown]
│   ├── Risk validation
│   │   ├── check_drawdown_limits()
│   │   ├── validate_trade()
│   │   └── check_margin_safety()
│   │
│   ├── Position Sizing
│   │   ├── calculate_position_size()      [FROM risk_manager]
│   │   └── calculate_leveraged_position_size()
│   │
│   ├── Leverage Selection
│   │   ├── calculate_optimal_leverage()   [FROM leverage_manager]
│   │   └── snap_to_allowed()
│   │
│   ├── SL/TP Calculation
│   │   ├── entry_price - sl_pct           [HARDCODED IN LOGIC]
│   │   └── calculate_tp_tranches()        [FROM leverage_manager]
│   │
│   ├── Trade Execution
│   │   ├── etoro_client.open_position()
│   │   └── execution.track_trade()
│   │
│   └── Order Management
│       ├── Update trailing SL            [FROM leverage_manager]
│       ├── Monitor TP tranches           [NOT FULLY IMPLEMENTED]
│       └── Check Stop Conditions
│
└── monitor_positions()
    ├── fetch portfolio
    ├── check_trailing_stop_losses()      [FROM leverage_manager]
    ├── check_tp_tranches_hit()
    ├── update_risk_state()               [FROM risk_manager]
    └── execute closes
```

## Where SL/TP Are Set (Across Multiple Modules)

### Risk Manager (risk_manager.py)
**calculate_position_size()** — Lines 104-125
```python
def calculate_position_size(portfolio_value, stop_loss_pct, config=None):
    """Position = (Portfolio * Risk%) / |Stop-Loss%|
    
    Example: $100k * 2% risk / 3% SL = $666 max loss
    Position Size = $100k * 2% / 3% = $66,666 exposure
    """
    risk_per_trade_pct = risk_cfg.get("risk_per_trade_pct", 2.0)  # Config default
    max_single_trade = config.get("demo_trading", {}).get("max_single_trade_usd", 5000)
    
    if stop_loss_pct == 0:
        stop_loss_pct = -3  # Fallback hardcoded default
    
    max_risk_usd = portfolio_value * (risk_per_trade_pct / 100)
    position_size = max_risk_usd / (abs(stop_loss_pct) / 100)
    position_size = min(position_size, max_single_trade)
    position_size = min(position_size, portfolio_value * max_position_pct / 100)
    
    return round(max(position_size, 0), 2)
```

**Key Insight:** Position sizing depends on SL percentage, but SL is NOT calculated here.
**Current Default:** -3% (hardcoded) if not provided.

### Leverage Manager (leverage_manager.py)
**calculate_tp_tranches()** — Lines 207-231
```python
def calculate_tp_tranches(entry_price, total_amount, config=None):
    """Gestaffelte Gewinne: 50% bei 3%, 30% bei 6%, 20% laufen lassen"""
    
    tranches = lev_cfg.get("tp_tranches", [
        {"pct_of_position": 50, "profit_target_pct": 3},
        {"pct_of_position": 30, "profit_target_pct": 6},
        {"pct_of_position": 20, "profit_target_pct": 10},
    ])
    
    return [{
        "target_price": entry_price * (1 + profit_target_pct / 100),
        "amount_usd": total_amount * pct_of_position / 100,
        ...
    }]
```

**Current State:** TP tranches are CALCULATED but NOT AUTOMATICALLY MANAGED by trader.py
**v5 Work Needed:** Monitor & execute TP partial closes

### Where SL Is Actually Set: IMPLICIT, SCATTERED

**In trader.py (implied):**
- Uses risk_manager's default -3% if not specified
- No explicit SL calculation function found in core logic
- Likely passed to eToro API but not shown in provided excerpt

**v5 Critical Issue:** SL calculation logic is MISSING or INCOMPLETE in trader.py

---

## Trailing Stop-Loss (Leverage Manager)

### `update_trailing_stop_loss()` — Lines 172-203
```python
def update_trailing_stop_loss(position_id, current_price, entry_price, 
                               leverage=1, config=None):
    """Trailing SL moves UP for longs (never down)
    
    Activation: Only when P/L >= activation_pct (default 1%)
    Trail Distance: Default 2% below current price
    """
    trail_pct = lev_cfg.get("trailing_sl_pct", 2.0)         # 2% default
    activation_pct = lev_cfg.get("trailing_sl_activation_pct", 1.0)  # 1% gain
    
    pnl_pct = (current_price - entry_price) / entry_price * 100
    
    # Only activate if in profit
    if pnl_pct < activation_pct:
        return None
    
    trail_level = current_price * (1 - trail_pct / 100)
    
    # Only increase SL, never decrease
    if pid in state:
        old_level = state[pid].get("sl_level", 0)
        if trail_level > old_level:
            state[pid]["sl_level"] = round(trail_level, 4)
            return trail_level
        else:
            return old_level
    else:
        state[pid] = {"sl_level": round(trail_level, 4), ...}
        return trail_level
```

**Stored In:** `trailing_sl_state.json`
**Key Limitation:** Requires manual polling - no automatic trigger detection

### `check_trailing_stop_losses()` — Lines 206-228
```python
def check_trailing_stop_losses(positions):
    """Prüfe ob Trailing SL hit wurde"""
    
    triggered = []
    for pos in positions:
        pid = str(pos.get("position_id", ""))
        if pid not in state:
            continue
        
        current_price = pos.get("current_price", 0)
        sl_level = state[pid].get("sl_level", 0)
        
        if current_price <= sl_level:
            triggered.append({...})
    
    return triggered  # List of positions to close
```

**v5 Integration Point:** This already works! Just needs to be called in monitor_positions() loop.

---

## Dynamic Position Sizing (Risk Manager)

### Current Implementation — Lines 104-175

**calculate_position_size()** — Base algorithm
- Input: portfolio_value, stop_loss_pct
- Uses: Risk % per trade (default 2%)
- Returns: Position size in USD

**calculate_leveraged_position_size()** — With leverage adjustment
```python
def calculate_leveraged_position_size(portfolio_value, stop_loss_pct, 
                                       leverage, config=None):
    """Position Sizing mit Hebel: Effektives Risiko = Verlust * Hebel"""
    effective_sl = stop_loss_pct * leverage
    base_size = calculate_position_size(portfolio_value, effective_sl, config)
    return round(base_size / leverage, 2)
```

**Current Flow:**
1. Takes stop-loss percentage
2. Divides portfolio risk by SL percentage
3. Caps at max single trade ($5,000) and max portfolio % (10%)

**v5 Enhancement:** Add market regime & volatility adjustments
```python
# NEW: Regime-based risk adjustment
if market_regime == "bear":
    risk_per_trade_pct *= 0.5  # 2% → 1% in bear
elif market_regime == "high_vol":
    risk_per_trade_pct *= 0.75  # 2% → 1.5% in high vol

# NEW: VIX-based adjustment
if vix_level > 30:
    position_size *= 0.5  # Halve position in extreme fear
```

---

## Cost Filter (eToro Spreads & Fees)

### `estimate_transaction_costs()` — Lines 289-303
```python
def estimate_transaction_costs(amount_usd, asset_class, is_leveraged=False):
    """Spread + Overnight Fees"""
    
    ETORO_SPREADS = {
        "stocks": 0.09,      # 0.09%
        "crypto": 1.0,       # 1%
        "forex": 0.01,       # 1 pip
        "commodities": 0.05,
    }
    
    spread_cost = amount_usd * spread_pct / 100
    
    if is_leveraged:
        overnight_daily = amount_usd * 0.02 / 100  # ~0.02% per night
    
    return {
        "spread_cost": spread_cost,
        "overnight_daily": overnight_daily,
        "overnight_weekly": overnight_daily * 5,
        "overnight_weekend_3x": overnight_daily * 3,  # Friday counts as 3x
        "total_entry_cost": spread_cost,
    }
```

**v5 Integration Point:** Use this in score_asset() to reject low-confidence trades where spread costs exceed potential profit.

### `adjust_profit_target_for_costs()` — Lines 306-316
```python
def adjust_profit_target_for_costs(take_profit_pct, asset_class, leverage=1):
    """Ensure TP > roundtrip costs
    
    Example: For stocks with 0.09% spread
    - Roundtrip = 2 * 0.09% = 0.18%
    - With 2x leverage: 0.36%
    - Min TP = 0.36% * 2 = 0.72%
    """
    spread_pct = ETORO_SPREADS.get(asset_class, 0.1)
    roundtrip_cost = spread_pct * 2
    effective_cost = roundtrip_cost * leverage
    min_tp = effective_cost * 2
    return max(take_profit_pct, min_tp)
```

**v5 Proposal:** Add to trader's TP calculation:
```python
# After calculate_tp_tranches()
for tranche in tranches:
    tranche["profit_target_pct"] = adjust_profit_target_for_costs(
        tranche["profit_target_pct"],
        asset_class,
        leverage
    )
```

---

## Overnight Risk Management

### `check_overnight_risk()` — Lines 318-340
```python
def check_overnight_risk(positions, config=None):
    """Identify positions that should close before market close"""
    
    to_close = []
    for pos in positions:
        cls = pos.get("asset_class", "stocks")
        leverage = pos.get("leverage", 1)
        
        # Crypto trades 24/7 - always exempt
        if cls == "crypto":
            continue
        
        # Close leveraged positions overnight
        if close_leveraged_overnight and leverage > 1:
            to_close.append(pos)
        
        # Close stocks if flag set
        elif close_stocks_overnight and cls == "stocks":
            to_close.append(pos)
    
    return to_close
```

### `check_weekend_fee_impact()` — Lines 343-373
```python
def check_weekend_fee_impact(positions, config=None):
    """Close leveraged positions Friday if fees > profit"""
    
    to_close = []
    for pos in positions:
        leverage = pos.get("leverage", 1)
        if leverage <= 1:
            continue
        
        weekend_cost_pct = costs["overnight_weekend_3x"] / invested * 100
        
        # Close if fee > remaining profit
        if pnl_pct < weekend_cost_pct or pnl_pct < 0:
            to_close.append(pos)
    
    return to_close
```

**v5 Integration:** Call in end-of-day monitoring:
```python
# Before market close on Friday
weekend_closes = check_weekend_fee_impact(positions)
for pos in weekend_closes:
    log.info(f"Closing {pos['symbol']} - Weekend fees > profit")
    etoro_client.close_position(pos['position_id'])
```

---

# 3. BRAIN (brain.py) — SELF-LEARNING ANALYSIS

## Data Structure

### `load_brain()` — Lines 22-43
```python
{
    "version": 2,
    "created": "2026-04-01...",
    "total_runs": int,
    
    # Core data
    "performance_snapshots": [
        {
            "date": "2026-04-01",
            "run_number": 42,
            "credit": float,
            "invested": float,
            "unrealized_pnl": float,
            "total_value": float,
            "num_positions": int,
            "positions": [{
                "instrument_id": int,
                "invested": float,
                "pnl": float,
                "pnl_pct": float,
                "leverage": int,
            }],
        }
    ],
    
    "instrument_scores": {
        "instrument_id": {
            "score": float,
            "avg_return_pct": float,
            "volatility": float,
            "trend": float,
            "consistency": float (0-100%),
            "sharpe": float,
            "days_held": int,
            "latest_pnl_pct": float,
        }
    },
    
    "learned_rules": [{
        "type": "INCREASE_ALLOCATION" | "DECREASE_ALLOCATION" | "REGIME_ADJUSTMENT" | "TIGHTEN_STOP_LOSS",
        "instrument_id": int,
        "symbol": str,
        "reason": str,
        "suggested_change_pct": float,
        "confidence": float (0-1),
        "created": "2026-04-01...",
    }],
    
    "market_regime": "bull" | "bear" | "sideways" | "unknown",
    "win_rate": float (0-100),
    "avg_return_pct": float,
    "sharpe_estimate": float,
    "best_performers": [iid, iid, iid],
    "worst_performers": [iid, iid, iid],
}
```

## Brain Cycle Architecture

### 1. `record_snapshot()` — Lines 60-88
**Stores:** Portfolio state at execution time
**Frequency:** Called once per trader run (daily/4-hourly)
**Data Saved:**
- Cash balance
- Total invested
- Unrealized P/L
- Position list with ID, invested amount, P/L%, leverage

**v5 Enhancement:** Add market context snapshot
```python
# NEW: Add market regime & VIX at capture time
snapshot["market_regime"] = brain.get("market_regime", "unknown")
snapshot["vix_level"] = get_current_context().get("vix_level")
```

### 2. `analyze_instrument_performance()` — Lines 110-161
**Calculates:** Score for each instrument based on historical performance
**Input:** All performance_snapshots
**Score Formula:**
```
score = (
    avg_return * 0.25 +           # 25% weight to returns
    trend * 0.20 +                # 20% weight to trend
    consistency * 30 +            # 30% weight to win rate (%) 
    sharpe * 10 +                 # 10% weight to risk-adj return
    (10 if latest_pnl > 0 else -5) # Current direction
)
```

**Outputs:**
- Best 3 performers
- Worst 3 performers
- Full scoring for all instruments

### 3. `detect_market_regime()` — Lines 164-195
**Current Logic:**
```python
recent = snapshots[-10:]  # Last 10 runs
changes = [(values[i] - values[i-1]) / values[i-1] * 100
           for i in range(1, len(values))]

avg_change = mean(changes)
positive_ratio = sum(1 for c in changes if c > 0) / len(changes)

if avg_change > 0.3 and positive_ratio > 0.6:
    regime = "bull"
elif avg_change < -0.3 and positive_ratio < 0.4:
    regime = "bear"
else:
    regime = "sideways"
```

**v5 Enhancement:** Integrate external VIX + Fear/Greed data
```python
# NEW: Cross-check with market_context
ctx = get_current_context()
vix = ctx.get("vix_level")
fear_greed = ctx.get("fear_greed_index")

# Override regime if extreme market conditions
if vix > 30 and avg_change < 0:
    regime = "panic"  # New regime type
elif fear_greed > 75 and avg_change > 0:
    regime = "euphoria"  # New regime type
```

### 4. `learn_rules()` — Lines 210-282
**Generates:** 4 types of adaptive rules

**Rule Type 1: INCREASE_ALLOCATION** (Lines 219-233)
- Condition: Top performer with score > 15 AND consistency > 60%
- Action: Suggest +2% allocation
- Confidence: min(consistency/100, 0.9)

**Rule Type 2: DECREASE_ALLOCATION** (Lines 236-250)
- Condition: Worst performer with score < -10 AND trend < -2
- Action: Suggest -2% allocation
- Confidence: min(|score|/30, 0.8)

**Rule Type 3: REGIME_ADJUSTMENT** (Lines 253-265)
- Bull Market: Reduce cash, increase growth positions (confidence 0.6)
- Bear Market: Increase cash, defensively position (confidence 0.6)

**Rule Type 4: TIGHTEN_STOP_LOSS** (Lines 268-278)
- Condition: Min recent P/L < -15%
- Action: Suggest tighter SL at max_loss * 0.7
- Confidence: 0.7

### 5. `optimize_strategy()` — Lines 285-334
**Applies:** High-confidence rules to config
**Filtering:** Only uses rules with confidence >= 0.7
**Processing:** Last 5 high-confidence rules
**Rebalancing:** Normalizes allocations back to 100%

### 6. `walk_forward_validate()` — Lines 502-533
**Validation:** Test-of-concept for Walk-Forward Analysis
**Split:** 80% training, 20% out-of-sample test
**Logic:**
```python
if test_return < -2 and rule.type == "INCREASE_ALLOCATION":
    REJECT rule
else:
    ACCEPT rule
```

**v5 Improvement:** Currently too simplistic. Should:
- Compare rule performance on train vs test
- Reject if out-of-sample sharpe degrades
- Implement proper parameter sweep (Optimization Space)

### 7. `run_brain_cycle()` — Lines 539-600
**Complete 7-step workflow:**
1. Record snapshot
2. Analyze instruments
3. Detect regime
4. Learn rules
5. Walk-forward validation
6. Optimize strategy
7. Generate report

**Called From:** trader.py main loop after each trading run

---

## Market Regime Data Available

From `brain.py`:
```python
brain["market_regime"]  # "bull" | "bear" | "sideways" | "unknown"
```

From `market_context.py` (integrated in v5):
```python
{
    "vix_level": float,                    # Current VIX
    "vix_regime": "low_vol" | "normal" | "elevated" | "high_fear",
    "fear_greed_index": int (0-100),
    "fear_greed_class": str,               # "Extreme Fear" ... "Extreme Greed"
    "sentiment_adjustment": float (-1 to +1),
    "market_regime": str,                  # From portfolio analysis
    "btc_dominance_proxy": float,          # BTC vs ETH performance
    "avoid_altcoins": bool,
    "macro_events_today": [],
    "high_impact_window": bool,
    "position_size_multiplier": float,     # 0-1 adjustment
}
```

---

# 4. MARKET CONTEXT (market_context.py)

## Data Structure & Sources

### `_load_context()` — Lines 32-42
```python
{
    "vix_level": float | None,
    "vix_regime": str,                     # "low_vol" | "normal" | "elevated" | "high_fear"
    "fear_greed_index": int (0-100) | None,
    "fear_greed_class": str,               # Classification string
    "sentiment_adjustment": float,         # -1 to +1
    "market_regime": str,                  # From brain
    "btc_dominance_proxy": float | None,
    "avoid_altcoins": bool,
    "macro_events_today": [],
    "high_impact_window": bool,
    "position_size_multiplier": float,     # 0-1
    "last_update": str (ISO timestamp),
}
```

## VIX Monitoring

### `fetch_vix()` — Lines 55-67
- Source: yfinance ("^VIX")
- Frequency: Fetches 5-day history, returns latest
- Returns: Float or None

### `get_vix_regime()` — Lines 70-85
**Classification:**
```
< 15  → "low_vol"      (Complacency - may precede selloff)
15-20 → "normal"
20-30 → "elevated"     (Caution)
30+   → "high_fear"    (Reduce positions)
```

**v5 Integration:** Use this to filter scanner results
```python
# In score_asset(), add:
if vix_regime == "high_fear" and signal_score < 0:
    return score * 0.5  # Weak sell signals unreliable in panic
```

## Sentiment & Fear/Greed Index

### `fetch_fear_greed()` — Lines 104-120
- Source: Alternative.me API (free, public)
- Endpoint: `https://api.alternative.me/fng/?limit=1`
- Returns: Dict with value (0-100) and classification

### `get_sentiment_adjustment()` — Lines 123-141
**Adjustments:**
```
< 25  (Extreme Fear)  → +0.3   (Contrarian buy signal)
25-45 (Fear)          → -0.1
45-55 (Neutral)       → 0
55-75 (Greed)         → -0.1
> 75  (Extreme Greed) → -0.3   (Sell caution)
```

**v5 Usage:** Apply to position sizing
```python
size_multiplier = 1.0
if fear_greed < 25:
    size_multiplier *= 1.2  # Increase size in extreme fear
elif fear_greed > 75:
    size_multiplier *= 0.6  # Reduce size in extreme greed
```

## Economic Calendar & Macro Events

### `fetch_economic_calendar()` — Lines 154-189
- Static heuristic: NFP = 1st Friday of month
- Could integrate external API (Trading Economics, Alpha Vantage)
- Falls back to manual_events from config

**Recurring Events (RECURRING_MACRO_EVENTS dict):**
```python
{
    "FOMC": {"impact": "high", "affected_classes": [...]},
    "NFP": {"impact": "high"},
    "CPI": {"impact": "high"},
    "ECB": {"impact": "high", "affected_classes": ["forex", "indices"]},
    "SNB": {"impact": "medium", "affected_classes": ["forex"]},
    "BOJ": {"impact": "medium", "affected_classes": ["forex"]},
}
```

### `is_high_impact_event_window()` — Lines 192-198
```python
def is_high_impact_event_window(events=None):
    """Reduce positions 2h before and 1h after high-impact events"""
    high_impact = [e for e in events if e.get("impact") == "high"]
    return len(high_impact) > 0
```

### `get_position_size_multiplier()` — Lines 201-221
**Combines all factors:**
```
multiplier = 1.0

if high_impact_event:
    multiplier *= 0.5

if vix > 30:
    multiplier *= 0.5
elif vix > 25:
    multiplier *= 0.75

return multiplier  # 0.0-1.0
```

## Crypto Regime (BTC Dominance)

### `fetch_btc_dominance()` — Lines 244-273
- Uses BTC vs ETH performance as proxy (yfinance doesn't provide direct dominance)
- Returns: btc_change - eth_change (percentage points)
- Interpretation: If BTC +10% and ETH +0%, BTC dominance = 10% (Bitcoin dominating)

### `should_avoid_altcoins()` — Lines 276-280
```python
def should_avoid_altcoins(btc_dominance_proxy):
    """Altcoins underperforming -> skip altcoin trades"""
    return btc_dominance_proxy > 10  # BTC outperforming by 10%
```

**v5 Integration:** In scanner, filter crypto
```python
if asset_class == "crypto" and should_avoid_altcoins(ctx["btc_dominance_proxy"]):
    if "ETH" in symbol or symbol not in ["BTC", "USDT", "USDC"]:
        return None  # Skip non-BTC altcoins in BTC-dominant regime
```

## Earnings Calendar (Stock Filter)

### `check_earnings_window()` — Lines 283-319
```python
def check_earnings_window(symbol):
    """Skip stocks 3 days before and 1 day after earnings"""
    
    ticker = yf.Ticker(symbol)
    earnings_date = ticker.calendar.get("Earnings Date")
    
    days_until = (earnings_dt - now).days
    
    if -1 <= days_until <= 3:
        return True, earnings_dt.strftime("%Y-%m-%d")  # IN BLACKOUT
    
    return False, earnings_dt.strftime("%Y-%m-%d")     # OK TO TRADE
```

**v5 Integration:** In analyze_single_asset(), check earnings
```python
in_earnings, date = check_earnings_window(symbol)
if in_earnings:
    log.warning(f"  {symbol} in earnings window ({date}) - lower confidence score")
    return None  # Skip this asset
```

## Seasonality (Commodities)

### `get_seasonal_adjustment()` — Lines 364-392
**Hardcoded Patterns:**
```
Gold:
  Q4/Q1 (Oct-Feb) → +0.2 (bullish)
  Summer (Jun-Aug) → -0.1 (bearish)

Oil:
  Driving season (May-Jul) → +0.15
  Heating season (Nov-Jan) → +0.1

Natural Gas:
  Winter demand (Oct-Jan) → +0.2
  Shoulder (Apr-Jun) → -0.15
```

**v5 Proposal:** Use this to boost/dampen seasonal commodity signals
```python
if asset_class == "commodities":
    seasonal_adj = get_seasonal_adjustment(asset_class, symbol)
    score += seasonal_adj * 10  # Add seasonal adjustment to score
```

## Full Context Update

### `update_full_context()` — Lines 336-361
**Single call updates everything:**
1. Fetch VIX
2. Fetch Fear/Greed
3. Fetch Macro Calendar
4. Fetch BTC Dominance
5. Calculate position_size_multiplier
6. Save all to cache

**Called:** Once per hour (rate limiting)
**Caching:** Returns immediately if called again within 3600s

---

# 5. RISK MANAGER (risk_manager.py)

## Position Sizing Algorithm

### `calculate_position_size()` — Lines 104-125

**Formula:**
```
Position Size = (Portfolio Value × Risk %) / |Stop-Loss %|

Example:
Portfolio = $100,000
Risk per trade = 2%
Stop Loss = -3%

Max Risk = $100,000 × 2% = $2,000
Position = $2,000 / 3% = $66,667 exposure
But cap at $5,000 max or 10% portfolio = $10,000

Final Position = $5,000
```

**Constraints:**
1. Never exceed config's "max_single_trade_usd" (default $5,000)
2. Never exceed config's "max_single_position_pct" (default 10%)
3. Risk_per_trade_pct from config (default 2%)
4. Fallback SL if not provided: -3%

**v5 Enhancement Points:**
1. **Regime Adjustment:** Reduce risk_per_trade in bear markets
2. **VIX Adjustment:** Reduce risk at high VIX
3. **Drawdown Recovery Mode:** Increase risk when coming out of drawdown (catch-up trades)

### Proposed v5 Implementation:
```python
def calculate_position_size(portfolio_value, stop_loss_pct, config=None,
                            market_regime="unknown", vix_level=None, 
                            drawdown_recovery=False):
    # Base risk
    risk_per_trade_pct = risk_cfg.get("risk_per_trade_pct", 2.0)
    
    # Regime adjustment
    if market_regime == "bear":
        risk_per_trade_pct *= 0.5  # 2% → 1%
    elif market_regime == "bull":
        risk_per_trade_pct *= 1.2  # 2% → 2.4% (slight boost)
    
    # VIX adjustment
    if vix_level is not None:
        if vix_level > 30:
            risk_per_trade_pct *= 0.5
        elif vix_level > 20:
            risk_per_trade_pct *= 0.75
    
    # Drawdown recovery: slightly increase size to catch up
    if drawdown_recovery:
        risk_per_trade_pct *= 1.1  # 2% → 2.2%
    
    # ... rest of calculation
```

## Drawdown Tracking & Auto-Pause

### `update_portfolio_tracking()` — Lines 76-118
**Tracks:**
```python
{
    "daily_pnl_usd": float,
    "daily_pnl_pct": float,
    "weekly_pnl_usd": float,
    "weekly_pnl_pct": float,
    "daily_start_value": float,
    "weekly_start_value": float,
    "last_daily_reset": "2026-04-01",
    "last_weekly_reset": "2026-03-31",  # Week starts Monday
    "paused_until": "2026-04-02T09:00:00",
    "pause_reason": "TAGES-DRAWDOWN-STOP",
    "consecutive_losses": int,
}
```

**Automatic Reset:** Daily at calendar flip, Weekly at Monday

### `check_drawdown_limits()` — Lines 121-169
**Stops Trading When:**
```python
daily_pnl_pct <= -5% (default)     → Paused until next 09:00
weekly_pnl_pct <= -10% (default)   → Paused until next Monday 09:00
paused_until != null               → Already paused (check expiry)
```

**v5 Drawdown Recovery Mode Addition:**

When paused, could implement recovery mode:
```python
# NEW: When exiting pause
def enter_recovery_mode():
    state = _load_risk_state()
    state["recovery_mode"] = True
    state["recovery_until"] = (now + timedelta(days=7)).isoformat()
    state["recovery_risk_multiplier"] = 1.5  # 50% larger positions
    _save_risk_state(state)
    log.info("Entered RECOVERY MODE - 7 days, 1.5x sizing to catch up")
```

## Correlation & Exposure Monitoring

### `check_correlation()` — Lines 177-210
**Limits per asset class:**
```
max_positions_per_class = 5        # Max 5 stocks, 5 crypto, etc.
max_class_allocation_pct = 40      # Max 40% in one asset class
```

**Check:**
```python
# Count positions in class
current_count = class_count.get(new_class, 0)
if current_count >= 5:
    return False, "Max 5 positions for crypto reached"

# Check % allocation
current_pct = class_value.get(new_class, 0) / total_value * 100
if current_pct >= 40:
    return False, "Max 40% for crypto reached"
```

### `calculate_exposure()` — Lines 223-260
**Returns detailed breakdown:**
```python
{
    "total_invested": float,
    "total_effective": float,  # invested * leverage
    "by_class": {
        "stocks": {
            "invested": float,
            "effective": float,
            "count": int,
        }
    },
    "by_instrument": {
        "1234": {
            "invested": float,
            "effective": float,
            "leverage": int,
        }
    },
    "worst_case_loss": float,  # 100% loss on all positions
    "exposure_pct": float,
    "margin_buffer_pct": float,
}
```

### `check_margin_safety()` — Lines 263-288
**Safety Checks:**
```
max_total_exposure_pct = 300       # Max 300% leverage across all (3:1)
min_margin_buffer_pct = 20         # Min 20% cash/margin buffer
```

**Returns:** (allowed: bool, reasons: [str], exposure: dict)

---

# 6. LEVERAGE MANAGER (leverage_manager.py)

## Dynamic Leverage Calculation

### `calculate_optimal_leverage()` — Lines 61-109

**Inputs:**
- asset_class: str
- volatility: float (percent, e.g., 5.2%)
- signal_confidence: float (-100 to +100, from scanner score)
- market_regime: "bull"|"bear"|"sideways"
- vix_level: float (e.g., 18.5)
- config: config_manager dict

**Algorithm:**
```
1. Start with base_leverage (default 2x) limited to asset_class max

2. Volatility adjustment:
   < 2% (low):   leverage *= 1.5  (boost to 3x)
   2-4% (medium): no change
   > 4% (high):  leverage *= 0.5  (reduce to 1x)

3. Signal confidence:
   |score| > 30:  leverage *= 1.2  (strengthen signal)
   |score| < 10:  leverage *= 0.5  (weaken signal)

4. Market regime:
   bear:    leverage *= 0.5
   sideways: leverage *= 0.75
   bull:    no change

5. VIX adjustment:
   > 30: leverage *= 0.5
   > 20: leverage *= 0.75
   < 20: no change

6. Snap to allowed levels
```

**eToro Max Leverage per Asset Class:**
```python
ETORO_MAX_LEVERAGE = {
    "forex_major": 30,      # EURUSD, GBPUSD, etc.
    "forex_minor": 20,      # Lower-volume pairs
    "forex_exotic": 20,
    "indices": 20,
    "commodities": 10,
    "stocks": 5,
    "etf": 5,
    "crypto": 2,
}
```

**Allowed Discrete Levels:**
```python
"stocks": [1, 2, 5],
"crypto": [1, 2],
"forex_major": [1, 2, 5, 10, 20, 30],
"indices": [1, 2, 5, 10, 20],
"commodities": [1, 2, 5, 10],
```

**v5 Enhancement:** Already excellent! Just needs integration in trader.py.

### `snap_to_allowed()` — Lines 50-57
```python
def snap_to_allowed(leverage, asset_class, symbol=""):
    """Round down to nearest allowed level"""
    allowed = get_allowed_leverages(asset_class, symbol)
    valid = [l for l in allowed if l <= leverage]
    return max(valid) if valid else 1
```

Example: If calculated leverage is 4.5x for stocks → rounds down to 2x (only [1,2,5] allowed)

## Trailing Stop-Loss Implementation

### `update_trailing_stop_loss()` — Lines 175-203

**State Storage:** `trailing_sl_state.json`

**Algorithm:**
```
1. Check if position in profit
   pnl_pct = (current_price - entry_price) / entry_price * 100
   if pnl_pct < 1% (activation_pct default):
       return None  # Not yet activated

2. Calculate trailing level
   trail_level = current_price * (1 - trail_pct / 100)
   # Default trail_pct = 2%, so SL is 2% below current price

3. Only move UP, never down (for longs)
   if trail_level > old_level:
       state[position_id]["sl_level"] = trail_level
       log.info(f"Trailing SL updated: ${trail_level}")
   else:
       keep old level

4. Record first activation time
```

**Configuration (from leverage config):**
```python
"trailing_sl_pct": 2.0,              # 2% below current for longs
"trailing_sl_activation_pct": 1.0,   # Activate after 1% profit
```

### `check_trailing_stop_losses()` — Lines 206-228

**Called in monitor loop:**
```python
triggered = []
for pos in current_positions:
    if pos.position_id in trailing_sl_state:
        sl_level = trailing_sl_state[pos.position_id]["sl_level"]
        if pos.current_price <= sl_level:
            triggered.append(pos)  # Close this position
```

**v5 Enhancement:** Currently returns triggered list; needs execution call
```python
# In trader.py monitor_positions():
triggered_sls = check_trailing_stop_losses(positions)
for pos in triggered_sls:
    etoro_client.close_position(pos["position_id"])
    log.warning(f"Trailing SL hit: {pos['symbol']}")
```

## Take-Profit Tranches

### `calculate_tp_tranches()` — Lines 232-261

**Default Tranches (from config):**
```python
[
    {"pct_of_position": 50, "profit_target_pct": 3},   # TP1: 50% @ 3%
    {"pct_of_position": 30, "profit_target_pct": 6},   # TP2: 30% @ 6%
    {"pct_of_position": 20, "profit_target_pct": 10},  # TP3: 20% @ 10%
]
```

**Calculation:**
```python
for tranche in tranches:
    tp_price = entry_price * (1 + profit_target_pct / 100)
    # Example: Entry $100, TP1 = $103, TP2 = $106, TP3 = $110
    
    amount_usd = total_amount * pct_of_position / 100
    # Example: Position $5000, TP1 = $2500, TP2 = $1500, TP3 = $1000
```

**Returns:**
```python
[
    {
        "target_price": 103.00,
        "target_pct": 3,
        "amount_usd": 2500,
        "pct_of_position": 50,
    },
    ...
]
```

**v5 Critical Gap:** TP tranches are calculated but NOT MONITORED!

**Needed in trader.py:**
```python
# After opening position with tranches
def monitor_tp_tranches(position_id, current_price, tranches):
    for tranche in tranches:
        if current_price >= tranche["target_price"] and not tranche["executed"]:
            close_amount = tranche["amount_usd"]
            etoro_client.close_position(position_id, amount=close_amount)
            tranche["executed"] = True
            log.info(f"TP tranche 1 hit: Closing ${close_amount}")
```

## Risk/Reward Validation

### `check_risk_reward()` — Lines 264-279

**Formula:**
```
risk = |entry - stop_loss|
reward = |take_profit - entry|
ratio = reward / risk

If ratio < min_ratio (default 2.0):
    REJECT TRADE
```

**Example:**
```
Entry: $100
SL: $97 (risk = $3)
TP: $104 (reward = $4)
Ratio = $4 / $3 = 1.33x (REJECTED, too low)

Entry: $100
SL: $97 (risk = $3)
TP: $107 (reward = $7)
Ratio = $7 / $3 = 2.33x (ACCEPTED)
```

**v5 Integration:** Call before executing trade
```python
allowed, ratio, reason = check_risk_reward(
    entry_price=100,
    stop_loss_price=97,
    take_profit_price=107,
    min_ratio=2.0
)
if not allowed:
    log.warning(f"Trade rejected: {reason}")
    return False
```

## Short Position Support

### `validate_short_entry()` — Lines 299-323

**Restrictions:**
```python
if not short_enabled:
    return False, "Short-Trading deaktiviert"

if asset_class not in short_allowed_classes:
    return False, f"Shorts not allowed for {asset_class}"

# In bull markets, only allow STRONG_SELL signals
if market_regime == "bull" and signal_score > -25:
    return False, "Bull market: shorts only on STRONG_SELL"
```

**Allowed Classes (default):**
```python
["stocks", "etf", "forex", "indices", "commodities"]
# NOT crypto (too volatile for shorts)
```

---

# INTEGRATION ROADMAP FOR V5

## Phase 1: Core Enhancements

### 1.1 Scanner → Regime Filter
```
market_scanner.score_asset()
├── Add regime_filter param
├── Pre-filter by market_regime
└── Apply VIX dampening
```

### 1.2 Risk Manager → Dynamic Sizing
```
risk_manager.calculate_position_size()
├── Add market_regime param
├── Add vix_level param
├── Add drawdown_recovery mode
└── Implement multipliers
```

### 1.3 Leverage Manager → Optimal Calculation
```
leverage_manager.calculate_optimal_leverage()
├── Already exists!
└── Just call from trader.py
```

## Phase 2: Execution Improvements

### 2.1 Trader → Explicit SL/TP Logic
```
trader.execute_trade()
├── Calculate SL from ATR or support
├── Calculate TP from score and risk/reward
├── Apply cost adjustments
└── Set tranches
```

### 2.2 Trader → Monitor Tranches
```
trader.monitor_positions()
├── Check trailing stops
├── Check TP tranches
├── Close % of position when hit
└── Log all executions
```

### 2.3 Risk Manager → Overnight Checks
```
trader.end_of_day()
├── check_overnight_risk()
├── check_weekend_fee_impact()
└── Auto-close non-crypto leveraged
```

## Phase 3: Learning & Adaptation

### 3.1 Brain → Enhanced Regime Detection
```
brain.detect_market_regime()
├── Integrate VIX levels
├── Integrate Fear/Greed
└── Add regime transitions
```

### 3.2 Brain → Drawdown Recovery
```
brain.run_brain_cycle()
├── Detect drawdown start
├── Enter recovery mode
├── Suggest catch-up sizing
└── Auto-exit after profit
```

### 3.3 Brain → Parameter Optimization
```
brain.analyze_parameter_performance()
├── Test different SL %
├── Test different TP %
├── Test different leverage levels
└── Recommend optimal combo per regime
```

---

## File Map: Where Everything Connects

```
config_manager.py (settings)
├── risk_management: {risk_per_trade_pct, daily_drawdown_stop_pct, ...}
├── leverage: {default_leverage, trailing_sl_pct, tp_tranches, ...}
├── demo_trading: {max_single_trade_usd, portfolio_targets, ...}
└── market_context: {alpha_vantage_key, manual_events, ...}

etoro_client.py (API)
├── get_portfolio()
├── open_position(symbol, amount, leverage, sl, tp)
├── close_position(position_id, [amount])
└── update_order(position_id, sl, tp)

market_scanner.py (opportunities)
├── scan_all_assets()
├── analyze_single_asset()
├── score_asset()
└── analyze_multi_timeframe()

market_context.py (market regime)
├── update_full_context()
├── fetch_vix()
├── fetch_fear_greed()
├── fetch_economic_calendar()
└── get_position_size_multiplier()

brain.py (learning)
├── record_snapshot()
├── detect_market_regime()
├── learn_rules()
├── optimize_strategy()
└── run_brain_cycle()

risk_manager.py (validation)
├── calculate_position_size()
├── check_drawdown_limits()
├── validate_trade()
└── calculate_exposure()

leverage_manager.py (execution params)
├── calculate_optimal_leverage()
├── update_trailing_stop_loss()
├── calculate_tp_tranches()
└── check_risk_reward()

trader.py (orchestration)
├── execute_trade()
│   ├── validate_trade()
│   ├── calculate_position_size()
│   ├── calculate_optimal_leverage()
│   ├── calculate_tp_tranches()
│   ├── check_risk_reward()
│   └── etoro_client.open_position()
│
├── monitor_positions()
│   ├── check_trailing_stop_losses()
│   ├── [NEW] monitor_tp_tranches()
│   ├── [NEW] check_overnight_risk()
│   ├── [NEW] check_weekend_fees()
│   └── etoro_client.close_position()
│
└── main()
    ├── market_scanner.scan_all_assets()
    ├── execute_trade() for top opportunities
    ├── monitor_positions()
    ├── brain.run_brain_cycle()
    └── [REPEAT]
```

---

## Summary Table: Key Function Integration Points

| Feature | Current Module | Called From | v5 Status | Integration Effort |
|---------|---|---|---|---|
| Regime Filter | brain.py + market_context.py | market_scanner.py | ✓ Data ready | Score function modification |
| Multi-Timeframe | market_scanner.py | scanner | ✓ Exists | Call in trader for confirmation |
| Trailing SL | leverage_manager.py | [MISSING] | ⚠️ Implemented but unused | Monitor loop integration |
| Dynamic Sizing | risk_manager.py | [INCOMPLETE] | ⚠️ Partial | Add regime/VIX params |
| TP Tranches | leverage_manager.py | [MISSING] | ⚠️ Calculated only | Monitor & execution logic |
| Cost Filter | risk_manager.py | [MISSING] | ⚠️ Calculated only | Apply in score_asset() |
| Overnight Risk | risk_manager.py | [MISSING] | ⚠️ Exists | Call end-of-day checks |
| Drawdown Recovery | risk_manager.py + brain.py | [MISSING] | ❌ Not implemented | New mode + logic |
| Weekend Fees | risk_manager.py | [MISSING] | ⚠️ Calculated only | Friday EOD closing logic |
| VIX Regime | market_context.py | [MISSING] | ✓ Exists | Use in position sizing |
| Earnings Filter | market_context.py | [MISSING] | ✓ Exists | Call in scanner |
| Optimal Leverage | leverage_manager.py | [MISSING] | ✓ Implemented! | Direct use in trader |

---

This analysis provides the roadmap for v5. The good news: 70% of functionality already exists, just needs wiring together!


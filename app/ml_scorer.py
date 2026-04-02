"""
InvestPilot - ML Scoring Module
Ersetzt fixe Gewichte durch trainiertes Gradient Boosting Modell.
Features: gleiche Indikatoren wie Scanner (RSI, MACD, Bollinger, SMA, Momentum, Volume).
Label: Preis steigt >1% in naechsten 5 Tagen.
Modell wird als JSON gespeichert (portabel, kein Pickle).
"""

import logging
import json
from datetime import datetime

log = logging.getLogger("MLScorer")

try:
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
    HAS_ML = True
except ImportError:
    HAS_ML = False
    np = None

from app.config_manager import load_json, save_json
from app.market_scanner import calc_rsi, calc_macd, calc_bollinger_position

# Cached model
_model = None
_model_info = None

FEATURE_NAMES = [
    "rsi", "macd_val", "macd_signal", "macd_hist",
    "bollinger_pos", "momentum_5d", "momentum_20d",
    "volatility", "volume_trend",
    "above_sma20", "above_sma50", "golden_cross",
    "rsi_slope", "price_vs_sma20_pct",
    "atr_pct", "adx", "obv_slope", "vwap_deviation_pct",
]


# ============================================================
# FEATURE ENGINEERING
# ============================================================

def _calc_atr(highs, lows, closes, period=14):
    """Average True Range als Prozent des Preises."""
    if len(closes) < period + 1:
        return 0
    trs = []
    for j in range(1, len(closes)):
        tr = max(highs[j] - lows[j],
                 abs(highs[j] - closes[j - 1]),
                 abs(lows[j] - closes[j - 1]))
        trs.append(tr)
    if len(trs) < period:
        return 0
    atr = sum(trs[-period:]) / period
    return (atr / closes[-1] * 100) if closes[-1] > 0 else 0


def _calc_adx(highs, lows, closes, period=14):
    """Average Directional Index (Trendstaerke 0-100)."""
    if len(closes) < period * 2:
        return 50  # neutral default
    plus_dm, minus_dm, tr_list = [], [], []
    for j in range(1, len(closes)):
        up = highs[j] - highs[j - 1]
        down = lows[j - 1] - lows[j]
        plus_dm.append(up if up > down and up > 0 else 0)
        minus_dm.append(down if down > up and down > 0 else 0)
        tr_list.append(max(highs[j] - lows[j],
                           abs(highs[j] - closes[j - 1]),
                           abs(lows[j] - closes[j - 1])))
    if len(tr_list) < period:
        return 50
    # Smoothed averages
    atr = sum(tr_list[:period]) / period
    plus_di = sum(plus_dm[:period]) / period
    minus_di = sum(minus_dm[:period]) / period
    for j in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[j]) / period
        plus_di = (plus_di * (period - 1) + plus_dm[j]) / period
        minus_di = (minus_di * (period - 1) + minus_dm[j]) / period
    if atr == 0:
        return 50
    plus_di_pct = (plus_di / atr) * 100
    minus_di_pct = (minus_di / atr) * 100
    di_sum = plus_di_pct + minus_di_pct
    if di_sum == 0:
        return 50
    dx = abs(plus_di_pct - minus_di_pct) / di_sum * 100
    return min(100, dx)


def _calc_obv_slope(closes, volumes, period=20):
    """On-Balance Volume Steigung (normalisiert)."""
    if len(closes) < period + 1:
        return 0
    obv = [0]
    for j in range(1, len(closes)):
        if closes[j] > closes[j - 1]:
            obv.append(obv[-1] + volumes[j])
        elif closes[j] < closes[j - 1]:
            obv.append(obv[-1] - volumes[j])
        else:
            obv.append(obv[-1])
    # Slope: (OBV now - OBV period ago) / abs(OBV period ago) normalisiert
    recent = obv[-1]
    past = obv[-period] if len(obv) >= period else obv[0]
    if abs(past) < 1:
        return 1.0 if recent > 0 else -1.0
    return max(-5, min(5, (recent - past) / abs(past)))


def prepare_features(closes, volumes, min_lookback=60, highs=None, lows=None):
    """Compute feature matrix from price/volume arrays.

    Args:
        closes: list of close prices
        volumes: list of volumes
        min_lookback: minimum bars needed before first feature row
        highs: list of high prices (optional, for ATR/ADX)
        lows: list of low prices (optional, for ATR/ADX)

    Returns:
        list of feature dicts (one per bar from min_lookback onward)
        list of corresponding bar indices
    """
    # Use closes as fallback for highs/lows if not provided
    if highs is None:
        highs = closes
    if lows is None:
        lows = closes

    features = []
    indices = []

    for i in range(min_lookback, len(closes)):
        window = closes[max(0, i - min_lookback):i + 1]
        vol_window = volumes[max(0, i - min_lookback):i + 1]
        high_window = highs[max(0, i - min_lookback):i + 1]
        low_window = lows[max(0, i - min_lookback):i + 1]

        if len(window) < 20:
            continue

        rsi = calc_rsi(window)
        macd_val, macd_signal, macd_hist = calc_macd(window)
        boll_pos = calc_bollinger_position(window)

        current = window[-1]

        momentum_5d = (window[-1] - window[-5]) / window[-5] * 100 if len(window) >= 5 else 0
        momentum_20d = (window[-1] - window[-20]) / window[-20] * 100 if len(window) >= 20 else 0

        sma_20 = sum(window[-20:]) / 20 if len(window) >= 20 else current
        sma_50 = sum(window[-50:]) / 50 if len(window) >= 50 else sma_20

        vol_trend = 1.0
        if len(vol_window) >= 10 and sum(vol_window[-10:-5]) > 0:
            vol_trend = sum(vol_window[-5:]) / sum(vol_window[-10:-5])

        # Volatility
        volatility = 5.0
        if len(window) >= 20:
            returns = [(window[j] - window[j - 1]) / window[j - 1]
                       for j in range(max(1, len(window) - 20), len(window))]
            volatility = (sum(r ** 2 for r in returns) / len(returns)) ** 0.5 * 100

        above_sma20 = 1.0 if current > sma_20 else 0.0
        above_sma50 = 1.0 if current > sma_50 else 0.0
        golden_cross = 1.0 if sma_20 > sma_50 else 0.0

        # RSI slope (change over last 5 bars)
        if i >= min_lookback + 5:
            prev_window = closes[max(0, i - 5 - min_lookback):i - 5 + 1]
            prev_rsi = calc_rsi(prev_window) if len(prev_window) >= 20 else rsi
            rsi_slope = rsi - prev_rsi
        else:
            rsi_slope = 0

        price_vs_sma20_pct = (current - sma_20) / sma_20 * 100 if sma_20 > 0 else 0

        # New v5 features
        atr_pct = _calc_atr(high_window, low_window, window)
        adx = _calc_adx(high_window, low_window, window)
        obv_slope = _calc_obv_slope(window, vol_window)

        # VWAP deviation
        if len(high_window) >= 20 and sum(vol_window[-20:]) > 0:
            typical = [(h + l + c) / 3 for h, l, c in
                       zip(high_window[-20:], low_window[-20:], window[-20:])]
            vols_20 = vol_window[-20:]
            vwap = sum(t * v for t, v in zip(typical, vols_20)) / sum(vols_20)
            vwap_deviation_pct = (current - vwap) / vwap * 100 if vwap > 0 else 0
        else:
            vwap_deviation_pct = 0

        features.append([
            rsi, macd_val, macd_signal, macd_hist,
            boll_pos, momentum_5d, momentum_20d,
            volatility, vol_trend,
            above_sma20, above_sma50, golden_cross,
            rsi_slope, price_vs_sma20_pct,
            atr_pct, adx, obv_slope, vwap_deviation_pct,
        ])
        indices.append(i)

    return features, indices


def prepare_labels(closes, indices, forward_days=5, threshold=0.01):
    """Create binary labels: 1 if price rose > threshold in forward_days.

    Args:
        closes: full close price list
        indices: bar indices corresponding to features
        forward_days: look-ahead period
        threshold: minimum return to be labeled positive (0.01 = 1%)

    Returns:
        list of labels (0 or 1), matching indices that have valid labels
        list of valid indices
    """
    labels = []
    valid_indices = []

    for idx in indices:
        future_idx = idx + forward_days
        if future_idx >= len(closes):
            break  # can't compute label

        future_return = (closes[future_idx] - closes[idx]) / closes[idx]
        labels.append(1 if future_return > threshold else 0)
        valid_indices.append(idx)

    return labels, valid_indices


# ============================================================
# MODEL TRAINING
# ============================================================

def train_model(histories, train_pct=0.8):
    """Train a GradientBoosting model on historical data.

    Args:
        histories: dict {symbol: DataFrame} from backtester.download_history()
        train_pct: fraction for training (rest for validation)

    Returns:
        dict with model info, metrics, feature importances
    """
    if not HAS_ML:
        log.error("scikit-learn nicht installiert")
        return {"error": "scikit-learn nicht installiert"}

    all_features = []
    all_labels = []

    for sym, hist in histories.items():
        closes = hist["Close"].values.tolist()
        volumes = hist["Volume"].values.tolist()
        highs = hist["High"].values.tolist() if "High" in hist.columns else None
        lows = hist["Low"].values.tolist() if "Low" in hist.columns else None

        features, indices = prepare_features(closes, volumes, highs=highs, lows=lows)
        labels, valid_indices = prepare_labels(closes, indices)

        # Trim features to match valid labels
        valid_set = set(valid_indices)
        for i, idx in enumerate(indices):
            if idx in valid_set and i < len(labels):
                all_features.append(features[i])
                all_labels.append(labels[i])

    if len(all_features) < 200:
        log.warning(f"Zu wenig Trainingsdaten: {len(all_features)} Samples")
        return {"error": f"Zu wenig Daten ({len(all_features)} Samples, min. 200)"}

    X = np.array(all_features)
    y = np.array(all_labels)

    # Train/test split
    split = int(len(X) * train_pct)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    log.info(f"Training: {len(X_train)} Samples, Test: {len(X_test)} Samples")
    log.info(f"Label Balance: {sum(y_train)}/{len(y_train)} positive ({sum(y_train)/len(y_train)*100:.1f}%)")

    # Train
    model = GradientBoostingClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        subsample=0.8,
        random_state=42,
    )
    model.fit(X_train, y_train)

    # Evaluate
    train_pred = model.predict(X_train)
    test_pred = model.predict(X_test)
    test_proba = model.predict_proba(X_test)[:, 1] if len(model.classes_) == 2 else np.zeros(len(X_test))

    train_acc = accuracy_score(y_train, train_pred)
    test_acc = accuracy_score(y_test, test_pred)
    test_prec = precision_score(y_test, test_pred, zero_division=0)
    test_rec = recall_score(y_test, test_pred, zero_division=0)
    test_f1 = f1_score(y_test, test_pred, zero_division=0)

    # Feature importances
    importances = dict(zip(FEATURE_NAMES, model.feature_importances_.tolist()))

    # Cache model
    global _model, _model_info
    _model = model

    _model_info = {
        "trained": datetime.now().isoformat(),
        "samples_train": len(X_train),
        "samples_test": len(X_test),
        "label_balance_pct": round(sum(y_train) / len(y_train) * 100, 1),
        "train_accuracy": round(train_acc * 100, 1),
        "test_accuracy": round(test_acc * 100, 1),
        "test_precision": round(test_prec * 100, 1),
        "test_recall": round(test_rec * 100, 1),
        "test_f1": round(test_f1 * 100, 1),
        "feature_importances": {k: round(v, 4) for k, v in
                                sorted(importances.items(), key=lambda x: x[1], reverse=True)},
        "model_params": {
            "n_estimators": 100,
            "max_depth": 4,
            "learning_rate": 0.1,
        },
    }

    # Save model info (not the model itself — we retrain on startup if needed)
    save_json("ml_model.json", _model_info)

    log.info(f"ML Model trained: Train Acc={train_acc:.1%}, Test Acc={test_acc:.1%}, "
             f"F1={test_f1:.1%}")
    log.info(f"Top Features: {list(importances.keys())[:5]}")

    return _model_info


# ============================================================
# PREDICTION / SCORING
# ============================================================

def score_asset_ml(analysis):
    """Score an asset using the ML model.

    Args:
        analysis: dict from market_scanner.analyze_single_asset()

    Returns:
        score 0-100 (probability * 100), or None if model not available
    """
    global _model
    if _model is None:
        return None

    if not HAS_ML:
        return None

    # Build feature vector from analysis dict
    rsi = analysis.get("rsi", 50)
    macd_val = analysis.get("macd", 0)
    macd_signal = analysis.get("macd_signal", 0)
    macd_hist = analysis.get("macd_histogram", 0)
    boll_pos = analysis.get("bollinger_pos", 0.5)
    momentum_5d = analysis.get("momentum_5d", 0)
    momentum_20d = analysis.get("momentum_20d", 0)
    volatility = analysis.get("volatility", 5)
    vol_trend = analysis.get("volume_trend", 1)
    above_sma20 = 1.0 if analysis.get("above_sma20", False) else 0.0
    above_sma50 = 1.0 if analysis.get("above_sma50", False) else 0.0
    golden_cross = 1.0 if analysis.get("golden_cross", False) else 0.0

    # Approximate features not directly in analysis dict
    rsi_slope = 0  # not available in single-point analysis
    price = analysis.get("price", 0)
    price_vs_sma20_pct = (boll_pos - 0.5) * 10  # rough approximation

    # New v5 features
    atr_pct = analysis.get("atr_pct", 0)
    adx = analysis.get("adx", 50)
    obv_slope = analysis.get("obv_slope", 0)
    vwap_deviation_pct = analysis.get("vwap_deviation_pct", 0)

    features = np.array([[
        rsi, macd_val, macd_signal, macd_hist,
        boll_pos, momentum_5d, momentum_20d,
        volatility, vol_trend,
        above_sma20, above_sma50, golden_cross,
        rsi_slope, price_vs_sma20_pct,
        atr_pct, adx, obv_slope, vwap_deviation_pct,
    ]])

    try:
        proba = _model.predict_proba(features)[0]
        # probability of positive class
        if len(proba) == 2:
            score = proba[1] * 100
        else:
            score = 50
        return round(score, 1)
    except Exception as e:
        log.warning(f"ML Scoring Fehler: {e}")
        return None


def get_model_info():
    """Return current model info, or load from disk."""
    global _model_info
    if _model_info:
        return _model_info
    info = load_json("ml_model.json")
    if info:
        _model_info = info
    return info


def is_model_trained():
    """Check if ML model is loaded and ready."""
    return _model is not None

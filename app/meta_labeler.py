"""
Meta-Labeler (Lopez de Prado, v12) for InvestPilot.

Concept:
  Primary signal = the scanner ("should we BUY?"). The meta-labeler is a
  *second* model that only looks at trades the primary model wanted to
  take, and answers: "given the scanner said BUY with these features, is
  this trade likely to succeed?"

  The scanner is tuned for RECALL (catch opportunities).
  The meta-labeler is tuned for PRECISION (filter false positives).

Data source:
  trade_history.json — only trades with scanner_score >= threshold count.

Integration in trader.py:
  Before executing a BUY, build a signal_context dict and call
  meta_predict(ctx).  In shadow mode the trade is still executed but
  the decision is logged to meta_labeling_shadow.json for analysis.
  Once shadow precision crosses a threshold the gate auto-switches to
  LIVE and starts blocking trades where p_win < decision_threshold.

Public API:
  - train_meta_labeler(trade_history=None) -> dict
  - meta_predict(signal_context, config=None) -> dict
  - log_shadow_decision(decision, outcome_unknown=True) -> None
  - check_and_maybe_activate(config) -> bool  (called by scheduler)
  - get_meta_status() -> dict
"""

import logging
import os
from datetime import datetime

from app.config_manager import load_json, save_json, get_data_path

log = logging.getLogger("MetaLabeler")

try:
    import numpy as np
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score
    HAS_ML = True
except ImportError:
    HAS_ML = False
    np = None

_REGIME_MAP = {"bull": 0, "sideways": 1, "bear": 2, "unknown": 3}
_SECTOR_MAP = {"tech": 0, "finance": 1, "health": 2, "consumer": 3,
               "growth": 4, "energy": 5, "commodities": 6, "unknown": 7}
_ASSET_CLASS_MAP = {"stocks": 0, "etf": 1, "crypto": 2, "commodities": 3,
                    "forex": 4, "indices": 5, "unknown": 6}

_FEATURES = [
    "scanner_score", "rsi", "macd_hist", "momentum_5d", "momentum_20d",
    "volatility", "volume_trend", "regime_code", "vix_level", "fear_greed",
    "sector_code", "asset_class_code",
]

MIN_TRADES_FOR_META_TRAIN = 30
META_MODEL_FILE = "meta_model.joblib"
META_META_FILE = "meta_model.json"
SHADOW_LOG_FILE = "meta_labeling_shadow.json"

_model = None
_model_info = None


def _encode(mapping, key):
    if key is None:
        return mapping.get("unknown", len(mapping))
    return mapping.get(str(key).lower(), mapping.get("unknown", len(mapping)))


def _extract_features(ctx):
    """Turn a dict of raw signal context into a feature row."""
    return [
        float(ctx.get("scanner_score", 0) or 0),
        float(ctx.get("rsi", 50) or 50),
        float(ctx.get("macd_hist", 0) or 0),
        float(ctx.get("momentum_5d", 0) or 0),
        float(ctx.get("momentum_20d", 0) or 0),
        float(ctx.get("volatility", 5) or 5),
        float(ctx.get("volume_trend", 1) or 1),
        _encode(_REGIME_MAP, ctx.get("market_regime")),
        float(ctx.get("vix_level", 20) or 20),
        float(ctx.get("fear_greed", 50) or 50),
        _encode(_SECTOR_MAP, ctx.get("sector")),
        _encode(_ASSET_CLASS_MAP, ctx.get("asset_class")),
    ]


def _load_model():
    """Lazy-load the persisted meta model via joblib."""
    global _model, _model_info
    if _model is not None:
        return _model
    try:
        from joblib import load as joblib_load
        path = get_data_path(META_MODEL_FILE)
        if os.path.exists(path):
            _model = joblib_load(path)
    except Exception as e:
        log.debug(f"Meta model konnte nicht geladen werden: {e}")
        _model = None
    if _model_info is None:
        _model_info = load_json(META_META_FILE) or {}
    return _model


def train_meta_labeler(trade_history=None, min_scanner_score=20):
    """Train the meta-labeler from trade_history.

    Only uses trades that the scanner originally wanted to take.
    """
    if not HAS_ML:
        return {"error": "scikit-learn nicht verfuegbar"}

    if trade_history is None:
        trade_history = load_json("trade_history.json") or []

    # v37q: Schema-Update — Bot-Schema schreibt pnl_pct erst auf CLOSE-Events.
    # BUY-Eintraege haben scanner_score+features, CLOSE-Eintraege haben pnl_pct.
    # Verbinde via position_id und joine BUY-Features + CLOSE-Outcome.
    closes_by_pid: dict[str, dict] = {}
    for t in trade_history:
        a = (t.get("action") or "").upper()
        if "CLOSE" in a:
            pid = str(t.get("position_id") or "")
            if pid:
                pnl = t.get("pnl_net_pct", t.get("pnl_pct", None))
                if pnl is not None:
                    closes_by_pid[pid] = pnl

    usable = []
    for t in trade_history:
        action = (t.get("action") or "").upper()
        if "BUY" not in action and action not in ("OPEN", "SCANNER_BUY"):
            continue
        score = t.get("scanner_score", 0) or 0
        if score < min_scanner_score:
            continue
        # Outcome: entweder direkt am BUY (legacy-Schema) oder via position_id-Join
        pnl = t.get("pnl_net_pct", t.get("pnl_pct", None))
        if pnl is None:
            pid = str(t.get("position_id") or "")
            if pid and pid in closes_by_pid:
                pnl = closes_by_pid[pid]
                # Annotate fuer downstream
                t = dict(t)
                t["pnl_pct"] = pnl
        if pnl is None:
            continue
        usable.append(t)

    if len(usable) < MIN_TRADES_FOR_META_TRAIN:
        msg = (f"Zu wenig qualifizierte Scanner-BUYs: {len(usable)}/"
               f"{MIN_TRADES_FOR_META_TRAIN}. Meta-Labeler Training skipped.")
        log.info(msg)
        return {"error": msg, "trades_available": len(usable)}

    log.info(f"Meta-Labeler Training auf {len(usable)} Scanner-BUYs ...")

    X, y = [], []
    for t in usable:
        ctx = {
            "scanner_score": t.get("scanner_score"),
            "rsi": t.get("rsi"),
            "macd_hist": t.get("macd_hist") or t.get("macd_histogram"),
            "momentum_5d": t.get("momentum_5d"),
            "momentum_20d": t.get("momentum_20d"),
            "volatility": t.get("volatility"),
            "volume_trend": t.get("volume_trend"),
            "market_regime": t.get("market_regime"),
            "vix_level": t.get("vix_level"),
            "fear_greed": t.get("fear_greed"),
            "sector": t.get("sector"),
            "asset_class": t.get("asset_class"),
        }
        X.append(_extract_features(ctx))
        pnl = t.get("pnl_net_pct", t.get("pnl_pct", 0))
        y.append(1 if pnl > 0 else 0)

    X = np.array(X, dtype=float)
    y = np.array(y)
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

    split = int(len(X) * 0.8)
    if len(X) - split < 5:
        split = int(len(X) * 0.7)
    X_tr, X_te = X[:split], X[split:]
    y_tr, y_te = y[:split], y[split:]

    model = GradientBoostingClassifier(
        n_estimators=150, max_depth=3, learning_rate=0.08, subsample=0.85,
        random_state=42,
    )
    model.fit(X_tr, y_tr)

    proba_te = (model.predict_proba(X_te)[:, 1]
                if len(model.classes_) == 2 else np.zeros(len(X_te)))
    # F1-optimaler Threshold (wie ml_scorer)
    best_t, best_f1 = 0.55, 0.0
    for th in np.arange(0.30, 0.81, 0.05):
        preds = (proba_te >= th).astype(int)
        f1 = f1_score(y_te, preds, zero_division=0)
        if f1 > best_f1:
            best_f1 = f1
            best_t = float(th)

    pred_te = (proba_te >= best_t).astype(int)
    prec = precision_score(y_te, pred_te, zero_division=0)
    rec = recall_score(y_te, pred_te, zero_division=0)
    acc = accuracy_score(y_te, pred_te)
    f1v = f1_score(y_te, pred_te, zero_division=0)

    importances = dict(zip(_FEATURES, model.feature_importances_.tolist()))

    global _model, _model_info
    _model = model
    _model_info = {
        "trained_at": datetime.now().isoformat(),
        "samples_total": int(len(X)),
        "samples_train": int(len(X_tr)),
        "samples_test": int(len(X_te)),
        "win_rate_base": round(float(y.mean()) * 100, 1),
        "accuracy": round(acc * 100, 1),
        "precision": round(prec * 100, 1),
        "recall": round(rec * 100, 1),
        "f1": round(f1v * 100, 1),
        "decision_threshold": round(best_t, 3),
        "feature_importances": {k: round(v, 4) for k, v in
                                 sorted(importances.items(), key=lambda x: -x[1])},
        "min_scanner_score_filter": min_scanner_score,
    }

    try:
        from joblib import dump as joblib_dump
        joblib_dump(model, str(get_data_path(META_MODEL_FILE)))
    except Exception as e:
        log.warning(f"Meta-Model speichern fehlgeschlagen: {e}")
    save_json(META_META_FILE, _model_info)
    log.info(f"  Meta-Labeler: Precision={prec:.1%}, Recall={rec:.1%}, "
             f"F1={f1v:.1%}, Threshold={best_t:.2f}")
    return _model_info


def meta_predict(signal_context, config=None):
    """Evaluate a pending BUY against the meta-labeler.

    Returns:
        {
            "p_win": float 0..1,
            "decision": "take" | "skip" | "shadow_take" | "shadow_skip",
            "threshold": float,
            "shadow_mode": bool,
            "reason": str,
        }

    If model not trained / disabled: always returns p_win=None, decision="take".
    """
    cfg = (config or {}).get("meta_labeling", {}) or {}
    if not cfg.get("enabled", False):
        return {"p_win": None, "decision": "take", "shadow_mode": False,
                "reason": "meta_labeling disabled"}

    model = _load_model()
    if model is None or not HAS_ML:
        return {"p_win": None, "decision": "take", "shadow_mode": True,
                "reason": "no meta model yet"}

    shadow_mode = cfg.get("shadow_mode", True)
    threshold = float((_model_info or {}).get("decision_threshold",
                                                cfg.get("decision_threshold", 0.55)))

    features = np.array([_extract_features(signal_context)], dtype=float)
    features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
    try:
        proba = model.predict_proba(features)[0]
        p_win = float(proba[1]) if len(proba) == 2 else 0.5
    except Exception as e:
        log.warning(f"meta_predict crashed: {e}")
        return {"p_win": None, "decision": "take", "shadow_mode": shadow_mode,
                "reason": f"predict error: {e}"}

    would_take = p_win >= threshold
    if shadow_mode:
        decision = "shadow_take" if would_take else "shadow_skip"
    else:
        decision = "take" if would_take else "skip"

    reason = (f"p_win={p_win:.2f} {'>=' if would_take else '<'} thr={threshold:.2f}"
              f"{' (shadow)' if shadow_mode else ''}")
    return {"p_win": round(p_win, 3), "decision": decision,
            "threshold": threshold, "shadow_mode": shadow_mode, "reason": reason}


def log_shadow_decision(decision_record):
    """Append a shadow decision to meta_labeling_shadow.json."""
    log_data = load_json(SHADOW_LOG_FILE) or []
    log_data.append(decision_record)
    # Rotate: keep last 1000
    if len(log_data) > 1000:
        log_data = log_data[-1000:]
    save_json(SHADOW_LOG_FILE, log_data)


def check_and_maybe_activate(config=None):
    """Check if we have enough shadow evidence to leave shadow mode.

    Called from the scheduler once per day.  Returns True if state changed.
    """
    cfg = (config or {}).get("meta_labeling", {}) or {}
    if not cfg.get("enabled", False):
        return False
    if not cfg.get("shadow_mode", True):
        return False

    min_trades = int(cfg.get("min_trades_to_activate", 50))
    min_prec = float(cfg.get("min_precision_to_activate", 0.65))

    shadow_log = load_json(SHADOW_LOG_FILE) or []
    # Pair shadow decisions with their eventual outcome. The outcome is only
    # known AFTER the position closes — we re-scan trade_history for matching
    # position IDs.
    trade_history = load_json("trade_history.json") or []
    closed_by_pid = {}
    for t in trade_history:
        pid = str(t.get("position_id") or "")
        if pid and "CLOSE" in (t.get("action") or "").upper():
            closed_by_pid[pid] = t.get("pnl_pct", 0) or 0

    matured = []  # (shadow_decision, profitable)
    for rec in shadow_log:
        pid = str(rec.get("position_id") or "")
        if pid and pid in closed_by_pid:
            matured.append((rec["decision"], closed_by_pid[pid] > 0))

    if len(matured) < min_trades:
        log.debug(f"Meta-Labeler Activation: nur {len(matured)}/{min_trades} "
                  f"matured trades")
        return False

    # Precision = of the shadow_take decisions, how many were profitable?
    takes = [p for d, p in matured if d == "shadow_take"]
    if not takes:
        return False
    precision = sum(1 for p in takes if p) / len(takes)

    log.info(f"Meta-Labeler Shadow-Precision: {precision:.1%} "
             f"(on {len(takes)} shadow-takes, threshold={min_prec:.0%})")
    if precision < min_prec:
        return False

    # v37q: auto_activate-Schalter (default False) damit nicht heimlich
    # geflipped wird. User entscheidet manuell wann Live-Mode an darf.
    auto_activate = bool(cfg.get("auto_activate", False))
    if not auto_activate:
        log.info(
            "  Meta-Labeler ERREICHT Aktivierungs-Schwelle "
            f"({len(matured)} matured / Precision {precision:.1%}) — "
            "Auto-Activation aber deaktiviert (auto_activate=false). "
            "Manuell aktivieren via config.meta_labeling.shadow_mode=false."
        )
        # Pushover-Alert: User soll mitkriegen dass das Modell ready ist
        try:
            from app.alerts import send_alert
            send_alert(
                f"Meta-Labeler ist BEREIT fuer Live-Aktivierung: "
                f"{len(matured)} matured Trades / Precision {precision:.1%} "
                f"(Schwelle {min_prec:.0%}). Auto-Activation ist aus — "
                f"manuell freischalten via config.meta_labeling.shadow_mode=false.",
                level="INFO",
            )
        except Exception:
            pass
        return False

    # Flip shadow_mode off in the live config
    config["meta_labeling"]["shadow_mode"] = False
    try:
        from app.config_manager import save_config
        save_config(config)
        log.info("  Meta-Labeler -> LIVE aktiviert. Shadow mode OFF.")
        # Pushover bei Auto-Activation
        try:
            from app.alerts import send_alert
            send_alert(
                f"Meta-Labeler AUTO-AKTIVIERT (Live-Mode): "
                f"{len(matured)} matured Trades / Precision {precision:.1%}. "
                f"Bot blockiert ab jetzt BUYs mit p_win < threshold.",
                level="WARNING",
            )
        except Exception:
            pass
    except Exception as e:
        log.warning(f"  Meta-Labeler activation save_config failed: {e}")
    return True


def get_meta_status():
    """Return status dict for dashboard/logging."""
    _load_model()
    shadow = load_json(SHADOW_LOG_FILE) or []
    return {
        "model_trained": _model is not None,
        "model_info": _model_info or {},
        "shadow_log_size": len(shadow),
    }

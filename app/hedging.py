"""
Portfolio Hedging for InvestPilot.

Reduces exposure during bear market regimes by adjusting position sizes
and preferring defensive sectors.
"""

import logging

log = logging.getLogger("Hedging")

# Defensive sectors that tend to outperform in bear markets
DEFAULT_DEFENSIVE_SECTORS = ["health", "consumer", "bonds", "commodities"]


def check_hedge_needed(regime_data, positions, config):
    """Check if portfolio hedging is needed based on market regime.

    Args:
        regime_data: dict from check_regime_filter() with brain_regime etc.
        positions: list of parsed position dicts
        config: full config dict

    Returns:
        dict with keys:
            hedge_needed: bool
            hedge_instrument: str (e.g. 'SPY')
            hedge_amount: float (USD)
            bear_position_multiplier: float (e.g. 0.5)
            defensive_sectors: list of str
            reason: str
    """
    hedge_config = config.get("hedging", {})

    result = {
        "hedge_needed": False,
        "hedge_instrument": None,
        "hedge_amount": 0,
        "bear_position_multiplier": 1.0,
        "defensive_sectors": [],
        "reason": "Kein Hedging noetig",
    }

    # Feature toggle
    if not hedge_config.get("enabled", False):
        result["reason"] = "Hedging deaktiviert"
        return result

    try:
        brain_regime = regime_data.get("brain_regime", "unknown")
        combined_score = regime_data.get("combined_score", 0)

        if brain_regime != "bear":
            result["reason"] = f"Regime={brain_regime}, kein Bear-Markt"
            return result

        # Calculate total exposure
        total_exposure = sum(
            p.get("invested", 0) * p.get("leverage", 1)
            for p in positions
        )

        bear_multiplier = hedge_config.get("bear_position_multiplier", 0.5)
        defensive_sectors = hedge_config.get("defensive_sectors",
                                             DEFAULT_DEFENSIVE_SECTORS)

        result["hedge_needed"] = True
        result["bear_position_multiplier"] = bear_multiplier
        result["defensive_sectors"] = defensive_sectors
        result["hedge_amount"] = total_exposure * (1 - bear_multiplier)
        result["hedge_instrument"] = "SPY"  # Reference instrument
        result["reason"] = (
            f"Bear-Regime aktiv: Positionsgroessen x{bear_multiplier}, "
            f"defensive Sektoren bevorzugt"
        )

        log.info(f"HEDGING: {result['reason']}")
        log.info(f"  Total Exposure: ${total_exposure:,.2f}")
        log.info(f"  Empfohlene Reduktion: ${result['hedge_amount']:,.2f}")
        log.info(f"  Defensive Sektoren: {', '.join(defensive_sectors)}")

        return result

    except Exception as e:
        log.warning(f"Hedging-Check fehlgeschlagen: {e}")
        result["reason"] = f"Hedging-Fehler: {e}"
        return result


def get_hedge_instruments():
    """Get list of protective/defensive assets available on eToro.

    Note: eToro may not have inverse ETFs, so we focus on
    reducing position sizes and preferring defensive assets.

    Returns:
        list of dicts with symbol, name, type
    """
    return [
        {"symbol": "GLD", "name": "Gold ETF", "type": "commodity"},
        {"symbol": "TLT", "name": "20+ Year Treasury Bond ETF", "type": "bond"},
        {"symbol": "XLV", "name": "Health Care Select Sector", "type": "health"},
        {"symbol": "XLP", "name": "Consumer Staples Select Sector", "type": "consumer"},
        {"symbol": "VZ", "name": "Verizon", "type": "telecom"},
        {"symbol": "JNJ", "name": "Johnson & Johnson", "type": "health"},
        {"symbol": "PG", "name": "Procter & Gamble", "type": "consumer"},
        {"symbol": "KO", "name": "Coca-Cola", "type": "consumer"},
    ]


def is_defensive_sector(sector, config=None):
    """Check if a sector is considered defensive.

    Args:
        sector: str sector name (e.g. 'health', 'tech')
        config: optional config dict

    Returns:
        bool
    """
    if not sector:
        return False

    hedge_config = {}
    if config:
        hedge_config = config.get("hedging", {})

    defensive = hedge_config.get("defensive_sectors", DEFAULT_DEFENSIVE_SECTORS)
    sector_lower = sector.lower()

    return any(d.lower() in sector_lower for d in defensive)


def apply_hedge_to_amount(amount, hedge_result, sector=None, config=None):
    """Apply hedging adjustments to a trade amount.

    Args:
        amount: original trade amount in USD
        hedge_result: dict from check_hedge_needed()
        sector: optional sector of the asset
        config: optional config dict

    Returns:
        adjusted amount (float)
    """
    if not hedge_result.get("hedge_needed", False):
        return amount

    multiplier = hedge_result.get("bear_position_multiplier", 1.0)

    # Defensive sectors get less reduction
    if sector and is_defensive_sector(sector, config):
        # Only reduce by half the penalty for defensive sectors
        multiplier = 1.0 - (1.0 - multiplier) * 0.5
        log.debug(f"  Defensiver Sektor '{sector}': Multiplier={multiplier:.2f}")

    adjusted = round(amount * multiplier, 2)
    if adjusted != amount:
        log.info(f"  Hedging: ${amount:,.2f} -> ${adjusted:,.2f} (x{multiplier:.2f})")

    return adjusted

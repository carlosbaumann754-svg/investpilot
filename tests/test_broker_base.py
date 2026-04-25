"""
Tests fuer die Broker-Abstraktion (W2 Migration eToro -> IBKR).

Kein Live-API-Call — nur Interface-Compliance + Factory-Routing.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Erlaube Import aus app/ ohne installierten Package
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from app.broker_base import BrokerBase, get_broker


def test_etoro_implements_broker_base():
    """EtoroClient muss alle abstract Methoden implementieren (sonst TypeError beim init)."""
    from app.etoro_client import EtoroClient
    client = EtoroClient({"etoro": {"public_key": "x", "demo_private_key": "y"}})
    assert isinstance(client, BrokerBase)
    assert client.broker_name == "etoro"


def test_ibkr_implements_broker_base():
    """IbkrBroker muss alle abstract Methoden implementieren."""
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker({})
    assert isinstance(broker, BrokerBase)
    assert broker.broker_name == "ibkr"
    assert broker.port == 4004, "Default-Port muss 4004 sein (socat-Bridge), nicht 4002"


def test_ibkr_write_ops_implemented_w3():
    """W3: Write-Ops sind implementiert — duerfen nicht mehr NotImplementedError werfen.

    Detail-Tests fuer das Verhalten in tests/test_ibkr_write_ops.py."""
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker({})
    # Methoden existieren als callable, nicht abstrakte Stubs
    assert callable(getattr(broker, "buy", None))
    assert callable(getattr(broker, "sell", None))
    assert callable(getattr(broker, "close_position", None))
    # Method.__qualname__ zeigt: liegt in IbkrBroker, nicht ABC-Stub
    assert IbkrBroker.buy.__qualname__.startswith("IbkrBroker.")
    assert IbkrBroker.close_position.__qualname__.startswith("IbkrBroker.")


def test_factory_routes_etoro():
    cfg = {"broker": "etoro", "etoro": {"public_key": "x", "demo_private_key": "y"}}
    b = get_broker(cfg)
    assert b.broker_name == "etoro"


def test_factory_routes_ibkr():
    cfg = {"broker": "ibkr", "ibkr": {"port": 4004}}
    b = get_broker(cfg)
    assert b.broker_name == "ibkr"


def test_factory_default_is_etoro():
    """Backwards-compat: ohne `broker`-key wird etoro gewaehlt."""
    cfg = {"etoro": {"public_key": "x", "demo_private_key": "y"}}
    b = get_broker(cfg)
    assert b.broker_name == "etoro"


def test_factory_unknown_broker_raises():
    with pytest.raises(ValueError, match="Unbekannter Broker"):
        get_broker({"broker": "fxcm"})


def test_factory_case_insensitive():
    cfg = {"broker": "IBKR", "ibkr": {}}
    b = get_broker(cfg)
    assert b.broker_name == "ibkr"


def test_ibkr_config_overrides_defaults():
    from app.ibkr_client import IbkrBroker
    broker = IbkrBroker({"ibkr": {"host": "custom-host", "port": 9999, "client_id": 42}})
    assert broker.host == "custom-host"
    assert broker.port == 9999
    assert broker.client_id == 42

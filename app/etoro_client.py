"""
InvestPilot - Unified eToro API Client
Vereint beide bisherigen Client-Klassen (demo_trader.py + investpilot.py).
Unterstuetzt automatische Key-Erkennung und beide Environments (demo/real).
"""

import uuid
import logging

try:
    import requests
except ImportError:
    raise ImportError("pip install requests")

log = logging.getLogger("EtoroClient")


class EtoroClient:
    """Unified Client fuer die eToro Public API."""

    def __init__(self, config):
        etoro = config.get("etoro", {})
        self.base_url = etoro.get("base_url", "https://public-api.etoro.com/api/v1")
        self.public_key = etoro.get("public_key", "")
        self.username = etoro.get("username", "")
        self.env = etoro.get("environment", "demo")

        # Private Key basierend auf Environment waehlen
        if self.env == "demo":
            self.private_key = etoro.get("demo_private_key", "")
        else:
            self.private_key = etoro.get("private_key", "")

        if not self.public_key or not self.private_key:
            log.warning(f"eToro API Keys fehlen fuer env={self.env}!")
            self.configured = False
        else:
            self.configured = True

        self._key_order = None  # A oder B, wird auto-detected

    # --- HTTP Helpers ---

    def _headers_a(self):
        """Variante A: x-api-key=public, x-user-key=private."""
        return {
            "x-api-key": self.public_key,
            "x-user-key": self.private_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _headers_b(self):
        """Variante B: x-api-key=private, x-user-key=public."""
        return {
            "x-api-key": self.private_key,
            "x-user-key": self.public_key,
            "x-request-id": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def _try_request(self, method, url, headers, json_payload=None):
        """Einzelner Request-Versuch."""
        try:
            if method == "GET":
                resp = requests.get(url, headers=headers, timeout=30)
            else:
                resp = requests.post(url, headers=headers, json=json_payload, timeout=30)

            if resp.status_code == 200:
                return resp.json() if resp.text.strip() else {}
            log.warning(f"  HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        except Exception as e:
            log.error(f"  Request Fehler: {e}")
            return None

    def _get(self, endpoint):
        """GET mit automatischer Key-Erkennung."""
        url = f"{self.base_url}{endpoint}"

        if self._key_order == "A":
            return self._try_request("GET", url, self._headers_a())
        elif self._key_order == "B":
            return self._try_request("GET", url, self._headers_b())

        # Auto-detect: beide Varianten testen
        result = self._try_request("GET", url, self._headers_a())
        if result is not None:
            self._key_order = "A"
            return result

        result = self._try_request("GET", url, self._headers_b())
        if result is not None:
            self._key_order = "B"
            return result

        log.error(f"Beide Key-Varianten fehlgeschlagen fuer {endpoint}")
        return None

    def _post(self, endpoint, payload):
        """POST mit bekannter Key-Reihenfolge."""
        url = f"{self.base_url}{endpoint}"
        headers = self._headers_a() if self._key_order != "B" else self._headers_b()
        return self._try_request("POST", url, headers, payload)

    # --- Portfolio ---

    def get_portfolio(self):
        """Portfolio mit Positionen und P/L laden."""
        data = self._get(f"/trading/info/{self.env}/pnl")
        if not data:
            return None
        return data.get("clientPortfolio", data)

    def get_equity(self):
        """Equity-Wert."""
        return self._get(f"/trading/info/{self.env}/equity")

    def get_available_cash(self):
        """Verfuegbares Cash."""
        return self._get(f"/trading/info/{self.env}/available-cash")

    def get_total_invested(self):
        """Total investiert."""
        return self._get(f"/trading/info/{self.env}/total-invested")

    def get_pnl(self):
        """Portfolio P/L und Positionen (Alias fuer get_portfolio raw)."""
        return self._get(f"/trading/info/{self.env}/pnl")

    # --- Trading ---

    def buy(self, instrument_id, amount_usd, leverage=1, stop_loss=0, take_profit=0):
        """Kauf-Order (Market, by Amount)."""
        payload = {
            "InstrumentID": instrument_id,
            "Amount": amount_usd,
            "IsBuy": True,
            "Leverage": leverage,
            "StopLossRate": stop_loss,
            "TakeProfitRate": take_profit,
            "IsTslEnabled": False,
        }
        log.info(f"  BUY: InstrumentID={instrument_id}, Amount=${amount_usd}, Leverage={leverage}x")
        result = self._post(f"/trading/execution/{self.env}/market-open-orders/by-amount", payload)
        if result:
            order = result.get("orderForOpen", {})
            log.info(f"  -> Order OK: ID={order.get('orderID')}, Status={order.get('statusID')}")
        return result

    def sell(self, instrument_id, amount_usd, leverage=1):
        """Sell/Short-Order (Market, by Amount)."""
        payload = {
            "InstrumentID": instrument_id,
            "Amount": amount_usd,
            "IsBuy": False,
            "Leverage": leverage,
            "IsTslEnabled": False,
        }
        log.info(f"  SELL: InstrumentID={instrument_id}, Amount=${amount_usd}")
        return self._post(f"/trading/execution/{self.env}/market-open-orders/by-amount", payload)

    def close_position(self, position_id, instrument_id=None):
        """Position schliessen."""
        log.info(f"  CLOSE: PositionID={position_id}" + (f", InstrumentID={instrument_id}" if instrument_id else ""))
        payload = {}
        if instrument_id:
            payload["InstrumentID"] = instrument_id
        return self._post(f"/trading/execution/{self.env}/market-close-orders/positions/{position_id}", payload)

    # --- Instruments ---

    def search_instrument(self, query):
        """Instrument suchen."""
        data = self._get(f"/market-data/search?query={query}")
        if not data:
            return []
        results = []
        for item in data.get("items", []):
            if item.get("isHiddenFromClient"):
                continue
            results.append({
                "id": item.get("internalInstrumentId"),
                "name": item.get("internalInstrumentDisplayName"),
                "symbol": item.get("internalSymbolFull"),
                "exchange": item.get("internalExchangeName"),
                "asset_class": item.get("internalAssetClassName"),
            })
        return results

    def get_instruments(self, instrument_ids=None):
        """Instrument-Metadaten."""
        endpoint = "/instruments"
        if instrument_ids:
            ids_str = ",".join(str(i) for i in instrument_ids)
            endpoint += f"?InstrumentIds={ids_str}"
        return self._get(endpoint)

    # --- Helpers ---

    @staticmethod
    def parse_position(pos):
        """Extrahiere Position-Daten mit korrekten eToro API Feldnamen."""
        iid = pos.get("instrumentID") or pos.get("instrumentId") or pos.get("InstrumentID")
        invested = pos.get("amount") or pos.get("investedAmount") or pos.get("Amount") or 0
        pid = pos.get("positionID") or pos.get("positionId") or pos.get("PositionID")
        leverage = pos.get("leverage", 1)

        pnl_raw = pos.get("unrealizedPnL", {})
        pnl_val = pnl_raw.get("pnL", 0) if isinstance(pnl_raw, dict) else 0
        pnl_pct = (pnl_val / invested * 100) if invested > 0 else 0

        # Preise aus der API extrahieren (verschiedene Feldnamen je nach Endpoint)
        current_price = (
            pos.get("currentRate") or pos.get("CurrentRate")
            or pos.get("current_rate") or pos.get("currentPrice")
            or None
        )
        entry_price = (
            pos.get("openRate") or pos.get("OpenRate")
            or pos.get("open_rate") or pos.get("openPrice")
            or None
        )

        # Open-Timestamp fuer Time-Stop Exit (v12). eToro liefert verschiedene Feldnamen
        # je nach Endpoint — wir nehmen das erste was da ist, Trader fällt sonst auf
        # trade_history.json zurueck.
        open_time = (
            pos.get("openDateTime") or pos.get("OpenDateTime")
            or pos.get("openDate") or pos.get("OpenDate")
            or pos.get("open_date") or pos.get("open_time")
            or pos.get("timestamp") or None
        )

        return {
            "instrument_id": iid,
            "position_id": pid,
            "invested": invested,
            "pnl": round(pnl_val, 2),
            "pnl_pct": round(pnl_pct, 2),
            "leverage": leverage,
            "current_price": current_price,
            "entry_price": entry_price,
            "open_time": open_time,
        }

# IBKR Setup + Troubleshooting Guide

Schneller Setup-Pfad und Lessons-Learned für die IBKR-Integration des
InvestPilot-Bots. Spart 2-4h Debugging falls jemand das nochmal aufsetzt.

> **Status (2026-04-25):** Bot lebt produktiv gegen Paper-Account
> DUP108015 via IB Gateway am VPS (`178.104.236.157:4002` localhost,
> `4004` socat-bridge intern). Real-Money-Cutover geplant ~28.05.

---

## Quickstart

```bash
# 1. Container am VPS hochfahren
ssh root@178.104.236.157
cd /opt/ib-gateway && docker compose up -d
cd /opt/investpilot && docker compose -f docker-compose.vps.yml up -d

# 2. Connection verifizieren
docker exec investpilot python -m app.ibkr_client
# -> Erwartung: {"ok": true, "server_version": 176, "accounts": ["DUP108015"]}

# 3. Bot auf IBKR umstellen
# In data/config.json: "broker": "ibkr"
docker compose -f /opt/investpilot/docker-compose.vps.yml restart investpilot

# 4. Live-Cycle beobachten
docker logs -f investpilot --tail 50
# -> Sucht: "INFO Trading-Cycle mit Broker 'ibkr'"
```

---

## Architektur

```
Internet
    │
    ▼
[Caddy Container] :443 → :8000 [investpilot Container] ──┐
                                          │              │
                                          │ ib_insync    │ docker network
                                          │ port 4004    │ "investpilot_default"
                                          │              │
                                          ▼              │
                              [ib-gateway Container] ────┘
                                  │
                                  ▼
                          [socat: 0.0.0.0:4004 → 127.0.0.1:4002]
                                  │
                                  ▼
                          [IB Gateway Java] :4002 (localhost only)
                                  │
                                  ▼
                          [IBKR Cloud (Paper-Account)]
```

**Schlüssel-Erkenntnis:** Das gnzsnz/ib-gateway Image lauscht nur auf
`127.0.0.1:4002` (strict localhost). Ein socat-Daemon im Container
exposed Port `0.0.0.0:4004` und forwarded zu localhost:4002. Damit
sieht IBG die Connection als "lokal" und akzeptiert sie.

→ **ib_insync MUSS zu Port 4004 connecten, NICHT 4002.**

---

## Die 4 Production-Bugs aus W4 Live-Smoke-Test

Jeder Bug hat den Bot beim Live-Cutover beim ersten Versuch kaputtgemacht.
Tests in `tests/test_w4_regression.py` decken sie ab — aber sie nochmal
zu kennen lohnt:

### Bug 1: ib_insync nicht in requirements.txt

**Symptom:** `ModuleNotFoundError: No module named 'ib_insync'` beim
ersten Bot-Cycle nach `docker compose up --build`.

**Ursache:** Im alten Container war ib_insync per `pip install` direkt
installiert (ad-hoc), aber nicht in `requirements.txt` aufgenommen.
Container-Rebuild verlor das Paket.

**Fix:** `ib_insync>=0.9.86` in `requirements.txt`. Test:
`test_w4_bug1_ib_insync_in_requirements`.

### Bug 2: IBC `ReadOnlyApi` defaulted to "yes"

**Symptom:** `Error 321: API interface is currently in Read-Only mode`.
get_equity/cash/positions returnen None, alle Trade-Versuche scheitern.

**Ursache:** Im IBC config.ini.tmpl steht `ReadOnlyApi=${READ_ONLY_API}`.
Wenn die env-var nicht gesetzt ist, interpretiert IBC den leeren Wert
als "yes" (Read-Only).

**Fix:** In `/opt/ib-gateway/docker-compose.yml` die env-var setzen:
```yaml
environment:
  - READ_ONLY_API=no
```
**Persistent dank Volume-Mount** für `config.ini.tmpl` (siehe v20).

### Bug 3: ticker.marketPrice ist Method, nicht Attribut

**Symptom:** `TypeError: '>' not supported between instances of 'method'
and 'int'`. get_quote() crasht, alle Orders blockiert.

**Ursache:** In ib_insync 0.9.86 ist `Ticker.marketPrice` eine **Methode**
(`callable`), nicht ein Attribut wie `Ticker.last` (float). Mein Code
holte `getattr(ticker, 'marketPrice')` und verglich direkt mit `> 0` —
das crasht weil `<method> > 0` nicht geht.

**Fix:** `_safe_num()` Helper in `ibkr_contract_resolver.py`. Ruft
callable wenn callable, gibt `Optional[float]` zurück. Test:
`test_w4_bug3_safe_num_handles_method_callable`.

### Bug 4: MarketOrder vom Paper-Account abgelehnt

**Symptom:** Order eingereicht (PendingSubmit) → sofort `Cancelled` mit
`Warning 202: No market data on major exchange for market order`.

**Ursache:** IBKR-Policy: Paper-Accounts ohne RT-Marktdaten-Abo lehnen
MarketOrders ab (sicherheitshalber, weil ohne aktuelle Quotes "blind"
gekauft wird).

**Fix:** **LimitOrder als Default** in `_place_market_order()` mit 0.5%
Slippage-Buffer. `LIMIT_SLIPPAGE_PCT=0.5` als Class-Konstante. Auch in
Production-Trading besser (kontrollierter Slippage-Cap).
MarketOrder bleibt verfügbar via `order_type="MARKET"` Param.
Test: `test_w4_bug4_default_order_type_is_limit`.

### W6 Hotfix v21: Falsche Top-Level-Keys in get_portfolio()

**Symptom:** Bot meldet sofort nach Cutover `TAGES-DRAWDOWN-STOP -100%`
und pausiert Trading bis nächsten Tag 09:00.

**Ursache:** `IbkrBroker.get_portfolio()` returnte `creditByRealizedEquity`
und `availableCash` als Top-Level-Keys. Aber `trader.py` liest die
**eToro-Standard-Keys**: `portfolio.get("credit", 0)` und `get("unrealizedPnL")`.
Resultat: cash=$0 erkannt → Drawdown-Stop triggered.

**Fix:** Top-Level-Keys auf `credit` + `unrealizedPnL` + `positions`
umgestellt (eToro-Standard). Legacy-Aliases bleiben für backwards-compat.
Test: `test_v21_hotfix_get_portfolio_returns_etoro_compatible_keys`.

---

## Persistente VPS-Konfiguration

Diese Patches sind **nicht im Bot-Repo** getrackt (gehören zur IBG-Infra):

### `/opt/ib-gateway/docker-compose.yml`

```yaml
environment:
  - READ_ONLY_API=no            # CRITICAL — ohne das kein Trading
  # ... (TWS_USERID, TWS_PASSWORD aus .env)
volumes:
  - /opt/ib-gateway/config.ini.tmpl:/home/ibgateway/ibc/config.ini.tmpl:ro
  - /opt/ib-gateway/jts.ini.tmpl:/home/ibgateway/Jts/jts.ini.tmpl:ro
```

### `/opt/ib-gateway/config.ini.tmpl` (gepatcht)

```ini
TrustedTwsApiClientIPs=172.18.0.2
```

(`172.18.0.2` = investpilot Container-IP im `investpilot_default`
Docker-Network. Falls Network neu erstellt wird mit anderem Subnet,
hier anpassen.)

### `/opt/ib-gateway/jts.ini.tmpl` (gepatcht)

```ini
TrustedIPs=127.0.0.1,172.18.0.2
```

(Belt-and-Suspenders — das `TrustedTwsApiClientIPs` aus IBC config wird
beim Start in jts.ini übersetzt; dieser direkte Eintrag ist redundant
aber schadet nicht.)

---

## Reconciliation-Skript

Bot-State vs IBKR-Realität-Vergleich:

```bash
docker exec investpilot python -m scripts.ibkr_reconcile --lookback-hours 24
```

Exit codes:
- `0` = sauber (keine Drifts)
- `1` = Drift gefunden (CASH_DRIFT, PHANTOM_POSITION, MISSED_FILL)
- `2` = IBKR-Connection-Fehler

Mit `--alert` wird bei Drift Telegram-Alert ausgelöst (wenn `app.alerts`
konfiguriert).

**Empfohlen als Cron** alle 30 Min während Paper-Phase:
```cron
*/30 * * * * docker exec investpilot python -m scripts.ibkr_reconcile --alert
```

---

## Cutover-Checkliste (Real-Money)

Vor dem Wechsel von Paper-Account `DUP108015` auf Real-Account `U...`:

- [ ] Min. 4 Wochen Paper-Trading sauber gelaufen
- [ ] Reconciliation-Skript hat 7 Tage in Folge "OK" geliefert
- [ ] Kelly-Sweep auf IBKR-Paper-Daten aktualisiert (`max_fraction` re-validiert)
- [ ] IBKR Real-Account aktiv und mit min. $2k gefundet
- [ ] Telegram Kill-Switch-Drill durchgeführt
- [ ] `data/config.json`: `ibkr.readonly: false` (ist es schon)
- [ ] Backup `data/risk_state.json` und `brain_state.json` vor Switch
- [ ] In IB Gateway docker-compose `TRADING_MODE=live` (statt `paper`)
- [ ] Container restart, Login mit Real-Credentials
- [ ] Erste 48h: Dashboard alle 2h, Telegram-Alerts überwachen

---

## Offene W4+ Punkte

Aus CLAUDE.md v18-v20:

- **Asset-Class-Erweiterung**: Indizes, Futures, Commodities werfen
  noch `NotImplementedError` im Resolver
- **Async-Order-Tracking**: Heute synchron mit 30s Timeout. Bei `Submitted`
  manuell entscheiden (Cancel/Wait/Retry)
- **Bot Nr. 5 Pairs Trading**: IBKR ermöglicht jetzt echtes Shorting
  (kein CFD wie bei eToro) — geplant für Phase 3

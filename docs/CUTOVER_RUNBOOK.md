# Cutover-Runbook (v37bb, Stand 30.04.2026)

> **Zweck:** Schritt-fuer-Schritt-Anleitung fuer den Real-Money-Cutover am
> **Donnerstag 28.05.2026** und Notfall-Pfade fuer die ersten 6 Wochen
> Live-Trading. Alles in **Schweizer Zeit (CEST/CET)**.

---

## 1. Cutover-Tag (Do 28.05.2026)

### Zeitplan

| Zeit (CEST) | Schritt | Wer | Erwartung |
|---|---|---|---|
| Morgens 09:00 | **GO/NO-GO-Check** vor Cutover | du | siehe Section 2 unten |
| 14:00-15:00 | Pre-Cutover-Snapshot | du | manueller Backup-Pull, Dashboard-Screenshot |
| **15:00** | **Cutover-Switch in docker-compose.yml** | du | `TRADING_MODE: paper -> live` |
| 15:01 | `docker compose up -d --force-recreate investpilot` | du | Container restart |
| 15:02-15:05 | Pushover-Alert: "Bot connected zu IBKR Real-Account" | passiv | bot-broker-status |
| 15:30 | US-Markt oeffnet | passiv | Bot startet ersten echten Trading-Cycle |
| 15:31-16:30 | **Aktive Beobachtung** | du | Augen aufhalten, Kill-Switch griffbereit |
| 16:30 | Erstes Pushover-Trade-Alert (oder kein Trade = OK) | passiv | wenn alles ok |
| 22:00 | US-Markt schliesst | passiv | Tagesabschluss-Pushover |

### Pre-Cutover-Aktionen (W4: 18.-24.05.)

- [ ] IBKR Real-Account oeffnet (U-Prefix, nicht DU-Paper)
- [ ] 2'000 CHF eingezahlt + bestaetigt im Konto
- [ ] **IBKR Master-Account-2FA aktiviert** (Hard-Gate #6)
- [ ] Read-Only Second-User `cbaumann_view` angelegt
- [ ] Final-Backup gezogen (zusaetzlich zum Auto-Cron)
- [ ] config.json broker-Setting checken
- [ ] Dashboard-Cutover-Readiness-Card alle 8/8 gruen oder gelbe explizit dokumentiert

---

## 2. GO/NO-GO-Checkliste (am Cutover-Tag morgens)

**Alle 8 Punkte muessen GRUEN sein.** Bei einem ROT: Cutover verschieben.

| # | Check | Erwartung |
|---|---|---|
| 1 | `bot.cbaumann.ch` erreichbar | Dashboard laedt < 3 Sek |
| 2 | Cutover-Readiness-Card 7-8/8 gruen | nur Master-2FA als rot zulaessig |
| 3 | Pushover-Test-Alert kommt an | manueller Test via /api/alerts/test/pushover |
| 4 | Reconcile-Status OK | letzter Cron-Run zeigt 0 Drifts |
| 5 | Backup von gestern Nacht da | /var/backups/investpilot/state_*.tar.gz frisch |
| 6 | Trading-Toggle = ON | im Dashboard-Header gruen |
| 7 | IBKR-Broker-Status = gruen | broker-badge im Header |
| 8 | Test-Suite gruen (lokal) | `pytest` keine FAIL |

**Bei NO-GO:** Nicht switchen. Verschieben um 1 Woche.

---

## 3. Notfall-Pfade waehrend Live-Trading

### 3.1 Soft-Stop ("Bot pausieren, Positionen halten")

**Wann:** unsicheres Setup, Markt wird wild, du willst nichts neues kaufen.

| Aktion | Wo |
|---|---|
| Toggle oben rechts auf OFF | Dashboard-Header |
| Pushover bestaetigt: "KILL SWITCH AKTIV" | Handy |

→ Bot tradet ab naechstem Cycle nicht mehr. Bestehende Positionen laufen mit SL/TP weiter.

### 3.2 Hard-Kill ("Alles raus, sofort")

**Wann:** echter Notfall, Bot tradet Amok, Markt-Crash, kritischer Bug.

| Aktion | Wo |
|---|---|
| Roten "KILL SWITCH AKTIVIEREN"-Button | Dashboard-Tab unten |
| Confirm-Dialog: "Bist du sicher?" -> Ja | |
| Pushover CRITICAL Priority 2 (Emergency-Repeat) | Handy |

→ Bot setzt Trading-Flag false + versucht alle Positionen zu schliessen.

**Worst-Case** wenn Phase-3 Position-Close fehlschlaegt:
- Bot stoppt sicher (Phase 1+2 garantiert)
- **Manuell** in IBKR-App (mobile.interactivebrokers.com) Positionen schliessen

### 3.3 Manueller Sell einer einzelnen Position

**Wann:** du willst nur eine Position raus, Rest behalten.

| Aktion | Wo |
|---|---|
| Position-Tabelle -> "Verkaufen"-Button rechts | Dashboard-Tab |
| Confirm-Dialog mit aktuellem PnL -> OK | |
| Pushover bestaetigt: "Manual-Sell von 'carlos': SYMBOL geschlossen" | Handy |

### 3.4 Earnings-Halten (bewusste Exemption)

**Wann:** du willst eine Position bewusst durch Earnings halten.

| Aktion | Wo |
|---|---|
| Earnings-Watchlist-Card -> "Halten"-Button am Symbol | Dashboard-Tab |
| Bot setzt Symbol exempt (one-shot, auto-removed nach Earnings) | |
| Pushover-INFO: "Earnings-Exemption hinzugefuegt: SYMBOL" | Handy |

→ Filter wird beim naechsten Earnings wieder aktiv (automatisch).

---

## 4. Pushover-Alert-Decoder

| Alert-Text-Anfang | Bedeutung | Severity | Was tun |
|---|---|---|---|
| 💰 InvestPilot — BUY ... | Trade ausgefuehrt | INFO | nichts, log nur |
| 💰 InvestPilot — STOP_LOSS_CLOSE ... | SL hat Position geschlossen | INFO | nichts, log nur |
| 💵 InvestPilot — TAKE_PROFIT_CLOSE ... | TP hat Position geschlossen | INFO | freuen |
| 📉 InvestPilot — Drawdown ... | Tagesverlust > Threshold | WARNING | Dashboard pruefen |
| ⚠️ InvestPilot — KILL SWITCH AKTIV ... | Trading wurde gestoppt | WARNING | du oder jemand anders hat gestoppt |
| ⚠️ InvestPilot — Manual-Sell ... | Du oder Second-User hat manuell verkauft | WARNING | bewusste Aktion |
| ⚠️ InvestPilot — Reconciliation Drift ... | IBKR und Bot sehen Cash/Position unterschiedlich | WARNING | siehe Section 5.2 |
| ⚠️ InvestPilot — IBKR Connection Lost | Broker disconnected | ERROR | siehe Section 5.1 |
| ❌ InvestPilot — Order Reject ... | IBKR hat Order abgelehnt | ERROR | siehe Section 5.3 |
| 🚨 InvestPilot — Dashboard Kill Switch | EMERGENCY-Repeat alle 30s | **CRITICAL** | **sofort Handy entsperren, im Pushover-App acknowlegen** |

---

## 5. Failure-Modes + Recovery

### 5.1 IBKR-Disconnect

**Symptom:** Pushover "Connection Lost", Broker-Badge orange/rot, ueblicher Bot-Cycle bricht ab.

**Recovery (Auto):**
- Cron `0 3 * * * docker restart ib-gateway` (taeglich) faengt das meiste
- Bot-internes Reconnect mit Random ClientId

**Recovery (Manuell):**
```bash
ssh root@178.104.236.157
docker restart ib-gateway
sleep 20
docker logs ib-gateway --tail 50
```

→ Erwartung: "API server connected" innerhalb 60 Sek.

### 5.2 Reconciliation-Drift

**Symptom:** Pushover "Reconciliation Drift — N Probleme".

**Schritte:**

1. **CASH_DRIFT** (Bot-Cash != IBKR-Cash):
   - Wenn Diff < 1% → meist Slippage, OK ignorieren
   - Wenn Diff > 1% → genauer pruefen via `/api/portfolio` vs IBKR-App
2. **PHANTOM_POSITION** (IBKR hat Position, Bot kennt sie nicht):
   - Heisst meist Initial-Position (vor Bot gekauft)
   - Akzeptieren via `python -m scripts.ibkr_reconcile --accept-phantom SYMBOL`
3. **MISSED_FILL** (Bot loggte Trade, IBKR sieht keinen):
   - Wenn Order pending → wird automatisch ignoriert (v37aa)
   - Wenn IBKR nicht mehr session-history hat → False-Positive, ignorieren
4. **Unbekannte Drift** → SSH auf VPS, `python -m scripts.ibkr_reconcile --json` fuer Details

### 5.3 Order Reject

**Symptom:** Pushover ERROR mit "Order Reject" oder Bot-Log "rejected".

**Mögliche Ursachen:**
- Insufficient Funds (Cash zu niedrig)
- Margin-Limit (zu viele offene Positions)
- Outside Trading-Hours (Order vor Markt-Open submitted)
- Symbol nicht traded (z.B. Halt-Status)

**Recovery:**
- Pruefe IBKR-App ob Order nun im "Cancelled"-State
- Bot wird beim naechsten Cycle erneut versuchen (gleiches oder anderes Setup)

### 5.4 Bot-Container down

**Symptom:** `bot.cbaumann.ch` nicht erreichbar.

```bash
ssh root@178.104.236.157
docker ps -a --filter name=investpilot
# Wenn "Exited":
cd /opt/investpilot && docker compose up -d
sleep 30
docker logs investpilot --tail 100
```

### 5.5 Pushover-Alerts kommen nicht

**Test:**
```bash
curl -X POST https://bot.cbaumann.ch/api/alerts/test/pushover \
  -H "Authorization: Bearer $TOKEN"
```

**Wenn keine Push-Notification:**
- Pushover-App auf Handy: Settings → Sound check
- pushover.net Account: Subscription aktiv?
- API-Token in `data/config.json` korrekt

---

## 6. Dashboard-Quick-Reference

| Tab | Was | Wann |
|---|---|---|
| **Dashboard** | Live-Status, Positionen, Quick-Actions, Cutover-Readiness | Daily |
| **Trades** | Trade-Historie | bei Drift-Diagnose |
| **Brain** | Bot-Lernzustand, WFO, Survivorship | Wochentlich |
| **Reports** | Backtest-Ergebnisse | nach WFO/Backtest-Run |
| **Backtest** | Manual-Backtest triggern | sehr selten |
| **Settings** | Strategy-Config, Cost-Model, Filter | bei Tuning |
| **Logs** | Live-Log-Stream | bei Bug-Diagnose |
| **Ask** | KI-Frage zum Bot | optional |

---

## 7. Erste 6 Wochen Live-Trading

### Allokations-Plan

| Phase | Wann | Capital | Begruendung |
|---|---|---|---|
| **Phase 1** | 28.05. - 13.06. (2 Wo) | **2'000 CHF** | Live-Stress-Test |
| **Phase 2** | 16.06. - 18.07. (5 Wo) | **5'000 CHF** | Wenn Phase 1 sauber |
| **Phase 3** | 21.07. - 19.09. (9 Wo) | **20'000 CHF** | Nach 2 Mo Track-Record |
| **Phase 4** | Q4 2026 | nach Vertrauen | Vollskala |

### Wann eingreifen?

**Eingriff JA** bei:
- Drawdown > 15% Portfolio in einer Woche
- Bot tradet visible kaputt (Endlos-Schleifen, repetitive Trades)
- IBKR-Account-Issue (margin call, suspension)
- Kritischer Bug-Push-Alert

**Eingriff NEIN bei:**
- Einzelner Trade unrealisiert -3% bis -5% (das ist normal)
- Drawdown 5-10% (das ist im Erwartungs-Bereich)
- "Position fuehlt sich falsch an" (Bauchgefuehl ohne Daten)
- Andere Trader auf Twitter sagen anderes

### Discipline-Anker

- **Kein Eingriff bei guter Performance** — du bist nicht klueger als Bot bei den Setups die er gut macht
- **Kein Eingriff bei schlechter Performance** ausser Hard-Stop-Kriterien greifen
- **Sonntag = Review-Tag**: WFO-Resultate, Sharpe-Trend, was ist passiert
- **Bei Frustration: Lese diese Discipline-Section** statt einzugreifen

---

## 8. Kontakte

| Was | Kontakt |
|---|---|
| IBKR Support | https://www.interactivebrokers.com/en/support/ |
| IBKR Hotline (CH/EU) | +41 41 726 95 35 |
| Bot-VPS Hetzner | https://console.hetzner.cloud/ |
| Github Repo | https://github.com/carlosbaumann754-svg/investpilot |
| Pushover Account | https://pushover.net/login |

---

## 9. Versions-Historie

| Stand | Datum | Aenderung |
|---|---|---|
| v37bb | 30.04.2026 | Initial Cutover-Runbook |

**Naechste Updates** vor Cutover-Tag (28.05.) bei:
- Neuen Failure-Modes die wir live entdecken
- Neuen Recovery-Pfaden
- W4-Setup-Erfahrungen

---

**Druck dieses Dokument vor dem Cutover-Tag aus oder oeffne es als zweites Browser-Tab waehrend des Switchovers.**

🇨🇭 Carlos — viel Erfolg beim Cutover.

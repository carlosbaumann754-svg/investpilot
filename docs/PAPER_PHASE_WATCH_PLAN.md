# Paper-Phase Watch-Plan (Mo 27.04. — Real-Money Cutover ~28.05.)

Wie beobachte ich den Bot in der ersten echten Trading-Woche? Was sind grüne
und rote Signale? Wann muss ich eingreifen?

> **Status Tag 0 (So 26.04. 12:00 CEST):**
> Bot tradet seit 25.04. 15:20 auf IBKR Paper-Account DUP108015. Gestern Nacht
> 128/128 Cycles erfolgreich (post Singleton-Pool-Fix). Heute Vormittag noch
> v26 (patchAsyncio, Loop-Errors weg) + v27 (Pairs Signals) + v28
> (Trading-Hours-Filter). DNS-Wechsel cbaumann.ch -> Cloudflare aktiv (1-24h
> Propagation). Disabled_symbols-Liste geleert (alle 71 Symbole werden Sonntag
> neu evaluiert).

---

## Wo schaue ich rein?

### Primär — Dashboard (alle 4-6h tagsüber)

URL: aktuell via SSH-Tunnel `http://localhost:8000`, ab Mo nachmittag
voraussichtlich `https://bot.cbaumann.ch` (Caddy holt Cert nach DNS-Propagation).

**Header-Badge** soll zeigen:
- `IBKR · PAPER` ✅ (gruen, "broker-badge-ok")
- Account `DUP108015` im Tooltip
- Equity ~$1.062.145 (Paper-Balance)

**Wichtige Cards (top-down):**

1. **Portfolio-Wert** — soll sich tagsueber bewegen sobald Trades passieren
2. **Cash / Investiert / Positionen** — werden ab erstem Trade ungleich 0
3. **Bot-Gesundheit (Watchdog)** — soll **HEALTHY** sein (gruen). Wenn ERROR:
   `/api/diagnostics` checken
4. **Regime Status** — VIX, F&G, MacroScore. BEAR = defensiv (wenig BUYs)
5. **Trade-Historie** — neue Eintraege ab US-Market-Open
6. **Watchdog Log** — nach Errors / Warnings filtern

### Sekundaer — VPS-Logs (1x/Tag oder bei Verdacht)

```bash
ssh -i ~/.ssh/hetzner_investpilot root@178.104.236.157 \
  'docker logs investpilot --since 24h --tail 100'
```

Suche nach:
- `Trading-Zyklus abgeschlossen` (regulaer)
- `Cycle wird uebersprungen` (sollte SELTEN sein)
- `ERROR` (sollte 0 sein)
- `Order .* status=Filled` (echte Fills!)

### Reconciliation-Cron (automatisch, alle 30 Min)

```bash
ssh root@178.104.236.157 'tail -50 /opt/investpilot/logs/reconcile.log'
```

Soll alle 30 Min `Status: OK` zeigen + `Bot Cash == IBKR Cash` matchen.

---

## Gruene vs. rote Signale

### 🟢 GRUEN (alles laeuft)

| Was | Erwartung |
|---|---|
| Cycles/24h | ~74 (5-Min-Intervall waehrend RTH 13:30-20:00 UTC, sonst pausiert v28) |
| Cycles geskippt | 0 (oder maximal 1-2 bei IBG-Hangs) |
| Reconciliation | Status: OK alle 30 Min |
| brain_state.json mtime | < 5 Min alt (waehrend RTH) |
| Cloud-Backup | alle ~5 Min, "OK 22 Dateien" |
| Drawdown | < 2% taeglich, < 5% wochentlich |
| Watchdog Status | HEALTHY |
| Telegram | (deaktiviert per User-Wahl) |

### 🟡 GELB (Beobachten, kein Eingriff)

- 1-2 Cycle-Skips/Tag (z.B. wegen Reconciliation-Cron-Race)
- Drawdown 2-5% — Bot's Risk-Manager pausiert auto bei -5%
- "Order Cancelled" wegen Limit nicht erreicht (Auto-Cancel ok)
- ROKU-Repeat-Order (Bot hat ROKU als Top-Signal — kein Bug)

### 🔴 ROT (Eingriff noetig)

| Symptom | Aktion |
|---|---|
| Mehrere Cycles in Folge skipped (>3) | IBG-Container-Restart, dann Bot-Restart |
| Watchdog ERROR mit "Connection-Lost" | broker-status checken, ggf. Container-Neustart |
| Reconciliation: CASH_DRIFT > $100 | Manueller Trade-History vs IBKR-Portal Vergleich |
| brain_state.json mtime > 1h alt waehrend RTH | Bot crashed — Logs checken |
| Daily Drawdown > 5% | Bot pausiert sich selbst — pruefen ob legit oder Bug |
| Order-Submit failure-Patterns | Logs `Error 326`, `not allowed` etc. -> docs/IBKR_SETUP.md |

---

## Tageszeitplan (typischer Mo-Fr)

| Zeit (CEST) | Was passiert |
|---|---|
| **00:00-15:30** | Bot pausiert (US-Markt zu, v28 Trading-Hours-Filter) |
| **15:30** | US-Markt oeffnet -> Bot startet ersten Cycle |
| **15:30-22:00** | Bot trade'd alle 5 Min, ~74 Cycles |
| **22:00-00:00** | Bot pausiert wieder |
| **22:30 (US-EOD)** | Equity-Snapshot fuer Equity-Curve |
| **Sa 06:00 UTC** | Wartungsblock (cron) |
| **So 06:00 UTC** | Wochen-Backtest + Universe-Health-Check |

---

## Erste 5 Tage Watch-Frequenz

| Tag | Frequenz | Fokus |
|---|---|---|
| Mo 27.04. | alle 1-2h | erster echter Trading-Tag — schaut sich der Bot stabil an? |
| Di 28.04. | alle 3-4h | repetitive Stabilitaet |
| Mi 29.04. | 2x/Tag | normale Beobachtung |
| Do 30.04. | 2x/Tag | wie Mi |
| Fr 02.05. | 2x/Tag + Wochenend-Review | Reconciliation-Logs der Woche durchschauen |

---

## Was ich NICHT tun werde

- 🚫 **Nicht intervenieren bei einzelnen Loss-Trades** — Strategie ist auf
  3.5 Sharpe getestet, einzelne Verluste sind normal
- 🚫 **Nicht config aendern waehrend Live-Phase** — Stop-Loss/Take-Profit
  Drift waere Bug. Aenderungen erst Wochen-Review
- 🚫 **Nicht broker auf real switchen** — Real-Money kommt am ~28.05. nach
  4-Wochen-Validierung
- 🚫 **Nicht Bot-Cycle manuell triggern** — Scheduler macht alle 5 Min
- 🚫 **Keine Panik bei -5% Daily-Drawdown** — Bot pausiert sich selbst,
  in 24h reaktiviert er

---

## Hard-Gates fuer Real-Money-Cutover (Stand vor ~28.05.)

Aus `docs/IBKR_SETUP.md` Cutover-Checkliste:

- [ ] Min. 4 Wochen Paper-Trading sauber gelaufen
- [ ] Reconciliation 7 Tage in Folge "OK"
- [ ] Kelly-Sweep auf IBKR-Paper-Daten aktualisiert (Kelly = 0.04 bestaetigt
      oder reduziert)
- [ ] IBKR Real-Account aktiv und mit min. CHF 2k gefundet
- [ ] Telegram Kill-Switch-Drill durchgefuehrt (oder Dashboard-Drill, da
      Telegram bewusst nicht aktiviert)
- [ ] 2FA aktiviert (vor Real-Money zwingend!)
- [ ] In IBG docker-compose `TRADING_MODE=live` (statt `paper`)
- [ ] Backup `data/risk_state.json` und `brain_state.json` vor Switch

---

## Quick-Reference Commands

```bash
# Bot-Status schnellcheck
ssh root@178.104.236.157 'docker exec investpilot python -c "
from app.config_manager import load_config
print(load_config().get(\"broker\"))"'

# Letzter Cycle
ssh root@178.104.236.157 'docker logs investpilot --since 30m | grep "abgeschlossen" | tail -3'

# Reconciliation manuell
ssh root@178.104.236.157 'docker exec investpilot python -m scripts.ibkr_reconcile --lookback-hours 1'

# Notfall: Container-Neustart (Bot)
ssh root@178.104.236.157 'cd /opt/investpilot && docker compose -f docker-compose.vps.yml restart investpilot'

# Notfall: Container-Neustart (IBG)
ssh root@178.104.236.157 'cd /opt/ib-gateway && docker compose restart'

# Kill-Switch (alle Trades stoppen)
# via Dashboard: Kill-Switch-Card -> "KILL SWITCH AKTIVIEREN" Button
```

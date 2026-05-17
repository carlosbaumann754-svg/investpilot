# 🚨 InvestPilot Notfall-Cheat-Sheet

**Stand:** 2026-05-08 11:50 CEST | **Cutover-Restzeit:** 20 Tage
**Use-Case:** Du bist unterwegs (Handy), Pushover-Alert kommt rein, brauchst sofort Aktion.

> **Korrigiert 08.05.2026**: Pause-Befehl in Schritt E nutzt jetzt `trading_enabled.flag` (echter Mechanismus) statt `bot_enabled` in config.json (existiert nicht im Code). Vorher hatte der Pause-Befehl gestern Abend 07.05. **nicht gewirkt** — Bot lief die ganze Nacht weiter.

---

## Schritt 1 — Was ist die Lage?

**Pushover-Alert lesen.** Wichtigste Felder:
- `[CRITICAL]` vs `[WARNING]` — CRITICAL = sofort handeln, WARNING = morgen anschauen reicht meistens
- Alert-Typ:
  - `MISSED_FILL: <SYMBOL>` → Bot loggte SCANNER_BUY, IBKR-Match fehlt
  - `RECONCILE_DRIFT: <BETRAG>` → Bot-Cash ≠ IBKR-Cash mismatch
  - `HEARTBEAT_LOST` → Bot-Container down oder Net-Issue
  - `ORDER_REJECTED: <SYMBOL>` → IBKR hat Order abgelehnt (z.B. Margin, Trading-Hours)
  - `STALE_ORDER: <SYMBOL>` → Pending Order >48h ohne Match
  - `PHANTOM_POSITION: <SYMBOL>` → IBKR hat Position, Bot-trade-history kennt sie nicht (Initial-Position)

---

## Schritt 2 — SSH-Zugang vom Handy

**Termius App** (iOS, free) → Tab "Hosts" → **Hetzner InvestPilot** auswählen → Connect.

Falls Termius nicht eingerichtet:
- Server: `178.104.236.157`
- User: `root`
- Key: `hetzner_investpilot` (sollte synchronisiert sein via Termius Cloud)
- Tailscale muss auf Handy aktiv sein (App öffnen, grün = OK)

---

## Schritt 3 — Notfall-Befehle (copy-paste)

### A. Schnell-Check: Bot lebt?
```bash
docker ps | grep investpilot
```
Erwartung: 1 Container "Up X minutes/hours". Falls leer → Bot crashed.

### B. Bot-Logs der letzten 5 Minuten
```bash
docker logs investpilot --tail 100 --since 5m
```

### C. Reconcile-Status (Bot vs IBKR Cash)
```bash
docker exec investpilot python -m scripts.ibkr_reconcile --lookback-hours 720 --missed-fill-lookback-hours 3 --cash-tolerance-pct 0.7 2>&1 | grep -E "Status:|Drifts|Bot Cash|IBKR Cash|Keine"
```
Erwartung: "Status: OK, ✅ Keine Drifts gefunden". Falls Drift sichtbar → KRITISCH.

### D. E27 Feature-Flag ROLLBACK (Tracker abschalten)
**Wann:** Wenn ein E27-Async-Bug aufpoppt (Race-Condition, Crash, falsche Stale-Marker).
```bash
docker exec investpilot python -c "
import json
with open('/app/data/config.json', 'r') as f:
    c = json.load(f)
c['realtime_status_tracker']['enabled'] = False
with open('/app/data/config.json', 'w') as f:
    json.dump(c, f, indent=2)
print('E27 disabled')
" && docker restart investpilot
```
Effekt: Bot läuft weiter, aber ohne E27. Du fällst zurück auf v37dh-Stand (Submit-Pfad-Statuse + Reconcile-Cron).

### E. Bot komplett pausieren (Soft-Stop) — KORRIGIERT 08.05.2026
**Wann:** Massive Drifts, Trade-Logik kaputt, Cutover-Risiko zu hoch.

**SSH-Variante (zuverlässig, keine Auth nötig):**
```bash
echo "false" > /opt/investpilot/data/trading_enabled.flag && echo "Bot pausiert (Flag=false)"
```

**API-Variante (alternativ via Dashboard-Token):**
```bash
curl -X POST -H "Authorization: Bearer $TOKEN" https://bot.cbaumann.ch/api/trading/stop
```

Effekt: Scheduler prüft die Flag bei jedem Trading-Cycle (~1-5 Min). Findet "false" → überspringt Trading-Logik. Keine neuen Trades. Bestehende Positionen bleiben offen + Trailing-SL aktiv.

**Reaktivierung:**
```bash
echo "true" > /opt/investpilot/data/trading_enabled.flag && echo "Bot reaktiviert (Flag=true)"
```

**WICHTIG — Lehre aus 07.05.2026**: Vorher stand hier ein Befehl der `bot_enabled=False` in `config.json` setzte. Dieser Key existiert NICHT im Bot-Code, wurde nie geprüft. Der Bot lief deshalb gestern Abend trotz "Pause"-Befehl die ganze Nacht weiter. Erst der korrigierte Befehl oben pausiert tatsächlich.

### F. Bot HARD-KILL (nuklear)
**Wann:** Sofort stoppen, koste was es wolle.
```bash
docker stop investpilot
```
Effekt: Container down. **Keine Stop-Loss-Wachung mehr** — riskant bei offenen Positionen, aber sicher für jede Code-Bug-Situation.

### G. Phantom-Position whitelisten (kein Drift mehr)
**Wann:** Reconcile alarmiert über alte Initial-Positionen die Bot vor >24h gekauft hat.
```bash
docker exec investpilot python -m scripts.ibkr_reconcile --accept-phantom SYMBOL_1 SYMBOL_2
```
Effekt: Symbole werden in `data/reconcile_accepted_phantoms.json` whitelisted. Reconcile meldet sie nicht mehr.

### H. .env-Variable hinzufügen/ändern — v37g (08.05.2026)
**Wann:** Neue Variable, oder bestehende ersetzen.

**Korrekt (Auto-Backup + Verify):**
```bash
bash /opt/investpilot/scripts/safe_env_update.sh "KEY=value"
```

**❌ NIEMALS:** `cat > .env <<EOF` oder `echo "X=y" > .env` — überschreibt ALLE existierenden Variables.

**Lehre 08.05.2026**: `cat >` für `.env` hat alle Login-Credentials, JWT-Secret, GitHub-Token, SMTP-Vars gelöscht — Bot war 4h nicht login-fähig. Wrapper-Script verhindert das strukturell. Plus daily Backup-Cron 04:30 UTC sichert `.env` nach `/var/backups/investpilot/env/` mit 30d Retention.

**Manueller .env-Restore (falls je nötig):**
```bash
ls /var/backups/investpilot/env/    # Liste der Backups
cp /var/backups/investpilot/env/.env.20260508 /opt/investpilot/.env
docker compose -f /opt/investpilot/docker-compose.vps.yml --env-file /opt/investpilot/.env up -d investpilot
```

---

## Schritt 4 — Wann was nutzen?

| Situation | Befehl | Reihenfolge |
|-----------|--------|-------------|
| MISSED_FILL nach E27-Aktivierung | C (Reconcile) → D (E27 off) | 2 Befehle |
| Reconcile-Drift sichtbar | C bestätigen → E (Pause) | 2 Befehle |
| Heartbeat-Lost | A (Bot lebt?) → B (Logs) | 2 Befehle |
| Komplett-Crash, Bot down | A bestätigen → in Termius `docker start investpilot` | 2 Befehle |
| Subjektiv: irgendwas riecht falsch | E (Pause) — kein Schaden, kein Stress | 1 Befehl |
| PHANTOM-Spam für alte Positionen | G (akzeptieren) | 1 Befehl |

---

## Schritt 5 — Carlos-Eskalations-Entscheidung

| Befund | Aktion | Cutover-Impact |
|--------|--------|----------------|
| 1× Alert, keine Drift, Reconcile OK | Schlafen — morgen anschauen | 0 |
| Mehrere Alerts in 1h | Ruhe → E27 rollback (D) → wieder schlafen | 1-2h Beobachtung Verlust |
| Drift sichtbar, Bot reagiert chaotisch | Pause (E) → Carlos mit mir Mo Sync | 1-2 Tage Verlust, Cutover noch machbar |
| Komplett-Crash + Margin-Position offen | Hard-Kill (F) + IBKR Web-Login + manuell Position schliessen | Cutover-Verschiebung wahrscheinlich |

---

## Schritt 6 — Was du NICHT alleine machst

- ❌ Code-Änderungen am Bot (kein git push, keine Code-Edits)
- ❌ Config-Werte ändern ausser den E27/trading_enabled-Flags hier
- ❌ Master-Branch-Manipulation
- ❌ IBKR-Account-Settings anpassen
- ❌ Position-Liquidation manuell ausser bei IBKR-Web-Login mit cbaumann_view (nur view) — du hast nur Read-Only Access, das ist Absicht

→ Bei Code/Config/Architektur-Fragen: **warten bis Sync mit mir**, niemals improvisieren.

---

## Kontakte (falls 100% Notfall)

- IBKR Support DACH: +49 30 22861700 (Account: DUP108015, Paper, kein Real-Money-Risiko aktuell!)
- Hetzner Support: console.hetzner.cloud (Login mit Carlos's Mail)
- Pushover-Status: status.pushover.net

**Wichtigste Erinnerung: Bei Pushover-Alerts ist NICHTS akut.** IBKR Paper = Spielgeld. Real-Cutover ist 01.06.2026 (Mo, verschoben von 28.05. am 17.05.) — bis dahin ist jeder Alert ein Geschenk an die Robustheit. Ruhig bleiben, durchatmen, Schritt 1-3 abarbeiten.

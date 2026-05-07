# 🚨 InvestPilot Notfall-Cheat-Sheet

**Stand:** 2026-05-07 18:00 CEST | **Cutover-Restzeit:** 21 Tage
**Use-Case:** Du bist unterwegs (Handy), Pushover-Alert kommt rein, brauchst sofort Aktion.

---

## Schritt 1 — Was ist die Lage?

**Pushover-Alert lesen.** Wichtigste Felder:
- `[CRITICAL]` vs `[WARNING]` — CRITICAL = sofort handeln, WARNING = morgen anschauen reicht meistens
- Alert-Typ:
  - `MISSED_FILL: <SYMBOL>` → Order intent vs reality drift (sollte mit E27 nicht mehr kommen!)
  - `RECONCILE_DRIFT: <BETRAG>` → Bot-Cash ≠ IBKR-Cash mismatch
  - `HEARTBEAT_LOST` → Bot-Container down oder Net-Issue
  - `ORDER_REJECTED: <SYMBOL>` → IBKR hat Order abgelehnt (z.B. Margin, Trading-Hours)
  - `STALE_ORDER: <SYMBOL>` → Pending Order >48h ohne Match (Strategie B)

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
docker exec investpilot python -c "from scripts.ibkr_reconcile import main; main()" 2>&1 | tail -20
```
Erwartung: "Status: OK, Bot Cash $X = IBKR Cash $X". Falls Drift >$10 → KRITISCH.

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

### E. Bot komplett pausieren (Soft-Stop)
**Wann:** Massive Drifts, Trade-Logik kaputt, Cutover-Risiko zu hoch.
```bash
docker exec investpilot python -c "
import json
with open('/app/data/config.json', 'r') as f:
    c = json.load(f)
c['bot_enabled'] = False
with open('/app/data/config.json', 'w') as f:
    json.dump(c, f, indent=2)
print('Bot disabled')
" && docker restart investpilot
```
Effekt: Bot wacht auf, prüft `bot_enabled=false`, schläft sofort wieder ein. Keine neuen Trades. Bestehende Positionen bleiben offen.

### F. Bot HARD-KILL (nuklear)
**Wann:** Sofort stoppen, koste was es wolle.
```bash
docker stop investpilot
```
Effekt: Container down. **Keine Stop-Loss-Wachung mehr** — riskant bei offenen Positionen, aber sicher für jede Code-Bug-Situation.

---

## Schritt 4 — Wann was nutzen?

| Situation | Befehl | Reihenfolge |
|-----------|--------|-------------|
| MISSED_FILL nach E27-Aktivierung | C (Reconcile) → D (E27 off) | 2 Befehle |
| Reconcile-Drift sichtbar | C bestätigen → E (Pause) | 2 Befehle |
| Heartbeat-Lost | A (Bot lebt?) → B (Logs) | 2 Befehle |
| Komplett-Crash, Bot down | A bestätigen → in Termius `docker start investpilot` | 2 Befehle |
| Subjektiv: irgendwas riecht falsch | E (Pause) — kein Schaden, kein Stress | 1 Befehl |

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
- ❌ Config-Werte ändern ausser den E27/bot_enabled-Flags hier
- ❌ Master-Branch-Manipulation
- ❌ IBKR-Account-Settings anpassen
- ❌ Position-Liquidation manuell ausser bei IBKR-Web-Login mit cbaumann_view (nur view) — du hast nur Read-Only Access, das ist Absicht

→ Bei Code/Config/Architektur-Fragen: **warten bis Sync mit mir**, niemals improvisieren.

---

## Kontakte (falls 100% Notfall)

- IBKR Support DACH: +49 30 22861700 (Account: DUP108015, Paper, kein Real-Money-Risiko aktuell!)
- Hetzner Support: console.hetzner.cloud (Login mit Carlos's Mail)
- Pushover-Status: status.pushover.net

**Wichtigste Erinnerung: Bei Pushover-Alerts ist NICHTS akut.** IBKR Paper = Spielgeld. Real-Cutover ist 28.05. — bis dahin ist jeder Alert ein Geschenk an die Robustheit. Ruhig bleiben, durchatmen, Schritt 1-3 abarbeiten.

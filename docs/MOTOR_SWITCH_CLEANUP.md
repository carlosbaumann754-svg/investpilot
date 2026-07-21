# Motor-Switch Cleanup — Audit & Aufräum-Plan

**Erstellt:** 2026-06-09 (nach dem Signal-Stack-Live-Switch, Phase 4)
**Zweck:** Sichtbar machen, welche Bot-Bestandteile nach dem Wechsel der **Auswahl-Engine**
(TA-Score → 5-Signal-EDGAR-Stack) aktiv / toter Fallback / verwaist sind — und was
sich aufräumen lässt **ohne** die gerade gestartete Validierungs-Soak-Uhr zu resetten.

> Prinzip: **Visibility vor Optimization.** Dieses Dokument macht den Hybrid-Zustand
> transparent. Es wird NICHT alles sofort umgebaut — vieles braucht erst Live-Daten
> des neuen Motors (die es noch nicht gibt) oder würde Trading-Verhalten ändern
> (= Soak-Reset). Pro Item ist die Konsequenz markiert.

---

## Verifizierter Stand (VPS live-config, 2026-06-09)

| Key | Wert | Bedeutung |
|---|---|---|
| `use_signal_stack` | **True** | Neuer Motor (Signal-Stack) ist die aktive Auswahl-Engine |
| `wfo_drift_watchdog.enabled` | **False** | Bewusst deaktiviert seit 29.05. (Soak-Cry-Wolf-Schutz) — korrekt aus |
| `optimizer.enabled` | **True** (`sunday_02:00`) | ⚠️ Wöchentlicher **TA-Optimizer** läuft noch — optimiert den ALTEN Motor |
| `meta_labeling.shadow_mode` | True | Shadow-only, kein Trading-Einfluss |
| `demo_trading.use_ml_scoring` | False | ML-Scoring aus |

> ⚠️ **Hinweis zum vorangegangenen Sub-Agent-Audit:** Ein Explore-Agent hat mehrere
> Config-Werte falsch zitiert (z.B. tp_tranches, trailing_sl_pct) und behauptet, der
> Drift-Watchdog laufe „enabled täglich". **Beides falsch** — die Werte oben sind
> direkt aus der VPS-live-config verifiziert. Die *Struktur*-Aussagen des Agents
> (welche Datei was tut) sind brauchbar, die *Zahlen* nicht.

---

## 🟢 Bucket A — Engine-agnostisches Chassis (bleibt, gilt 1:1 für den neuen Motor)

Alles, was NACH der Auswahl greift, ist motor-neutral und voll aktiv:

- **Risk/Sizing:** Kelly (`kelly_sizing`, half-kelly, max 0.04), Volatilitäts-Sizing, Konzentrations-Caps (`symbol_concentration`: max 1 Pos/Symbol, 15% Exposure), Cash-Reserve (10%), max_positions Tier-Map.
- **Exit:** Stop-Loss (-3.0), Take-Profit (15), Trailing-SL (ab +0.8%, 1.8%), TP-Tranchen (30%@4 / 30%@8 / 40%@15), Time-Stop.
- **Regime/Markt-Guards:** Regime-Filter (VIX-Halt 35), Earnings-Blackout, VIX-Term-Structure, Hedging, Macro-Signals.
- **Infra:** OrderStatusTracker (E27), Reconcile, Kill-Switch, Watchdog/Heartbeat, Pushover, Sentry, IBKR-Contract-Resolver (`qualifyContracts`), Universe-Health-Watcher.

→ **Nichts davon ist überflüssig.** Umschließt die Trades des neuen Motors unverändert.

> ⚠️ **Nuance (erkannt 10.06., 5. Alt-Motor-Fund):** Die MECHANISMEN sind motor-neutral, aber einige **WERTE** der Exit-/Sizing-Schicht (Stop-Loss −3%, Take-Profit 15, TP-Tranchen, Trailing, **Time-Stop 10d**, **Kelly-Fraction 0.04**) sind für den alten TA-Momentum-Motor (kurzer Horizont) getunt. Der neue Motor ist fundamental (längerer Horizont) → Time-Stop 10d + SL −3% sind die klarsten Mismatches. Werte sind als Start **sicher (konservativ)**, aber **nicht getunt** → Re-Kalibrierung als Phase-3-Item (datenabhängig, siehe Fahrplan). Wichtig fürs Soak-Interpretieren: der Soak misst „neue Auswahl + alte Exits" → unterschätzt evtl. die Edge.

---

## 🟡 Bucket B — Alter Motor (TA-Selektion), jetzt Fallback

- TA-Scoring (`market_scanner.score_asset` / `analyze_single_asset`): nicht ausgebaut, nur ausgekuppelt. Springt automatisch an, wenn `use_signal_stack=false` ODER der Stack-Pfad fehlschlägt (fail-safe).
- **Wichtige Kopplung (verifiziert):** `_scan_via_signal_stack` ruft für jeden Stack-Kandidaten weiterhin `analyze_single_asset` (yfinance) auf — für die **Sizing-/Entry-Analyse**, NICHT für die Auswahl. Heisst: fällt yfinance aus → Stack-Buy-Liste wird leer (fail-safe, aber ein Single-Point-of-Dependency, den wir später entkoppeln können).
- Altes `ASSET_UNIVERSE` (51 ETFs/Mega-Caps): bleibt für den Fallback-Pfad.

→ **Behalten** als Sicherheitsnetz. Kein Handlungsbedarf.

---

## 🔴 Bucket C — Verwaist / für den ALTEN Motor kalibriert

| Instrument | Status | Konsequenz bei Änderung | Empfehlung |
|---|---|---|---|
| **WFO / Backtester** | Optimiert/validiert TA-Strategie (SL/TP/min_score), kennt den Stack nicht | – (read-only Tooling) | **Behalten, dormant** + als „TA-only/legacy" dokumentieren |
| **WFO-Drift-Watchdog** | `enabled=False` (korrekt). Baseline = TA-WFO-PF → für neuen Motor falscher Massstab | DATEN-ABHÄNGIG | **Re-Baseline nötig** (braucht ≥30 Live-Trade-Tage des neuen Motors), DANN re-enable. Task #4 neu skopen. |
| **Wöchentlicher Optimizer** | `enabled=True`, `sunday_02:00`. Optimiert TA-Params, hat lt. `_audit` schon Live-Params resettet | TRADING-relevant (kann Params ändern) | **ENTSCHEIDUNG: während Soak pausieren** (`optimizer.enabled=false`) → schützt Soak-Kriterium 1 (stabile Params) |
| **disabled_symbols (21)** | Aus TA-Per-Symbol-Backtest der 51er-Welt. Stack nutzt eigenes S&P-600-Universum | Ändern = ändert Fallback-Verhalten + `bootstrap_v12` re-synct beim Boot | **Unangetastet lassen** (konservativ). Nur dokumentieren. |
| **min_scanner_score (30/40)** | TA-Score-Schwelle. Im Stack-Modus via `(s-50)*2` + hartcodierte 10/25-Schwellen → Config-Wert ist „Ghost" | Ändern = ändert TA-Fallback + Backtest | **Lassen + dokumentieren** („nur TA-Fallback"). Für den Stack später eigene Schwellen kalibrieren. |
| **ML-Scoring / Meta-Labeling** | aus / shadow-only | – | **Behalten** als optionales TA-Feature. Kein Stack-Einfluss. |

---

## Was lässt sich WANN tun?

### ✅ Heute Abend (trading-neutral, kein Soak-Reset)
1. **Dieses Sichtbarkeits-Dokument** (erledigt).
2. **Roadmap/Recap:** Cleanup als Soak-Phase-Arbeitspaket eintragen; Task #4 von „re-enable" auf „**re-baseline für neuen Motor, dann re-enable**" umskopen.
3. *(Optional, nach Freigabe)* **Optimizer während Soak pausieren** — siehe Entscheidung unten.

### 🟠 Entscheidung nötig (verhaltens-relevant)
- **Wöchentlichen TA-Optimizer pausieren?** Empfehlung **JA**: Er optimiert einen Motor, den wir nicht mehr zur Auswahl nutzen, und kann Live-Params ändern → würde die Soak-Stabilität (Kriterium 1: ≥2 Wochen stabile Params) unterlaufen. Pausieren = `optimizer.enabled=false` auf VPS, reversibel, **stabilisierend** (kein Soak-Reset, im Gegenteil).

### ⛔ NICHT heute Abend möglich (daten-abhängig — per Definition)
- **WFO-Drift-Watchdog neu eichen** + **Signal-Stack via WFO validieren**: braucht ≥30 Live-Trade-Tage des neuen Motors. Die existieren erst im Soak. Das ist der eigentliche „Generalüberholungs"-Kern — und er ist ein **Soak-Phasen-Projekt**, kein Heute-Abend-Task.

---

## Kernaussage
Der Switch ist architektonisch sauber (Chassis motor-neutral, fail-safe). Der „Hybrid-
Zustand" ist normale Technical Debt nach einem Motor-Wechsel — **nicht gefährlich**.
Die *echte* Aufräum-/Eich-Arbeit braucht Live-Daten und gehört in den Soak. Heute Abend
sinnvoll = **Sichtbarkeit schaffen + den TA-Optimizer für die Soak-Dauer stilllegen**,
damit nichts unter dem neuen Motor an den Parametern dreht.

---

# Retirement-Register (Kill-Kriterien) — Stand 10.06.2026

> **Governance-Prinzip:** Dormant ≠ permanent. Jedes Tool, das wir abgeschaltet, in
> Shadow gestellt oder als Fallback geparkt haben, steht auf einem **Retirement-Pfad** —
> NICHT auf Dauer-Standby. Sobald sein Beweis-Kriterium erfüllt ist, wird es **endgültig
> entfernt (Code + Config)**, damit kein Zombie-Ballast wächst.
> **Review-Trigger:** Cutover-Tag (04.08.2026) + jede Soak-Phasen-Entscheidung.
>
> **ABER — drei Klassen, unterschiedlich aggressiv (NICHT blind alles killen):**
> - 🟢 **KILL-WHEN-PROVEN** (pure Cruft/verwaist): sobald bewiesen unnötig → raus, ohne Zögern.
> - 🔁 **RE-PURPOSE** (Tooling, das der neue Motor wiederverwenden kann): umbauen statt killen.
> - 🛡️ **SAFETY-NET** (Fallbacks/Schutz): nur mit STARKEM Live-Beweis killen — ein Netz zu früh zu entfernen ist gefährlicher als etwas Cruft zu behalten.

| Posten | Status jetzt | Klasse | Beweis-Kriterium für endgültiges Kill | Kill-Aktion |
|---|---|---|---|---|
| **Meta-Labeler** | shadow | 🟢 | Stack-Live-Daten zeigen: Filter bringt KEINEN Mehrwert | meta_labeler-Modul + meta_labeling-config + Dashboard-Card raus |
| **ML-Scoring** | aus | 🟢 | mit TA-Pfad-Kill (gehört zum TA-Motor) | ml_scorer + use_ml_scoring raus |
| **disabled_symbols (21)** | orphaned | 🟢 | mit TA-Pfad-Kill (51er-TA-Welt) | Liste + bootstrap-Sync raus |
| **alter universe_health-Producer (backtester)** | sekundär | 🟢 | Stack-Shadow-Producer Wochen stabil | backtester-universe_health-Write raus (Shadow = einzige Quelle) |
| **TA-Optimizer** | pausiert | 🔁/🟢 | Soak grün UND (Stack hat eigenes Tuning ODER braucht keins) | kein Tuning nötig → Cron+Modul+config raus; sonst auf Stack umbauen |
| **WFO / Backtester (TA)** | dormant | 🔁 | Soak grün → Stack-WFO darauf ODER TA endgültig verworfen | NICHT blind killen — ggf. für Stack-WFO wiederverwenden, TA-Teile dann raus |
| **WFO-Drift-Watchdog** | disabled (alte Baseline) | 🔁 | ≥30 Stack-Trades → re-baseline (Task #4); wenn nutzlos → pensionieren | re-baseline behalten ODER Modul+config raus |
| **Regime Fear-&-Greed-Block** | AUS (Variante A, Test) | 🛡️ | Soak klar: Angst-Traden besser → Block endgültig raus; schlechter → zurück | wenn bestätigt: F&G-Halt-Logik in check_regime_filter ENTFERNEN (nicht nur Schwellen=0) |
| **TA-Score-Selektion (Fallback)** | Fallback (dormant) | 🛡️ | Neuer Motor MONATE Live-stabil + qualifyContracts robust | GROSSE Entscheidung: market_scanner-TA-Pfad + ASSET_UNIVERSE + Schicht-B-Penalties raus. NUR mit starkem Beweis — bis dahin behalten. |
| **VIX>35-Hard-Halt** | aktiv | 🛡️ | — (kein Kill geplant, echter Tail-Schutz) | BEHALTEN |

**Kurz:** Die 🟢-Posten sterben, sobald der Stack sich beweist (grösstenteils am Cutover-Tag).
Die 🔁-Posten werden umgebaut statt gekillt. Die 🛡️-Posten (Sicherheitsnetze) bleiben, bis
Live-Daten über Monate beweisen, dass sie überflüssig sind — ein Netz zu früh zu entfernen
ist der teurere Fehler.

---

# Display-Retirement (Dashboard-Anzeigen auf Alt-Motor) — Stand 10.06.2026

Schwester-Register zum Code-Retirement-Register + der Memory-Regel `feedback_display_replace_not_accumulate`. Anzeigen sterben, sobald das dahinterliegende Tool stirbt — ODER werden vorher ersetzt, wenn sie den FALSCHEN Motor zeigen.

| Display | Status | Aktion |
|---|---|---|
| **Walk-Forward-Card (TA-WFO −0.92, IS/OOS, Run-Button)** | ✅ ERLEDIGT 10.06. | In-place ersetzt durch Stack-Validierung (commit d2ef055) |
| **Survivorship-Card: TA-WFO-Bias-Korrektur** (surv-wfo-block + History-Spalte) | ✅ ERLEDIGT 10.06. | Entfernt; Rest (alive/dead + generische Sharpe-Reduktions-Schätzung) ist engine-agnostisch + bleibt |
| **Optimizer-Card** (TA-Param-Optimierung, OOS-Decay) | offen | stirbt mit dem TA-Optimizer (pausiert); zeigt aktuell korrekt den pausierten Stand → kein „falscher Motor" |
| **ML Feature Importances** (RSI/MACD etc.) | offen | stirbt mit ML-Scoring (aus); zeigt TA-ML-Features |
| **Settings: SL/TP/Min-Scanner-Score „WFO-locked"** | offen | SL/TP motor-agnostisch (bleiben); min_scanner_score + „WFO-locked"-Label = TA-Fallback → sterben mit dem TA-Pfad |
| **Regime-Strategien-Tooltip** („Momentum-Bonus im Bull…") | offen | beschreibt TA-Regime-Logik; Tooltip-Text bei Gelegenheit auf Stack anpassen |
| **Backtest-Tab** (TA-WFO/Backtests) | offen | RE-PURPOSE: wird Stack-Backtest-Tool ODER stirbt mit TA |

**Prinzip:** Das Dashboard zeigt nie den FALSCHEN (alten) Motor. Anzeigen von dormanten Tools, die KORREKT deren pausierten/aus-Stand spiegeln, bleiben — bis das Tool selbst pensioniert wird (dann fällt die Anzeige mit). Nur Anzeigen, die den ersetzten Motor als aktuell ausgeben, werden sofort ersetzt/entfernt (TA-WFO, beide oben erledigt).

---

# Tool-für-Tool-Fahrplan (nach Trigger sequenziert) — Stand 10.06.2026

Konsolidiert das Code-Retirement-Register + Display-Retirement + die Soak-Roadmap zu EINEM abhakbaren Ablauf: jedes Tool wird angefasst, **wenn sein Trigger feuert** — nicht früher, nicht alles auf einmal. Aktion je Tool: 🔧 **überarbeiten/anpassen** · 🔁 **re-purpose (auf Stack umbauen)** · 🗑️ **löschen**. Review-Takt: jede Soak-Entscheidung + Cutover-Tag (04.08.).

### Phase 1 — JETZT / trading-neutral (Trigger: bereits da)
| Tool/Anzeige | Aktion | Status |
|---|---|---|
| TA-WFO-Dashboard-Card | 🔁 → Stack-Validierung | ✅ erledigt (d2ef055) |
| Survivorship-TA-WFO-Block | 🗑️ entfernt | ✅ erledigt (520c6af) |
| Stack-Robustheits-Baseline | 🔁 erstellt | ✅ erledigt (stack_wfo_baseline.json) |

### Phase 2 — Trigger: erste Stack-Trades bestätigt (heute US-Open)
| Tool | Aktion |
|---|---|
| `_scan_via_signal_stack` yfinance-Kopplung | 🔧 entkoppeln (Single-Point-Dependency) |
| S&P 600 M–Z | 🔧 Universum vervollständigen + EDGAR-Cache |

### Phase 3 — Trigger: ≥30 Stack-Live-Trades (~Richtung Juli)
| Tool | Aktion |
|---|---|
| WFO-Drift-Watchdog (Task #4) | 🔁 re-baseline auf Stack → dann enable |
| Meta-Labeler | 🔁 reset+retrain auf Stack ODER 🗑️ pensionieren (je nach Mehrwert) |
| **Chassis-Exit/Sizing** (SL −3% / TP 15 / TP-Tranchen / Trailing / Time-Stop 10d / Kelly 0.04) | 🔧 auf das Trade-Profil des neuen Motors re-kalibrieren (fundamental = längerer Horizont). Klarste Mismatches: **Time-Stop 10d** + **SL −3%** (stoppen gute Picks zu früh). 5. Alt-Motor-Fund. |

### Phase 4 — Trigger: Soak zeigt Regime-Ergebnis
| Tool | Aktion |
|---|---|
| Regime Fear-&-Greed-Block (Variante A) | 🗑️ endgültig raus (wenn bestätigt) ODER 🔧 zurück |

### Phase 5 — Trigger: Stack über Monate bewiesen / Cutover-Tag (04.08.)
| Tool | Aktion |
|---|---|
| TA-Optimizer | 🗑️ löschen (oder 🔁 falls Stack-Tuning gewollt) |
| WFO/Backtester (TA) | 🔁 für Stack-Backtest re-purposen, TA-Teile 🗑️ |
| ML-Scoring + ML-Importances-Display | 🗑️ |
| disabled_symbols + Bootstrap-Sync | 🗑️ |
| min_scanner_score + Settings-„WFO-locked"-Label | 🗑️ (TA-Fallback) |
| **TA-Score-Fallback + ASSET_UNIVERSE** (Sicherheitsnetz!) | 🗑️ — NUR mit Monate-Live-Beweis; bis dahin behalten |
| Backtest-Tab + Optimizer-Card-Display | 🗑️/🔁 mit den Tools |
| VIX>35-Hard-Halt | BEHALTEN (echter Tail-Schutz) |

**Prinzip:** Wir gehen Tool für Tool, sobald der jeweilige Trigger erfüllt ist — Cruft 🗑️ entschlossen, Tooling 🔁 umbauen, Sicherheitsnetze erst mit hartem Live-Beweis. Kein Big-Bang, kein Zombie-Ballast. Dieser Fahrplan ist die Single-Source für „was ist als Nächstes dran".

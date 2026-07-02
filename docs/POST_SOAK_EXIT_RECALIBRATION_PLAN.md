# Post-Soak Exit- & Cash-Rekalibrierung — Analyse-Notiz (Vorbereitung)

**Status:** VORBEREITUNG — NICHT vor Soak-Exit (~≥30 geschlossene Trades / vor Cutover-Entscheidung) anwenden.
**Erstellt:** 2026-06-23 (Soak Tag 12, 12/50 Trades) · **Anlass:** α IWM −4.55 % über 2 Wochen.
**Disziplin:** Während des Soaks NICHT eingreifen (sonst verfälschen wir die Mess-Daten). Dies ist das **Playbook** für den Moment, in dem wir handlungsfähig sein wollen.

---

## 1. Symptom (beobachtet)

Seit dem Motor-Switch (09.06.) liegt der Bot **deutlich hinter der korrekten Small-Cap-Benchmark**:

| | 09.→22.06. |
|---|---|
| Bot | **+0.32 %** |
| IWM (Russell 2000) | **+4.87 %** |
| **α IWM** | **−4.55 %** |

Der Bot **verdient** (positiv, Win-Rate ~50 %, Ø +1.27 %/Trade), **nimmt aber die Small-Cap-Rally fast nicht mit**.

## 2. Zwei Hypothesen — mit Daten gestützt

### (A) „Alte Beine" — die noch TA-getunten Exits stoppen fundamentale Picks zu früh
Exit-Grund-Breakdown der 12 Soak-Closes (Stand 23.06.):

| Exit-Grund | n | Ø PnL | Summe | Gewinner |
|---|---|---|---|---|
| **STOP_LOSS** | 4 | **−5.66 %** | −22.64 % | **0/4** |
| TRAILING | 5 | +2.35 % | +11.73 % | 3/5 |
| TAKE_PROFIT/PARTIAL | 3 | **+8.71 %** | +26.13 % | 3/3 |
| **SL + Trailing (Momentum-Exits)** | **9** | **−1.21 %** | — | — |

**Lesart:** Die **fundamentale Gewinnmitnahme (+8.71 %) trägt**, die **Momentum-Exits (SL/Trailing, Ø −1.21 %) ziehen runter**. Alle 4 harten Stop-Losses waren Verlierer bei ~−5.7 % — der Bot wirft bei einem −5 %-Wackler raus, obwohl die fundamentale These über Wochen/Monate läuft.

### (B) Cash-Drag — der Bot ist im steigenden Markt unterinvestiert
- Aktuell **~53 % investiert / ~47 % Cash** (GrossPositionValue 551k / NetLiq 1039k CHF).
- Grobe Zerlegung der −4.55 % Lücke: **~−2.3 %** allein aus Cash-Drag (0.47 × IWM +4.87 %), **~−2.25 %** aus Exit-Timing + Positionen, die nicht mithalten.
- Die Caps sind offen (20/Klasse, 1000 % Allocation) → die Cash-Quote kommt NICHT von Caps, sondern von **Sizing** (Half-Kelly, max_fraction 1 %/Position) + **Buy-Flow** (nur Top-Ranked, limitiert pro Zyklus).

## 3. Diagnose, die NACH ≥30 Trades zu rechnen ist (datengestützt, nicht raten)

> Erst messen, dann drehen. Die folgenden Auswertungen liefern die Begründung.

1. **Counterfactual der Stop-Losses (DIE Kernfrage):** Für jeden SL-Close: ist die Aktie in den N Tagen DANACH wieder über den Einstieg gestiegen?
   - Viele erholten sich → **verfrühter Stop** → SL zu eng → weiten.
   - Sie fielen weiter → **Stop war korrekt** → NICHT weiten (echtes Risk-Management).
   - Quelle: trade_history (Exit-Zeitpunkt + Einstieg) + yfinance-Kursverlauf nach dem Exit.
2. **Trailing-Check:** Schließt der Trailing-SL (Aktivierung 6 % / Abstand 4 %) Gewinner zu früh? Vergleiche realisierten Trailing-Gewinn vs. Kurs-Hoch nach dem Exit.
3. **Cash-Drag-Quantifizierung:** Investitions-Quote über die Zeit (GrossPositionValue/NetLiq aus equity_history) × Benchmark-Return = Cash-Drag-Anteil. Trennt „Exit-Problem" von „Deployment-Problem".
4. **Selektions-Check (Gegenprobe):** Hätte ein simples „kaufen + halten bis Time-Stop" der gleichen Picks die Benchmark geschlagen? Isoliert Auswahl-Güte von Exit-Schaden.

## 4. Kandidaten-Hebel (was rekalibriert würde)

| Hebel | Heute (live) | Richtung (Hypothese) | Begründung |
|---|---|---|---|
| **Stop-Loss-Breite** | ~−5 % (demo) / asset-class ~−3.9 % | **weiter** (z.B. −8…−12 %) | Mittelfristig-fundamental verträgt Wackler; −5 % ist Day-Trade-eng |
| **Trailing-SL Abstand** | Aktiv. 6 % / Abstand 4 % | **lockerer** | gibt Gewinnern Raum, statt bei 4 % Rückgang zu schließen |
| **Time-Stop** | max_days_stale 30 | evtl. **länger** | fundamentale These braucht ggf. Monate, nicht 30 Tage |
| **TP-Tranchen** | 8/16/30 | ggf. **höher/weniger** | funktioniert (+8.71 %), aber Tranchen kappen Rally-Upside |
| **Cash-Quote / Sizing** | ~47 % Cash, max_fraction 1 % | **mehr deployen** | Cash-Drag im steigenden Markt; mehr/größere Positionen |

**Wichtig — Regime-Abhängigkeit:** Weitere Stops + mehr Deployment helfen in der **Rally**, schaden aber im **Crash** (mehr Downside). Jede Änderung muss in BEIDEN Regimes geprüft werden, nicht nur am aktuellen Aufwärts-Sample.

## 5. Validierungs-Methode (PFLICHT vor jeder Live-Änderung)

Validierungs-Hierarchie: **Live > WFO > Optimizer/Single-Run.** Kein Wert wird live gedreht, bevor:
1. **Backtest/WFO auf dem NEUEN Motor** (sp600 + Signal-Stack) mit den neuen Exit-Params gerechnet ist — über mehrere Marktregimes, mit Out-of-Sample-Fenster (kein Overfit auf das Rally-Sample).
2. Bestätigt ist, dass die neuen Exits **risiko-adjustiert** besser sind (Sharpe/PF), nicht nur im Schönwetter.
3. Dies verbindet sich mit dem offenen Task **WFO-Drift-Watchdog RE-BASELINE auf neuen Motor** (der braucht ohnehin eine frische Baseline) → in einem Schritt erledigen.

## 6. Entscheidungs-Gate

- **Wann:** Soak-Exit-Checkpoint (~≥30 Trades / vor Cutover-Entscheidung Anf. Juli–Aug). NICHT früher.
- **Konservative Defaults bei Unklarheit:** im Zweifel die vorsichtigere Variante. Für den mittelfristig-fundamentalen Bot heißt „konservativ" hier oft **weniger Eingriff durch Exits = länger halten** (passt zur Bot-Identität) — aber NICHT blind max-deployen (Cash-Quote ist teils bewusste Risiko-Wahl).
- **Reihenfolge:** erst Diagnose (Abschnitt 3) → dann 1 Hebel isoliert testen (nicht alle gleichzeitig → sonst weiß man nicht, was wirkte) → WFO-validieren → live.

## 7. Risiken & Caveats

- **Kleine Stichprobe:** selbst 30 Trades sind wenig — Exit-Params NICHT auf 30 Trades overfitten.
- **Ein Regime:** die IWM-Rally ist eine Marktphase; die Exits müssen auch im Abschwung schützen.
- **Cash-Quote = teils bewusst:** mehr Deployment = mehr Markt-Exposure = mehr Crash-Downside. Kein blindes Voll-Investieren.
- **Nicht das Selektions-Baby mit dem Exit-Bade ausschütten:** der Motor (Auswahl) ist validiert; hier geht es NUR um Exits + Deployment, nicht um die Signal-Logik.

---

**Kernaussage:** Der Soak zeigt mit Daten, dass die TA-getunten Exits (v.a. der −5 %-Stop-Loss) + die hohe Cash-Quote den fundamentalen Motor in der Rally ausbremsen. Das ist der erwartete „neuer Kopf, alte Beine"-Effekt. Nach ≥30 Trades: Counterfactual rechnen → 1 Hebel isoliert → WFO-validieren → live. Bis dahin: **beobachten, α IWM verfolgen, nicht eingreifen.**

---

# VALIDIERUNG — Schritt A abgeschlossen (02.07.2026, 30er-Gate erreicht)

**Werkzeug:** `app/signal_stack_backtester.py` (neu, motor-korrekt, committet — ersetzt das nicht-reproduzierbare `stack_wfo_baseline`-Einmal-Skript; = zugleich WFO-Re-Baseline / Task #4).

**Diagnose (read-only, 30 Live-Trades):**
- **Counterfactual:** 60 % der Stop-Losses waren VERFRÜHT (Aktie kam über den Einstieg zurück). → SL zu eng.
- **Cash-Drag-Zerlegung:** Ø 54 % investiert → ~2.4 % der −2.45 % α-Lücke ist reiner Cash-Drag; die investierte Quote lag ≈ IWM. → **Cash-Drag ist der PRIMÄRE Treiber der Underperformance, nicht die Exits.**

**Backtest-Validierung (2019–2026, incl. Crash 2020 + Bär 2022, top_n=15, trailing 6/4):**
- **SL-Sweep:** −5 % (aktuell) = zu eng; **~−10 % = risiko-adjustiertes Optimum** (Sharpe 1.27 vs 1.23, Return +331 % vs +240 %); −8 % konservativ/bär-sicher (bester im 2022-Bären); **„kein SL" katastrophal** (−31 % MaxDD). → Stop bleibt Pflicht, aber weiter.
- **Deployment-Sweep:** **Sharpe KONSTANT (1.23) über alle Level** → Cash-Quote ist ein **reiner Risiko-Regler** (mehr Deploy = proportional mehr Return UND Drawdown, gleiche Effizienz). Bot ist effizient, nur konservativ.
- **Motor-Edge bestätigt:** schlägt IWM risiko-adjustiert (Sharpe 1.2+ vs 0.57) über 8 Jahre inkl. Crashs. Starke Cutover-Confidence.
- **Caveat:** Survivorship (sp600 = heutige Member) bläht ABSOLUTE Renditen; Sharpe + relative Rangfolge robust. Lean-MVP.

**Validierter Vorschlag (→ Schritt B, live anwenden):**
1. **SL: −5 % → ~−10 %** (−8 % konservativ). E6-Catastrophic-Stop (−20 %) bleibt Backstop.
2. **Deployment: ~54 % → moderat höher (~70–80 %)** — Risiko-Appetit-Entscheid (gleicher Sharpe, mehr Return + Drawdown). Nicht 100 % (Dry-Powder/Margin-Puffer).
3. **Reihenfolge B:** Deployment zuerst (größter Hebel), dann SL. Live-Anwendung = Soak-Uhr-Reset auf die verbesserte Config (Carlos-Entscheid).

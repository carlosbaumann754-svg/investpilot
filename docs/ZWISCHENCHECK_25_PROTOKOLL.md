# Zwischencheck bei 25 sauberen Round-Trips — vorregistriertes Protokoll (R-B53)

**Festgeschrieben am 13.08.2026 bei Zaehlerstand 19/80 — BEVOR die Daten da sind.**
Das ist Absicht: Wer die Messlatte erst nach dem Blick auf die Zahlen definiert,
kann sie unbewusst dorthin schieben, wo die Zahlen gut aussehen. Dieses Protokoll
gilt unveraendert, egal was bei Trade 20–25 passiert.

## Was gemessen wird (alles aus `clean_roundtrip_stats`, Soak-Start 21.07. 12:00)

| Kennzahl | Referenzrahmen (R-B18/R-B29, 873 Backtest-Trades, PF 1.698) |
|---|---|
| Profit-Faktor | Band bei n=25: p05 = **0.35**, p25 = 0.97, Median = **1.60** |
| Netto-USD | informativ (skalenabhaengig, kein Kriterium) |
| Trefferquote + AvgWin/AvgLoss | Konsistenz mit Trailing-Mechanik (wenige grosse Gewinner zahlen die Stops) |
| Cancel-/Partial-Quote | vs. 29%-Baseline (R-B40) — Ausfuehrungs-, nicht Strategie-Frage |
| Deployment-Grad | Slot-Blockade durch Teilfuellungen (bekanntes Paper-Artefakt) |

## Was bei 25 entschieden werden KANN

1. **WEITER (Default):** PF ≥ 0.35 → alles im Erwartungsband des eigenen
   Backtests. Weiter sammeln Richtung 80. Auch PF < 1.0 ist bei n=25 KEIN
   Alarmsignal — das Band ist so breit, weil 25 Trades Rauschen sind.
2. **UNTERSUCHEN:** PF < 0.35 → schlechter als 95 % der Zufallspfade des
   eigenen Backtests. Dann Ursachen-Diagnose (Ausfuehrung? Regime? Datenfehler?)
   — aber KEIN Parameter-Tuning ins Blaue.
3. **AUSFUEHRUNGS-FIXES:** Nur wenn Cancel-/Partial-Quote massiv vom
   29%-Baseline abweicht → Ausfuehrungsschicht pruefen (z. B. Echtzeit-Abo
   vorziehen). Das aendert nicht die Strategie.

## Was bei 25 explizit NICHT entschieden werden kann

- **Kein Go-Live-Urteil** — egal wie gut die Zahl aussieht. Das 5%-Quantil
  der Referenz ueberschreitet 1.0 erst bei n=80 (p05 = 1.03). Ein PF von z. B.
  2.0 bei n=25 ist erfreulich und beweist nichts.
- **Kein Exit-/Parameter-Tuning** aufgrund der 25er-Zahlen. Die Exit-Familie
  ist als nicht walk-forward-tunebar belegt (R-B26/27, 54 % = Muenzwurf).
  Config bleibt eingefroren.
- **Kein Soak-Reset** — die Uhr laeuft durch bis 80, ausser ein UNTERSUCHEN-
  Befund deckt einen echten Mess- oder Ausfuehrungsfehler auf (dann gilt die
  Regel aus R-B15: sauberer Schnitt, ehrlich dokumentiert).

## Ablauf am Tag X

Der Host-Cron (`scripts/zwischencheck_trigger.py`, taeglich 22:15 CH) meldet
das Erreichen einmalig per Pushover. Beim naechsten Claude-Check: dieses
Protokoll oeffnen, Tabelle fuellen, Einordnung in Laiensprache, Ergebnis in
Roadmap-CHANGELOG + Session-Recap. Erwarteter Zeitbedarf: < 1 Stunde.
Danach gilt wieder: laufen lassen, Stille heisst gut.

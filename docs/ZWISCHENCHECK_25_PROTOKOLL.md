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

---

# ERGEBNIS — gefahren am 27.08.2026 (Wecker feuerte 26.08. 22:15)

## Protokoll-Tabelle (Ist vs. vorregistrierte Messlatte)

| Kennzahl | Ist bei n=25 | Messlatte | Urteil |
|---|---|---|---|
| Profit-Faktor (USD) | **0.86** | WEITER solange >= 0.35 (p05) | **WEITER** |
| Profit-Faktor (%-Basis, backtest-vergleichbar) | **0.99** | Median-Referenz 1.60 | unter Median, im Band |
| Netto | -4'372 USD | informativ | — |
| Trefferquote | 60.0% | Konsistenz-Check | Breakeven braeuchte 63.6% |
| AvgWin / AvgLoss | +1'794 / -3'128 | Trailing-Mechanik | Asymmetrie bestaetigt |
| Cancel-Quote | 35.4% | Baseline 29% | erhoeht, nicht massiv |
| Partial-Quote | 41.5% | Paper-Artefakt | dokumentiert |
| Deployment | ~28-29% | Backtest nimmt 60% an (k=4) | **halbiert — Paper-Datenqualitaet** |
| Geerbt (Kontrolle) | n=12, +15'336 | getrennt | sauber getrennt |

**Formales Urteil nach vorregistrierter Regel: WEITER.** Kein Untersuchen-
Trigger (0.86 >> 0.35), kein Go-Live-Urteil moeglich (by design), kein
Exit-Tuning, kein Soak-Reset.

## Zusatzanalyse 1: Rechter Rand (Gewinner-Verteilung)

>= +6%: 4 von 25 | >= +8%: 2 | >= +12%: 2 (DGII +20.7%, EZPW +14.2%).
Der zweite Tail-Gewinner kam am 26.08. — die These "Tail-Gewinner treten
live gar nicht auf" ist damit ABGESCHWAECHT: sie treten auf, Frequenz
(~1 pro 12 Trades) entscheidet. Bei 80 messbar.

## Zusatzanalyse 2: Stop-vs-Signalhorizont (NEU, wichtigster Befund)

Alle 10 Stop-Ausstiege, Kurs am Stop-Tag vs. letzter Archiv-Kurs (PIT-Daten):
**6 von 10 notieren HEUTE ueber dem Ausstiegskurs, Durchschnitt +4.1% seit
Stop.** Extremfaelle: EZPW nach Stop +25.7% (der Bot kaufte spaeter erneut
und nahm +14.2% mit), JBGS +10.2%, INSP +8.0% (auch re-gekauft, +4.8%).
Das stuetzt die Hypothese, dass der -8%-Stop bei 30%+-Vol-Small-Caps
regelmaessig RAUSCHEN erntet, bevor der 1-Monats-Signalhorizont wirken kann.
GEGENGEWICHT (nicht unterschlagen): Fenster war ein steigender Small-Cap-
Markt (Stops sehen in Rallyes immer schlecht aus); n=10; der Juli-Walk-
Forward fand Exits nicht tunebar. KONSEQUENZ: KEINE Aenderung jetzt
(vorregistriertes Verbot) — die Analyse laeuft formal bis 80 mit und wird
dort zur Hauptfrage: "Konzept tot vs. Konzept falsch verdrahtet".

## Ausfuehrungs-Befund

Cancel 35% + Partial 42% + Deployment ~29% (Backtest-Annahme fuer k=4: 60%)
— die Paper-Datenqualitaet halbiert den Kapitaleinsatz. Kein Strategie-,
ein Infrastruktur-Problem. Fix bleibt das Echtzeit-Abo (Pre-Cutover-Item).

## Abbruch-Kriterium — BINDEND (Carlos-Freigabe 27.08.2026, "ja setze beides um")

Analog zur 25er-Vorregistrierung, BEVOR weitere Daten da sind — seit
27.08.2026 verbindlicher Teil des Go/No-Go-Protokolls bei 80:
**STOPP des Konzepts bei 80, wenn (a) PF(%-Basis) < 1.0 UND (b) die
Stop-Rebound-Quote weiter >= 50% UND (c) < 5 Tail-Gewinner (>= +8%) in 80.**
Treffen nur (b)+(c) zu, aber PF >= 1.0: kein Stopp, sondern EIN definierter
Umbau-Versuch (Exit-Geometrie an Signalhorizont anpassen) mit neuem,
letztem 80er-Soak. Zaehler-Stand bei Registrierung: 25.


## Futility-Check bei 50 — BINDEND (Carlos-Freigabe 27.08.2026, R-B58)

Registriert bei Zaehlerstand 25, BEVOR weitere Daten da sind. EIN einziger
zusaetzlicher Blick (kein Optional Stopping): Erreicht der Zaehler 50 und
liegt der PF (%-Basis) unter **0.48** — der p01-Glueckspfad-Untergrenze der
eigenen Referenz bei n=50 —, ist der Bot schlechter als 99 von 100
Zufallspfaden der nachweislich profitablen Backtest-Strategie. Dann lautet
die bindende Empfehlung: **Soak stoppen** (Carlos-Entscheid beim naechsten
Check). Liegt PF% >= 0.48: weiter bis 80, keine Aktion. Der Meilenstein-
Wecker (scripts/zwischencheck_trigger.py) meldet den Check automatisch und
rechnet das Urteil in die Pushover-Meldung.

## Korrektur der Futility-Grenze (R-B64, Carlos-Freigabe 28.08.2026)

0.48 stammte aus der Close-basierten Referenz, die der validierte
OHLC-Simulator als +4pp/Trade optimistisch entlarvt hat (R-B63/64).
Neue bindende Grenze bei n=50: **PF% < 0.41** (p05 der live-validierten
Referenz). Gleiche Logik, ehrliche Latte. Wecker-Skript angepasst.

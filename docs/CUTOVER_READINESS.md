# Cutover-Readiness — Go/No-Go-Kriterien + Status

**Zweck:** Die Entscheidung „Bot auf echtes Geld umstellen (Cutover)" an messbaren,
motor-passenden Kriterien aufhängen — statt an der irreführenden Alt-Metrik „50
geschlossene Trades". Referenz für die Cutover-Entscheidung (Ziel-Datum bisher 04.08.2026).

**Erstellt:** 2026-07-16 (rekalibrierte Config seit 02.07.2026 live, 22 Closes / 13 Round-Trips).
**Reproduktion:** Analyse-Skript-Logik in `scripts/` bzw. `docs/` (trade_history.json +
equity_history.json read-only; Kennzahlen unten neu berechnen für ein Update).

---

## Kernbefund (ehrlich)

Die **Selektion funktioniert** (α positiv, Risiko zahm), aber wir haben **noch NICHT
bewiesen, dass der Bot Round-Trips profitabel abschließt.** Der Kontogewinn ist fast
komplett **unrealisiert** (Papier). „2 Wochen die Benchmark auf dem Papier schlagen" ist
KEIN Cutover-Nachweis. → **04.08. ist zu früh; realistisch +~4–6 Wochen.**

## Die Zahlen (Stand 16.07.2026, seit Soak-Reset 02.07.)

**Gut:**
- **α vs IWM: +2.30 Ppkt** über 10 Handelstage (Bot **+1.69 %**, IWM **−0.61 %**). Echte
  Selektions-Edge (Bot hoch, während Small-Caps leicht runter).
- **Max-Drawdown nur −1.11 %** — sehr ruhiges Risikoprofil.
- **Breit gestreut:** Top-3-Gewinner = 25 % des Brutto-Gewinns (kein Ein-Namen-Glück).
- **Exit-Mechanik richtig:** Take-Profit/Partial Ø +8.81 % (n=10), Trailing Ø +4.20 %
  (n=8), Stop-Loss Ø −8.78 % (n=4, 0 Gewinner — schneidet Verlierer korrekt).

**Der ehrliche Haken (was die 82 % Win-Rate versteckt):**
- **Win-Rate 82 % ist aufgebläht:** von „22 Trades" sind nur **13 echte Voll-Round-Trips**;
  9 sind Teil-Verkäufe (Tranchen) auf NOCH OFFENEN Gewinnern — Teil-Closes sind per
  Konstruktion immer Gewinner.
- **Realisierte Round-Trips netto NEGATIV: −$8.324.** Die +$8.282 aus Teil-Closes gleichen
  es aus → realisiert ≈ null. Der +1.69 % NetLiq-Gewinn ist **unrealisiert** (Papier).
- **Der −8 %-Stop kostet sichtbar:** 4 Stop-Losses à ~−$5.700 (= −$23k) fraßen den GESAMTEN
  Gewinner-Topf. Große Verlierer, kleine Gewinner (in $; Payoff-Ratio 0.77). Im Backtest
  netto positiv — im 2-Wochen-Live-Sample neutral. Braucht mehr Daten.
- Leichter Churn: AOSL/CRSR/FIZZ je 2× geschlossen.

## Warum „50 geschlossene Trades" ein schlechtes Gate ist
1. **Aufgebläht durch Teil-Closes** — „50" sind keine 50 Round-Trips.
2. **Unterscheidet nicht realisiert vs. unrealisiert** — misst nicht, ob wir *profitabel
   abschließen*.
3. **Kein Risiko-/α-Bezug** — reine Zahl, keine Aussage über Edge oder Drawdown.
4. **Passt nicht zum Halte-Motor** — der soll HALTEN (weite Stops, lange Time-Stops) →
   schließt wenige Trades; 50 könnten Monate dauern.

## Revidierte Go/No-Go-Kriterien

| # | Kriterium | Ziel | Stand 16.07. |
|---|---|---|---|
| 1 | Echte **Voll-Round-Trips** (keine Tranchen) | ≥ 20–25 | 13 |
| 2 | **Realisierte Round-Trips netto positiv** | **> 0** | **−$8.3k** ← Knackpunkt |
| 3 | **α vs IWM positiv über ≥ 4–6 Wochen** | ✔ + Dauer | +2.30 Ppkt / 2 Wo |
| 4 | Max-Drawdown | < 8–10 % | −1.11 % ✔ |
| 5 | Keine offenen (echten) Sentry-Fehler | ✔ | ✔ |
| 6 | Deployment im Zielband | ~60–75 % | ~57 % (regime-bedingt) |

**Kriterium 2 (realisierte Profitabilität) ist der eigentliche Gatekeeper** — solange die
abgeschlossenen Round-Trips netto negativ sind, ist der Edge nicht bewiesen, nur „auf dem
Papier".

## Timeline / Empfehlung
- **04.08. zu früh.** Realistisch ~4–6 Wochen mehr (→ ~Ende Aug / Anf. Sep), bis Kriterium 2
  ins Plus dreht und die α-Dauer (Kriterium 3) steht.
- **Bis dahin:** beobachten, nicht tunen. Bei jeder Status-Anfrage Kriterien 1–4 neu rechnen.
- **NICHT** wegen Ungeduld abkürzen — genau das (zu früh live) war der Fehler beim edgeless
  Alt-Motor.

## Caveats
- 13 Round-Trips sind ein kleines Sample; die −$8.3k können 1–2 große Stops sein, die sich
  mit mehr Trades ausmitteln (oder auch nicht — deshalb messen).
- α +2.30 Ppkt über 2 Wochen ist ermutigend, aber kurz; ein Markt-Regime (die IWM-Schwäche)
  ist keine Vollprobe.
- Survivorship + Lean-MVP-Caveats des Backtests bleiben (siehe POST_SOAK_EXIT_RECALIBRATION_PLAN.md).

---

# NACHTRAG 20.07.2026 — Gatekeeper-Methodik festgenagelt

## Anlass: zwei Zahlen, dieselben Daten
Beim Status-Check am 20.07. ergab eine schnelle Nachrechnung **+14'791 USD** — scheinbar
„Gate erfüllt". Gegen die **−8'300 USD** vom 16.07. Beide aus `trade_history.json`, beide
„realisierte Round-Trips". Der Unterschied war **nicht** die Marktentwicklung, sondern eine
**unterschiedliche Abgrenzung**. Genau der Fehler, gegen den dieses Dokument schützen soll.

Konsequenz: Die Regel ist ab jetzt **als Code fixiert** in
[`scripts/gatekeeper_roundtrips.py`](../scripts/gatekeeper_roundtrips.py) und wird nicht mehr
ad-hoc nachgerechnet.

## Die Leitmetrik (V3, „sauber")
> Gezählt wird eine Position **nur, wenn der Bot sie im Soak-Fenster sowohl eröffnet als
> auch geschlossen hat.**

Begründung: Nur dann ist das Ergebnis eine Entscheidung des **neuen** Fundamental-Motors.
Vor dem Soak gekaufte Positionen („geerbt") tragen Gewinne, die unter **alten** Parametern
entstanden sind — sie dem neuen Motor gutzuschreiben ist Cherry-Picking. Exakt das hätte das
Gate am 20.07. fälschlich auf grün gedreht (die zwei größten Gewinner der Woche, AGO +8'863
und EPRT +4'814, waren geerbte Positionen).

Verworfene Abgrenzungen:
| Variante | Wert 20.07. | Warum nicht |
|---|---|---|
| V1 alles im Fenster geschlossen | **+11'394** | mischt geerbte Alt-Motor-Gewinne rein |
| V2 nur Episoden ohne Teilverkäufe | −27'571 | **verzerrt**: Teilverkäufe passieren fast nur bei Gewinnern → filtert Gewinner raus |
| V4 nur geerbte Positionen | +18'928 | misst Altlast-Abbau, nicht den Motor |

## Ergebnis Stand 20.07.2026
| Kennzahl | Wert | Bewertung |
|---|---|---|
| Round-Trips (sauber) | **9** | Sample sehr klein |
| **Netto realisiert** | **−7'534 USD** | **Gate NICHT erfüllt** |
| Trefferquote | 66.7 % (6/9) | gut |
| Ø Gewinn | +783 | |
| Ø Verlust | −4'077 | **5.2× größer als Ø Gewinn** |
| Profit-Faktor | **0.38** | Ziel > 1.0, WFO-Baseline 1.71 |

## Die eigentliche Erkenntnis: Payoff-Asymmetrie
Der Bot **trifft häufig richtig (67 %), verdient aber trotzdem nicht** — weil die Verlierer
5× so groß sind wie die Gewinner. Zwei Stops (AOSL −5'570, DIOD −6'393) fressen sechs
Gewinne auf. Muster: der Trailing-SL schneidet Gewinner früh ab (+91 bis +1'918), während
Verlierer bis −8 % (real bis ~−13 % nach Slippage/Gap) laufen dürfen.

Das ist ein **Exit-Design-Problem, kein Signal-Problem** — der Einstieg (Fundamental-Ranking)
liefert 67 % Trefferquote, die Exit-Regeln zerstören die Erwartung. Kandidat für die
Post-Soak-Rekalibrierung (Trailing später aktivieren / weiter stellen, damit Gewinner laufen).

## Offene Diskrepanz: Watchdog sagt „gesund"
Der WFO-Drift-Watchdog meldet täglich `live_pf 11.27 vs wfo_pf 1.27 → +83.5 % gesund`,
während die Round-Trip-Ökonomie PF **0.38** zeigt. Der Watchdog misst offenbar auf anderer
Basis (Tages-Equity statt Round-Trips) und wird vom geerbten Buch dominiert.
**→ zu prüfen: ob der Watchdog das Richtige misst.** Solange das offen ist, ist sein „grün"
kein Freibrief.

## Revidierte Bewertung
- **Kriterium 2 bleibt der Gatekeeper und bleibt ROT** (−7'534, PF 0.38).
- **04.08. bestätigt zu früh.** Bei n=9 ist statistisch ohnehin nichts entschieden — weder
  positiv noch negativ. Es braucht schlicht mehr abgeschlossene Round-Trips.
- **Im Soak weiter nicht tunen.** Die Payoff-Asymmetrie ist notiert, aber ein Exit-Umbau
  mitten im Soak würde die Messung entwerten (dann misst man wieder etwas Neues).
- **Nebenbefund Deployment:** 15/15 Positionen (am Cap), aber nur ~36 % investiert
  (CHF 386k von 1'081k) — die WFO-Validierung lief mit 70 %. Der Motor läuft live auf
  halber Auslastung; bindend ist der Positions-Cap, nicht der Signal-Nachschub.

# Motor-Kandidaten-Test 1-3 — vorregistrierte Kriterien (R-B61, 28.08.2026 00:50)

**Festgeschrieben BEVOR die erste Zahl gerechnet ist.** Frage (Carlos): Was
bringen die Kandidaten — und waeren sie ggf. besser geeignet als der
aktuelle Motor?

## Vergleichsmassstab ("besser geeignet")
Ein Kandidat gilt nur dann als ernsthafter Nachfolge-/Ersatz-Kandidat, wenn:
(a) Monats-Sharpe UND Profit-Faktor der Monatsrenditen >= aktueller Motor
    (Basis R-B59: hold-Modus 5-Signal, PF-Monatsbasis wird mitgerechnet),
(b) 2024+-Scheibe nicht schlechter als der aktuelle Motor,
(c) MaxDD <= 8% (Kelly-Hard-Gate) OHNE nachtraegliche Hebel-Anpassung,
(d) schlaegt seine eigene passive Benchmark (Motor 1: SPY Buy&Hold;
    Motor 2: IJR Buy&Hold) — sonst ist er nur teures Beta.
Erfuellt ein Kandidat (a)-(d) nicht, bleibt er Kandidaten-Bank-Eintrag mit
Zahlen — KEIN Motor-Wechsel-Vorschlag. Der laufende Soak bleibt in jedem
Fall unberuehrt (reine Rechnung).

## Motor 1 — ETF-Trend-Rotation (Dual-Momentum-Klasse)
Universum: SPY QQQ IJR EFA EEM GLD TLT IEF (+SHY als Cash-Proxy).
Regel (fix, kein Fitting): monatlich 12-1-Momentum der Risiko-ETFs;
Top-2 gleichgewichtet WENN deren 12M-Rendite > SHY-12M (Absolut-Filter),
sonst Anteil in IEF. Fenster 2005-2026 (so tief wie Daten reichen).
Varianten NUR: Top-1/Top-2/Top-3 und 6-1 vs 12-1 Momentum (2x3=6 Zellen,
alle werden berichtet — kein Rosinenpicken).

## Motor 2 — Langsamer Fundamental-Motor (gleiche 5 Signale, Quartals+/Halbjahres-Horizont)
Exakt die R-B59-Pipeline (identische Picks, Kosten, hold-Modus), Horizonte
H=126 und H=252 Handelstage, je: pur / mit Kat-Stop -20. Sizing-Ehrlichkeit:
MaxDD wird OHNE Kelly-Reskalierung berichtet; verletzt er die 8%, gilt (c)
als nicht erfuellt (die gestrige V3/V5-Lektion).

## Motor 3 — Earnings-Drift Small Caps (PEAD)
VORBEDINGUNG Datenpruefung: historische Earnings-Termine fuer >=200 der 306
Symbole ueber >=5 Jahre aus Gratis-Quellen. Surprise-Proxy (mangels
Schaetzungen): Ankuendigungs-Reaktion t-1..t+1; Drift-Messung t+2..t+42.
Reicht die Datenbasis nicht: EHRLICHER ABBRUCH mit Befund, kein Wackel-Test.

## Basisraten-Ehrlichkeit
Vier Tests diese Woche, bisher 3x NEIN. Erwartung fuer Motor 1: eher
"solide Beta-Plus-Maschine" als "Alpha-Wunder" — auch das waere ein
wertvolles Ergebnis (Beta-Modus-Motorisierung fuer den Widerlegungs-Fall).

---

# Nachtrag R-B64 (28.08.2026): OHLC-Simulator — vorregistrierte Bestehens-Kriterien

Der Tagesschluss-Blindfleck ist belegt (R-B63: Bias +4.07pp/Trade, nur 14/25
gleiche Exit-Gruende). Der OHLC-Simulator (Stops/Trailing auf Tages-Hoch/Tief,
Gap-Behandlung ueber Open) gilt als VALIDIERT, wenn er die 25 Live-Paare
reproduziert mit: (1) |Bias| <= 1.5pp, (2) mittlere |Abweichung| <= 3.0pp,
(3) gleicher Exit-Grund >= 18/25. Erst NACH bestandener Probe zaehlt sein
M0-vs-M2d-Vergleich; zusaetzlich liefert er die korrigierte Referenz-
verteilung fuer den 50er-Futility-Check (Pflicht vor Ende September).
Konventionen (fix): Entry am Tagesschluss; Gap-Open unter Stop -> Fill zum
Open; Trailing-Trigger prueft gegen den Peak bis GESTERN (kein Selbst-Peak
am selben Tag); Peak aus Tages-Hochs.

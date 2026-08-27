# M2A-MOTOR — Regelwerk, Uebergang, Live-Gates (R-B65, Entwurf 28.08.2026)

Status: FINAL zur Carlos-Freigabe (Zahlen aus validiertem Z3-Modell eingesetzt) (Schnitt geplant Mo 31.08. vor
Marktoeffnung). Nach Freigabe ist dieses Dokument BINDEND — gleiche
Disziplin wie Zwischencheck-/Abbruch-Regeln.

## 1. Motor-Definition (was sich aendert — und was nicht)

UNVERAENDERT: Signal-Stack (5 Signale, EDGAR, S&P-600), Top-15-Auswahl,
Eligibility/Coverage-Regeln, min_score 25, 15 Slots.

NEU:
- HALTEDAUER: fix 126 Handelstage (~6 Monate) je Position, Verkauf zum
  Tagesschluss von Tag 126. KEIN Stop-Loss, KEIN Trailing, KEINE Tranchen.
- VERSICHERUNG: E6-Broker-Katastrophen-Stop bleibt, auf -40% (statt -20).
  Feintest-Entscheid (vorregistrierte Akzeptanz): -35% (Z2) DURCHGEFALLEN
  (kostet 0.08 Sharpe + 1.35pp CAGR), -40% (Z3) BESTANDEN (-0.02 Sharpe,
  -0.87pp CAGR = ~gratis). Sieger-Konfig = Z3: PF 3.39, CAGR 15.4%,
  MaxDD -19.2%, SharpeM 0.95.
- KAUF-FENSTER: Neukaeufe nur in den ersten 3 Handelstagen des Monats
  (Modell-Treue; entschaerft nebenbei das Teilfuellungs-Problem).
- SIZING: deployment 0.60 (= Carlos-Risiko-Budget -20%; historisch
  gemessener Tages-MaxDD -19.9%, real koennen es -25/-30% werden —
  PRE-COMMIT: kein manueller Eingriff oberhalb des Budgets; Ausstieg
  NUR ueber die Gates in Abschnitt 3).

## 2. Uebergang am Schnitt-Tag (Mo 31.08., vor 15:30)

- Die 15 offenen M0-Positionen werden als GEERBT markiert und laufen
  unter den ALTEN Exits (SL-8/Trailing) aus — kein Verkaufsschock, keine
  Vermischung. Sie zaehlen fuer KEINE M2a-Messung.
- Neukaeufe ab Schnitt nach M2a-Regeln; Anlauf-Staffelung gemaess
  Feintest Z4 (max. 5 Neukaeufe/Monat), bis das Depot einmal komplett
  aus M2a-Positionen besteht (~3 Monate Anlauf).
- Die M0-Soak-Akte (25 saubere Round-Trips, PF% 0.99) wird geschlossen
  und archiviert — sie bleibt die Messung des alten Motors.

## 3. Live-Gates (vorregistriert, KALENDER-basiert)

Grundproblem: ~20 Round-Trips/Jahr — ein Zaehler-Gate wie beim M0-Soak
waere absurd. Stattdessen traegt das LIVE-VALIDIERTE Modell (R-B64,
25/25-Paarprobe) die Beweislast, und live wird laufend geprueft, ob die
Realitaet dem Modell folgt:

G1 — MONATLICHE BAND-PRUEFUNG (automatischer Wecker, 1. Handelstag):
    Live-Monatsrendite (USD, MTM) wird gegen die Modell-Baender gelegt:
    p01 = -8.3% | p05 = -5.4% | p25 = -1.2% | Median = +1.2% | Mittel = +1.3%/Mt
    Einzelmonate unter p05 sind ERWARTBAR (5% der Faelle) — kein Alarm,
    nur Protokoll.

G2 — PAAR-PROBE JE HORIZONT-EXIT (fortlaufend):
    Jede geschlossene M2a-Position wird gegen ihren Modell-Zwilling
    gerechnet (gleicher Einstieg, OHLC-Sim). Ab 10 Paaren gilt:
    |mittlerer Bias| > 3pp -> AUSFUEHRUNGS-ALARM (Prio 1), Ursache
    klaeren bevor weiter zugekauft wird.

G3+G4 — 6-MONATS-LEITER (bindend, EIN Blick ~Ende Feb 2027), Basis:
    84 gerollte 6-Monats-Fenster des validierten Z3-Modells
    (p01 = -10.7% | p05 = -8.0% | p25 = +1.2% | Median = +5.6%):
    - Live 6-Mt < -10.7% (p01)  -> FUTILITY: STOPP-Empfehlung.
    - -10.7..-8.0% (p01..p05)   -> WARNSTUFE: verlaengern auf Monat 9,
      Zukauf-Stopp bis geklaert.
    - -8.0..+1.2% (p05..p25)    -> UNENTSCHIEDEN: verlaengern auf
      Monat 9, dann 12 — danach erzwungener Entscheid.
    - >= +1.2% (p25) UND Paar-Proben-Bias < 3pp UND keine Regelwerk-
      Verletzung -> GO-EMPFEHLUNG Real-Cutover.

G5 — MODELL-STURZ (jederzeit):
    Faellt eine kuenftige Validierung des Simulators (>= 10 neue Paare)
    durch die R-B64-Kriterien, verlieren ALLE Gates ihre Grundlage ->
    sofortige Grundsatz-Neubewertung.

## 4. Umsetzungs-Spezifikation (Code, Wochenende 29./30.08.)

1. Config + Locks: exit_mode='horizont', horizon_handelstage=126;
   stop_loss/trailing deaktiviert via manual_lock_overrides-NEUFASSUNG
   (bewusster, dokumentierter Locks-Wechsel); E6 cat_stop_pct -> 40 (Z3-Sieger).
2. trader.py: unbedingter Zeit-Exit bei Positions-Alter >= 126
   Handelstage (neuer Exit-Zweig, VOR allen anderen Pruefungen; Tests:
   Tag 125 haelt, Tag 126 verkauft, geerbte Positionen ausgenommen);
   Kauf-Fenster-Gate (nur Handelstag 1-3 des Monats; Test).
3. Geerbt-Markierung: Bestands-Positionen beim Schnitt in
   data/m2a_geerbt.json einfrieren; Exit-Loop behandelt geerbte nach
   Alt-Regeln (Test).
4. Gates-Wecker: scripts/m2a_gate_check.py (Host-Cron, 1. Handelstag
   06:00 UTC): G1-Protokoll + G3/G4-Termine + Pushover; Paar-Probe G2
   als woechentlicher Lauf. AUDIT_METADATA + Bauplan-Whitelist.
5. Anzeige: Soak-Karte -> 'M2a-Gates'-Karte (G1-G4-Status, Baender,
   geerbt-Restbestand); Tooltips nachziehen (Display-Regel: Altes raus).
6. Referenz-Artefakte: data/m2a_erwartungsbaender.json (aus Feintest),
   M0-Referenzen bleiben als Archiv liegen.
7. Rollback-Pfad: ein dokumentierter Config-Schnitt zurueck auf
   M0-Locks (Datei-Kopie liegt bei) — Notausstieg ohne Code-Deploy.

## 5. Der Paradigmen-Wechsel (explizit, damit klar ist, was freigegeben wird)

Der M0-Soak folgte dem Paradigma "LIVE beweist den Edge" (80 Round-Trips,
p05>1). Ein 6-Monats-Motor kann so nicht bewiesen werden (20 Trades/Jahr).
M2a folgt daher dem Paradigma: "Das LIVE-VALIDIERTE MODELL traegt den
Beweis (25/25-Paarprobe + 7.5 Jahre Historie), Live prueft laufend die
KONFORMITAET" (G1/G2) und liefert nach 6 Monaten ein Leiter-Urteil (G3/G4).
Wer dem Modell nicht traut, darf diesem Wechsel nicht zustimmen — das ist
die eigentliche Entscheidung hinter diesem Dokument.

## 6. Ehrlichkeits-Anhang

- Beweisbasis M2a: 195 Sim-Trades 2019-2026, davon ~40 seit 2024; das
  Modell ist live-validiert, aber an M0-Exits — Horizont-Exits sind
  simulationsfreundlicher (weniger Annahmen), bleiben aber bis zur
  ersten eigenen Paar-Probe eine Extrapolation.
- Der Motor erbt die offene Kernfrage des Projekts (lebt der
  Fundamental-Edge seit 2024?) — er beantwortet sie nur mit besserem
  Hebel: 2024+ PF 1.44 vs 0.96. Faellt die Antwort negativ aus, greifen
  G3/G4 und danach der Beta-Pfad (Motor 1, validiert).

# Insider-Signal-Test — vorregistrierte Kriterien (R-B60, 27.08.2026)

**Festgeschrieben BEVOR die erste Zahl gerechnet ist** (Carlos: "alles
umsetzen, damit ich heute noch entscheiden kann"). Gleiche Disziplin wie
Exit-Sprint R-B59 und Zwischencheck R-B53.

## Hypothese
Insider-CLUSTER-Kaeufe (mehrere unterschiedliche Insider kaufen open-market
binnen 60 Tagen, wertgewichtet) prognostizieren in unserem S&P-600-Universum
positive Folgerenditen und verbessern den 5-Signal-Stack als 6. Signal.

## Daten
SEC EDGAR Insider-Transactions-Datasets (Form 3/4/5, quartalsweise, gratis),
2018Q3-2026Q3, gefiltert auf unser Universum via CIK-Map. Informationsdatum
= FILING-Datum (nicht Transaktionsdatum) — Look-Ahead-Schutz. Nur
transactionCode P (Open-Market-Kauf); Verkaeufe bewusst NICHT als
Negativ-Signal (Insider verkaufen aus tausend Gruenden, kaufen aus einem).

## Vorregistrierte Aufnahme-Kriterien (ALLE muessen erfuellt sein)
(a) EIGENSTAENDIGKEIT: Monatlicher Quer-schnitts-IC (Spearman, Signal vs.
    Folgemonatsrendite) > 0 mit t >= 2.0 ueber das volle Fenster.
(b) STACK-VERBESSERUNG: 6-Signal-Variante schlaegt den 5er-Stack im
    hold-Modus-Backtest (identische Picks-Pipeline, Kosten, Exits) in
    PF GESAMT **UND** in der 2024+-Scheibe.
(c) RISIKO: MaxDD nicht schlechter als der 5er-Stack (+1 Prozentpunkt Toleranz).
Getestete Integrationsformen: (i) 6. Rang im Komposit-Mittel, (ii) Tilt
(Bonus auf 5er-Score), (iii) Veto-only. Erfuellt KEINE Form alle Kriterien:
Signal wird NICHT eingebaut; die Live-Sammlung laeuft trotzdem weiter
(Datenschatz fuer spaeter).

## Basisraten-Ehrlichkeit
Rank-Band-Test (Juli): NEIN. Exit-Geometrie-Test (R-B59): NEIN. Die Methode
lehnt meistens ab — genau dafuer ist sie da. Ein NEIN heisst: 0 CHF verloren,
eine Illusion weniger.

## Entscheidungs-Sequenz
Test-Ergebnis -> Carlos. Einbau = Motor-Aenderung = Uhr-Reset (expliziter
Carlos-Entscheid) ODER parken als belegter Kandidat fuer die 80.

---

# ERGEBNIS — 27.08.2026, ~00:15 (gerechnet in derselben Nacht)

Datenbasis: 6'116 Open-Market-Kaeufe / 292 Symbole, SEC Form-345-Datasets
2018Q3-2026Q2, FILING-Datum, ISO-normalisiert (data/insider_events_pit.json).

| Kriterium | Messwert | Schwelle | Urteil |
|---|---|---|---|
| (a) Eigenstaendigkeit | Excess +0.003%/Mt, t=0.01 (89 Monate); Spearman-IC -0.005 (t=-0.7) | t >= 2.0 | **NICHT ERFUELLT** |
| (b) Stack-Verbesserung | Komposit PF 1.63 / 2024+ 1.42; Tilt 1.60 / 1.42; Promotion 1.62 / 1.35 — Basis: 1.675 / 1.448 | beide besser | **NICHT ERFUELLT** (alle drei schlechter) |
| (c) Risiko | MaxDD: Komposit -9.9%, Tilt -8.4%, Promotion -6.6% vs Basis -6.0% | nicht schlechter (+1pp) | Komposit/Tilt verletzt |

**URTEIL NACH VORREGISTRIERTEN KRITERIEN: NEIN — das Insider-Signal wird
NICHT eingebaut.** Insider-Cluster-Kaeufe prognostizieren in unserem
Universum/Zeitraum schlicht nichts (t=0.01 ist Punktlandung auf Null), und
jede Beimischung verduennt den funktionierenden 5er-Stack.

BEWUSST KEIN Parameter-Fishing (andere Fenster/Schwellen nachschieben, bis
etwas signifikant aussieht) — genau das verbietet die Vorregistrierung.
Das PIT-Archiv bleibt bestehen (Quartals-Cron pflegt es) fuer kuenftige,
NEU vorregistrierte Hypothesen. Dritter Methoden-Nein in Folge
(Rank-Band, Exit-Geometrie, Insider): 0 CHF verloren, drei Illusionen weniger.

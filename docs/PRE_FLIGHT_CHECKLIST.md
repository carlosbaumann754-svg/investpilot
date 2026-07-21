# Pre-Flight-Checkliste: vor jeder backtest-gestützten Config-Änderung

**Zweck:** Vier der fünf Punkte stammen aus echten Fehlern der Woche vom 20.–21.07.2026.
Jeder davon hat entweder einen Live-Rückbau ausgelöst oder ein Frühwarnsystem
unbrauchbar gemacht. Sie kosten zusammen etwa 30 Minuten — der billigste Rückbau
dieser Woche kostete einen Abend.

Diese Liste ist **abzuarbeiten, nicht zu überfliegen**. Wer sie überspringt, sollte
im Commit begründen, warum.

---

## 1. Kennt das Modell alle Mechanismen, die der Bot hat?

**Prüfen:** Jeden Exit-/Entry-Mechanismus der Live-Config im Backtester-Code suchen.

```bash
grep -c "tp_tranches" app/signal_stack_backtester.py     # 0 = wird NICHT modelliert
```

**Woher der Punkt kommt (20.07.):** Der Backtester kannte `tp_tranches` nicht —
0 Vorkommen im ganzen Modul. Live feuerten **10 von 10** Teilverkäufen bei +8 %.
Der Exit-Sweep bewertete damit eine Konfiguration, die es so nicht gab, und die
Live-Exits wurden systematisch **zu mild** dargestellt.

---

## 2. Hängt der Modell-Vorteil an Mechanismen, die der Bot NICHT hat?

**Prüfen:** Exit-Gründe auszählen und den Rendite-Beitrag je Grund aufaddieren.
Anteil eines Mechanismus am Gesamtgewinn > ~30 % → nachsehen, ob es ihn live gibt.

```python
agg = collections.defaultdict(float)
for t in res["trades"]:
    agg[t["reason"]] += t["ret_net"]
```

**Woher der Punkt kommt (20.07.):** Die empfohlene Variante holte **62 % der
Ausstiege und ~86 % des Gewinns** aus dem monatlichen Rebalancing des
Backtesters. Der Live-Bot rotiert nicht aus dem Ranking heraus — den Mechanismus
gibt es schlicht nicht. Die Änderung ging live und musste am selben Abend
zurückgebaut werden.

**Faustregel:** Die Abhängigkeit steigt mit jeder Lockerung der Exits
(gemessen: 11 % → 34 % → 70 % → 100 %). Je lockerer der Vorschlag, desto
gründlicher prüfen.

---

## 3. Fenster-Stabilität statt Mittelwert

**Prüfen:** Kennzahl pro OOS-Fenster ausgeben, nicht nur gepoolt. Kandidat nur,
wenn er in **fast allen** Fenstern hält und kein Fenster unprofitabel wird.

**Woher der Punkt kommt:** Ein Mittelwert aus drei guten und vier schlechten
Jahren sieht identisch aus wie ein durchgehend solides Ergebnis. Bei der
TP-Entscheidung (21.07.) war genau das der Ausschlag: besser in **6 von 7**
Fenstern, min-PF gleichauf → tragfähig.

---

## 4. Ist irgendein Schwellwert GERATEN statt gemessen?

**Prüfen:** Jede Zahl im Vorschlag durchgehen. Für jede fragen: *Woher kommt sie?*
Wenn die Antwort „schien vernünftig" lautet — **messen**.

**Woher der Punkt kommt (20.07.):** Der Min-Sample-Guard des Motor-Edge-Signals
wurde auf `min_n = 12` geschätzt. Nachgemessen: Bei n=12 löst ein **kerngesundes**
System in **41.5 %** der Fälle Fehlalarm aus. Das Signal, das vor Cry-Wolf
schützen sollte, war selbst der Cry-Wolf.

**Methode:** Empirische Verteilung aus dem Backtest ziehen
(`scripts/build_roundtrip_reference.py` als Vorlage), Quantil ablesen, fertig.

---

## 4b. Lockert der Vorschlag die Ausstiege? Dann greift er den Erntemechanismus an

**Prüfen:** Verlängert die Änderung die Haltedauer? Wenn ja: Wie verhält sich die
neue erwartete Haltedauer zum **Vorteils-Fenster** von rund einem Monat?

```bash
python scripts/haltedauer_analyse.py     # Zerfallskurve: lebt der Vorteil noch?
python scripts/rotation_vs_halten.py     # aktuelle Haltedauer im Backtest
```

**Woher der Punkt kommt (21.07.2026, R-B26/R-B27):** Die Zerfallskurve zeigt, dass
der Vorsprung der ausgewählten Namen **genau einen Monat** lebt (+1,078 %, t=3,46)
und ab Monat 2 verschwindet (−0,068 % / +0,360 % / +0,253 % … alle unauffällig).
Gleichzeitig liegt die tatsächliche Haltedauer bei **15 Handelstagen im Median**,
61,7 % der Positionen schließen innerhalb von 21 Tagen.

Daraus folgt die Umdeutung, die diesen Punkt nötig macht:

> **Die engen Ausstiege sind nicht Risikomanagement — sie sind der
> Erntemechanismus.** Sie holen den Vorsprung ab, bevor er verfällt.

Jeder Vorschlag im Geist von „Gewinner laufen lassen" verlängert die Haltedauer
in die Monate 2 bis 6 hinein — nachweislich der Bereich ohne Vorteil. Er kostet
also nicht nur Risiko, er greift die Quelle der Rendite an.

Genau das war die Änderung vom 20.07. (Trailing 10/12, Take-Profit aus, Tranchen
aus). Sie wurde am selben Abend zurückgebaut, damals wegen eines Modellfehlers.
Der tiefere Grund war dieser hier — er war nur noch nicht bekannt.

**Faustregel:** Ausstiege lockern = beweispflichtig. Nicht „warum nicht", sondern
„welcher Beleg zeigt, dass der Vorteil länger lebt als bisher gemessen?"

---

## 5. Drawdown, nicht nur Rendite

**Prüfen:** Max-Drawdown je Fenster — und **täglich zum Marktwert**, nicht aus
Monatsrenditen.

**Woher der Punkt kommt (21.07.):** Im Hold-Modus entstehen Monatsrenditen erst
beim **Ausstieg**. Eine Position, die 30 % einbricht und sich erholt, ist dort
unsichtbar → der Drawdown wird systematisch zu klein ausgewiesen, ausgerechnet
bei der Frage, wo es aufs Risiko ankommt.

---

## Zusätzlich: nach der Änderung

- **Neustart-Test.** Hält die Config einen `docker restart` aus? Bootstrap und
  WFO-Locks können still zurückdrehen.
  *(Vorfall: `trailing_sl_state` ratcht nur nach oben — eine Trail-Weitung
  erreichte zwei offene Positionen gar nicht. Config-Änderung ≠
  Verhaltensänderung.)*
- **Referenztabellen neu erzeugen.** Nach jeder Motor-/Exit-Änderung
  `scripts/build_roundtrip_reference.py` laufen lassen, sonst blockt der
  Staleness-Guard den Alarm.
- **Falsifizierbare Erwartung notieren.** Was muss in den nächsten Tagen
  sichtbar sein, damit die Änderung gewirkt hat? Tritt es nicht ein, war sie
  wirkungslos — und das gehört gesagt.

---

---

# Teil 2: Vor jedem Aufruf (nicht nur vor Config-Änderungen)

Die fünf Punkte oben schützen vor **falschen Empfehlungen**. Dieser Teil schützt vor
**versehentlichen Aktionen** — er entstand aus einem Vorfall am 21.07.2026, bei dem
beinahe das gesamte Depot liquidiert worden wäre.

## 6. Endpunkte und Funktionen nie blind aufrufen

**Regel:** Vor dem Aufruf lesen, was die Funktion *tut*. Kein Massenaufruf über eine
API, die auch schreibende Operationen enthält — auch nicht „nur zum Auslesen".

**Der Vorfall (21.07.2026):** Für einen Dashboard-Audit wurden alle `api_*`-Funktionen
programmatisch durchgerufen, um ihre Rückgabewerte zu vergleichen. Darunter war der
**Kill-Switch**. Er feuerte:

```
!!! EMERGENCY CLOSE ALL: Dashboard Kill Switch !!!
  [1/3] Trading-Flag gesetzt -> false
  [2/3] Risk-Pause 24h gesetzt
  [3/3] Alle 3 Fetch-Versuche lieferten keine Positionen
```

**Dass nichts verkauft wurde, war Glück, nicht Können:** Der Portfolio-Abruf scheiterte
dreimal, weil der laufende Bot die IBKR-Verbindung belegte (`client_id 1 already in
use`). Ohne diese Kollision wären 15 Positionen / 1.08 Mio liquidiert worden.

Ein `api_`-Präfix sagt nichts darüber, ob eine Funktion liest oder schreibt.

## 7. Ein Umweg um eine Schutzmaßnahme braucht MEHR Vorsicht, nicht weniger

**Regel:** Wenn der vorgesehene Weg blockiert ist, ist das eine Grenze — kein Hindernis.
Die richtige Antwort lautet „ich komme ohne dich nicht weiter", nicht ein Workaround.

**Warum das der eigentliche Fehler war:** Der Auslöser war ein fehlender Dashboard-Login.
Statt das zu akzeptieren, wurde ein programmatischer Umweg gebaut — und **genau hinter
diesem Login liegt der Not-Aus**. Die Authentifizierung war nicht im Weg, sie *war* der
Schutz. Der Umweg hat die menschliche Freigabe entfernt, die sie darstellt.

Verstärkend kam eine falsche Verallgemeinerung dazu: Vorher wurden den ganzen Vormittag
gelesene Funktionen direkt aufgerufen (`check_wfo_drift`, `clean_roundtrip_stats` …).
Daraus wurde fälschlich „Funktionen direkt aufrufen ist sicher" — obwohl der entscheidende
Unterschied war, dass die vorherigen **gelesen** worden waren.

## 8. Read-only heißt: nachweislich read-only

Bei Diagnose auf einem Live-System nur Aufrufe verwenden, deren Seiteneffektfreiheit
belegt ist — Datei lesen, Zustand ausgeben, reine Berechnungsfunktion. Im Zweifel den
Zustand aus den State-Dateien lesen statt über die Anwendungs-API.

---

## Das Muster hinter allen fünf Punkten

> **Handeln, bevor geprüft wurde.**

Beide Rückbauten dieser Woche folgten demselben Ablauf: Empfehlung ausgesprochen,
Prüfung nachgeholt, nachdem der Nutzer nachhakte oder der Schaden sichtbar war.
Die Prüfungen selbst dauerten jeweils unter 15 Minuten.

**Zwei von vier Kehrtwenden waren echter Erkenntnisgewinn** (man kann nicht
wissen, dass das Modell einen Mechanismus vermisst, den man noch nicht kennt).
**Zwei waren vermeidbar.** Die Punkte 1–5 adressieren die zweite Hälfte.

Der Kill-Switch-Vorfall (Punkte 6–8) ist dasselbe Muster auf der Aktions-Ebene:
aufgerufen, ohne zu lesen, was der Aufruf tut. Mit einem Unterschied — hier war
der potenzielle Schaden nicht ein verlorener Abend, sondern **das Depot**.

## Die eine Frage, die alles abdeckt

> **Was passiert, wenn ich mich irre?**

Bei einer Empfehlung: ein Rückbau, ein paar Tage Messung. Bei einem Aufruf auf
einem Live-System: möglicherweise irreversibel. Der Aufwand für die Prüfung muss
sich am *Schadenspotenzial* orientieren, nicht daran, wie sicher man sich fühlt.

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

## Das Muster hinter allen fünf Punkten

> **Liefern, bevor geprüft wurde.**

Beide Rückbauten dieser Woche folgten demselben Ablauf: Empfehlung ausgesprochen,
Prüfung nachgeholt, nachdem der Nutzer nachhakte oder der Schaden sichtbar war.
Die Prüfungen selbst dauerten jeweils unter 15 Minuten.

**Zwei von vier Kehrtwenden waren echter Erkenntnisgewinn** (man kann nicht
wissen, dass das Modell einen Mechanismus vermisst, den man noch nicht kennt).
**Zwei waren vermeidbar.** Diese Liste adressiert die zweite Hälfte.

# Signal-Research-Plan — echten Edge finden

**Erstellt:** 07.06.2026 (nach WFO-Deep-Dive + Signal-Diagnose)
**Status:** Phase 0 offen — neue Phase nach Abschluss der Diagnose

---

## Ausgangslage (was wir SICHER wissen)
- **Aktuelles Signal (TA-Composite + ML auf TA-Features): IC ≈ 0.**
  Definitiv gemessen über 1400 Backtest-Trades: Spearman-IC **−0.015**, Verdikt
  „KEIN Edge". Top-Quartil-Trades 0.80 % vs Bottom 0.55 % — Rauschen.
- **Warum:** Alle Features sind preis-abgeleitete, öffentliche, längst
  arbitrierte Indikatoren (RSI, MACD, Momentum 5/20d, SMA, Bollinger, ATR, OBV).
  „Garbage in" → das ML-Modell kann daraus auch keinen Edge ziehen
  (Precision 33-38 % < Basis-Rate 42 %).
- **Was steht:** Robustes System (Execution, Risk, Safety, Daten-Pipeline) +
  ehrliche IC-Metrik (R-B13, Spearman) als Massstab für JEDES neue Signal.

## Prinzipien (die Lektion vom 07.06.)
1. **Jedes Signal wird per Spearman-IC OOS gemessen** — nie per in-sample-Backtest.
2. **Kill-Kriterium:** Spearman-IC < 0.03 → Signal verwerfen, weiter. KEIN Tuning
   toter Signale (genau die Falle, die uns 3 Monate gekostet hat).
3. **Kleine, falsifizierbare Hypothesen** — eine Frage, eine Messung.
4. **Realismus:** Die meisten Ideen scheitern. Echtes Alpha ist selten. Erwartung
   niedrig halten; nicht in eine neue Illusion verlieben.

---

## Phase 0 — Forschungs-Harness (1 Session)
**Ziel:** schnell jedes Feature/Signal als IC messen können.
- [ ] Backtest erweitern: pro Trade die EINZEL-Features mitschreiben (rsi,
      momentum_5/20d, volatility, atr_pct, obv_slope, bollinger_pos, ...).
- [ ] `_compute_feature_ics(trades)` — Spearman-IC pro Feature vs pnl_net_pct.
- [ ] Re-Run → Tabelle „Feature → IC". Antwort: hat IRGENDEIN vorhandenes Feature
      Signal, oder sind alle tot (→ neue Daten nötig)?

## Phase 1 — Kandidaten mit echtem Vorhersage-Potenzial (je 1 IC-Messung)
Priorisiert nach Erwartungswert / Aufwand:

1. **Insider-Signale (Finnhub)** — der Bot sammelt sie BEREITS (Shadow-Mode,
   insider_shadow_log.jsonl). Insider-KÄUFE haben dokumentierten Vorhersagewert.
   Billigster Check. → IC testen.
2. **Lang-Horizont Cross-Sectional Momentum (3-12 Monate)** — der EINE Faktor mit
   persistenter historischer Prämie. Aktuell nutzt der Bot nur 5/20d (= Rauschen).
   Rang-basiert über das Universe. Nur Preisdaten nötig. → IC testen.
3. **Earnings-Surprise / PEAD** (Post-Earnings-Announcement-Drift) — dokumentierte
   Anomalie. Bot hat Earnings-Daten. → IC testen.
4. **Value/Quality-Fundamentals** (P/E, ROE, Margin-Trends) — langsame, modeste
   Prämien. Braucht Fundamentaldaten (Alpha Vantage / Polygon). → IC testen.

## Phase 2 — Falls ein Kandidat Spearman-IC > 0.05 zeigt
- [ ] Sauberes Modell auf dem prädiktiven Feature bauen (nicht überfitten).
- [ ] Walk-Forward OOS validieren (ehrliche Metriken, R-B13-Gate).
- [ ] Kelly-Position-Sizing erst NACH bestätigtem, STABILEM OOS-Edge.
- [ ] Erst dann Real-Money-Diskussion (WFO-Hard-Gate muss grün sein).

## Phase 3 — Falls nichts funktioniert (ehrliche Möglichkeit)
- Eingeständnis: ein profitabler systematischer Edge ist mit den verfügbaren
  Daten evtl. nicht erreichbar.
- Projekt-Ziel neu definieren: Lern-/Infrastruktur-Projekt? Paper-only?
  Anderer Markt / anderer Ansatz?

---

## Realismus-Hinweis (wichtig)
Profitable Signale zu finden ist das SCHWERSTE in der quantitativen Finanz —
Profis mit Millionen-Budgets und Alternativdaten scheitern regelmässig daran.
Die Erwartung sollte niedrig sein. ABER: Du misst jetzt EHRLICH und betrügst
dich nicht mehr selbst. Das allein ist mehr Disziplin als 90 % der Retail-Algo-
Trader haben — und die einzige Basis, auf der echte Forschung überhaupt möglich ist.

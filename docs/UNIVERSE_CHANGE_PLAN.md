# Universums-Wechsel — Plan & Spec

**Erstellt:** 07.06.2026 (nach Signal-Diagnose: TA/ML/Momentum/Insider alle null bzw. kosten-negativ im jetzigen Universum)
**Entscheidung:** Universum wechseln — weg von 51 ETFs/Mega-Caps, hin zu **US Small-Cap (S&P SmallCap 600)**.
**Status:** Phase A (Validierung) offen.

---

## Warum (die Diagnose-Lektion)
Vier ehrliche Tests im jetzigen Universum (51 ETFs + Mega-Caps):
| Signal | Ergebnis |
|---|---|
| TA-Composite | tot (Spearman-IC −0.015 über 1400 Trades) |
| ML-Scorer | unter Basis-Rate |
| Momentum (20d) | real (~0.05 IC), aber von Kosten gefressen (netto ≈ 0) |
| Insider | kein Edge (IC −0.017) — **falsches Universum** |

**Strukturelle Erkenntnis:** ETFs + Mega-Caps sind ein hocheffizientes, durcharbitriertes Feld — genau dort, wo Stock-Selection-Signale am *schwächsten* sind. Insider/Momentum/Fundamentals-Prämien existieren v.a. bei **Small-/Mid-Caps**.

## Warum S&P 600 (nicht Russell 2000, nicht Europa)
- **Setup-Fit:** US-Aktien = beste Abdeckung aller drei genutzten Datenquellen (yfinance-Preise, Finnhub-Insider, AV/Polygon-Fundamentals). Europäische Small-Caps = lückenhafte Daten + dünne Insider-Daten.
- **Ausführung:** IBKR handelt US-Small-Caps sauber (USD, kein FX, SMART-Routing).
- **Handelbarkeit:** S&P 600 hat eingebaute Qualitäts-/Liquiditätshürde (positive Gewinne + Mindestvolumen) → der *handelbare* Teil der Small-Caps, kein Microcap-Schrott (Russell 2000).
- **Skalierung:** 600 Titel = 12× heute (mit gestaffeltem/gecachtem Fetchen machbar). Russell 2000 = 40× → Rate-Limits + untradbarer Tail.

---

## Phase A — Validierungs-Probe (jetzt, billig, niedriges Commitment)
**Ziel:** Beweisen ODER widerlegen, dass Momentum/Insider im neuen Universum einen Netto-Edge nach realistischen Small-Cap-Kosten haben — BEVOR irgendetwas live umgebaut wird.

1. **Unverzerrte S&P-600-Liste** beschaffen (öffentlich, WebSearch/Wikipedia).
2. **Preisdaten** via yfinance laden (`download_history(symbols=...)`, ~2-5 J).
3. **Momentum-IC** (rank rsi+momentum_20d, 20d-Forward, cross-sectional) neu messen.
4. **Insider-IC** (Finnhub net P/S-Flow, trailing 30d, 20d-Forward) neu messen.
5. **Realistisches Small-Cap-Kostenmodell** anwenden (breitere Spreads als Mega-Caps!).
6. **Entscheidungs-Gate:**
   - Netto-Edge > Small-Cap-Kostenhürde **und** OOS-konsistent → **Phase B**.
   - Sonst → ehrlich abgehakt, kein Live-Umbau auf Sand.

**Survivorship-Caveat:** Schnelle Probe nutzt *heutige* S&P-600-Mitglieder → leichter Survivorship-Rest (delistete Verlierer fehlen). Weit unverzerrter als Hand-Auswahl, reicht für Richtungs-Signal. **Vor echtem Geld:** auf Point-in-Time-Konstituenten eskalieren (historisch korrekte Liste).

## Phase B — Live-Universums-Migration (NUR falls A besteht)
Volle Spec-/Plan-Disziplin. Scanner + Daten-Pipeline auf 600 Titel skalieren (Batching, Rate-Limit-Handling, Caching), realistisches Kostenmodell in den Live-Sizer, Liquiditäts-Filter, schrittweiser Shadow→Live-Rollout.

---

---

## Phase A — Zwischenergebnisse (07.06.2026)

**Datenbasis:** 309 S&P-600-Small-Caps (A–M, alphabetisch = rendite-neutrale Stichprobe), 5J via yfinance (Split/Div-adjustiert), OOS ~1 Jahr ab 2025-08. Realistisches Small-Cap-Kostenmodell (Round-Trip 1.0/1.5/2.0 % Szenarien).

**Methodik-Fund (wichtig):** `download_history` fetcht NUR ASSET_UNIVERSE-Symbole → für Fremd-Ticker direkt `yf.download` nötig (Gate umgangen).

### Momentum-Probe — Ergebnis: kein robuster Edge
| Faktor | bias-freier IC (OOS) | Dezil-Long-Short | Urteil |
|---|---|---|---|
| **12-1M Momentum** (echter Faktor) | +0.011 (VOLL −0.013) | −0.24 % (4/10 pos) | **null** |
| **mom_20d** (1M) | **−0.04** (Reversal-Zone) | **+1.7 %** (Sharpe 2.2, 21/37) | **nicht-monoton/fragil** |
| RSI | −0.04 | — | Reversal/overbought |

- **mom_20d ist nicht-monoton:** Dezil-Spread positiv (Schwanz-Effekt: extreme Gewinner laufen weiter), aber IC negativ (breite Mitte dreht). Ein Signal, das sich nicht konsistent charakterisieren lässt → fragil, period-spezifisch.
- **Kosten-Killer:** mom_20d = 20-Tage-Rebalancing ≈ 100 % Turnover → ~1.5 % Kosten/Periode frisst den dünnen Long-only-Alpha (~+0.5 % netto). Long-Short unmöglich (Small-Cap-Shorting = Borrow-Kosten + hard-to-borrow).
- **Methodik-Limit:** 1-Jahr-OOS (~10 unabhängige Perioden) ist zu dünn; Methoden widersprechen sich → kein belastbares Momentum-Urteil aus einem einzelnen Split. Definitiver Test bräuchte echte Multi-Fold-Walk-Forward über die volle Historie.

### Insider-Probe (S&P 600) — ERSTES POSITIVES SIGNAL
285 Symbole mit Insider-Aktivität, **23.189 aktive (Symbol,Datum)-Paare** (vs. nur 3.077 bei Mega-Caps — viel reicher).

| Maß | Mega-Caps (altes Universum) | **S&P 600 Small-Caps** |
|---|---|---|
| IC(Insider-NetFlow vs 20d-fwd) | −0.017 (tot) | **+0.0413 (positiv, bias-frei, n=23k)** |
| Netto-KAUF avg / median | +2.44 / +1.81 % | **+2.52 % / +1.75 %** (n=3.831) |
| Netto-VERKAUF avg / median | +4.44 / +1.29 % | +1.34 % / +0.55 % (n=19.358) |

- **Vorzeichen, Mittel UND Median stimmen überein** (Kauf schlägt Verkauf um +1.2 pp/20T) — anders als bei Mega-Caps, wo der Mittelwert das falsche Vorzeichen hatte (ausreißer-getrieben).
- **Alpha vs. Beta:** Netto-KAUF +2.52 % vs. Universums-Schnitt ~+1.43 % → **~+1.1 pp Alpha**; Netto-VERKAUF ~= Universum. Das ist ein echter cross-sektionaler Tilt, kein reines Beta.
- **Netto nach Kosten (~1.5 % RT):** Median ~+0.25 %, Mittel ~+1.0 %/20T — dünn aber positiv.
- **Einordnung:** IC +0.041 liegt ÜBER dem Kill-Kriterium (0.03), KNAPP UNTER der Phase-2-Schwelle (0.05). = **schwach-aber-echt**. Erstes Signal der ganzen Untersuchung mit echtem, konsistentem, bias-freiem Edge — und genau das on-thesis-Signal, das den Universums-Wechsel motiviert hat.

**Caveats (nicht überverkaufen):** schwach (0.041), ~2-Jahre-Fenster = ein Regime (Small-Cap-Rally), Netto-Edge dünn. = **echter Lead, keine fertige Strategie.**

### Phase-A-Gesamturteil
Der Universums-Wechsel war **richtig**: Er hat ein reales Brutto-Signal (Insider) sichtbar gemacht, das im Mega-Cap-Universum unsichtbar/tot war (−0.017 → +0.041). Momentum bleibt auch hier null/fragil.

---

## Phase 2 — Insider-Validierung (07.06.2026): NICHT bestanden (als Standalone-Edge)

Multi-Horizont-Netto-Alpha (realistische 1.5 % RT-Kosten, Turnover) + zeitliche Konsistenz:

| Horizont | IC | Brutto-Alpha | Netto-Alpha | n | verwertbar |
|---|---|---|---|---|---|
| 20T | +0.041 | +0.34 % | **−1.16 %** (Sh −1.50) | 12 | ✅ → verliert netto |
| 60T | +0.012 | −3.81 % | −5.31 % | 3 | ⚠️ zu wenig |
| 120T | +0.099 | +12.8 % | +11.3 % | **1** | ❌ Einzel-Obs, bedeutungslos |

**Zeitliche Konsistenz (IC/Monat, HOLD=60):** +0.13, −0.04, −0.09, +0.03, +0.12, +0.07, +0.05, +0.04, −0.11, −0.08 → **kippt monatlich, jüngste 2 Monate negativ.**

**Drei Killer:** (1) am einzigen verwertbaren Horizont (20T) netto −1.16 %/Periode — Turnover frisst den Alpha; (2) IC zeitlich instabil (kein stabiler Edge); (3) Langhorizont (wo Insider zählen würde) nicht validierbar — nur 1 J Finnhub-Daten (120T = n=1).

### VERDIKT
**Insider = reales, aber schwaches + instabiles Signal. KEIN handelbarer Standalone-Edge.** Brutto-IC echt (kein Bias), aber zu schwach für Kosten, zeitlich instabil, am Langhorizont datenarm. **Medallion-Einordnung:** eine reale *Zutat* von vielen — nur kombiniert (unkorreliert gestapelt) + mit mehr Daten handelbar. Stand: **0 validierte Standalone-Edges** (Fortschritt: 0 → 1 reale Zutat).

### Offene Optionen (Entscheidung Carlos)
1. **Insider verfeinern:** Cluster-Käufe (≥2 Insider) + Mindest-Kaufgröße — dokumentiert stärker/stabiler. Schnelltest, könnte Verdikt drehen.
2. **Geduld/Datensammlung:** Insider-Shadow-Mode im Bot AKTIVIEREN → 12+ Mo saubere Point-in-Time-Daten sammeln → Langhorizont später korrekt validieren.
3. **Nächste Zutat suchen:** Fundamentals (E6) / PEAD — weitere schwach-aber-echte Signale fürs Stacking.

### Offene, methodisch-korrekte Eskalation (falls ein Signal Hinweis zeigt)
Statt 80/20-Single-Split → **Walk-Forward über volle 5J** (mehrere OOS-Folds) + **turnover-basiertes Kostenmodell**. Bar: Signal muss über Folds KONSISTENT UND netto-positiv sein.

---

---

## DATEN-DURCHBRUCH: SEC EDGAR löst die Daten-Armut (07.06.2026, gratis)

**Ausgangsproblem:** Insider (Finnhub ~1J) + Fundamentals (yfinance ~5 Quartale) zu flach für langsame Signale → Validierung unmöglich. Vorschnelle Idee „Sharadar ~$50/Mo kaufen" — von Carlos korrekt hinterfragt (Roadmap lehnt Subscription-Tools ab, `ELITE_REJECTED_TOOLS`).

**Auflösung — Tools ≠ Daten:** Die abgelehnten Tools (Unusual Whales, TrendSpider, Quiver, Danelfin…) sind alle **Signal-/Scanner-Black-Boxes** (vorgekaute, edgelose TA/Sentiment-Scores — heute bewiesen). RAW-DATEN sind eine andere Kategorie. Und die Roadmap hatte die Gratis-Antwort schon: **`E5b` SEC-EDGAR-Scraper** (Quiver-Ablehnung Z.1561: „GRATIS und Goldstandard, Daten-Asset ohne Vendor-Lock-in"). Der Fehler war: wir nutzten die *flache Convenience-Quelle* (yfinance/Finnhub) statt der *tiefen Gratis-Quelle* (EDGAR).

**EDGAR-Feasibility bestätigt (XBRL company-facts API, `data.sec.gov`, kein API-Key):**
| | yfinance | **SEC EDGAR** |
|---|---|---|
| Tiefe | ~5 Quartale | **15–17 Jahre** |
| Point-in-Time | nein | **ja — echtes `filed`-Datum pro Wert** (kein Look-Ahead) |
| Kosten | gratis | **gratis** |

NetIncome/Equity/Assets/GrossProfit jeweils 100–334 Datenpunkte über 15+ J, mit Filing-Datum + Periode + Form. CIK-Mapping (10.400 Ticker) via `company_tickers.json`.

**Status:** E5b war NIE gebaut (nur geplant) → EDGAR-Research-Pipeline jetzt gebaut (CIK-Map + XBRL company-facts Fetch+Cache, 309/309 Symbole).
**Caveat:** S&P-600-Liste = heutige Mitglieder → über 15J wächst Survivorship-Bias (delistete fehlen). Erste Probe mit diesem Caveat; sauberer Fix = Point-in-Time-Index-Membership (später).

### Fundamental-Faktor-Test (EDGAR, point-in-time, 15J, ~56 Quartals-Perioden)
| Faktor | 6-Mo IC | 12-Mo IC | % Per. + | Top-vs-Univ |
|---|---|---|---|---|
| **Gross-Profitability (GP/Assets)** | **+0.028** | **+0.036** | **61–65 %** | **+1.67 % / +0.65 %** |
| ROE | +0.009 | +0.004 | ~50 % | neg |
| ROA | +0.004 | −0.001 | ~50 % | neg |

**Gross-Profitability = ZWEITE reale Zutat.** IC pro Jahr positiv in ~10/15 J (durch COVID + 2022-Bär). **Kein Survivorship-Artefakt:** gpa positiv WÄHREND roe/roa negativ im selben Datensatz → echter Cross-Sektions-Effekt (Novy-Marx-Prämie). Robuster als Insider auf den Dimensionen die zählen: zeitlich stabil (vs Insider kippt monatlich) + niedriger Turnover → übersteht Kosten. **Unkorreliert zu Insider** → ideal fürs Stacking (E9).
**Caveat:** schwach (+0.03, survivorship-inflationiert → wahr ~0.02), KEIN Standalone-Edge — eine Zutat.

### STAND nach 07.06.: 0 → 2 reale Zutaten
1. **Insider** (Small-Cap, IC +0.04, instabil + high-turnover) — Zutat.
2. **Gross-Profitability** (15J, IC +0.03, stabil + low-cost) — Zutat.
EDGAR entsperrt die Zutaten-Jagd: viele weitere Faktoren (Value/FCF-Yield, Accruals, Margin-Trends, Earnings-Growth) jetzt sauber über 15J testbar.

### Faktor-Batterie (EDGAR, 12-Mo, 15J) + Korrekturen
| Faktor | mean-IC | Urteil |
|---|---|---|
| Book-to-Market (Value) | +0.047–0.059 | ✅ stark (zykl. echt: tot 2019, Boom 2020-21) |
| Gross-Profitability (Quality) | +0.034 | ✅ |
| FCF-Yield / Earnings-Yield | +0.033 / +0.033 | ✅ (Value-Cluster) |
| Net-Margin / Asset-Growth / Accruals | ~0 / schwach / falsch | ✗ (Accruals seit ~2003 wegarbitriert) |

**Methodik-Lektion (2 korrigierte Fehlhypothesen):** (1) Erst-Stacking nutzte Value-KOMPOSIT (B/M+E/P+FCF) + 4-Faktor-Sample-Restriktion → verwässerte B/M auf +0.0135 → voreiliges „kein Edge". (2) Vermutete Financials-Ursache — WIDERLEGT (B/M in Nicht-Financials +0.055, Financials nur etwas stärker +0.072). Echte Ursache: Komposit/Sample-Restriktion, nicht Sektor.

### ★ SAUBERES STACKING (B/M + GP, 15J) — Stacking-These VALIDIERT
| | mean-IC | IC-IR | Top-Dezil vs Univ |
|---|---|---|---|
| Value (B/M) | +0.047 | +0.30 | +12.5 % |
| Quality (GP) | +0.034 | +0.25 | +0.6 % |
| **Kombiniert** | **+0.064** | **+0.56** | +4.5 % |

**Kombi-IC > beide Einzel** (echt unkorreliert) + **IC-IR ~verdoppelt** (0.30 → 0.56) = der Medallion-Diversifikations-Hebel sichtbar. Quality polstert Values schlechte Jahre (2019: Value −0.22 isoliert → kombiniert −0.05). **Erster echter Edge-KANDIDAT** (Kombi-IC > 0.05, positives Top-Dezil, hohe Konsistenz).
**Caveats:** Survivorship (wahr ~0.04-0.05), IC-IR überlappungs-inflationiert (Jahres-IR ~0.5), 1 Universum, noch keine Kosten-/Portfolio-Sim → Kandidat, kein bewiesener Edge.

### ★ NETTO-TEST (Portfolio-Sim, quartalsw., 15J, gemessener Turnover × 1.5%) — KANDIDAT NICHT BESTANDEN
| Portfolio | Netto-CAGR | Netto-Sharpe | Netto-Alpha vs Bench |
|---|---|---|---|
| Top 10% | +16.7% | 0.76 | +0.2 %/J |
| Top 20% | +17.3% | 0.89 | +0.8 %/J |
| Top 33% | +15.4% | 0.89 | −1.0 %/J |
| **Benchmark (Equal-Weight)** | **+16.5%** | **0.99** | — |

**VERDIKT: positiver IC (+0.064) ≠ handelbarer Edge.** Der V+Q-Score schlägt das Equal-Weight-Universum NICHT (Netto-Alpha ~0, Sharpe sogar < Benchmark). Kosten klein (Fundamentals low-turnover, Brutto-Netto ~1%/J), aber selbst brutto dünn. Survivorship-robust (Benchmark + Strategie gleich inflationiert → ~0 Alpha bleibt).

**Strukturelle Erkenntnis:** Long-only-Faktor-Tilts gegen einen starken Benchmark haben eine niedrige Alpha-Decke. Medallions Hebel = Long-Short + hoher Turnover + marktneutral + Hunderte Signale — ein strukturell anderes Spiel.

### STAND nach 07.06.: weiterhin 0 handelbare Standalone-Edges
Gefunden: reale schwache IC-Signale (Insider, Value, Quality), aber KEINES übersteht den Long-Only-Netto-Test vs Benchmark. **Der echte Gewinn = die EDGAR-Maschine + die Methode** (jeder Faktor bis Netto-Sharpe testbar, IC-Illusionen entlarvt).

## EXHAUSTIVE BILANZ (08.06.2026): klassisches Faktor-Programm erschöpft — kein handelbarer Edge
Getestet (point-in-time, netto, long UND short):
| Signal | IC | Long-only netto | Long-Short netto |
|---|---|---|---|
| TA / ML / Momentum | ~0 | — | — |
| Insider | +0.04 | kosten-negativ | — |
| Value (B/M) | +0.05 | ~0 vs Bench | — |
| Quality (GP) | +0.03 | ~0 vs Bench | — |
| V+Q kombiniert | +0.064 | ~0 vs Bench | ~0/neg |
| PEAD (8-K-Datum) | +0.02 | zu schwach | — |

**PEAD:** Meldungs-Datum-Fix (8-K Item 2.02) verbesserte IC 0.016→0.025, aber bleibt unter Schwelle (decayed seit 1990ern).
**Long-Short V+Q:** jährliches Rebalancing brutto Sharpe 0.27, ABER Short-Seite = junky hard-to-borrow Small-Caps (Borrow 5–15%) → netto ~0/negativ. Long-Short rettet es nicht.

**VERDIKT: In diesem Universum mit Gratis-Daten produziert keine klassische Anomalie einen handelbaren Edge** — long oder short, alle netto ~0. Signale real-aber-schwach (IC 0.02–0.06), übersetzt sich nicht in realisierte Netto-Rendite.

### KORREKTUR (Kosten/Rebalancing-Re-Test): V+Q = Enhanced-Return-Tilt, kein Alpha
Das frühere „V+Q netto ~0" war teils ein **Rebalancing-Artefakt** (quartalsweise = langsames 12-Mo-Signal totgetradet). **Jährlich** rebalanciert: V+Q Top-20% = **+2.4–2.9 %/J mehr Rendite** als Equal-Weight-Benchmark, robust über alle Kosten (Turnover nur 35 %/J → Kosten fast egal). **ABER Sharpe NICHT besser (0.73 vs 0.75)** — die Mehrrendite ist reine **Risiko-Kompensation** (Konzentration ~25 vs ~120 Namen, Vol ~26 % vs ~22 %), kein risiko-adjustierter Edge. **Honest:** legitimer „Enhanced-Return-Tilt" (sinnvolle Basis für neu-definiertes Ziel 1b), aber NICHT „Markt schlagen". Net-Share-Issuance ebenfalls getestet → null (IC −0.017, falsche Dezil-Richtung; war wieder ein Cross-Sektions-Faktor).

### Lektion: Cross-Sektions-Faktor-KLASSE ist erschöpft → nächste Richtung muss die Klasse verlassen
Event-getriebene Special-Situations (nicht „ranke alle", sondern „handle um ein Ereignis") + Crypto sind die genuin anderen, ungetesteten Richtungen.

### Strategische Optionen (frisch entscheiden — das klassische Programm ist erschöpft)
1. **Akzeptieren / Ziel neu definieren** — Bot als Infra-/Lern-Werk oder passiver Läufer; systematisches Alpha mit diesem Setup nicht erreichbar.
2. **Nische-Edges** (`ELITE_NICHE_EDGES` in Roadmap) — kapazitäts-beschränkte „zu klein für Funds"-Ineffizienzen, wo Retail strukturell besser kann. Die EINE ungetestete, fundamental andere Richtung.
3. (Erschöpft: klassische Faktoren long-only/long-short, mehr Universum würde dasselbe zeigen.)

---

## Disziplin-Prinzipien (vom 07.06.)
1. Jedes Signal per Spearman-IC OOS gemessen — nie in-sample.
2. Kill-Kriterium IC < 0.03 → verwerfen.
3. Realistische Kosten zuerst — Brutto-Alpha ohne Kosten ist Selbstbetrug.
4. Survivorship-Bias ist die Haupt-Falle des Universums-Wechsels — unverzerrte Liste ist Pflicht.
5. Erst validieren, dann bauen.

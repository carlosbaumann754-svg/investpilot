"""R-B60: SEC Insider-Transactions-Datasets -> PIT-Ereignisarchiv.

Quelle: strukturierte Quartals-Zips der SEC (Form 3/4/5). Je Quartal:
SUBMISSION.tsv (ACCESSION_NUMBER, FILING_DATE, ISSUERCIK),
NONDERIV_TRANS.tsv (TRANS_CODE, TRANS_SHARES, TRANS_PRICEPERSHARE, ...),
REPORTINGOWNER.tsv (RPTOWNERCIK) fuer Cluster-Zaehlung (distinct owners).
Gefiltert auf unser Universum (CIK-Map), nur Code P (Open-Market-Kauf).
Output: /app/data/insider_events_pit.json
  {symbol: [[filing_date, trans_date, owner_cik, shares, price], ...]}
"""
import io, json, sys, time, zipfile

# R-B60 (27.08.2026): Marker fuer den Bauplan-Generator (Host-Cron-Skript).
AUDIT_METADATA = {
    "purpose": (
        "SEC-Form-345-Sammler: laedt die strukturierten Quartals-Datensaetze "
        "der SEC (Insider-Transaktionen), filtert auf das S&P-600-Universum "
        "und pflegt das Punkt-in-Zeit-Ereignisarchiv (nur Open-Market-Kaeufe, "
        "Informationsdatum = FILING-Datum, ISO-normalisiert). Grundlage des "
        "vorregistrierten Insider-Signal-Tests (Ergebnis 27.08.: NEIN) und "
        "jedes kuenftigen Insider-Kandidaten. Quartals-Cron haelt das Archiv "
        "aktuell — Komplett-Neubau jederzeit in ~1 Minute moeglich."
    ),
    "config_section": None,
    "state_files": ["insider_events_pit.json"],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "R-B60 (27.08.2026)",
}

import urllib.request

sys.path.insert(0, "/app")

UA = {"User-Agent": "InvestPilot Research carlosbaumann754@gmail.com"}
QUARTALE = [(y, q) for y in range(2018, 2027) for q in range(1, 5)
            if not (y == 2018 and q < 3) and not (y == 2026 and q > 3)]
URL_MUSTER = [
    "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{y}q{q}_form345.zip",
    "https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/{y}q{q}_form345.zip",
]

def lade_quartal(y, q):
    for m in URL_MUSTER:
        url = m.format(y=y, q=q)
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read(), url
        except Exception:
            continue
    return None, None

def tsv_dict(zf, name):
    """Liest eine TSV aus dem Zip -> Liste dicts (nur benoetigte Spalten)."""
    with zf.open(name) as f:
        raw = io.TextIOWrapper(f, encoding="utf-8", errors="replace")
        header = raw.readline().rstrip("\n").split("\t")
        idx = {h: i for i, h in enumerate(header)}
        rows = []
        for line in raw:
            rows.append(line.rstrip("\n").split("\t"))
        return idx, rows

def main():
    from app import sp600_universe
    cik_map = json.load(open("/app/data/edgar_cik_map.json"))
    # Map kann {symbol: cik} oder {symbol: {cik: ...}} sein — normalisieren
    sym2cik = {}
    for s, v in cik_map.items():
        c = v.get("cik") if isinstance(v, dict) else v
        if c:
            sym2cik[s.upper()] = str(int(str(c).lstrip("0") or "0"))
    universum = set(s.upper() for s in sp600_universe.get_symbols())
    cik2sym = {c: s for s, c in sym2cik.items() if s in universum}
    print(f"Universum: {len(universum)} | mit CIK: {len(cik2sym)}", flush=True)

    events = {}
    for (y, q) in QUARTALE:
        t0 = time.time()
        blob, url = lade_quartal(y, q)
        if blob is None:
            print(f"{y}q{q}: DOWNLOAD FEHLGESCHLAGEN (beide URL-Muster)", flush=True)
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(blob))
            namen = {n.upper().split("/")[-1]: n for n in zf.namelist()}
            si, srows = tsv_dict(zf, namen["SUBMISSION.TSV"])
            acc2meta = {}
            for r in srows:
                cik_raw = r[si["ISSUERCIK"]].strip()
                cik = str(int(cik_raw)) if cik_raw.isdigit() else cik_raw
                sym = cik2sym.get(cik)
                if sym:
                    acc2meta[r[si["ACCESSION_NUMBER"]]] = (sym, r[si["FILING_DATE"]])
            oi, orows = tsv_dict(zf, namen["REPORTINGOWNER.TSV"])
            acc2owner = {}
            for r in orows:
                acc = r[oi["ACCESSION_NUMBER"]]
                if acc in acc2meta:
                    acc2owner.setdefault(acc, r[oi["RPTOWNERCIK"]])
            ni, nrows = tsv_dict(zf, namen["NONDERIV_TRANS.TSV"])
            n_neu = 0
            for r in nrows:
                acc = r[ni["ACCESSION_NUMBER"]]
                meta = acc2meta.get(acc)
                if not meta:
                    continue
                if r[ni["TRANS_CODE"]].strip() != "P":
                    continue
                sym, fdate = meta
                try:
                    shares = float(r[ni["TRANS_SHARES"]] or 0)
                    preis = float(r[ni["TRANS_PRICEPERSHARE"]] or 0)
                except ValueError:
                    shares, preis = 0.0, 0.0
                tdate = r[ni["TRANS_DATE"]].strip()
                events.setdefault(sym, []).append(
                    [fdate, tdate, acc2owner.get(acc, "?"), shares, preis])
                n_neu += 1
            print(f"{y}q{q}: {len(acc2meta)} Filings im Universum, "
                  f"{n_neu} P-Kaeufe [{time.time()-t0:.0f}s, {len(blob)//1048576}MB]",
                  flush=True)
        except Exception as e:
            print(f"{y}q{q}: PARSE-FEHLER {e}", flush=True)
        time.sleep(1)

    from datetime import datetime as _dt
    def _iso(x):
        x = (x or "").strip()
        try:
            return _dt.strptime(x, "%d-%b-%Y").date().isoformat()
        except ValueError:
            return x[:10]
    for sym in events:
        for r in events[sym]:
            r[0], r[1] = _iso(r[0]), _iso(r[1])
        events[sym].sort()
    gesamt = sum(len(v) for v in events.values())
    with open("/app/data/insider_events_pit.json", "w", encoding="utf-8") as f:
        json.dump({"generated_for": "R-B60 Insider-Signal-Test",
                   "datumsformat": "ISO", "quelle": "SEC Form345-Datasets, nur TRANS_CODE=P, "
                             "Informationsdatum=FILING_DATE",
                   "symbole": len(events), "kaeufe": gesamt,
                   "events": events}, f)
    print(f"FERTIG: {gesamt} Open-Market-Kaeufe / {len(events)} Symbole "
          f"-> data/insider_events_pit.json", flush=True)

main()

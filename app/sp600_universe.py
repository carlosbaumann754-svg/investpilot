"""S&P-SmallCap-600-Universum (validierte Teilmenge) fuer den Signal-Stack.

Das neue Auswahl-Universum fuer die Fundamental-Strategie: US-Small-Caps, wo die
5 Signale strukturell funktionieren (anders als bei den 51 ETFs/Mega-Caps des
Alt-Universums, wo Fundamental-Signale gar nicht existieren bzw. tot sind).

Diese Liste = die EXAKTE Teilmenge (alphabetisch A-M, ~330 Namen), auf der der
5-Signal-Stack validiert wurde (Sharpe 1.02 vs Benchmark 0.77, 15J, positiv in
beiden Halbperioden — siehe docs/UNIVERSE_CHANGE_PLAN.md). Alphabetisch =
rendite-neutral, also unverzerrt. Erweiterung auf die vollen 600 + Point-in-Time-
Membership ist ein spaeterer Rigor-Schritt.

WICHTIG vor Phase-4-Live-Switch: IBKR-Handelbarkeit aller Symbole pruefen
(qualifyContracts). Fuer Phase 2 (Shadow, kein Trading) nicht noetig.
"""
from __future__ import annotations

AUDIT_METADATA = {
    "purpose": "S&P-600-Small-Cap-Universum (validierte ~330-Teilmenge) fuer den Fundamental-Signal-Stack — ersetzt das 51-ETF/Mega-Cap-Universum in der Auswahl",
    "config_section": "signal_stack",
    "state_files": [],
    "self_tests": [],
    "scheduler_hooks": [],
    "health_check": None,
    "added_in": "v38 (Fundamental-Signal-Stack — Phase 3 Universum)",
}

# Validierte Teilmenge des S&P SmallCap 600 (Quelle: Wikipedia-Konstituenten,
# alphabetisch A-M). Reine US-Stocks (kein ETF/Fonds).
SP600_SYMBOLS = [
    "AAMI", "AAP", "AAT", "ABCB", "ABG", "ABM", "ABR", "ACA", "ACAD", "ACHC",
    "ACIW", "ACLS", "ACMR", "ACT", "ADEA", "ADMA", "ADNT", "ADT", "ADUS", "AEO",
    "AESI", "AGO", "AGX", "AGYS", "AHCO", "AIN", "AIR", "AKR", "ALG", "ALGT",
    "ALHC", "ALKS", "ALRM", "AMN", "AMPH", "AMR", "AMRX", "AMSF", "AMWD", "ANDE",
    "ANIP", "AORT", "AOSL", "APAM", "APLE", "APOG", "ARCB", "ARI", "ARLO", "AROC",
    "ARR", "ASO", "ASTE", "ASTH", "ATEN", "ATMU", "AUB", "AVA", "AVNS", "AWI",
    "AWR", "AX", "AZTA", "AZZ", "BANC", "BANF", "BANR", "BCC", "BCPC", "BFAM",
    "BFH", "BFS", "BGC", "BHE", "BJRI", "BKE", "BKU", "BL", "BLFS", "BMI",
    "BNL", "BOH", "BOOT", "BOX", "BRC", "BTU", "BXMT", "CABO", "CAKE", "CALM",
    "CALX", "CARG", "CASH", "CATY", "CBRL", "CBU", "CC", "CCOI", "CCS", "CE",
    "CENT", "CENX", "CERT", "CFFN", "CHCO", "CHEF", "CLB", "CLSK", "CNK", "CNMD",
    "CNR", "CNS", "CNXN", "COHU", "COLL", "CORT", "CPF", "CPK", "CPRX", "CRC",
    "CRGY", "CRI", "CRK", "CRSR", "CRVL", "CSR", "CSW", "CTS", "CUBI", "CVBF",
    "CVCO", "CVI", "CWEN", "CWK", "CWST", "CWT", "CXM", "CXW", "CZR", "DAN",
    "DBD", "DCOM", "DEA", "DEI", "DFH", "DFIN", "DGII", "DIOD", "DLX", "DNOW",
    "DORM", "DRH", "DV", "DXC", "DXPE", "EAT", "ECPG", "EFC", "EGBN", "EIG",
    "EMBC", "EMN", "ENOV", "ENPH", "ENR", "ENVA", "EPAC", "EPC", "EPRT", "ESE",
    "ETD", "ETSY", "EVTC", "EXTR", "EYE", "EZPW", "FBK", "FBNC", "FBP", "FBRT",
    "FCF", "FCPT", "FDP", "FELE", "FFBC", "FG", "FHB", "FIBK", "FIZZ", "FLO",
    "FMC", "FORM", "FOXF", "FRPT", "FSS", "FTDR", "FTRE", "FUL", "FULT", "FUN",
    "FWRD", "GBX", "GDYN", "GEO", "GFF", "GIII", "GKOS", "GNL", "GNW", "GO",
    "GOGO", "GOLF", "GPI", "GRBK", "GSHD", "GTES", "GTY", "GVA", "HAFC", "HASI",
    "HAYW", "HCC", "HCI", "HCSG", "HE", "HFWA", "HIW", "HLIT", "HLX", "HMN",
    "HNI", "HOPE", "HP", "HRMY", "HSTM", "HTH", "HTLD", "HTZ", "HUBG", "HWKN",
    "HZO", "IART", "IBP", "ICHR", "ICUI", "IIIN", "IIPR", "INDB", "INDV", "INSP",
    "INSW", "INVA", "IOSP", "IPAR", "IRDM", "ITGR", "ITRI", "JBGS", "JBLU", "JBSS",
    "JJSF", "JOE", "JXN", "KAI", "KALU", "KFY", "KGS", "KLIC", "KMPR", "KMT",
    "KMX", "KN", "KNTK", "KOP", "KRYS", "KSS", "KTB", "KW", "KWR", "LAUR",
    "LBRT", "LCII", "LEG", "LFST", "LGIH", "LGND", "LKFN", "LKQ", "LMAT", "LNC",
    "LNN", "LPG", "LQDT", "LRN", "LTC", "LTH", "LUMN", "LW", "LXP", "LYFT",
    "LZ", "LZB", "MAC", "MAN", "MARA", "MATW", "MATX", "MBC", "MBIN",
]


def get_symbols() -> list:
    """Die Symbol-Liste (Kopie, dedupliziert, sortiert)."""
    return sorted(set(SP600_SYMBOLS))


def universe_entries() -> dict:
    """{symbol: {yf, class, name, sector}} — Format kompatibel mit ASSET_UNIVERSE.

    Fuer das Mergen ins Trading-Universum (Phase 4). yf == Symbol (US-Ticker),
    class == 'stocks'. name/sector als Platzhalter (per EDGAR/IBKR anreicherbar).
    """
    return {
        s: {"yf": s, "class": "stocks", "name": s, "sector": "smallcap"}
        for s in get_symbols()
    }

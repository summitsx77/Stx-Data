# SEC-Daten für die Momentum-Früherkennung (S&P-500-Mitglieder seit 2009).
# 1) cik_map.csv      Ticker -> CIK (aktuelle SEC-Liste + historische Ticker aus den Insiderdaten)
# 2) fundamentals.parquet  Quartals-/Jahreswerte ausgewählter XBRL-Positionen aus den
#    "Financial Statement Data Sets" mit Einreichungsdatum (point-in-time: erste Veröffentlichung)
# 3) events_8k.parquet     8-K-Meldungen mit Ereignisnummern (Items) aus der Submissions-API
# Läuft inkrementell; pro Lauf höchstens MAX_Q Quartale, damit der Job im Zeitlimit bleibt.
import os, io, sys, json, time, gzip, zipfile, datetime, urllib.request
import pandas as pd

OUT = 'data/sec'
UA = os.environ.get('SEC_UA', '').strip()
MAX_Q = int(os.environ.get('MAX_Q', '12'))
MEMBERS_URL = 'https://github.com/Johnbrick123/sp500-data/releases/download/data/membership_intervals.parquet'
FSDS = 'https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip'
TICKERS = 'https://www.sec.gov/files/company_tickers.json'
SUBM = 'https://data.sec.gov/submissions/{name}'
TAGS = {  # Umsatz, Ergebnis, Margen, Cashflow, Vorlaufkennzahlen
    'Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'RevenueFromContractWithCustomerIncludingAssessedTax',
    'SalesRevenueNet', 'SalesRevenueGoodsNet', 'SalesRevenueServicesNet', 'RevenuesNetOfInterestExpense',
    'GrossProfit', 'CostOfRevenue', 'CostOfGoodsAndServicesSold', 'OperatingIncomeLoss', 'NetIncomeLoss',
    'EarningsPerShareDiluted', 'EarningsPerShareBasic', 'WeightedAverageNumberOfDilutedSharesOutstanding',
    'NetCashProvidedByUsedInOperatingActivities', 'PaymentsToAcquirePropertyPlantAndEquipment',
    'RevenueRemainingPerformanceObligation', 'ContractWithCustomerLiabilityCurrent', 'ContractWithCustomerLiability',
    'DeferredRevenueCurrent', 'ResearchAndDevelopmentExpense', 'LongTermDebt', 'StockholdersEquity', 'Assets'}
FORMS = {'10-Q', '10-K', '10-Q/A', '10-K/A', '20-F', '40-F', '10-KT'}
log = []

def get(url, tries=4):
    for k in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip, deflate'}), timeout=180)
            b = r.read()
            if r.headers.get('Content-Encoding', '') == 'gzip': b = gzip.decompress(b)
            return b
        except urllib.error.HTTPError as e:
            if e.code == 404: raise
            time.sleep(5 * (k + 1))
        except Exception:
            time.sleep(5 * (k + 1))
    raise RuntimeError('download failed: ' + url)

def base_symbol(label):
    s = label.rsplit('-', 1)
    return s[0] if len(s) == 2 and s[1].isdigit() and len(s[1]) == 6 else label

def build_cik_map():
    iv = pd.read_parquet(io.BytesIO(urllib.request.urlopen(MEMBERS_URL, timeout=120).read()))
    iv['end'] = pd.to_datetime(iv['end']).fillna(pd.Timestamp('2100-01-01'))
    iv = iv[iv['end'] >= '2008-01-01'].copy()
    iv['sym'] = iv['ticker'].map(base_symbol)
    cur = json.loads(get(TICKERS))
    cur = pd.DataFrame(cur.values()).rename(columns={'cik_str': 'cik', 'ticker': 'sym'})
    cur['sym'] = cur['sym'].str.upper().str.replace('.', '-', regex=False)
    rows = []
    ins = None
    if os.path.exists('data/insider/purchases.csv.gz'):
        ins = pd.read_csv('data/insider/purchases.csv.gz', usecols=['ISSUERCIK', 'ISSUERTRADINGSYMBOL', 'FILING_DATE'], dtype=str)
        ins['sym'] = ins['ISSUERTRADINGSYMBOL'].str.upper().str.strip().str.replace('.', '-', regex=False)
        ins['fd'] = pd.to_datetime(ins['FILING_DATE'], format='%d-%b-%Y', errors='coerce')
    for r in iv.itertuples():
        cik, src = None, ''
        live = r.end > pd.Timestamp.today()
        if live:
            m = cur[cur['sym'] == r.sym]
            if len(m): cik, src = int(m['cik'].iloc[0]), 'sec_aktuell'
        if cik is None and ins is not None:
            m = ins[(ins['sym'] == r.sym) & (ins['fd'] >= pd.Timestamp(r.start)) & (ins['fd'] < r.end)]
            if len(m): cik, src = int(m['ISSUERCIK'].mode().iloc[0]), 'insider_zeitraum'
        if cik is None and not live:
            m = cur[cur['sym'] == r.sym]
            if len(m): cik, src = int(m['cik'].iloc[0]), 'sec_aktuell_unsicher'
        rows.append((r.ticker, r.sym, str(pd.Timestamp(r.start).date()), str(r.end.date()) if r.end.year < 2100 else '', cik, src))
    cm = pd.DataFrame(rows, columns=['label', 'sym', 'start', 'end', 'cik', 'quelle'])
    over = 'routinen/cik_overrides.csv'          # Handpflege: label,cik
    if os.path.exists(over):
        o = pd.read_csv(over, dtype={'cik': 'Int64'})
        cm = cm.merge(o, on='label', how='left', suffixes=('', '_hand'))
        cm['quelle'] = cm['quelle'].where(cm['cik_hand'].isna(), 'hand')
        cm['cik'] = cm['cik_hand'].combine_first(cm['cik']); cm = cm.drop(columns='cik_hand')
    cm['cik'] = cm['cik'].astype('Int64')
    cm.to_csv(f'{OUT}/cik_map.csv', index=False)
    log.append(f'cik_map: {cm["cik"].notna().sum()} von {len(cm)} Labels zugeordnet')
    return set(int(c) for c in cm['cik'].dropna())

def read_tsv(z, name, usecols=None, chunks=False):
    n = next(x for x in z.namelist() if x.lower().endswith(name))
    kw = dict(sep='\t', dtype=str, on_bad_lines='skip', encoding_errors='replace', quoting=3)
    if usecols: kw['usecols'] = lambda c: c in usecols
    if chunks: return pd.read_csv(z.open(n), chunksize=1_500_000, **kw)
    return pd.read_csv(z.open(n), **kw)

def parse_quarter(b, ciks):
    z = zipfile.ZipFile(io.BytesIO(b))
    sub = read_tsv(z, 'sub.txt', {'adsh', 'cik', 'name', 'sic', 'form', 'period', 'fy', 'fp', 'filed', 'accepted'})
    sub['cik'] = pd.to_numeric(sub['cik'], errors='coerce')
    sub = sub[sub['cik'].isin(ciks) & sub['form'].isin(FORMS)]
    keep = set(sub['adsh'])
    parts = []
    for ch in read_tsv(z, 'num.txt', {'adsh', 'tag', 'ddate', 'qtrs', 'uom', 'value', 'coreg', 'segments'}, chunks=True):
        ch = ch[ch['adsh'].isin(keep) & ch['tag'].isin(TAGS)]
        for c in ('coreg', 'segments'):        # nur Konzernwerte ohne Segment-/Tochterzerlegung
            if c in ch.columns: ch = ch[ch[c].isna() | (ch[c] == '')]
        parts.append(ch[['adsh', 'tag', 'ddate', 'qtrs', 'uom', 'value']])
    num = pd.concat(parts) if parts else pd.DataFrame(columns=['adsh', 'tag', 'ddate', 'qtrs', 'uom', 'value'])
    m = num.merge(sub, on='adsh', how='inner')
    m['value'] = pd.to_numeric(m['value'], errors='coerce'); m['qtrs'] = pd.to_numeric(m['qtrs'], errors='coerce')
    return m

def fundamentals(ciks):
    f_out = f'{OUT}/fundamentals.parquet'; done_f = f'{OUT}/_fsds_done.txt'
    done = set(open(done_f).read().split()) if os.path.exists(done_f) else set()
    today = datetime.date.today()
    qs = [f'{y}q{q}' for y in range(2009, today.year + 1) for q in range(1, 5) if (y, q) < (today.year, (today.month - 1) // 3 + 1)]
    todo = [q for q in qs if q not in done][:MAX_Q]
    old = pd.read_parquet(f_out) if os.path.exists(f_out) else None
    new = []
    for q in todo:
        try:
            m = parse_quarter(get(FSDS.format(q=q)), ciks); m['src_q'] = q
            new.append(m); done.add(q); log.append(f'fsds {q}: {len(m)} Werte, {m["cik"].nunique()} Firmen')
        except Exception as e:
            log.append(f'fsds {q}: FAIL {str(e)[:150]}')
        time.sleep(1)
    if new:
        allf = pd.concat(([old] if old is not None else []) + new, ignore_index=True)
        allf = allf.sort_values('filed').drop_duplicates(['cik', 'tag', 'ddate', 'qtrs', 'uom', 'adsh'])
        allf.to_parquet(f_out, index=False)
        open(done_f, 'w').write('\n'.join(sorted(done)) + '\n')

def events(ciks):
    e_out = f'{OUT}/events_8k.parquet'
    old = pd.read_parquet(e_out) if os.path.exists(e_out) else None
    have_old = set(old['cik']) if old is not None else set()
    rows = []
    for k, cik in enumerate(sorted(ciks)):
        name = f'CIK{cik:010d}.json'
        try:
            j = json.loads(get(SUBM.format(name=name)))
            blocks = [j['filings']['recent']]
            if cik not in have_old:                   # ältere Seiten nur beim ersten Abruf
                for f in j['filings'].get('files', []):
                    time.sleep(0.12); blocks.append(json.loads(get(SUBM.format(name=f['name']))))
            for bl in blocks:
                d = pd.DataFrame({c: bl.get(c, []) for c in ['accessionNumber', 'form', 'filingDate', 'acceptanceDateTime', 'reportDate', 'items']})
                d = d[d['form'].isin(['8-K', '8-K/A'])]; d['cik'] = cik; rows.append(d)
        except Exception as e:
            log.append(f'8-K {cik}: FAIL {str(e)[:100]}')
        time.sleep(0.12)                              # SEC-Grenze: höchstens 10 Abrufe pro Sekunde
    if rows:
        ev = pd.concat(rows + ([old] if old is not None else []), ignore_index=True).drop_duplicates('accessionNumber')
        ev.to_parquet(e_out, index=False); log.append(f'8-K: {len(ev)} Meldungen, {ev["cik"].nunique()} Firmen')

if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    if not UA:
        print('SEC_UA secret fehlt - Abbruch'); open(f'{OUT}/_log.txt', 'w').write('SEC_UA fehlt\n'); sys.exit(0)
    ciks = build_cik_map()
    fundamentals(ciks)
    events(ciks)
    open(f'{OUT}/_log.txt', 'w').write(f'{datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC\n' + '\n'.join(log) + '\n')
    print('\n'.join(log))

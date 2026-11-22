# Daten für die Value-Analysen (S&P-500-Mitglieder seit 2009) – getrennt von der Momentum-Pipeline.
# 1) data/value/sub/<q>.parquet   Verzeichnis aller 10-K/10-Q-Einreicher je Quartal (CIK, Name, Branche, Datum)
# 2) data/value/cik_ext.csv       Label -> alle CIKs über die Zeit. Grund: Umgründungen/Holdings wechseln den CIK
#    (z. B. Exxon, Disney, Cigna, BlackRock, Alphabet); die Momentum-Zuordnung kennt nur den aktuellen CIK.
#    Ergänzt per gleichem Firmennamen (normalisiert) und per Namensmuster aus routinen/value_cik_muster.csv,
#    jeweils nur für Einreichungen innerhalb der Index-Mitgliedschaft des Labels.
# 3) data/value/fsds/<q>.parquet  Bewertungsrelevante XBRL-Positionen (point-in-time, Einreichungsdatum) für alle
#    CIKs aus 2). Aktienzahlen behalten Klassenzerlegungen (segments), damit Mehrklassen-Firmen summierbar sind.
# 4) data/value/french/           Fama/French-Faktoren und Value-Portfolios USA + international
import os, io, re, sys, time, gzip, zipfile, hashlib, datetime, urllib.request
import pandas as pd

OUT = 'data/value'
UA = os.environ.get('SEC_UA', '').strip()
CACHE = os.environ.get('FSDS_CACHE', '/tmp/fsds')
FSDS = 'https://www.sec.gov/files/dera/data/financial-statement-data-sets/{q}.zip'
SHARE_TAGS = {'EntityCommonStockSharesOutstanding', 'CommonStockSharesOutstanding',
              'WeightedAverageNumberOfDilutedSharesOutstanding', 'WeightedAverageNumberOfSharesOutstandingBasic'}
TAGS = SHARE_TAGS | {
    # Bilanz
    'Assets', 'AssetsCurrent', 'Liabilities', 'LiabilitiesCurrent', 'LiabilitiesAndStockholdersEquity',
    'StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest', 'MinorityInterest',
    'PreferredStockValue', 'CashAndCashEquivalentsAtCarryingValue', 'Cash',
    'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', 'ShortTermInvestments', 'MarketableSecuritiesCurrent',
    'AvailableForSaleSecuritiesDebtSecuritiesCurrent', 'LongTermDebt', 'LongTermDebtNoncurrent', 'LongTermDebtCurrent',
    'DebtCurrent', 'ShortTermBorrowings', 'CommercialPaper', 'LongTermDebtAndCapitalLeaseObligations',
    'LongTermDebtAndCapitalLeaseObligationsCurrent', 'DebtLongtermAndShorttermCombinedAmount', 'OperatingLeaseLiability',
    'Goodwill', 'IntangibleAssetsNetExcludingGoodwill', 'RetainedEarningsAccumulatedDeficit', 'InventoryNet',
    'AccountsReceivableNetCurrent', 'PropertyPlantAndEquipmentNet',
    # Ergebnisrechnung
    'Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'RevenueFromContractWithCustomerIncludingAssessedTax',
    'SalesRevenueNet', 'SalesRevenueGoodsNet', 'SalesRevenueServicesNet', 'RevenuesNetOfInterestExpense',
    'InterestAndDividendIncomeOperating', 'InterestIncomeExpenseNet', 'NoninterestIncome',
    'GrossProfit', 'CostOfRevenue', 'CostOfGoodsAndServicesSold', 'OperatingIncomeLoss',
    'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
    'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments',
    'IncomeTaxExpenseBenefit', 'InterestExpense', 'NetIncomeLoss', 'NetIncomeLossAvailableToCommonStockholdersBasic',
    'ProfitLoss', 'EarningsPerShareDiluted', 'EarningsPerShareBasic', 'ResearchAndDevelopmentExpense',
    'SellingGeneralAndAdministrativeExpense', 'DepreciationDepletionAndAmortization', 'DepreciationAndAmortization',
    'DepreciationAmortizationAndAccretionNet', 'Depreciation',
    # Kapitalfluss und Ausschüttung
    'NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations',
    'PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets', 'PaymentsOfDividends',
    'PaymentsOfDividendsCommonStock', 'PaymentsForRepurchaseOfCommonStock', 'ProceedsFromIssuanceOfCommonStock',
    'ShareBasedCompensation', 'AllocatedShareBasedCompensationExpense', 'CommonStockDividendsPerShareDeclared',
    'CommonStockDividendsPerShareCashPaid'}
FORMS = {'10-Q', '10-K', '10-Q/A', '10-K/A', '20-F', '40-F', '10-KT'}
FRENCH = 'https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/'
FRENCH_FILES = [
    'F-F_Research_Data_Factors_CSV.zip', 'F-F_Research_Data_5_Factors_2x3_CSV.zip', 'F-F_Momentum_Factor_CSV.zip',
    'Portfolios_Formed_on_BE-ME_CSV.zip', 'Portfolios_Formed_on_E-P_CSV.zip', 'Portfolios_Formed_on_CF-P_CSV.zip',
    'Portfolios_Formed_on_D-P_CSV.zip', '6_Portfolios_2x3_CSV.zip', '25_Portfolios_5x5_CSV.zip',
    '25_Portfolios_BEME_OP_5x5_CSV.zip',
    'Developed_3_Factors_CSV.zip', 'Developed_ex_US_3_Factors_CSV.zip', 'Europe_3_Factors_CSV.zip', 'Japan_3_Factors_CSV.zip',
    'Asia_Pacific_ex_Japan_3_Factors_CSV.zip', 'North_America_3_Factors_CSV.zip',
    'Developed_5_Factors_CSV.zip', 'Developed_ex_US_5_Factors_CSV.zip', 'Europe_5_Factors_CSV.zip', 'Japan_5_Factors_CSV.zip',
    'Asia_Pacific_ex_Japan_5_Factors_CSV.zip', 'North_America_5_Factors_CSV.zip', 'Emerging_5_Factors_CSV.zip',
    'Developed_6_Portfolios_ME_BE-ME_CSV.zip', 'Developed_ex_US_6_Portfolios_ME_BE-ME_CSV.zip',
    'Europe_6_Portfolios_ME_BE-ME_CSV.zip', 'Japan_6_Portfolios_ME_BE-ME_CSV.zip',
    'Asia_Pacific_ex_Japan_6_Portfolios_ME_BE-ME_CSV.zip', 'North_America_6_Portfolios_ME_BE-ME_CSV.zip',
    'Emerging_Markets_6_Portfolios_ME_BE-ME_CSV.zip', 'Developed_Mom_Factor_CSV.zip', 'Developed_ex_US_Mom_Factor_CSV.zip']
STOP = {'INC', 'CORP', 'CORPORATION', 'CO', 'COMPANY', 'LTD', 'LIMITED', 'PLC', 'HOLDINGS', 'HOLDING', 'GROUP', 'NV',
        'SA', 'AG', 'LP', 'LLC', 'THE', 'NEW', 'DE', 'INCORPORATED', 'CORPORATIO', 'TRUST', 'REIT'}
log = []
T0 = time.time()

def norm(name):
    s = re.sub(r'/[A-Z]{2,3}/?', ' ', str(name).upper())
    s = re.sub(r'[^A-Z0-9 ]', ' ', s.replace('&', ' '))
    return ' '.join(w for w in s.split() if w not in STOP)

def get(url, headers, tries=4):
    for k in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=300)
            b = r.read()
            if r.headers.get('Content-Encoding', '') == 'gzip': b = gzip.decompress(b)
            return b
        except urllib.error.HTTPError as e:
            if e.code == 404: raise
            time.sleep(5 * (k + 1))
        except Exception:
            time.sleep(5 * (k + 1))
    raise RuntimeError('download failed: ' + url)

def zip_for(q):
    p = f'{CACHE}/{q}.zip'
    if not os.path.exists(p):
        b = get(FSDS.format(q=q), {'User-Agent': UA, 'Accept-Encoding': 'gzip, deflate'})
        open(p, 'wb').write(b); time.sleep(1)
    return zipfile.ZipFile(p)

def read_tsv(z, name, usecols=None, chunks=False):
    n = next(x for x in z.namelist() if x.lower().endswith(name))
    kw = dict(sep='\t', dtype=str, on_bad_lines='skip', encoding_errors='replace', quoting=3)
    if usecols: kw['usecols'] = lambda c: c in usecols
    if chunks: return pd.read_csv(z.open(n), chunksize=1_500_000, **kw)
    return pd.read_csv(z.open(n), **kw)

def quarters():
    today = datetime.date.today(); cur = (today.year, (today.month - 1) // 3 + 1)
    return [f'{y}q{q}' for y in range(2009, today.year + 1) for q in range(1, 5) if (y, q) < cur]

def subs(qs):
    d = f'{OUT}/sub'; os.makedirs(d, exist_ok=True)
    last2 = qs[-2:]
    for q in qs:
        if os.path.exists(f'{d}/{q}.parquet') and q not in last2: continue
        try:
            s = read_tsv(zip_for(q), 'sub.txt', {'adsh', 'cik', 'name', 'sic', 'form', 'period', 'fy', 'fp', 'filed'})
            s = s[s['form'].isin(FORMS)]
            s.to_parquet(f'{d}/{q}.parquet', index=False)
        except Exception as e:
            log.append(f'sub {q}: FAIL {str(e)[:150]}')
    return pd.concat([pd.read_parquet(f'{d}/{f}') for f in sorted(os.listdir(d)) if f.endswith('.parquet')], ignore_index=True)

def cik_ext(sub):
    cm = pd.read_csv('data/sec/cik_map.csv', dtype={'cik': 'Int64'})
    cm['start'] = pd.to_datetime(cm['start']); cm['end'] = pd.to_datetime(cm['end']).fillna(pd.Timestamp('2100-01-01'))
    sub = sub.copy()
    sub['cik'] = pd.to_numeric(sub['cik'], errors='coerce').astype('Int64')
    sub['filed'] = pd.to_datetime(sub['filed'], format='%Y%m%d', errors='coerce')
    sub['nn'] = sub['name'].map(norm)
    sub['nu'] = sub['name'].astype(str).str.upper()
    spans = sub.groupby(['cik', 'nn', 'nu'])['filed'].agg(['min', 'max']).reset_index()
    names = sub.groupby('cik')['nn'].agg(lambda s: set(s)).to_dict()
    life = sub.groupby('cik')['filed'].agg(['min', 'max'])
    pat = pd.read_csv('routinen/value_cik_muster.csv') if os.path.exists('routinen/value_cik_muster.csv') else pd.DataFrame(columns=['label', 'muster'])
    rows = []
    for r in cm.itertuples(index=False):
        lo, hi = r.start - pd.Timedelta(days=200), r.end + pd.Timedelta(days=120)
        def add(cands, via):
            for c in cands.itertuples(index=False):
                if c.max >= lo and c.min <= hi:
                    rows.append((r.label, int(c.cik), c.nu, via, str(max(c.min, lo).date()), str(min(c.max, hi).date())))
        if pd.notna(r.cik):
            own = spans[spans['cik'] == r.cik]
            add(own, 'karte')
            nn = names.get(r.cik, set())
            if nn and r.cik in life.index:
                a, b = life.loc[r.cik, 'min'], life.loc[r.cik, 'max']
                c = spans[spans['nn'].isin(nn) & (spans['cik'] != r.cik)]
                # Vorgänger/Nachfolger reichen nicht gleichzeitig ein (höchstens 120 Tage Überlappung)
                c = c[(c['max'] <= a + pd.Timedelta(days=120)) | (c['min'] >= b - pd.Timedelta(days=120))]
                add(c, 'name')
        for m in pat.loc[pat['label'] == r.label, 'muster']:
            add(spans[spans['nu'].str.contains(m, regex=True, na=False)], 'muster')
    ext = pd.DataFrame(rows, columns=['label', 'cik', 'name', 'via', 'von', 'bis']).drop_duplicates(['label', 'cik', 'name'])
    ext.to_csv(f'{OUT}/cik_ext.csv', index=False)
    log.append(f'cik_ext: {ext["label"].nunique()} Labels, {ext["cik"].nunique()} CIKs (davon per Name {ext.loc[ext.via=="name","cik"].nunique()}, per Muster {ext.loc[ext.via=="muster","cik"].nunique()})')
    return set(ext['cik'])

def parse_quarter(z, ciks):
    sub = read_tsv(z, 'sub.txt', {'adsh', 'cik', 'name', 'sic', 'form', 'period', 'fy', 'fp', 'filed', 'accepted'})
    sub['cik'] = pd.to_numeric(sub['cik'], errors='coerce')
    sub = sub[sub['cik'].isin(ciks) & sub['form'].isin(FORMS)]
    keep = set(sub['adsh'])
    parts = []
    for ch in read_tsv(z, 'num.txt', {'adsh', 'tag', 'ddate', 'qtrs', 'uom', 'value', 'coreg', 'segments'}, chunks=True):
        ch = ch[ch['adsh'].isin(keep) & ch['tag'].isin(TAGS)].copy()
        for c in ('coreg', 'segments'):
            if c not in ch.columns: ch[c] = ''
            ch[c] = ch[c].fillna('')
        plain = (ch['coreg'] == '') & (ch['segments'] == '')
        ch = ch[plain | (ch['tag'].isin(SHARE_TAGS) & (ch['coreg'] == ''))]
        parts.append(ch[['adsh', 'tag', 'ddate', 'qtrs', 'uom', 'value', 'segments']])
    num = pd.concat(parts) if parts else pd.DataFrame(columns=['adsh', 'tag', 'ddate', 'qtrs', 'uom', 'value', 'segments'])
    m = num.merge(sub, on='adsh', how='inner')
    m['value'] = pd.to_numeric(m['value'], errors='coerce'); m['qtrs'] = pd.to_numeric(m['qtrs'], errors='coerce')
    return m

def fundamentals(qs, ciks):
    d = f'{OUT}/fsds'; os.makedirs(d, exist_ok=True)
    h = hashlib.sha1(','.join(map(str, sorted(ciks))).encode()).hexdigest()[:12]
    hf = f'{d}/_ciks.txt'
    if not os.path.exists(hf) or open(hf).read().strip() != h:   # CIK-Menge geändert -> alles neu
        for f in os.listdir(d):
            if f.endswith('.parquet'): os.remove(f'{d}/{f}')
    last2 = qs[-2:]
    todo = [q for q in qs if not os.path.exists(f'{d}/{q}.parquet') or q in last2]
    for q in todo:
        if time.time() - T0 > 4.5 * 3600:
            log.append('Zeitlimit erreicht - Rest im nächsten Lauf'); break
        try:
            m = parse_quarter(zip_for(q), ciks); m['src_q'] = q
            m.to_parquet(f'{d}/{q}.parquet', index=False)
            log.append(f'fsds {q}: {len(m)} Werte, {m["cik"].nunique()} Firmen')
        except Exception as e:
            log.append(f'fsds {q}: FAIL {str(e)[:150]}')
    open(hf, 'w').write(h + '\n')

def french():
    d = f'{OUT}/french'; os.makedirs(d, exist_ok=True)
    for f in FRENCH_FILES:
        try:
            z = zipfile.ZipFile(io.BytesIO(get(FRENCH + f, {'User-Agent': 'Mozilla/5.0'})))
            for n in z.namelist():
                open(f'{d}/{n}', 'wb').write(z.read(n))
            log.append(f'french {f}: OK')
        except Exception as e:
            log.append(f'french {f}: FAIL {str(e)[:100]}')
        time.sleep(1)

if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True); os.makedirs(CACHE, exist_ok=True)
    french()
    if UA:
        qs = quarters()
        ciks = cik_ext(subs(qs))
        fundamentals(qs, ciks)
    else:
        log.append('SEC_UA secret fehlt - SEC-Teil übersprungen')
    open(f'{OUT}/_log.txt', 'w').write(f'{datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC\n' + '\n'.join(log) + '\n')
    print('\n'.join(log))

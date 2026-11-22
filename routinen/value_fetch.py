# Daten für die Value-Analysen (S&P-500-Mitglieder seit 2009) – getrennt von der Momentum-Pipeline.
# 1) data/value/fsds/<quartal>.parquet  Bewertungsrelevante XBRL-Positionen aus den SEC "Financial Statement
#    Data Sets" mit Einreichungsdatum (point-in-time). Je Quartal eine Datei, damit ein Zeitlimit keinen
#    Fortschritt kostet. Aktienzahlen (dei/us-gaap) behalten Klassenzerlegungen (segments), damit
#    Mehrklassen-Firmen summiert werden können; alle übrigen Positionen nur auf Konzernebene.
# 2) data/value/french/  Fama/French-Faktoren und Value-Portfolios USA + international (Kenneth French Data Library)
# CIKs kommen aus data/sec/cik_map.csv (Momentum-Pipeline), sonst aus der Mitgliederliste.
import os, io, sys, time, gzip, zipfile, datetime, urllib.request
import pandas as pd

OUT = 'data/value'
UA = os.environ.get('SEC_UA', '').strip()
MAX_Q = int(os.environ.get('MAX_Q', '80'))
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
log = []

def get(url, headers, tries=4):
    for k in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=180)
            b = r.read()
            if r.headers.get('Content-Encoding', '') == 'gzip': b = gzip.decompress(b)
            return b
        except urllib.error.HTTPError as e:
            if e.code == 404: raise
            time.sleep(5 * (k + 1))
        except Exception:
            time.sleep(5 * (k + 1))
    raise RuntimeError('download failed: ' + url)

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

def ciks_from_map():
    cm = pd.read_csv('data/sec/cik_map.csv', dtype={'cik': 'Int64'})
    return set(int(c) for c in cm['cik'].dropna())

def fundamentals(ciks):
    d = f'{OUT}/fsds'; os.makedirs(d, exist_ok=True)
    today = datetime.date.today()
    cur_q = (today.year, (today.month - 1) // 3 + 1)
    qs = [f'{y}q{q}' for y in range(2009, today.year + 1) for q in range(1, 5) if (y, q) < cur_q]
    last2 = qs[-2:]                       # die jüngsten zwei Quartale bei jedem Lauf neu (SEC ergänzt nachträglich)
    todo = [q for q in qs if not os.path.exists(f'{d}/{q}.parquet') or q in last2][:MAX_Q]
    t0 = time.time()
    for q in todo:
        if time.time() - t0 > 4.5 * 3600:
            log.append('Zeitlimit erreicht - Rest im nächsten Lauf'); break
        try:
            m = parse_quarter(get(FSDS.format(q=q), {'User-Agent': UA, 'Accept-Encoding': 'gzip, deflate'}), ciks)
            m['src_q'] = q
            m.to_parquet(f'{d}/{q}.parquet', index=False)
            log.append(f'fsds {q}: {len(m)} Werte, {m["cik"].nunique()} Firmen')
        except Exception as e:
            log.append(f'fsds {q}: FAIL {str(e)[:150]}')
        time.sleep(1)

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
    os.makedirs(OUT, exist_ok=True)
    french()
    if UA:
        fundamentals(ciks_from_map())
    else:
        log.append('SEC_UA secret fehlt - SEC-Teil übersprungen')
    open(f'{OUT}/_log.txt', 'w').write(f'{datetime.datetime.utcnow():%Y-%m-%d %H:%M} UTC\n' + '\n'.join(log) + '\n')
    print('\n'.join(log))

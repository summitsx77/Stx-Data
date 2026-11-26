#!/usr/bin/env python3
"""Buch-Kompositum – Auswahl und Papierdepot (Stx-Data/routinen/kompositum.py)

Regel (festgelegt 26.11.2026):
  Universum  S&P-500-Mitglieder am Stichtag ohne Finanzwerte (SIC 6000–6799)
  Filter     EBIT > 0, operativer Cashflow > 0, Schulden/EBITDA < 3,
             Vermögenswachstum (12 Monate) unter dem 80-%-Quantil des Universums
  Score      Mittel der Perzentilränge aus EBIT/EV, EBIT/investiertes Kapital,
             Ausschüttungsrendite (Dividenden + Rückkäufe − Emissionen) und −Vermögenswachstum
  Auswahl    20 Titel gleich gewichtet, Prüfung Ende Juni; gehaltene Titel bleiben bis Rang 40,
             danach jährlich alle Positionen wieder gleich gewichtet
  Daten      nur Berichte, die vor dem Stichtag bei der SEC eingereicht waren (12-Monats-Werte)

Aufruf:
  python kompositum.py auswahl --stx <Stx-Data> --sp500 <Ordner mit prices.parquet,
         membership_intervals.parquet> --stichtag 2027-06-30 --bestand "MAS,MO,..." --out ergebnis.json
  python kompositum.py wert --sp500 <Ordner> --von 2026-06-30 --bis 2027-06-30 --bestand "MAS,MO,..."
Benötigt: pandas, numpy, pyarrow, duckdb
"""
import argparse, glob, json, os, sys
import numpy as np, pandas as pd, duckdb

FLOW = {'rev': ['Revenues', 'RevenueFromContractWithCustomerExcludingAssessedTax', 'SalesRevenueNet',
                'RevenueFromContractWithCustomerIncludingAssessedTax', 'RevenuesNetOfInterestExpense'],
        'ebit': ['OperatingIncomeLoss'],
        'pretax': ['IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
                   'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments'],
        'intexp': ['InterestExpense'], 'ni': ['NetIncomeLoss', 'ProfitLoss'], 'gp': ['GrossProfit'],
        'cogs': ['CostOfGoodsAndServicesSold', 'CostOfRevenue'],
        'ocf': ['NetCashProvidedByUsedInOperatingActivities', 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'],
        'capex': ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets'],
        'da': ['DepreciationDepletionAndAmortization', 'DepreciationAndAmortization', 'DepreciationAmortizationAndAccretionNet', 'Depreciation'],
        'div': ['PaymentsOfDividendsCommonStock', 'PaymentsOfDividends'], 'buyb': ['PaymentsForRepurchaseOfCommonStock'],
        'issue': ['ProceedsFromIssuanceOfCommonStock'], 'eps': ['EarningsPerShareDiluted', 'EarningsPerShareBasic']}
STOCK = {'assets': ['Assets'], 'equity': ['StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'],
         'ca': ['AssetsCurrent'], 'cl': ['LiabilitiesCurrent'], 'ppe': ['PropertyPlantAndEquipmentNet'],
         'cash': ['CashAndCashEquivalentsAtCarryingValue', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents', 'Cash'],
         'sti': ['ShortTermInvestments', 'MarketableSecuritiesCurrent', 'AvailableForSaleSecuritiesDebtSecuritiesCurrent'],
         'ltd_nc': ['LongTermDebtNoncurrent', 'LongTermDebtAndCapitalLeaseObligations'],
         'ltd_c': ['LongTermDebtCurrent', 'LongTermDebtAndCapitalLeaseObligationsCurrent', 'DebtCurrent'],
         'ltd': ['LongTermDebt'], 'stb': ['ShortTermBorrowings', 'CommercialPaper'],
         'shares': ['CommonStockSharesOutstanding'], 'liab': ['Liabilities'], 'lse': ['LiabilitiesAndStockholdersEquity'],
         'eqnci': ['StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest']}
N, BUF = 20, 2


def fund_panel(stx):
    fs = sorted(glob.glob(os.path.join(stx, 'data/value/fsds/*.parquet')))
    df = pd.concat([pd.read_parquet(f) for f in fs], ignore_index=True)
    df = df[(df.segments.isna()) | (df.segments == '')]
    df = df[df.form.isin(['10-K', '10-Q', '10-KT'])]
    for c in ['ddate', 'period', 'filed']:
        df[c] = pd.to_datetime(df[c].astype(str))
    df = df.drop_duplicates(['adsh', 'tag', 'ddate', 'qtrs', 'uom'])
    alltags = set(sum(FLOW.values(), []) + sum(STOCK.values(), []) + ['WeightedAverageNumberOfDilutedSharesOutstanding'])
    df = df[df.tag.isin(alltags)]
    fil = df.drop_duplicates('adsh')[['adsh', 'cik', 'sic', 'form', 'period', 'fy', 'fp', 'filed']].copy()
    d = df[['adsh', 'tag', 'ddate', 'qtrs', 'value']].merge(fil[['adsh', 'period', 'fp']], on='adsh')
    d['lag'] = (d.period - d.ddate).dt.days
    qk = {'Q1': 1, 'Q2': 2, 'Q3': 3, 'FY': 4}
    d['k'] = d.fp.map(qk)
    out = fil.set_index('adsh').copy()
    cur = d[d.lag.abs() <= 10]
    pri = d[d.lag.between(355, 375)]
    for name, tags in FLOW.items():
        res = pd.DataFrame(columns=['c', 'p'], dtype=float)
        for t in tags:
            c = cur[(cur.tag == t) & (cur.qtrs == cur.k)].drop_duplicates('adsh').set_index('adsh')['value']
            p = pri[(pri.tag == t) & (pri.qtrs == pri.k)].drop_duplicates('adsh').set_index('adsh')['value']
            a = pd.DataFrame({'c': c, 'p': p}); a = a[a.c.notna()]; a = a[~a.index.isin(res.index)]
            res = pd.concat([res, a])
        out[name + '_ytd'] = res.c.reindex(out.index); out[name + '_ytdp'] = res.p.reindex(out.index)
    for name, tags in STOCK.items():
        s = None
        for t in tags:
            c = cur[(cur.tag == t) & (cur.qtrs == 0)].drop_duplicates('adsh').set_index('adsh')['value']
            s = c if s is None else s.combine_first(c)
        out[name] = s.reindex(out.index)
    w = cur[(cur.tag == 'WeightedAverageNumberOfDilutedSharesOutstanding') & (cur.qtrs == 1)].drop_duplicates('adsh').set_index('adsh')['value']
    w4 = cur[(cur.tag == 'WeightedAverageNumberOfDilutedSharesOutstanding') & (cur.qtrs == 4)].drop_duplicates('adsh').set_index('adsh')['value']
    out['wsh'] = w.combine_first(w4)
    out = out.reset_index().sort_values(['cik', 'period', 'filed']).drop_duplicates(['cik', 'period'], keep='first')
    fy = out[out.fp == 'FY'][['cik', 'period'] + [n + '_ytd' for n in FLOW]].rename(columns={'period': 'fyp'}).sort_values('fyp')
    m = pd.merge_asof(out.sort_values('period'), fy.rename(columns={n + '_ytd': n + '_fy' for n in FLOW}),
                      left_on='period', right_on='fyp', by='cik', allow_exact_matches=False)
    for n in FLOW:
        q = m[n + '_fy'] + m[n + '_ytd'] - m[n + '_ytdp']
        recent = (m.period - m.fyp).dt.days <= 290
        m[n] = np.where(m.fp == 'FY', m[n + '_ytd'], np.where(recent, q, np.nan))
    F = m[['adsh', 'cik', 'sic', 'form', 'period', 'fp', 'filed'] + list(FLOW) + list(STOCK) + ['wsh']].copy()
    F['shares_b'] = F.shares.where(F.shares > 0).fillna(F.wsh).fillna(F.ni / F.eps.where(F.eps.abs() > 0.01))
    F['debt'] = F[['ltd_nc', 'ltd_c', 'stb']].fillna(0).sum(axis=1)
    F['debt'] = np.where(F[['ltd_nc', 'ltd_c', 'stb']].isna().all(axis=1), F.ltd.fillna(0), F.debt)
    F['liab2'] = F.liab.fillna(F.lse - F.eqnci.fillna(F.equity))
    nodebt = F[['ltd_nc', 'ltd_c', 'stb', 'ltd']].isna().all(axis=1)
    F.loc[nodebt, 'debt'] = (F.liab2 - F.cl).where(lambda s: s > 0)
    F['ebit2'] = F.ebit.fillna(F.pretax + F.intexp.fillna(0))
    F['ebitda'] = F.ebit2 + F.da.fillna(0)
    F = F.sort_values('period')

    def lag(years, cols):
        L = F[['cik', 'period'] + cols].copy(); L['period_l'] = L.period; L = L.drop(columns='period')
        L = L.rename(columns={c: c + f'_l{years}' for c in cols}).sort_values('period_l')
        tgt = F[['cik', 'period']].copy(); tgt['key'] = tgt.period - pd.DateOffset(years=years) + pd.Timedelta(days=20)
        r = pd.merge_asof(tgt.sort_values('key'), L, left_on='key', right_on='period_l', by='cik', direction='backward', tolerance=pd.Timedelta(days=60))
        return r.drop(columns=['key', 'period_l'])
    F = F.merge(lag(1, ['assets']), on=['cik', 'period'], how='left')
    return F.sort_values('filed')


def load_prices(sp500, tickers, start):
    con = duckdb.connect()
    con.register('tk', pd.DataFrame({'ticker': list(tickers)}))
    p = os.path.join(sp500, 'prices.parquet')
    d = con.sql(f"select p.date,p.ticker,p.close,p.adj_close,p.stock_splits,p.source from '{p}' p join tk using(ticker) where p.date>='{start}'").df()
    d['date'] = pd.to_datetime(d.date)
    d['sp'] = d.stock_splits.where(d.stock_splits > 0, 1.0)
    d = d.sort_values(['ticker', 'date'])
    d['cum'] = d.groupby('ticker').sp.cumprod()
    last = d.groupby('ticker').cum.transform('last')
    d['raw'] = np.where(d.source == 'yahoo', d.close * last / d.cum, d.close)
    return (d.pivot(index='date', columns='ticker', values='adj_close'), d.pivot(index='date', columns='ticker', values='raw'),
            d.pivot(index='date', columns='ticker', values='cum'))


def auswahl(a):
    stichtag = pd.Timestamp(a.stichtag)
    mem = pd.read_parquet(os.path.join(a.sp500, 'membership_intervals.parquet'))
    mem['end'] = mem.end.fillna(pd.Timestamp('2100-01-01'))
    F = fund_panel(a.stx)
    cm = pd.read_csv(os.path.join(a.stx, 'data/value/cik_ext.csv'), parse_dates=['von', 'bis'])
    adj, raw, cum = load_prices(a.sp500, list(mem[mem.end > stichtag - pd.Timedelta(days=800)].ticker.unique()) + ['SPY'],
                                (stichtag - pd.Timedelta(days=900)).date())
    td = raw.index
    t_eff = td[td <= stichtag][-1]
    members = mem[(mem.start <= t_eff) & (mem.end > t_eff)].ticker.values
    rows, nocik = [], []
    for tk in members:
        c = cm[cm.label == tk]
        if c.empty:
            nocik.append(tk); continue
        cc = c[(c.von <= t_eff + pd.Timedelta(days=200)) & (c.bis >= t_eff - pd.Timedelta(days=400))]
        rows.append((t_eff, tk, int((cc if not cc.empty else c).iloc[-1].cik)))
    U = pd.DataFrame(rows, columns=['date', 'ticker', 'cik'])
    X = pd.merge_asof(U.sort_values('date'), F.rename(columns={'filed': 'filed_f'}), left_on='date', right_on='filed_f', by='cik',
                      allow_exact_matches=False, direction='backward', tolerance=pd.Timedelta(days=200))
    X['raw'] = [raw.at[t_eff, t] if t in raw.columns else np.nan for t in X.ticker]
    X['cumT'] = [cum.at[t_eff, t] if t in cum.columns else np.nan for t in X.ticker]
    cp = []
    for d, t in zip(X.period, X.ticker):
        if pd.isna(d) or t not in cum.columns: cp.append(np.nan); continue
        s = cum[t].loc[:d].dropna(); cp.append(s.iloc[-1] if len(s) else np.nan)
    X['split_adj'] = X.cumT / np.array(cp, dtype=float)
    X['imp'] = (X.ni / X.eps.where(X.eps.abs() > 0.05)).where(lambda s: s > 0)
    c = X[['shares', 'wsh', 'imp']].where(lambda d: d > 0)

    def choose(r):
        v = [r.shares, r.wsh, r.imp]
        for i, x in enumerate(v):
            if pd.isna(x): continue
            for j, y in enumerate(v):
                if i != j and not pd.isna(y) and 0.8 < x / y < 1.25: return x
        vals = [x for x in v if not pd.isna(x)]
        return vals[0] if len(vals) == 1 else np.nan
    X['sh'] = c.apply(choose, axis=1)
    for col in ['shares', 'wsh', 'imp']:
        mc = X[col] * X.split_adj * X.raw
        ok = X.sh.isna() & mc.between(3e8, 8e12)
        X.loc[ok, 'sh'] = X.loc[ok, col]
    X['mcap'] = X.sh * X.split_adj * X.raw
    X.loc[(X.mcap < 3e8) | (X.mcap > 8e12), 'mcap'] = np.nan
    X['ev'] = X.mcap + X.debt - X.cash.fillna(0) - X.sti.fillna(0)
    X['sic'] = pd.to_numeric(X.sic, errors='coerce')
    X = X[X.mcap.notna() & ~X.sic.between(6000, 6799)].copy()
    X['ic'] = X.equity + X.debt - X.cash.fillna(0) - X.sti.fillna(0)
    X['ey'] = np.where(X.ev > 0, X.ebit2 / X.ev, np.nan)
    X['roic'] = np.where(X.ic > 0, X.ebit2 / X.ic, np.nan)
    X['pe'] = np.where(X.ni > 0, X.mcap / X.ni, np.nan)
    X['dy'] = X['div'].fillna(0).abs() / X.mcap
    X['shy'] = (X['div'].fillna(0).abs() + X.buyb.fillna(0).abs() - X.issue.fillna(0).abs()) / X.mcap
    X['assetg'] = X.assets / X.assets_l1 - 1
    X['de'] = np.where(X.ebitda > 0, X.debt / X.ebitda, np.inf)
    X = X[~((X.ey > 0.4) | ((X.pe < 3) & X.pe.notna()))]
    u = X.set_index('ticker')
    d = u[(u.ebit2 > 0) & (u.ocf > 0) & (u.de < 3) & u.ey.notna() & u.roic.notna() & u.assetg.notna()]
    d = d[d.assetg < u.assetg.quantile(0.8)]
    sc = ((d.ey.rank(pct=True) + d.roic.rank(pct=True) + d.shy.rank(pct=True) + (-d.assetg).rank(pct=True)) / 4).sort_values(ascending=False)
    bestand = [t.strip() for t in (a.bestand or '').split(',') if t.strip()]
    keep = [t for t in bestand if t in sc.index[:BUF * N]]
    neu = [t for t in sc.index if t not in keep][:N - len(keep)]
    pick = keep + neu
    rank = {t: i + 1 for i, t in enumerate(sc.index)}
    res = {'stichtag': str(stichtag.date()), 'kursdatum': str(t_eff.date()),
           'universum': int(len(members)), 'ohne_cik': nocik, 'mit_kennzahlen': int(len(u)), 'nach_filtern': int(len(d)),
           'abdeckung': round(len(u) / max(1, len(members)), 3),
           'halten': keep, 'verkaufen': [t for t in bestand if t not in keep], 'kaufen': neu, 'neuer_bestand': pick,
           'rang_bestand': {t: rank.get(t) for t in bestand},
           'top40': [{'rang': i + 1, 'ticker': t, 'score': round(float(sc[t]), 4), 'ebit_ev': round(float(d.at[t, 'ey']), 4),
                      'roic': round(float(d.at[t, 'roic']), 4), 'ausschuettung': round(float(d.at[t, 'shy']), 4),
                      'verm_wachstum': round(float(d.at[t, 'assetg']), 4), 'kurs_usd': round(float(raw.at[t_eff, t]), 4)}
                     for i, t in enumerate(sc.index[:40])],
           'kurse_usd': {t: (round(float(raw.at[t_eff, t]), 4) if t in raw.columns and not pd.isna(raw.at[t_eff, t]) else None) for t in set(pick) | set(bestand)}}
    js = json.dumps(res, ensure_ascii=False, indent=1)
    if a.out: open(a.out, 'w').write(js)
    print(js)


def wert(a):
    bestand = [t.strip() for t in a.bestand.split(',') if t.strip()]
    adj, raw, _ = load_prices(a.sp500, bestand + ['SPY'], (pd.Timestamp(a.von) - pd.Timedelta(days=10)).date())
    v0, v1 = pd.Timestamp(a.von), pd.Timestamp(a.bis)
    seg = adj.loc[:v1]
    seg = seg.loc[seg.index[seg.index <= v0][-1]:]
    start, end = seg.iloc[0], seg.ffill().iloc[-1]
    r = (end / start - 1)
    out = {'von': str(seg.index[0].date()), 'bis': str(seg.index[-1].date()),
           'titel': {t: round(float(r[t]), 4) if t in r and not pd.isna(r[t]) else None for t in bestand},
           'depot_gleichgewichtet': round(float(r[bestand].mean()), 4), 'spy': round(float(r['SPY']), 4),
           'ohne_kurs_am_ende': [t for t in bestand if t in adj.columns and pd.isna(adj[t].loc[:v1].iloc[-1])]}
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    sp = p.add_subparsers(dest='cmd', required=True)
    s1 = sp.add_parser('auswahl'); s1.add_argument('--stx', required=True); s1.add_argument('--sp500', required=True)
    s1.add_argument('--stichtag', required=True); s1.add_argument('--bestand', default=''); s1.add_argument('--out')
    s2 = sp.add_parser('wert'); s2.add_argument('--sp500', required=True); s2.add_argument('--von', required=True)
    s2.add_argument('--bis', required=True); s2.add_argument('--bestand', required=True)
    a = p.parse_args()
    auswahl(a) if a.cmd == 'auswahl' else wert(a)

# Squeeze-out-Monitor: erkennt Strukturmaßnahmen (Squeeze-out, Beherrschungsvertrag, Delisting,
# Übernahme) aus Nachrichten, verfolgt den Kurs gegen Abfindung/Angebot und meldet günstige Fälle.
# Fälle stehen in data/sonder/faelle.csv. Spalten betrag, ticker, status dürfen von Hand
# gepflegt werden und haben Vorrang vor den automatisch erkannten Werten.
import os, re, csv, time, json, datetime, urllib.parse
import xml.etree.ElementTree as ET
import requests

SCHWELLE = 0.02        # Signal, wenn Kurs höchstens 2 % über Abfindung/Angebot liegt
UNTERGRENZE = -0.10    # deutlich darunter: eher Erkennungsfehler -> "prüfen"
ORDNER = 'data/sonder'
FAELLE = ORDNER + '/faelle.csv'
HANDPFLEGE = 'routinen/handpflege.csv'   # key,firma,typ,betrag,ticker,status,notiz – hat Vorrang
BERICHT = 'reports/sondersituationen.md'
SUCHEN = [
    'Squeeze-out Barabfindung',
    'Squeeze-out Abfindung Aktie',
    'verschmelzungsrechtlicher Squeeze-out',
    'Beherrschungs- und Gewinnabführungsvertrag Abfindung',
    'Beherrschungsvertrag Barabfindung',
    'Delisting Erwerbsangebot je Aktie',
    'Übernahmeangebot je Aktie Annahmefrist',
]
SPALTEN = ['key', 'firma', 'typ', 'betrag_auto', 'betrag', 'ticker_auto', 'yahoo_name', 'ticker', 'status',
           'erst_gesehen', 'zuletzt_gesehen', 'n_meldungen', 'quelle', 'link',
           'kurs', 'kurs_datum', 'aufschlag_pct', 'signal', 'gemeldet', 'nachgesucht']
BOERSEN = ['.DE', '.F', '.MU', '.SG', '.DU', '.HM', '.BE', '.HA']   # nur deutsche Handelsplätze
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) personal-monitor'}
TYPNAME = {'SO': 'Squeeze-out', 'BGAV': 'Beherrschungsvertrag', 'DEL': 'Delisting', 'UEB': 'Übernahmeangebot'}
RANG = {'SO': 4, 'BGAV': 3, 'DEL': 2, 'UEB': 1}

# ---------- Erkennung ----------
def typ_von(text):
    t = text.lower()
    if 'squeeze' in t:
        return 'SO'
    if 'beherrschungs' in t or 'gewinnabführungs' in t or 'gewinnabfuehrungs' in t:
        return 'BGAV'
    if 'delisting' in t:
        return 'DEL'
    if 'übernahmeangebot' in t or 'uebernahmeangebot' in t or 'erwerbsangebot' in t:
        return 'UEB'
    if 'abfindung' in t and ('aktie' in t or 'aktionär' in t):
        return 'SO'   # Abfindung für Aktionäre ohne nähere Angabe: wie Squeeze-out behandeln, Typ im Bericht prüfen
    return None

SUFFIX = r'(?:AG|SE|KGaA|AG & Co\. KGaA|SE & Co\. KGaA|GmbH & Co\. KGaA)'
RX_FIRMA = [
    re.compile(r'^(?:EQS-[\w-]+:\s*)?([A-ZÄÖÜ0-9][^:]{1,80}?' + SUFFIX + r')\s*:'),
    re.compile(r'^([A-ZÄÖÜ0-9][\w.&\- ]{1,50}?)[ -]Aktie\b'),
    re.compile(r'\b(?:der|bei der|bei|an der|für die|die)\s+([A-ZÄÖÜ0-9][\w.&\-]*(?:\s+[A-ZÄÖÜ0-9][\w.&\-]*){0,5}\s+' + SUFFIX + r')(?=[\s,.;:)]|$)'),
    re.compile(r'([A-ZÄÖÜ][\w.&\-]*(?:\s+[A-ZÄÖÜ0-9][\w.&\-]*){0,4}\s+' + SUFFIX + r')(?=[\s,.;:)]|$)'),
]
STOP = {'die', 'der', 'das', 'aktie', 'aktien', 'squeeze-out', 'hauptversammlung'}

def firma_von(text):
    for rx in RX_FIRMA:
        m = rx.search(text)
        if m:
            f = m.group(1).strip(' -–,')
            if f.lower() not in STOP and len(f) >= 3:
                return f
    return None

def schluessel(firma):
    k = firma.lower()
    k = re.sub(r'\b(ag|se|kgaa|gmbh|co|n\.v|s\.a)\b\.?', ' ', k)
    k = re.sub(r'[^a-z0-9äöüß]+', ' ', k)
    return ' '.join(k.split())

RX_BETRAG = re.compile(r'(?:EUR|€)\s*(\d{1,5}(?:\.\d{3})*,\d{1,2})|(\d{1,5}(?:\.\d{3})*,\d{1,2})\s*(?:EUR|Euro|€)')
SCHLUESSELWORTE = ('abfindung', 'angebot', 'gegenleistung', 'je aktie', 'pro aktie', 'kaufpreis')

def betrag_von(text):
    for m in RX_BETRAG.finditer(text):
        roh = m.group(1) or m.group(2)
        davor = text[max(0, m.start() - 45):m.start()].lower()
        danach = text[m.end():m.end() + 20].lower()
        letztes = max((davor.rfind(w), w) for w in SCHLUESSELWORTE + ('ausgleich',))
        if letztes[0] >= 0 and letztes[1] == 'ausgleich':
            continue
        if letztes[0] >= 0 or 'je aktie' in danach or 'pro aktie' in danach:
            return float(roh.replace('.', '').replace(',', '.'))
    return None

# ---------- Quellen ----------
def news(suche, tage):
    q = urllib.parse.quote(f'{suche} when:{tage}d')
    url = f'https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de'
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return parse_rss(r.content)

def parse_rss(xml_bytes):
    out = []
    root = ET.fromstring(xml_bytes)
    for it in root.iter('item'):
        titel = (it.findtext('title') or '').strip()
        quelle = (it.findtext('source') or '').strip()
        if quelle and titel.endswith(' - ' + quelle):
            titel = titel[: -len(' - ' + quelle)]
        datum = ''
        try:
            datum = datetime.datetime.strptime(it.findtext('pubDate') or '', '%a, %d %b %Y %H:%M:%S %Z').date().isoformat()
        except ValueError:
            pass
        out.append({'titel': titel, 'quelle': quelle, 'link': (it.findtext('link') or '').strip(), 'datum': datum})
    return out

def ereignis(item):
    typ = typ_von(item['titel'])
    if not typ:
        return None
    firma = firma_von(item['titel'])
    if not firma:
        return None
    return {'typ': typ, 'firma': firma, 'key': schluessel(firma), 'betrag': betrag_von(item['titel']),
            'datum': item['datum'], 'quelle': item['quelle'], 'link': item['link']}

def norm(s):
    s = s.lower()
    for a, b in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('ß', 'ss')):
        s = s.replace(a, b)
    return re.sub(r'[^a-z0-9]+', '', s)

def yahoo_suche(name):
    such = re.sub(r'[\s-]*\b(AG|SE|KGaA)\b.*$', '', name).strip() or name
    quotes = []
    umschrift = such
    for a, b in (('ä', 'ae'), ('ö', 'oe'), ('ü', 'ue'), ('Ä', 'Ae'), ('Ö', 'Oe'), ('Ü', 'Ue'), ('ß', 'ss')):
        umschrift = umschrift.replace(a, b)
    for q in dict.fromkeys([such, umschrift]):
        try:
            r = requests.get('https://query2.finance.yahoo.com/v1/finance/search',
                             params={'q': q, 'quotesCount': 10, 'newsCount': 0, 'lang': 'de-DE', 'region': 'DE'},
                             headers=UA, timeout=30)
            quotes += r.json().get('quotes', [])
        except Exception as e:
            print('yahoo search', q, e)
    if not quotes:
        try:
            import yfinance as yf
            quotes = yf.Search(such, max_results=10, news_count=0).quotes
        except Exception as e:
            print('yf.Search', name, e)
    rang = BOERSEN
    erstes_wort = norm(name.split()[0])
    beste = None
    for q in quotes:
        if q.get('quoteType') != 'EQUITY':
            continue
        sym = q.get('symbol', '')
        nm = (q.get('shortname') or q.get('longname') or '')
        if erstes_wort[:4] not in norm(nm):
            continue
        suf = '.' + sym.split('.')[-1] if '.' in sym else ''
        if suf not in rang:
            continue
        r_ = rang.index(suf)
        if beste is None or r_ < beste[0]:
            beste = (r_, sym, ' '.join(nm.split()))
    return (beste[1], beste[2]) if beste else (None, None)

def kurs(sym):
    import yfinance as yf
    h = yf.Ticker(sym).history(period='15d', auto_adjust=False)
    h = h['Close'].dropna() if len(h) else h
    if not len(h):
        return None, None
    return float(h.iloc[-1]), h.index[-1].date().isoformat()

def betrag_nachsuchen(f):
    """Gezielte Suche nach dem Betrag, wenn die erste Meldung keinen enthielt."""
    such = re.sub(r'[\s-]*\b(AG|SE|KGaA)\b.*$', '', f['firma']).strip() or f['firma']
    kern = norm(such)[:8]
    treffer = []
    for q in (f'"{such}" Abfindung', f'"{such}" Angebot je Aktie'):
        try:
            items = news(q, 365)
        except Exception as ex:
            print('Nachsuche', q, ex)
            continue
        for it in items:
            t = it['titel']
            tl = t.lower()
            if kern not in norm(t):
                continue
            if f['typ'] in ('SO', 'BGAV') and 'abfindung' not in tl:
                continue
            if f['typ'] in ('DEL', 'UEB') and not ('angebot' in tl or 'abfindung' in tl):
                continue
            b = betrag_von(t)
            if b:
                treffer.append((it['datum'], b, it))
        time.sleep(2)
    if not treffer:
        return None
    datum, b, it = max(treffer, key=lambda x: x[0])
    return b, it

# ---------- Ablage ----------
def lade():
    if not os.path.exists(FAELLE):
        return {}
    with open(FAELLE, newline='', encoding='utf-8') as f:
        return {r['key']: {s: r.get(s, '') for s in SPALTEN} for r in csv.DictReader(f)}

def speichere(faelle):
    os.makedirs(ORDNER, exist_ok=True)
    with open(FAELLE, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=SPALTEN)
        w.writeheader()
        for k in sorted(faelle, key=lambda k: faelle[k]['zuletzt_gesehen'], reverse=True):
            w.writerow(faelle[k])

def handpflege(faelle, heute):
    if not os.path.exists(HANDPFLEGE):
        return
    with open(HANDPFLEGE, newline='', encoding='utf-8') as fh:
        for r in csv.DictReader(fh):
            k = (r.get('key') or '').strip()
            if not k:
                continue
            f = faelle.get(k)
            if f is None:
                f = {s: '' for s in SPALTEN}
                f.update(key=k, firma=r.get('firma') or k, typ=r.get('typ') or 'SO', status='aktiv',
                         erst_gesehen=heute, zuletzt_gesehen=heute, n_meldungen='0')
                faelle[k] = f
            for feld in ('betrag', 'ticker', 'status'):
                if (r.get(feld) or '').strip():
                    f[feld] = r[feld].strip()
            if (r.get('typ') or '').strip():
                f['typ'] = r['typ'].strip()

def einarbeiten(faelle, ereignisse, heute, gesehen):
    neu = []
    for e in sorted(ereignisse, key=lambda e: e['datum'] or ''):
        if e['link'] in gesehen:
            continue
        gesehen.add(e['link'])
        f = faelle.get(e['key'])
        if f is None:
            f = {s: '' for s in SPALTEN}
            f.update(key=e['key'], firma=e['firma'], typ=e['typ'], erst_gesehen=e['datum'] or heute,
                     n_meldungen='0', status='aktiv')
            faelle[e['key']] = f
            neu.append(e['key'])
        if RANG.get(e['typ'], 0) >= RANG.get(f['typ'], 0):
            f['typ'] = e['typ']
            if e['betrag']:
                f['betrag_auto'] = f'{e["betrag"]:.2f}'
        elif e['betrag'] and not f['betrag_auto']:
            f['betrag_auto'] = f'{e["betrag"]:.2f}'
        f['zuletzt_gesehen'] = max(f['zuletzt_gesehen'] or '', e['datum'] or heute)
        f['n_meldungen'] = str(int(f['n_meldungen'] or 0) + 1)
        f['quelle'], f['link'] = e['quelle'], e['link']
    return neu

def zahl(s):
    try:
        return float(str(s).replace(',', '.'))
    except ValueError:
        return None

def bewerten(f):
    if f['status'] in ('erledigt', 'ignoriert'):
        return ''
    b = zahl(f['betrag']) or zahl(f['betrag_auto'])
    k = zahl(f['kurs'])
    if not (f['ticker'] or f['ticker_auto']):
        f['aufschlag_pct'] = ''
        return 'ohne Ticker'
    if not b or not k:
        f['aufschlag_pct'] = ''
        return 'Vorstufe' if f['typ'] == 'UEB' else 'Daten fehlen'
    a = k / b - 1
    f['aufschlag_pct'] = f'{a * 100:.2f}'
    if f['typ'] == 'UEB':
        return 'Vorstufe'
    if a < UNTERGRENZE:
        return 'PRÜFEN'
    if a <= SCHWELLE:
        return 'GÜNSTIG'
    return 'beobachten'

# ---------- Ausgabe ----------
def issue(titel, text):
    tok, repo = os.environ.get('GITHUB_TOKEN'), os.environ.get('GITHUB_REPOSITORY')
    if not tok or not repo:
        print('kein Token - Meldung nur im Log:', titel)
        return
    r = requests.post(f'https://api.github.com/repos/{repo}/issues', timeout=30,
                      headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json'},
                      json={'title': titel, 'body': text})
    print('Issue', r.status_code, r.text[:200] if r.status_code >= 300 else '')

def zeile(f):
    b = f['betrag'] or f['betrag_auto'] or '–'
    t = f['ticker'] or f['ticker_auto'] or '–'
    hand = ' (Hand)' if f['betrag'] else ''
    quelle = f'[{f["quelle"] or "Quelle"}]({f["link"]})' if f['link'] else ''
    return (f'| {f["firma"]} | {TYPNAME.get(f["typ"], f["typ"])} | {b}{hand} | {t} | {f["kurs"] or "–"} '
            f'| {f["aufschlag_pct"] or "–"} | {f["signal"]} | {f["zuletzt_gesehen"]} | {quelle} |')

def bericht(faelle, stand):
    kopf = ('| Firma | Typ | Abfindung/Angebot € | Ticker | Kurs € | Aufschlag % | Signal | zuletzt | Quelle |\n'
            '|---|---|---|---|---|---|---|---|---|')
    alle = [f for f in faelle.values() if f['status'] not in ('erledigt', 'ignoriert')]
    g = lambda *s: [f for f in alle if f['signal'] in s]
    teile = [f'# Sondersituationen – Stand {stand}', '',
             f'Signal **GÜNSTIG**: Kurs höchstens {SCHWELLE * 100:.0f} % über Abfindung/Angebot. '
             'Betrag und Ticker sind automatisch erkannt und vor jedem Kauf an der Originalmeldung zu prüfen.', '']
    for titel, liste in [('Günstig', g('GÜNSTIG')), ('Prüfen (Kurs deutlich unter Betrag – Erkennung fraglich)', g('PRÜFEN')),
                         ('Beobachten', g('beobachten')), ('Daten fehlen (Betrag oder Kurs)', g('Daten fehlen')),
                         ('Übernahmeangebote (mögliche Vorstufe)', g('Vorstufe')),
                         ('Ohne deutsche Börsennotierung gefunden', g('ohne Ticker'))]:
        teile += [f'## {titel} ({len(liste)})', '']
        if liste:
            teile += [kopf] + [zeile(f) for f in sorted(liste, key=lambda f: f['zuletzt_gesehen'], reverse=True)]
        teile.append('')
    teile += ['## Pflege', '',
              'Korrekturen und eigene Fälle gehören in `routinen/handpflege.csv` (Spalten key, firma, typ, betrag, '
              'ticker, status, notiz); sie haben Vorrang vor der automatischen Erkennung. `key` = Firmenname klein '
              'ohne Rechtsform (wie in `data/sonder/faelle.csv`). typ: SO, BGAV, DEL, UEB; status: aktiv, erledigt, ignoriert.']
    os.makedirs(os.path.dirname(BERICHT), exist_ok=True)
    open(BERICHT, 'w', encoding='utf-8').write('\n'.join(teile) + '\n')

def main():
    heute = datetime.date.today().isoformat()
    faelle = lade()
    tage = 14 if faelle else 120
    ereignisse, log = [], []
    for s in SUCHEN:
        try:
            items = news(s, tage)
            ev = [e for e in map(ereignis, items) if e]
            ereignisse += ev
            log.append(f'{s}: {len(items)} Meldungen, {len(ev)} erkannt')
        except Exception as ex:
            log.append(f'{s}: FEHLER {ex}')
        time.sleep(2)
    gf = ORDNER + '/_gesehen.txt'
    gesehen = set(open(gf).read().split()) if os.path.exists(gf) else set()
    neu = einarbeiten(faelle, ereignisse, heute, gesehen)
    handpflege(faelle, heute)
    os.makedirs(ORDNER, exist_ok=True)
    open(gf, 'w').write('\n'.join(sorted(gesehen)[-5000:]) + '\n')
    nachsuchen_heute = 0
    vor_7_tagen = (datetime.date.today() - datetime.timedelta(days=7)).isoformat()
    for f in faelle.values():
        if f['status'] in ('erledigt', 'ignoriert'):
            f['signal'] = ''
            continue
        ta = f['ticker_auto']
        if ta and ('.' + ta.split('.')[-1] if '.' in ta else '') not in BOERSEN:
            f['ticker_auto'] = f['yahoo_name'] = ''   # ausländische Notierung verwerfen
        if not f['ticker'] and not f['ticker_auto'] and f['nachgesucht'] != heute:
            f['ticker_auto'], f['yahoo_name'] = [x or '' for x in yahoo_suche(f['firma'])]
            time.sleep(1)
        f['yahoo_name'] = ' '.join(f['yahoo_name'].split())
        if (not f['betrag'] and not f['betrag_auto'] and (f['ticker'] or f['ticker_auto'])
                and nachsuchen_heute < 12 and (f['nachgesucht'] or '0') <= vor_7_tagen):
            nachsuchen_heute += 1
            f['nachgesucht'] = heute
            gefunden = betrag_nachsuchen(f)
            if gefunden:
                b, it = gefunden
                f['betrag_auto'] = f'{b:.2f}'
                f['quelle'], f['link'] = it['quelle'], it['link']
                log.append(f'Nachsuche {f["firma"]}: {b:.2f} € ({it["datum"]})')
            else:
                log.append(f'Nachsuche {f["firma"]}: kein Betrag gefunden')
        sym = f['ticker'] or f['ticker_auto']
        f['kurs'] = f['kurs_datum'] = ''
        if sym:
            try:
                k, d = kurs(sym)
                if k:
                    f['kurs'], f['kurs_datum'] = f'{k:.2f}', d
            except Exception as ex:
                log.append(f'Kurs {sym}: {ex}')
        f['signal'] = bewerten(f)
    meldungen = []
    for k in neu:
        f = faelle[k]
        if f['typ'] in ('SO', 'BGAV', 'DEL'):
            meldungen.append(f'- NEU: **{f["firma"]}** – {TYPNAME[f["typ"]]}, Betrag {f["betrag_auto"] or "?"} €, '
                             f'Ticker {f["ticker_auto"] or "?"}, Aufschlag {f["aufschlag_pct"] or "?"} % ({f["link"]})')
    for f in faelle.values():
        if f['signal'] == 'GÜNSTIG' and f['gemeldet'] != 'GÜNSTIG':
            meldungen.append(f'- GÜNSTIG: **{f["firma"]}** – Kurs {f["kurs"]} € vs. {f["betrag"] or f["betrag_auto"]} € '
                             f'({f["aufschlag_pct"]} %), Ticker {f["ticker"] or f["ticker_auto"]}')
        f['gemeldet'] = f['signal']
    speichere(faelle)
    bericht(faelle, heute)
    open(ORDNER + '/_log.txt', 'w').write('\n'.join(log) + f'\nFälle: {len(faelle)}, neu: {len(neu)}\n')
    print('\n'.join(log))
    erster_lauf = tage > 14
    if meldungen and not erster_lauf:
        issue(f'Sondersituationen {heute}: {len(meldungen)} Meldung(en)',
              '\n'.join(meldungen) + '\n\nBericht: reports/sondersituationen.md')
    elif erster_lauf:
        issue(f'Sondersituationen: erster Lauf {heute}',
              f'{len(faelle)} Fälle erfasst, davon {sum(f["signal"] == "GÜNSTIG" for f in faelle.values())} mit Signal GÜNSTIG.\n\n'
              'Bericht: reports/sondersituationen.md')

if __name__ == '__main__':
    main()

# Zwangsversteigerungs-Vorfilter: liest die Terminliste des ZVG-Portals (Bayern), filtert die
# Zielregionen nach Postleitzahl und meldet neue Termine. Detailseiten und Gutachten werden
# nicht automatisch abgerufen (robots.txt des Portals) - der Bericht verlinkt sie zum Öffnen von Hand.
import os, re, csv, gzip, time, datetime, urllib.parse
import requests
from bs4 import BeautifulSoup

BASE = 'https://www.zvg-portal.de/'
LAND = 'Bayern'
LAND_ABK = 'by'
REGIONEN = {   # Postleitzahl-Anfänge je Region; frei anpassbar
    'Fünfseenland': ['8213', '8215', '8220', '8221', '8222', '8223', '8228', '8231', '8232', '8233',
                     '8234', '8239', '8691', '8692', '8693', '8694'],
    'Regensburg': ['930', '931'],
}
OHNE = ['garage', 'stellplatz', 'keller', 'teileigentum']   # reine Nebenobjekte ausblenden
ORDNER = 'data/zvg'
DATEI = ORDNER + '/termine.csv'
BERICHT = 'reports/zwangsversteigerungen.md'
SPALTEN = ['id', 'zvg_id', 'az', 'gericht', 'region', 'objekt', 'adresse', 'plz', 'ort', 'verkehrswert',
           'wert_text', 'termin', 'aufgehoben', 'wertgrenzen_weg', 'aktualisiert', 'erst_gesehen', 'zuletzt_gesehen']
UA = {'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) personal-monitor'}
MONATE = {m: i + 1 for i, m in enumerate(['januar', 'februar', 'märz', 'april', 'mai', 'juni', 'juli', 'august',
                                           'september', 'oktober', 'november', 'dezember'])}

# ---------- Abruf ----------
def formular_daten(html, wertgrenzen):
    soup = suppe(html)
    form = None
    for f in soup.find_all('form'):
        if any(o.get_text(strip=True) == LAND for o in f.find_all('option')):
            form = f
            break
    if form is None:
        raise RuntimeError('Suchformular nicht gefunden')
    daten = {}
    for el in form.find_all(['input', 'select', 'textarea', 'button']):
        name = el.get('name')
        if not name:
            continue
        if el.name == 'select':
            if el.has_attr('multiple'):
                continue
            opts = el.find_all('option')
            land = [o for o in opts if o.get_text(strip=True) == LAND]
            if land:
                daten[name] = land[0].get('value', LAND_ABK)
            else:
                sel = [o for o in opts if o.has_attr('selected')] or opts[:1]
                daten[name] = sel[0].get('value', '') if sel else ''
            continue
        typ = (el.get('type') or ('submit' if el.name == 'button' else 'text')).lower()
        if typ in ('checkbox', 'radio'):
            umfeld = el.parent.get_text(' ', strip=True) if el.parent else ''
            if wertgrenzen and 'Wertgrenze' in umfeld:
                daten[name] = el.get('value', 'on')
            continue
        if typ in ('submit', 'button', 'image', 'reset'):
            wert = el.get('value') or el.get_text(strip=True)
            if 'such' in (wert or '').lower() and 'zurück' not in (wert or '').lower():
                daten[name] = wert
            continue
        daten[name] = el.get('value', '')
    aktion = urllib.parse.urljoin(BASE, form.get('action') or 'index.php?button=Suchen')
    return aktion, daten

def abrufen(wertgrenzen=False):
    """Liefert (html, soll, protokoll, kandidaten). Das Portal liefert die Gesamtliste, wenn dieselben
    Suchfelder erneut per POST an index.php?button=Suchen&all=1 gehen (so wie die Auswahl 'alle')."""
    s = requests.Session()
    s.headers.update(UA)
    r = s.get(BASE + 'index.php?button=Termine%20suchen', timeout=60)
    r.raise_for_status()
    aktion, daten = formular_daten(r.text, wertgrenzen)
    r2 = s.post(aktion, data=daten, timeout=90)
    r2.raise_for_status()
    soll = gesamtzahl(r2.text)
    kandidaten = [('POST erste Seite', r2.text)]
    try:
        kandidaten.append(('POST all=1', s.post(BASE + 'index.php?button=Suchen&all=1', data=daten, timeout=180).text))
    except Exception as ex:
        kandidaten.append((f'POST all=1 Fehler {ex}', ''))
    protokoll = [f'Felder {sorted(daten)}', f'Soll laut Portal: {soll}']
    beste = None
    for name, h in kandidaten:
        n = len(parse(h)) if h else 0
        protokoll.append(f'{name[:160]}: {n} Einträge')
        if beste is None or n > beste[0]:
            beste = (n, h)
    if soll and beste[0] < soll:   # Rückfall: seitenweise blättern wie die Seitenknöpfe des Portals
        teile = [beste[1]]
        for seite in range(2, (soll + 9) // 10 + 1):
            l = (seite - 1) // 10 * 10 + 1
            url = f'{BASE}index.php?button=Suchen&seite={seite}&l={l}&r={l + 10}&all=&order_by=-1&desc=-1'
            try:
                teile.append(s.post(url, data=daten, timeout=90).text)
            except Exception as ex:
                protokoll.append(f'Seite {seite}: {ex}')
                break
            time.sleep(1)
        gesamt = '\n'.join(teile)
        n = len(parse(gesamt))
        protokoll.append(f'Blättern {len(teile)} Seiten: {n} Einträge')
        kandidaten.append(('Blättern', gesamt))
        if n > beste[0]:
            beste = (n, gesamt)
    return beste[1], soll, protokoll, kandidaten

# ---------- Auswertung ----------
def suppe(html):
    for p in ('html5lib', 'lxml', 'html.parser'):
        try:
            return BeautifulSoup(html, p)
        except Exception:
            continue

def text(el):
    return re.sub(r'\s+', ' ', el.get_text(' ', strip=True)).strip()

def gesamtzahl(html):
    m = re.search(r'Insgesamt\s+(\d+)', text(suppe(html)))
    return int(m.group(1)) if m else None

def parse(html):
    soup = suppe(html)
    eintraege, cur = [], None
    for tr in soup.find_all('tr'):
        zellen = tr.find_all(['td', 'th'], recursive=False)
        if len(zellen) < 2:
            continue
        label = text(zellen[0])
        if len(label) > 25:
            continue
        wert = text(zellen[1])
        if label == 'Aktenzeichen':
            cur = {'az': re.sub(r'\s*\(Detailansicht\)', '', wert).strip(), 'zvg_id': ''}
            a = zellen[1].find('a', href=re.compile(r'zvg_id=\d+'))
            if a:
                cur['zvg_id'] = re.search(r'zvg_id=(\d+)', a['href']).group(1)
            akt = re.search(r'(\d{2})-(\d{2})-(\d{4})', ' '.join(text(z) for z in zellen[2:]))
            cur['aktualisiert'] = f'{akt.group(3)}-{akt.group(2)}-{akt.group(1)}' if akt else ''
            eintraege.append(cur)
        elif cur is None:
            continue
        elif label == 'Amtsgericht':
            cur['gericht'] = re.sub(r'\s*in Bayern$', '', wert).strip()   # Portal liefert den Namen nicht immer
        elif label.startswith('Objekt'):
            cur['objekt_lage'] = wert
        elif label.startswith('Verkehrswert'):
            cur['wert_text'] = wert
        elif label == 'Termin':
            cur['termin_text'] = wert
    return [aufbereiten(e) for e in eintraege]

RX_EURO = re.compile(r'\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+,\d{1,2}')

def eur(s):
    return float(s.replace('.', '').replace(',', '.'))

def verkehrswert(t):
    if not t:
        return None
    unten = t.lower()
    if 'gesamt' in unten:
        rest = t[unten.rfind('gesamt'):]
        m = RX_EURO.search(rest)
        if m:
            return eur(m.group(0))
    werte = [eur(x) for x in RX_EURO.findall(t)]
    if 'jeweils' in unten and len(werte) == 1:
        return None
    return sum(werte) if werte else None

def termin(t):
    m = re.search(r'(\d{1,2})\.\s*([A-Za-zäöüÄÖÜ]+)\s+(\d{4})(?:,\s*(\d{1,2}):(\d{2}))?', t or '')
    if not m or m.group(2).lower() not in MONATE:
        return ''
    d = datetime.datetime(int(m.group(3)), MONATE[m.group(2).lower()], int(m.group(1)),
                          int(m.group(4) or 0), int(m.group(5) or 0))
    return d.strftime('%Y-%m-%d %H:%M')

def aufbereiten(e):
    ol = e.get('objekt_lage', '')
    objekt, _, adresse = ol.partition(': ')
    if not adresse:
        objekt, adresse = '', ol
    m = re.search(r'\b(\d{5})\s+([^,]+)', adresse)
    return {
        'zvg_id': e.get('zvg_id', ''), 'az': e.get('az', ''), 'gericht': e.get('gericht', ''),
        'objekt': objekt.strip(), 'adresse': adresse.strip(),
        'plz': m.group(1) if m else '', 'ort': m.group(2).strip() if m else '',
        'verkehrswert': verkehrswert(e.get('wert_text', '')), 'wert_text': e.get('wert_text', ''),
        'termin': termin(e.get('termin_text', '')),
        'aufgehoben': 'ja' if 'aufgehoben' in e.get('termin_text', '').lower() else '',
        'aktualisiert': e.get('aktualisiert', ''),
    }

def region(plz):
    for name, praefixe in REGIONEN.items():
        if any(plz.startswith(p) for p in praefixe):
            return name
    return ''

def nebenobjekt(objekt):
    haupt = re.split(r',| mit | und ', objekt.lower())[0]
    return any(w in haupt for w in OHNE)

def schluessel(e):
    return e['zvg_id'] or f'{e["gericht"]}|{e["az"]}'

# ---------- Ablage und Ausgabe ----------
def lade():
    if not os.path.exists(DATEI):
        return {}
    with open(DATEI, newline='', encoding='utf-8') as f:
        return {r['id']: r for r in csv.DictReader(f)}

def speichere(alle):
    os.makedirs(ORDNER, exist_ok=True)
    with open(DATEI, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=SPALTEN, extrasaction='ignore')
        w.writeheader()
        for e in sorted(alle.values(), key=lambda e: (e['termin'] or '9999', e['id']), reverse=True):
            w.writerow(e)

def link(e):
    if e['zvg_id']:
        return f'[Details]({BASE}index.php?button=showZvg&zvg_id={e["zvg_id"]}&land_abk={LAND_ABK})'
    return ''

def wert_fmt(v):
    try:
        return f'{float(v):,.0f}'.replace(',', '.')
    except (TypeError, ValueError):
        return '–'

def bericht(alle, neu_ids, stand):
    jetzt = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    teile = [f'# Zwangsversteigerungen – Stand {stand}', '',
             'Quelle: ZVG-Portal der Justiz (nur Terminliste). **NEU** = seit dem letzten Lauf hinzugekommen; '
             '**ohne Wertgrenzen** = die 5/10- und 7/10-Grenzen sind weggefallen, Zuschlag also auch unter 50 % '
             'des Verkehrswerts möglich. Gutachten bitte über den Detail-Link selbst laden.', '']
    for reg in REGIONEN:
        liste = [e for e in alle.values() if e['region'] == reg and e['termin'] >= jetzt and not e['aufgehoben']
                 and e['zuletzt_gesehen'] == stand]
        liste.sort(key=lambda e: e['termin'])
        teile += [f'## {reg} ({len(liste)} anstehende Termine)', '',
                  '| Termin | Objekt | Ort | Verkehrswert € | Gericht / Az. | Hinweis | Link |', '|---|---|---|---|---|---|---|']
        for e in liste:
            hinweis = ' '.join(x for x in ['**NEU**' if e['id'] in neu_ids else '',
                                          'ohne Wertgrenzen' if e['wertgrenzen_weg'] else ''] if x)
            teile.append(f'| {e["termin"]} | {e["objekt"]} | {e["plz"]} {e["ort"]} | {wert_fmt(e["verkehrswert"])} '
                         f'| {e["gericht"]} {e["az"]} | {hinweis} | {link(e)} |')
        teile.append('')
    os.makedirs(os.path.dirname(BERICHT), exist_ok=True)
    open(BERICHT, 'w', encoding='utf-8').write('\n'.join(teile) + '\n')

def issue(titel, text_):
    tok, repo = os.environ.get('GITHUB_TOKEN'), os.environ.get('GITHUB_REPOSITORY')
    if not tok or not repo:
        print('kein Token - Meldung nur im Log:', titel)
        return
    r = requests.post(f'https://api.github.com/repos/{repo}/issues', timeout=30,
                      headers={'Authorization': 'Bearer ' + tok, 'Accept': 'application/vnd.github+json'},
                      json={'title': titel, 'body': text_})
    print('Issue', r.status_code, r.text[:200] if r.status_code >= 300 else '')

def main():
    heute = datetime.date.today().isoformat()
    os.makedirs(ORDNER, exist_ok=True)
    log = []
    html, soll, protokoll, kandidaten = abrufen(False)
    eintraege = parse(html)
    log += protokoll + [f'Einträge Bayern: {len(eintraege)}']
    auffaellig = [e for e in eintraege if e['zvg_id'] and not e['plz']]
    if not eintraege or (soll and len(eintraege) < soll) or auffaellig:
        with gzip.open(ORDNER + '/_roh.html.gz', 'wt', encoding='utf-8') as f:
            for name, h in kandidaten:
                f.write(f'\n<!-- ===== {name} ===== -->\n{h}')
        log.append(f'Diagnose: Rohseiten in _roh.html.gz (unvollständig: {soll and len(eintraege) < soll}, '
                   f'auffällige Einträge: {len(auffaellig)})')
    elif os.path.exists(ORDNER + '/_roh.html.gz'):
        os.remove(ORDNER + '/_roh.html.gz')
    if not eintraege:
        open(ORDNER + '/_log.txt', 'w').write('\n'.join(log) + '\n')
        raise SystemExit('ZVG: keine Einträge erkannt')
    try:
        h2, soll2, prot2, _ = abrufen(True)
        ohne = {schluessel(e) for e in parse(h2)}
        log.append(f'ohne Wertgrenzen (Bayern): {len(ohne)} (Soll {soll2})')
        if soll and soll2 is not None and soll2 >= soll:
            log.append('Filter Wertgrenzen wirkungslos - Kennzeichnung ausgelassen')
            ohne = set()
    except Exception as ex:
        ohne = set()
        log.append(f'Abfrage ohne Wertgrenzen fehlgeschlagen: {ex}')
    alle = lade()
    erster_lauf = not alle
    neu = set()
    for e in eintraege:
        reg = region(e['plz'])
        if not reg or nebenobjekt(e['objekt']):
            continue
        k = schluessel(e)
        e.update(id=k, region=reg, wertgrenzen_weg='ja' if k in ohne else '')
        alt = alle.get(k)
        e['erst_gesehen'] = alt['erst_gesehen'] if alt else heute
        e['zuletzt_gesehen'] = heute
        if not alt:
            neu.add(k)
        alle[k] = e
    speichere(alle)
    bericht(alle, neu, heute)
    treffer = [alle[k] for k in neu if not alle[k]['aufgehoben']]
    log.append(f'Region: {sum(1 for e in alle.values() if e["zuletzt_gesehen"] == heute)} aktuell, {len(neu)} neu')
    open(ORDNER + '/_log.txt', 'w').write('\n'.join(log) + '\n')
    print('\n'.join(log))
    if treffer:
        zeilen = [f'- {e["region"]}: {e["objekt"]}, {e["plz"]} {e["ort"]}, Verkehrswert {wert_fmt(e["verkehrswert"])} €, '
                  f'Termin {e["termin"]}{", ohne Wertgrenzen" if e["wertgrenzen_weg"] else ""} {link(e)}'
                  for e in sorted(treffer, key=lambda e: e['termin'])]
        titel = (f'Zwangsversteigerungen: erster Lauf, {len(treffer)} Termine' if erster_lauf
                 else f'Zwangsversteigerungen {heute}: {len(treffer)} neu')
        issue(titel, '\n'.join(zeilen) + '\n\nBericht: reports/zwangsversteigerungen.md')

if __name__ == '__main__':
    main()

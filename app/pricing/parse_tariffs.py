"""Parst die 10 Start-Kit-Tarife (xlsx/pdf/docx/txt/jpg) in ein einheitliches JSON.
Usage: python -m app.pricing.parse_tariffs data/start-kit data/tariffs.json"""
import sys, re, json, subprocess, pathlib
import openpyxl, docx

kit = pathlib.Path(sys.argv[1]); out = sys.argv[2]
NUM = re.compile(r'-?\d+(?:[.,]\d+)?')
def num(x):
    if x is None: return None
    if isinstance(x, (int, float)): return float(x)
    m = NUM.search(str(x).replace('EUR', '')); 
    return float(m.group().replace(',', '.')) if m else None
def codes(s): return re.findall(r'\b[A-Z]{3}\b', s)
def brk(h):
    m = re.search(r'\d+', str(h)); return int(m.group()) if m else None
def transit(x):
    m = re.search(r'\d+', str(x)); return int(m.group()) if m else None
SUR = re.compile(r'^(.*?):\s*fuel\s+(?:EUR\s*)?([\d.,]+)\s+per kg,\s*security\s+(?:EUR\s*)?([\d.,]+)', re.I)

lanes = {}   # (airline, o, d) -> dict
def add(airline, o, d, tr, mn, breaks, rates):
    lanes[(airline, o, d)] = dict(airline=airline, origin=o, destination=d, transit_days=tr,
        minimum=mn, breaks=[dict(from_kg=b, rate=r) for b, r in zip(breaks, rates)], fuel=None, security=None)
def sur(airline, text):
    m = SUR.match(text.strip())
    if not m: return False
    c = codes(m.group(1)); k = (airline, c[0], c[-1])
    lanes[k]['fuel'] = num(m.group(2)); lanes[k]['security'] = num(m.group(3)); return True
def datalines(rows): return [r for r in rows if any(c is not None for c in r)]

# ---------- XLSX ----------
def xlsx_rows(f):
    ws = openpyxl.load_workbook(kit / f, data_only=True).worksheets[0]
    return [tuple(c for c in r) for r in ws.iter_rows(values_only=True)]

# Elbflug: "Origin: XXX", Kopf "from N kg", Preise als Strings mit Komma
rows = xlsx_rows('elbflug-air-freight-tariff-2026-11.xlsx'); A = 'Elbflug Air Freight'
origin = None; br = None
for r in datalines(rows):
    s = str(r[0])
    if s.startswith('Origin:'): origin = s.split(':')[1].strip()
    elif s == 'Destination': br = [brk(h) for h in r[3:] if h]
    elif re.fullmatch(r'[A-Z]{3}', s) and br:
        add(A, origin, s, transit(r[1]), num(r[2]), br, [num(x) for x in r[3:3 + len(br)]])
    else: sur(A, s)

# Kestrel: Surcharges stehen in den Spalten K/L, Transit "D+4"
rows = xlsx_rows('kestrel-airways-tariff-2026-11.xlsx'); A = 'Kestrel Airways'
origin = None; br = None
for r in datalines(rows):
    s = str(r[0])
    if s.startswith('Origin:'): origin = s.split(':')[1].strip()
    elif s == 'Destination': pass
    elif r[0] is None and r[3] in ('-45',): br = [brk(h) for h in r[3:10]]
    elif re.fullmatch(r'[A-Z]{3}', s) and br:
        add(A, origin, s, transit(r[1]), num(r[2]), br, [num(x) for x in r[3:10]])
        lanes[(A, origin, s)]['fuel'] = num(r[10]); lanes[(A, origin, s)]['security'] = num(r[11])

# Vega: "Lagos (LOS)", Origin: "Cologne (CGN)"
rows = xlsx_rows('vega-skyfreight-tariff-2026-11.xlsx'); A = 'Vega Skyfreight'
origin = None; br = None
for r in datalines(rows):
    s = str(r[0])
    if s.startswith('Origin:'): origin = codes(s)[0]
    elif r[0] is None and r[3] == '-45': br = [brk(h) for h in r[3:10]]
    elif re.search(r'\([A-Z]{3}\)$', s) and br and isinstance(r[1], (int, float)):
        add(A, origin, codes(s)[0], transit(r[1]), num(r[2]), br, [num(x) for x in r[3:10]])
    else: sur(A, s)

# Wayzata: "CGN Cologne > DEL Delhi", "2 days"
rows = xlsx_rows('wayzata-air-cargo-tariff-2026-11.xlsx'); A = 'Wayzata Air Cargo'
br = None
for r in datalines(rows):
    s = str(r[0])
    if r[0] is None and r[3] == '-45': br = [brk(h) for h in r[3:9]]
    elif '>' in s and r[1] and br and ':' not in s:
        c = codes(s); add(A, c[0], c[-1], transit(r[1]), num(r[2]), br, [num(x) for x in r[3:9]])
    else: sur(A, s)

# ---------- DOCX (Quetzal) ----------
A = 'Quetzal Air Freight'; d = docx.Document(kit / 'quetzal-air-freight-tariff-2026-11.docx')
for t in d.tables:
    cells = [[c.text for c in r.cells] for r in t.rows]
    if cells[0][0] == 'Lane':
        br = [brk(h) for h in cells[0][3:]]
        for r in cells[1:]:
            c = codes(r[0]); add(A, c[0], c[-1], transit(r[1]), num(r[2]), br, [num(x) for x in r[3:]])
for p in d.paragraphs: sur(A, p.text)

# ---------- TXT (Yellowtail) ----------
A = 'Yellowtail Air Freight'; txt = (kit / 'yellowtail-air-freight-tariff-2026-11.txt').read_text().splitlines()
br = [0, 45, 100, 250, 500, 1000, 2000]
for ln in txt:
    m = re.match(r'^([A-Z]{3})-([A-Z]{3})\s+(\d+)\s+(.*)$', ln)
    if m:
        v = [num(x) for x in NUM.findall(m.group(4).replace('EUR', ''))]
        add(A, m.group(1), m.group(2), int(m.group(3)), v[0], br, v[1:8])
    else: sur(A, ln)

# ---------- PDF ----------
def pdf_lines(f): return subprocess.run(['pdftotext', '-layout', str(kit / f), '-'], capture_output=True, text=True).stdout.splitlines()

# Helix: Tabelle
A = 'Helix Air Freight'; br = [0, 45, 100, 250, 500, 1000, 2000]
for ln in pdf_lines('helix-air-freight-tariff-2026-11.pdf'):
    m = re.match(r'^(.*\([A-Z]{3}\) to .*\([A-Z]{3}\))\s+D\+(\d+)\s+(EUR.*)$', ln.strip())
    if m:
        c = codes(m.group(1)); v = [num(x) for x in re.findall(r'EUR\s*[\d.,]+', m.group(3))]
        add(A, c[0], c[-1], int(m.group(2)), v[0], br, v[1:8])
    else: sur(A, ln)

# Orion: Blöcke "CGN-MIA / Transit / Minimum / from N kg"
A = 'Orion Cargo Air'; cur = None
for ln in pdf_lines('orion-cargo-air-tariff-2026-11.pdf'):
    s = ln.strip()
    if re.fullmatch(r'[A-Z]{3}-[A-Z]{3}', s): cur = dict(c=s.split('-'), br=[], rt=[])
    elif cur and s.startswith('Transit'): cur['tr'] = transit(s)
    elif cur and s.startswith('Minimum'): cur['mn'] = num(s)
    elif cur and s.startswith('from'):
        cur['br'].append(brk(s)); cur['rt'].append(num(s.split('kg')[1]))
        if cur['br'][-1] == 1000: add(A, cur['c'][0], cur['c'][1], cur['tr'], cur['mn'], cur['br'], cur['rt'])
    else: sur(A, s)

# Cobalt: Blöcke "CGN Cologne > EZE Buenos Aires" ... Fuel / Security
A = 'Cobalt Skylift'; cur = None
def flush():
    if cur and cur.get('sec') is not None:
        add(A, cur['c'][0], cur['c'][1], cur['tr'], cur['mn'], cur['br'], cur['rt'])
        l = lanes[(A, cur['c'][0], cur['c'][1])]; l['fuel'] = cur['fuel']; l['security'] = cur['sec']
for ln in pdf_lines('cobalt-skylift-tariff-2026-11.pdf'):
    s = ln.strip()
    if re.match(r'^[A-Z]{3} \w.* > [A-Z]{3} ', s): flush(); c = codes(s); cur = dict(c=[c[0], c[-1]], br=[], rt=[], sec=None)
    elif cur and s.startswith('Transit'): cur['tr'] = transit(s)
    elif cur and s.startswith('Minimum'): cur['mn'] = num(s)
    elif cur and s.startswith('from'): cur['br'].append(brk(s)); cur['rt'].append(num(s.split('kg')[1]))
    elif cur and s.startswith('Fuel'): cur['fuel'] = num(s)
    elif cur and s.startswith('Security'): cur['sec'] = num(s)
flush()

# ---------- JPG (Danube): manuell aus den Bildern transkribiert -> VERIFIZIEREN ----------
A = 'Danube Wings Cargo'; br = [0, 45, 100, 300, 500, 1000]
D = [('CGN','SYD',4,134,[6.28,5.34,4.28,3.89,3.27,2.84],1.13,.23), ('DUS','MEX',5,101,[3.02,2.36,1.89,1.57,1.32,1.00],.74,.24),
     ('DUS','SYD',5,110,[5.78,4.45,3.92,2.90,2.20,1.89],.69,.16), ('DUS','YYZ',4,62,[3.08,2.62,2.07,1.61,1.22,0.96],.88,.21),
     ('FRA','DXB',2,138.86,[2.90,2.23,2.05,1.66,1.45,1.33],1.12,.22), ('FRA','EZE',2,126,[4.57,3.34,2.74,2.52,1.92,1.48],1.06,.21),
     ('MUC','JNB',7,105,[3.74,2.81,2.19,1.62,1.39,1.22],.85,.24), ('MUC','TPE',5,69,[4.10,3.73,3.21,2.57,2.36,2.03],1.15,.21),
     ('STR','SGN',5,83,[4.63,4.21,3.45,2.93,2.40,2.18],1.09,.11)]
for o, d_, tr, mn, rt, f, s_ in D:
    add(A, o, d_, tr, float(mn), br, rt); lanes[(A, o, d_)].update(fuel=f, security=s_, source='transcribed-from-jpg')

res = list(lanes.values())
bad = [l for l in res if l['fuel'] is None or l['security'] is None or l['minimum'] is None or None in [b['rate'] for b in l['breaks']]]
json.dump(dict(currency='EUR', valid_from='2026-11-01', valid_to='2026-11-30', volume_factor=167, lanes=res), open(out, 'w'), indent=1)
from collections import Counter
print('Lanes gesamt:', len(res)); print(dict(Counter(l['airline'] for l in res))); print('unvollständig:', len(bad), [(l['airline'], l['origin'], l['destination']) for l in bad])

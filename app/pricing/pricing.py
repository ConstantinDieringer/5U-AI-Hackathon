"""Deterministisches Preis-Modul fuer die Sheet-Airlines (kein LLM-Call).

Regeln laut allen 10 Sheets (identischer Wortlaut):
- Chargeable Weight (CW) = max(Ist-Gewicht, Volumen_cbm * 167)
- Rate pro kg nach Gewichtsstufe des abgerechneten Gewichts
- Minimum gilt nur fuer die Fracht, VOR Zuschlaegen
- Fuel + Security pro kg des abgerechneten Gewichts

Zusaetzlich: Break-Point-Optimierung. Liegt das Gewicht knapp unter einer Stufe,
kann Abrechnen zum Stufengewicht billiger sein. Ob der 5U-Scorer das als "besten Preis"
wertet, ist NICHT bestaetigt -> an echten Quotes/Board pruefen (siehe best_total vs. plain_total).
"""
import json, pathlib

VOL = 167.0
_DEFAULT = pathlib.Path(__file__).resolve().parents[2] / 'data' / 'tariffs.json'

def load(path=_DEFAULT):
    return json.loads(pathlib.Path(path).read_text())['lanes']

def _rate(lane, kg):
    r = lane['breaks'][0]['rate']
    for b in lane['breaks']:
        if kg >= b['from_kg']:
            r = b['rate']
    return r

def _total(lane, billed):
    freight = max(lane['minimum'], _rate(lane, billed) * billed)
    return round(freight + (lane['fuel'] + lane['security']) * billed, 2)

def price_lane(lane, weight_kg, volume_cbm=0.0):
    cw = max(weight_kg, volume_cbm * VOL)
    plain = _total(lane, cw)
    best, billed = plain, cw
    for b in lane['breaks']:
        if b['from_kg'] > cw:
            t = _total(lane, b['from_kg'])
            if t < best:
                best, billed = t, b['from_kg']
    return dict(airline=lane['airline'], origin=lane['origin'], destination=lane['destination'],
                transit_days=lane['transit_days'], chargeable_kg=round(cw, 2),
                plain_total=plain, best_total=best, billed_kg=billed, currency='EUR')

def quotes(lanes, origin, destination, weight_kg, volume_cbm=0.0):
    return [price_lane(l, weight_kg, volume_cbm) for l in lanes
            if l['origin'] == origin and l['destination'] == destination]

def pick(options, mode='cheapest', key='best_total'):
    """mode: 'cheapest' | 'fastest' | 'balanced' (Preis/Transit 50/50, min-max-normiert).
    Gleichstand: cheapest -> schneller gewinnt, fastest -> billiger gewinnt."""
    if not options:
        return None
    if mode == 'cheapest':
        return min(options, key=lambda o: (o[key], o['transit_days']))
    if mode == 'fastest':
        return min(options, key=lambda o: (o['transit_days'], o[key]))
    ps = [o[key] for o in options]; ts = [o['transit_days'] for o in options]
    n = lambda v, lo, hi: 0.0 if hi == lo else (v - lo) / (hi - lo)
    return min(options, key=lambda o: 0.5 * n(o[key], min(ps), max(ps)) + 0.5 * n(o['transit_days'], min(ts), max(ts)))

if __name__ == '__main__':
    import sys
    o, d, kg = sys.argv[1], sys.argv[2], float(sys.argv[3])
    cbm = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
    opts = quotes(load(), o, d, kg, cbm)
    for x in sorted(opts, key=lambda x: x['best_total']):
        print(x)
    for m in ('cheapest', 'fastest', 'balanced'):
        print(m, '->', pick(opts, m))

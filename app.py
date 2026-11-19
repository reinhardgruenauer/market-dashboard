"""
Market Dashboard - ES Future & NQ Future Daily Analysis
v5 FIX: prev_close via separatem 1d-Call ohne includePrePost
        → garantiert offizieller Settlement-Kurs (= Google/Yahoo Referenz)
"""
from flask import Flask, render_template, jsonify, request
import feedparser
import requests as http_requests
import datetime
import threading
import time
import json
import os
from zoneinfo import ZoneInfo
from pathlib import Path

TZ_VIENNA = ZoneInfo("Europe/Vienna")
CALENDAR_CACHE_FILE = Path(__file__).parent / "calendar_cache.json"
app = Flask(__name__)

WEIGHTS_FILE = Path(__file__).parent / "weights.json"

def _load_weights():
    """Indexgewichte aus weights.json (leicht aktualisierbar ohne Code-Änderung)."""
    with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
        w = json.load(f)
    return w

_WEIGHTS     = _load_weights()
SP500_TOP10  = _WEIGHTS["sp500"]["top10"]
NQ100_TOP10  = _WEIGHTS["nq100"]["top10"]
VOL_INDICES  = [("^VIX", "VIX"), ("^VXN", "VXN")]
INDEX_SYMBOL = {"nq100": "^NDX", "sp500": "^GSPC"}   # für die Gewichts-Fortschreibung

# Barometer-Parameter
BARO_FULL_SCALE_PCT = 2.0   # ±2 % gewichtete Veränderung = Vollausschlag ±100
BARO_W_CHANGE       = 0.7   # Anteil gewichtete Veränderung
BARO_W_POSITION     = 0.3   # Anteil Lage zur Referenz (Vortagesschluss bzw. VWAP)

_cache = {
    "stock_data": None, "news_data": None, "calendar_data": None,
    "last_stock_update": None, "last_news_update": None, "last_calendar_update": None,
}
_cache_lock = threading.Lock()
STOCK_CACHE_SECONDS    = 60
NEWS_CACHE_SECONDS     = 300
CALENDAR_CACHE_SECONDS = 3600

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://finance.yahoo.com/",
    "Origin":          "https://finance.yahoo.com",
}

def _all_symbols():
    return sorted({s["symbol"] for s in SP500_TOP10 + NQ100_TOP10})


def _yahoo_api(symbol, interval, range_str, include_pre_post=True):
    """Einheitliche Yahoo Finance v8 Chart API."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "interval":       interval,
        "range":          range_str,
        "includePrePost": "true" if include_pre_post else "false",
    }
    r = http_requests.get(url, params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    result = r.json().get("chart", {}).get("result", [])
    return result[0] if result else {}


def _get_daily_closes(symbol):
    """
    Offizielle Tages-Schlusskurse (ohne Pre/Post-Market) der letzten Handelstage
    VOR heute, älteste zuerst. [-1] = letzter Schluss, [-2] = Schluss davor.
    Separater Call nötig, weil Yahoo mit includePrePost=true manchmal einen
    pre/post-market-berechneten Wert statt des echten Schlusskurses liefert.
    """
    today = datetime.datetime.now(TZ_VIENNA).date()
    try:
        chart  = _yahoo_api(symbol, interval="1d", range_str="10d", include_pre_post=False)
        ts     = chart.get("timestamp", []) or []
        closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        out = []
        for i, t in enumerate(ts):
            cv = closes[i] if i < len(closes) else None
            if cv and datetime.datetime.fromtimestamp(t, tz=TZ_VIENNA).date() < today:
                out.append(round(cv, 2))
        if not out:
            meta = chart.get("meta", {})
            pc = meta.get("chartPreviousClose") or meta.get("previousClose") or 0
            out = [pc] if pc else []
        return out
    except Exception as e:
        print(f"  daily closes error {symbol}: {e}")
        return []


def _calc_vwap(meta, timestamps, highs, lows, closes, volumes):
    """
    VWAP der heutigen regulären Sitzung (typischer Preis × Volumen).
    Sitzungsgrenzen kommen aus Yahoos currentTradingPeriod → stimmt auch bei
    Sommerzeit-Verschiebungen. Vor 15:30 MEZ (kein Volumen) → 0.
    """
    try:
        reg = meta.get("currentTradingPeriod", {}).get("regular", {})
        start, end = reg.get("start"), reg.get("end")
        if not start or not end:
            return 0
        pv = vol = 0.0
        for i, ts in enumerate(timestamps):
            if ts < start or ts >= end:
                continue
            c = closes[i] if i < len(closes) else None
            v = volumes[i] if i < len(volumes) else None
            if not c or not v:
                continue
            hi = highs[i] if i < len(highs) and highs[i] else c
            lo = lows[i] if i < len(lows) and lows[i] else c
            pv += (hi + lo + c) / 3 * v
            vol += v
        return round(pv / vol, 2) if vol > 0 else 0
    except Exception as e:
        print(f"  VWAP error: {e}")
        return 0


def _parse_stand(stand):
    try:
        return datetime.datetime.strptime(stand.strip()[:10], "%d.%m.%Y").date()
    except Exception:
        return None


def _close_on_or_before(symbol, day, today):
    """(Schluss am/vor Stichtag, letzter Schluss vor heute) aus Tagesdaten."""
    chart  = _yahoo_api(symbol, interval="1d", range_str="1y", include_pre_post=False)
    ts     = chart.get("timestamp", []) or []
    closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    base = last = None
    for i, t in enumerate(ts):
        cv = closes[i] if i < len(closes) else None
        if not cv: continue
        d = datetime.datetime.fromtimestamp(t, tz=TZ_VIENNA).date()
        if d <= day:  base = cv
        if d < today: last = cv
    return base, last


def _compute_drifted_weights(key):
    """
    Schreibt die Stichtags-Gewichte aus weights.json mit der Kursentwicklung fort:
      w_heute = w_stichtag × (Aktie_jetzt / Aktie_stichtag) ÷ (Index_jetzt / Index_stichtag)
    Basis = letzter Tagesschluss. So bleiben die Gewichte ohne manuelle Pflege aktuell;
    nur bei Änderungen der Top-10-Zusammensetzung muss weights.json angepasst werden.
    """
    cfg   = _WEIGHTS[key]
    base  = cfg["top10"]
    stand = _parse_stand(cfg.get("stand", ""))
    today = datetime.datetime.now(TZ_VIENNA).date()
    if not stand or stand >= today:
        return [dict(s, weight_base=s["weight"]) for s in base], None
    try:
        ib, il = _close_on_or_before(INDEX_SYMBOL[key], stand, today)
        if not ib or not il:
            raise ValueError("keine Indexdaten")
        idx_factor = il / ib
        out, last_day = [], None
        for s in base:
            sb, sl = _close_on_or_before(s["symbol"], stand, today)
            time.sleep(0.1)
            f = (sl / sb / idx_factor) if (sb and sl) else 1.0
            out.append(dict(s, weight=round(s["weight"] * f, 2), weight_base=s["weight"]))
        out.sort(key=lambda x: x["weight"], reverse=True)
        return out, today
    except Exception as e:
        print(f"  weight drift error {key}: {e}")
        return [dict(s, weight_base=s["weight"]) for s in base], None


_weights_cache = {"day": None, "nq100": None, "sp500": None, "updated": None}
_weights_lock  = threading.Lock()

def get_weights(key):
    """Fortgeschriebene Gewichte, einmal pro Tag neu berechnet."""
    today = datetime.datetime.now(TZ_VIENNA).date()
    with _weights_lock:
        if _weights_cache["day"] == today and _weights_cache[key]:
            return _weights_cache[key]
    w, upd = _compute_drifted_weights(key)
    with _weights_lock:
        if _weights_cache["day"] != today:
            _weights_cache.update({"day": today, "nq100": None, "sp500": None})
        _weights_cache[key] = w
        _weights_cache["updated"] = upd
    return w


def _weights_info(key):
    stand = _WEIGHTS[key].get("stand", "")
    upd = _weights_cache.get("updated")
    if upd:
        return f"Basis {stand}, täglich mit den Kursen fortgeschrieben (Stand {upd.strftime('%d.%m.%Y')})"
    return f"Stand {stand}"


def _detect_phase(meta=None):
    """
    Sitzungsphase der US-Aktien:
      PRE    = vorbörslicher US-Handel
      REG    = regulärer US-Handel
      POST   = nachbörslicher US-Handel
      EU     = werktags außerhalb der US-Zeiten (Europa-Session, über Nacht)
      CLOSED = Wochenende
    Grenzen aus Yahoos currentTradingPeriod → stimmt auch bei Zeitumstellungen.
    """
    now_v = datetime.datetime.now(TZ_VIENNA)
    if now_v.weekday() >= 5:
        return "CLOSED"
    now = time.time()
    ctp = (meta or {}).get("currentTradingPeriod") or {}
    try:
        for key, name in (("pre", "PRE"), ("regular", "REG"), ("post", "POST")):
            p = ctp.get(key) or {}
            if p.get("start") and p.get("end") and p["start"] <= now < p["end"]:
                return name
        if ctp:
            return "EU"
    except Exception as e:
        print(f"  phase error: {e}")
    # Fallback ohne Yahoo-Zeiten: feste Wiener Uhrzeiten
    h = now_v.hour + now_v.minute / 60.0
    if 10.0 <= h < 15.5:  return "PRE"
    if 15.5 <= h < 22.0:  return "REG"
    if h >= 22.0 or h < 2.0: return "POST"
    return "EU"


def _baro_label(score):
    if score <= -50: return "Stark bärisch"
    if score <= -15: return "Bärisch"
    if score < 15:   return "Neutral"
    if score < 50:   return "Bullisch"
    return "Stark bullisch"


def _calc_barometer(top10, stock_data, future_level, phase):
    """
    Barometer −100 … +100:
      70 % gewichtete Veränderung zum Vortagesschluss (±2 % = Vollausschlag)
      30 % gewichtete Lage zur Referenz: im US-Handel Tages-VWAP, sonst Vortagesschluss
    Beschreibt den Ist-Zustand der Schwergewichte – keine Prognose.
    """
    use_vwap = phase == "REG"
    sw = wchg = wpos = 0.0
    up = above = valid = 0
    rows = []
    for s in top10:
        d   = stock_data.get(s["symbol"], {})
        w   = s["weight"]
        chg = d.get("change_pct", 0) or 0
        cur = d.get("current", 0) or 0
        vw  = d.get("vwap", 0) or 0
        if cur <= 0:
            continue
        valid += 1
        diff = (cur - vw) if (use_vwap and vw > 0) else chg
        sign = 1 if diff > 0 else (-1 if diff < 0 else 0)   # exakt 0 = neutral
        sw   += w
        wchg += chg * w
        wpos += sign * w
        up    += 1 if chg > 0 else 0
        above += 1 if sign > 0 else 0
        rows.append({"symbol": s["symbol"], "above": sign > 0, "sign": sign,
                     "contrib_pts": round(chg / 100 * w / 100 * (future_level or 0), 1)})
    if sw == 0:
        return {"score": 0, "label": "Keine Daten", "weighted_change": 0, "up_count": 0,
                "above_count": 0, "count": 0, "reference": "—", "weight_sum": 0, "stocks": []}
    wc   = wchg / sw
    chgp = max(-1.0, min(1.0, wc / BARO_FULL_SCALE_PCT))
    posp = wpos / sw
    score = round((BARO_W_CHANGE * chgp + BARO_W_POSITION * posp) * 100)
    return {
        "score": score, "label": _baro_label(score),
        "weighted_change": round(wc, 2),
        "up_count": up, "above_count": above, "count": valid,
        "reference": "VWAP" if use_vwap else ("Schluss davor" if phase in ("EU", "CLOSED") else "Vortagesschluss"),
        "weight_sum": round(sum(s["weight"] for s in top10), 1),
        "stocks": rows,
    }


def _fetch_stock_data():
    """
    Live-Modus (PRE/REG/POST): aktueller Kurs vs. letzter offizieller Schluss, Chart = heute.
    Letzte-Sitzung-Modus (EU/CLOSED, nur Aktien & VIX/VXN): letzter verfügbarer Kurs
    inkl. Nachbörse vs. Schluss davor, Chart = komplette letzte US-Sitzung.
    Futures sind fast rund um die Uhr live und bleiben immer im Live-Modus.
    """
    symbols  = _all_symbols()
    futures  = ["ES=F", "NQ=F"]
    indices  = [v[0] for v in VOL_INDICES]
    all_syms = symbols + futures + indices

    result = {}
    for sym in all_syms:
        result[sym] = {"current": 0, "prev_close": 0, "open": 0, "change_pct": 0,
                       "prices": [], "labels": [], "high": 0, "low": 0, "vwap": 0}
    result["ES=F"]["name"] = "ES Future"; result["NQ=F"]["name"] = "NQ Future"
    for idx_sym, idx_name in VOL_INDICES:
        result[idx_sym]["name"] = idx_name

    # Phase einmal pro Abruf anhand einer Referenzaktie bestimmen
    try:
        ref_meta = _yahoo_api("AAPL", interval="5m", range_str="1d", include_pre_post=True).get("meta", {})
    except Exception as e:
        print(f"  phase meta error: {e}"); ref_meta = {}
    phase = _detect_phase(ref_meta)
    result["__phase__"] = phase

    now_v  = datetime.datetime.now(TZ_VIENNA)
    today  = now_v.date()
    now_ts = now_v.strftime("%H:%M")

    for sym in all_syms:
        try:
            is_future = sym in futures
            last_session = (phase in ("EU", "CLOSED")) and not is_future

            # ── 1. Offizielle Tagesschlüsse (ohne PrePost) ─────────────────
            dcl = _get_daily_closes(sym)
            time.sleep(0.15)

            # ── 2. Intraday 5m inkl. Pre/Post ──────────────────────────────
            chart      = _yahoo_api(sym, interval="5m", range_str="5d", include_pre_post=True)
            meta       = chart.get("meta", {})
            timestamps = chart.get("timestamp", []) or []
            q          = chart.get("indicators", {}).get("quote", [{}])[0]
            closes     = q.get("close", []) or []
            highs      = q.get("high",  []) or []
            lows       = q.get("low",   []) or []
            volumes    = q.get("volume", []) or []

            reg_p = meta.get("regularMarketPrice") or 0
            # Yahoo liefert vor-/nachbörsliche Kurse inzwischen als "fulldayPrice"
            # (früher preMarketPrice/postMarketPrice – beide Varianten unterstützt).
            ext_p = (meta.get("fulldayPrice") or meta.get("preMarketPrice")
                     or meta.get("postMarketPrice") or 0)
            # Fallback: letzter 5-Minuten-Bar der laufenden Phase
            ctp = meta.get("currentTradingPeriod") or {}
            def _last_bar_since(start_ts):
                if not start_ts: return 0
                for i in range(len(timestamps) - 1, -1, -1):
                    cv = closes[i] if i < len(closes) else None
                    if cv and timestamps[i] >= start_ts:
                        return cv
                return 0

            # ── 3. Aktueller Kurs + Referenz je Modus ──────────────────────
            if is_future:
                current_price = reg_p; session = "LIVE"
                prev_close = dcl[-1] if dcl else 0
            elif last_session:
                current_price = ext_p or reg_p or (dcl[-1] if dcl else 0); session = "LAST"
                prev_close = dcl[-2] if len(dcl) >= 2 else (dcl[-1] if dcl else 0)
            else:
                if phase == "PRE":
                    current_price = ext_p or _last_bar_since((ctp.get("pre") or {}).get("start")) or reg_p
                elif phase == "POST":
                    current_price = ext_p or _last_bar_since((ctp.get("post") or {}).get("start")) or reg_p
                else:
                    current_price = reg_p or ext_p
                session = phase
                prev_close = dcl[-1] if dcl else 0

            # ── 4. Chartpunkte ─────────────────────────────────────────────
            bars = []
            for i, t in enumerate(timestamps):
                cv = closes[i] if i < len(closes) else None
                if cv is None: continue
                bars.append((datetime.datetime.fromtimestamp(t, tz=TZ_VIENNA), cv))

            if last_session:
                # komplette letzte US-Sitzung (vorbörslich bis nachbörslich)
                past = [b for b in bars if b[0].date() < today or b[0].hour < 10]
                if past:
                    # Sitzung = Bars ab 10:00 MEZ des letzten Handelstags bis 02:00 danach
                    last_day = max(b[0].date() for b in past if b[0].hour >= 10) if any(b[0].hour >= 10 for b in past) else past[-1][0].date()
                    start = datetime.datetime.combine(last_day, datetime.time(10, 0), tzinfo=TZ_VIENNA)
                    end   = start + datetime.timedelta(hours=16)
                    sel = [b for b in past if start <= b[0] < end]
                else:
                    sel = []
            else:
                cutoff = datetime.datetime.combine(
                    today, datetime.time(0, 0) if (is_future or sym in indices) else datetime.time(10, 0),
                    tzinfo=TZ_VIENNA)
                sel = [b for b in bars if b[0] >= cutoff]

            prices = [round(b[1], 2) for b in sel]
            labels = [b[0].strftime("%H:%M") for b in sel]
            if not last_session and current_price > 0 and (not prices or prices[-1] != round(current_price, 2)):
                prices.append(round(current_price, 2)); labels.append(now_ts)
            if not current_price and prices:
                current_price = prices[-1]

            open_price = prices[0] if prices else (meta.get("regularMarketOpen") or 0)

            # ── 5. Veränderung ────────────────────────────────────────────
            if prev_close > 0 and current_price > 0:
                change_pct = (current_price - prev_close) / prev_close * 100
            else:
                change_pct = 0

            # ── 6. VWAP der regulären Sitzung (nur live) ───────────────────
            vwap = 0 if last_session else _calc_vwap(meta, timestamps, highs, lows, closes, volumes)

            result[sym].update({
                "current":    round(current_price, 2),
                "prev_close": round(prev_close, 2),
                "open":       round(open_price, 2),
                "change_pct": round(change_pct, 2),
                "prices":     prices,
                "labels":     labels,
                "high":       round(max([v for v in highs if v] or [0]), 2),
                "low":        round(min([v for v in lows if v and v > 0] or [0]), 2),
                "session":    session,
                "vwap":       vwap,
            })
            print(f"  {sym} [{session}]: {current_price:.2f} | ref={prev_close:.2f} | "
                  f"chg={change_pct:+.2f}% | pts={len(prices)}")
            time.sleep(0.2)

        except Exception as e:
            print(f"  ERROR {sym}: {e}")

    return result


def _fetch_news():
    all_news = {}
    seen = {s["symbol"]: s["name"] for s in SP500_TOP10 + NQ100_TOP10}
    for sym, name in seen.items():
        try:
            q    = name.split(" (")[0].replace(" ", "+") + "+Aktie"
            feed = feedparser.parse(f"https://news.google.com/rss/search?q={q}&hl=de&gl=DE&ceid=DE:de")
            all_news[sym] = [
                {"title": e.get("title",""), "link": e.get("link",""),
                 "published": e.get("published",""),
                 "source": e.get("source",{}).get("title","") if isinstance(e.get("source"),dict) else ""}
                for e in feed.entries[:3]
            ]
        except Exception as e:
            print(f"  News error {sym}: {e}")
            all_news[sym] = []
    return all_news


def _translate_event_title(title):
    t = {
        "GDP q/q":"Bruttoinlandsprodukt (BIP) (Quartal)","GDP m/m":"BIP (Monat)","GDP y/y":"BIP (Jahr)",
        "Prelim GDP q/q":"BIP (Quartal) Vorabschätzung","Advance GDP q/q":"BIP Erstschätzung (Quartal)",
        "Non-Farm Employment Change":"Beschäftigung außerhalb der Landwirtschaft",
        "Employment Change":"Beschäftigungsänderung","Unemployment Rate":"Arbeitslosenquote",
        "Unemployment Claims":"Erstanträge Arbeitslosenhilfe",
        "Average Hourly Earnings m/m":"Durchschnittliche Stundenlöhne (Monat)",
        "ADP Non-Farm Employment Change":"ADP Beschäftigung außerh. Landwirtschaft",
        "JOLTS Job Openings":"JOLTS Stellenangebote",
        "Nonfarm Payrolls":"Beschäftigung außerh. Landwirtschaft (NFP)",
        "CPI m/m":"Verbraucherpreisindex (Monat)","CPI y/y":"Verbraucherpreisindex (Jahr)",
        "Core CPI m/m":"Kernrate Verbraucherpreisindex (Monat)",
        "PPI m/m":"Erzeugerpreisindex (Monat)","PPI y/y":"Erzeugerpreisindex (Jahr)",
        "Core PPI m/m":"Kernrate Erzeugerpreisindex (Monat)",
        "PCE Price Index m/m":"PCE-Preisindex (Monat)",
        "Core PCE Price Index m/m":"PCE-Kernrate Preisindex (Monat)",
        "Federal Funds Rate":"US-Leitzins (Fed Funds Rate)",
        "FOMC Statement":"FOMC Zinsentscheid / Statement",
        "FOMC Meeting Minutes":"FOMC Sitzungsprotokoll",
        "FOMC Press Conference":"FOMC Pressekonferenz",
        "Interest Rate Decision":"Zinsentscheid",
        "ECB Press Conference":"EZB Pressekonferenz",
        "ECB Interest Rate Decision":"EZB Zinsentscheid",
        "BOE Interest Rate Decision":"BoE Zinsentscheid",
        "Retail Sales m/m":"Einzelhandelsumsätze (Monat)",
        "Consumer Confidence":"Verbrauchervertrauen",
        "CB Consumer Confidence":"CB Verbrauchervertrauen",
        "Michigan Consumer Sentiment":"Verbraucherstimmung Michigan",
        "Prelim UoM Consumer Sentiment":"Verbraucherstimmung Michigan (vorläufig)",
        "ISM Manufacturing PMI":"ISM Einkaufsmanagerindex Produktion",
        "ISM Services PMI":"ISM Einkaufsmanagerindex Dienstleistung",
        "Industrial Production m/m":"Industrieproduktion (Monat)",
        "Manufacturing PMI":"Einkaufsmanagerindex Produktion",
        "Services PMI":"Einkaufsmanagerindex Dienstleistung",
        "Flash Manufacturing PMI":"Einkaufsmanagerindex Prod. (Schnellsch.)",
        "Flash Services PMI":"Einkaufsmanagerindex Dienstl. (Schnellsch.)",
        "Durable Goods Orders m/m":"Aufträge langlebiger Güter (Monat)",
        "Factory Orders m/m":"Fabrikaufträge (Monat)",
        "Existing Home Sales":"Verkäufe bestehender Häuser",
        "New Home Sales":"Neubauverkäufe","Building Permits":"Baugenehmigungen",
        "Housing Starts":"Baubeginne","Trade Balance":"Handelsbilanz",
        "Crude Oil Inventories":"Rohöl-Lagerbestände",
        "Natural Gas Storage":"Erdgas-Lagerbestände",
        "Federal Budget Balance":"Bundeshaushaltssaldo",
        "Beige Book":"Beige Book (Konjunkturbericht)",
    }
    if title in t: return t[title]
    tl = title.lower()
    for en, de in t.items():
        if en.lower() == tl: return de
    for p, d in [("gdp","BIP"),("unemployment rate","Arbeitslosenquote"),
                 ("employment change","Beschäftigungsänderung"),
                 ("interest rate","Zinsentscheid"),("retail sales","Einzelhandelsumsätze"),
                 ("manufacturing pmi","Einkaufsmanagerindex Produktion"),
                 ("services pmi","Einkaufsmanagerindex Dienstleistung"),
                 ("industrial production","Industrieproduktion"),
                 ("consumer confidence","Verbrauchervertrauen"),
                 ("building permits","Baugenehmigungen"),("housing","Immobilien")]:
        if p in tl: return d
    return title


def _save_calendar_to_file(raw, events):
    try:
        with open(CALENDAR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": str(datetime.date.today()),
                       "fetched_at": datetime.datetime.now(TZ_VIENNA).isoformat(),
                       "raw_data": raw, "events": events}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  Calendar save error: {e}")

def _load_calendar_from_file():
    try:
        if CALENDAR_CACHE_FILE.exists():
            with open(CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
                c = json.load(f)
            if c.get("date") == str(datetime.date.today()):
                return c.get("events", [])
    except Exception as e:
        print(f"  Calendar load error: {e}")
    return None

def _parse_ff_events(raw):
    events = []
    today  = datetime.date.today()
    cm = {"USD":"USA","EUR":"Eurozone","GBP":"Großbritannien","JPY":"Japan",
          "CAD":"Kanada","AUD":"Australien","NZD":"Neuseeland","CHF":"Schweiz","CNY":"China"}
    for ev in raw:
        if ev.get("impact","") != "High": continue
        try: ed = datetime.datetime.fromisoformat(ev.get("date","")).date()
        except Exception:
            try: ed = datetime.datetime.strptime(ev.get("date","")[:10],"%Y-%m-%d").date()
            except Exception: continue
        if ed != today: continue
        try:
            edt = datetime.datetime.fromisoformat(ev.get("date",""))
            if edt.tzinfo is None:
                edt = edt.replace(tzinfo=ZoneInfo("America/New_York"))
            et = edt.astimezone(TZ_VIENNA).strftime("%H:%M")
        except Exception: et = ""
        def _de(v):
            if not v or not isinstance(v,str): return v or ""
            return v.replace(".",",") if "." in v else v
        events.append({"time":et,"country":cm.get(ev.get("country",""),ev.get("country","")),
                        "currency":ev.get("country",""),
                        "title":_translate_event_title(ev.get("title","")),
                        "impact":"high","forecast":_de(ev.get("forecast","")),
                        "previous":_de(ev.get("previous","")),"actual":_de(ev.get("actual",""))})
    events.sort(key=lambda x: x.get("time","99:99"))
    return events

def _fetch_economic_calendar():
    try:
        r = http_requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                              headers={"User-Agent": HEADERS["User-Agent"],
                                       "Accept": "application/json"}, timeout=15)
        if r.status_code == 200:
            raw = r.json(); evts = _parse_ff_events(raw)
            _save_calendar_to_file(raw, evts); return evts
    except Exception as e:
        print(f"  Calendar fetch error: {e}")
    cached = _load_calendar_from_file()
    return cached if cached is not None else []

def get_stock_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["stock_data"] and _cache["last_stock_update"] and \
                now - _cache["last_stock_update"] < STOCK_CACHE_SECONDS:
            return _cache["stock_data"]
    data = _fetch_stock_data()
    with _cache_lock:
        _cache["stock_data"] = data; _cache["last_stock_update"] = time.time()
    return data

def get_news_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["news_data"] and _cache["last_news_update"] and \
                now - _cache["last_news_update"] < NEWS_CACHE_SECONDS:
            return _cache["news_data"]
    data = _fetch_news()
    with _cache_lock:
        _cache["news_data"] = data; _cache["last_news_update"] = time.time()
    return data

def get_calendar_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["calendar_data"] and _cache["last_calendar_update"] and \
                now - _cache["last_calendar_update"] < CALENDAR_CACHE_SECONDS:
            return _cache["calendar_data"]
    data = _fetch_economic_calendar()
    with _cache_lock:
        _cache["calendar_data"] = data; _cache["last_calendar_update"] = time.time()
    return data

def _calc_probability(top10, stock_data):
    tw = 0; twu = 0; up = 0
    for s in top10:
        tw += s["weight"]
        if stock_data.get(s["symbol"],{}).get("change_pct",0) > 0:
            up += 1; twu += s["weight"]
    prob = (twu / tw * 100) if tw > 0 else 50
    return {"long_pct":round(prob,1),"short_pct":round(100-prob,1),
            "up_count":up,"down_count":len(top10)-up,
            "signal":"LONG" if prob>50 else "SHORT" if prob<50 else "NEUTRAL"}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/data")
def api_data():
    force = request.args.get("force","false").lower() == "true"
    sd = get_stock_data(force=force)
    nd = get_news_data(force=force)
    cd = get_calendar_data(force=force)
    phase = sd.get("__phase__") or _detect_phase()
    es_lvl = sd.get("ES=F", {}).get("current", 0)
    nq_lvl = sd.get("NQ=F", {}).get("current", 0)
    w_sp = get_weights("sp500")
    w_nq = get_weights("nq100")
    sp500 = [{**s, **sd.get(s["symbol"],{}), "news": nd.get(s["symbol"],[])} for s in w_sp]
    nq100 = [{**s, **sd.get(s["symbol"],{}), "news": nd.get(s["symbol"],[])} for s in w_nq]
    return jsonify({
        "timestamp": datetime.datetime.now(TZ_VIENNA).strftime("%Y-%m-%d %H:%M:%S") + " MEZ",
        "phase":    phase,
        "futures":  {"ES": sd.get("ES=F",{}), "NQ": sd.get("NQ=F",{})},
        "vol":      {"VIX": sd.get("^VIX",{}), "VXN": sd.get("^VXN",{})},
        "sp500":    {"stocks": sp500, "barometer": _calc_barometer(w_sp, sd, es_lvl, phase),
                     "probability": _calc_probability(w_sp, sd),
                     "weights_stand": _weights_info("sp500")},
        "nq100":    {"stocks": nq100, "barometer": _calc_barometer(w_nq, sd, nq_lvl, phase),
                     "probability": _calc_probability(w_nq, sd),
                     "weights_stand": _weights_info("nq100")},
        "calendar": cd,
    })

@app.after_request
def add_headers(response):
    response.headers["X-Frame-Options"]         = "ALLOW-FROM https://www.rg-trading.at"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://www.rg-trading.at https://rg-trading.at"
    return response

if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)

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

SP500_TOP10 = [
    {"symbol": "NVDA",  "name": "Nvidia",               "weight": 7.81},
    {"symbol": "AAPL",  "name": "Apple",                "weight": 6.65},
    {"symbol": "MSFT",  "name": "Microsoft",            "weight": 5.20},
    {"symbol": "AMZN",  "name": "Amazon",               "weight": 3.57},
    {"symbol": "GOOGL", "name": "Alphabet A",           "weight": 3.10},
    {"symbol": "AVGO",  "name": "Broadcom",             "weight": 2.79},
    {"symbol": "GOOG",  "name": "Alphabet C",           "weight": 2.48},
    {"symbol": "META",  "name": "Meta Platforms",       "weight": 2.46},
    {"symbol": "TSLA",  "name": "Tesla",                "weight": 1.98},
    {"symbol": "BRK-B", "name": "Berkshire Hathaway B", "weight": 1.56},
]
NQ100_TOP10 = [
    {"symbol": "NVDA",  "name": "Nvidia",         "weight": 8.90},
    {"symbol": "AAPL",  "name": "Apple",          "weight": 7.35},
    {"symbol": "MSFT",  "name": "Microsoft",      "weight": 6.13},
    {"symbol": "AMZN",  "name": "Amazon",         "weight": 4.90},
    {"symbol": "META",  "name": "Meta Platforms", "weight": 4.03},
    {"symbol": "GOOGL", "name": "Alphabet A",     "weight": 3.77},
    {"symbol": "TSLA",  "name": "Tesla",          "weight": 3.65},
    {"symbol": "GOOG",  "name": "Alphabet C",     "weight": 3.51},
    {"symbol": "WMT",   "name": "Walmart",        "weight": 3.08},
    {"symbol": "AVGO",  "name": "Broadcom",       "weight": 3.00},
]

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


def _get_official_prev_close(symbol):
    """
    Offizieller Vortags-Schlusskurs via 1d-Call OHNE Pre/Post-Market.
    Das ist exakt der Wert den Google Finance und Yahoo Finance selbst
    als Referenz für die % Veränderung nutzen.
    
    Warum separater Call nötig:
    Mit includePrePost=true gibt Yahoo manchmal einen pre/post-market-
    berechneten Wert zurück statt dem echten Börsenschlusskurs.
    """
    today = datetime.datetime.now(TZ_VIENNA).date()
    try:
        chart = _yahoo_api(symbol, interval="1d", range_str="5d", include_pre_post=False)
        ts     = chart.get("timestamp", []) or []
        closes = chart.get("indicators", {}).get("quote", [{}])[0].get("close", [])
        
        # Letzter Close der NICHT heute ist → offizieller Settlement
        for i in range(len(ts) - 1, -1, -1):
            dt = datetime.datetime.fromtimestamp(ts[i], tz=TZ_VIENNA)
            cv = closes[i] if i < len(closes) else None
            if cv and dt.date() < today:
                return round(cv, 2)
        
        # Fallback aus meta
        meta = chart.get("meta", {})
        return meta.get("chartPreviousClose") or meta.get("previousClose") or 0
    except Exception as e:
        print(f"  prev_close error {symbol}: {e}")
        return 0


def _fetch_stock_data():
    symbols  = _all_symbols()
    futures  = ["ES=F", "NQ=F"]
    all_syms = symbols + futures

    result = {}
    for sym in symbols:
        result[sym] = {"current": 0, "prev_close": 0, "open": 0,
                       "change_pct": 0, "prices": [], "labels": [], "high": 0, "low": 0}
    for fut_sym, fut_name in [("ES=F", "ES Future"), ("NQ=F", "NQ Future")]:
        result[fut_sym] = {"name": fut_name, "current": 0, "prev_close": 0, "open": 0,
                           "change_pct": 0, "prices": [], "labels": [], "high": 0, "low": 0}

    now_v  = datetime.datetime.now(TZ_VIENNA)
    today  = now_v.date()
    now_ts = now_v.strftime("%H:%M")
    h      = now_v.hour + now_v.minute / 60.0
    is_pre  = 10.0 <= h < 15.5
    is_reg  = 15.5 <= h < 22.0
    is_post = h >= 22.0 or h < 2.0

    for sym in all_syms:
        try:
            is_future = sym in futures

            # ── 1. Offiziellen prev_close holen (separater Call ohne PrePost) ──
            prev_close = _get_official_prev_close(sym)
            time.sleep(0.15)

            # ── 2. Intraday-Daten mit PrePost=true für Chart + current_price ──
            chart      = _yahoo_api(sym, interval="5m", range_str="5d", include_pre_post=True)
            meta       = chart.get("meta", {})
            timestamps = chart.get("timestamp", []) or []
            quote_data = chart.get("indicators", {}).get("quote", [{}])[0]
            closes     = quote_data.get("close", [])
            highs      = quote_data.get("high",  [])
            lows       = quote_data.get("low",   [])

            # ── 3. Aktuellen Preis aus Meta (immer aktuell, auch Pre-Market) ──
            pre_p  = meta.get("preMarketPrice")    or 0
            reg_p  = meta.get("regularMarketPrice") or 0
            post_p = meta.get("postMarketPrice")   or 0

            if is_pre and pre_p > 0:
                current_price = pre_p;  session = "PRE"
            elif is_reg and reg_p > 0:
                current_price = reg_p;  session = "REG"
            elif is_post and post_p > 0:
                current_price = post_p; session = "POST"
            elif reg_p > 0:
                current_price = reg_p;  session = "REG"
            elif pre_p > 0:
                current_price = pre_p;  session = "PRE"
            else:
                current_price = 0;      session = "—"

            # ── 4. Chart-Punkte für heute ──────────────────────────────────
            cutoff = datetime.datetime.combine(
                today,
                datetime.time(0, 0) if is_future else datetime.time(10, 0),
                tzinfo=TZ_VIENNA
            )

            today_prices, today_labels = [], []
            for i, ts in enumerate(timestamps):
                cv = closes[i] if i < len(closes) else None
                if cv is None: continue
                dt = datetime.datetime.fromtimestamp(ts, tz=TZ_VIENNA)
                if dt >= cutoff:
                    today_prices.append(round(cv, 2))
                    today_labels.append(dt.strftime("%H:%M"))

            # Vor Cash Open: aktuellen Preis als Startpunkt einfügen
            if not today_prices and current_price > 0:
                today_prices = [round(current_price, 2)]
                today_labels = [now_ts]

            # Aktuellsten Preis ans Ende
            if current_price > 0:
                if not today_prices:
                    today_prices = [round(current_price, 2)]
                    today_labels = [now_ts]
                elif today_prices[-1] != round(current_price, 2):
                    today_prices.append(round(current_price, 2))
                    today_labels.append(now_ts)

            # Wochenend-Fallback
            if not today_prices and timestamps:
                last_date = datetime.datetime.fromtimestamp(timestamps[-1], tz=TZ_VIENNA).date()
                for i, ts in enumerate(timestamps):
                    cv = closes[i] if i < len(closes) else None
                    if cv is None: continue
                    if datetime.datetime.fromtimestamp(ts, tz=TZ_VIENNA).date() == last_date:
                        today_prices.append(round(cv, 2))
                        today_labels.append(datetime.datetime.fromtimestamp(ts, tz=TZ_VIENNA).strftime("%H:%M"))
                if today_prices and not current_price:
                    current_price = today_prices[-1]

            open_price = today_prices[0] if today_prices else (meta.get("regularMarketOpen") or 0)

            # ── 5. % Change mit offiziellem prev_close ─────────────────────
            if prev_close > 0 and current_price > 0:
                change_pct = (current_price - prev_close) / prev_close * 100
            elif open_price > 0 and current_price > 0:
                change_pct = (current_price - open_price) / open_price * 100
            else:
                change_pct = 0

            processed = {
                "current":    round(current_price, 2),
                "prev_close": round(prev_close, 2),
                "open":       round(open_price, 2),
                "change_pct": round(change_pct, 2),
                "prices":     today_prices,
                "labels":     today_labels,
                "high":       round(max([v for v in highs if v] or [0]), 2),
                "low":        round(min([v for v in lows if v and v > 0] or [0]), 2),
                "session":    session,
            }
            if sym in futures:
                processed["name"] = "ES Future" if sym == "ES=F" else "NQ Future"

            result[sym] = {**result[sym], **processed}
            print(f"  {sym} [{session}]: {current_price:.2f} | prev={prev_close:.2f} | "
                  f"chg={change_pct:+.2f}% | pts={len(today_prices)} | "
                  f"labels={today_labels[0] if today_labels else '—'}..{today_labels[-1] if today_labels else '—'}")
            time.sleep(0.2)

        except Exception as e:
            print(f"  ERROR {sym}: {e}")

    return result


def _fetch_news():
    all_news = {}
    seen = {s["symbol"]: s["name"] for s in SP500_TOP10 + NQ100_TOP10}
    for sym, name in seen.items():
        try:
            q    = name.replace(" ", "+") + "+Aktie"
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
            "up_count":up,"down_count":10-up,
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
    sp500 = [{**s, **sd.get(s["symbol"],{}), "news": nd.get(s["symbol"],[])} for s in SP500_TOP10]
    nq100 = [{**s, **sd.get(s["symbol"],{}), "news": nd.get(s["symbol"],[])} for s in NQ100_TOP10]
    return jsonify({
        "timestamp": datetime.datetime.now(TZ_VIENNA).strftime("%Y-%m-%d %H:%M:%S") + " MEZ",
        "futures":  {"ES": sd.get("ES=F",{}), "NQ": sd.get("NQ=F",{})},
        "sp500":    {"stocks": sp500, "probability": _calc_probability(SP500_TOP10, sd)},
        "nq100":    {"stocks": nq100, "probability": _calc_probability(NQ100_TOP10, sd)},
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

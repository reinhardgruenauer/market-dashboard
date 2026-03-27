"""
Market Dashboard - ES Future & NQ Future Daily Analysis
Zeigt die Top 10 gewichteten Unternehmen, deren Tagesperformance,
News und eine Long/Short-Wahrscheinlichkeit.
"""
from flask import Flask, render_template, jsonify, request
import feedparser
import requests as http_requests
import datetime
import threading
import time
import re
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
    "stock_data": None,
    "news_data": None,
    "calendar_data": None,
    "last_stock_update": None,
    "last_news_update": None,
    "last_calendar_update": None,
}
_cache_lock = threading.Lock()

STOCK_CACHE_SECONDS    = 60
NEWS_CACHE_SECONDS     = 300
CALENDAR_CACHE_SECONDS = 3600

def _all_symbols():
    syms = set()
    for s in SP500_TOP10 + NQ100_TOP10:
        syms.add(s["symbol"])
    return sorted(syms)

def _yahoo_chart_api(symbol, interval="5m", range_str="5d"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {
        "interval":       interval,
        "range":          range_str,
        "includePrePost": "true",   # ← Pre- & Post-Market einbeziehen
    }
    headers = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept":          "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://finance.yahoo.com/",
        "Origin":          "https://finance.yahoo.com",
    }
    resp = http_requests.get(url, params=params, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()

def _get_current_price(meta, all_valid_closes):
    """
    Gibt den aktuellsten verfügbaren Kurs zurück – inkl. Pre-Market und
    Post-Market. Reihenfolge: preMarket → postMarket → regularMarket →
    letzter Close aus den Zeitreihendaten.

    BUGFIX: Früher wurde nur 'regularMarketPrice' verwendet, das erst ab
    Cash Open (15:30 MEZ) aktualisiert wird.  Jetzt wird der echte
    Pre-Market-Kurs berücksichtigt.
    """
    now_vienna = datetime.datetime.now(TZ_VIENNA)
    hour = now_vienna.hour

    # Zeitzonen-Hinweis: ET = MEZ - 6h (Winter) / MEZ - 5h (Sommer)
    # Pre-Market ET:  04:00–09:30 = MEZ 10:00–15:30
    # Regular ET:     09:30–16:00 = MEZ 15:30–22:00
    # Post-Market ET: 16:00–20:00 = MEZ 22:00–02:00

    pre_market_price  = meta.get("preMarketPrice")
    post_market_price = meta.get("postMarketPrice")
    regular_price     = meta.get("regularMarketPrice")

    # Tatsächlich letzte Candle aus Zeitreihendaten – immer am aktuellsten
    last_close = all_valid_closes[-1] if all_valid_closes else None

    # Bevorzuge den letzten Candle-Close, weil der immer aktuell ist
    # (schließt Pre-Market ein wenn includePrePost=true)
    if last_close and last_close > 0:
        return last_close

    # Fallback: explizite Pre/Post/Regular-Market-Felder aus Meta
    if pre_market_price and pre_market_price > 0:
        return pre_market_price
    if post_market_price and post_market_price > 0:
        return post_market_price
    if regular_price and regular_price > 0:
        return regular_price

    return 0

def _fetch_stock_data():
    symbols = _all_symbols()
    futures = ["ES=F", "NQ=F"]
    all_syms = symbols + futures

    result = {}
    for sym in symbols:
        result[sym] = {
            "current": 0, "prev_close": 0, "open": 0,
            "change_pct": 0, "prices": [], "labels": [],
            "high": 0, "low": 0,
        }
    for fut_sym, fut_name in [("ES=F", "ES Future"), ("NQ=F", "NQ Future")]:
        result[fut_sym] = {
            "name": fut_name, "current": 0, "prev_close": 0,
            "open": 0, "change_pct": 0, "prices": [], "labels": [],
            "high": 0, "low": 0,
        }

    for sym in all_syms:
        try:
            data       = _yahoo_chart_api(sym, interval="5m", range_str="5d")
            chart      = data.get("chart", {}).get("result", [])
            if not chart:
                print(f"No chart data for {sym}")
                continue

            chart_data = chart[0]
            meta       = chart_data.get("meta", {})
            timestamps = chart_data.get("timestamp", [])
            indicators = chart_data.get("indicators", {}).get("quote", [{}])[0]

            if not timestamps or not indicators.get("close"):
                print(f"No timestamp/close data for {sym}")
                continue

            closes = indicators.get("close", [])
            highs  = indicators.get("high",  [])
            lows   = indicators.get("low",   [])

            # Vorheriger regulärer Schlusskurs als Referenz für % Change
            prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")

            is_future   = sym in futures
            now_vienna  = datetime.datetime.now(TZ_VIENNA)
            today       = now_vienna.date()

            # Stocks: Pre-Market beginnt 10:00 MEZ (= 04:00 ET)
            # Futures: laufen fast 24h – ab Mitternacht zeigen
            if is_future:
                session_start = datetime.datetime.combine(
                    today, datetime.time(0, 0), tzinfo=TZ_VIENNA)
            else:
                session_start = datetime.datetime.combine(
                    today, datetime.time(10, 0), tzinfo=TZ_VIENNA)

            today_prices      = []
            today_labels      = []
            all_valid_closes  = []

            for i, ts in enumerate(timestamps):
                close_val = closes[i] if i < len(closes) else None
                if close_val is None:
                    continue
                all_valid_closes.append(close_val)
                dt = datetime.datetime.fromtimestamp(ts, tz=TZ_VIENNA)
                if dt >= session_start:
                    today_prices.append(round(close_val, 2))
                    today_labels.append(dt.strftime("%H:%M"))

            # Kein heutiger Datenpunkt → letzten verfügbaren Handelstag nutzen
            if not today_prices and timestamps:
                last_ts   = datetime.datetime.fromtimestamp(timestamps[-1], tz=TZ_VIENNA)
                last_date = last_ts.date()
                for i, ts in enumerate(timestamps):
                    close_val = closes[i] if i < len(closes) else None
                    if close_val is None:
                        continue
                    dt = datetime.datetime.fromtimestamp(ts, tz=TZ_VIENNA)
                    if dt.date() == last_date:
                        today_prices.append(round(close_val, 2))
                        today_labels.append(dt.strftime("%H:%M"))

            # ── BUGFIX: aktuellsten Preis inkl. Pre-Market holen ──────────
            current_price = _get_current_price(meta, all_valid_closes)
            # ─────────────────────────────────────────────────────────────

            open_price = today_prices[0] if today_prices else (meta.get("regularMarketOpen") or 0)

            if prev_close and prev_close > 0:
                change_pct = ((current_price - prev_close) / prev_close) * 100
            elif open_price and open_price > 0:
                change_pct = ((current_price - open_price) / open_price) * 100
            else:
                change_pct = 0

            # Aktuellen Preis auch in die Chart-Reihe einfügen wenn neuer als letzter Punkt
            if today_prices and current_price > 0 and current_price != today_prices[-1]:
                today_prices.append(round(current_price, 2))
                today_labels.append(now_vienna.strftime("%H:%M"))

            processed = {
                "current":    round(current_price, 2),
                "prev_close": round(prev_close, 2) if prev_close else round(open_price, 2),
                "open":       round(open_price, 2),
                "change_pct": round(change_pct, 2),
                "prices":     today_prices,
                "labels":     today_labels,
                "high":       round(max([h for h in highs if h is not None] or [0]), 2),
                "low":        round(min([l for l in lows  if l is not None and l > 0] or [0]), 2),
            }

            if sym in futures:
                processed["name"] = "ES Future" if sym == "ES=F" else "NQ Future"

            result[sym] = {**result[sym], **processed}
            print(f"  {sym}: price={processed['current']}, change={processed['change_pct']}%, points={len(today_prices)}")
            time.sleep(0.3)

        except Exception as e:
            print(f"Error fetching {sym}: {e}")

    return result


def _fetch_news():
    all_news       = {}
    symbols_names  = {}
    for s in SP500_TOP10 + NQ100_TOP10:
        symbols_names[s["symbol"]] = s["name"]
    for sym, name in symbols_names.items():
        try:
            query = name.replace(" ", "+") + "+Aktie"
            url   = f"https://news.google.com/rss/search?q={query}&hl=de&gl=DE&ceid=DE:de"
            feed  = feedparser.parse(url)
            articles = []
            for entry in feed.entries[:3]:
                articles.append({
                    "title":     entry.get("title", ""),
                    "link":      entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source":    entry.get("source", {}).get("title", "") if isinstance(entry.get("source"), dict) else "",
                })
            all_news[sym] = articles
        except Exception as e:
            print(f"Error fetching news for {sym}: {e}")
            all_news[sym] = []
    return all_news


def _translate_event_title(title):
    translations = {
        "GDP q/q": "Bruttoinlandsprodukt (BIP) (Quartal)",
        "GDP m/m": "BIP (Monat)", "GDP y/y": "BIP (Jahr)",
        "Prelim GDP q/q": "Bruttoinlandsprodukt (BIP) (Quartal) P",
        "Advance GDP q/q": "BIP Erstschätzung (Quartal)",
        "Final GDP q/q": "BIP Endgültig (Quartal)",
        "Prelim GDP Price Index q/q": "BIP-Preisindex (Quartal) P",
        "GDP Price Index q/q": "BIP-Preisindex (Quartal)",
        "Non-Farm Employment Change": "Beschäftigung außerhalb der Landwirtschaft",
        "Employment Change": "Beschäftigungsänderung",
        "Unemployment Rate": "Arbeitslosenquote",
        "Unemployment Claims": "Erstanträge Arbeitslosenhilfe",
        "Average Hourly Earnings m/m": "Durchschnittliche Stundenlöhne (Monat)",
        "Average Hourly Earnings y/y": "Durchschnittliche Stundenlöhne (Jahr)",
        "ADP Non-Farm Employment Change": "ADP Beschäftigung außerh. Landwirtschaft",
        "JOLTS Job Openings": "JOLTS Stellenangebote",
        "Nonfarm Payrolls": "Beschäftigung außerh. Landwirtschaft (NFP)",
        "CPI m/m": "Verbraucherpreisindex (Monat)", "CPI y/y": "Verbraucherpreisindex (Jahr)",
        "Core CPI m/m": "Kernrate Verbraucherpreisindex (Monat)",
        "Core CPI y/y": "Kernrate Verbraucherpreisindex (Jahr)",
        "PPI m/m": "Erzeugerpreisindex (Monat)", "PPI y/y": "Erzeugerpreisindex (Jahr)",
        "Core PPI m/m": "Kernrate Erzeugerpreisindex (Monat)",
        "Core PPI y/y": "Kernrate Erzeugerpreisindex (Jahr)",
        "PCE Price Index m/m": "PCE-Preisindex (Monat)",
        "PCE Price Index y/y": "PCE-Preisindex (Jahr)",
        "Core PCE Price Index m/m": "PCE-Kernrate Preisindex (Monat)",
        "Core PCE Price Index y/y": "PCE-Kernrate Preisindex (Jahr)",
        "Federal Funds Rate": "US-Leitzins (Fed Funds Rate)",
        "FOMC Statement": "FOMC Zinsentscheid / Statement",
        "FOMC Meeting Minutes": "FOMC Sitzungsprotokoll",
        "FOMC Press Conference": "FOMC Pressekonferenz",
        "Interest Rate Decision": "Zinsentscheid",
        "ECB Press Conference": "EZB Pressekonferenz",
        "ECB Interest Rate Decision": "EZB Zinsentscheid",
        "BOE Interest Rate Decision": "BoE Zinsentscheid",
        "BOJ Interest Rate Decision": "BoJ Zinsentscheid",
        "Monetary Policy Statement": "Geldpolitisches Statement",
        "Retail Sales m/m": "Einzelhandelsumsätze (Monat)",
        "Retail Sales y/y": "Einzelhandelsumsätze (Jahr)",
        "Core Retail Sales m/m": "Kernrate Einzelhandelsumsätze (Monat)",
        "Consumer Confidence": "Verbrauchervertrauen",
        "CB Consumer Confidence": "CB Verbrauchervertrauen",
        "Michigan Consumer Sentiment": "Verbraucherstimmung Michigan",
        "Prelim UoM Consumer Sentiment": "Verbraucherstimmung Michigan (vorläufig)",
        "Revised UoM Consumer Sentiment": "Verbraucherstimmung Michigan (revidiert)",
        "Prelim UoM Inflation Expectations": "Inflationserwartungen Michigan (vorl.)",
        "Personal Spending m/m": "Persönliche Ausgaben (Monat)",
        "Personal Income m/m": "Persönliches Einkommen (Monat)",
        "ISM Manufacturing PMI": "ISM Einkaufsmanagerindex Produktion",
        "ISM Services PMI": "ISM Einkaufsmanagerindex Dienstleistung",
        "ISM Manufacturing Prices": "ISM Produktion Preise",
        "Industrial Production m/m": "Industrieproduktion (Monat)",
        "Manufacturing PMI": "Einkaufsmanagerindex Produktion",
        "Services PMI": "Einkaufsmanagerindex Dienstleistung",
        "Flash Manufacturing PMI": "Einkaufsmanagerindex Prod. (Schnellsch.)",
        "Flash Services PMI": "Einkaufsmanagerindex Dienstl. (Schnellsch.)",
        "Durable Goods Orders m/m": "Aufträge langlebiger Güter (Monat)",
        "Core Durable Goods Orders m/m": "Kernrate Aufträge langleb. Güter (Monat)",
        "Factory Orders m/m": "Fabrikaufträge (Monat)",
        "Capacity Utilization Rate": "Kapazitätsauslastung",
        "Existing Home Sales": "Verkäufe bestehender Häuser",
        "New Home Sales": "Neubauverkäufe",
        "Building Permits": "Baugenehmigungen",
        "Housing Starts": "Baubeginne",
        "Pending Home Sales m/m": "Schwebende Hausverkäufe (Monat)",
        "Trade Balance": "Handelsbilanz", "Current Account": "Leistungsbilanz",
        "Crude Oil Inventories": "Rohöl-Lagerbestände",
        "Natural Gas Storage": "Erdgas-Lagerbestände",
        "Federal Budget Balance": "Bundeshaushaltssaldo",
        "Beige Book": "Beige Book (Konjunkturbericht)",
    }
    if title in translations:
        return translations[title]
    title_lower = title.lower()
    for en, de in translations.items():
        if en.lower() == title_lower:
            return de
    patterns = [
        ("gdp", "BIP"), ("unemployment rate", "Arbeitslosenquote"),
        ("employment change", "Beschäftigungsänderung"),
        ("consumer price index", "Verbraucherpreisindex"),
        ("interest rate", "Zinsentscheid"), ("retail sales", "Einzelhandelsumsätze"),
        ("trade balance", "Handelsbilanz"), ("inflation rate", "Inflationsrate"),
        ("manufacturing pmi", "Einkaufsmanagerindex Produktion"),
        ("services pmi", "Einkaufsmanagerindex Dienstleistung"),
        ("industrial production", "Industrieproduktion"),
        ("consumer confidence", "Verbrauchervertrauen"),
        ("housing", "Immobilien"), ("building permits", "Baugenehmigungen"),
    ]
    for pattern, de_prefix in patterns:
        if pattern in title_lower:
            return de_prefix
    return title


def _save_calendar_to_file(raw_data, events):
    try:
        cache = {
            "date":       str(datetime.date.today()),
            "fetched_at": datetime.datetime.now(TZ_VIENNA).isoformat(),
            "raw_data":   raw_data,
            "events":     events,
        }
        with open(CALENDAR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"Calendar saved to file cache ({len(events)} events)")
    except Exception as e:
        print(f"Error saving calendar cache: {e}")


def _load_calendar_from_file():
    try:
        if CALENDAR_CACHE_FILE.exists():
            with open(CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("date") == str(datetime.date.today()):
                events = cache.get("events", [])
                print(f"Calendar loaded from file cache ({len(events)} events)")
                return events
    except Exception as e:
        print(f"Error loading calendar cache: {e}")
    return None


def _parse_ff_events(raw_data):
    events = []
    today  = datetime.date.today()
    country_map = {
        "USD": "USA", "EUR": "Eurozone", "GBP": "Großbritannien",
        "JPY": "Japan", "CAD": "Kanada", "AUD": "Australien",
        "NZD": "Neuseeland", "CHF": "Schweiz", "CNY": "China",
    }
    for event in raw_data:
        if event.get("impact", "") != "High":
            continue
        event_date_str = event.get("date", "")
        try:
            event_date = datetime.datetime.fromisoformat(event_date_str).date()
        except (ValueError, TypeError):
            try:
                event_date = datetime.datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
        if event_date != today:
            continue
        try:
            event_dt = datetime.datetime.fromisoformat(event_date_str)
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=ZoneInfo("America/New_York"))
            vienna_dt  = event_dt.astimezone(TZ_VIENNA)
            event_time = vienna_dt.strftime("%H:%M")
        except (ValueError, TypeError):
            event_time = ""

        def _de_number(val):
            if not val or not isinstance(val, str):
                return val or ""
            return val.replace(".", ",") if "." in val else val

        events.append({
            "time":     event_time,
            "country":  country_map.get(event.get("country", ""), event.get("country", "")),
            "currency": event.get("country", ""),
            "title":    _translate_event_title(event.get("title", "")),
            "impact":   "high",
            "forecast": _de_number(event.get("forecast", "")),
            "previous": _de_number(event.get("previous", "")),
            "actual":   _de_number(event.get("actual", "")),
        })
    events.sort(key=lambda x: x.get("time", "99:99"))
    return events


def _fetch_economic_calendar():
    try:
        url  = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept":     "application/json, text/plain, */*",
        }
        resp = http_requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            raw_data = resp.json()
            events   = _parse_ff_events(raw_data)
            _save_calendar_to_file(raw_data, events)
            print(f"Forex Factory: fetched {len(events)} high-impact events for today")
            return events
        print(f"Forex Factory returned status {resp.status_code}, trying file cache...")
    except Exception as e:
        print(f"Error fetching Forex Factory: {e}, trying file cache...")
    cached = _load_calendar_from_file()
    if cached is not None:
        return cached
    print("No calendar data available")
    return []


def get_stock_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["stock_data"] and _cache["last_stock_update"] and \
                (now - _cache["last_stock_update"] < STOCK_CACHE_SECONDS):
            return _cache["stock_data"]
    data = _fetch_stock_data()
    with _cache_lock:
        _cache["stock_data"]        = data
        _cache["last_stock_update"] = time.time()
    return data


def get_news_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["news_data"] and _cache["last_news_update"] and \
                (now - _cache["last_news_update"] < NEWS_CACHE_SECONDS):
            return _cache["news_data"]
    data = _fetch_news()
    with _cache_lock:
        _cache["news_data"]        = data
        _cache["last_news_update"] = time.time()
    return data


def get_calendar_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["calendar_data"] and _cache["last_calendar_update"] and \
                (now - _cache["last_calendar_update"] < CALENDAR_CACHE_SECONDS):
            return _cache["calendar_data"]
    data = _fetch_economic_calendar()
    with _cache_lock:
        _cache["calendar_data"]        = data
        _cache["last_calendar_update"] = time.time()
    return data


def _calc_probability(top10, stock_data):
    up = 0
    total_weight_up = 0
    total_weight    = 0
    for s in top10:
        sym = s["symbol"]
        w   = s["weight"]
        total_weight += w
        if sym in stock_data and stock_data[sym]["change_pct"] > 0:
            up              += 1
            total_weight_up += w
    prob = (total_weight_up / total_weight * 100) if total_weight > 0 else 50
    return {
        "long_pct":   round(prob, 1),
        "short_pct":  round(100 - prob, 1),
        "up_count":   up,
        "down_count": 10 - up,
        "signal":     "LONG" if prob > 50 else "SHORT" if prob < 50 else "NEUTRAL",
    }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    force        = request.args.get("force", "false").lower() == "true"
    stock_data   = get_stock_data(force=force)
    news_data    = get_news_data(force=force)
    calendar_data = get_calendar_data(force=force)
    sp500_prob   = _calc_probability(SP500_TOP10, stock_data)
    nq100_prob   = _calc_probability(NQ100_TOP10, stock_data)

    sp500_stocks = []
    for s in SP500_TOP10:
        sym = s["symbol"]
        sp500_stocks.append({**s, **stock_data.get(sym, {}), "news": news_data.get(sym, [])})

    nq100_stocks = []
    for s in NQ100_TOP10:
        sym = s["symbol"]
        nq100_stocks.append({**s, **stock_data.get(sym, {}), "news": news_data.get(sym, [])})

    return jsonify({
        "timestamp": datetime.datetime.now(TZ_VIENNA).strftime("%Y-%m-%d %H:%M:%S") + " MEZ",
        "futures": {
            "ES": stock_data.get("ES=F", {}),
            "NQ": stock_data.get("NQ=F", {}),
        },
        "sp500": {
            "stocks":      sp500_stocks,
            "probability": sp500_prob,
        },
        "nq100": {
            "stocks":      nq100_stocks,
            "probability": nq100_prob,
        },
        "calendar": calendar_data,
    })


@app.after_request
def add_headers(response):
    response.headers["X-Frame-Options"]        = "ALLOW-FROM https://www.rg-trading.at"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://www.rg-trading.at https://rg-trading.at"
    return response


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)

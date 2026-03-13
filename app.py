"""
Market Dashboard - ES Future & NQ Future Daily Analysis
Zeigt die Top 10 gewichteten Unternehmen, deren Tagesperformance,
News und eine Long/Short-Wahrscheinlichkeit.
"""

from flask import Flask, render_template, jsonify, request
import yfinance as yf
import feedparser
import requests
import datetime
import threading
import time
import re
import json
import os
from zoneinfo import ZoneInfo
from pathlib import Path

# Zeitzone für Österreich/Deutschland (MEZ/MESZ)
TZ_VIENNA = ZoneInfo("Europe/Vienna")

# Persistent cache file for economic calendar
CALENDAR_CACHE_FILE = Path(__file__).parent / "calendar_cache.json"

app = Flask(__name__)

# ── Top 10 Gewichtungen ──────────────────────────────────────────────

SP500_TOP10 = [
    {"symbol": "NVDA",  "name": "Nvidia",              "weight": 7.81},
    {"symbol": "AAPL",  "name": "Apple",               "weight": 6.65},
    {"symbol": "MSFT",  "name": "Microsoft",           "weight": 5.20},
    {"symbol": "AMZN",  "name": "Amazon",              "weight": 3.57},
    {"symbol": "GOOGL", "name": "Alphabet A",          "weight": 3.10},
    {"symbol": "AVGO",  "name": "Broadcom",            "weight": 2.79},
    {"symbol": "GOOG",  "name": "Alphabet C",          "weight": 2.48},
    {"symbol": "META",  "name": "Meta Platforms",       "weight": 2.46},
    {"symbol": "TSLA",  "name": "Tesla",               "weight": 1.98},
    {"symbol": "BRK-B", "name": "Berkshire Hathaway B", "weight": 1.56},
]

NQ100_TOP10 = [
    {"symbol": "NVDA",  "name": "Nvidia",        "weight": 8.90},
    {"symbol": "AAPL",  "name": "Apple",         "weight": 7.35},
    {"symbol": "MSFT",  "name": "Microsoft",     "weight": 6.13},
    {"symbol": "AMZN",  "name": "Amazon",        "weight": 4.90},
    {"symbol": "META",  "name": "Meta Platforms", "weight": 4.03},
    {"symbol": "GOOGL", "name": "Alphabet A",    "weight": 3.77},
    {"symbol": "TSLA",  "name": "Tesla",         "weight": 3.65},
    {"symbol": "GOOG",  "name": "Alphabet C",    "weight": 3.51},
    {"symbol": "WMT",   "name": "Walmart",       "weight": 3.08},
    {"symbol": "AVGO",  "name": "Broadcom",      "weight": 3.00},
]

# ── Cache ─────────────────────────────────────────────────────────────

_cache = {
    "stock_data": None,
    "news_data": None,
    "calendar_data": None,
    "last_stock_update": None,
    "last_news_update": None,
    "last_calendar_update": None,
}
_cache_lock = threading.Lock()

STOCK_CACHE_SECONDS = 60
NEWS_CACHE_SECONDS = 300
CALENDAR_CACHE_SECONDS = 3600  # 1 Stunde — Kalender ändert sich selten


def _all_symbols():
    syms = set()
    for s in SP500_TOP10 + NQ100_TOP10:
        syms.add(s["symbol"])
    return sorted(syms)


def _process_symbol_data(hist, daily_hist, sym):
    """Process downloaded data for a single symbol into result dict."""
    if hist.empty:
        return {
            "current": 0, "prev_close": 0, "open": 0,
            "change_pct": 0, "prices": [], "labels": [],
            "high": 0, "low": 0,
        }

    prev_close = None
    if not daily_hist.empty:
        if len(daily_hist) >= 2:
            prev_close = float(daily_hist["Close"].iloc[-2])
        elif len(daily_hist) == 1:
            prev_close = float(daily_hist["Open"].iloc[0])

    current_price = float(hist["Close"].iloc[-1])
    open_price = float(hist["Open"].iloc[0])

    if prev_close and prev_close > 0:
        change_pct = ((current_price - prev_close) / prev_close) * 100
    else:
        change_pct = ((current_price - open_price) / open_price) * 100 if open_price > 0 else 0

    prices = []
    labels = []
    for idx, row in hist.iterrows():
        ts = idx
        if hasattr(ts, "astimezone"):
            vienna_ts = ts.astimezone(TZ_VIENNA)
            labels.append(vienna_ts.strftime("%H:%M"))
        elif hasattr(ts, "strftime"):
            labels.append(ts.strftime("%H:%M"))
        else:
            labels.append(str(ts))
        prices.append(round(float(row["Close"]), 2))

    return {
        "current": round(current_price, 2),
        "prev_close": round(prev_close, 2) if prev_close else round(open_price, 2),
        "open": round(open_price, 2),
        "change_pct": round(change_pct, 2),
        "prices": prices,
        "labels": labels,
        "high": round(float(hist["High"].max()), 2),
        "low": round(float(hist["Low"].min()), 2),
    }


def _fetch_stock_data():
    """Fetch intraday data for all symbols using bulk download to avoid rate limiting."""
    symbols = _all_symbols()
    futures = ["ES=F", "NQ=F"]
    all_syms = symbols + futures
    all_syms_str = " ".join(all_syms)
    result = {}

    # Initialize empty results for all symbols
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

    try:
        # BULK download: 1 request for intraday data (all symbols at once)
        print(f"Bulk downloading intraday data for {len(all_syms)} symbols...")
        intraday = yf.download(all_syms_str, period="5d", interval="5m", group_by="ticker", threads=True)
        print(f"Intraday download complete. Shape: {intraday.shape}")

        # BULK download: 1 request for daily data (for prev_close)
        print(f"Bulk downloading daily data...")
        daily = yf.download(all_syms_str, period="5d", interval="1d", group_by="ticker", threads=True)
        print(f"Daily download complete. Shape: {daily.shape}")

        # Process each symbol
        for sym in all_syms:
            try:
                # Extract symbol data from multi-level DataFrame
                if len(all_syms) == 1:
                    sym_intraday = intraday
                    sym_daily = daily
                else:
                    if sym in intraday.columns.get_level_values(0):
                        sym_intraday = intraday[sym].dropna(subset=["Close"])
                    else:
                        sym_intraday = intraday.xs(sym, level=0, axis=1).dropna(subset=["Close"]) if sym in intraday.columns else None

                    if sym in daily.columns.get_level_values(0):
                        sym_daily = daily[sym].dropna(subset=["Close"])
                    else:
                        sym_daily = daily.xs(sym, level=0, axis=1).dropna(subset=["Close"]) if sym in daily.columns else None

                if sym_intraday is None or sym_intraday.empty:
                    print(f"No intraday data for {sym}")
                    continue

                if sym_daily is None:
                    sym_daily = sym_intraday  # fallback

                processed = _process_symbol_data(sym_intraday, sym_daily, sym)

                if sym in futures:
                    fut_name = "ES Future" if sym == "ES=F" else "NQ Future"
                    processed["name"] = fut_name

                result[sym] = {**result[sym], **processed}
                print(f"  {sym}: price={processed['current']}, change={processed['change_pct']}%")

            except Exception as e:
                print(f"Error processing {sym}: {e}")

    except Exception as e:
        print(f"Error in bulk download: {e}")
        # Fallback: try individual downloads with delays
        print("Falling back to individual downloads with delays...")
        for sym in all_syms:
            try:
                time.sleep(1)  # 1 second delay between requests
                ticker = yf.Ticker(sym)
                hist = ticker.history(period="5d", interval="5m")
                time.sleep(0.5)
                daily_hist = ticker.history(period="5d", interval="1d")

                processed = _process_symbol_data(hist, daily_hist, sym)

                if sym in futures:
                    fut_name = "ES Future" if sym == "ES=F" else "NQ Future"
                    processed["name"] = fut_name

                result[sym] = {**result[sym], **processed}
                print(f"  {sym}: price={processed['current']}, change={processed['change_pct']}%")

            except Exception as e:
                print(f"Error fetching {sym}: {e}")

    return result


def _fetch_news():
    """Fetch news from Google News RSS in GERMAN for each company."""
    all_news = {}
    symbols_names = {}
    for s in SP500_TOP10 + NQ100_TOP10:
        symbols_names[s["symbol"]] = s["name"]

    for sym, name in symbols_names.items():
        try:
            query = name.replace(" ", "+") + "+Aktie"
            # German language Google News RSS
            url = f"https://news.google.com/rss/search?q={query}&hl=de&gl=DE&ceid=DE:de"
            feed = feedparser.parse(url)
            articles = []
            for entry in feed.entries[:3]:
                articles.append({
                    "title": entry.get("title", ""),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source": entry.get("source", {}).get("title", "") if isinstance(entry.get("source"), dict) else "",
                })
            all_news[sym] = articles
        except Exception as e:
            print(f"Error fetching news for {sym}: {e}")
            all_news[sym] = []

    return all_news


def _translate_event_title(title):
    """Translate economic calendar event titles to German (like Investing.com)."""
    # Exact match translations
    translations = {
        # ── GDP / BIP ──
        "GDP q/q": "Bruttoinlandsprodukt (BIP) (Quartal)",
        "GDP m/m": "BIP (Monat)",
        "GDP y/y": "BIP (Jahr)",
        "Prelim GDP q/q": "Bruttoinlandsprodukt (BIP) (Quartal) P",
        "Advance GDP q/q": "BIP Erstschätzung (Quartal)",
        "Final GDP q/q": "BIP Endgültig (Quartal)",
        "Prelim GDP Price Index q/q": "BIP-Preisindex (Quartal) P",
        "GDP Price Index q/q": "BIP-Preisindex (Quartal)",
        # ── Arbeitsmarkt ──
        "Non-Farm Employment Change": "Beschäftigung außerhalb der Landwirtschaft",
        "Employment Change": "Beschäftigungsänderung",
        "Unemployment Rate": "Arbeitslosenquote",
        "Unemployment Claims": "Erstanträge Arbeitslosenhilfe",
        "Average Hourly Earnings m/m": "Durchschnittliche Stundenlöhne (Monat)",
        "Average Hourly Earnings y/y": "Durchschnittliche Stundenlöhne (Jahr)",
        "ADP Non-Farm Employment Change": "ADP Beschäftigung außerh. Landwirtschaft",
        "JOLTS Job Openings": "JOLTS Stellenangebote",
        "Job Openings": "Stellenangebote",
        "Nonfarm Payrolls": "Beschäftigung außerh. Landwirtschaft (NFP)",
        # ── Inflation / Preise ──
        "CPI m/m": "Verbraucherpreisindex (Monat)",
        "CPI y/y": "Verbraucherpreisindex (Jahr)",
        "Core CPI m/m": "Kernrate Verbraucherpreisindex (Monat)",
        "Core CPI y/y": "Kernrate Verbraucherpreisindex (Jahr)",
        "PPI m/m": "Erzeugerpreisindex (Monat)",
        "PPI y/y": "Erzeugerpreisindex (Jahr)",
        "Core PPI m/m": "Kernrate Erzeugerpreisindex (Monat)",
        "Core PPI y/y": "Kernrate Erzeugerpreisindex (Jahr)",
        "PCE Price Index m/m": "PCE-Preisindex (Monat)",
        "PCE Price Index y/y": "PCE-Preisindex (Jahr)",
        "Core PCE Price Index m/m": "PCE-Kernrate Preisindex (Monat)",
        "Core PCE Price Index y/y": "PCE-Kernrate Preisindex (Jahr)",
        # ── Zinsen / Notenbank ──
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
        # ── Einzelhandel / Konsum ──
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
        # ── Industrie / Produktion ──
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
        # ── Immobilien ──
        "Existing Home Sales": "Verkäufe bestehender Häuser",
        "New Home Sales": "Neubauverkäufe",
        "Building Permits": "Baugenehmigungen",
        "Housing Starts": "Baubeginne",
        "Pending Home Sales m/m": "Schwebende Hausverkäufe (Monat)",
        # ── Handel ──
        "Trade Balance": "Handelsbilanz",
        "Current Account": "Leistungsbilanz",
        # ── Sonstiges ──
        "Crude Oil Inventories": "Rohöl-Lagerbestände",
        "Natural Gas Storage": "Erdgas-Lagerbestände",
        "Treasury Currency Report": "Finanzministerium Währungsbericht",
        "Federal Budget Balance": "Bundeshaushaltssaldo",
        "Beige Book": "Beige Book (Konjunkturbericht)",
        "S&P/CS Composite-20 HPI y/y": "S&P/CS Häuserpreisindex (Jahr)",
    }

    # Check exact match first
    if title in translations:
        return translations[title]

    # Check case-insensitive
    title_lower = title.lower()
    for en, de in translations.items():
        if en.lower() == title_lower:
            return de

    # Partial match / pattern based translations
    patterns = [
        ("gdp", "BIP"),
        ("unemployment rate", "Arbeitslosenquote"),
        ("employment change", "Beschäftigungsänderung"),
        ("consumer price index", "Verbraucherpreisindex"),
        ("interest rate", "Zinsentscheid"),
        ("retail sales", "Einzelhandelsumsätze"),
        ("trade balance", "Handelsbilanz"),
        ("inflation rate", "Inflationsrate"),
        ("manufacturing pmi", "Einkaufsmanagerindex Produktion"),
        ("services pmi", "Einkaufsmanagerindex Dienstleistung"),
        ("industrial production", "Industrieproduktion"),
        ("consumer confidence", "Verbrauchervertrauen"),
        ("business confidence", "Geschäftsvertrauen"),
        ("housing", "Immobilien"),
        ("building permits", "Baugenehmigungen"),
    ]

    for pattern, de_prefix in patterns:
        if pattern in title_lower:
            return de_prefix

    # No translation found — return original
    return title


def _save_calendar_to_file(raw_data, events):
    """Save raw API data and processed events to persistent cache file."""
    try:
        cache = {
            "date": str(datetime.date.today()),
            "fetched_at": datetime.datetime.now(TZ_VIENNA).isoformat(),
            "raw_data": raw_data,
            "events": events,
        }
        with open(CALENDAR_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f"Calendar saved to file cache ({len(events)} events)")
    except Exception as e:
        print(f"Error saving calendar cache: {e}")


def _load_calendar_from_file():
    """Load cached calendar from file if available for today."""
    try:
        if CALENDAR_CACHE_FILE.exists():
            with open(CALENDAR_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("date") == str(datetime.date.today()):
                events = cache.get("events", [])
                print(f"Calendar loaded from file cache ({len(events)} events, fetched at {cache.get('fetched_at', '?')})")
                return events
            else:
                print("Calendar file cache is from a different day, ignoring")
    except Exception as e:
        print(f"Error loading calendar cache: {e}")
    return None


def _parse_ff_events(raw_data):
    """Parse Forex Factory raw data into translated German events for today."""
    events = []
    today = datetime.date.today()

    country_map = {
        "USD": "USA", "EUR": "Eurozone", "GBP": "Großbritannien",
        "JPY": "Japan", "CAD": "Kanada", "AUD": "Australien",
        "NZD": "Neuseeland", "CHF": "Schweiz", "CNY": "China",
    }

    for event in raw_data:
        impact = event.get("impact", "")
        if impact != "High":
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

        # Convert to MEZ
        try:
            event_dt = datetime.datetime.fromisoformat(event_date_str)
            if event_dt.tzinfo is None:
                event_dt = event_dt.replace(tzinfo=ZoneInfo("America/New_York"))
            vienna_dt = event_dt.astimezone(TZ_VIENNA)
            event_time = vienna_dt.strftime("%H:%M")
        except (ValueError, TypeError):
            event_time = ""

        country = event.get("country", "")
        original_title = event.get("title", "")
        german_title = _translate_event_title(original_title)

        # Format numbers with German locale (comma as decimal separator)
        def _de_number(val):
            if not val or not isinstance(val, str):
                return val or ""
            return val.replace(".", ",") if val.replace(".", "").replace("-", "").replace("%", "").replace("K", "").replace("M", "").replace("B", "").replace("T", "").strip().replace(",", "").isdigit() or "." in val else val

        events.append({
            "time": event_time,
            "country": country_map.get(country, country),
            "currency": country,
            "title": german_title,
            "impact": "high",
            "forecast": _de_number(event.get("forecast", "")),
            "previous": _de_number(event.get("previous", "")),
            "actual": _de_number(event.get("actual", "")),
        })

    events.sort(key=lambda x: x.get("time", "99:99"))
    return events


def _fetch_economic_calendar():
    """Fetch today's high-impact economic events. Uses file cache as fallback."""
    events = []

    # 1) Try fetching from Forex Factory
    try:
        url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=15)

        if resp.status_code == 200:
            raw_data = resp.json()
            events = _parse_ff_events(raw_data)
            # Save to persistent file cache
            _save_calendar_to_file(raw_data, events)
            print(f"Forex Factory: fetched {len(events)} high-impact events for today")
            return events
        else:
            print(f"Forex Factory returned status {resp.status_code}, trying file cache...")

    except Exception as e:
        print(f"Error fetching Forex Factory: {e}, trying file cache...")

    # 2) Fallback: load from persistent file cache
    cached = _load_calendar_from_file()
    if cached is not None:
        return cached

    # 3) If no cache, return empty (will show "keine Termine" message)
    print("No calendar data available (API unreachable and no file cache)")
    return []


def get_stock_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["stock_data"] and _cache["last_stock_update"] and (now - _cache["last_stock_update"] < STOCK_CACHE_SECONDS):
            return _cache["stock_data"]

    data = _fetch_stock_data()
    with _cache_lock:
        _cache["stock_data"] = data
        _cache["last_stock_update"] = now
    return data


def get_news_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["news_data"] and _cache["last_news_update"] and (now - _cache["last_news_update"] < NEWS_CACHE_SECONDS):
            return _cache["news_data"]

    data = _fetch_news()
    with _cache_lock:
        _cache["news_data"] = data
        _cache["last_news_update"] = now
    return data


def get_calendar_data(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["calendar_data"] and _cache["last_calendar_update"] and (now - _cache["last_calendar_update"] < CALENDAR_CACHE_SECONDS):
            return _cache["calendar_data"]

    data = _fetch_economic_calendar()
    with _cache_lock:
        _cache["calendar_data"] = data
        _cache["last_calendar_update"] = now
    return data


def _calc_probability(top10, stock_data):
    """Calculate long/short probability based on how many top 10 are green."""
    up = 0
    total_weight_up = 0
    total_weight = 0
    for s in top10:
        sym = s["symbol"]
        w = s["weight"]
        total_weight += w
        if sym in stock_data and stock_data[sym]["change_pct"] > 0:
            up += 1
            total_weight_up += w

    if total_weight > 0:
        prob = (total_weight_up / total_weight) * 100
    else:
        prob = 50

    return {
        "long_pct": round(prob, 1),
        "short_pct": round(100 - prob, 1),
        "up_count": up,
        "down_count": 10 - up,
        "signal": "LONG" if prob > 50 else "SHORT" if prob < 50 else "NEUTRAL",
    }


# ── Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    # force=true parameter bypasses cache (for manual refresh)
    force = request.args.get("force", "false").lower() == "true"

    stock_data = get_stock_data(force=force)
    news_data = get_news_data(force=force)
    calendar_data = get_calendar_data(force=force)

    sp500_prob = _calc_probability(SP500_TOP10, stock_data)
    nq100_prob = _calc_probability(NQ100_TOP10, stock_data)

    sp500_stocks = []
    for s in SP500_TOP10:
        sym = s["symbol"]
        sd = stock_data.get(sym, {})
        sp500_stocks.append({**s, **sd, "news": news_data.get(sym, [])})

    nq100_stocks = []
    for s in NQ100_TOP10:
        sym = s["symbol"]
        sd = stock_data.get(sym, {})
        nq100_stocks.append({**s, **sd, "news": news_data.get(sym, [])})

    return jsonify({
        "timestamp": datetime.datetime.now(TZ_VIENNA).strftime("%Y-%m-%d %H:%M:%S") + " MEZ",
        "futures": {
            "ES": stock_data.get("ES=F", {}),
            "NQ": stock_data.get("NQ=F", {}),
        },
        "sp500": {
            "stocks": sp500_stocks,
            "probability": sp500_prob,
        },
        "nq100": {
            "stocks": nq100_stocks,
            "probability": nq100_prob,
        },
        "calendar": calendar_data,
    })


@app.after_request
def add_headers(response):
    """Allow iframe embedding from rg-trading.at"""
    response.headers["X-Frame-Options"] = "ALLOW-FROM https://www.rg-trading.at"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self' https://www.rg-trading.at https://rg-trading.at"
    return response


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(debug=debug, host="0.0.0.0", port=port)

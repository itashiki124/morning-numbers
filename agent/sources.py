"""Public, key-free market data sources.

Each fetcher returns a Quote with the latest *completed* daily value and the
one before it. Nothing is estimated: if a source fails, the Quote carries an
error and no numbers.
"""
import csv
import datetime as dt
import io
import json
import time
import urllib.request

UA = "hacksgiving-agent/0.1 (+numbers-only market brief)"
TIMEOUT = 30

VIX_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
VIX_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=10d&interval=1d"
VIX_TOLERANCE = 0.005  # closes are published to 2 decimals
BTC_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=86400"
SPX_URL = "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=10d&interval=1d"
UST_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           "daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
           "&field_tdr_date_value={year}&page&_format=csv")


class Quote(object):
    def __init__(self, key, label, source, unit=""):
        self.key = key
        self.label = label
        self.source = source
        self.unit = unit
        self.date = None
        self.value = None
        self.prev_date = None
        self.prev_value = None
        self.error = None
        self.check = None  # cross-source check note, e.g. "Cboe-verified 11/20"

    @property
    def ok(self):
        return self.error is None and self.value is not None and self.prev_value is not None

    @property
    def change_pct(self):
        if not self.ok or self.prev_value == 0:
            return None
        return (self.value - self.prev_value) / self.prev_value * 100.0

    def to_dict(self):
        return {
            "key": self.key, "label": self.label, "source": self.source, "unit": self.unit,
            "date": self.date, "value": self.value,
            "prev_date": self.prev_date, "prev_value": self.prev_value,
            "change_pct": None if self.change_pct is None else round(self.change_pct, 4),
            "error": self.error, "check": self.check,
        }


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8")


def _set(q, rows):
    """rows: list of (iso_date, float) sorted ascending, completed days only."""
    if len(rows) < 2:
        raise ValueError("fewer than 2 completed daily values")
    (q.prev_date, q.prev_value), (q.date, q.value) = rows[-2], rows[-1]


def _mdy(s):
    return dt.datetime.strptime(s.strip(), "%m/%d/%Y").date().isoformat()


# ---- parsers (pure, unit-tested with fixtures) ----

def parse_vix_csv(text):
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        close = (rec.get("CLOSE") or "").strip()
        if close:
            rows.append((_mdy(rec["DATE"]), float(close)))
    rows.sort()
    return rows


def parse_coinbase_candles(text, now_utc=None):
    """Coinbase candles: [time, low, high, open, close, volume], newest first.
    The candle for the current UTC day is still open, so it is dropped."""
    now_utc = now_utc or dt.datetime.utcnow()
    today = now_utc.date()
    rows = []
    for c in json.loads(text):
        day = dt.datetime.utcfromtimestamp(int(c[0])).date()
        if day < today:
            rows.append((day.isoformat(), float(c[4])))
    rows.sort()
    return rows


def parse_yahoo_chart(text, now_epoch=None):
    """Daily closes from Yahoo's chart API. While the regular session is open the
    last bar is intraday, so it is dropped."""
    res = json.loads(text)["chart"]["result"][0]
    tz_off = int(res["meta"].get("gmtoffset", 0))
    period = res["meta"].get("currentTradingPeriod", {}).get("regular", {})
    now_epoch = now_epoch if now_epoch is not None else int(time.time())
    session_open = bool(period) and period["start"] <= now_epoch < period["end"]
    session_day = (dt.datetime.utcfromtimestamp(period["start"] + tz_off).date()
                   if period else None)
    rows = []
    for ts, close in zip(res.get("timestamp", []), res["indicators"]["quote"][0]["close"]):
        if close is None:
            continue
        day = dt.datetime.utcfromtimestamp(int(ts) + tz_off).date()
        if session_open and day == session_day:
            continue
        rows.append((day.isoformat(), float(close)))
    rows.sort()
    return rows


def parse_treasury_csv(text, column="10 Yr"):
    rows = []
    for rec in csv.DictReader(io.StringIO(text)):
        v = (rec.get(column) or "").strip()
        if v:
            rows.append((_mdy(rec["Date"]), float(v)))
    rows.sort()
    return rows


# ---- fetchers ----

def cross_check_vix(q, yahoo_rows, cboe_rows):
    """Yahoo ^VIX supplies the latest closes; the official Cboe CSV (which lags by
    about one session at 07:00 JST) confirms whichever of those dates it already has.
    Sets q.check; never changes the numbers."""
    yahoo, cboe = dict(yahoo_rows), dict(cboe_rows)
    checked, bad = [], []
    for d in (q.prev_date, q.date):
        if d in cboe and d in yahoo:
            (checked if abs(cboe[d] - yahoo[d]) <= VIX_TOLERANCE else bad).append(d)
    md = lambda d: d[5:].replace("-", "/")
    if bad:
        q.check = "UNVERIFIED: Cboe differs on " + ", ".join(
            "{} ({:.2f})".format(md(d), cboe[d]) for d in bad)
    elif checked:
        q.check = "Cboe-verified " + ", ".join(md(d) for d in checked)
    else:
        q.check = "not yet in Cboe file"
    return q


def fetch_vix():
    q = Quote("vix", "VIX", "Yahoo ^VIX, checked against Cboe VIX_History.csv")
    try:
        cboe = parse_vix_csv(_get(VIX_URL))
    except Exception as e:  # noqa: BLE001
        cboe, cboe_err = [], "%s: %s" % (type(e).__name__, e)
    else:
        cboe_err = None
    try:
        yahoo = parse_yahoo_chart(_get(VIX_YAHOO_URL))
        _set(q, yahoo)
    except Exception:  # noqa: BLE001 - fall back to the official file alone
        q = Quote("vix", "VIX", "Cboe VIX_History.csv (Yahoo unavailable)")
        try:
            if cboe_err:
                raise RuntimeError(cboe_err)
            _set(q, cboe)
        except Exception as e:  # noqa: BLE001 - report, never invent
            q.error = "%s: %s" % (type(e).__name__, e)
        return q
    if cboe_err:
        q.check = "Cboe check unavailable"
    else:
        cross_check_vix(q, yahoo, cboe)
    return q


def fetch_btc():
    q = Quote("btc", "BTC-USD", "Coinbase Exchange daily candles (UTC close)", "USD")
    try:
        _set(q, parse_coinbase_candles(_get(BTC_URL)))
    except Exception as e:  # noqa: BLE001
        q.error = "%s: %s" % (type(e).__name__, e)
    return q


def fetch_spx():
    q = Quote("spx", "S&P 500", "Yahoo Finance chart API (^GSPC, unofficial)")
    try:
        _set(q, parse_yahoo_chart(_get(SPX_URL)))
    except Exception as e:  # noqa: BLE001
        q.error = "%s: %s" % (type(e).__name__, e)
    return q


def fetch_us10y(today=None):
    q = Quote("us10y", "US 10Y", "U.S. Treasury daily par yield curve", "%")
    year = (today or dt.date.today()).year
    try:
        rows = parse_treasury_csv(_get(UST_URL.format(year=year)))
        if len(rows) < 2:  # early January: previous value is in last year's file
            rows = parse_treasury_csv(_get(UST_URL.format(year=year - 1))) + rows
        _set(q, rows)
    except Exception as e:  # noqa: BLE001
        q.error = "%s: %s" % (type(e).__name__, e)
    return q


def fetch_all():
    """The four fetches run in parallel, so the worst case is about one source's
    timeouts (VIX: Cboe + Yahoo, 10Y: two yearly files), not the sum of all."""
    from concurrent.futures import ThreadPoolExecutor
    fns = [fetch_vix, fetch_btc, fetch_spx, fetch_us10y]
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        return [f.result() for f in [ex.submit(fn) for fn in fns]]

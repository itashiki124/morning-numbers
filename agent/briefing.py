"""Numbers-only English briefing. Values are printed as fetched; the only
derived numbers are the day-over-day % change (and bp for the 10Y yield).

US rows (VIX, S&P 500, 10Y) are aligned on one base date: the latest US session
any of them has. A row still on an older session is shown as "not updated" with
its date, and its change is not printed, so stale and fresh moves are never read
side by side."""

US_KEYS = ("vix", "spx", "us10y")


def _fmt_value(q):
    if q.key in ("btc", "spx"):
        return "{:,.2f}".format(q.value)
    if q.key == "us10y":
        return "{:.2f}%".format(q.value)
    return "{:.2f}".format(q.value)


def _md(iso):
    return iso[5:].replace("-", "/")


def us_session(quotes):
    """Latest US session date among the US rows that have data (ISO), or None."""
    dates = [q.date for q in quotes if q.key in US_KEYS and q.ok]
    return max(dates) if dates else None


def is_stale(q, session):
    return q.key in US_KEYS and q.ok and session is not None and q.date < session


def _bp(q):
    return round((q.value - q.prev_value) * 100)


def line(q, session=None):
    if not q.ok:
        return "{:<8} n/a  (source unavailable: {})".format(q.label, q.source)
    head = "{:<8} {:>10}".format(q.label, _fmt_value(q))
    if is_stale(q, session):
        return "{}  not updated: last value {} (US latest {})".format(
            head, _md(q.date), _md(session))
    if q.key == "us10y":
        chg = "{:+d} bp".format(_bp(q))
    else:
        pct = q.change_pct
        chg = "{:+.2f}%".format(pct) if pct is not None else "n/a"
    when = "[{} vs {}{}]".format(_md(q.date), _md(q.prev_date),
                                 " UTC close" if q.key == "btc" else "")
    out = "{}  {}  {}".format(head, chg, when)
    if q.check:
        out += "  " + q.check
    return out


def build(quotes, run_date, prev_us_session=None):
    session = us_session(quotes)
    lines = ["Numbers-only market brief | {} (JST)".format(run_date)]
    if session:
        lines.append("US session: {}".format(_md(session)))
        if prev_us_session and prev_us_session == session:
            lines.append("No new US session since last brief.")
    lines += [line(q, session) for q in quotes]
    lines.append("Change vs the previous completed session; 10Y in bp. "
                 "Sources: " + "; ".join(q.source for q in quotes) + ".")
    lines.append("Public data only. Not investment advice.")
    return "\n".join(lines)

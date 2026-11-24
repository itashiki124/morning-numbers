import datetime as dt
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FX = os.path.join(HERE, "fixtures")


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


from agent import archive, briefing, notify, pipeline, sources  # noqa: E402


def yahoo(now_open):
    start = 1795444200  # 2026-11-23 09:30 ET
    return json.dumps({"chart": {"result": [{
        "meta": {"gmtoffset": -18000,
                 "currentTradingPeriod": {"regular": {"start": start, "end": start + 23400}}},
        "timestamp": [1795530600 - 86400 * 4, 1795530600 - 86400 * 3, start],
        "indicators": {"quote": [{"close": [7175.83, 6910.39, 7226.9]}]}}]}})


class Parsers(unittest.TestCase):
    def test_vix(self):
        self.assertEqual(sources.parse_vix_csv(fx("vix.csv"))[-1], ("2026-11-20", 35.89))

    def test_treasury_sorted_ascending(self):
        rows = sources.parse_treasury_csv(fx("ust.csv"))
        self.assertEqual(rows, [("2026-11-19", 5.04), ("2026-11-20", 4.92)])

    def test_btc_drops_open_utc_day(self):
        rows = sources.parse_coinbase_candles(fx("btc.json"), now_utc=dt.datetime(2026, 11, 23, 16, 0))
        self.assertEqual(rows[-1], ("2026-11-22", 61163.01))

    def test_spx_drops_intraday_bar_while_open(self):
        rows = sources.parse_yahoo_chart(yahoo(True), now_epoch=1795444200 + 3600)
        self.assertEqual(rows[-1][1], 6910.39)

    def test_spx_keeps_bar_after_close(self):
        rows = sources.parse_yahoo_chart(yahoo(False), now_epoch=1795444200 + 30000)
        self.assertEqual(rows[-1][1], 7226.9)


def quote(key, label, v, pv):
    q = sources.Quote(key, label, "src")
    q.date, q.value, q.prev_date, q.prev_value = "2026-11-20", v, "2026-11-19", pv
    return q


class Brief(unittest.TestCase):
    def test_numbers_and_missing(self):
        bad = sources.Quote("btc", "BTC-USD", "Coinbase")
        bad.error = "timeout"
        text = briefing.build([quote("vix", "VIX", 35.89, 28.44), bad,
                               quote("us10y", "US 10Y", 4.92, 5.04)], "2026-11-24")
        self.assertIn("+26.20%", text)
        self.assertIn("-12 bp", text)
        self.assertIn("BTC-USD  n/a", text)
        self.assertLessEqual(len(text), notify.MAX_LEN)

    def test_dry_run_masks_user(self):
        os.environ["PUSHOVER_USER_KEY"] = "uABCDEFGHIJ"
        try:
            r = notify.send("hi", dry_run=True)
        finally:
            del os.environ["PUSHOVER_USER_KEY"]
        self.assertFalse(r["sent"])
        self.assertEqual(r["payload"]["user"], "uAB***")


class ArchiveAsk(unittest.TestCase):
    def test_archive_then_ask(self):
        saved = archive.ROOT
        self.addCleanup(setattr, archive, "ROOT", saved)
        with tempfile.TemporaryDirectory() as d:
            archive.ROOT = d
            qs = [quote("vix", "VIX", 35.89, 28.44), quote("us10y", "US 10Y", 4.92, 5.04)]
            archive.write_local("2026-11-24", "x", qs)
            archive.write_local("2026-11-24", "y", qs)
            with open(os.path.join(d, "2026-11-24.md")) as f:
                self.assertEqual(f.read().count("## Run"), 2)
            a = pipeline.ask("what happened to the vix?")
            self.assertIn("up from 28.44", a)
            self.assertNotIn("US 10Y", a)


def q2(key, label, d, v, pd, pv):
    q = sources.Quote(key, label, "src")
    q.date, q.value, q.prev_date, q.prev_value = d, v, pd, pv
    return q


def fake_quotes():
    """This morning's misreading case: Cboe VIX one session behind the rest."""
    return [q2("vix", "VIX", "2026-11-20", 35.89, "2026-11-19", 28.44),
            q2("btc", "BTC-USD", "2026-11-23", 61189.97, "2026-11-22", 61163.01),
            q2("spx", "S&P 500", "2026-11-23", 7237.17, "2026-11-20", 6910.39),
            q2("us10y", "US 10Y", "2026-11-23", 4.92, "2026-11-20", 4.92)]


class BaseDate(unittest.TestCase):
    def test_stale_us_row_has_no_change(self):
        text = briefing.build(fake_quotes(), "2026-11-24")
        vix = [l for l in text.splitlines() if l.startswith("VIX")][0]
        self.assertIn("not updated: last value 11/20 (US latest 11/23)", vix)
        self.assertNotIn("%", vix)
        self.assertIn("US session: 11/23", text)
        self.assertNotIn("No new US session", text)

    def test_btc_not_marked_stale_and_utc(self):
        text = briefing.build(fake_quotes(), "2026-11-24")
        btc = [l for l in text.splitlines() if l.startswith("BTC")][0]
        self.assertIn("UTC close", btc)
        self.assertIn("+0.04%", btc)

    def test_10y_bp_only(self):
        text = briefing.build(fake_quotes(), "2026-11-24")
        ten = [l for l in text.splitlines() if l.startswith("US 10Y")][0]
        self.assertIn("+0 bp", ten)
        self.assertNotIn("+0.00%", ten)

    def test_no_new_session_line(self):
        text = briefing.build(fake_quotes(), "2026-11-27", prev_us_session="2026-11-23")
        self.assertIn("No new US session since last brief.", text)


class VixCheck(unittest.TestCase):
    def _q(self):
        return q2("vix", "VIX", "2026-11-23", 26.62, "2026-11-20", 35.89)

    def test_prev_day_verified(self):
        q = sources.cross_check_vix(self._q(), [("2026-11-20", 35.89), ("2026-11-23", 26.62)],
                                    [("2026-11-19", 28.44), ("2026-11-20", 35.89)])
        self.assertEqual(q.check, "Cboe-verified 11/20")
        line = briefing.line(q, "2026-11-23")
        self.assertIn("-25.83%", line)
        self.assertIn("Cboe-verified 11/20", line)

    def test_both_days_verified(self):
        q = sources.cross_check_vix(self._q(), [("2026-11-20", 35.89), ("2026-11-23", 26.62)],
                                    [("2026-11-20", 35.89), ("2026-11-23", 26.62)])
        self.assertEqual(q.check, "Cboe-verified 11/20, 11/23")

    def test_mismatch_is_flagged_not_hidden(self):
        q = sources.cross_check_vix(self._q(), [("2026-11-20", 35.89), ("2026-11-23", 26.62)],
                                    [("2026-11-20", 35.70)])
        self.assertTrue(q.check.startswith("UNVERIFIED"))
        self.assertEqual(q.value, 26.62)


class Runs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (archive.ROOT, sources.fetch_all, notify.send, archive.push_github)
        archive.ROOT = os.path.join(self.tmp.name, "archive")
        os.environ["STATE_DIR"] = os.path.join(self.tmp.name, "state")
        sources.fetch_all = fake_quotes
        self.sent = []

    def tearDown(self):
        archive.ROOT, sources.fetch_all, notify.send, archive.push_github = self.saved
        del os.environ["STATE_DIR"]
        self.tmp.cleanup()

    def _fake_send(self, text, dry_run=False):
        self.sent.append(text)
        return {"sent": True}

    def test_dry_run_writes_nothing(self):
        r = pipeline.run(dry_run=True, run_date="2026-11-24")
        self.assertFalse(os.path.exists(archive.ROOT))
        self.assertFalse(os.path.exists(os.environ["STATE_DIR"]))
        self.assertIsNone(r["archive"]["local"])
        self.assertEqual(r["stale"], ["VIX"])

    def test_once_per_day_unless_forced(self):
        notify.send = self._fake_send
        archive.push_github = lambda d, dry_run=False: {"pushed": False, "reason": "not configured"}
        first = pipeline.run(run_date="2026-11-24")
        second = pipeline.run(run_date="2026-11-24")
        self.assertIn("skipped", second)
        self.assertEqual(len(self.sent), 1)
        pipeline.run(run_date="2026-11-24", force=True)
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(first["delivery"]["notify_sent"])

    def test_nothing_delivered_leaves_no_marker(self):
        notify.send = lambda t, dry_run=False: {"sent": False, "reason": "not configured"}
        archive.push_github = lambda d, dry_run=False: {"pushed": False, "reason": "not configured"}
        pipeline.run(run_date="2026-11-24")
        self.assertIsNone(archive.sent_marker("2026-11-24"))

    def test_notify_crash_does_not_stop_github(self):
        def boom(text, dry_run=False):
            raise TimeoutError("timed out")
        pushed = []
        notify.send = boom
        archive.push_github = lambda d, dry_run=False: pushed.append(d) or {"pushed": True}
        r = pipeline.run(run_date="2026-11-24")
        self.assertEqual(pushed, ["2026-11-24"])
        self.assertIn("TimeoutError", r["delivery"]["notify_reason"])
        with open(os.path.join(archive.ROOT, "2026-11-24.json")) as f:
            self.assertIn("TimeoutError", json.load(f)["delivery"]["notify_reason"])

    def test_no_new_us_session_after_holiday(self):
        notify.send = self._fake_send
        archive.push_github = lambda d, dry_run=False: {"pushed": True}
        pipeline.run(run_date="2026-11-26")
        r = pipeline.run(dry_run=True, run_date="2026-11-27")
        self.assertTrue(r["no_new_us_session"])
        self.assertIn("No new US session since last brief.", r["briefing"])


class NetworkErrors(unittest.TestCase):
    def test_notify_timeout_is_reported(self):
        import urllib.request as ur
        saved = ur.urlopen
        os.environ.update(PUSHOVER_GATEWAY_URL="https://gw.invalid", PUSHOVER_USER_KEY="uX")
        def raise_timeout(*a, **k):
            raise TimeoutError("timed out")
        ur.urlopen = raise_timeout
        try:
            r = notify.send("hi")
        finally:
            ur.urlopen = saved
            del os.environ["PUSHOVER_GATEWAY_URL"], os.environ["PUSHOVER_USER_KEY"]
        self.assertFalse(r["sent"])
        self.assertIn("TimeoutError", r["reason"])

    def test_github_timeout_is_reported(self):
        saved, saved_root = archive._gh, archive.ROOT
        archive._gh = lambda *a, **k: (None, {}, "URLError: timed out")
        os.environ.update(GITHUB_GATEWAY_URL="https://gw.invalid", GITHUB_REPO="o/r")
        tmp = tempfile.TemporaryDirectory()
        archive.ROOT = tmp.name
        archive.write_local("2026-11-24", "x", [])
        try:
            r = archive.push_github("2026-11-24")
        finally:
            archive._gh, archive.ROOT = saved, saved_root
            tmp.cleanup()
            del os.environ["GITHUB_GATEWAY_URL"], os.environ["GITHUB_REPO"]
        self.assertFalse(r["pushed"])
        self.assertIn("timed out", r["reason"])


if __name__ == "__main__":
    unittest.main()

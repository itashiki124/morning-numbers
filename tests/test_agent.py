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


class Decline(unittest.TestCase):
    """/ask declines why / will / should, logs it, and still returns the numbers."""

    def setUp(self):
        self.saved = archive.ROOT, archive._gh
        self.tmp = tempfile.TemporaryDirectory()
        archive.ROOT = self.tmp.name
        self.calls = []
        archive._gh = lambda m, url, body=None: self.calls.append((m, url, body)) or (
            (404, {}, "HTTP 404") if m == "GET" else (201, {}, None))
        os.environ.update(GITHUB_GATEWAY_URL="https://gw.invalid", GITHUB_REPO="o/r")
        archive.write_local("2026-11-23", "x", fake_quotes()[:1], us_session="2026-11-20")
        archive.write_local("2026-11-24", "x", [
            q2("vix", "VIX", "2026-11-23", 26.62, "2026-11-20", 35.89),
            q2("btc", "BTC-USD", "2026-11-23", 61189.97, "2026-11-22", 61163.01)],
            us_session="2026-11-23")

    def tearDown(self):
        archive.ROOT, archive._gh = self.saved
        del os.environ["GITHUB_GATEWAY_URL"], os.environ["GITHUB_REPO"]
        self.tmp.cleanup()

    def test_opinion_detection(self):
        for q in ("why did VIX fall?", "should I buy bitcoin?",
                  "what will the S&P do tomorrow?", "Is BTC worth it"):
            self.assertTrue(pipeline.is_opinion(q), q)
        for q in ("vix", "what happened to the vix?", "btc this week", "show me willow"):
            self.assertFalse(pipeline.is_opinion(q), q)

    def test_decline_then_numbers_and_commit(self):
        a = pipeline.ask("why did VIX fall?")
        lines = a.splitlines()
        self.assertEqual(lines[0], pipeline.DECLINE_LINE)
        self.assertIn("VIX was 26.62 on 2026-11-23, down from 35.89", lines[1])
        self.assertEqual(lines[-1], "Declined so far: 1.")
        with open(os.path.join(archive.ROOT, "declined.md")) as f:
            self.assertIn("why did VIX fall?", f.read())
        put = [c for c in self.calls if c[0] == "PUT"][0]
        self.assertTrue(put[1].endswith("/repos/o/r/contents/archive/declined.md"))
        self.assertEqual(put[2]["message"], "declined: why did VIX fall?")
        self.assertIn("Declined so far: 2.", pipeline.ask("should I buy bitcoin?"))

    def test_plain_question_unchanged(self):
        a = pipeline.ask("what happened to the vix?")
        self.assertNotIn(pipeline.DECLINE_LINE, a)
        self.assertNotIn("Declined", a)
        self.assertFalse(os.path.exists(os.path.join(archive.ROOT, "declined.md")))
        self.assertEqual(self.calls, [])

    def test_dry_run_declines_without_logging(self):
        a = pipeline.ask("why did VIX fall?", dry_run=True)
        self.assertTrue(a.startswith(pipeline.DECLINE_LINE))
        self.assertIn("not recorded: dry-run", a)
        self.assertFalse(os.path.exists(os.path.join(archive.ROOT, "declined.md")))
        self.assertEqual(self.calls, [])

    def test_clean_and_truncate(self):
        q = archive.clean_question("why??? <script>alert(1)</script> " + "x" * 200 + " \u65e5\u672c")
        self.assertLessEqual(len(q), archive.DECLINED_MAX_CHARS)
        self.assertNotIn("<", q)
        self.assertTrue(q.isascii())

    def test_daily_cap(self):
        now = dt.datetime(2026, 11, 24, 6, 0)
        for i in range(archive.DECLINED_CAP["owner"]):
            self.assertTrue(archive.record_declined("why %d" % i, now=now)["recorded"])
        r = archive.record_declined("why again", now=now)
        self.assertFalse(r["recorded"])
        self.assertEqual(r["reason"], "daily cap reached")
        self.assertTrue(archive.record_declined("why", now=now + dt.timedelta(days=1))["recorded"])

    def test_public_and_owner_caps_are_separate(self):
        now = dt.datetime(2026, 11, 24, 6, 0)
        for i in range(archive.DECLINED_CAP["public"]):
            self.assertTrue(archive.record_declined("why %d" % i, now=now, public=True,
                                                    matched=["why"], metrics=["VIX"])["recorded"])
        self.assertFalse(archive.record_declined("why", now=now, public=True,
                                                 matched=["why"])["recorded"])
        # strangers used up their share; the owner's demo still gets logged
        self.assertTrue(archive.record_declined("why did VIX fall?", now=now)["recorded"])
        a = pipeline.ask("should I buy bitcoin?", public=True)
        self.assertIn("not recorded: daily cap reached", a)
        self.assertTrue(a.startswith(pipeline.DECLINE_LINE))

    def test_public_log_keeps_no_typed_text(self):
        a = pipeline.ask("Should I buy Bitcoin, my email is a@b.c", public=True)
        self.assertTrue(a.startswith(pipeline.DECLINE_LINE))
        with open(os.path.join(archive.ROOT, "declined.md")) as f:
            body = f.read()
        self.assertIn("(public) should/buy + BTC-USD", body)
        self.assertNotIn("email", body)
        self.assertNotIn("Bitcoin", body)
        put = [c for c in self.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2]["message"], "declined: (public) should/buy + BTC-USD")
        self.assertNotIn("email", json.dumps(put[2]))

    def test_github_failure_still_answers(self):
        def boom(*a, **k):
            raise RuntimeError("gateway down")
        archive._gh = boom
        a = pipeline.ask("why did VIX fall?")
        self.assertIn("VIX was 26.62", a)
        self.assertIn("Declined so far: 1.", a)

    def test_this_week_lists_stored_days(self):
        a = pipeline.ask("vix this week")
        self.assertIn("VIX this week: 11/19 28.44, 11/20 35.89, 11/23 26.62.", a)
        self.assertIn("2 archived brief(s)", a)
        self.assertNotIn("BTC", a.splitlines()[0])

    def test_this_week_with_opinion(self):
        a = pipeline.ask("will btc rise this week?")
        self.assertTrue(a.startswith(pipeline.DECLINE_LINE))
        self.assertIn("BTC-USD this week: 11/22 61,163.01, 11/23 61,189.97.", a)


class Jev(unittest.TestCase):
    """Jev is a second judge: only a probability, used only when the word list lets a
    question through, and the answer never contains generated text."""

    def setUp(self):
        self.saved = archive.ROOT, archive._gh, pipeline._jev_post
        self.tmp = tempfile.TemporaryDirectory()
        archive.ROOT = self.tmp.name
        archive._gh = lambda m, url, body=None: (404, {}, "HTTP 404") if m == "GET" else (201, {}, None)
        archive.write_local("2026-11-24", "x", [
            q2("btc", "BTC-USD", "2026-11-23", 61189.97, "2026-11-22", 61163.01)],
            us_session="2026-11-23")
        self.sent = []

    def tearDown(self):
        archive.ROOT, archive._gh, pipeline._jev_post = self.saved
        for k in ("TYPESAFE_GATEWAY_URL", "JEV", "GITHUB_GATEWAY_URL", "GITHUB_REPO"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def fake(self, p):
        def post(url, body):
            self.sent.append((url, body))
            return {"model": "jev-1.14.0", "answers": {"opinion": {"type": "noul", "noul": p}}}
        pipeline._jev_post = post

    def test_off_without_key_word_list_only(self):
        self.fake(0.99)
        a = pipeline.ask("is now a good time to get into bitcoin?")
        self.assertNotIn(pipeline.DECLINE_LINE, a)
        self.assertEqual(self.sent, [])
        self.assertIsNone(pipeline.jev_judge("anything"))

    def test_catches_what_the_words_miss(self):
        os.environ["TYPESAFE_GATEWAY_URL"] = "https://api.sprites.dev/v1/gateway/typesafe/x/"
        self.fake(0.97)
        a = pipeline.ask("is now a good time to get into bitcoin?", public=True)
        lines = a.splitlines()
        self.assertEqual(lines[0], pipeline.DECLINE_LINE)
        self.assertEqual(lines[1], "Declined (Jev: opinion-seeking, p=0.97).")
        self.assertIn("BTC-USD was 61,189.97", a)
        url, body = self.sent[0]
        self.assertEqual(url, "https://api.sprites.dev/v1/gateway/typesafe/x/v1/systemone")
        self.assertEqual(body["questions"]["opinion"]["type"], "noul")
        with open(os.path.join(archive.ROOT, "declined.md")) as f:
            self.assertIn("(public) Jev 0.97 + BTC-USD", f.read())

    def test_low_probability_answers_normally(self):
        os.environ["TYPESAFE_GATEWAY_URL"] = "https://gw.invalid"
        self.fake(0.12)
        a = pipeline.ask("what was bitcoin yesterday?")
        self.assertNotIn(pipeline.DECLINE_LINE, a)
        self.assertIn("BTC-USD was", a)

    def test_word_list_first_no_jev_call(self):
        os.environ["TYPESAFE_GATEWAY_URL"] = "https://gw.invalid"
        self.fake(0.01)
        a = pipeline.ask("why did bitcoin rise?", dry_run=True)
        self.assertTrue(a.startswith(pipeline.DECLINE_LINE))
        self.assertEqual(self.sent, [])

    def test_jev_down_or_odd_shape_falls_back(self):
        os.environ["TYPESAFE_GATEWAY_URL"] = "https://gw.invalid"

        def boom(url, body):
            raise OSError("down")
        pipeline._jev_post = boom
        self.assertIsNone(pipeline.jev_judge("good time for bitcoin?"))
        pipeline._jev_post = lambda u, b: {"answers": {}}
        self.assertIsNone(pipeline.jev_judge("good time for bitcoin?"))
        os.environ["JEV"] = "0"
        self.fake(0.99)
        self.assertIsNone(pipeline.jev_judge("good time for bitcoin?"))


class Server(unittest.TestCase):
    """Run key required, 429 on floods, a page that shows no environment."""

    @classmethod
    def setUpClass(cls):
        import threading
        import server
        cls.server_mod = server
        cls.saved = archive.ROOT, archive._gh
        cls.tmp = tempfile.TemporaryDirectory()
        archive.ROOT = cls.tmp.name
        archive._gh = lambda m, url, body=None: (404, {}, "HTTP 404") if m == "GET" else (201, {}, None)
        archive.write_local("2026-11-24", "VIX 26.62 <b>", [
            q2("vix", "VIX", "2026-11-23", 26.62, "2026-11-20", 35.89)], us_session="2026-11-23")
        os.environ.update(RUN_KEY="k" * 32, GITHUB_GATEWAY_URL="https://gw.invalid/SECRETPATH",
                          GITHUB_REPO="o/r", DRY_RUN="1")
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        archive.ROOT, archive._gh = cls.saved
        for k in ("RUN_KEY", "GITHUB_GATEWAY_URL", "GITHUB_REPO", "DRY_RUN"):
            os.environ.pop(k, None)
        cls.tmp.cleanup()

    def setUp(self):
        self.server_mod.LIMIT = self.server_mod.RateLimit(total=30, per_client=8)
        self.saved_run = pipeline.run
        pipeline.run = lambda dry_run=False, force=False: {"dry_run": dry_run, "force": force}

    def tearDown(self):
        pipeline.run = self.saved_run

    def get(self, path, key=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     headers={"X-Run-Key": key} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_run_needs_key_even_dry(self):
        self.assertEqual(self.get("/run?dry=1")[0], 403)
        self.assertEqual(self.get("/run?dry=1", key="wrong")[0], 403)
        st, body = self.get("/run?dry=1", key="k" * 32)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["dry_run"])

    def test_run_fails_closed_without_run_key(self):
        saved = os.environ.pop("RUN_KEY")
        try:
            self.assertEqual(self.get("/run?dry=1", key="")[0], 403)
            self.assertFalse(self.server_mod.key_ok(""))
        finally:
            os.environ["RUN_KEY"] = saved

    def test_page_shows_brief_and_no_env(self):
        st, body = self.get("/")
        self.assertEqual(st, 200)
        self.assertIn("The uncle with no opinions", body)
        self.assertIn("No generated words. No guesses.", body)
        self.assertIn("Declined so far:", body)
        self.assertIn("VIX 26.62 &lt;b&gt;", body)
        for secret in ("SECRETPATH", "k" * 32, "gw.invalid", "RUN_KEY"):
            self.assertNotIn(secret, body)

    def test_flood_gets_429_owner_not_limited(self):
        self.server_mod.LIMIT = self.server_mod.RateLimit(total=30, per_client=3)
        codes = [self.get("/ask?q=vix")[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])
        self.assertEqual(self.get("/ask?q=vix", key="k" * 32)[0], 200)
        self.assertEqual(self.get("/healthz")[0], 200)

    def test_rate_limit_window(self):
        t = [0.0]
        rl = self.server_mod.RateLimit(total=2, per_client=5, clock=lambda: t[0])
        self.assertTrue(rl.allow("a"))
        self.assertTrue(rl.allow("b"))
        self.assertFalse(rl.allow("c"))
        t[0] = 61.0
        self.assertTrue(rl.allow("c"))


if __name__ == "__main__":
    unittest.main()

"""crawl-descriptions worker pool (no network anywhere: fake openers inside a REAL http.Client, or fake clients).

Rules under test (docs/DATA_RULES.md, "Descriptions"):
- the thread-safe limiter keeps request STARTS >= 1/4.4 s apart whatever the number of workers; with 3 workers and
  0.3-0.5 s per page the achieved rate approaches but never exceeds 4.4/s
- Client.halt: once set, no new request starts (Halted, nothing sent), also for callers sleeping in the limiter
- first 429 from any worker -> halt at once: zero starts after it (only requests already in flight finish, at most
  workers - 1), their results recorded, BLOCKED line, exit 2, cooldown marker
- SIGTERM drains the in-flight requests, flushes them, 'interrupted'
- the covered-by-SEC check still runs just before each submit
- soft statuses and the consecutive-error stop keep their meaning, counted in completion order
- --workers default 3, clamped to 1..4
"""
from __future__ import annotations

import contextlib
import io
import os
import random
import signal
import sys
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

import test_cli_coverage as tcc  # noqa: E402
import test_crawl_stream_rules as tsr  # noqa: E402
import test_tradingview_profiles as tp  # noqa: E402
from jevscreen import cli, guard, store  # noqa: E402
from jevscreen.http import Client, Halted, Response  # noqa: E402
from jevscreen.sources import tradingview_profiles as tvp  # noqa: E402

INTERVAL = 1 / 4.4          # 0.2273 s
EPS_SLOT = 1e-6             # limiter slots: exact (monotonic arithmetic)
EPS_WIRE = 0.02             # starts seen by the opener: thread-switch jitter after the slot is claimed


class LatencyOpener:
    """Opener for a real http.Client: each open() is a request START (time, url, halt already set?) and then takes
    `latency` seconds (seeded uniform) before answering from `script` (url -> (status, body[, headers]));
    unknown URLs get a small valid page."""

    def __init__(self, client: Client, script: dict | None = None, latency=(0.3, 0.5), seed: int = 7):
        self.client, self.script, self.latency = client, script or {}, latency
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.starts: list[tuple[float, str, bool]] = []
        self.raised_at: dict[str, float] = {}

    def open(self, req, timeout=None):
        url = req.full_url
        with self.lock:
            self.starts.append((time.monotonic(), url, self.client.halt.is_set()))
            delay = self.rng.uniform(*self.latency)
        time.sleep(delay)
        sym = url.rstrip("/").rsplit("-", 1)[-1]
        status, body, *rest = self.script.get(url, (200, tp.page_for(sym).encode()))
        headers = rest[0] if rest else {}
        if status >= 400:
            with self.lock:
                self.raised_at[url] = time.monotonic()
            raise urllib.error.HTTPError(url, status, "err", headers, io.BytesIO(body))
        return tp._FakeHTTPResponse(status, body, headers)

    def urls(self) -> list[str]:
        return [u for _, u, _ in sorted(self.starts)]


def real_client(script: dict | None = None, latency=(0.3, 0.5)) -> tuple[Client, LatencyOpener, list[float]]:
    """Client at 4.4 req/s with a latency opener; also records every limiter slot the client claims."""
    c = Client(user_agent="test", min_interval_s=INTERVAL)
    c._opener = LatencyOpener(c, script, latency)
    slots: list[float] = []
    lock = threading.Lock()
    claim = c._wait

    def recording(host, url=None):
        t = claim(host, url)
        with lock:
            slots.append(t)
        return t
    c._wait = recording
    return c, c._opener, slots


def max_in_window(times: list[float], window: float) -> int:
    """Most starts inside any half-open window [t, t + window) (t = a start)."""
    t = sorted(times)
    best, j = 0, 0
    for i in range(len(t)):
        while t[i] - t[j] >= window:
            j += 1
        best = max(best, i - j + 1)
    return best


def gaps(times: list[float]) -> list[float]:
    t = sorted(times)
    return [b - a for a, b in zip(t, t[1:])]


class SlowFakeClient(tp.FakeClient):
    """FakeClient with per-URL latency and its own halt Event (crawl uses it); records concurrency and whether
    halt was already set when a request started. Its counters are updated under a lock, so it declares itself
    thread-safe (crawl gives more than one worker only to such clients)."""

    thread_safe = True

    def __init__(self, pages, latency: dict | float = 0.2, hook=None):
        super().__init__(pages)
        self.latency, self.hook = latency, hook
        self.halt = threading.Event()
        self.lock = threading.Lock()
        self.active = self.max_active = 0
        self.started_with_halt = 0

    def get(self, url: str, **kw) -> Response:
        with self.lock:
            self.started_with_halt += int(self.halt.is_set())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.hook:
                self.hook(url)
            time.sleep(self.latency.get(url, 0.05) if isinstance(self.latency, dict) else self.latency)
            with self.lock:
                return super().get(url, **kw)
        finally:
            with self.lock:
                self.active -= 1


class LimiterThreads(unittest.TestCase):
    def test_concurrent_callers_are_spaced_by_min_interval(self):
        c = Client(user_agent="test", min_interval_s=0.05)
        slots, lock = [], threading.Lock()

        def caller():
            for _ in range(5):
                t = c._wait("h")
                with lock:
                    slots.append(t)
        threads = [threading.Thread(target=caller) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((len(slots), c.requests_made), (20, 20))
        self.assertGreaterEqual(min(gaps(slots)), 0.05 - EPS_SLOT)

    def test_halt_stops_new_and_sleeping_requests(self):
        url = tvp.profile_url("A:ONE")
        c = Client(user_agent="test", min_interval_s=0.5)
        c._opener = tp.FakeOpener({url: (200, tp.page_for("ONE").encode())})
        c.get_until(url, tvp.ProductDetector())                  # claims a slot: the next caller must sleep
        errors = []

        def sleeper():
            try:
                c.get_until(url, tvp.ProductDetector())
            except Halted as e:
                errors.append(e)
        t = threading.Thread(target=sleeper)
        t.start()
        time.sleep(0.1)                                          # it is sleeping in the limiter now
        c.halt.set()
        t.join()
        self.assertEqual(len(errors), 1)
        with self.assertRaises(Halted):
            c.get_until(url, tvp.ProductDetector())
        with self.assertRaises(Halted):
            c.get(url)
        self.assertEqual((len(c._opener.calls), c.requests_made), (1, 1))   # nothing else was sent


class CrawlWorkers(tp.Base):
    def ids(self, n: int, prefix: str = "W") -> list[str]:
        out = [f"A:{prefix}{i}" for i in range(n)]
        for i, sid in enumerate(out):
            self.add(sid, 100 - i)
        return out

    def test_three_workers_approach_but_never_exceed_4_4(self):
        ids = self.ids(20)
        client, opener, slots = real_client()
        t0 = time.monotonic()
        s = tvp.crawl(self.cfg, client, progress_every=0, workers=3)
        elapsed = time.monotonic() - t0
        self.assertEqual((s["ok"], s["status"], s["workers"], s["requests"]), (20, "ok", 3, 20))
        self.assertEqual(sorted(opener.urls()), sorted(tvp.profile_url(x) for x in ids))
        self.assertGreaterEqual(min(gaps(slots)), INTERVAL - EPS_SLOT)                  # the limiter's slots
        wire = [t for t, _, _ in opener.starts]
        self.assertGreaterEqual(min(gaps(wire)), INTERVAL - EPS_WIRE)                   # as seen on the "wire"
        rate = (len(slots) - 1) / (max(slots) - min(slots))
        self.assertLessEqual(rate, 4.4 + 1e-9)
        self.assertGreater(rate, 3.8)                                    # approaches the 4-per-second window cap
        self.assertLessEqual(max_in_window(slots, 1.0 - EPS_SLOT), 4)             # never 5 starts in any rolling second
        self.assertLessEqual(max_in_window(wire, 1.0 - EPS_WIRE), 4)
        # sequential at ~0.4 s per page would reach ~2.5/s: 20 pages would take ~8 s
        self.assertLess(elapsed, 7.0)
        self.assertEqual(len(self.states()), 20)

    def test_first_429_halts_everything_in_flight_finishes(self):
        ids = self.ids(12)
        blocked = tvp.profile_url(ids[4])
        client, opener, _ = real_client({blocked: (429, b"slow down")})
        seen, err = [], io.StringIO()
        with contextlib.redirect_stderr(err):
            s = tvp.crawl(self.cfg, client, progress_every=0, workers=3, on_blocked=seen.append)
        order = opener.urls()
        k = order.index(blocked) + 1
        self.assertEqual(sum(h for _, _, h in opener.starts), 0)          # no request started after the halt
        self.assertLessEqual(len(order), k + 3 - 1)                        # at most workers - 1 already in flight
        # the client sets halt in the same step that observes the 429: no start after the 429 came back
        self.assertTrue(all(t <= opener.raised_at[blocked] for t, _, _ in opener.starts))
        self.assertEqual(client.requests_made, len(order))                 # no retry
        self.assertEqual((s["status"], s["blocked_url"], s["blocked_status"], s["blocked_reason"]),
                         ("blocked", blocked, 429, "http_429"))
        self.assertEqual([(e.url, e.status) for e in seen], [(blocked, 429)])   # the CLI's BLOCKED hook, once
        self.assertIn("draining", err.getvalue())
        st = self.states()
        self.assertEqual(set(st), {x for x in ids if tvp.profile_url(x) in order})   # every started one recorded
        self.assertEqual(st[ids[4]][:2], ("blocked", 429))
        self.assertEqual({v[0] for k_, v in st.items() if k_ != ids[4]}, {"ok"})     # in-flight results kept
        self.assertEqual(s["remaining"], 12 - len(order))
        self.assertIsNotNone(guard.recent_block_marker(self.cfg, guard.CMD_CRAWL_DESCRIPTIONS))
        self.assertEqual(self.run_row()[:2], ("blocked", len(order)))
        self.assertEqual(tvp.queue(self.con)[0], ids[4])                  # the next run starts again from it

    def test_sigterm_drains_in_flight_and_flushes(self):
        ids = self.ids(10, "T")

        def term(url: str) -> None:
            if url == tvp.profile_url(ids[3]):
                os.kill(os.getpid(), signal.SIGTERM)
        client = SlowFakeClient({tvp.profile_url(x): tp.page_for(x.split(":")[1]) for x in ids}, 0.3, term)
        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(cli.Terminated), cli.sigterm_as_interrupt():
            tvp.crawl(self.cfg, client, progress_every=0, flush_every=50, workers=3)
        self.assertEqual(signal.getsignal(signal.SIGTERM), before)
        self.assertEqual(client.started_with_halt, 0)                      # nothing new after the signal
        self.assertIn(tvp.profile_url(ids[3]), client.calls)
        self.assertLess(len(client.calls), 10)
        st = self.states()
        self.assertEqual(sorted(tvp.profile_url(x) for x in st), sorted(client.calls))   # in-flight drained+flushed
        self.assertEqual({v[0] for v in st.values()}, {"ok"})
        self.assertEqual(self.run_row()[0], "interrupted")
        self.assertIsNotNone(self.run_row()[2])

    def test_covered_refresh_still_skips_before_submit(self):
        names = ["ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX"]
        cks = {n: self.add(f"A:{n}", 10 - i, isin=f"US00000000{i}Y") for i, n in enumerate(names)}
        gate = threading.Event()
        cover = tsr.CrawlStreaming._cover_by_sec_text
        document = tsr.CrawlStreaming._cover_by_document

        def sync_sec_progress(url: str) -> None:
            if url == tvp.profile_url("A:ONE"):   # sync-sec covers FIVE and SIX while ONE, TWO, THREE are in flight
                with store.session(self.cfg, wait_s=5) as con:
                    cover(self, con, "A:FIVE", cks["FIVE"])
                    document(self, con, "A:SIX", cks["SIX"], cik="0000789019")
                gate.set()
            else:
                gate.wait(10)                      # nothing completes before the coverage is committed

        client = SlowFakeClient({tvp.profile_url(f"A:{n}"): tp.page_for(n) for n in names}, 0.05,
                                sync_sec_progress)
        s = tvp.crawl(self.cfg, client, progress_every=0, flush_every=1, workers=3)
        self.assertEqual(sorted(client.calls), sorted(tvp.profile_url(f"A:{n}") for n in names[:4]))
        self.assertEqual((s["skipped_sec_covered"], s["attempted"], s["ok"], s["remaining"]), (2, 4, 4, 0))
        self.assertNotIn("A:FIVE", self.states())
        self.assertNotIn("A:SIX", self.states())

    def test_soft_statuses_never_stop_the_run(self):
        ids = self.ids(9, "S")
        big = b"<html>" + b"z" * (300 * 1024)
        script = {}
        for i, sid in enumerate(ids):
            url = tvp.profile_url(sid)
            script[url] = [(200, tp.page_for("OTHER").encode()), (302, b"", {"Location": "https://x/"}),
                           (200, big)][i % 3]
        script[tvp.profile_url(ids[1])] = (500, b'{"e":1}', {"Content-Type": "application/json"})
        script[tvp.profile_url(ids[5])] = (500, b'{"e":1}', {"Content-Type": "application/json"})
        client, opener, _ = real_client(script, latency=(0.05, 0.15))
        client.max_retries = 0
        s = tvp.crawl(self.cfg, client, progress_every=0, workers=3, max_consecutive_errors=3)
        self.assertEqual((s["stopped_reason"], s["attempted"], s["error"]), (None, 9, 2))
        self.assertEqual(s["mismatch"] + s["redirect"] + s["no_product_within_cap"], 7)
        self.assertEqual(len(tvp.queue(self.con)), 9)                     # soft + error: all retried later
        # soft statuses neither count nor reset: 3 errors among soft pages stop the run whatever the order
        self.tearDown()
        self.setUp()
        ids = self.ids(9, "S")
        script = {tvp.profile_url(x): (302, b"", {"Location": "https://x/"}) for x in ids}
        for i in (0, 4, 8):
            script[tvp.profile_url(ids[i])] = (500, b'{"e":1}', {"Content-Type": "application/json"})
        client, _, _ = real_client(script, latency=(0.05, 0.15))
        client.max_retries = 0
        s = tvp.crawl(self.cfg, client, progress_every=0, workers=3, max_consecutive_errors=3)
        self.assertEqual((s["stopped_reason"], s["error"]), ("consecutive_errors", 3))

    def test_consecutive_errors_counted_in_completion_order(self):
        # queue order: err, err, ok, err, err, ok (never 3 errors in a row), but completion order is
        # B(0.1) D(0.2) E(0.3) C(0.5) A(0.9): three errors in a row -> stop at ~0.3 s; A and C are drained
        names = ["A", "B", "C", "D", "E", "F"]
        for i, n in enumerate(names):
            self.add(f"A:{n}", 10 - i)
        u = {n: tvp.profile_url(f"A:{n}") for n in names}
        pages = {u["A"]: 500, u["B"]: 500, u["C"]: tp.page_for("C"), u["D"]: 500, u["E"]: 500,
                 u["F"]: tp.page_for("F")}
        latency = {u["A"]: 0.9, u["B"]: 0.1, u["C"]: 0.5, u["D"]: 0.1, u["E"]: 0.1, u["F"]: 0.1}
        client = SlowFakeClient(pages, latency)
        s = tvp.crawl(self.cfg, client, progress_every=0, workers=3, max_consecutive_errors=3)
        self.assertEqual((s["stopped_reason"], s["status"], s["error"], s["ok"], s["attempted"]),
                         ("consecutive_errors", "stopped_errors", 4, 1, 5))
        self.assertNotIn(u["F"], client.calls)
        self.assertEqual(client.started_with_halt, 0)
        self.assertEqual(sorted(self.states()), ["A:A", "A:B", "A:C", "A:D", "A:E"])   # in-flight A, C recorded
        # all errors: the stop comes after 3 completions, at most workers - 1 more were in flight
        self.tearDown()
        self.setUp()
        ids = self.ids(10, "E")
        client = SlowFakeClient({tvp.profile_url(x): 500 for x in ids}, 0.1)
        s = tvp.crawl(self.cfg, client, progress_every=0, workers=3, max_consecutive_errors=3)
        self.assertEqual(s["stopped_reason"], "consecutive_errors")
        self.assertLessEqual(len(client.calls), 3 + 2)
        self.assertEqual(len(self.states()), len(client.calls))

    def test_workers_clamped_and_concurrency_bounded(self):
        self.assertEqual([tvp.clamp_workers(w) for w in (None, 0, -3, 1, 3, 4, 9, "x")], [3, 1, 1, 1, 3, 4, 4, 3])
        ids = self.ids(12, "C")
        client = SlowFakeClient({tvp.profile_url(x): tp.page_for(x.split(":")[1]) for x in ids}, 0.15)
        s = tvp.crawl(self.cfg, client, progress_every=0, workers=10)
        self.assertEqual((s["workers"], s["ok"], client.max_active), (4, 12, 4))
        self.tearDown()
        self.setUp()
        ids = self.ids(4, "D")
        client = SlowFakeClient({tvp.profile_url(x): tp.page_for(x.split(":")[1]) for x in ids}, 0.05)
        s = tvp.crawl(self.cfg, client, progress_every=0, workers=0)
        self.assertEqual((s["workers"], client.max_active, client.calls), (1, 1, [tvp.profile_url(x) for x in ids]))


class CliWorkers(tcc._CliBase):
    cfg = tsr.CliCrawlRules.cfg
    populate = tsr.CliCrawlRules.populate
    runs = tsr.CliCrawlRules.runs

    def test_workers_flag_default_and_clamp(self):
        self.assertEqual(cli.build_parser().parse_args(["crawl-descriptions"]).workers, 3)
        self.assertEqual(cli.build_parser().parse_args(["crawl-descriptions"]).rate, 4.4)
        seen = []
        crawl = lambda cfg, client, **kw: seen.append((kw["workers"], client.min_interval_s)) or {"ok": 0}  # noqa
        with mock.patch.dict(sys.modules, {cli.PROFILES: tcc._fake_module(cli.PROFILES, crawl=crawl)}):
            self.assertEqual(self.run_cli("crawl-descriptions")[:1], (0,))
            code, _, err = self.run_cli("crawl-descriptions", "--workers", "9")
            self.assertIn("clamped to 4", err)
            code, _, err = self.run_cli("crawl-descriptions", "--workers", "0")
            self.assertIn("clamped to 1", err)
            self.run_cli("crawl-descriptions", "--workers", "2", "--rate", "10")
        self.assertEqual([w for w, _ in seen], [3, 4, 1, 2])
        for _, interval in seen:
            self.assertAlmostEqual(interval, INTERVAL)                     # rate unchanged by workers

    def test_429_with_default_workers_blocked_line_exit_2(self):
        ids = [f"A:B{i}" for i in range(8)]
        self.populate(ids)
        blocked = tvp.profile_url(ids[3])
        client, opener, _ = real_client({blocked: (429, b"slow down")}, latency=(0.3, 0.4))
        with mock.patch.object(cli, "make_crawl_client", return_value=client):
            code, out, _ = self.run_cli("crawl-descriptions")
        self.assertEqual(code, 2)
        lines = out.strip().splitlines()
        self.assertEqual(lines[-1], f"BLOCKED url={blocked} status=429 reason=http_429")
        self.assertEqual(sum(ln.startswith("BLOCKED") for ln in lines), 1)
        self.assertEqual(sum(h for _, _, h in opener.starts), 0)            # zero starts after the halt
        self.assertLessEqual(len(opener.starts), opener.urls().index(blocked) + 1 + 2)
        self.assertEqual(self.runs()[-1][1], "blocked")
        self.assertIsNotNone(guard.recent_block_marker(self.cfg(), "crawl-descriptions"))
        with store.session(self.cfg(), read_only=True, wait_s=5) as con:
            recorded = {r[0] for r in con.execute("SELECT security_id FROM crawl_state").fetchall()}
        self.assertEqual({tvp.profile_url(x) for x in recorded}, set(opener.urls()))   # in-flight flushed
        with mock.patch.object(cli, "make_crawl_client", side_effect=AssertionError("no client")):
            self.assertEqual(self.run_cli("crawl-descriptions")[0], 2)       # cooldown: user decides to resume

    def test_sigterm_with_default_workers_exits_130_after_drain(self):
        ids = [f"A:G{i}" for i in range(8)]
        self.populate(ids)

        def term(url: str) -> None:
            if url == tvp.profile_url(ids[2]):
                os.kill(os.getpid(), signal.SIGTERM)
        fake = SlowFakeClient({tvp.profile_url(x): tp.page_for(x.split(":")[1]) for x in ids}, 0.3, term)
        with mock.patch.object(cli, "make_crawl_client", return_value=fake):
            code, _, _ = self.run_cli("crawl-descriptions")
        self.assertEqual(code, 130)
        self.assertEqual(fake.started_with_halt, 0)
        self.assertEqual([r[1] for r in self.runs()], ["interrupted"])
        with store.session(self.cfg(), read_only=True, wait_s=5) as con:
            recorded = {r[0] for r in con.execute("SELECT security_id FROM crawl_state").fetchall()}
        self.assertEqual({tvp.profile_url(x) for x in recorded}, set(fake.calls))


if __name__ == "__main__":
    unittest.main()

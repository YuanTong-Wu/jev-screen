"""Tests for the Jev client. No network and no paid calls: every transport is a local fake.

The OpenRouter key is never read from the real environment or key file: setUp points OPENROUTER_API_KEY and
JEVSCREEN_OPENROUTER_KEY_FILE at fake values for every test.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import jev, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_KEY = "sk-or-v1-FAKE-test-key-0123456789abcdef"

QUESTION = jev.Question(
    key="fit",
    instructions="Using only state.text, does state.issuer sell picks and shovels for AI data centres?",
    criteria={"core": "Main business clearly fits.", "adjacent": "Some exposure.", "none": "No stated fit.",
              "insufficient": "Text too thin to judge."})


def items(n: int, text: str = "Makes power transformers and switchgear for data centres.") -> list[jev.Item]:
    return [jev.Item(item_id=f"isin:ID{i:010d}", issuer=f"Company {i}", text=f"{text} ({i})") for i in range(n)]


def answer(label: str, labels=tuple(QUESTION.criteria), p: float = 0.91) -> dict:
    rest = (1 - p) / (len(labels) - 1)
    probs = {lab: (p if lab == label else rest) for lab in labels}
    return {"type": "choice", "choice": label, "probabilities": probs, "confidence": 0.9}


def ok_response(payload: dict, label: str = "core", cost: float | None = 0.0001, **overrides) -> dict:
    resp = {"model": "typesafe/jev-1.13-20260917",
            "answers": {qid: answer(label) for qid in payload["questions"]},
            "usage": {"input_tokens": 1000, "output_tokens": 50}}
    if cost is not None:
        resp["usage"]["cost"] = cost
    resp.update(overrides)
    return resp


class FakeTransport:
    """Records calls; `behaviour(payload, call_no)` returns a dict/bytes or raises."""

    def __init__(self, behaviour=None, latency: float = 0.0):
        self.behaviour = behaviour or (lambda payload, n: ok_response(payload))
        self.latency = latency
        self.calls: list[dict] = []
        self.call_times: list[float] = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def __call__(self, payload: dict):
        with self.lock:
            self.calls.append(payload)
            self.call_times.append(time.monotonic())
            n = len(self.calls)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.latency:
                time.sleep(self.latency)
            return self.behaviour(payload, n)
        finally:
            with self.lock:
                self.active -= 1


class JevTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.env = mock.patch.dict(os.environ, {
            "OPENROUTER_API_KEY": FAKE_KEY,
            "JEVSCREEN_OPENROUTER_KEY_FILE": str(Path(self.tmp.name) / "no-such-key-file"),
            "JEVSCREEN_HOME": str(self.home)})
        self.env.start()
        self.cfg = Config(home=self.home)

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def client(self, transport, run_id="run-a", budget=1.0, **kw) -> jev.JevClient:
        kw.setdefault("backoff_s", 0.0)
        c = jev.JevClient(self.cfg, run_id=run_id, layer="l1", budget_usd=budget, transport=transport, **kw)
        c._limiter = jev.RateLimiter(1000.0)     # tests do not need the 15/s spacing
        return c

    def ledger(self) -> list[dict]:
        with store.session(self.cfg, read_only=True) as con:
            cur = con.execute(f"SELECT {', '.join(jev.LEDGER_COLS)} FROM jev_requests ORDER BY request_id")
            return [dict(zip(jev.LEDGER_COLS, r)) for r in cur.fetchall()]


class PayloadTests(JevTestBase):
    def test_payload_shape_and_isolation(self):
        packets, notes, skipped = jev.build_packets(items(3), QUESTION, pack_size=8)
        self.assertEqual((len(packets), notes, skipped), (1, {}, {}))
        p = packets[0].payload
        self.assertEqual(set(p), {"model", "state", "questions"})
        self.assertEqual(p["model"], "typesafe/jev-1.13")
        self.assertEqual(p["state"], {"items": [{"issuer": f"Company {i}", "text": items(3)[i].text}
                                                for i in range(3)]})
        self.assertEqual(list(p["questions"]), ["item_0__fit", "item_1__fit", "item_2__fit"])
        q1 = p["questions"]["item_1__fit"]
        self.assertEqual(q1["type"], "choice")
        self.assertEqual(q1["criteria"], QUESTION.criteria)
        self.assertTrue(q1["instructions"].startswith(
            "This question is ONLY about state.items[1].issuer. Use ONLY state.items[1].text; other array items "
            "are other companies and are NOT evidence. "))
        self.assertIn("Using only state.items[1].text, does state.items[1].issuer sell", q1["instructions"])
        self.assertNotRegex(q1["instructions"], r"state\.(issuer|text)\b")
        self.assertEqual(packets[0].qids, list(p["questions"]))
        # meta is never sent; request_id is a pure function of the payload
        it = items(1)
        it[0].meta = {"secret_note": "local only"}
        pk = jev.build_packets(it, QUESTION)[0][0]
        self.assertNotIn("local only", json.dumps(pk.payload))
        self.assertEqual(pk.request_id, jev.request_id_of(jev.build_packets(items(1), QUESTION)[0][0].payload))
        self.assertEqual(len(pk.request_id), 24)

    def test_packing_caps_truncation_and_empty(self):
        packets, _, _ = jev.build_packets(items(19), QUESTION, pack_size=8)
        self.assertEqual([len(p.positions) for p in packets], [8, 8, 3])
        self.assertEqual([pos for p in packets for pos in p.positions], list(range(19)))
        # CJK text: 2,500 chars x 3 bytes = 7,500 bytes -> only 3 fit under 24,000 bytes
        cjk = [jev.Item(f"k{i}", f"公司{i}", "数据中心电力设备" * 400) for i in range(7)]
        packets, notes, _ = jev.build_packets(cjk, QUESTION, pack_size=8)
        self.assertEqual([len(p.positions) for p in packets], [3, 3, 1])
        for p in packets:
            self.assertLessEqual(sum(len(x["text"].encode()) for x in p.payload["state"]["items"]), 24_000)
            self.assertTrue(all(len(x["text"]) == 2_500 for x in p.payload["state"]["items"]))
        self.assertEqual(notes[0], "text truncated from 3200 to 2500 chars")
        # configurable cap
        packets, notes, _ = jev.build_packets(cjk[:1], QUESTION, max_text_chars=1000)
        self.assertEqual(len(packets[0].payload["state"]["items"][0]["text"]), 1000)
        # empty text is not sent
        mixed = items(2) + [jev.Item("empty", "Empty Co", "   ")]
        packets, _, skipped = jev.build_packets(mixed, QUESTION)
        self.assertEqual(skipped, {2: "empty_text"})
        self.assertEqual(packets[0].positions, [0, 1])

    def test_question_validation(self):
        for bad in (jev.Question("bad key", "x", {"a": "1", "b": "2"}), jev.Question("k", "", {"a": "1", "b": "2"}),
                    jev.Question("k", "x", {"a": "1"})):
            with self.assertRaises(ValueError):
                jev.build_packets(items(1), bad)


class ValidationTests(JevTestBase):
    def _fixture(self, name):
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    def _remap(self, fx: dict, dim: str) -> tuple[dict, list[str]]:
        """Keep one question's answers from an old packed response, renamed to this client's qids."""
        resp = copy.deepcopy(fx["response"])
        answers = {f"item_{i}__{dim}": resp["answers"][f"item_{i}_{dim}"] for i in range(fx["items"])}
        resp["answers"] = answers
        return resp, list(answers)

    def test_real_packed_fixtures_validate(self):
        for name in ("jev_response_pack4.json", "jev_response_cjk_pack8.json"):
            fx = self._fixture(name)
            for dim, labels in fx["criteria_labels"].items():
                resp, qids = self._remap(fx, dim)
                usage, out = jev.parse_response(jev.strict_json(json.dumps(resp)), qids, labels)
                self.assertEqual([e for _, _, e, _ in out], [None] * fx["items"], (name, dim))
                for qid, (label, probs, _, conf) in zip(qids, out):
                    self.assertEqual(conf, jev.answer_confidence(resp["answers"][qid]))
                    self.assertEqual(label, resp["answers"][qid]["choice"])
                    self.assertAlmostEqual(sum(probs.values()), 1.0, delta=0.011)
                cost, basis = jev.accounted_cost(usage, 0.5)
                self.assertEqual(basis, "provider_reported")
                self.assertAlmostEqual(cost, usage["input_tokens"] * 0.042e-6, places=12)

    def test_real_smoke_fixture_without_cost(self):
        fx = self._fixture("jev_response_smoke.json")
        resp = fx["response"]
        self.assertTrue(jev.model_ok(resp["model"]))           # dated model name 'typesafe/jev-1.13-20260917'
        for qid, labels in fx["criteria_labels"].items():
            label, probs = jev.validate_answer(resp["answers"][qid], labels)
            self.assertEqual(label, resp["answers"][qid]["choice"])
        cost, basis = jev.accounted_cost(resp["usage"], 0.5)
        self.assertEqual(basis, "token_estimate")
        self.assertAlmostEqual(cost, 1176 * 0.042e-6)

    def test_envelope_and_answer_failures(self):
        pk = jev.build_packets(items(3), QUESTION)[0][0]
        labels = list(QUESTION.criteria)
        good = ok_response(pk.payload)
        for mutate, detail in [
            (lambda r: r.update(model="typesafe/jev-2.0"), "model_mismatch"),
            (lambda r: r.update(model="other/typesafe/jev-1.13"), "model_mismatch"),
            (lambda r: r.update(answers=[]), "answers_not_object"),
            (lambda r: r["answers"].update(item_9__fit=answer("core")), "answer_keys_unexpected"),
            (lambda r: r.pop("usage"), "usage_not_object"),
            (lambda r: r["usage"].update(input_tokens=-1), "usage_token_count_invalid"),
            (lambda r: r["usage"].update(output_tokens=1.5), "usage_token_count_invalid"),
        ]:
            bad = copy.deepcopy(good)
            mutate(bad)
            with self.assertRaises(jev.InvalidResponse) as cm:
                jev.parse_response(bad, pk.qids, labels)
            self.assertEqual(cm.exception.detail, detail)
        # per-item isolation: one bad answer does not void the packet
        cases = {
            "probability_sum_invalid": {"type": "choice", "choice": "core",
                                        "probabilities": {"core": 0.7, "none": 0.2}},
            "probability_label_unknown": {"type": "choice", "choice": "core",
                                          "probabilities": {"core": 0.9, "maybe": 0.1}},
            "choice_not_argmax": {"type": "choice", "choice": "none",
                                  "probabilities": {"core": 0.8, "none": 0.2}},
            "probability_value_invalid": {"type": "choice", "probabilities": {"core": 1.2, "none": -0.2}},
            "answer_type_invalid": {"type": "number", "value": 3},
        }
        for detail, bad_answer in cases.items():
            bad = copy.deepcopy(good)
            bad["answers"]["item_1__fit"] = bad_answer
            _, out = jev.parse_response(bad, pk.qids, labels)
            self.assertEqual(out[1], (None, {}, f"invalid_answer:{detail}", None))
            self.assertEqual([o[0] for o in (out[0], out[2])], ["core", "core"])
        bad = copy.deepcopy(good)
        del bad["answers"]["item_2__fit"]
        _, out = jev.parse_response(bad, pk.qids, labels)
        self.assertEqual(out[2][2], "invalid_answer:answer_missing")
        # label = argmax even without 'choice'; subset of labels allowed; rounding tolerance 0.01
        label, probs = jev.validate_answer({"type": "choice", "probabilities": {"none": 0.34, "core": 0.665}}, labels)
        self.assertEqual((label, probs), ("core", {"none": 0.34, "core": 0.665}))

    def test_strict_json(self):
        for raw in ('{"a": NaN}', '{"a": 1, "a": 2}', '{"a": Infinity}'):
            with self.assertRaises(ValueError):
                jev.strict_json(raw)


class LedgerTests(JevTestBase):
    def test_ok_rows_files_and_cache_reuse_across_clients(self):
        states_seen = []

        def behaviour(payload, n):
            with store.session(self.cfg, wait_s=5) as con:   # the row must say 'sent' while the request is out
                states_seen.append(con.execute("SELECT status FROM jev_requests WHERE payload_sha256 = ?",
                                               [jev.payload_sha256(payload)]).fetchone()[0])
            return ok_response(payload, cost=0.0002)

        t1 = FakeTransport(behaviour)
        c1 = self.client(t1, run_id="run-1", workers=1)
        res = c1.classify(items(10), QUESTION)
        self.assertEqual([r["status"] for r in res], ["ok"] * 10)
        self.assertEqual({r["label"] for r in res}, {"core"})
        self.assertEqual([r["item_id"] for r in res], [i.item_id for i in items(10)])
        self.assertFalse(any(r["cached"] for r in res))
        self.assertEqual(states_seen, ["sent", "sent"])
        self.assertEqual((len(t1.calls), c1.requests_sent), (2, 2))
        self.assertAlmostEqual(c1.spent_usd, 0.0004)          # once per request, never per item
        rows = self.ledger()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual((row["status"], row["run_id"], row["layer"], row["model"]),
                             ("ok", "run-1", "l1", "typesafe/jev-1.13"))
            self.assertEqual((row["cost_usd"], row["cost_basis"], row["http_status"]), (0.0002, "provider_reported", 200))
            self.assertEqual((row["input_tokens"], row["output_tokens"]), (1000, 50))
            self.assertTrue(row["request_id"].startswith(row["payload_sha256"][:24] + "-"))   # one row per send
            path = Path(row["response_path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent.parent, self.home / "jev")
            self.assertEqual(path.name, row["request_id"] + ".json")
            self.assertIsNotNone(row["sent_at"])
            self.assertIsNotNone(row["completed_at"])
        # a second client instance (new run) reuses the saved responses: zero calls, zero cost
        t2 = FakeTransport()
        c2 = self.client(t2, run_id="run-2")
        res2 = c2.classify(items(10), QUESTION)
        self.assertEqual(len(t2.calls), 0)
        self.assertEqual([r["status"] for r in res2], ["ok"] * 10)
        self.assertTrue(all(r["cached"] for r in res2))
        self.assertEqual([r["request_id"] for r in res2], [r["request_id"] for r in res])
        self.assertEqual((c2.spent_usd, c2.requests_sent, c2.cached_requests), (0.0, 0, 2))
        # the cache is per item (jev_items), not per packet: another order, other pack sizes and a subset all hit
        t3 = FakeTransport()
        c3 = self.client(t3, run_id="run-3", pack_size=3)
        shuffled = list(reversed(items(10)))[:7]
        res3 = c3.classify(shuffled, QUESTION)
        self.assertEqual(len(t3.calls), 0)
        self.assertEqual([r["item_id"] for r in res3], [i.item_id for i in shuffled])
        self.assertTrue(all(r["cached"] and r["status"] == "ok" for r in res3))
        with store.session(self.cfg, read_only=True) as con:
            self.assertEqual(con.execute("SELECT status, count(*) FROM jev_items GROUP BY 1").fetchall(), [("ok", 10)])

    def test_failed_and_retries(self):
        def flaky(payload, n):
            if n <= 2:
                raise jev.JevHTTPError(429 if n == 1 else 503)
            return ok_response(payload)

        t = FakeTransport(flaky)
        c = self.client(t, workers=1)
        res = c.classify(items(3), QUESTION)
        self.assertEqual([r["status"] for r in res], ["ok"] * 3)
        self.assertEqual((len(t.calls), c.requests_sent, c.attempts), (3, 1, 3))

        t = FakeTransport(lambda p, n: (_ for _ in ()).throw(jev.JevHTTPError(500)))
        c = self.client(t, run_id="run-b", workers=1)
        res = c.classify(items(3, text="Other text"), QUESTION)
        self.assertEqual(len(t.calls), 4)                     # 1 + 3 retries, then failed
        self.assertEqual({r["status"] for r in res}, {"failed"})
        row = [r for r in self.ledger() if r["run_id"] == "run-b"][0]
        self.assertEqual((row["status"], row["http_status"], row["cost_usd"]), ("failed", 500, 0.0))
        # failed rows may be resent
        t2 = FakeTransport()
        self.client(t2, run_id="run-c").classify(items(3, text="Other text"), QUESTION)
        self.assertEqual(len(t2.calls), 1)

    def test_invalid_answer_isolated_and_invalid_envelope(self):
        def behaviour(payload, n):
            r = ok_response(payload)
            r["answers"]["item_1__fit"]["probabilities"]["core"] = 0.3
            return r

        res = self.client(FakeTransport(behaviour)).classify(items(3), QUESTION)
        self.assertEqual([r["status"] for r in res], ["ok", "failed", "ok"])
        self.assertEqual(res[1]["error"], "invalid_answer:probability_sum_invalid")

        t = FakeTransport(lambda p, n: ok_response(p, model="someone/else"))
        c = self.client(t, run_id="run-x")
        res = c.classify(items(2, text="Envelope case"), QUESTION)
        self.assertEqual({r["error"] for r in res}, {"invalid_response:model_mismatch"})
        row = [r for r in self.ledger() if r["run_id"] == "run-x"][0]
        self.assertEqual((row["status"], row["cost_basis"]), ("failed", "provider_reported"))
        self.assertTrue(Path(row["response_path"]).is_file())      # the paid body is kept for inspection

    def test_uncertain_is_never_resent_in_the_same_run(self):
        t = FakeTransport(lambda p, n: (_ for _ in ()).throw(jev.JevUncertain("read timeout")))
        c = self.client(t, run_id="run-u", budget=1.0)
        res = c.classify(items(3), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"uncertain"})
        self.assertEqual(len(t.calls), 1)
        self.assertEqual(self.ledger()[0]["status"], "uncertain")
        self.assertEqual(self.ledger()[0]["cost_basis"], "reservation_estimate")
        self.assertGreater(c.spent_usd, 0)                   # an unknown outcome keeps its reservation
        c.classify(items(3), QUESTION)                         # same client
        ok = FakeTransport()
        self.client(ok, run_id="run-u", retry_uncertain=True).classify(items(3), QUESTION)   # same run id
        self.assertEqual((len(t.calls), len(ok.calls)), (1, 0))
        # later run: reported, not resent, unless retry_uncertain=True
        c2 = self.client(ok, run_id="run-v")
        res = c2.classify(items(3), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"uncertain"})
        self.assertEqual(len(ok.calls), 0)
        self.assertEqual(c2.uncertain_not_resent[0]["previous_run_id"], "run-u")
        res = self.client(ok, run_id="run-w", retry_uncertain=True).classify(items(3), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"ok"})
        self.assertEqual(len(ok.calls), 1)
        # the resend is its own row: the uncertain send and its reserved charge stay in the ledger
        self.assertEqual(sorted((r["run_id"], r["status"]) for r in self.ledger()),
                         [("run-u", "uncertain"), ("run-w", "ok")])

    def test_crashed_sent_row_counts_as_uncertain(self):
        pk = jev.build_packets(items(2), QUESTION)[0][0]
        rid = pk.request_id + "-deadbeef"
        with store.session(self.cfg) as con:
            store.upsert_many(con, "jev_requests", jev.LEDGER_COLS, [
                (rid, "old-run", "l1", pk.payload_sha256, jev.MODEL, 2, 2, "sent", None, None, None,
                 None, None, store.now_utc(), None, None, None, "openrouter")])
            store.upsert_many(con, "jev_items", jev.ITEM_COLS, [
                (jev.item_key(QUESTION, it.issuer, it.text), rid, "old-run", "l1", i, "sent", None, None, None,
                 store.now_utc(), 0, None) for i, it in enumerate(items(2))])
        t = FakeTransport()
        res = self.client(t, run_id="new-run").classify(items(2), QUESTION)
        self.assertEqual(({r["status"] for r in res}, len(t.calls)), ({"uncertain"}, 0))


class BudgetTests(JevTestBase):
    def test_budget_stop_with_reservations_under_concurrency(self):
        its = items(80)
        packets = jev.build_packets(its, QUESTION, pack_size=4)[0]
        self.assertEqual(len(packets), 20)
        est = packets[0].est_cost_usd
        cost = est * 1.1                                    # actual cost a bit above the estimate
        violations = []
        holder = {}

        def behaviour(payload, n):
            c = holder["client"]
            if c.spent_usd + c.reserved_usd > c.budget_usd + 1e-12:
                violations.append((c.spent_usd, c.reserved_usd))
            return ok_response(payload, cost=cost)

        t = FakeTransport(behaviour, latency=0.03)
        budget = est * 1.25 * 9.5                            # 9 reservations fit; with actual costs at most 10
        c = self.client(t, budget=budget, workers=6, pack_size=4)
        holder["client"] = c
        res = c.classify(its, QUESTION)
        self.assertEqual(violations, [])
        self.assertLessEqual(c.spent_usd, budget + 1e-12)
        self.assertGreater(t.max_active, 1)                  # really concurrent
        sent = len(t.calls)
        self.assertTrue(9 <= sent <= 10, sent)
        self.assertEqual(c.requests_sent, sent)
        statuses = [r["status"] for r in res]
        self.assertEqual(statuses.count("ok"), 4 * sent)
        self.assertEqual(statuses.count("skipped_budget"), 80 - 4 * sent)
        self.assertEqual(statuses[:4 * sent], ["ok"] * (4 * sent))      # dispatched in order
        self.assertAlmostEqual(c.reserved_usd, 0.0, places=12)
        self.assertEqual(c.stop_reason, "budget")
        self.assertEqual(len(self.ledger()), sent)            # nothing written for packets never sent

    def test_zero_budget_sends_nothing_but_cache_still_serves(self):
        t = FakeTransport()
        self.client(t, run_id="r1").classify(items(4), QUESTION)
        t0 = FakeTransport()
        res = self.client(t0, run_id="r2", budget=0.0).classify(items(6), QUESTION)
        self.assertEqual(len(t0.calls), 0)
        # per-item cache: the 4 items answered before are served although the packet differs
        self.assertEqual([r["status"] for r in res], ["ok"] * 4 + ["skipped_budget"] * 2)
        res = self.client(t0, run_id="r3", budget=0.0).classify(items(4), QUESTION)
        self.assertEqual([r["status"] for r in res], ["ok"] * 4)

    def test_estimate_basics(self):
        c = self.client(FakeTransport(), pack_size=8)
        est = c.estimate(items(20) + [jev.Item("e", "E", "")], QUESTION)
        packets = jev.build_packets(items(20), QUESTION, pack_size=8)[0]
        self.assertEqual((est["requests"], est["items"], est["skipped_empty"]), (3, 20, 1))
        self.assertEqual(est["est_input_tokens"], sum(p.est_input_tokens for p in packets))
        self.assertAlmostEqual(est["est_cost_usd"], est["est_input_tokens"] * 0.042e-6, places=8)
        self.assertIn("calibrated", est["basis"])


class UnavailableAndSecretTests(JevTestBase):
    def test_401_402_403_stop_with_no_further_calls(self):
        for status in (401, 402, 403):
            t = FakeTransport(lambda p, n, s=status: (_ for _ in ()).throw(jev.JevHTTPError(s)))
            c = self.client(t, run_id=f"run-{status}", workers=1)
            with self.assertRaises(jev.JevUnavailable):
                c.classify(items(40, text=f"status {status}"), QUESTION)
            self.assertEqual(len(t.calls), 1, status)
            row = [r for r in self.ledger() if r["run_id"] == f"run-{status}"]
            self.assertEqual([(r["status"], r["http_status"]) for r in row], [("failed", status)])
            with self.assertRaises(jev.JevUnavailable):     # the client stays stopped
                c.classify(items(2, text="later"), QUESTION)
            self.assertEqual(len(t.calls), 1)

    def test_401_under_concurrency_starts_nothing_after_it(self):
        refused_at = []

        def behaviour(payload, n):
            if n == 1:
                refused_at.append(time.monotonic())
                raise jev.JevHTTPError(401)
            time.sleep(0.05)
            return ok_response(payload)

        t = FakeTransport(behaviour)
        c = self.client(t, workers=4, pack_size=2)
        c._limiter = jev.RateLimiter(50.0)
        with self.assertRaises(jev.JevUnavailable):
            c.classify(items(40), QUESTION)
        self.assertLessEqual(len(t.calls), 4)
        self.assertTrue(all(ts <= refused_at[0] + 0.005 for ts in t.call_times), (t.call_times, refused_at))
        statuses = {r["status"] for r in self.ledger()}
        self.assertNotIn("sent", statuses)                    # in-flight requests were recorded

    def test_missing_key_raises_before_any_request_or_row(self):
        opener = FakeOpener([])
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}):
            c = self.client(jev.UrllibTransport(self.cfg, opener=opener))
            with self.assertRaises(jev.JevUnavailable):
                c.classify(items(3), QUESTION)
        self.assertEqual(opener.requests, [])
        self.assertEqual(self.ledger(), [])

    def test_key_never_leaks(self):
        pk = jev.build_packets(items(2), QUESTION)[0][0]
        pk2 = jev.build_packets(items(2, text="second batch"), QUESTION)[0][0]
        opener = FakeOpener([
            (200, json.dumps(ok_response(pk.payload)).encode()),
            (401, json.dumps({"error": {"message": f"bad key {FAKE_KEY}"}}).encode()),
        ])
        transport = jev.UrllibTransport(self.cfg, opener=opener)
        c = self.client(transport, workers=1)
        res = c.classify(items(2), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"ok"})
        self.assertEqual(opener.requests[0]["auth"], "Bearer " + FAKE_KEY)
        self.assertEqual(opener.requests[0]["url"], jev.ENDPOINT)
        self.assertEqual(json.loads(opener.requests[0]["body"]), pk.payload)
        with self.assertRaises(jev.JevUnavailable) as cm:
            c.classify(items(2, text="second batch"), QUESTION)
        texts = [str(cm.exception), repr(cm.exception), repr(c), repr(transport)]
        texts += [json.dumps(r, default=str) for r in self.ledger()]
        texts += [p.read_text(errors="replace") for p in self.home.rglob("*") if p.is_file() and p.suffix != ".duckdb"]
        texts.append((self.home / "jevscreen.duckdb").read_bytes().decode("latin-1"))
        for text in texts:
            self.assertNotIn(FAKE_KEY, text)
        row = [r for r in self.ledger() if r["payload_sha256"] == pk2.payload_sha256][0]
        self.assertEqual((row["status"], row["http_status"]), ("failed", 401))
        self.assertIn(jev.KEY_PLACEHOLDER, row["error"])

    def test_timeout_after_send_is_uncertain(self):
        opener = FakeOpener([TimeoutError("read timed out")])
        c = self.client(jev.UrllibTransport(self.cfg, opener=opener))
        res = c.classify(items(2), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"uncertain"})
        self.assertEqual(len(opener.requests), 1)


class DryRunTests(JevTestBase):
    def test_dry_run_is_pure(self):
        t = FakeTransport()
        c = jev.JevClient(self.cfg, run_id="dry", layer="l1", budget_usd=1.0, dry_run=True, transport=t)
        res = c.classify(items(10) + [jev.Item("e", "E", "")], QUESTION)
        self.assertEqual([r["status"] for r in res], ["dry_run"] * 10 + ["failed"])
        self.assertTrue(all(r["request_id"] for r in res[:10]))
        self.assertEqual(c.estimate(items(10), QUESTION)["requests"], 2)
        self.assertEqual(len(t.calls), 0)
        self.assertFalse((self.home / "jevscreen.duckdb").exists())
        self.assertFalse((self.home / "jev").exists())
        # a dry run never reads the key either
        with mock.patch.object(Config, "openrouter_key", side_effect=AssertionError("key read")):
            jev.JevClient(self.cfg, run_id="dry", layer="l1", budget_usd=1.0, dry_run=True).classify(items(2), QUESTION)


class CalibrationTests(unittest.TestCase):
    def test_token_model_matches_journals(self):
        fx = json.loads((FIXTURES / "jev_calibration.json").read_text())
        fit = fx["fit"]
        self.assertEqual((jev.TOKENS_PER_REQUEST, jev.TOKENS_PER_QUESTION, jev.TOKENS_PER_ASCII_CHAR,
                          jev.TOKENS_PER_NON_ASCII_CHAR),
                         (fit["per_request"], fit["per_question"], fit["per_ascii_char"], fit["per_non_ascii_char"]))
        errs = []
        for r in fx["rows"]:
            pred = (jev.TOKENS_PER_REQUEST + jev.TOKENS_PER_QUESTION * r["nq"]
                    + jev.TOKENS_PER_ASCII_CHAR * (r["qa"] + r["sa"]) + jev.TOKENS_PER_NON_ASCII_CHAR * (r["qn"] + r["sn"]))
            errs.append(abs(pred - r["inp"]) / r["inp"])
        self.assertLess(max(errs), 0.02)
        self.assertLess(sum(errs) / len(errs), 0.005)
        # measured per-request means by pack size / question bank
        by = fx["by_source"]
        self.assertAlmostEqual(by["pilot-single"]["mean_input_tokens"], 1336.7, delta=1)
        self.assertAlmostEqual(by["full-primary"]["mean_input_tokens"], 10174.1, delta=1)
        self.assertAlmostEqual(by["three-world"]["mean_input_tokens"], 19015.3, delta=1)

    def test_out_of_sample_live_request(self):
        fx = json.loads((FIXTURES / "jev_response_smoke.json").read_text())
        est = jev.estimate_payload_tokens(fx["request"])
        actual = fx["response"]["usage"]["input_tokens"]
        self.assertEqual(actual, 1176)
        self.assertLess(abs(est - actual) / actual, 0.02)

    def test_cost_per_item_for_this_client(self):
        # One English question per item (~200-char instructions + isolation prefix), 2,500-char texts, packs of 8:
        # ~ 235/8 + 31 + 0.1953 x (question ~600 chars + text 2,500) ~ 670 tokens/item ~ US$2.8e-5 per item-question.
        its = [jev.Item(f"i{i}", f"Issuer {i}", ("Designs and sells industrial power equipment. " * 60)[:2500])
               for i in range(8)]
        pk = jev.build_packets(its, QUESTION, pack_size=8)[0][0]
        per_item = pk.est_input_tokens / 8
        self.assertTrue(600 <= per_item <= 720, per_item)
        self.assertAlmostEqual(pk.est_cost_usd / 8, per_item * 0.042e-6, places=9)
        self.assertLess(pk.est_cost_usd / 8, 3.1e-5)
        # a short Chinese description costs more per char: non-ASCII ~1.09 tokens/char vs ASCII ~0.195
        cjk = jev.estimate_payload_tokens({"questions": {}, "state": {"text": "数据中心" * 100}})
        ascii_ = jev.estimate_payload_tokens({"questions": {}, "state": {"text": "abcd" * 100}})
        self.assertGreater(cjk - 235, 4 * (ascii_ - 235))


class ReviewFixTests(JevTestBase):
    """Fixes from the review: redaction before truncation, one ledger row per send, price drift, gateway timeouts,
    partial results on JevUnavailable, interrupt drain, the one-process lock, duplicates."""

    LONG_KEY = "sk-or-v1-" + "a" * 64          # 73 chars, fake

    def _secret_fragments(self, key: str, n: int = 12) -> list[str]:
        return [key[i:i + n] for i in range(0, len(key) - n + 1)]

    def test_error_body_is_redacted_before_it_is_cut(self):
        key = self.LONG_KEY
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": key}):
            # 401: key straddles the old 300-byte cut
            body401 = b"x" * 228 + b"Invalid key " + key.encode()
            # 400: same body, becomes each item's error too
            body400 = b"y" * 228 + b"Invalid key " + key.encode()
            # 500 x4 with the key straddling the 2049-byte read limit
            body500 = b"z" * 2020 + key.encode() + b" tail"
            opener = FakeOpener([(401, body401)])
            c = self.client(jev.UrllibTransport(self.cfg, opener=opener), run_id="r401")
            with self.assertRaises(jev.JevUnavailable) as cm:
                c.classify(items(2), QUESTION)
            opener = FakeOpener([(400, body400)])
            res400 = self.client(jev.UrllibTransport(self.cfg, opener=opener), run_id="r400").classify(
                items(2, text="four hundred"), QUESTION)
            opener = FakeOpener([(500, body500)] * 4)
            res500 = self.client(jev.UrllibTransport(self.cfg, opener=opener), run_id="r500").classify(
                items(2, text="five hundred"), QUESTION)
        self.assertEqual({r["status"] for r in res400}, {"failed"})
        self.assertIn(jev.KEY_PLACEHOLDER, res400[0]["error"])
        texts = [str(cm.exception), json.dumps(res400), json.dumps(res500)]
        texts += [json.dumps(r, default=str) for r in self.ledger()]
        with store.session(self.cfg, read_only=True) as con:
            texts += [json.dumps(r, default=str) for r in con.execute("SELECT * FROM jev_items").fetchall()]
        for text in texts:
            for frag in self._secret_fragments(key):
                self.assertNotIn(frag, text)
        self.assertNotIn("sk-or-v1-aaaa", "".join(texts))

    def test_strip_partial_secret(self):
        key = self.LONG_KEY
        self.assertEqual(jev.redact_cut("abc " + key[:30], [key], 300, "<k>"), "abc ")
        self.assertEqual(jev.redact_cut("abc " + key + " def", [key], 300, "<k>"), "abc <k> def")
        self.assertEqual(jev.redact_cut("x" * 10 + key, [key], 12, "<k>"), "x" * 10 + "<k"[:2])

    def test_resend_keeps_every_charge_in_the_ledger(self):
        t1 = FakeTransport(lambda p, n: ok_response(p, cost=0.5, model="someone/else"))
        c1 = self.client(t1, run_id="run1")
        res = c1.classify(items(3), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"failed"})
        t2 = FakeTransport(lambda p, n: ok_response(p, cost=0.25))
        c2 = self.client(t2, run_id="run2")
        res = c2.classify(items(3), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"ok"})
        rows = self.ledger()
        self.assertEqual(sorted((r["run_id"], r["status"], r["cost_usd"]) for r in rows),
                         [("run1", "failed", 0.5), ("run2", "ok", 0.25)])
        self.assertEqual(len({r["payload_sha256"] for r in rows}), 1)
        self.assertEqual(len({r["request_id"] for r in rows}), 2)
        self.assertAlmostEqual(sum(r["cost_usd"] for r in rows), c1.spent_usd + c2.spent_usd)

    def test_price_drift_overshoots_by_at_most_one_request(self):
        its = items(60)
        pk0 = jev.build_packets(its[:1], QUESTION, pack_size=1)[0][0]
        est = pk0.est_cost_usd
        budget = est * 1.25 * 16.5
        t = FakeTransport(lambda p, n: ok_response(p, cost=jev.estimate_payload_tokens(p) * 0.042e-6 * 3),
                          latency=0.01)
        c = self.client(t, budget=budget, workers=16, pack_size=1)
        res = c.classify(its, QUESTION)
        one = est * 3 * 1.05
        self.assertLessEqual(c.spent_usd, budget + one)
        self.assertAlmostEqual(c.price_drift_ratio, 3.0, places=2)
        self.assertGreater(sum(r["status"] == "skipped_budget" for r in res), 0)
        self.assertAlmostEqual(c.reserved_usd, 0.0, places=12)

    def test_gateway_timeout_is_uncertain_and_not_retried(self):
        t = FakeTransport(lambda p, n: (_ for _ in ()).throw(jev.JevHTTPError(504)))
        c = self.client(t, run_id="r504")
        res = c.classify(items(2), QUESTION)
        self.assertEqual(len(t.calls), 1)
        self.assertEqual({r["status"] for r in res}, {"uncertain"})
        row = self.ledger()[0]
        self.assertEqual((row["status"], row["http_status"], row["cost_basis"]), ("uncertain", 504,
                                                                                   "reservation_estimate"))
        self.assertGreater(row["cost_usd"], 0)
        # per-attempt statuses are kept for reconciliation
        t = FakeTransport(lambda p, n: (_ for _ in ()).throw(jev.JevHTTPError(503 if n < 3 else 502)))
        res = self.client(t, run_id="r503").classify(items(2, text="retry trail"), QUESTION)
        self.assertEqual(len(t.calls), 3)
        self.assertEqual({r["status"] for r in res}, {"uncertain"})
        self.assertIn("attempts: 503, 503, 502", res[0]["error"])

    def test_unavailable_carries_completed_results(self):
        def behaviour(payload, n):
            if n == 2:
                raise jev.JevHTTPError(402)
            return ok_response(payload, cost=0.01)

        c = self.client(FakeTransport(behaviour), workers=1)
        with self.assertRaises(jev.JevUnavailable) as cm:
            c.classify(items(24), QUESTION)
        res = cm.exception.results
        self.assertEqual(len(res), 24)
        self.assertEqual([r["status"] for r in res[:8]], ["ok"] * 8)
        self.assertEqual({r["status"] for r in res[8:]}, {"failed"})
        self.assertAlmostEqual(c.spent_usd, 0.01)

    def test_interrupt_drains_and_records_in_flight(self):
        real_wait = jev.wait
        calls = {"n": 0}

        def interrupting_wait(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise KeyboardInterrupt
            return real_wait(*a, **kw)

        t = FakeTransport(lambda p, n: ok_response(p, cost=0.001), latency=0.05)
        c = self.client(t, run_id="r-int")
        with mock.patch.object(jev, "wait", interrupting_wait), self.assertRaises(KeyboardInterrupt):
            c.classify(items(24), QUESTION)
        self.assertEqual(len(t.calls), 1)                      # the probe; nothing started after the interrupt
        rows = self.ledger()
        self.assertEqual([(r["status"], r["cost_usd"]) for r in rows], [("ok", 0.001)])
        with store.session(self.cfg, read_only=True) as con:
            self.assertEqual(con.execute("SELECT status, count(*) FROM jev_items GROUP BY 1").fetchall(),
                             [("ok", 8)])
        # a rerun reuses the paid answers
        t2 = FakeTransport()
        res = self.client(t2, run_id="r-int-2").classify(items(8), QUESTION)
        self.assertEqual((len(t2.calls), {r["cached"] for r in res}), (0, {True}))

    def test_second_process_is_refused(self):
        """The paying lock is 'jev'; an older version's 'openrouter-jev' lock blocks too (and is held by us, so the
        older version is blocked in turn)."""
        import fcntl
        self.assertEqual((jev.LOCK_BUDGET, jev.LEGACY_LOCK_BUDGETS), ("jev", ("openrouter-jev",)))
        for name in ("jev", "openrouter-jev"):
            lock = self.home / "locks" / f"{name}.lock"
            lock.parent.mkdir(parents=True, exist_ok=True)
            with open(lock, "a+") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)   # as another process would hold it
                t = FakeTransport()
                with self.assertRaises(jev.JevBusy):
                    self.client(t).classify(items(3), QUESTION)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            self.assertEqual(len(t.calls), 0, name)
            self.assertFalse((self.home / "jevscreen.duckdb").exists() and self.ledger())
        self.assertTrue(issubclass(jev.JevBusy, jev.JevUnavailable))

    def test_the_paying_lock_holds_both_names(self):
        """While classify pays, an older jev-screen asking for 'openrouter-jev' is refused, and a new one asking for
        'jev' too; both are free afterwards."""
        from jevscreen import guard
        seen = {}

        def probe(p, n):
            for name in ("jev", "openrouter-jev"):
                try:
                    with guard.budget_lock(self.cfg, name):
                        seen[name] = "free"
                except guard.Busy:
                    seen[name] = "held"
            return ok_response(p)
        self.client(FakeTransport(probe)).classify(items(1), QUESTION)
        self.assertEqual(seen, {"jev": "held", "openrouter-jev": "held"})
        for name in ("jev", "openrouter-jev"):
            with guard.budget_lock(self.cfg, name):
                pass

    def test_duplicate_items_are_sent_once(self):
        its = items(3) + [jev.Item("dup", "Company 1", items(3)[1].text)]
        t = FakeTransport()
        res = self.client(t).classify(its, QUESTION)
        self.assertEqual(len(t.calls[0]["questions"]), 3)
        self.assertEqual(res[3]["item_id"], "dup")
        self.assertEqual((res[3]["status"], res[3]["request_id"]), ("ok", res[1]["request_id"]))


# ---------------------------------------------------------------------------------------------------------------
# urllib fakes (no sockets)

class ReadIndexAndConfidenceTests(JevTestBase):
    """Question.read (repeat reads) and Jev's per-answer confidence."""

    # Captured from the code BEFORE Question.read existed (HEAD 7c80218): read-0 keys must never change, or every
    # cached answer in jev_items would be re-bought. Issuers and texts are invented.
    PINNED = (
        ("Company 0", "Makes power transformers and switchgear for data centres. (0)", "5fe05f20360dc48d7b7e186b4a41abbe"),
        ("北辰信安", "公司主营终端安全管理与身份认证。", "91670491c26a790632bbbdc6dd89e909"),
    )

    def test_read0_item_key_is_pinned(self):
        q0 = jev.Question(QUESTION.key, QUESTION.instructions, dict(QUESTION.criteria), read=0)
        for issuer, text, digest in self.PINNED:
            self.assertEqual(jev.item_key(QUESTION, issuer, text), digest)
            self.assertEqual(jev.item_key(q0, issuer, text), digest)
        # the real L2 question (no rules) keeps its key too
        from jevscreen import screen
        q2 = screen.build_l2_question("企业 AI agent 的身份与权限管控")
        self.assertEqual(jev.item_key(q2, "Sailwise Identity", "Identity security for AI agents."),
                         "46635ff7d9bed92ad2e90370aee5694a")

    def test_read_salts_the_key_but_not_the_payload(self):
        issuer, text, digest = self.PINNED[0]
        qs = [jev.Question(QUESTION.key, QUESTION.instructions, dict(QUESTION.criteria), read=r) for r in (0, 1, 2)]
        keys = [jev.item_key(q, issuer, text) for q in qs]
        self.assertEqual(keys[0], digest)
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual(keys[1], jev.item_key(qs[1], issuer, text))        # deterministic
        payloads = [jev.make_payload([(issuer, text)], q) for q in qs]
        self.assertEqual(payloads[0], payloads[1])
        self.assertEqual(payloads[0], payloads[2])
        self.assertNotIn("read", json.dumps(payloads[1]))
        pks = [jev.build_packets(items(3), q)[0][0] for q in qs]
        self.assertEqual({p.payload_sha256 for p in pks}, {pks[0].payload_sha256})
        for bad in (-1, "1", 1.0, True, None):
            with self.assertRaises(ValueError):
                jev.validate_question(jev.Question("k", "x", {"a": "1", "b": "2"}, read=bad))

    def test_answer_confidence(self):
        self.assertEqual(jev.answer_confidence({"type": "choice", "confidence": 0.9}), 0.9)
        self.assertEqual(jev.answer_confidence({"confidence": 1}), 1.0)
        self.assertIsInstance(jev.answer_confidence({"confidence": 0}), float)
        for absent in ({"type": "choice"}, {"confidence": None}, None, "x"):
            self.assertIsNone(jev.answer_confidence(absent))
        for bad in (1.5, -0.1, "0.9", True, float("nan"), float("inf"), [0.5]):
            with self.assertRaises(jev.InvalidResponse) as cm:
                jev.answer_confidence({"confidence": bad})
            self.assertEqual(cm.exception.detail, "confidence_invalid")
        # parse_response: 4-tuples; an invalid confidence fails only its own item
        pk = jev.build_packets(items(3), QUESTION)[0][0]
        resp = ok_response(pk.payload)
        resp["answers"]["item_0__fit"]["confidence"] = 0.42
        del resp["answers"]["item_1__fit"]["confidence"]
        resp["answers"]["item_2__fit"]["confidence"] = 7
        _, out = jev.parse_response(resp, pk.qids, list(QUESTION.criteria))
        self.assertEqual([len(o) for o in out], [4, 4, 4])
        self.assertEqual((out[0][0], out[0][2], out[0][3]), ("core", None, 0.42))
        self.assertEqual((out[1][0], out[1][2], out[1][3]), ("core", None, None))
        self.assertEqual(out[2], (None, {}, "invalid_answer:confidence_invalid", None))

    def test_item_rows_carry_read_index_and_confidence(self):
        self.assertEqual(jev.ITEM_COLS[-2:], ("read_index", "confidence"))
        q1 = jev.Question(QUESTION.key, QUESTION.instructions, dict(QUESTION.criteria), read=1)
        t = FakeTransport()
        res0 = self.client(t, run_id="run-r0").classify(items(3), QUESTION)
        self.assertEqual([(r["status"], r["confidence"], r["cached"]) for r in res0], [("ok", 0.9, False)] * 3)
        # read 1 of the same items and question is a fresh draw, not a cache hit of read 0
        res1 = self.client(t, run_id="run-r1").classify(items(3), q1)
        self.assertEqual(len(t.calls), 2)
        self.assertEqual(t.calls[0], t.calls[1])                           # identical payload bytes
        self.assertEqual([(r["status"], r["cached"]) for r in res1], [("ok", False)] * 3)
        self.assertNotEqual(res0[0]["request_id"], res1[0]["request_id"])
        # reruns of either read are served from the cache, confidence included
        t2 = FakeTransport()
        for q, first in ((QUESTION, res0), (q1, res1)):
            again = self.client(t2, run_id="run-r2").classify(items(3), q)
            self.assertEqual([(r["cached"], r["confidence"], r["request_id"]) for r in again],
                             [(True, 0.9, r["request_id"]) for r in first])
        self.assertEqual(len(t2.calls), 0)
        with store.session(self.cfg, read_only=True) as con:
            rows = con.execute("SELECT run_id, read_index, confidence, status FROM jev_items "
                               "ORDER BY run_id, position").fetchall()
        self.assertEqual(rows, [("run-r0", 0, 0.9, "ok")] * 3 + [("run-r1", 1, 0.9, "ok")] * 3)

    def test_sent_and_failed_rows_keep_read_index(self):
        q2 = jev.Question(QUESTION.key, QUESTION.instructions, dict(QUESTION.criteria), read=2)
        seen = []

        def behaviour(payload, n):
            with store.session(self.cfg, wait_s=5) as con:
                seen.extend(con.execute("SELECT status, read_index, confidence FROM jev_items").fetchall())
            resp = ok_response(payload)
            resp["answers"]["item_1__fit"]["probabilities"] = {"core": 2.0}
            return resp

        res = self.client(FakeTransport(behaviour), run_id="run-s").classify(items(2), q2)
        self.assertEqual(seen, [("sent", 2, None)] * 2)
        self.assertEqual([(r["status"], r["confidence"]) for r in res], [("ok", 0.9), ("failed", None)])
        with store.session(self.cfg, read_only=True) as con:
            self.assertEqual(con.execute("SELECT status, read_index, confidence FROM jev_items ORDER BY position")
                             .fetchall(), [("ok", 2, 0.9), ("failed", 2, None)])


class _Resp(io.BytesIO):
    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeOpener:
    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []

    def open(self, req, timeout=None):
        self.requests.append({"url": req.full_url, "auth": req.get_header("Authorization"), "body": req.data})
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        status, body = step
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", {}, io.BytesIO(body))
        return _Resp(status, body)


if __name__ == "__main__":
    unittest.main()

"""Jev providers (TypeSafe official API, OpenRouter, Vercel AI Gateway): selection, payloads, response normalisation,
cost accounting, retries, cache isolation, keys, doctor and the quickstart key question. No network and no paid
calls: every transport and opener is a local fake, and every key is an invented test value."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import consent, doctor, jev, keys, quickstart, store  # noqa: E402
from jevscreen.config import Config  # noqa: E402

FAKE_OR = "sk-or-v1-FAKE-openrouter-0123456789abcdef"
FAKE_TS = "ts-FAKE-typesafe-0123456789abcdef"
FAKE_VC = "vck_FAKE-vercel-0123456789abcdef"
JEV_ENVS = ("OPENROUTER_API_KEY", "JEVSCREEN_OPENROUTER_KEY_FILE", "TYPESAFE_API_KEY", "JEVSCREEN_TYPESAFE_KEY_FILE",
            "AI_GATEWAY_API_KEY", "JEVSCREEN_VERCEL_KEY_FILE", "JEVSCREEN_JEV_PROVIDER")

QUESTION = jev.Question(key="fit", instructions="Does state.issuer make paper cups, per state.text?",
                        criteria={"yes": "It says so.", "no": "It does not."})


def items(n: int, text: str = "Northwind Paper Cups Ltd makes paper cups.") -> list[jev.Item]:
    return [jev.Item(item_id=f"t/{i}", issuer=f"Northwind {i}", text=f"{text} ({i})") for i in range(n)]


def answers(payload: dict, label: str = "yes") -> dict:
    other = "no" if label == "yes" else "yes"
    return {qid: {"type": "choice", "choice": label, "probabilities": {label: 0.9, other: 0.1}, "confidence": 0.8}
            for qid in payload["questions"]}


def typesafe_response(payload: dict, model: str = "jev-1.13.0") -> dict:
    return {"model": model, "answers": answers(payload), "usage": {"input_tokens": 2000, "output_tokens": 40}}


def vercel_response(payload: dict, cost: str | None = "0.0000840", camel: bool = False) -> dict:
    usage = {"inputTokens": 2000, "outputTokens": 40} if camel else {"input_tokens": 2000, "output_tokens": 40}
    out = {"model": "typesafe-ai/jev", "answers": answers(payload), "usage": usage}
    if cost is not None:
        gw = {"routing": {"originalModelId": "typesafe-ai/jev", "finalProvider": "typesafe-ai"}, "cost": cost,
              "marketCost": cost, "surchargeCost": "0", "gatewayCost": cost, "generationId": "gen_test"}
        out["providerMetadata" if camel else "provider_metadata"] = {"gateway": gw}
    return out


def openrouter_response(payload: dict) -> dict:
    return {"model": "typesafe/jev-1.13", "answers": answers(payload),
            "usage": {"input_tokens": 2000, "output_tokens": 40, "cost": 0.000084}}


class Fake:
    def __init__(self, fn):
        self.fn = fn
        self.calls: list[dict] = []

    def __call__(self, payload):
        self.calls.append(payload)
        return self.fn(payload)


class _Resp(io.BytesIO):
    def __init__(self, status: int, body: bytes):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Opener:
    def __init__(self, script: list):
        self.script = list(script)
        self.requests: list[dict] = []

    def open(self, req, timeout=None):
        self.requests.append({"url": req.full_url, "auth": req.get_header("Authorization"), "body": req.data,
                              "method": req.get_method()})
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        status, body, *hdrs = step
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", hdrs[0] if hdrs else {}, io.BytesIO(body))
        return _Resp(status, body)

    __call__ = open


class Base(unittest.TestCase):
    env: dict[str, str] = {}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self._saved = {k: os.environ.pop(k) for k in JEV_ENVS if k in os.environ}   # safe_env dropped them already
        self.patch = mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home), **self.env})
        self.patch.start()
        self.cfg = Config(home=self.home)

    def tearDown(self) -> None:
        self.patch.stop()
        os.environ.update(self._saved)
        self.tmp.cleanup()

    def client(self, transport, **kw) -> jev.JevClient:
        kw.setdefault("backoff_s", 0.0)
        c = jev.JevClient(self.cfg, run_id=kw.pop("run_id", "run-p"), layer="l1", budget_usd=kw.pop("budget", 1.0),
                          transport=transport, **kw)
        c._limiter = jev.RateLimiter(1000.0)
        return c

    def ledger(self) -> list[dict]:
        with store.session(self.cfg, read_only=True) as con:
            cur = con.execute(f"SELECT {', '.join(jev.LEDGER_COLS)} FROM jev_requests ORDER BY sent_at, request_id")
            return [dict(zip(jev.LEDGER_COLS, r)) for r in cur.fetchall()]


class ResolveTests(Base):
    def test_default_is_openrouter_without_any_key(self):
        prov, why = jev.resolve_provider(self.cfg)
        self.assertEqual((prov.name, why), ("openrouter", "default"))

    def test_first_configured_key_wins_in_order(self):
        # no saved choice (keys from the environment only): OpenRouter first, where the old cache lives
        with mock.patch.dict(os.environ, {"AI_GATEWAY_API_KEY": FAKE_VC}):
            self.assertEqual(jev.resolve_provider(self.cfg)[0].name, "vercel")
            with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS}):
                self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["typesafe"], "key"))
                with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_OR}):
                    self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["openrouter"], "key"))
                    self.assertEqual(jev.configured_providers(self.cfg), ["openrouter", "typesafe", "vercel"])

    def test_explicit_choice_wins_even_without_its_key(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS, "JEVSCREEN_JEV_PROVIDER": " Vercel "}):
            self.assertEqual(jev.resolve_provider(self.cfg), (jev.PROVIDERS["vercel"], "explicit"))

    def test_unknown_provider_is_refused_before_anything_is_sent(self):
        t = Fake(typesafe_response)
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "anthropic"}):
            with self.assertRaises(jev.ProviderError):
                jev.resolve_provider(self.cfg)
            c = self.client(t)
            with self.assertRaises(jev.JevUnavailable) as cm:
                c.classify(items(2), QUESTION)
        self.assertIn("JEVSCREEN_JEV_PROVIDER", str(cm.exception))
        self.assertEqual(t.calls, [])
        self.assertFalse((self.home / "jevscreen.duckdb").exists())

    def test_provider_table(self):
        P = jev.PROVIDERS
        self.assertEqual((P["typesafe"].endpoint, P["typesafe"].model, P["typesafe"].pinned),
                         ("https://api.typesafe.ai/v1/systemone", "jev-1.13.0", True))
        self.assertEqual((P["openrouter"].endpoint, P["openrouter"].model),
                         ("https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13"))
        self.assertEqual((P["vercel"].endpoint, P["vercel"].model, P["vercel"].pinned),
                         ("https://ai-gateway.vercel.sh/typesafe/v1/systemone", "typesafe-ai/jev", False))
        self.assertNotIn("latest", P["typesafe"].model)
        self.assertEqual((jev.MODEL, jev.ENDPOINT), (P["openrouter"].model, P["openrouter"].endpoint))


class CacheKeyTests(Base):
    def test_openrouter_keys_unchanged_and_providers_never_share_keys(self):
        legacy = jev.item_key(QUESTION, "Co", "text")
        self.assertEqual(jev.item_key(QUESTION, "Co", "text", "openrouter"), legacy)
        ks = {n: jev.item_key(QUESTION, "Co", "text", n) for n in jev.PROVIDER_ORDER}
        self.assertEqual(len(set(ks.values())), 3)
        q1 = jev.Question(QUESTION.key, QUESTION.instructions, QUESTION.criteria, read=1)
        self.assertNotEqual(jev.item_key(q1, "Co", "text", "typesafe"), ks["typesafe"])

    def test_switching_provider_reads_again_and_never_mixes_answers(self):
        its = items(3)
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": FAKE_OR}):
            first = Fake(openrouter_response)
            res = self.client(first, run_id="r-or").classify(its, QUESTION)
            self.assertEqual({r["status"] for r in res}, {"ok"})
            again = self.client(Fake(openrouter_response), run_id="r-or2").classify(its, QUESTION)
            self.assertTrue(all(r["cached"] for r in again))
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS}):
            ts = Fake(typesafe_response)
            res = self.client(ts, run_id="r-ts").classify(its, QUESTION)
        self.assertEqual(len(ts.calls), 1)                              # nothing reused from OpenRouter
        self.assertEqual(ts.calls[0]["model"], "jev-1.13.0")
        self.assertFalse(any(r["cached"] for r in res))
        rows = self.ledger()
        self.assertEqual([(r["run_id"], r["provider"], r["model"]) for r in rows],
                         [("r-or", "openrouter", "typesafe/jev-1.13"), ("r-ts", "typesafe", "jev-1.13.0")])


class NormaliseTests(Base):
    def test_typesafe_cost_is_tokens_times_list_price(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS}):
            c = self.client(Fake(typesafe_response))
            self.assertEqual(c.provider.name, "typesafe")
            res = c.classify(items(2), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"ok"})
        self.assertEqual(res[0]["confidence"], 0.8)
        row = self.ledger()[0]
        self.assertEqual((row["cost_basis"], row["input_tokens"]), ("token_estimate", 2000))
        self.assertAlmostEqual(row["cost_usd"], 2000 * 0.042e-6)
        self.assertAlmostEqual(c.spent_usd, 2000 * 0.042e-6)

    def test_vercel_cost_from_gateway_metadata(self):
        for camel in (False, True):
            with self.subTest(camel=camel), mock.patch.dict(os.environ, {"AI_GATEWAY_API_KEY": FAKE_VC}):
                c = self.client(Fake(lambda p: vercel_response(p, camel=camel)), run_id=f"v-{camel}")
                res = c.classify(items(2, text=f"camel {camel}"), QUESTION)
                self.assertEqual({r["status"] for r in res}, {"ok"})
                self.assertAlmostEqual(c.spent_usd, 0.000084)
        rows = self.ledger()
        self.assertEqual({(r["cost_basis"], r["provider"], r["input_tokens"]) for r in rows},
                         {("provider_reported", "vercel", 2000)})

    def test_normalize_response_edge_cases(self):
        data = {"model": "typesafe-ai/jev", "answers": {}, "usage": {"input_tokens": 5, "output_tokens": 0},
                "provider_metadata": {"gateway": {"cost": "not a number"}}}
        self.assertNotIn("cost", jev.normalize_response("vercel", data)["usage"])
        self.assertEqual(jev.normalize_response("vercel", "junk"), "junk")
        or_data = {"model": "typesafe/jev-1.13", "usage": {"input_tokens": 1, "output_tokens": 0, "cost": 1e-6}}
        self.assertIs(jev.normalize_response("openrouter", or_data), or_data)
        ts = jev.normalize_response("typesafe", {"model": "jev-1.13.0", "usage": {"inputTokens": 7, "outputTokens": 1,
                                                                                  "cost": "x"}})
        self.assertEqual(ts["usage"], {"input_tokens": 7, "output_tokens": 1})
        self.assertEqual(jev._decimal("0.00001155"), 0.00001155)
        self.assertIsNone(jev._decimal("-1"))
        self.assertIsNone(jev._decimal(float("nan")))

    def test_a_different_version_fails_the_packet(self):
        for returned in ("jev-latest", "jev-1.14.0", "typesafe/jev-1.13"):
            with self.subTest(returned=returned), mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS}):
                c = self.client(Fake(lambda p: typesafe_response(p, model=returned)), run_id=f"m-{returned}")
                res = c.classify(items(2, text=returned), QUESTION)
                self.assertEqual({r["status"] for r in res}, {"failed"})
                self.assertEqual({r["error"] for r in res}, {"invalid_response:model_mismatch"})


class TransportTests(Base):
    def test_each_provider_gets_its_endpoint_key_and_model(self):
        env = {"TYPESAFE_API_KEY": FAKE_TS, "OPENROUTER_API_KEY": FAKE_OR, "AI_GATEWAY_API_KEY": FAKE_VC}
        make = {"typesafe": typesafe_response, "openrouter": openrouter_response, "vercel": vercel_response}
        key = {"typesafe": FAKE_TS, "openrouter": FAKE_OR, "vercel": FAKE_VC}
        with mock.patch.dict(os.environ, env):
            for name in jev.PROVIDER_ORDER:
                with self.subTest(name=name):
                    pk = jev.build_packets(items(2, text=name), QUESTION, model=jev.PROVIDERS[name].model)[0][0]
                    opener = Opener([(200, json.dumps(make[name](pk.payload)).encode())])
                    t = jev.UrllibTransport(self.cfg, opener=opener, provider=name)
                    c = self.client(t, provider=name, run_id=f"t-{name}")
                    res = c.classify(items(2, text=name), QUESTION)
                    self.assertEqual({r["status"] for r in res}, {"ok"})
                    req = opener.requests[0]
                    self.assertEqual((req["url"], req["auth"]), (jev.PROVIDERS[name].endpoint, "Bearer " + key[name]))
                    self.assertEqual(json.loads(req["body"]), pk.payload)
                    self.assertEqual(json.loads(req["body"])["model"], jev.PROVIDERS[name].model)

    def test_missing_key_names_the_provider_and_sends_nothing(self):
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "vercel", "OPENROUTER_API_KEY": FAKE_OR}):
            opener = Opener([])
            c = self.client(jev.UrllibTransport(self.cfg, opener=opener))
            with self.assertRaises(jev.JevUnavailable) as cm:
                c.classify(items(2), QUESTION)
        self.assertIn("Vercel AI Gateway key missing", str(cm.exception))
        self.assertIn("AI_GATEWAY_API_KEY", str(cm.exception))
        self.assertEqual(opener.requests, [])

    def test_529_is_retried_honouring_retry_after_ms(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS}):
            pk = jev.build_packets(items(2), QUESTION, model="jev-1.13.0")[0][0]
            opener = Opener([(529, b'{"message": "overloaded"}', {"retry-after-ms": "20"}),
                             (429, b'{"message": "slow down"}', {"Retry-After": "0"}),
                             (200, json.dumps(typesafe_response(pk.payload)).encode())])
            c = self.client(jev.UrllibTransport(self.cfg, opener=opener))
            with mock.patch.object(c, "_backoff", wraps=c._backoff) as bo:
                res = c.classify(items(2), QUESTION)
        self.assertEqual({r["status"] for r in res}, {"ok"})
        self.assertEqual(len(opener.requests), 3)
        self.assertAlmostEqual(bo.call_args_list[0].args[1], 0.02)
        self.assertIn(529, jev.RETRY_HTTP)

    def test_key_is_redacted_for_every_provider(self):
        with mock.patch.dict(os.environ, {"AI_GATEWAY_API_KEY": FAKE_VC}):
            opener = Opener([(401, json.dumps({"error": {"message": f"bad key {FAKE_VC}"}}).encode())])
            c = self.client(jev.UrllibTransport(self.cfg, opener=opener))
            with self.assertRaises(jev.JevUnavailable) as cm:
                c.classify(items(2), QUESTION)
        self.assertNotIn(FAKE_VC, str(cm.exception))
        row = self.ledger()[0]
        self.assertNotIn(FAKE_VC, json.dumps(row, default=str))
        self.assertIn(jev.KEY_PLACEHOLDER, row["error"])


class KeysTests(Base):
    def test_set_check_clear_new_keys(self):
        out = keys.write_key(self.cfg, "typesafe", FAKE_TS)
        self.assertEqual((out["path"], out["mode"]), (str(self.home / "typesafe_api_key"), "0600"))
        self.assertEqual(self.cfg.jev_key("typesafe"), FAKE_TS)
        keys.write_key(self.cfg, "vercel", f"  '{FAKE_VC}'\n")
        self.assertEqual(self.cfg.jev_key("vercel"), FAKE_VC)
        chk = keys.check(self.cfg, "vercel")
        self.assertEqual((chk["configured"], chk["shape_ok"], chk["source"]), (True, True, "file"))
        self.assertNotIn(FAKE_VC, json.dumps(chk))
        self.assertEqual(jev.resolve_provider(self.cfg)[0].name, "vercel")        # the last Jev key set
        self.assertTrue(keys.clear_key(self.cfg, "vercel")["removed"])
        self.assertEqual(jev.resolve_provider(self.cfg)[0].name, "typesafe")

    def test_wrong_provider_keys_are_refused_with_a_pointer(self):
        with self.assertRaises(keys.KeyProblem) as cm:
            keys.write_key(self.cfg, "vercel", FAKE_OR)
        self.assertIn("keys set openrouter", str(cm.exception))
        with self.assertRaises(keys.KeyProblem) as cm:
            keys.write_key(self.cfg, "openrouter", FAKE_VC)
        self.assertIn("keys set vercel", str(cm.exception))
        with self.assertRaises(keys.KeyProblem) as cm:
            keys.write_key(self.cfg, "typesafe", "short")
        self.assertNotIn("short", str(cm.exception).replace("too short", ""))
        for msg in (str(cm.exception),):
            self.assertNotIn(FAKE_OR, msg)

    def test_from_file_for_vercel_and_typesafe(self):
        f = Path(self.tmp.name) / "my-vercel-key.txt"
        f.write_text(FAKE_VC + "\n")
        os.chmod(f, 0o600)
        out = keys.record_key_file(self.cfg, "vercel", f)
        self.assertEqual((out["recorded"], out["source"]), (True, "recorded-file"))
        self.assertNotIn(str(f), json.dumps(out))
        self.assertEqual(self.cfg.jev_key("vercel"), FAKE_VC)
        self.assertEqual(self.cfg.jev_key_source("vercel"), ("recorded-file", f.resolve()))
        self.assertTrue(keys.presence(self.cfg, "vercel")["recorded"])
        bad = Path(self.tmp.name) / "ts.env"
        bad.write_text(f"TYPESAFE_API_KEY={FAKE_TS}\n")
        with self.assertRaises(keys.KeyProblem) as cm:
            keys.record_key_file(self.cfg, "typesafe", bad)
        self.assertNotIn(FAKE_TS, str(cm.exception))
        with self.assertRaises(keys.KeyProblem):
            keys.record_key_file(self.cfg, "edinet", f)
        keys.clear_key(self.cfg, "vercel")
        self.assertIsNone(self.cfg.jev_key_location("vercel"))


class DoctorTests(Base):
    def by_id(self, checks):
        return {c["id"]: c for c in checks}

    def test_no_key_asks_the_account_question(self):
        c = doctor.jev_provider_check(self.cfg)
        self.assertEqual((c["status"], c["fix_command"], c["ask_human"]), ("fail", None, True))
        for word in ("TypeSafe", "OpenRouter", "Vercel", "console.typesafe.ai/keys", "openrouter.ai/settings/keys"):
            self.assertIn(word, c["human_question"])
        self.assertIn("TypeSafe", c["human_question_zh"])
        self.assertEqual(c["key_commands"]["vercel"], "jevscreen keys set vercel --dialog")
        ks = self.by_id(doctor.key_checks(self.cfg))
        self.assertEqual({ks[f"key_{n}"]["status"] for n in jev.PROVIDER_ORDER}, {"skip"})

    def test_active_provider_is_reported(self):
        with mock.patch.dict(os.environ, {"AI_GATEWAY_API_KEY": FAKE_VC, "OPENROUTER_API_KEY": FAKE_OR}):
            c = doctor.jev_provider_check(self.cfg)
        self.assertEqual((c["status"], c["provider"], c["model"], c["pinned"]),
                         ("ok", "openrouter", "typesafe/jev-1.13", True))
        self.assertIn("jevscreen keys use vercel", c["detail"])
        with mock.patch.dict(os.environ, {"AI_GATEWAY_API_KEY": FAKE_VC}):
            c = doctor.jev_provider_check(self.cfg)
        self.assertEqual((c["status"], c["provider"], c["pinned"]), ("ok", "vercel", False))
        self.assertIn("no version", c["detail"])
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "typesafe", "AI_GATEWAY_API_KEY": FAKE_VC}):
            c = doctor.jev_provider_check(self.cfg)
        self.assertEqual((c["status"], c["fix_command"]), ("fail", "jevscreen keys set typesafe"))

    def test_check_jev_vercel_balance_and_typesafe_models(self):
        with mock.patch.dict(os.environ, {"AI_GATEWAY_API_KEY": FAKE_VC}):
            op = Opener([(200, b'{"balance": "0.40", "total_used": "4.60"}')])
            c = doctor.check_jev(self.cfg, op.open)
            self.assertEqual(op.requests[0]["url"], "https://ai-gateway.vercel.sh/v1/credits")
            self.assertEqual(op.requests[0]["method"], "GET")
            self.assertEqual((c["status"], c["balance_usd"], c["provider"]), ("warn", 0.4, "vercel"))
            op = Opener([(200, b'{"balance": "95.50", "total_used": "4.50"}')])
            self.assertEqual(doctor.check_jev(self.cfg, op.open)["status"], "ok")
            op = Opener([(402, b'{"error": {"type": "insufficient"}}')])
            c = doctor.check_jev(self.cfg, op.open)
            self.assertEqual((c["status"], c["http_status"]), ("fail", 402))
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": FAKE_TS}):
            op = Opener([(200, b'{"data": [{"id": "jev-1.13.0"}, {"id": "jev-latest"}]}')])
            c = doctor.check_jev(self.cfg, op.open)
            self.assertEqual(op.requests[0]["url"], "https://api.typesafe.ai/v1/models")
            self.assertEqual(op.requests[0]["auth"], "Bearer " + FAKE_TS)
            self.assertEqual((c["status"], c["model_listed"]), ("ok", True))
            op = Opener([(401, f"bad {FAKE_TS}".encode())])
            c = doctor.check_jev(self.cfg, op.open)
            self.assertEqual((c["status"], c["fix_command"]), ("fail", "jevscreen keys set typesafe"))
            self.assertNotIn(FAKE_TS, json.dumps(c))


class QuickstartKeyTests(Base):
    def test_no_key_one_account_question_with_three_choices(self):
        k = quickstart.key_state(self.cfg)
        self.assertEqual((k["configured"], k["reason"]), (False, "default"))
        item = quickstart.key_item(self.cfg, k, rejected=False)
        self.assertEqual((item["id"], item["human_action"], item["provider"]), ("key_jev", True, None))
        self.assertNotIn("ask_human", item)
        self.assertEqual([c["provider"] for c in item["choices"]], ["typesafe", "openrouter", "vercel"])
        self.assertEqual(item["choices"][2]["agent_try"], "jevscreen keys set vercel --dialog")
        for lang in ("zh", "en"):
            q = item[f"question_{lang}"]
            for url in ("https://console.typesafe.ai", "https://console.typesafe.ai/keys",
                        "https://openrouter.ai/settings/keys", "https://vercel.com/signup",
                        jev.PROVIDERS["vercel"].key_url):
                self.assertIn(url, q)
            self.assertTrue(item[f"text_{lang}"].startswith(q))
        self.assertIn("暂停", item["question_zh"])
        self.assertIn("paused", item["question_en"])
        self.assertEqual(item["question_en"].count("Which one do you have?"), 1)       # one question

    def test_known_provider_and_rejection_texts(self):
        with mock.patch.dict(os.environ, {"JEVSCREEN_JEV_PROVIDER": "typesafe"}):
            k = quickstart.key_state(self.cfg)
            item = quickstart.key_item(self.cfg, k, rejected=False)
        self.assertEqual((item["provider"], item["agent_try"]), ("typesafe", "jevscreen keys set typesafe --dialog"))
        self.assertIn("https://console.typesafe.ai/keys", item["text_en"])
        self.assertNotIn("choices", item)
        with mock.patch.dict(os.environ, {"AI_GATEWAY_API_KEY": FAKE_VC}):
            k = quickstart.key_state(self.cfg)
            self.assertEqual((k["configured"], k["provider"], k["fingerprint"]), (True, "vercel", "vercel:env"))
            item = quickstart.key_item(self.cfg, k, rejected=True, http_status=403)
        self.assertIn("credits", item["text_en"])
        self.assertIn("credits", item["text_zh"])
        failure_en = quickstart.STRINGS["en"]["no_credit"].format(label="Vercel AI Gateway", credits_url="u")
        self.assertIn("Vercel AI Gateway", failure_en)

    def test_fingerprint_changes_with_provider(self):
        keys.write_key(self.cfg, "openrouter", FAKE_OR)
        fp_or = quickstart.key_state(self.cfg)["fingerprint"]
        self.assertNotIn(":openrouter", fp_or)
        keys.write_key(self.cfg, "typesafe", FAKE_TS)
        fp_ts = quickstart.key_state(self.cfg)["fingerprint"]
        self.assertTrue(fp_ts.startswith("typesafe:"))
        self.assertNotEqual(fp_or, fp_ts)


class ConsentV4Tests(unittest.TestCase):
    def test_statement_v4_wording(self):
        self.assertEqual(consent.STATEMENT_VERSION, 4)
        st = consent.STATEMENTS[consent.GRAY_SOURCES]
        self.assertEqual(st["version"], 4)
        self.assertTrue(st["zh"].startswith("先说一声：股票清单和公司简介来自 TradingView 和 Yahoo，属于灰色用法"))
        self.assertTrue(st["zh"].endswith("可以吗？（可以 / 不要）"))
        self.assertTrue(st["en"].startswith("Quick heads-up: the stock list and company profiles come from TradingView "
                                            "and Yahoo. That's a gray area"))
        self.assertTrue(st["en"].endswith("OK? (yes / no)"))
        for word in ("SEC", "CNINFO", "BSE", "Jev", "rate-limited"):
            self.assertIn(word, st["en"])
        self.assertNotIn("OpenRouter", st["en"] + st["zh"])
        self.assertEqual(doctor.GRAY_QUESTION, st["en"])


class DocsTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]
    DOCS = ("README.md", "README.zh-CN.md", "AGENTS.md", "DATA_LICENSES.md", "docs/REFERENCE.md",
            "docs/AGENT_API.md")

    def test_docs_name_all_three_providers(self):
        for rel in self.DOCS:
            text = (self.ROOT / rel).read_text(encoding="utf-8")
            with self.subTest(doc=rel):
                for name in ("TypeSafe", "OpenRouter", "Vercel"):
                    self.assertIn(name, text)
                self.assertNotRegex(text, r"(?i)must (use|have) (an )?OpenRouter")
                self.assertNotIn("key_openrouter\", \"human_action\"", text)

    def test_agents_md_quotes_consent_v4(self):
        text = " ".join((self.ROOT / "AGENTS.md").read_text(encoding="utf-8").replace("> ", " ").split())
        self.assertIn(" ".join(consent.STATEMENTS[consent.GRAY_SOURCES]["en"].split()), text)


if __name__ == "__main__":
    unittest.main()

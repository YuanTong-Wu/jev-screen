"""Small paid Jev client (OpenRouter `typesafe/jev-1.13`) with packing, a request ledger and a hard budget.

Ported from the earlier research tools (outputs/jev-research/jev.py, packed_screen.py, batch_screen.py):

- Payload: several issuers per request under `state.items`, one choice question per item. Every question starts with
  an isolation prefix ("This question is ONLY about state.items[i]...") and its `state.issuer` / `state.text`
  references are rewritten to `state.items[i].*`, so other items are never evidence.
- Response validation is strict at the envelope (model name, answers object, usage counts) and per answer
  (type, labels subset of the criteria, finite probabilities in [0, 1] summing to 1 +- 0.01, choice = argmax).
  A bad answer fails only its own item; the rest of the packet stands.
- Ledger (`jev_requests`): one row per physical send. request_id = sha256(canonical payload)[:24] + '-' + a random
  suffix, so a resend of the same payload (after a charged failure, an 'uncertain' retried with retry_uncertain=True,
  ...) gets its own row and its own cost; payload_sha256 keeps the content link. Before a send the row is written as
  'sent' in a short store.session; afterwards the same row becomes 'ok' | 'failed' | 'uncertain'.
  Raw responses go to <home>/jev/<date>/<request_id>.json.
- Reuse cache (`jev_items`): one row per (item, send), keyed by item_key = sha256(model, question key/instructions/
  criteria, issuer, text) - not by packet - so a rerun reuses every answered item whatever the packing order or the
  neighbours in its packet (a universe or market-cap change no longer reshuffles every packet). An item whose latest
  send is 'ok' is served from the cache at zero cost (cached=True). An item whose latest send is 'uncertain' or
  still 'sent' (crash, locked final write) is never resent in the same run, and in a later run only with
  retry_uncertain=True; it comes back as 'uncertain' and is listed in `uncertain_not_resent`.
- Repeated reads: Question.read (default 0) salts item_key with "read" only when > 0, so read 0 keeps every existing
  key byte-identical (pinned by a test) and reads 1, 2, ... of the same item and question are separate cache entries,
  i.e. fresh draws that are then cached like any other answer. The payload never carries `read`; jev_items stores it
  as read_index. Jev's per-answer `confidence` is validated, returned with each result and stored (jev_items.confidence)
  but is not used in any decision.
- One process at a time: classify holds guard.budget_lock(cfg, 'openrouter-jev') from the cache lookup to the last
  ledger write, so two concurrent runs never both pay for the same items. A second process gets JevBusy at once.
- Budget: accounted spend = provider usage.cost when present, else a token estimate. Each in-flight request holds a
  reservation (calibrated estimate x max(reserve_factor, observed actual/estimate ratio x 1.1)); no packet is
  dispatched when spent + reservations + its own reservation would exceed budget_usd. Only one request is in flight
  until the first priced completion (probe), and again whenever a completion cost more than its reservation, so a
  price change overshoots the budget by at most one request. price_drift_ratio is set when actual cost runs above
  2x the estimate. Items never dispatched come back as 'skipped_budget' (classify does not raise BudgetExceeded for
  that; the class exists for callers that want to turn it into an exception).
- Errors: missing key -> JevUnavailable before any request or ledger write. HTTP 401/402/403 -> stop dispatching,
  let in-flight requests finish and be recorded, then raise JevUnavailable carrying the per-item results already
  completed (e.results). 429/500/503 -> at most 3 retries with backoff, then 'failed'. 502/504/520/524 (gateway or
  upstream timeout: the model call may have run and been billed) -> no retry, 'uncertain', reservation charged.
  Every attempt's HTTP status is kept in the error text. The key is read once (cfg.openrouter_key()) at the first
  real send and never appears in exceptions, ledger rows, logs or saved files (error bodies are redacted before they
  are cut, and a trailing partial copy of the key is dropped).
- Ctrl-C / SIGTERM / an unexpected error during dispatch: halt, drain in-flight requests (bounded), write their
  outcomes and all pending ledger rows, then re-raise.
- dry_run=True is pure: payloads and estimates only, no network, no database, no files; every item 'dry_run'.

Cost calibration (measured 2026-09-26 on 10,859 journaled requests of the old project; see
tests/fixtures/jev_calibration.json): provider input tokens ~= 235.4 per request + 31.4 per question
+ 0.1953 per ASCII char + 1.0888 per non-ASCII char of json.dumps(questions) + json.dumps(state)
(ensure_ascii=False). Mean abs error 0.16 %, max 1.1 %, over pack sizes 1/4/8, English and Chinese question banks.
Price: US$0.042 per million input tokens, output US$0 (usage.cost == input_tokens x 0.042e-6 on every request).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import json
import math
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from . import guard, store
from .config import Config, redact, secret_variants

MODEL = "typesafe/jev-1.13"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"

INPUT_USD_PER_MILLION = 0.042
OUTPUT_USD_PER_MILLION = 0.0
# Calibrated token model (see module docstring and tests/fixtures/jev_calibration.json).
TOKENS_PER_REQUEST = 235.4478
TOKENS_PER_QUESTION = 31.4306
TOKENS_PER_ASCII_CHAR = 0.1953
TOKENS_PER_NON_ASCII_CHAR = 1.0888
OUTPUT_TOKENS_PER_QUESTION = 62.0      # measured 55-62; priced at US$0
CALIBRATION_BASIS = ("calibrated token model: 235.4/request + 31.4/question + 0.1953/ASCII char + 1.0888/non-ASCII "
                     "char of the JSON questions+state (10,859 journaled requests, mean abs error 0.16%); "
                     "US$0.042 per million input tokens, output US$0")

MAX_TEXT_CHARS = 2_500                 # per item; longer texts are truncated with a note
MAX_PACKET_TEXT_BYTES = 24_000         # UTF-8 bytes of item text per request (the old tool's packet cap)
MAX_REQUEST_TOKENS = 30_000            # estimated input tokens per request (old reserved context was 32,000)
MAX_RESPONSE_BYTES = 1_000_000
REQUEST_TIMEOUT_S = 60.0
MAX_RETRIES = 3                        # 429 / 500 / 503 retries per request
RETRY_HTTP = frozenset({429, 500, 503})
# Gateway / upstream timeouts: the model call may have run (and been billed) upstream -> uncertain, never retried.
UNCERTAIN_HTTP = frozenset({502, 504, 520, 524})
PRICE_DRIFT_WARN = 2.0                 # actual/estimated cost above this -> price_drift_ratio is set
DRIFT_MARGIN = 1.1                     # reservation factor >= observed actual/estimate ratio x this
DRAIN_S = REQUEST_TIMEOUT_S + 5.0   # bounded wait for in-flight requests after an interrupt
LOCK_BUDGET = "openrouter-jev"         # guard.budget_lock name: one paying process at a time
ERROR_EXCERPT_CHARS = 300
MAX_RETRY_AFTER_S = 10.0
ERROR_STORM = 5                        # consecutive failed requests -> stop dispatching
KEY_PLACEHOLDER = "<openrouter-key>"
PROB_SUM_TOLERANCE = 0.01

STATUSES = ("ok", "failed", "uncertain", "skipped_budget", "dry_run")


class BudgetExceeded(RuntimeError):
    """The layer's budget cannot cover the work (classify reports this per item as 'skipped_budget')."""


class JevUnavailable(RuntimeError):
    """Jev cannot be used: no key, or the provider refused us (HTTP 401/402/403). Stop; do not retry.

    `results` (when not None) holds classify's per-item results at the moment it stopped: answers already paid for
    are 'ok', items never sent are 'failed' with error 'not sent: provider_unavailable'."""

    def __init__(self, message: str = "", *, results: list | None = None):
        super().__init__(message)
        self.results = results


class JevBusy(JevUnavailable):
    """Another process is using Jev (holds the openrouter-jev budget lock). Nothing was sent."""


class JevHTTPError(RuntimeError):
    """Transport-level HTTP error. `message` must be sanitized by whoever raises it (never the key)."""

    def __init__(self, status: int, message: str | None = None, retry_after: float | None = None):
        super().__init__(message or f"HTTP {status}")
        self.status = int(status)
        self.retry_after = retry_after


class JevUncertain(RuntimeError):
    """The request may have reached the provider but its outcome is unknown (timeout/reset after send)."""


class JevNotSent(RuntimeError):
    """The request certainly did not leave this machine (DNS failure, connection refused)."""


class InvalidResponse(ValueError):
    """Response failed validation. `detail` is a fixed code."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass
class Question:
    key: str
    instructions: str
    criteria: dict[str, str]   # label -> criterion text (choice question)
    read: int = 0              # repeat-read index: salts item_key when > 0, never sent (make_payload ignores it)


@dataclass
class Item:
    item_id: str
    issuer: str
    text: str
    meta: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------------------------
# Payloads, packing, estimates (pure)

_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def request_id_of(payload: Any) -> str:
    return payload_sha256(payload)[:24]


def question_read(question: Any) -> int:
    """The question's repeat-read index (0 for objects without a `read` field, e.g. an older question type)."""
    return getattr(question, "read", 0) or 0


def item_key(question: Question, issuer: str, text: str) -> str:
    """Reuse-cache key of one item's answer: model + question (key, instructions, criteria) + cleaned issuer/text.
    Independent of the packet (position, neighbours), so reordering the universe keeps every cache hit.
    "read" enters the hashed dict only when question.read > 0: read-0 keys are byte-identical to the keys written
    before repeated reads existed, so the whole cache stays valid."""
    fields = {"model": MODEL, "key": question.key, "instructions": question.instructions,
              "criteria": question.criteria, "issuer": issuer, "text": text}
    read = question_read(question)
    if read > 0:
        fields["read"] = read
    return hashlib.sha256(canonical_bytes(fields)).hexdigest()[:32]


def strip_partial_secret(text: str, secrets: list[str], min_len: int = 4) -> str:
    """Drop a trailing fragment that is a prefix (>= min_len chars) of a secret variant: what is left when a body
    was cut (read limit, truncation) in the middle of a key. Call it after redact()."""
    for secret in secrets:
        for v in secret_variants(secret):
            for n in range(min(len(v) - 1, len(text)), min_len - 1, -1):
                if text.endswith(v[:n]):
                    text = text[:-n]
                    break
    return text


def redact_cut(text: Any, secrets: list[str], limit: int, placeholder: str) -> str | None:
    """Redact every full occurrence first, drop a trailing partial copy, then cut to `limit` chars."""
    if text is None:
        return None
    out = str(redact(str(text), secrets, placeholder))
    out = strip_partial_secret(out, secrets)[:limit]
    return strip_partial_secret(out, secrets)


def question_id(index: int, key: str) -> str:
    return f"item_{index}__{key}"


def isolation_prefix(index: int) -> str:
    return (f"This question is ONLY about state.items[{index}].issuer. "
            f"Use ONLY state.items[{index}].text; other array items are other companies and are NOT evidence. ")


def item_instructions(question: Question, index: int) -> str:
    text = question.instructions
    for name in ("issuer", "text"):
        text = re.sub(rf"\bstate\.{name}\b", f"state.items[{index}].{name}", text)
    return isolation_prefix(index) + text


def validate_question(question: Question) -> None:
    if not isinstance(question.key, str) or not _KEY_RE.match(question.key):
        raise ValueError("question.key must be 1-64 characters of [A-Za-z0-9_]")
    if not isinstance(question.instructions, str) or not question.instructions.strip():
        raise ValueError("question.instructions must be a nonempty string")
    crit = question.criteria
    if not isinstance(crit, dict) or len(crit) < 2:
        raise ValueError("question.criteria must map at least two labels to criterion texts")
    for label, text in crit.items():
        if not isinstance(label, str) or not label.strip() or not isinstance(text, str) or not text.strip():
            raise ValueError("question.criteria labels and texts must be nonempty strings")
    read = getattr(question, "read", 0)
    if type(read) is not int or read < 0:
        raise ValueError("question.read must be an int >= 0")


def make_payload(entries: list[tuple[str, str]], question: Question) -> dict:
    """entries: [(issuer, text)] already cleaned/truncated. One question per item. question.read is never sent."""
    questions = {question_id(i, question.key): {"type": "choice", "instructions": item_instructions(question, i),
                                                "criteria": dict(question.criteria)}
                 for i in range(len(entries))}
    return {"model": MODEL, "state": {"items": [{"issuer": issuer, "text": text} for issuer, text in entries]},
            "questions": questions}


def _char_counts(s: str) -> tuple[int, int]:
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    return len(s) - non_ascii, non_ascii


def estimate_payload_tokens(payload: dict) -> int:
    """Calibrated estimate of provider input_tokens for one payload."""
    qa, qn = _char_counts(json.dumps(payload.get("questions", {}), ensure_ascii=False))
    sa, sn = _char_counts(json.dumps(payload.get("state", {}), ensure_ascii=False))
    tokens = (TOKENS_PER_REQUEST + TOKENS_PER_QUESTION * len(payload.get("questions", {}))
              + TOKENS_PER_ASCII_CHAR * (qa + sa) + TOKENS_PER_NON_ASCII_CHAR * (qn + sn))
    return int(math.ceil(tokens))


def tokens_cost_usd(input_tokens: float, output_tokens: float = 0.0) -> float:
    return (input_tokens * INPUT_USD_PER_MILLION + output_tokens * OUTPUT_USD_PER_MILLION) / 1_000_000


@dataclass
class Packet:
    positions: list[int]            # indexes into the caller's item list
    payload: dict
    request_id: str
    payload_sha256: str
    est_input_tokens: int
    est_cost_usd: float
    qids: list[str]


def _clean_item(item: Item, max_chars: int) -> tuple[str, str, str | None, str | None]:
    """-> (issuer, text, note, skip_reason)."""
    text = item.text if isinstance(item.text, str) else ""
    text = text.strip()
    issuer = item.issuer.strip() if isinstance(item.issuer, str) and item.issuer.strip() else str(item.item_id)
    if not text:
        return issuer, "", None, "empty_text"
    note = None
    if len(text) > max_chars:
        note = f"text truncated from {len(text)} to {max_chars} chars"
        text = text[:max_chars]
    return issuer, text, note, None


def build_packets(items: list[Item], question: Question, *, pack_size: int = 8, max_text_chars: int = MAX_TEXT_CHARS,
                  max_packet_text_bytes: int = MAX_PACKET_TEXT_BYTES,
                  max_request_tokens: int = MAX_REQUEST_TOKENS) -> tuple[list[Packet], dict[int, str], dict[int, str]]:
    """Pack items in order. Returns (packets, notes by position, skip reasons by position)."""
    validate_question(question)
    notes: dict[int, str] = {}
    skipped: dict[int, str] = {}
    packets: list[Packet] = []
    cur: list[tuple[int, str, str]] = []
    cur_bytes = 0

    def close() -> None:
        nonlocal cur, cur_bytes
        if cur:
            packets.append(_packet(cur, question))
        cur, cur_bytes = [], 0

    for pos, item in enumerate(items):
        issuer, text, note, skip = _clean_item(item, max_text_chars)
        if skip:
            skipped[pos] = skip
            continue
        if note:
            notes[pos] = note
        nbytes = len(text.encode("utf-8"))
        if cur and (len(cur) >= pack_size or cur_bytes + nbytes > max_packet_text_bytes):
            close()
        if cur:
            trial = make_payload([(i, t) for _, i, t in cur] + [(issuer, text)], question)
            if estimate_payload_tokens(trial) > max_request_tokens:
                close()
        cur.append((pos, issuer, text))
        cur_bytes += nbytes
    close()
    return packets, notes, skipped


def _packet(entries: list[tuple[int, str, str]], question: Question) -> Packet:
    payload = make_payload([(issuer, text) for _, issuer, text in entries], question)
    digest = payload_sha256(payload)
    tokens = estimate_payload_tokens(payload)
    return Packet(positions=[p for p, _, _ in entries], payload=payload, request_id=digest[:24],
                  payload_sha256=digest, est_input_tokens=tokens, est_cost_usd=tokens_cost_usd(tokens),
                  qids=[question_id(i, question.key) for i in range(len(entries))])


# ---------------------------------------------------------------------------------------------------------------
# Response validation

def strict_json(raw: bytes | str) -> Any:
    def reject_constant(_value):
        raise ValueError("non-finite JSON number")

    def unique_keys(pairs):
        out = {}
        for k, v in pairs:
            if k in out:
                raise ValueError("duplicate JSON key")
            out[k] = v
        return out

    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8")
    return json.loads(raw, parse_constant=reject_constant, object_pairs_hook=unique_keys)


def _is_prob(v: Any) -> bool:
    return type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1


def model_ok(returned: Any, requested: str = MODEL) -> bool:
    return isinstance(returned, str) and (returned == requested or returned.startswith(requested + "-"))


def validate_envelope(data: Any, qids: list[str]) -> tuple[dict, dict]:
    """Request-wide checks. Returns (answers, usage) or raises InvalidResponse. Missing answer keys are left to the
    per-item check (one missing answer must not void the packet); unexpected keys void it (misbinding)."""
    if not isinstance(data, dict):
        raise InvalidResponse("top_level_not_object")
    if not model_ok(data.get("model")):
        raise InvalidResponse("model_mismatch")
    answers, usage = data.get("answers"), data.get("usage")
    if not isinstance(answers, dict):
        raise InvalidResponse("answers_not_object")
    if set(answers) - set(qids):
        raise InvalidResponse("answer_keys_unexpected")
    if not isinstance(usage, dict):
        raise InvalidResponse("usage_not_object")
    for name in ("input_tokens", "output_tokens"):
        if type(usage.get(name)) is not int or usage[name] < 0:
            raise InvalidResponse("usage_token_count_invalid")
    return answers, usage


def validate_answer(answer: Any, labels: list[str] | set[str]) -> tuple[str, dict[str, float]]:
    """-> (label = argmax probability, probs). Raises InvalidResponse."""
    if answer is None:
        raise InvalidResponse("answer_missing")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise InvalidResponse("answer_type_invalid")
    probs = answer.get("probabilities")
    if not isinstance(probs, dict) or not probs:
        raise InvalidResponse("probabilities_not_object")
    if not set(probs) <= set(labels):
        raise InvalidResponse("probability_label_unknown")
    if not all(_is_prob(v) for v in probs.values()):
        raise InvalidResponse("probability_value_invalid")
    if abs(math.fsum(probs.values()) - 1) > PROB_SUM_TOLERANCE + 1e-12:
        raise InvalidResponse("probability_sum_invalid")
    top = max(probs.values())
    choice = answer.get("choice")
    if choice is not None:
        if not isinstance(choice, str) or choice not in probs:
            raise InvalidResponse("choice_invalid")
        if probs[choice] + 1e-8 < top:
            raise InvalidResponse("choice_not_argmax")
    if "confidence" in answer and answer["confidence"] is not None and not _is_prob(answer["confidence"]):
        raise InvalidResponse("confidence_invalid")
    if choice is not None and probs[choice] + 1e-8 >= top:
        label = choice
    else:
        label = next(lab for lab in labels if lab in probs and probs[lab] == top) if isinstance(labels, list) \
            else max(probs, key=lambda k: probs[k])
    return label, {k: float(v) for k, v in probs.items()}


def answer_confidence(answer: Any) -> float | None:
    """Jev's own `confidence` of one answer: a finite number in [0, 1], or None when absent/null (or the answer is not
    an object). Any other value raises InvalidResponse('confidence_invalid'), as validate_answer does. Stored for
    later validation only; no decision uses it."""
    if not isinstance(answer, dict):
        return None
    value = answer.get("confidence")
    if value is None:
        return None
    if not _is_prob(value):
        raise InvalidResponse("confidence_invalid")
    return float(value)


Parsed = tuple[str | None, dict, str | None, float | None]   # (label, probs, error, confidence) of one qid


def parse_response(data: Any, qids: list[str], labels: list[str]) -> tuple[dict, list[Parsed]]:
    """Validate one response. Returns (usage, [(label, probs, error, confidence)] per qid); a failed answer is
    (None, {}, 'invalid_answer:<detail>', None). Envelope errors raise."""
    answers, usage = validate_envelope(data, qids)
    out: list[Parsed] = []
    for qid in qids:
        try:
            label, probs = validate_answer(answers.get(qid), labels)
            out.append((label, probs, None, answer_confidence(answers.get(qid))))
        except InvalidResponse as e:
            out.append((None, {}, f"invalid_answer:{e.detail}", None))
    return usage, out


def accounted_cost(usage: dict | None, est_cost: float) -> tuple[float, str]:
    if isinstance(usage, dict):
        c = usage.get("cost")
        if type(c) in (int, float) and math.isfinite(c) and c >= 0:
            return float(c), "provider_reported"
        if type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int:
            return tokens_cost_usd(usage["input_tokens"], usage["output_tokens"]), "token_estimate"
    return est_cost, "reservation_estimate"


# ---------------------------------------------------------------------------------------------------------------
# Transport

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise JevHTTPError(code, f"HTTP {code} redirect rejected; authorization not forwarded")


class UrllibTransport:
    """POST the canonical payload to ENDPOINT. Returns raw response bytes. The key is read once, in prepare()."""

    def __init__(self, cfg: Config, *, endpoint: str = ENDPOINT, timeout_s: float = REQUEST_TIMEOUT_S, opener=None):
        self._cfg = cfg
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self._opener = opener or urllib.request.build_opener(_NoRedirect())
        self._key: str | None = None
        self._key_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"UrllibTransport(endpoint={self.endpoint!r})"

    def prepare(self) -> None:
        with self._key_lock:
            if self._key is None:
                key = self._cfg.openrouter_key()
                if not key:
                    raise JevUnavailable(self._cfg.openrouter_key_hint())   # names where it looked, never a value
                if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                    kind, where = self._cfg.openrouter_key_source()
                    raise JevUnavailable("OpenRouter key malformed (one line of printable ASCII expected); fix "
                                         + ("OPENROUTER_API_KEY" if kind == "env" else
                                            "the key file recorded with `jevscreen keys set openrouter --from-file` "
                                            "(it must hold only the key)" if kind == "recorded-file" else str(where)))
                self._key = key

    def secrets(self) -> list[str]:
        return [self._key] if self._key else []

    def _clean(self, text: str, limit: int = ERROR_EXCERPT_CHARS) -> str:
        return redact_cut(text, self.secrets(), limit, KEY_PLACEHOLDER) or ""

    def __call__(self, payload: dict) -> bytes:
        self.prepare()
        req = urllib.request.Request(self.endpoint, data=canonical_bytes(payload), method="POST", headers={
            "Authorization": "Bearer " + self._key, "Content-Type": "application/json",
            "Accept": "application/json"})
        try:
            resp = self._opener.open(req, timeout=self.timeout_s)
        except urllib.error.HTTPError as e:
            try:
                body = e.read(2049) if e.fp else b""
            except Exception:  # noqa: BLE001
                body = b""
            finally:
                try:
                    e.close()
                except Exception:  # noqa: BLE001
                    pass
            retry_after = None
            try:
                retry_after = float((e.headers or {}).get("Retry-After", ""))
            except (TypeError, ValueError):
                retry_after = None
            # Redact the whole body read (up to 2049 bytes) BEFORE cutting it: a cut through the key would leave a
            # partial copy that no longer matches. redact_cut also drops a key fragment at the read limit.
            excerpt = self._clean(body.decode("utf-8", "replace")).replace("\n", " ").strip()
            raise JevHTTPError(e.code, f"HTTP {e.code}: {excerpt}" if excerpt else f"HTTP {e.code}",
                               retry_after) from None
        except JevHTTPError:
            raise
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
                raise JevNotSent(f"connection not established ({type(reason).__name__})") from None
            raise JevUncertain(f"network error after connect ({type(reason).__name__})") from None
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            raise JevUncertain(f"network error ({type(e).__name__})") from None
        try:
            with resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(resp, "status", 200)
        except (TimeoutError, OSError, http.client.HTTPException) as e:
            raise JevUncertain(f"response read failed ({type(e).__name__})") from None
        if status != 200:
            raise JevHTTPError(status, f"HTTP {status}")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise InvalidResponse("response_too_large")
        return raw


# ---------------------------------------------------------------------------------------------------------------
# Rate limiter

class RateLimiter:
    """Thread-safe minimum spacing between request starts (1/rate s). wait() returns False if `halt` got set."""

    def __init__(self, rate_per_second: float):
        self.interval = 1.0 / float(rate_per_second)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self, halt: threading.Event | None = None) -> bool:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        while delay > 0:
            if halt is not None and halt.is_set():
                return False
            time.sleep(min(delay, 0.05))
            delay = slot - time.monotonic()
        return not (halt is not None and halt.is_set())


# ---------------------------------------------------------------------------------------------------------------
# Client

LEDGER_COLS = ("request_id", "run_id", "layer", "payload_sha256", "model", "items", "questions", "status",
               "http_status", "cost_usd", "cost_basis", "input_tokens", "output_tokens", "sent_at", "completed_at",
               "response_path", "error")
# read_index / confidence were added by store.init's ALTERs (NULL on rows written before them).
ITEM_COLS = ("item_key", "request_id", "run_id", "layer", "position", "status", "label", "probs_json", "error",
             "created_at", "read_index", "confidence")


@dataclass
class _Outcome:
    status: str                       # ok | failed | uncertain
    http_status: int | None = None
    cost: float = 0.0
    cost_basis: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    response_path: str | None = None
    error: str | None = None
    answers: list | None = None       # [(label, probs, error, confidence)] per qid when the envelope validated
    unavailable: bool = False
    sent: bool = True
    attempts: int = 0


@dataclass
class _Send:
    packet: Packet
    send_id: str                      # jev_requests.request_id of this physical send
    reservation: float
    sent_at: dt.datetime
    keys: list[str]                   # item_key per packet position
    read: int = 0                     # question.read of this send (jev_items.read_index)


def new_send_id(pk: Packet) -> str:
    return f"{pk.request_id}-{uuid.uuid4().hex[:8]}"


class JevClient:
    def __init__(self, cfg: Config, *, run_id: str, layer: str, budget_usd: float, pack_size: int = 8,
                 workers: int = 16, rate_per_second: float = 15.0, dry_run: bool = False, transport=None,
                 retry_uncertain: bool = False, max_text_chars: int = MAX_TEXT_CHARS,
                 max_packet_text_bytes: int = MAX_PACKET_TEXT_BYTES, max_retries: int = MAX_RETRIES,
                 backoff_s: float = 0.5, reserve_factor: float = 1.25, db_wait_s: float = 60.0,
                 drain_s: float = DRAIN_S, on_packet=None):
        if not isinstance(budget_usd, (int, float)) or not math.isfinite(budget_usd) or budget_usd < 0:
            raise ValueError("budget_usd must be a finite number >= 0")
        if not 1 <= int(pack_size) <= 32:
            raise ValueError("pack_size must be 1..32")
        if not 1 <= int(workers) <= 64:
            raise ValueError("workers must be 1..64")
        if not (isinstance(rate_per_second, (int, float)) and math.isfinite(rate_per_second)
                and 0 < rate_per_second <= 19):
            raise ValueError("rate_per_second must be in (0, 19]")
        if not 100 <= int(max_text_chars):
            raise ValueError("max_text_chars must be >= 100")
        self.cfg = cfg
        self.run_id = str(run_id)
        self.layer = str(layer)
        self.budget_usd = float(budget_usd)
        self.pack_size = int(pack_size)
        self.workers = int(workers)
        self.rate_per_second = float(rate_per_second)
        self.dry_run = bool(dry_run)
        self.retry_uncertain = bool(retry_uncertain)
        self.max_text_chars = int(max_text_chars)
        self.max_packet_text_bytes = int(max_packet_text_bytes)
        self.max_retries = max(0, int(max_retries))
        self.backoff_s = max(0.0, float(backoff_s))
        self.reserve_factor = max(1.0, float(reserve_factor))
        self.db_wait_s = float(db_wait_s)
        self.drain_s = max(0.0, float(drain_s))
        self._transport = transport
        self._limiter = RateLimiter(self.rate_per_second)
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._spent = 0.0
        self._reserved = 0.0
        self._requests_sent = 0
        self._attempts = 0
        self._cached_items = 0
        self._cached_request_ids: set[str] = set()
        self._obs_actual = 0.0            # priced completions: sum of actual cost ...
        self._obs_est = 0.0               # ... and of their calibrated estimates
        self._uncertain_keys: set[str] = set()
        self.uncertain_not_resent: list[dict] = []
        self.ledger_errors: list[str] = []
        self.stop_reason: str | None = None
        self.price_drift_ratio: float | None = None
        self._unavailable_msg: str | None = None
        # progress heartbeat: on_packet(done_items, total_items) after the cache lookup and after each completed
        # request of classify(); an exception in the callback is swallowed (it must never stop a paid run)
        self.on_packet = on_packet
        self._progress = [0, 0]

    def _tick(self, add: int = 0) -> None:
        cb = self.on_packet
        if cb is None:
            return
        self._progress[0] += add
        try:
            cb(self._progress[0], self._progress[1])
        except Exception:  # noqa: BLE001
            pass

    def __repr__(self) -> str:
        return (f"JevClient(run_id={self.run_id!r}, layer={self.layer!r}, budget_usd={self.budget_usd}, "
                f"spent_usd={self.spent_usd:.6f}, requests_sent={self.requests_sent})")

    # -- public counters
    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._spent

    @property
    def reserved_usd(self) -> float:
        with self._lock:
            return self._reserved

    @property
    def requests_sent(self) -> int:
        with self._lock:
            return self._requests_sent

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    @property
    def cached_requests(self) -> int:
        """Distinct earlier sends whose answers were reused by this client."""
        return len(self._cached_request_ids)

    @property
    def cached_items(self) -> int:
        return self._cached_items

    @property
    def observed_cost_ratio(self) -> float | None:
        with self._lock:
            return self._obs_actual / self._obs_est if self._obs_est > 0 else None

    # -- estimate
    def _packets(self, items: list[Item], question: Question):
        return build_packets(list(items), question, pack_size=self.pack_size, max_text_chars=self.max_text_chars,
                             max_packet_text_bytes=self.max_packet_text_bytes)

    def estimate(self, items: list[Item], question: Question) -> dict:
        """Estimate for sending every item (the reuse cache is not consulted: an upper bound)."""
        packets, notes, skipped = self._packets(items, question)
        tokens = sum(p.est_input_tokens for p in packets)
        nq = sum(len(p.qids) for p in packets)
        return {"requests": len(packets), "items": nq, "est_input_tokens": tokens,
                "est_cost_usd": round(tokens_cost_usd(tokens), 8), "basis": CALIBRATION_BASIS,
                "est_output_tokens": int(OUTPUT_TOKENS_PER_QUESTION * nq), "skipped_empty": len(skipped),
                "truncated": len(notes), "pack_size": self.pack_size,
                "est_reserved_usd": round(tokens_cost_usd(tokens) * self.reserve_factor, 8)}

    # -- classify
    def classify(self, items: list[Item], question: Question) -> list[dict]:
        items = list(items)
        validate_question(question)
        labels = list(question.criteria)
        if not self.dry_run and self._halt.is_set():
            raise JevUnavailable(self._unavailable_msg or "provider refused an earlier request of this client")
        self.stop_reason = None
        results: list[dict | None] = [None] * len(items)
        notes: dict[int, str] = {}
        keys: dict[int, str] = {}

        def put(pos: int, **kw) -> None:
            base = {"item_id": items[pos].item_id, "label": None, "probs": {}, "request_id": None,
                    "status": "failed", "error": None, "cached": False, "note": notes.get(pos), "confidence": None}
            base.update(kw)
            results[pos] = base

        for pos, item in enumerate(items):
            issuer, text, note, skip = _clean_item(item, self.max_text_chars)
            if skip:
                put(pos, status="failed", error=skip)
                continue
            if note:
                notes[pos] = note
            keys[pos] = item_key(question, issuer, text)
        if self.dry_run:
            packets, _, _ = self._packets(items, question)
            for pk in packets:
                for pos in pk.positions:
                    put(pos, status="dry_run", request_id=pk.request_id)
            return list(results)  # type: ignore[arg-type]
        if not keys:
            return list(results)  # type: ignore[arg-type]

        try:
            lock = guard.budget_lock(self.cfg, LOCK_BUDGET, reentrant=True)
            lock.__enter__()
        except guard.Busy:
            raise JevBusy("another process is using Jev (openrouter-jev lock held); nothing was sent") from None
        exc_info: tuple = (None, None, None)
        try:
            dup_of = self._classify_locked(items, question, labels, keys, put)
        except BaseException as e:
            exc_info = (type(e), e, e.__traceback__)
            raise
        finally:
            lock.__exit__(*exc_info)
        for pos, src in dup_of.items():
            put(pos, **{k: v for k, v in (results[src] or {}).items() if k not in ("item_id", "note")})
        if self.stop_reason == "provider_unavailable":
            raise JevUnavailable(self._unavailable_msg or "provider refused the request", results=list(results))
        return list(results)  # type: ignore[arg-type]

    def _classify_locked(self, items: list[Item], question: Question, labels: list[str], keys: dict[int, str],
                         put) -> dict[int, int]:
        """Cache lookup + dispatch under the Jev lock. Returns {duplicate position: first position}."""
        prior = self._item_lookup(set(keys.values()))
        first_of: dict[str, int] = {}
        dup_of: dict[int, int] = {}
        to_send: list[int] = []
        uncertain_runs: dict[str, int] = {}
        self._progress = [0, len(items)]
        for pos, k in keys.items():
            if k in first_of:                    # identical item twice in one call: send once
                dup_of[pos] = first_of[k]
                continue
            first_of[k] = pos
            row = prior.get(k)
            if row and row.get("ok"):
                ok = row["ok"]
                self._cached_items += 1
                self._cached_request_ids.add(ok["request_id"])
                put(pos, status="ok", label=ok["label"], probs=ok["probs"], request_id=ok["request_id"], cached=True,
                    confidence=ok["confidence"])
                continue
            latest = (row or {}).get("latest")
            pending = latest is not None and latest["status"] in ("uncertain", "sent")
            if pending or k in self._uncertain_keys:
                same_run = k in self._uncertain_keys or (latest is not None and latest["run_id"] == self.run_id)
                if same_run or not self.retry_uncertain:
                    prev_run = latest["run_id"] if latest else self.run_id
                    why = ("outcome unknown in this run; never resent in the same run" if same_run else
                           f"outcome unknown (run {prev_run}); pass retry_uncertain=True to resend")
                    uncertain_runs[prev_run] = uncertain_runs.get(prev_run, 0) + 1
                    put(pos, status="uncertain", request_id=latest["request_id"] if latest else None, error=why)
                    continue
            to_send.append(pos)
        for prev_run, n in uncertain_runs.items():
            self.uncertain_not_resent.append({"layer": self.layer, "items": n, "previous_run_id": prev_run})
        self._tick(len(items) - len(to_send))

        if to_send:
            packets, _, _ = self._packets([items[p] for p in to_send], question)
            for pk in packets:                   # positions -> the caller's list
                pk.positions = [to_send[i] for i in pk.positions]
            prepare = getattr(self._transport_fn(), "prepare", None)
            if callable(prepare):
                prepare()                        # JevUnavailable here: nothing sent, nothing written
            self._dispatch(deque(packets), put, labels, keys, question_read(question))

        return dup_of

    # -- internals
    def _transport_fn(self):
        if self._transport is None:
            self._transport = UrllibTransport(self.cfg)
        return self._transport

    def _secrets(self) -> list[str]:
        fn = getattr(self._transport, "secrets", None)
        try:
            return [s for s in (fn() if callable(fn) else []) if s]
        except Exception:  # noqa: BLE001
            return []

    def _clean(self, text: Any, limit: int = 500) -> str | None:
        return redact_cut(text, self._secrets(), limit, KEY_PLACEHOLDER)

    def _fill(self, put, pk: Packet, send_id: str, answers: list) -> None:
        for pos, (label, probs, err, conf) in zip(pk.positions, answers):
            if err is None:
                put(pos, status="ok", label=label, probs=probs, request_id=send_id, cached=False, confidence=conf)
            else:
                put(pos, status="failed", request_id=send_id, error=err, cached=False)

    def _item_lookup(self, item_keys: set[str]) -> dict[str, dict]:
        """item_key -> {'ok': latest ok answer or None, 'latest': latest send of any status}."""
        with store.session(self.cfg, wait_s=self.db_wait_s) as con:
            rows = con.execute(
                "SELECT item_key, request_id, run_id, status, label, probs_json, created_at, confidence FROM jev_items "
                "WHERE item_key IN (SELECT unnest(?::VARCHAR[])) ORDER BY created_at, request_id",
                [sorted(item_keys)]).fetchall()
        out: dict[str, dict] = {}
        for k, rid, run_id, status, label, probs_json, _created, conf in rows:
            d = out.setdefault(k, {"ok": None, "latest": None})
            d["latest"] = {"request_id": rid, "run_id": run_id, "status": status}
            if status == "ok" and label is not None:
                try:
                    probs = json.loads(probs_json) if probs_json else {}
                except ValueError:
                    continue
                d["ok"] = {"request_id": rid, "label": label, "probs": probs, "confidence": conf}
        return out

    def _ledger_write(self, req_rows: list[tuple], item_rows: list[tuple], wait_s: float | None = None) -> bool:
        if not req_rows and not item_rows:
            return True
        try:
            with store.session(self.cfg, wait_s=self.db_wait_s if wait_s is None else wait_s) as con:
                store.upsert_many(con, "jev_requests", LEDGER_COLS, req_rows)
                store.upsert_many(con, "jev_items", ITEM_COLS, item_rows)
            return True
        except store.StoreLocked as e:
            self.ledger_errors.append(self._clean(f"StoreLocked: {e}", 200) or "StoreLocked")
            return False

    def _sent_rows(self, s: _Send) -> tuple[tuple, list[tuple]]:
        pk = s.packet
        req = (s.send_id, self.run_id, self.layer, pk.payload_sha256, MODEL, len(pk.positions), len(pk.qids),
               "sent", None, None, None, None, None, s.sent_at, None, None, None)
        items = [(k, s.send_id, self.run_id, self.layer, i, "sent", None, None, None, s.sent_at, s.read, None)
                 for i, k in enumerate(s.keys)]
        return req, items

    def _done_rows(self, s: _Send, o: _Outcome) -> tuple[tuple, list[tuple]]:
        pk = s.packet
        now = store.now_utc()
        req = (s.send_id, self.run_id, self.layer, pk.payload_sha256, MODEL, len(pk.positions), len(pk.qids),
               o.status, o.http_status, o.cost, o.cost_basis or ("not_charged" if o.cost == 0 else None),
               o.input_tokens, o.output_tokens, s.sent_at, now, o.response_path, self._clean(o.error))
        items = []
        for i, k in enumerate(s.keys):
            if o.answers is not None:
                label, probs, err, conf = o.answers[i]
                if err is None:
                    items.append((k, s.send_id, self.run_id, self.layer, i, "ok", label,
                                  json.dumps(probs, sort_keys=True), None, now, s.read, conf))
                else:
                    items.append((k, s.send_id, self.run_id, self.layer, i, "failed", None, None, err[:300], now,
                                  s.read, None))
            else:
                items.append((k, s.send_id, self.run_id, self.layer, i, o.status, None, None,
                              self._clean(o.error, 300), now, s.read, None))
        return req, items

    def _reserve_factor_now(self) -> float:
        ratio = self.observed_cost_ratio
        return max(self.reserve_factor, (ratio or 0.0) * DRIFT_MARGIN)

    def _reservation(self, pk: Packet) -> float:
        return pk.est_cost_usd * self._reserve_factor_now()

    def _dispatch(self, queue: deque, put, labels: list[str], keys: dict[int, str], read: int = 0) -> None:
        in_flight: dict[Any, _Send] = {}
        pending_req: list[tuple] = []
        pending_items: list[tuple] = []
        state = {"failures": 0, "limit": 1}      # one request in flight until the first priced completion

        def complete(fut) -> None:
            s = in_flight.pop(fut)
            pk = s.packet
            try:
                o = fut.result()
            except Exception as e:  # noqa: BLE001 - never stringify: may carry request details
                o = _Outcome("uncertain", error=f"internal error ({type(e).__name__})",
                             cost=s.reservation, cost_basis="reservation_estimate")
            with self._lock:
                self._reserved -= s.reservation
                self._spent += o.cost
                if not o.sent:
                    self._requests_sent -= 1
                if o.cost_basis in ("provider_reported", "token_estimate") and pk.est_cost_usd > 0:
                    self._obs_actual += o.cost
                    self._obs_est += pk.est_cost_usd
                    ratio = self._obs_actual / self._obs_est
                    # A completion above its reservation: back to one request in flight until reservations (now
                    # scaled by the observed ratio) cover the actual cost again.
                    state["limit"] = 1 if o.cost > s.reservation + 1e-15 else self.workers
                    if ratio > PRICE_DRIFT_WARN:
                        self.price_drift_ratio = round(ratio, 3)
            if o.status == "uncertain":
                self._uncertain_keys.update(s.keys)
            if o.unavailable:
                self._halt.set()
                self.stop_reason = "provider_unavailable"
                self._unavailable_msg = self._clean(o.error, 200)
            if o.status == "failed" and o.sent:
                state["failures"] += 1
                if state["failures"] >= ERROR_STORM and self.stop_reason is None:
                    self.stop_reason = "error_storm"
            elif o.status == "ok":
                state["failures"] = 0
            req, its = self._done_rows(s, o)
            pending_req.append(req)
            pending_items.extend(its)
            if o.answers is not None:
                self._fill(put, pk, s.send_id, o.answers)
            else:
                for pos in pk.positions:
                    put(pos, status=o.status, request_id=s.send_id, error=self._clean(o.error, 300))

        def flush(wait_s: float) -> None:
            if self._ledger_write(pending_req, pending_items, wait_s=wait_s):
                pending_req.clear()
                pending_items.clear()

        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="jev")
        interrupted = False
        try:
            while queue or in_flight:
                wave: list[_Send] = []
                while queue and self.stop_reason is None and len(in_flight) + len(wave) < state["limit"]:
                    pk = queue[0]
                    res = self._reservation(pk)
                    with self._lock:
                        if self._spent + self._reserved + res > self.budget_usd + 1e-12:
                            if not in_flight and not wave:
                                self.stop_reason = "budget"
                            break
                        self._reserved += res
                    queue.popleft()
                    wave.append(_Send(pk, new_send_id(pk), res, store.now_utc(), [keys[p] for p in pk.positions],
                                      read))
                if wave:
                    rows = [self._sent_rows(s) for s in wave]
                    if not self._ledger_write([r for r, _ in rows], [i for _, its in rows for i in its]):
                        # Cannot record the send: do not send. Put the packets back and stop dispatching.
                        with self._lock:
                            self._reserved -= sum(s.reservation for s in wave)
                        for s in reversed(wave):
                            queue.appendleft(s.packet)
                        self.stop_reason = self.stop_reason or "store_locked"
                    else:
                        for s in wave:
                            with self._lock:
                                self._requests_sent += 1
                            in_flight[pool.submit(self._send, s, labels)] = s
                if not in_flight:
                    if queue and self.stop_reason is None:
                        self.stop_reason = "budget"
                    break
                done, _ = wait(list(in_flight), timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in done:
                    n_done = len(in_flight[fut].packet.positions)
                    complete(fut)
                    self._tick(n_done)
                if pending_req or pending_items:
                    flush(min(self.db_wait_s, 5.0))
        except BaseException:
            # Ctrl-C / SIGTERM / unexpected error: start nothing new, let the requests already out finish (bounded),
            # record their outcomes (they were paid for), then re-raise. Rows still 'sent' count as uncertain later.
            interrupted = True
            self._halt.set()
            self.stop_reason = self.stop_reason or "interrupted"
            self._unavailable_msg = self._unavailable_msg or "client interrupted"
            deadline = time.monotonic() + self.drain_s
            while in_flight and time.monotonic() < deadline:
                done, _ = wait(list(in_flight), timeout=min(1.0, max(0.0, deadline - time.monotonic())),
                               return_when=FIRST_COMPLETED)
                for fut in done:
                    complete(fut)
            raise
        finally:
            pool.shutdown(wait=not interrupted, cancel_futures=True)
            if (pending_req or pending_items) and not self._ledger_write(
                    pending_req, pending_items, wait_s=max(self.db_wait_s, 120.0)):
                self.ledger_errors.append(f"{len(pending_req)} ledger rows not written (store locked); "
                                          "responses kept on disk")
        why = {"budget": "skipped_budget", "provider_unavailable": "failed", "error_storm": "failed",
               "store_locked": "failed"}
        for pk in queue:
            status = why.get(self.stop_reason or "budget", "skipped_budget")
            err = None if status == "skipped_budget" else f"not sent: {self.stop_reason}"
            for pos in pk.positions:
                put(pos, status=status, request_id=None, error=err)

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = self.backoff_s * (2 ** (attempt - 1))
        if retry_after is not None and math.isfinite(retry_after) and 0 < retry_after <= MAX_RETRY_AFTER_S:
            delay = max(delay, retry_after)
        end = time.monotonic() + delay
        while time.monotonic() < end and not self._halt.is_set():
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def _send(self, s: _Send, labels: list[str]) -> _Outcome:
        pk = s.packet
        transport = self._transport_fn()
        attempt = 0
        statuses: list[str] = []                 # per-attempt HTTP status / error class, kept for reconciliation

        def trail() -> str:
            return f" (attempts: {', '.join(statuses)})" if len(statuses) > 1 else ""

        while True:
            if self._halt.is_set() or not self._limiter.wait(self._halt):
                if attempt == 0:
                    return _Outcome("failed", error="not sent: halted", sent=False)
                return _Outcome("failed", http_status=int(statuses[-1]) if statuses[-1].isdigit() else None,
                                error=f"retry stopped by halt{trail() or ' after ' + statuses[-1]}", attempts=attempt)
            attempt += 1
            with self._lock:
                self._attempts += 1
            try:
                raw = transport(pk.payload)
            except JevUnavailable as e:
                return _Outcome("failed", error=f"unavailable: {self._clean(e, 200)}", unavailable=True,
                                sent=attempt > 1, attempts=attempt)
            except JevHTTPError as e:
                statuses.append(str(e.status))
                if e.status in (401, 402, 403):
                    return _Outcome("failed", http_status=e.status, error=self._clean(f"{e}{trail()}", 300),
                                    unavailable=True, attempts=attempt)
                if e.status in UNCERTAIN_HTTP:
                    # The gateway gave up waiting; the model call may still have run and been billed upstream.
                    return _Outcome("uncertain", http_status=e.status, cost=s.reservation,
                                    cost_basis="reservation_estimate", attempts=attempt,
                                    error=self._clean(f"{e}; outcome unknown (gateway timeout){trail()}", 300))
                if e.status in RETRY_HTTP and attempt <= self.max_retries:
                    self._backoff(attempt, e.retry_after)
                    continue
                return _Outcome("failed", http_status=e.status, error=self._clean(f"{e}{trail()}", 300),
                                attempts=attempt)
            except JevNotSent as e:
                statuses.append("not_sent")
                if attempt <= self.max_retries:
                    self._backoff(attempt, None)
                    continue
                return _Outcome("failed", error=self._clean(f"{e}{trail()}", 300), attempts=attempt)
            except JevUncertain as e:
                statuses.append("uncertain")
                return _Outcome("uncertain", error=self._clean(f"{e}{trail()}", 300), cost=s.reservation,
                                cost_basis="reservation_estimate", attempts=attempt)
            except InvalidResponse as e:
                return _Outcome("failed", http_status=200, error=f"invalid_response:{e.detail}",
                                cost=s.reservation, cost_basis="reservation_estimate", attempts=attempt)
            except Exception as e:  # noqa: BLE001 - unknown completion; never stringify (may hold secrets)
                return _Outcome("uncertain", error=f"transport error ({type(e).__name__})",
                                cost=s.reservation, cost_basis="reservation_estimate", attempts=attempt)
            return self._handle_response(s, raw, labels, attempt)

    def _save_response(self, request_id: str, raw: bytes) -> str | None:
        folder = self.cfg.home / "jev" / store.now_utc().strftime("%Y-%m-%d")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{request_id}.json"
            tmp = folder / f".{request_id}.{threading.get_ident()}.tmp"
            tmp.write_bytes(raw)
            tmp.replace(path)
            return str(path)
        except OSError:
            return None

    def _handle_response(self, s: _Send, raw: Any, labels: list[str], attempt: int) -> _Outcome:
        pk = s.packet
        if isinstance(raw, (bytes, bytearray)):
            body = bytes(raw)
        elif isinstance(raw, str):
            body = raw.encode("utf-8")
        else:
            try:
                body = json.dumps(raw, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                body = b""
        secrets = self._secrets()
        if secrets:
            body = str(redact(body.decode("utf-8", "replace"), secrets, KEY_PLACEHOLDER)).encode("utf-8")
        path = self._save_response(s.send_id, body) if body else None
        est = s.reservation
        try:
            data = raw if isinstance(raw, dict) else strict_json(body)
        except (ValueError, UnicodeError, RecursionError):
            return _Outcome("failed", http_status=200, error="invalid_response:invalid_json", cost=est,
                            cost_basis="reservation_estimate", response_path=path, attempts=attempt)
        usage_raw = data.get("usage") if isinstance(data, dict) else None
        try:
            usage, answers = parse_response(data, pk.qids, labels)
        except InvalidResponse as e:
            cost, basis = accounted_cost(usage_raw if isinstance(usage_raw, dict) else None, est)
            return _Outcome("failed", http_status=200, error=f"invalid_response:{e.detail}", cost=cost,
                            cost_basis=basis, response_path=path, attempts=attempt,
                            input_tokens=_int_or_none(usage_raw, "input_tokens"),
                            output_tokens=_int_or_none(usage_raw, "output_tokens"))
        cost, basis = accounted_cost(usage, est)
        n_bad = sum(1 for a in answers if a[2] is not None)
        return _Outcome("ok", http_status=200, cost=cost, cost_basis=basis, input_tokens=usage["input_tokens"],
                        output_tokens=usage["output_tokens"], response_path=path, answers=answers,
                        error=f"{n_bad} invalid answers" if n_bad else None, attempts=attempt)


def _int_or_none(d: Any, key: str) -> int | None:
    if isinstance(d, dict) and type(d.get(key)) is int and d[key] >= 0:
        return d[key]
    return None

"""Polite HTTP client shared by all adapters.

- Per-host minimum interval (default 1 s) enforced across calls on the same client.
- Retries only on 5xx / timeouts (max 2, backoff), never on 4xx.
- HTTP 403 or 429, a `cf-mitigated: challenge` header, a Cloudflare challenge signature in an HTML body (first 64 KB),
  or a Cloudflare-served HTML 503 raises Blocked before any retry: callers must stop the whole run. Bare words such
  as 'captcha' are NOT signals (normal pages load reCAPTCHA for login dialogs or mention it in text).
- Bodies are capped (max_bytes, default 256 MB; error bodies 64 KB) and Response.truncated says when a cap was hit.
- Never follows redirects silently; a redirect is returned as-is so callers can decide, except that a 3xx whose
  Location points at a challenge path (a CHALLENGE_SIGNATURES entry or '/cdn-cgi/') raises Blocked
  ('challenge_redirect').
- Rate: min_interval_s is the minimum gap between request STARTS on one host (4.4 req/s -> 0.2273 s). The sustained
  rate never exceeds 1/min_interval_s. With max_per_window = N (optional), at most N starts fall in any rolling
  `window_s` (default 1 s) as well: the crawl client uses N = floor(4.4) = 4, so the client itself never puts 5 starts
  in one second and connection/handshake jitter has a margin before the server could see more than 4.4/s.
  The limiter is thread-safe: one lock per rate key is held across read-last-starts / sleep / record-start, so the
  caller holding it owns the next slot and concurrent callers queue behind it, however many threads share the client.
- Halt: Client.halt is a threading.Event. The Client sets it ITSELF at the moment it sees a block, before raising
  Blocked (no window in which another thread could still start a request). Once it is set, no NEW request (or retry)
  starts: the limiter raises Halted before touching the network, also for a caller that was already queued or
  sleeping in the limiter, and a retry backoff ends at once. Requests already on the wire finish normally. Halted is
  raised only when THIS call sent nothing; a retry cut short by the halt re-raises the previous attempt's error (or
  returns its 5xx response) instead, so the caller records a page that was really sent. Whoever runs the next job on
  the same client decides when to clear the flag.
- get_until() streams a GET in chunks (default 16 KB) and disconnects as soon as the caller's stop(buffer) returns
  True; max_bytes (default 256 KB) is only the hard cap. Same limiter, same Blocked rules: 403/429 raise before any
  body byte is read, challenge signatures are checked in the bytes read so far. Response.stopped_early /
  Response.truncated (cap reached without stop) tell the caller what happened.
- Deadline: every request ATTEMPT has a total wall-clock deadline (Client.deadline_s, default 120 s; per call
  `deadline_s=`), from the start of the attempt (open included) to the last body byte. timeout_s alone bounds each
  socket operation, so a server that trickles bytes or stalls between reads could hold a download for ever (seen live:
  an OpenDART document.xml held for 10+ minutes on an ESTABLISHED socket). Bodies are read in chunks (read1 on real
  sockets: one system call per chunk) with the elapsed time checked before each chunk, and the socket timeout of each
  read is lowered to the time left, so a stall ends at the deadline too. Exceeding it closes the connection and raises
  RequestTimeout (a TimeoutError, NOT Blocked: the provider did not refuse us). A body read whose socket timeout
  (timeout_s) fires while the deadline is further away is RequestTimeout kind='stall' and follows the same rule. A
  GET whose attempt received no body byte is retried once (counted in max_retries); after partial data, or for a
  POST, it is never retried and RequestTimeout surfaces to the caller (the adapters record crawl_state 'error' with
  note 'deadline;...' / 'stall;...'). HTTP 403/429 raise Blocked from the status line alone: the error body is never
  read, so a stalled 403/429 body can neither delay the halt nor be retried.
- Never bypasses bot protection.
"""
from __future__ import annotations

import contextlib
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

# Cloudflare-specific challenge signatures, matched case-insensitively in the first CHALLENGE_WINDOW bytes of HTML.
CHALLENGE_SIGNATURES = (
    "<title>just a moment...</title>", "<title>attention required! | cloudflare</title>",
    "/cdn-cgi/challenge-platform/", "window._cf_chl_opt", "cf-chl-",
)
CHALLENGE_WINDOW = 64 * 1024
# A refusal page sent without an HTTP status line (http.client raises BadStatusLine with the page text).
_RAW_REFUSAL = re.compile(r"<h1>\s*forbidden\s*</h1>|<title>\s*(?:403 )?forbidden\s*</title>|access denied|request rejected",
                          re.I)
DEFAULT_MAX_BYTES = 256 * 1024 * 1024
STREAM_MAX_BYTES = 256 * 1024     # get_until: hard cap per page
STREAM_CHUNK_BYTES = 16 * 1024    # get_until: bytes per read()
BACKOFF_SLICE_S = 0.25            # retry backoff sleeps in slices so a halt ends it at once
DEFAULT_DEADLINE_S = 120.0        # total wall-clock time per request attempt (open + body)
READ_CHUNK_BYTES = 64 * 1024      # request(): bytes per body read (the deadline is checked between reads)
MIN_SOCKET_TIMEOUT_S = 0.05       # floor of the per-read socket timeout lowered to the time left
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD"})


def _header(headers: dict[str, str], name: str) -> str:
    name = name.lower()
    return next((str(v) for k, v in headers.items() if k.lower() == name), "")


def challenge_reason(status: int, headers: dict[str, str], body: bytes) -> str | None:
    """Rule: only specific bot-challenge signals count; returns the reason or None.

    1. `cf-mitigated: challenge` header on any response. 1b. A 3xx whose Location is a challenge path. 2. For HTML (or untyped) bodies with status 200/503, a
    Cloudflare challenge signature in the first 64 KB. 3. A 503 HTML page served by Cloudflare (never retried).
    """
    if "challenge" in _header(headers, "cf-mitigated").lower():
        return "cf_mitigated_challenge"
    if 300 <= status < 400:
        location = _header(headers, "location").lower()
        if location and ("/cdn-cgi/" in location or any(sig in location for sig in CHALLENGE_SIGNATURES)):
            return "challenge_redirect"
    ctype = _header(headers, "content-type").lower()
    is_html = not ctype or "html" in ctype
    if status in (200, 503) and is_html:
        head = body[:CHALLENGE_WINDOW].decode("utf-8", "replace").lower()
        if any(sig in head for sig in CHALLENGE_SIGNATURES):
            return "challenge_marker"
    if status == 503 and ctype and "html" in ctype and "cloudflare" in _header(headers, "server").lower():
        return "cloudflare_503"
    return None


class Blocked(RuntimeError):
    """Provider refused or challenged us. Stop the run; do not retry, do not work around."""

    def __init__(self, url: str, status: int | None, reason: str):
        super().__init__(f"blocked: {reason} (status={status}) at {url}")
        self.url, self.status, self.reason = url, status, reason


class Halted(RuntimeError):
    """Client.halt was set: this call did NOT send anything (raised only before its first attempt). Not a block; the
    item is simply left for a later run."""

    def __init__(self, url: str | None = None):
        super().__init__(f"halted: request not started ({url})" if url else "halted: request not started")
        self.url = url


class RequestTimeout(TimeoutError):
    """An attempt timed out after it was sent. kind='deadline': the attempt's total wall-clock deadline passed (open +
    body). kind='stall': headers had arrived but one body read got no byte within the socket timeout (timeout_s)
    while the deadline was still further away. Not a block: the connection was closed and the caller records an
    error ('deadline' / 'stall'). `received` = body bytes read before it; `status` = the HTTP status when headers had
    arrived. Retried once only for GET/HEAD with no body byte received. `url` may carry an API key in its query:
    callers redact before recording it (str(self) never contains the URL)."""

    def __init__(self, url: str, deadline_s: float, elapsed_s: float, received: int, status: int | None = None,
                 kind: str = "deadline", timeout_s: float | None = None):
        if kind == "stall":
            msg = (f"stall: no body data within the {float(timeout_s or 0):g}s socket timeout after "
                   f"{elapsed_s:.1f}s ({received} body bytes received, status={status})")
        else:
            msg = (f"deadline: {deadline_s:g}s per attempt exceeded after {elapsed_s:.1f}s "
                   f"({received} body bytes received, status={status})")
        super().__init__(msg)
        self.url, self.deadline_s, self.elapsed_s = url, deadline_s, elapsed_s
        self.received, self.status, self.kind, self.timeout_s = received, status, kind, timeout_s


def _http_stream(obj: Any) -> http.client.HTTPResponse | None:
    """The real http.client.HTTPResponse behind a response or an HTTPError (None for test fakes)."""
    if isinstance(obj, http.client.HTTPResponse):
        return obj
    fp = getattr(obj, "fp", None) if isinstance(obj, urllib.error.HTTPError) else None
    return fp if isinstance(fp, http.client.HTTPResponse) else None


def _raw_socket(stream: http.client.HTTPResponse | None) -> Any:
    """The socket under an HTTPResponse (fp = BufferedReader over SocketIO), or None."""
    raw = getattr(getattr(stream, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if hasattr(sock, "settimeout") else None


def _abort(obj: Any) -> None:
    """Close a response (or HTTPError) at once: shut the socket down, then close; never raises."""
    sock = _raw_socket(_http_stream(obj))
    if sock is not None:
        with contextlib.suppress(Exception):
            sock.shutdown(2)          # socket.SHUT_RDWR
    with contextlib.suppress(Exception):
        obj.close()


class _Deadline:
    """Wall-clock budget of one request attempt (seconds None = no deadline)."""

    def __init__(self, url: str, seconds: float | None, timeout_s: float, t0: float):
        self.url, self.seconds, self.timeout_s, self.t0 = url, seconds, timeout_s, t0

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    def remaining(self) -> float | None:
        return None if self.seconds is None else self.seconds - self.elapsed()

    def expired(self) -> bool:
        rem = self.remaining()
        return rem is not None and rem <= 0

    def open_timeout(self) -> float:
        """Socket timeout for open(): timeout_s, lowered to the deadline."""
        rem = self.remaining()
        return self.timeout_s if rem is None else max(MIN_SOCKET_TIMEOUT_S, min(self.timeout_s, rem))

    def arm(self, stream: http.client.HTTPResponse | None) -> bool:
        """Lower the socket timeout of the next read to the time left; True when the deadline is the binding limit
        (a socket timeout on that read then means the deadline passed)."""
        rem = self.remaining()
        if rem is None or rem >= self.timeout_s:
            timeout, capped = self.timeout_s, False
        else:
            timeout, capped = max(MIN_SOCKET_TIMEOUT_S, rem), True
        sock = _raw_socket(stream)
        if sock is not None:
            with contextlib.suppress(Exception):
                sock.settimeout(timeout)
        return capped

    def error(self, received: int, status: int | None) -> RequestTimeout:
        return RequestTimeout(self.url, float(self.seconds or 0), self.elapsed(), received, status)

    def stall(self, received: int, status: int | None) -> RequestTimeout:
        """A body read's socket timeout fired while the deadline was still further away (or there is none)."""
        return RequestTimeout(self.url, float(self.seconds or 0), self.elapsed(), received, status,
                              kind="stall", timeout_s=self.timeout_s)


def _body_finished(stream: http.client.HTTPResponse | None) -> bool:
    """True when a real HTTPResponse has nothing left to read (known length consumed, or closed)."""
    return stream is not None and (stream.fp is None or stream.length == 0)


def _reader(obj: Any) -> tuple[Callable[[int], bytes], bool]:
    """(read function, returns-after-one-system-call). A real HTTPResponse (also behind an HTTPError) reads with
    read1; so does any object whose class defines read1. Other objects (test fakes) keep plain read(n)."""
    stream = _http_stream(obj)
    if stream is not None:
        return stream.read1, True
    if callable(getattr(type(obj), "read1", None)):
        return obj.read1, True
    return obj.read, False


def _read_some(obj: Any, n: int, dl: _Deadline, received: int, status: int | None) -> bytes:
    """One chunk of at most n bytes under the deadline: RequestTimeout (connection NOT yet closed) when the deadline
    has passed before the read, or when the read's socket timeout (lowered to the time left) fired. A socket timeout
    that fires while the deadline is still further away is RequestTimeout kind='stall', never a bare TimeoutError, so
    request() applies the same rule (no retry after partial data or for a POST). Real sockets use read1 (one system
    call), so a server trickling bytes cannot keep one read going past the deadline."""
    stream = _http_stream(obj)
    if dl.expired():
        if _body_finished(stream):
            return b""
        raise dl.error(received, status)
    capped = dl.arm(stream)
    read, _ = _reader(obj)
    try:
        return read(n)
    except TimeoutError as e:
        if capped or dl.expired():
            raise dl.error(received, status) from e
        raise dl.stall(received, status) from e


def _read_body(obj: Any, limit: int, dl: _Deadline, status: int | None, chunk: int = READ_CHUNK_BYTES) -> bytes:
    """Up to `limit` bytes of a body under the deadline (see _read_some): in chunks until EOF on a read1 reader;
    an object with plain read(n) semantics (a whole-body read, as before) is read once."""
    if not _reader(obj)[1]:
        return _read_some(obj, limit, dl, 0, status)
    buf = bytearray()
    while len(buf) < limit:
        data = _read_some(obj, min(chunk, limit - len(buf)), dl, len(buf), status)
        if not data:
            break
        buf += data
    return bytes(buf)


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict[str, str]
    elapsed_s: float
    truncated: bool = False   # the body hit the read cap; callers must not treat missing content as final
    stopped_early: bool = False   # get_until: the caller's stop() returned True and the connection was closed

    def json(self) -> Any:
        return json.loads(self.body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


@dataclass
class Client:
    user_agent: str = field(repr=False)   # may be the SEC fair-access UA (name + email): never repr it
    min_interval_s: float = 1.0
    timeout_s: float = 60.0
    max_retries: int = 2
    max_bytes: int | None = None   # default read cap per response (None = DEFAULT_MAX_BYTES)
    requests_made: int = 0
    _last: dict[str, float] = field(default_factory=dict)
    _opener: Any = field(default_factory=lambda: urllib.request.build_opener(_NoRedirect()))
    halt: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)
    _locks: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _locks_guard: Any = field(default_factory=threading.Lock, repr=False, compare=False)
    _count_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)
    max_per_window: int | None = None   # optional: at most this many starts per rolling window_s on one rate key
    window_s: float = 1.0
    deadline_s: float | None = DEFAULT_DEADLINE_S   # total wall-clock time per attempt (None = no deadline)
    _recent: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    thread_safe = True   # class attribute (not a field): callers may share one Client between threads

    def _blocked(self, url: str, status: int | None, reason: str) -> Blocked:
        """Set halt at the very moment the block is observed, then hand back the exception to raise."""
        self.halt.set()
        return Blocked(url, status, reason)

    def _backoff(self, seconds: float) -> None:
        """Retry backoff that ends as soon as halt is set (the next _wait then stops the retry)."""
        remaining = float(seconds)
        while remaining > 0 and not self.halt.is_set():
            step = min(BACKOFF_SLICE_S, remaining)
            time.sleep(step)
            remaining -= step

    def _key_lock(self, host: str) -> Any:
        with self._locks_guard:
            lock = self._locks.get(host)
            if lock is None:
                lock = self._locks[host] = threading.Lock()
            return lock

    def _wait(self, host: str, url: str | None = None) -> float:
        """Claim the next start slot for `host` (thread-safe), count the request and return the slot's monotonic
        time; raise Halted instead once self.halt is set. The per-key lock is held from reading the last starts
        through the sleep to recording this start, so starts on one key are never closer than min_interval_s and
        (with max_per_window) never more than max_per_window in any rolling window_s, whatever the number of
        threads."""
        with self._key_lock(host):
            if self.halt.is_set():
                raise Halted(url)
            last = self._last.get(host)
            now = time.monotonic()
            wait = 0.0
            if last is not None:
                wait = self.min_interval_s - (now - last)
            n = self.max_per_window
            recent = self._recent.get(host)
            if n and n > 0 and recent is not None and len(recent) >= n:
                wait = max(wait, self.window_s - (now - recent[-n]))
            if wait > 0:
                time.sleep(wait)
            if self.halt.is_set():          # set while we slept: this slot is abandoned, nothing is sent
                raise Halted(url)
            start = self._last[host] = time.monotonic()
            if n and n > 0:
                recent = self._recent.setdefault(host, [])
                recent.append(start)
                del recent[:-n]
            with self._count_lock:
                self.requests_made += 1
            return start

    def _deadline_for(self, deadline_s: float | None) -> float | None:
        """Per-call deadline, else the client's; None or <= 0 = no deadline."""
        d = deadline_s if deadline_s is not None else self.deadline_s
        return float(d) if d is not None and float(d) > 0 else None

    def _retry_deadline(self, method: str, e: RequestTimeout, attempt: int, retried: bool) -> bool:
        """A deadline is retried once, only for an idempotent method whose attempt received no body byte."""
        return method.upper() in IDEMPOTENT_METHODS and e.received == 0 and not retried and attempt < self.max_retries

    def request(self, method: str, url: str, *, json_body: Any = None, headers: dict[str, str] | None = None,
                check_challenge: bool = True, max_bytes: int | None = None, rate_key: str | None = None,
                deadline_s: float | None = None, data: bytes | None = None) -> Response:
        """One polite request; `max_bytes` overrides the client's read cap for this call only, `deadline_s` the
        client's total wall-clock deadline per attempt (RequestTimeout when exceeded; see the module doc).
        `data` sends a raw body (e.g. a urlencoded form; the caller sets its Content-Type), `json_body` a JSON one.

        `rate_key` lets several hosts share one limiter (SEC: www.sec.gov + data.sec.gov count as one budget).
        """
        cap = max_bytes or self.max_bytes or DEFAULT_MAX_BYTES
        limit = self._deadline_for(deadline_s)
        host = rate_key or urlparse(url).netloc
        if json_body is not None and data is not None:
            raise ValueError("pass json_body or data, not both")
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if json_body is not None:
            data = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        attempt = 0
        deadline_retried = False
        last: Response | BaseException | None = None    # previous attempt's outcome (it WAS sent)
        while True:
            halted_retry = self._wait_or_last(host, url, last)   # Halted only if nothing was sent yet
            if halted_retry is not None:
                return halted_retry
            t0 = time.monotonic()
            dl = _Deadline(url, limit, self.timeout_s, t0)
            opened = False
            try:
                try:
                    resp = self._opener.open(urllib.request.Request(url, data=data, headers=hdrs, method=method),
                                             timeout=dl.open_timeout())
                except http.client.HTTPException as e:
                    # A refusal page sent WITHOUT a status line (live 2026-09-27, doc.twse.com.tw: BadStatusLine
                    # carrying "<H1>Forbidden</H1> The requested URL is not allowed.") is a block like a 403.
                    if _RAW_REFUSAL.search(str(e)):
                        raise self._blocked(url, 403, "raw_forbidden") from e
                    raise
                except urllib.error.HTTPError as e:
                    opened = True
                    ecap = min(cap, CHALLENGE_WINDOW)
                    try:
                        if e.code in (403, 429):
                            raise self._blocked(url, e.code, f"http_{e.code}")   # halt set; body never read
                        body = _read_body(e, ecap + 1, dl, e.code) if e.fp else b""
                    except RequestTimeout:
                        _abort(e)
                        raise
                    finally:
                        with contextlib.suppress(Exception):
                            e.close()
                    out = Response(url, e.code, body[:ecap], dict(e.headers or {}), time.monotonic() - t0,
                                   truncated=len(body) > ecap)
                else:
                    opened = True
                    try:
                        if resp.status in (403, 429):
                            raise self._blocked(url, resp.status, f"http_{resp.status}")   # body never read
                        body = _read_body(resp, cap + 1, dl, resp.status)
                    except RequestTimeout:
                        _abort(resp)
                        raise
                    finally:
                        with contextlib.suppress(Exception):
                            resp.close()
                    out = Response(url, resp.status, body[:cap], dict(resp.headers or {}), time.monotonic() - t0,
                                   truncated=len(body) > cap)
            except RequestTimeout as e:
                if self._retry_deadline(method, e, attempt, deadline_retried):
                    attempt += 1
                    deadline_retried = True
                    last = e
                    self._backoff(2 ** attempt)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                if not opened and dl.expired():  # open() itself used up the deadline: nothing was received
                    te = dl.error(0, None)
                    if self._retry_deadline(method, te, attempt, deadline_retried):
                        attempt += 1
                        deadline_retried = True
                        last = te
                        self._backoff(2 ** attempt)
                        continue
                    raise te from e
                if attempt < self.max_retries:
                    attempt += 1
                    last = e
                    self._backoff(2 ** attempt)
                    continue
                raise
            if out.status in (403, 429):
                raise self._blocked(url, out.status, f"http_{out.status}")
            reason = challenge_reason(out.status, out.headers, out.body) if check_challenge else None
            if reason:
                raise self._blocked(url, out.status, reason)
            if 500 <= out.status < 600 and attempt < self.max_retries:
                attempt += 1
                last = out
                self._backoff(2 ** attempt)
                continue
            return out

    def _wait_or_last(self, host: str, url: str, last: Response | BaseException | None) -> Response | None:
        """_wait for the next attempt (returns None). If halt stops a RETRY (an earlier attempt of this call was
        sent), that attempt's outcome stands: its exception is re-raised, or its (5xx) response is returned for the
        caller to hand back. Halted is raised only when nothing was sent."""
        try:
            self._wait(host, url)
        except Halted:
            if last is None:
                raise
            if isinstance(last, BaseException):
                raise last
            return last
        return None

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def get_until(self, url: str, stop: Callable[[bytes], bool], *, max_bytes: int = STREAM_MAX_BYTES,
                  chunk_size: int = STREAM_CHUNK_BYTES, rate_key: str | None = None,
                  headers: dict[str, str] | None = None, deadline_s: float | None = None) -> Response:
        """Polite streaming GET: read `chunk_size` bytes at a time, call stop(bytes_so_far) after each chunk of a
        2xx body and close the connection as soon as it returns True (Response.stopped_early). `max_bytes` is a hard
        cap; reaching it without stop() sets Response.truncated when more bytes were waiting.

        Politeness and blocking are the same as request(): the per-host limiter is waited on for every attempt
        (thread-safe; Halted instead of a start once self.halt is set);
        HTTP 403/429 raise Blocked before any body byte is read; a `cf-mitigated: challenge` header, a Cloudflare
        challenge signature in the bytes read so far (first 64 KB) or a Cloudflare HTML 503 raise Blocked. 4xx are
        never retried; only non-challenge 5xx and connection errors/timeouts at open are retried (max_retries).
        The total wall-clock deadline per attempt (`deadline_s`, else Client.deadline_s) applies to the stream:
        RequestTimeout, retried once only when no body byte had arrived.
        """
        cap = max(1, int(max_bytes))
        limit = self._deadline_for(deadline_s)
        size = max(1, int(chunk_size))
        host = rate_key or urlparse(url).netloc
        hdrs = {"User-Agent": self.user_agent, "Accept": "*/*"}
        hdrs.update(headers or {})
        attempt = 0
        deadline_retried = False
        last: Response | BaseException | None = None    # previous attempt's outcome (it WAS sent)
        while True:
            halted_retry = self._wait_or_last(host, url, last)   # Halted only if nothing was sent yet
            if halted_retry is not None:
                return halted_retry
            t0 = time.monotonic()
            dl = _Deadline(url, limit, self.timeout_s, t0)
            try:
                try:
                    resp = self._opener.open(urllib.request.Request(url, headers=hdrs, method="GET"),
                                             timeout=dl.open_timeout())
                except urllib.error.HTTPError as e:
                    try:
                        if e.code in (403, 429):
                            raise self._blocked(url, e.code, f"http_{e.code}")   # halt set; body never read
                        ecap = min(cap, CHALLENGE_WINDOW)
                        body = _read_body(e, ecap + 1, dl, e.code) if e.fp else b""
                    except RequestTimeout:
                        _abort(e)
                        raise
                    finally:
                        with contextlib.suppress(Exception):
                            e.close()
                    out = Response(url, e.code, body[:ecap], dict(e.headers or {}), time.monotonic() - t0,
                                   truncated=len(body) > ecap)
                except (urllib.error.URLError, TimeoutError) as e:
                    if dl.expired():             # open() itself used up the deadline: nothing was received
                        raise dl.error(0, None) from e
                    if attempt < self.max_retries:
                        attempt += 1
                        last = e
                        self._backoff(2 ** attempt)
                        continue
                    raise
                else:
                    try:
                        out = self._stream(url, resp, stop, cap, size, dl)
                    except RequestTimeout:
                        _abort(resp)
                        raise
                    finally:
                        with contextlib.suppress(Exception):
                            resp.close()
            except RequestTimeout as e:
                if self._retry_deadline("GET", e, attempt, deadline_retried):
                    attempt += 1
                    deadline_retried = True
                    last = e
                    self._backoff(2 ** attempt)
                    continue
                raise
            if out.status in (403, 429):
                raise self._blocked(url, out.status, f"http_{out.status}")
            reason = challenge_reason(out.status, out.headers, out.body)
            if reason:
                raise self._blocked(url, out.status, reason)
            if 500 <= out.status < 600 and attempt < self.max_retries:
                attempt += 1
                last = out
                self._backoff(2 ** attempt)
                continue
            return out

    def _stream(self, url: str, resp: Any, stop: Callable[[bytes], bool], cap: int, size: int,
                dl: _Deadline) -> Response:
        status, headers = int(resp.status), dict(resp.headers or {})
        if status in (403, 429):
            raise self._blocked(url, status, f"http_{status}")        # before reading the body
        reason = challenge_reason(status, headers, b"")                # header-only signals
        if reason:
            raise self._blocked(url, status, reason)
        buf = bytearray()
        stopped = truncated = False
        while len(buf) < cap:
            chunk = _read_some(resp, min(size, cap - len(buf)), dl, len(buf), status)
            if not chunk:
                break
            before = len(buf)
            buf += chunk
            if before < CHALLENGE_WINDOW:                               # chunk touched the challenge window
                reason = challenge_reason(status, headers, bytes(buf[:CHALLENGE_WINDOW]))
                if reason:
                    raise self._blocked(url, status, reason)
            if 200 <= status < 300 and stop(bytes(buf)):
                stopped = True
                break
        else:
            try:                                                        # cap reached: was more waiting?
                dl.arm(_http_stream(resp))                              # a stalled probe ends by the deadline
                truncated = bool(resp.read(1))
            except (OSError, http.client.HTTPException):                # the bytes read so far are still usable
                truncated = True
        return Response(url, status, bytes(buf), headers, dl.elapsed(), truncated=truncated,
                        stopped_early=stopped)

    def post_json(self, url: str, body: Any, **kw) -> Response:
        return self.request("POST", url, json_body=body, **kw)

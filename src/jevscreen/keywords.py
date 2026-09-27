"""Idea -> English rendering + annual-report search phrases in en / zh / ja / ko, from a LOCAL model only.

Rules:
- Local model only: Qwen3.5-4B read from the Hugging Face cache. Offline is forced (HF_HUB_OFFLINE=1,
  TRANSFORMERS_OFFLINE=1, local_files_only=True, loaded from the snapshot directory), so nothing is ever downloaded
  and no paid call is made. JEVSCREEN_KEYWORDS_MODEL may name another local snapshot directory.
- The checkpoint is a vision-language one (Qwen3_5ForConditionalGeneration). Only the text decoder is needed:
  AutoModelForCausalLM maps model_type qwen3_5 to Qwen3_5ForCausalLM, which loads the model.language_model.* weights
  and ignores the vision tower and the MTP head. The prompt is a text-only chat with thinking disabled.
- torch MPS when available, else CPU; bfloat16. Greedy decoding, bounded max_new_tokens.
- The model is loaded once per process (module-level cache, thread-safe).
- The prompt asks per language for 'category' phrases (narrow, established product categories: what annual reports
  actually say) and 1-3 'specific' phrases with the idea's buzzword; they are merged, categories first, into one flat
  list per language (a small model otherwise prefixes every phrase with the buzzword, e.g. "AI agent IAM").
- Output is parsed robustly (first JSON object, types validated, phrases stripped / deduped / capped, each phrase must
  be written in its language's script). One retry with a stricter prompt; then KeywordsUnavailable.
- Results are cached per idea in <home>/keywords/<sha256(idea)[:16]>.json; refresh=True bypasses the cache.
  Invalid output (after the retry) is cached for FAILED_TTL_S in <key>.failed.json, so screening the same idea again
  does not spend two more generations before giving up; refresh=True or a new PROMPT_VERSION ignores it.
- The cached idea_en goes into the paid Jev question text. Bumping PROMPT_VERSION, switching the model (the cache is
  not keyed on it) or deleting <home>/keywords changes idea_en and so re-prices the L1/L2 answers of every
  translated idea: do it deliberately.
- torch / transformers are optional imports: this module imports without them, and generate() raises
  KeywordsUnavailable when the model cannot be loaded.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

MODEL_REPO = "Qwen/Qwen3.5-4B"
MODEL_ENV = "JEVSCREEN_KEYWORDS_MODEL"   # optional: a local snapshot directory to use instead
PROMPT_VERSION = 2
LANGS: tuple[str, ...] = ("en", "zh", "ja", "ko")
MIN_PER_LANG = 3          # fewer valid phrases than this in any language -> the output is rejected
MAX_PER_LANG = 15
MAX_PHRASE_CHARS = 60
MAX_IDEA_EN_CHARS = 300
MAX_IDEA_CHARS = 2000
MAX_NEW_TOKENS = 900
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
FAILED_TTL_S = 3600       # invalid model output is remembered this long (negative cache)

# generator(messages, max_new_tokens) -> raw model text
Generator = Callable[[list[dict[str, str]], int], str]


class KeywordsUnavailable(RuntimeError):
    """The local model could not be loaded, or did not produce valid keywords after one retry."""


# ---------------------------------------------------------------------------------------------------------------
# Prompt

SYSTEM_PROMPT = (
    "You turn an investment idea into search phrases for finding companies in annual reports. "
    "You answer with one JSON object only."
)

USER_TEMPLATE = """Investment idea: {idea}

Annual reports describe products with established category names, so first name the categories the idea belongs
to, then a few idea-specific phrases.

Return ONE JSON object with exactly this shape:
{{
  "idea_en": "<the idea rendered in English, one sentence>",
  "categories_en": ["<3 to 6 narrow established product categories, e.g. from analyst market taxonomies>"],
  "keywords": {{
    "en": {{"category": [...], "specific": [...]}},
    "zh": {{"category": [...], "specific": [...]}},
    "ja": {{"category": [...], "specific": [...]}},
    "ko": {{"category": [...], "specific": [...]}}
  }}
}}

Rules:
- "category": 8 to 12 short phrases (1 to 4 words) naming NARROW, established product, service or technology
  categories and their standard synonyms and sub-products, as a company already selling them would write in its
  annual report. Too broad is useless: terms most companies of a whole sector mention ("enterprise software",
  "cloud computing", "cybersecurity", "food products") are not allowed. Do NOT put the idea's newest buzzword in
  these phrases.
- "specific": 1 to 3 phrases that do contain the idea's newest buzzword.
- Languages: "en" as in US 10-K / 20-F business sections; "zh" as in Chinese A-share annual reports (Simplified
  Chinese); "ja" as in Japanese 有価証券報告書; "ko" as in Korean 사업보고서. Each list is written in its own language
  (acronyms may appear inside a phrase). Translate the concepts; do not transliterate English word by word.
- Style example ONLY, for a DIFFERENT idea ("AI for frozen food"); never copy these phrases:
  "en": {{"category": ["frozen food", "cold chain logistics"], "specific": ["AI demand forecasting"]}},
  "zh": {{"category": ["速冻食品", "冷链物流"], "specific": ["AI 需求预测"]}},
  "ja": {{"category": ["冷凍食品", "低温物流"], "specific": ["AI 需要予測"]}},
  "ko": {{"category": ["냉동식품", "콜드체인 물류"], "specific": ["AI 수요 예측"]}}
- No company names, no tickers, no duplicates, no explanations.
Output only the JSON object."""

STRICT_SUFFIX = """

IMPORTANT: your previous answer could not be used ({reason}).
Answer with the JSON object ONLY: no markdown fences, no comments, no text before or after it.
Use double quotes. Every language needs 7 to 12 "category" strings and 1 to 3 "specific" strings, written in
that language."""


EXAMPLE_PHRASES = frozenset(p.casefold() for p in (
    "frozen food", "cold chain logistics", "速冻食品", "冷链物流", "冷凍食品", "低温物流", "냉동식품", "콜드체인 물류",
    "AI demand forecasting", "AI 需求预测", "AI 需要予測", "AI 수요 예측"))


def drop_example_echoes(keywords: dict[str, list[str]], *texts: str) -> dict[str, list[str]]:
    """Remove prompt-example phrases the model echoed, unless the idea itself mentions them."""
    hay = " ".join(t for t in texts if t).casefold()
    return {lang: [p for p in v if p.casefold() not in EXAMPLE_PHRASES or p.casefold() in hay]
            for lang, v in keywords.items()}


def build_messages(idea: str, *, strict_reason: str | None = None) -> list[dict[str, str]]:
    user = USER_TEMPLATE.format(idea=idea)
    if strict_reason:
        user += STRICT_SUFFIX.format(reason=strict_reason)
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------------------------------------------
# Parsing and validation

_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_WS_RE = re.compile(r"\s+")
_HAN = r"㐀-䶿一-鿿豈-﫿"
_KANA = r"぀-ヿㇰ-ㇿｦ-ﾟ"
_HANGUL = r"가-힯ᄀ-ᇿ㄰-㆏"
_HAS_HAN = re.compile(f"[{_HAN}]")
_HAS_KANA = re.compile(f"[{_KANA}]")
_HAS_HANGUL = re.compile(f"[{_HANGUL}]")
_HAS_LATIN = re.compile(r"[A-Za-z]")
_STRIP_CHARS = " \t\r\n\"'`“”‘’「」『』《》【】[]()（）,，.。;；:：、・*-–—•"


def _script_ok(lang: str, s: str) -> bool:
    han, kana, hangul = bool(_HAS_HAN.search(s)), bool(_HAS_KANA.search(s)), bool(_HAS_HANGUL.search(s))
    if lang == "en":
        return bool(_HAS_LATIN.search(s)) and not (han or kana or hangul)
    if lang == "zh":
        return han and not (kana or hangul)
    if lang == "ja":
        return (han or kana) and not hangul
    if lang == "ko":
        return hangul and not kana
    return False


def clean_phrase(s: Any) -> str | None:
    if not isinstance(s, str):
        return None
    s = _WS_RE.sub(" ", s).strip().strip(_STRIP_CHARS).strip()
    if not s or len(s) > MAX_PHRASE_CHARS:
        return None
    return s


def _as_list(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        return re.split(r"[,，、;；\n]", v)
    return []


def clean_list(lang: str, items: Any) -> list[str]:
    if isinstance(items, dict):  # {"category": [...], "specific": [...]}: categories first
        items = [x for key in ("category", "categories", "specific") for x in _as_list(items.get(key))] \
            + [x for key, v in items.items() if key not in ("category", "categories", "specific")
               for x in _as_list(v)]
    if isinstance(items, str):   # "a, b, c" instead of a list
        items = re.split(r"[,，、;；\n]", items)
    if not isinstance(items, list):
        return []
    out, seen = [], set()
    for it in items:
        s = clean_phrase(it)
        if s is None or not _script_ok(lang, s):
            continue
        key = s.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= MAX_PER_LANG:
            break
    return out


def extract_json_object(text: str) -> dict | None:
    """The first JSON object in `text` (thinking blocks and markdown fences tolerated), else None."""
    if not isinstance(text, str):
        return None
    text = _THINK_RE.sub("", text)
    dec = json.JSONDecoder()
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            return obj
        i = text.find("{", i + 1)
    return None


def parse_output(text: str, idea: str = "") -> dict:
    """Validate raw model text. Returns {'idea_en', 'keywords', 'categories_en'}; raises ValueError with a reason.

    A language may be a flat list or {"category": [...], "specific": [...]} (merged, categories first)."""
    obj = extract_json_object(text)
    if obj is None:
        raise ValueError("no JSON object found")
    idea_en = obj.get("idea_en")
    if not isinstance(idea_en, str) or not _WS_RE.sub(" ", idea_en).strip():
        raise ValueError("idea_en missing or not a string")
    idea_en = _WS_RE.sub(" ", idea_en).strip()
    if len(idea_en) > MAX_IDEA_EN_CHARS:
        idea_en = idea_en[:MAX_IDEA_EN_CHARS].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    kw = obj.get("keywords")
    if not isinstance(kw, dict):   # tolerate the languages at the top level
        kw = {k: obj[k] for k in LANGS if k in obj}
    if not kw:
        raise ValueError("keywords missing or not an object")
    keywords = drop_example_echoes({lang: clean_list(lang, kw.get(lang)) for lang in LANGS}, idea, idea_en)
    short = [f"{lang}={len(v)}" for lang, v in keywords.items() if len(v) < MIN_PER_LANG]
    if short:
        raise ValueError(f"too few valid phrases ({', '.join(short)}; need >= {MIN_PER_LANG} each)")
    return {"idea_en": idea_en, "keywords": keywords, "categories_en": clean_list("en", obj.get("categories_en"))[:6]}


# ---------------------------------------------------------------------------------------------------------------
# Local model

_LOCK = threading.Lock()
_MODEL: dict[str, Any] | None = None   # {'tokenizer', 'model', 'device', 'dir', 'name', 'load_s'}


def force_offline() -> None:
    """Make every Hugging Face code path offline for this process (set before transformers is imported)."""
    os.environ.update(OFFLINE_ENV)
    try:  # huggingface_hub reads the flag at import time; patch it if it is already loaded
        import sys
        hub_constants = sys.modules.get("huggingface_hub.constants")
        if hub_constants is not None:
            hub_constants.HF_HUB_OFFLINE = True
    except Exception:
        pass


def _cache_roots() -> list[Path]:
    roots = []
    for env in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]).expanduser())
    if os.environ.get("HF_HOME"):
        roots.append(Path(os.environ["HF_HOME"]).expanduser() / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return roots


def _usable_snapshot(d: Path) -> bool:
    return (d / "config.json").is_file() and any(d.glob("*.safetensors"))


def resolve_model_dir() -> Path:
    """The local snapshot directory of the model; KeywordsUnavailable when it is not on disk."""
    override = os.environ.get(MODEL_ENV)
    if override:
        d = Path(override).expanduser()
        if d.is_dir() and _usable_snapshot(d):
            return d
        raise KeywordsUnavailable(f"{MODEL_ENV} does not name a local model directory with config.json and weights")
    repo_dir = "models--" + MODEL_REPO.replace("/", "--")
    for root in _cache_roots():
        base = root / repo_dir
        ref = base / "refs" / "main"
        if ref.is_file():
            d = base / "snapshots" / ref.read_text().strip()
            if _usable_snapshot(d):
                return d
        snaps = sorted((p for p in (base / "snapshots").glob("*") if _usable_snapshot(p)),
                       key=lambda p: p.stat().st_mtime, reverse=True) if (base / "snapshots").is_dir() else []
        if snaps:
            return snaps[0]
    raise KeywordsUnavailable(f"local model {MODEL_REPO} not found in the Hugging Face cache "
                              f"({', '.join(str(r) for r in _cache_roots())}); nothing is downloaded")


def _load_model() -> tuple[dict[str, Any], float]:
    """(model bundle, seconds spent loading in THIS call; 0.0 when it was already loaded)."""
    global _MODEL
    force_offline()
    with _LOCK:
        if _MODEL is not None:
            return _MODEL, 0.0
        t0 = time.monotonic()
        model_dir = resolve_model_dir()
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except Exception as e:  # ImportError, or a broken install
            raise KeywordsUnavailable(f"torch/transformers not available: {type(e).__name__}: {e}") from e
        try:
            device = "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
            tok = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
            model = AutoModelForCausalLM.from_pretrained(str(model_dir), local_files_only=True,
                                                         dtype=torch.bfloat16)
            model.to(device).eval()
        except Exception as e:
            raise KeywordsUnavailable(f"could not load local model {MODEL_REPO}: {type(e).__name__}: "
                                      f"{str(e)[:300]}") from e
        load_s = round(time.monotonic() - t0, 2)
        _MODEL = {"tokenizer": tok, "model": model, "device": device, "dir": str(model_dir),
                  "name": f"{MODEL_REPO}@{model_dir.name[:12]}", "load_s": load_s}
        return _MODEL, load_s


def _local_generator(bundle: dict[str, Any]) -> Generator:
    def run(messages: list[dict[str, str]], max_new_tokens: int) -> str:
        import torch
        tok, model = bundle["tokenizer"], bundle["model"]
        prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                         enable_thinking=False)
        inputs = tok(prompt, return_tensors="pt").to(bundle["device"])
        with _LOCK, torch.inference_mode():
            out = model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False,
                                 temperature=None, top_p=None, top_k=None,
                                 pad_token_id=tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id)
        return tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return run


# ---------------------------------------------------------------------------------------------------------------
# Cache and public entry point

def idea_key(idea: str) -> str:
    return hashlib.sha256(idea.strip().encode("utf-8")).hexdigest()[:16]


def cache_path(cfg, idea: str) -> Path:
    return Path(cfg.home) / "keywords" / f"{idea_key(idea)}.json"


def _read_cache(path: Path, idea: str) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("idea") != idea.strip() \
            or data.get("prompt_version") != PROMPT_VERSION:
        return None
    kw = data.get("keywords")
    if not isinstance(data.get("idea_en"), str) or not isinstance(kw, dict) \
            or not all(isinstance(kw.get(lang), list) and kw[lang] for lang in LANGS):
        return None
    return data


def failed_path(cfg, idea: str) -> Path:
    return Path(cfg.home) / "keywords" / f"{idea_key(idea)}.failed.json"


def _read_failed(path: Path, idea: str, now: float | None = None) -> dict | None:
    """A recent cached failure for this idea and prompt version, else None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("idea") != idea.strip() \
            or data.get("prompt_version") != PROMPT_VERSION:
        return None
    try:
        age = (now if now is not None else time.time()) - float(data.get("failed_at_epoch"))
    except (TypeError, ValueError):
        return None
    return data if 0 <= age < FAILED_TTL_S else None


def _write_cache(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def generate(cfg, idea: str, *, refresh: bool = False, generator: Generator | None = None,
             max_new_tokens: int = MAX_NEW_TOKENS) -> dict:
    """{'idea_en', 'keywords': {'en','zh','ja','ko'}, 'model', 'cached', 'load_s', 'gen_s', ...}.

    `generator` (messages, max_new_tokens) -> text replaces the local model (tests). Raises KeywordsUnavailable when
    the model cannot load or its output stays invalid after one stricter retry; ValueError for an empty idea."""
    if not isinstance(idea, str) or not idea.strip():
        raise ValueError("idea must be a non-empty string")
    idea = idea.strip()
    if len(idea) > MAX_IDEA_CHARS:
        raise ValueError(f"idea is longer than {MAX_IDEA_CHARS} characters")
    path, fpath = cache_path(cfg, idea), failed_path(cfg, idea)
    if not refresh:
        hit = _read_cache(path, idea)
        if hit is not None:
            return {**hit, "cached": True, "load_s": 0.0, "gen_s": 0.0, "cache_path": str(path)}
        failed = _read_failed(fpath, idea)
        if failed is not None:
            raise KeywordsUnavailable(f"local model output was invalid at {failed.get('failed_at')} (cached for "
                                      f"{FAILED_TTL_S // 60} min; `jevscreen keywords --refresh` retries now): "
                                      f"{str(failed.get('error'))[:300]}")

    load_s = 0.0
    if generator is None:
        bundle, load_s = _load_model()
        generator, model_name = _local_generator(bundle), bundle["name"]
    else:
        model_name = getattr(generator, "model_name", "custom-generator")

    reasons: list[str] = []
    parsed, raw, attempts = None, "", 0
    t0 = time.monotonic()
    for strict in (False, True):
        attempts += 1
        messages = build_messages(idea, strict_reason=reasons[-1] if strict else None)
        try:
            raw = generator(messages, max_new_tokens)
        except KeywordsUnavailable:
            raise
        except Exception as e:
            raise KeywordsUnavailable(f"local model generation failed: {type(e).__name__}: {str(e)[:300]}") from e
        try:
            parsed = parse_output(raw, idea)
            break
        except ValueError as e:
            reasons.append(str(e))
    gen_s = round(time.monotonic() - t0, 2)
    if parsed is None:
        snippet = _WS_RE.sub(" ", raw or "")[:200]
        msg = (f"local model output invalid after {attempts} attempts ({'; '.join(reasons)}); last output starts: "
               f"{snippet!r}")
        with contextlib.suppress(OSError):
            _write_cache(fpath, {"idea": idea, "prompt_version": PROMPT_VERSION, "model": model_name, "error": msg,
                                 "failed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                 "failed_at_epoch": time.time()})
        raise KeywordsUnavailable(msg)
    with contextlib.suppress(OSError):
        fpath.unlink(missing_ok=True)

    result = {"idea": idea, "idea_en": parsed["idea_en"], "keywords": parsed["keywords"],
              "categories_en": parsed["categories_en"], "model": model_name,
              "prompt_version": PROMPT_VERSION, "attempts": attempts,
              "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "load_s": load_s, "gen_s": gen_s}
    out = {**result, "cached": False, "cache_path": str(path)}
    try:
        _write_cache(path, result)
    except OSError as e:   # the keywords are still good; only the cache is lost
        out["cache_path"], out["cache_error"] = None, f"{type(e).__name__}: {e}"
    return out

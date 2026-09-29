"""The sand panning of the one page (jevscreen.page_sand): real numbers and the final list's short names as data
attributes in every state, one language per page, the CSP untouched, nothing fetched, and the scene's behaviour
across reloads (the opening plays on the first view only, each stage's shake plays once and shows its numbers beside
its rim for a moment, reduced motion draws once, a hidden tab and the text view stop it), the model-viewer controls
(wheel, drag, right-drag, pinch, double-click, keys) and the pure camera math, in node with a stand-in DOM.
Synthetic jobs only."""
from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)
from jevscreen import config, page, page_sand  # noqa: E402
import onepage_states as S  # noqa: E402
from test_one_page import ZH_OK_WORDS  # noqa: E402
from test_quickstart import CLEAR_ENV, no_network  # noqa: E402

CSP_0928 = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; "
            "base-uri 'none'; form-action 'none'")


def attrs_of(html: str) -> dict[str, str]:
    m = re.search(r'<div id="sand" class="sand" ([^>]*)>', html)
    if not m:
        return {}
    return {k: _html.unescape(v) for k, v in re.findall(r'data-([\w-]+)="([^"]*)"', m.group(1))}


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        env = mock.patch.dict(os.environ, {"JEVSCREEN_HOME": str(self.home), "LANG": "en_US.UTF-8"})
        env.start()
        self.addCleanup(env.stop)
        for k in CLEAR_ENV:
            os.environ.pop(k, None)
        self.cfg = config.Config(home=self.home).ensure()

    def tearDown(self):
        self._tmp.cleanup()

    def page(self, state: str, lang: str = "zh") -> str:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JEVSCREEN_HOME": tmp}):
            cfg = config.Config(home=Path(tmp)).ensure()
            return S.write_state_page(cfg, state, lang=lang).read_text(encoding="utf-8")


class TestFacts(unittest.TestCase):
    def test_stages_from_a_job(self):
        job = {"state": "running", "min_mcap_usd": 1e9, "steps": [], "progress": {"stage": "universe"}}
        f = page_sand.facts(job)
        self.assertEqual((f["stage"], f["active"], f["frac"], f["state"]), (0, 0, None, "run"))
        job["steps"] = [{"id": "universe", "status": "ok", "companies": 3120, "listed": 21480}]
        job["progress"] = {"stage": "descriptions", "stages": {"descriptions": {"done": 5e6, "total": 1e7}}}
        f = page_sand.facts(job)
        self.assertEqual((f["stage"], f["active"], f["frac"], f["listed"], f["pool"]), (1, 1, 0.5, 21480, 3120))
        job["steps"].append({"id": "descriptions", "status": "ok", "described": 2154})
        job["progress"] = {"stage": "l1", "stages": {"l1": {"done": 900, "total": 2154}}}
        f = page_sand.facts(job)
        self.assertEqual((f["stage"], f["active"], f["l1_done"], f["l1_total"]), (2, 2, 900, 2154))
        job["progress"] = {"stage": "l2", "stages": {"l1": {"done": 2154, "total": 2154},
                                                     "l2": {"done": 30, "total": 120}}}
        f = page_sand.facts(job)
        self.assertEqual((f["stage"], f["active"], f["frac"]), (3, 3, 0.25))
        job.update(state="done")
        job["steps"].append({"id": "screen", "status": "ok"})
        f = page_sand.facts(job, {"status": "ok", "funnel": {"universe": 3100, "described": 2150, "l1": 118,
                                                              "listed": 12}})
        self.assertEqual((f["stage"], f["active"], f["state"], f["final"], f["l1_pass"], f["pool"]),
                         (4, None, "done", 12, 118, 3100))

    def test_nothing_without_a_job_or_result(self):
        self.assertIsNone(page_sand.facts(None, None))
        self.assertEqual(page_sand.markup({"live": None}), "")
        self.assertEqual(page_sand.markup({"live": {"sand": None}}), "")

    def test_failed_and_waiting(self):
        self.assertEqual(page_sand.facts({"state": "failed", "steps": []})["state"], "fail")
        self.assertEqual(page_sand.facts({"state": "waiting", "steps": []})["state"], "wait")


class TestPageStates(Case):
    WANT = {  # state: (stage, active, state word, frac)
        "fresh": ("0", "", "wait", ""), "downloading": ("1", "1", "run", "0.414"),
        "waiting_key": ("2", "", "wait", ""), "key_rejected": ("2", "", "fail", ""), "blocked": ("0", "", "fail", ""),
        "screening": ("3", "3", "run", "0.375"), "done": ("4", "", "done", ""),
    }

    def test_every_state_carries_its_numbers(self):
        for state in S.STATES:
            for lang in ("zh", "en"):
                html = self.page(state, lang)
                a = attrs_of(html)
                self.assertTrue(a, (state, lang))
                self.assertIn('<canvas role="img" tabindex="0" aria-label="', html)
                self.assertEqual((a["stage"], a["active"], a["state"], a["frac"]), self.WANT[state], (state, lang))
                self.assertEqual(a["lang"], lang)
                blob = re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1)
                self.assertEqual(a["key"], json.loads(blob)["idea_key"])
                # each sieve's numbers (shown beside its rim for a moment when its stage completes): 'a,b'
                nums = (a["n0"], a["n1"], a["n2"])
                if state in ("downloading", "waiting_key", "screening"):
                    self.assertEqual(a["n0"], "3120,21480", state)
                if state == "downloading":
                    self.assertEqual(nums[1:], ("3120,", ""))        # the first read's queue; no report check yet
                if state == "waiting_key":
                    self.assertEqual(a["n1"], "2154,")               # profiles ready for the first read
                if state == "screening":
                    self.assertEqual(nums[1:], ("120,2154", "45,120"))
                if state in ("fresh", "blocked"):
                    self.assertEqual(nums, ("", "", ""))
                for gone in ("running", "r0-label", "r1-a", "ready-sum", "ready-show"):
                    self.assertNotIn(gone, a)                        # no attribute the scene does not read
                if state == "done":
                    self.assertEqual(a["dots"], "3")
                    self.assertEqual(nums, ("9,21480", "8,8", "3,8"))  # the result's funnel wins once there is one
                else:
                    self.assertEqual(a["dots"], "0")

    def test_counts_and_the_final_short_names(self):
        a = attrs_of(self.page("screening", "en"))
        self.assertEqual((a["cnt"], a["picks"]), ("21480,3120,120,", "[]"))    # the report check's intake
        self.assertEqual(attrs_of(self.page("downloading", "en"))["cnt"], "21480,3120,,")
        en = attrs_of(self.page("done", "en"))
        self.assertEqual(en["cnt"], "21480,9,8,3")
        # [short name, ticker, rank]: the rank finds the row whose evidence a click on the grain opens
        self.assertEqual(json.loads(en["picks"]), [["Qinglan Thermal Tech", "399101", 1],
                                                   ["Baifeng Aluminium", "699102", 2], ["Heyuan Controls", "399103", 3]])
        zh = json.loads(attrs_of(self.page("done", "zh"))["picks"])
        self.assertEqual([p[0] for p in zh[:2]], ["青澜科技", "百峰铝业"])       # the Chinese short names on a zh page
        self.assertEqual(zh[2], ["", "399103", 3])  # no Chinese short name: the ticker alone, never an English name

    def test_short_names(self):
        for name, want in (("Qinglan Thermal Tech Co. Ltd. Class A", "Qinglan Thermal Tech"), ("Apple Inc.", "Apple"),
                           ("Contemporary Amperex Technology Co., Limited", "Contemporary Amperex Technology"),
                           ("青澜科技", "青澜科技"), ("Co.", "Co.")):
            self.assertEqual(page_sand.short_name(name), want)

    def test_particles_are_capped(self):
        self.assertLessEqual(int(re.search(r"var NM=(\d+)", page_sand.JS).group(1)), 2500)

    def test_one_language_per_page(self):
        for state in S.STATES:
            for lang in ("zh", "en"):
                html = self.page(state, lang)
                a = attrs_of(html)
                label = _html.unescape(re.search(r'<canvas role="img" tabindex="0" aria-label="([^"]*)"', html).group(1))
                texts = [label] + [a[f"tip{k}"] for k in range(3)] + [a["tip-click"], a["tip-again"]]
                for t in texts:
                    if lang == "en":
                        self.assertNotRegex(t, r"[㐀-鿿]", (state, t))
                    else:
                        words = set(re.findall(r"[A-Za-z][A-Za-z.]*", t))
                        self.assertLessEqual(words, ZH_OK_WORDS, (state, t))
                        self.assertRegex(t, "[㐀-鿿]", (state, t))
        for lang, L in page_sand.LABELS.items():
            for t in L.values():
                self.assertEqual(bool(re.search(r"[㐀-鿿]", t)), lang == "zh", t)

    def test_csp_unchanged_and_nothing_fetched(self):
        self.assertEqual(page.CSP, CSP_0928)
        for state in ("downloading", "screening", "done"):
            html = self.page(state)
            self.assertIn(f'<meta http-equiv="Content-Security-Policy" content="{CSP_0928}">', html)
            # still ONE executable script (the page tests read it with one regex) and one data block
            self.assertEqual(len(re.findall(r"<script>", html)), 1)
            self.assertEqual(len(re.findall(r'<script type="application/json" id="data">', html)), 1)
        code = page_sand.JS + page_sand.CSS + page_sand.markup(
            {"lang": "zh", "live": {"sand": page_sand.facts({"state": "running", "steps": []})}})
        for bad in ("http:", "https:", "//cdn", "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "import(",
                    "sendBeacon", "url(", "@import", "<img", "src=", "localStorage", "eval(", "new Function"):
            self.assertNotIn(bad, code, bad)

    def test_no_socket_while_writing(self):
        def boom(*a, **kw):
            raise AssertionError("network used")
        with mock.patch.object(socket.socket, "connect", boom), no_network():
            for state in S.STATES:
                self.assertTrue(attrs_of(self.page(state)))

    def test_sand_sits_outside_the_app_and_is_hidden_without_js(self):
        html = self.page("screening")
        self.assertLess(html.index('<div id="sand"'), html.index('<div id="app">'))
        self.assertIn(".sand{display:none;", html)
        self.assertIn("html.hud-text .sand,html.hud-text .sand.on{display:none}", html)   # the text view hides the scene
        self.assertIn("'hud-on'", page_sand.JS)                    # ... which runs only under the HUD
        self.assertIn("background:#070707", html)             # dark behind the canvas: no white flash
        self.assertIn("prefers-reduced-motion", page_sand.JS)
        self.assertIn("239,159,39", page_sand.JS)             # amber (#EF9F27): the final list only


class TestTipsAndRefresh(Case):
    def test_each_sieve_tells_its_real_numbers(self):
        a = attrs_of(self.page("screening", "zh"))
        self.assertEqual([a["tip0"], a["tip1"], a["tip2"]],
                         ["市值 ≥ 10 亿美元：3,120 / 21,480 家", "AI 初读：通过 120 / 2,154 家", "年报核对：已核对 45 / 120 家"])
        a = attrs_of(self.page("done", "en"))
        self.assertEqual([a["tip0"], a["tip1"], a["tip2"]],
                         ["Market cap ≥ $1B: 9 of 21,480 companies", "First read: 8 of 8 passed",
                          "Report check: 3 of 8 made the list"])
        a = attrs_of(self.page("downloading", "zh"))
        self.assertEqual((a["tip1"], a["tip2"]), ("AI 初读：待读 3,120 家", "年报核对：还没开始"))
        self.assertEqual(attrs_of(self.page("fresh", "en"))["tip1"], "First read: not started yet")
        f = {"stage": 2, "listed": 49682, "pool": 10261, "l1_done": 2600, "l1_total": 10261, "floor": 1e9}
        self.assertEqual(page_sand.tips(f, "zh")[1], "AI 初读：已读 2,600 / 10,261 家")
        f.update(stage=4, l1_pass=296, final=10)
        self.assertEqual(page_sand.tips(f, "zh")[1:], ["AI 初读：通过 296 / 10,261 家", "年报核对：入选 10 / 296 家"])

    def test_the_refresh_waits_for_the_user_and_has_a_noscript_fallback(self):
        html = self.page("screening")
        head = html[:html.index("</head>")]
        self.assertIn('<meta name="jevscreen-refresh" content="3">', head)
        self.assertIn('<noscript><meta http-equiv="refresh" content="3"></noscript>', head)
        self.assertEqual(html.count('http-equiv="refresh"'), 1)           # only inside <noscript>
        self.assertIn(page.REFRESH_JS, html)
        done = self.page("done")
        self.assertNotIn("jevscreen-refresh", done.replace(page.REFRESH_JS, ""))
        self.assertNotIn('http-equiv="refresh"', done)


    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_reload_survives_a_page_script_that_throws(self):
        # the page's own script throws while it renders (a data shape written mid-run): the next reload still comes,
        # so the worker's next rewrite can repair the page (the old meta refresh reloaded whatever the script did)
        html = self.page("screening")
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        self.assertLess(js.index(page.REFRESH_JS), js.index(page.JS))
        drive = REFRESH + ("document.getElementById=()=>{throw new Error('mid-run');};console.error=()=>{};\n" + js +
                           "\nrun(3250);console.log(JSON.stringify({reloads}));")
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "t.js"
            f.write_text(drive, encoding="utf-8")
            r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout.strip().splitlines()[-1])["reloads"], 1)


class TestHero(unittest.TestCase):
    """The README hero GIFs: an 8 s loop whose seam is dark (the scene dips, the words stay), under 2.5 MB, and the
    final list keeps its one accent colour (amber) in the frames that show it."""

    def test_the_loop_and_the_final_list(self):
        try:
            from PIL import Image, ImageStat
        except ImportError:
            self.skipTest("Pillow not installed")
        root = Path(__file__).resolve().parents[1] / "docs" / "assets"
        for lang in ("en", "zh"):
            gif = root / f"hero-{lang}.gif"
            self.assertLessEqual(gif.stat().st_size, 2_560_000, lang)
            with Image.open(gif) as im:
                self.assertEqual(im.size, (800, 400))
                n = im.n_frames
                total = 0
                for k in range(n):
                    im.seek(k)
                    total += im.info.get("duration", 0)
                self.assertTrue(7000 <= total <= 9000, (lang, total))
                for k in (0, n - 1):                 # dark at both ends: the loop meets itself
                    im.seek(k)
                    self.assertLess(ImageStat.Stat(im.convert("L")).mean[0], 12, (lang, k))
                for k in (int(n * 0.8), int(n * 0.85), int(n * 0.9)):
                    im.seek(k)
                    px = im.convert("RGB").getdata()
                    amber = sum(1 for r, g, b in px if r > 110 and r - b > 60)
                    self.assertGreater(amber, 50, (lang, k))


# ------------------------------------------------------------------------------------------ behaviour (node)

HARNESS = r"""
const calls={raf:0,cancel:0,draw:0,fillText:[],strokes:0};let rafQ=[];
function ctx2d(){return new Proxy({},{get(t,k){if(k==='createRadialGradient')return ()=>({addColorStop(){}});
 if(k==='fillText')return (s)=>{calls.fillText.push(String(s));};if(k==='clearRect')return ()=>{calls.draw++;};
 if(k==='measureText')return (s)=>({width:String(s).length*6});
 if(k==='stroke')return ()=>{calls.strokes++;};if(k in t)return t[k];return ()=>{};},set(t,k,v){t[k]=v;return true;}});}
// a canvas event also reaches the window's listeners (it bubbles there in a browser)
const cvL={};const canvas={getContext:()=>CTX,width:1,height:1,style:{},addEventListener(k,f){cvL[k]=(e)=>{f(e);const g=listeners['w:'+k];if(g)g(e);};},
 setPointerCapture(){},releasePointerCapture(){},getBoundingClientRect(){return box.getBoundingClientRect();}};const CTX=ctx2d();
const bodyKids=[],scrolled=[],ROWS={};for(let r=1;r<=10;r++)ROWS['r'+r]={open:false,className:'row',scrollIntoView(){scrolled.push('r'+r);}};
const box={attrs:ATTRS,className:'sand',getAttribute(k){const v=this.attrs[k.replace(/^data-/,'')];return v===undefined?null:v;},
 querySelector(s){return s==='.sandlog'?LOGN:canvas;},getBoundingClientRect(){return {width:900,height:300,left:0,top:0};}};
// the reading log's column: its line elements (textContent only)
const LOGN={kids:[],style:{},appendChild(n){this.kids.push(n);return n;},removeChild(n){const i=this.kids.indexOf(n);if(i>=0)this.kids.splice(i,1);return n;}};
const store={};if(MEM)store['jevscreen-sand:'+ATTRS.key]=JSON.stringify(MEM);
let hidden=false;const listeners={};
global.document={getElementById:(id)=>id==='sand'?box:(ROWS[id]||null),body:{appendChild(n){bodyKids.push(n);return n;}},
 querySelector:()=>null,get hidden(){return hidden;},addEventListener:(k,f)=>{listeners[k]=f;}};
if(DECLS!==null)global.document.documentElement={className:DECLS};
let now=NOW;Date.now=()=>now;
global.performance={now:()=>now};
global.window={requestAnimationFrame:(f)=>{calls.raf++;rafQ.push(f);return rafQ.length;},cancelAnimationFrame:()=>{calls.cancel++;},
 devicePixelRatio:2,matchMedia:(q)=>({matches:REDUCED&&q.indexOf('reduced')>=0}),
 sessionStorage:{getItem:(k)=>store[k]===undefined?null:store[k],setItem:(k,v)=>{store[k]=v;}},
 addEventListener:(k,f)=>{listeners['w:'+k]=f;},performance:global.performance};
// the HUD's handle (page_hud.JS) when the test asks for one: it records what the scene tells it
const hudLog=[];if(HUD)global.window.__jevHud={inset(){return {r:HUD.r||0,b:0};},hold(){hudLog.push('hold');},
 finished(){hudLog.push('finished');},pick(r){hudLog.push('pick:'+r);return !!HUD.pick;},hover(r){hudLog.push('hover:'+r);}};
global.setInterval=()=>0;global.setTimeout=(f)=>{f();return 0;};global.clearTimeout=()=>{};
class N{constructor(tag,cls){this.tagName=tag;this.className=cls||'';this.kids=[];this.style={};this.textContent='';this.attrs={};}
 setAttribute(k,v){this.attrs[k]=v;}removeAttribute(k){delete this.attrs[k];}get offsetWidth(){return 120;}}
global.document.createElement=(t)=>new N(t);
global.window.innerWidth=WIDTH;global.window.innerHeight=800;
"""

DRIVE = r"""
function step(ms){now+=ms;const q=rafQ;rafQ=[];q.forEach(f=>f(now));}
const sync=calls.draw;const syncText=calls.fillText.slice();
const out={sync:sync,syncText:syncText,cls0:box.className};
step(16);const d1=calls.draw;
for(let i=0;i<STEPS;i++)step(16);
const t0=calls.fillText.length;for(let i=0;i<30;i++)step(16);out.tail=calls.fillText.slice(t0);
hidden=true;if(listeners.visibilitychange)listeners.visibilitychange();const rafBefore=calls.raf;step(16);step(16);
const mem=JSON.parse(store['jevscreen-sand:'+ATTRS.key]||'{}');
out.stoppedWhenHidden=calls.raf===rafBefore;out.draws=calls.draw;out.first=d1;out.mem=mem;out.cls=box.className;
out.texts=calls.fillText.slice();out.hud=hudLog;out.api=!!window.__jevSand;
console.log(JSON.stringify(out));
"""


def js_head(attrs: dict, mem: dict | None, reduced: bool, now: int, width: int, steps: int,
            hud: dict | None = None, de: str | None = None) -> str:
    return (f"const ATTRS={json.dumps(attrs)};const MEM={json.dumps(mem)};const REDUCED={json.dumps(reduced)};"
            f"const NOW={now};const WIDTH={width};const STEPS={steps};const HUD={json.dumps(hud)};"
            f"const DECLS={json.dumps(de)};\n")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestBehaviour(Case):
    def run_js(self, attrs: dict, mem: dict | None = None, reduced: bool = False, now: int = 1_000_000,
               width: int = 1280, steps: int = 150, hud: dict | None = None, de: str | None = None,
               drive: str = DRIVE) -> dict:
        js = js_head(attrs, mem, reduced, now, width, steps, hud, de) + HARNESS + page_sand.JS + drive
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "r.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def attrs(self, state: str, lang: str = "en") -> dict:
        return attrs_of(self.page(state, lang))

    def test_runs_and_remembers_the_phase_with_no_words(self):
        # a page seen long ago in this tab: no opening, no shake, ~30 fps, and no word or number drawn while calm
        out = self.run_js(self.attrs("screening"), mem={"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3})
        self.assertEqual(out["cls0"], "sand on")
        self.assertEqual(out["sync"], 1)                         # drawn at once, before any animation frame
        self.assertEqual(out["texts"], [])                       # no label column, no permanent numbers
        self.assertGreater(out["draws"], 60)                     # ~30 fps while calm (2.9 s driven at 60 Hz here)
        self.assertLess(out["draws"], 115)
        self.assertEqual(out["mem"]["stage"], 3)
        self.assertGreater(out["mem"]["t"], 52.0)                # the phase goes on across reloads
        self.assertNotIn("tr", out["mem"])
        self.assertTrue(out["stoppedWhenHidden"])
        self.assertTrue(out["api"])                              # the HUD's handle (window.__jevSand)

    def test_every_state_draws_the_scene(self):
        for state in S.STATES:
            out = self.run_js(self.attrs(state), mem={"t": 5.0, "at": 1_000_000 - 600_000, "stage": 0}, steps=10)
            self.assertEqual((out["cls0"], out["sync"]), ("sand on", 1), state)

    def test_the_first_view_opens_with_the_fly_in_once(self):
        a = self.attrs("screening")
        first = self.run_js(a)
        self.assertEqual(first["sync"], 1)                       # the first frame is drawn at once: the star sea
        self.assertEqual(first["texts"], [])
        self.assertNotIn("tr", first["mem"])                     # a first visit plays no shake
        calm = self.run_js(a, mem={"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3})
        self.assertGreater(first["draws"], calm["draws"] + 20)   # full frame rate during the opening only
        again = self.run_js(a, mem=first["mem"], now=1_000_000 + 3000)
        self.assertLess(again["draws"], 115)                     # the next reload does not fly in again

    def test_a_new_stage_shakes_once_and_its_numbers_come_and_go(self):
        a = self.attrs("screening")
        out = self.run_js(a, mem={"t": 50.0, "at": 1_000_000 - 3000, "stage": 2}, steps=200)
        self.assertEqual(out["mem"]["stage"], 3)
        self.assertNotIn("tr", out["mem"])                       # finished within the 3.2 s driven here
        # the first read's sieve completes: its real numbers roll up beside its rim ('120 of 2,154 passed') ...
        self.assertIn("120 / 2,154", out["texts"])
        self.assertTrue(any(re.fullmatch(r"\d{1,3} / 2,154", t) and t != "120 / 2,154" for t in out["texts"]))
        self.assertEqual(out["tail"], [])                        # ... and are gone ~2.6 s later
        calm = self.run_js(a, mem={"t": 50.0, "at": 1_000_000 - 3000, "stage": 3}, steps=200)
        self.assertEqual(calm["texts"], [])
        self.assertGreater(out["draws"], calm["draws"])          # full frame rate during the rush only
        # an old memory (the page opened again much later) is not a live transition: no rush, no numbers
        late = self.run_js(a, mem={"t": 50.0, "at": 1_000_000 - 600_000, "stage": 2})
        self.assertNotIn("tr", late["mem"])
        self.assertEqual(late["texts"], [])
        # mid-rush, a reload resumes it instead of starting again
        mid = self.run_js(a, mem={"t": 50.0, "at": 1_000_000 - 200, "stage": 3,
                                  "tr": {"from": 2, "to": 3, "at": 1_000_000 - 200}})
        self.assertEqual(mid["mem"]["stage"], 3)

    def test_a_live_finish_holds_the_panel_until_the_gold_links_up(self):
        a = self.attrs("done")
        out = self.run_js(a, mem={"t": 10.0, "at": 1_000_000 - 3000, "stage": 3}, steps=200, hud={"r": 300})
        self.assertEqual(out["hud"][:1], ["hold"])               # the panel waits ...
        self.assertIn("finished", out["hud"])                    # ... and opens once the gold has linked up
        self.assertIn("3 / 8", out["texts"])                     # the report check: 3 of the 8 it read
        later = self.run_js(a, mem={"t": 10.0, "at": 1_000_000 - 120_000, "stage": 4}, hud={"r": 300})
        self.assertEqual(later["hud"], [])                       # a finished page seen later: nothing held
        self.assertEqual(later["texts"], [])

    def test_reduced_motion_draws_once_without_numbers(self):
        out = self.run_js(self.attrs("screening"), reduced=True, mem={"t": 5.0, "at": 1_000_000 - 3000, "stage": 2})
        self.assertEqual((out["first"], out["draws"]), (1, 1))
        self.assertNotIn("tr", out["mem"])
        self.assertEqual(out["texts"], [])                       # no rush, no rolling numbers (the tooltips tell)

    def test_the_text_view_and_a_page_without_the_hud(self):
        a = self.attrs("screening")
        mem = {"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3}
        tv = self.run_js(a, mem=mem, de="hud-on hud-text", drive=DRIVE.replace(
            "const out={", "const paused={draws:calls.draw,raf:calls.raf};window.__jevSand.pause(false);const out={paused:paused,"))
        self.assertEqual(tv["paused"], {"draws": 0, "raf": 0})    # hidden behind the text view: nothing drawn
        self.assertGreater(tv["draws"], 50)                      # back to the scene: it runs
        off = self.run_js(a, mem=mem, de="")                     # no HUD (its script failed): no scene at all
        self.assertEqual((off["draws"], off["cls0"], off["api"]), (0, "sand", False))

    def test_a_dead_worker_stops_the_fill(self):
        a = self.attrs("screening")
        a.update({"stale-since": "2026-01-01T00:00:00Z", "stale-after": "90"})
        out = self.run_js(a, now=1_900_000_000_000)
        self.assertEqual(out["mem"]["a"], None)


PHYS = r"""
const S=SandCore.make(4),P=SandCore.P,h=1/120,out={};
function run(sec,f){for(let k=0;k<Math.round(sec/h);k++){SandCore.step(S,h);if(f)f(k);}}
// gravity settles: a grain dropped from above lands, bounces softly and sleeps at its place
S.hu[0]=0;S.hv[0]=0;S.ha[0]=1;S.y[0]=0.3;S.aw[0]=1;let maxUp=0,land=-1,prev=0.3;
run(3,(k)=>{if(S.y[0]<=0&&land<0)land=k;if(land>=0)maxUp=Math.max(maxUp,S.y[0]);});
out.settle={y:S.y[0],aw:S.aw[0],bounce:maxUp,land:land,vis:S.vis[0]};
// a force pushes: a grain pushed along +x moves along +x, then creeps home and sleeps
SandCore.zero(S,1);S.ha[1]=1;run(0.1,()=>SandCore.push(S,1,8,0,0,h));out.pushed=S.x[1];
// the furrow lingers: still out after 2 s (it rests 1.3 s before creeping), then heals slowly and sleeps
run(2);out.linger=S.x[1];run(16);out.home={x:S.x[1],aw:S.aw[1]};
// the shockwave is radial: impulses away from the source, stronger nearer, zero beyond the radius, lifted up
const rs=[];for(let a=0;a<8;a++){const T=SandCore.make(1);const dx=Math.cos(a*Math.PI/4),dz=Math.sin(a*Math.PI/4);
 SandCore.radial(T,0,dx,0,dz,0.5,3,0.9,0);rs.push([T.vx[0]*dx+T.vz[0]*dz,T.vx[0]*dz-T.vz[0]*dx,T.vy[0]]);}
out.radial=rs;const near=SandCore.make(1),far=SandCore.make(1),none=SandCore.make(1);
SandCore.radial(near,0,1,0,0,0.9,3,0,0);SandCore.radial(far,0,1,0,0,0.2,3,0,0);SandCore.radial(none,0,1,0,0,-0.1,3,0,0);
out.fall=[near.vx[0],far.vx[0],none.vx[0],none.aw[0]];
// damping bounded: a huge kick is capped at vmax and the speed decays to rest
SandCore.zero(S,2);S.ha[2]=50;SandCore.push(S,2,500,300,-400,0);let vmax=0,sp=[];
run(8,(k)=>{const v=Math.hypot(S.vx[2],S.vy[2],S.vz[2]);vmax=Math.max(vmax,v);if(k%60===0)sp.push(v);});
out.damp={vmax:vmax,cap:P.vmax,end:Math.hypot(S.vx[2],S.vy[2],S.vz[2]),aw:S.aw[2],y:S.y[2]};
// gold is heavier: the same push moves it much less and it only wobbles back to its place
const L=SandCore.make(2);L.m[1]=P.heavy;let lm=0,gm=0;
for(let k=0;k<12;k++){SandCore.push(L,0,10,0,0,h);SandCore.push(L,1,10,0,0,h);SandCore.step(L,h);}
for(let k=0;k<240;k++){lm=Math.max(lm,Math.abs(L.x[0]));gm=Math.max(gm,Math.abs(L.x[1]));SandCore.step(L,h);}
out.gold={light:lm,heavy:gm,end:L.x[1],aw:L.aw[1]};
// off the sieve's edge it falls through, fades out below and comes back from above
const E=SandCore.make(1);E.hu[0]=0.95;E.ha[0]=1;SandCore.push(E,0,1.5,0,0,0);let fell=0,minVis=1,back=0;
for(let k=0;k<480;k++){SandCore.step(E,h);if(E.fall[0])fell=1;minVis=Math.min(minVis,E.vis[0]);if(fell&&!E.fall[0]&&E.y[0]>0.1)back=1;}
for(let k=0;k<720;k++)SandCore.step(E,h);
out.edge={fell:fell,minVis:minVis,back:back,x:E.x[0],rest:E.aw[0]};
console.log(JSON.stringify(out));
"""

REFRESH = r"""
let now=1e6,ticks=[],reloads=0,saves=0;const L={};Date.now=()=>now;
global.window={__jevSave:()=>{saves++;},location:{reload:()=>{reloads++;}}};
global.document={querySelector:(s)=>s==='meta[name="jevscreen-refresh"]'?{getAttribute:()=>'3'}:null,addEventListener:(k,f)=>{L[k]=f;}};
global.setInterval=(f)=>{ticks.push(f);return 1;};
function run(ms){for(let t=0;t<ms;t+=250){now+=250;ticks.forEach(f=>f());}}
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestPhysicsCore(unittest.TestCase):
    def node(self, js: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "p.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_the_core_is_pure_and_in_the_page(self):
        self.assertIn(page_sand.CORE, page_sand.JS)
        for bad in ("document", "window", "Date", "performance", "Math.random"):
            self.assertNotIn(bad, page_sand.CORE, bad)

    def test_gravity_force_shockwave_damping_gold(self):
        o = self.node(page_sand.CORE + PHYS)
        s = o["settle"]
        self.assertGreaterEqual(s["land"], 0)
        self.assertEqual((s["y"], s["aw"], s["vis"]), (0, 0, 1))          # settled on the plane, asleep
        self.assertGreater(s["bounce"], 0.001)                             # a soft bounce ...
        self.assertLess(s["bounce"], 0.1)                                  # ... well below the drop (0.3)
        self.assertGreater(o["pushed"], 0.005)
        self.assertEqual(o["home"], {"x": 0, "aw": 0})                     # it crept home and sleeps
        self.assertGreater(o["linger"], 0.8 * o["pushed"])                 # ... not at once: a furrow lingers
        for along, side, up in o["radial"]:                                # every direction: away from the source
            self.assertGreater(along, 0.5)
            self.assertAlmostEqual(side, 0, places=5)
            self.assertGreater(up, 0)
        self.assertEqual(len({round(r[0], 5) for r in o["radial"]}), 1)   # the same strength all around
        near, far, none, aw = o["fall"]
        self.assertGreater(near, far)
        self.assertGreater(far, 0)
        self.assertEqual((none, aw), (0, 0))                               # beyond the radius: nothing
        d = o["damp"]
        self.assertLessEqual(d["vmax"], d["cap"] + 1e-4)
        self.assertEqual((d["end"], d["aw"], d["y"]), (0, 0, 0))
        g = o["gold"]
        self.assertLess(g["heavy"], g["light"] / 4)                        # heavier: it only wobbles
        self.assertEqual((g["end"], g["aw"]), (0, 0))
        e = o["edge"]
        self.assertEqual((e["fell"], e["back"], e["x"], e["rest"]), (1, 1, 0, 0))   # back, landed softly, resting
        self.assertLess(e["minVis"], 0.05)

    def test_the_refresh_timer_waits_while_someone_plays(self):
        js = REFRESH + page.REFRESH_JS + r"""
run(2750);const a=reloads;run(500);const b=reloads;         // idle: reloads at 3 s, once
reloads=0;now=1e6;ticks=[];window.__jevSandBusy=0;""" + page.REFRESH_JS + r"""
run(2000);window.__jevSandBusy=now;run(2000);const c=reloads;run(2250);const d=reloads;
reloads=0;now=1e6;ticks=[];window.__jevSandBusy=0;""" + page.REFRESH_JS + r"""
run(1000);L.pointerdown();run(3500);const e=reloads;run(750);const f=reloads;
console.log(JSON.stringify({a,b,c,d,e,f,saves}));"""
        o = self.node(js)
        self.assertEqual((o["a"], o["b"]), (0, 1))
        self.assertEqual((o["c"], o["d"]), (0, 1))         # the sand played at 2 s: no reload before 6 s
        self.assertEqual((o["e"], o["f"]), (0, 1))         # a pointer pressed at 1 s: no reload before 5 s
        self.assertEqual(o["saves"], 3)                    # the page's state is saved before each reload


INTERACT = r"""
function step(ms){now+=ms;const q=rafQ;rafQ=[];q.forEach(f=>f(now));}
for(let i=0;i<STEPS;i++)step(16);
function ev(k,x,y,o){const e=Object.assign({clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,preventDefault(){}},o||{});(cvL[k]||(()=>{}))(e);}
function tip(){const t=bodyKids.find(n=>n.className&&n.className.indexOf('sandtip')===0);return t&&t.className.indexOf(' on')>0?t.textContent:'';}
// a sieve tells its numbers after a short rest; an amber grain at once
const seen={};let goldAt=null;
for(let y=4;y<300;y+=10)for(let x=4;x<900;x+=10){ev('pointermove',x,y);let s=tip();if(!s){now+=400;step(16);ev('pointermove',x,y);step(16);s=tip();}
 if(s){seen[s]=(seen[s]||0)+1;if(!goldAt&&/#\d/.test(s))goldAt=[x,y,s];}}
// the scene drifts while the sweep goes on: find that grain again where it is now (a grain answers at once)
if(goldAt){const g0=goldAt;goldAt=null;for(let dy=-40;dy<=40&&!goldAt;dy+=4)for(let dx=-40;dx<=40;dx+=4){ev('pointermove',g0[0]+dx,g0[1]+dy);const s=tip();
 if(s&&/#\d/.test(s)){goldAt=[g0[0]+dx,g0[1]+dy,s];break;}}}
const out={seen:seen,busy:now-window.__jevSandBusy<100,goldAt:goldAt};
if(goldAt){ev('pointermove',goldAt[0],goldAt[1]);ev('pointerdown',goldAt[0],goldAt[1]);ev('pointerup',goldAt[0],goldAt[1]);}
out.rows=Object.keys(ROWS).filter(k=>ROWS[k].open);out.scrolled=scrolled.slice();
ev('pointerleave',0,0);out.afterLeave=tip();
// a drag orbits the stack; the angle is kept for the next page
now+=5000;ev('pointerdown',300,150);for(let i=1;i<=10;i++){now+=16;ev('pointermove',300+i*12,150);step(0);}ev('pointerup',420,150);
for(let i=0;i<120;i++)step(16);
if(listeners['w:beforeunload'])listeners['w:beforeunload']();
out.mem=JSON.parse(store['jevscreen-sand:'+ATTRS.key]||'{}');out.ms=box.__ms;out.hud=hudLog;
console.log(JSON.stringify(out));
"""


CALM = r"""
function step(ms){now+=ms;const q=rafQ;rafQ=[];q.forEach(f=>f(now));}
for(let i=0;i<STEPS;i++)step(16);
function ev(k,x,y,o){const e=Object.assign({clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,preventDefault(){}},o||{});(cvL[k]||(()=>{}))(e);}
function tip(){const t=bodyKids.find(n=>n.className&&n.className.indexOf('sandtip')===0);return t&&t.className.indexOf(' on')>0?t.textContent:'';}
function mem(){if(listeners['w:beforeunload'])listeners['w:beforeunload']();return JSON.parse(store['jevscreen-sand:'+ATTRS.key]||'{}');}
const out={};
// sweeping across the sieves shows no sieve tooltip (the sand is being played with) ...
let fast=0;for(let x=40;x<560;x+=12){now+=5;ev('pointermove',x,150);step(0);if(tip())fast++;}out.fast=fast;
// ... resting on one shows it after ~350 ms, not at once
out.rest=null;for(const [x,y] of [[300,150],[250,170],[330,120],[280,200],[200,160],[450,150],[520,120]]){ev('pointermove',x,y);now+=2;ev('pointermove',x,y);
 const early=tip();for(let i=0;i<30;i++)step(16);if(tip()){out.rest={early:early,after:tip()};break;}}
// a flick, then the pointer holds still: the sand settles and the frame rate drops back to ~30 fps
for(let i=0;i<10;i++){now+=8;ev('pointermove',120+i*30,190);step(0);}
for(let i=0;i<300;i++)step(16);const d0=calls.draw;for(let i=0;i<62;i++)step(16);out.stillFps=calls.draw-d0;
ev('pointerleave',0,0);
// a drag, held still, then released: the view does not glide on
now+=5000;ev('pointerdown',300,150);for(let i=1;i<=6;i++){now+=16;ev('pointermove',300+i*6,150);step(0);}
for(let i=0;i<15;i++)step(16);ev('pointerup',336,150);step(16);const m0=mem().oy;for(let i=0;i<120;i++)step(16);
out.held=[m0,mem().oy];
console.log(JSON.stringify(out));
"""


# the model-viewer controls, each from the resting view (a reset between them)
CONTROLS = r"""
function step(ms){now+=ms;const q=rafQ;rafQ=[];q.forEach(f=>f(now));}
function run(n){for(let i=0;i<n;i++)step(16);}
run(STEPS);
let prevented=0;
function ev(k,x,y,o){const e=Object.assign({clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,preventDefault(){prevented++;}},o||{});(cvL[k]||(()=>{}))(e);}
function tip(){const t=bodyKids.find(n=>n.className&&n.className.indexOf('sandtip')===0);return t&&t.className.indexOf(' on')>0?t.textContent:'';}
function mem(){if(listeners['w:beforeunload'])listeners['w:beforeunload']();return JSON.parse(store['jevscreen-sand:'+ATTRS.key]||'{}');}
function key(k){(cvL.keydown||(()=>{}))({key:k,preventDefault(){prevented++;}});}
function reset(){key('0');run(90);now+=5000;}
const out={};
// the wheel zooms (the page never scrolls here) and pokes the refresh
let p0=prevented;ev('wheel',450,150,{deltaY:-400});run(60);out.wheelIn={d:mem().d,prevented:prevented-p0,busy:window.__jevSandBusy===now-60*16};
for(let i=0;i<30;i++)ev('wheel',450,150,{deltaY:240});run(90);out.wheelOut=mem().d;reset();
out.afterReset=mem();
// a right-drag (or a shift-drag) pans: the look-at point moves, the angle stays
ev('pointerdown',300,150,{button:2});for(let i=1;i<=10;i++){now+=16;ev('pointermove',300+i*10,150,{button:2});step(0);}ev('pointerup',400,150,{button:2});run(60);
out.pan=mem();reset();
ev('pointerdown',300,150,{shiftKey:true});for(let i=1;i<=10;i++){now+=16;ev('pointermove',300,150+i*8);step(0);}ev('pointerup',300,230);run(60);
out.shiftPan=mem();reset();
// two fingers spreading zoom in
ev('pointerdown',400,150,{pointerId:7,pointerType:'touch'});ev('pointerdown',500,150,{pointerId:8,pointerType:'touch'});
for(let i=1;i<=10;i++){now+=16;ev('pointermove',400-i*6,150,{pointerId:7,pointerType:'touch'});ev('pointermove',500+i*6,150,{pointerId:8,pointerType:'touch'});step(0);}
ev('pointerup',340,150,{pointerId:7,pointerType:'touch'});ev('pointerup',560,150,{pointerId:8,pointerType:'touch'});run(60);out.pinch=mem().d;reset();
// a double-click on a sieve flies to it; on empty space it resets the view
let sieve=null;for(let y=40;y<300&&!sieve;y+=12)for(let x=10;x<900;x+=12){ev('pointermove',x,y,{});now+=400;step(16);ev('pointermove',x,y);step(16);step(16);
 const s=tip();if(s===ATTRS.tip1){sieve=[x,y];break;}}
ev('pointerleave',0,0);
if(sieve){ev('dblclick',sieve[0],sieve[1]);run(120);out.fly=mem();}
ev('dblclick',3,3);run(120);out.dblReset=mem();
// the keys: up/down step through the sieves (their tooltip shows), Enter flies there, + zooms, 0 resets
key('ArrowDown');out.k1=tip();key('ArrowDown');out.k2=tip();key('Enter');run(120);out.kfly=mem();
key('0');run(120);out.kreset=mem();key('+');run(60);out.kzoom=mem().d;key('Escape');run(60);
key('ArrowRight');run(60);out.korbit=mem().oy;
p0=prevented;(cvL.contextmenu||(()=>{}))({preventDefault(){prevented++;}});out.menu=prevented-p0;
out.hud=hudLog;out.ms=box.__ms;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestInteraction(Case):
    def play(self, attrs: dict, mem: dict | None = None, reduced: bool = False, steps: int = 20,
             drive: str = INTERACT, hud: dict | None = None) -> dict:
        js = js_head(attrs, mem, reduced, 1_000_000, 1280, steps, hud) + HARNESS + page_sand.JS + drive
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "i.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def attrs(self, state: str, lang: str = "en") -> dict:
        return attrs_of(self.page(state, lang))

    def test_hover_tells_each_sieve_and_a_drag_is_kept(self):
        a = self.attrs("screening", "zh")
        out = self.play(a, mem={"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3})
        self.assertLessEqual({a["tip0"], a["tip1"], a["tip2"]}, set(out["seen"]))
        self.assertTrue(out["busy"])                       # the page's refresh waits while the scene is used
        self.assertEqual(out["afterLeave"], "")
        self.assertGreater(out["mem"]["oy"], 0.2)          # dragged to the right: the view angle is kept
        self.assertLessEqual(out["mem"]["oy"], 1.1)
        again = self.play(a, mem=out["mem"])
        self.assertAlmostEqual(again["mem"]["oy"], out["mem"]["oy"], delta=0.12)

    def test_a_gold_grain_names_its_company_and_opens_its_row(self):
        a = self.attrs("done", "zh")
        out = self.play(a, mem={"t": 10.0, "at": 1_000_000 - 3000, "stage": 3}, steps=60)
        self.assertIsNotNone(out["goldAt"])
        self.assertIn(out["goldAt"][2], ("#1 青澜科技 · 399101", "#2 百峰铝业 · 699102", "#3 399103"))
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["scrolled"], out["rows"])
        # with the HUD, the panel opens the row (and the row lights while the grain is pointed at)
        h = self.play(a, mem={"t": 10.0, "at": 1_000_000 - 3000, "stage": 3}, steps=60, hud={"r": 0, "pick": True})
        rank = h["goldAt"][2].split()[0][1:]
        self.assertIn("pick:" + rank, h["hud"])
        self.assertIn("hover:" + rank, h["hud"])
        self.assertEqual(h["rows"], [])

    def test_calm_reading_settling_and_release(self):
        a = self.attrs("screening", "zh")
        out = self.play(a, mem={"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3}, drive=CALM)
        self.assertEqual(out["fast"], 0)                                   # sweeping: no sieve tooltip in the way
        self.assertIsNotNone(out["rest"])
        self.assertEqual(out["rest"]["early"], "")                         # not at once ...
        self.assertIn(out["rest"]["after"], (a["tip0"], a["tip1"], a["tip2"]))   # ... after a short rest
        self.assertLessEqual(out["stillFps"], 36)                          # a still cursor stops pushing
        m0, m1 = out["held"]
        self.assertAlmostEqual(m0, m1, delta=0.01)                         # a held drag released: no fling

    def test_model_viewer_controls(self):
        a = self.attrs("screening", "en")
        o = self.play(a, mem={"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3}, drive=CONTROLS)
        d0 = 3.2 * 1.95                                                    # the harness's 900 x 300 scene
        self.assertLess(o["wheelIn"]["d"], 0.87 * d0)                      # the wheel zooms in ...
        self.assertEqual(o["wheelIn"]["prevented"], 1)                     # ... instead of scrolling the page
        self.assertAlmostEqual(o["wheelOut"], 2.2 * d0, places=3)          # zooming out stops at its limit
        # nothing of the view is kept after a reset (the breathing and the reading log keep their own state)
        self.assertEqual(set(o["afterReset"]) - {"br", "log"}, {"t", "at", "stage", "f", "a"})
        self.assertEqual((o["pan"]["oy"], o["pan"]["op"]), (0, 0))         # a right-drag pans, never orbits
        self.assertGreater(abs(o["pan"]["tx"]) + abs(o["pan"]["tz"]), 0.1)
        self.assertGreater(abs(o["shiftPan"]["ty"]), 0.05)                 # a shift-drag down pans too
        self.assertLess(o["pinch"], 0.97 * d0)                             # two fingers spreading zoom in
        self.assertAlmostEqual(o["fly"]["d"], 0.42 * d0, delta=0.01)       # a double-click flies to the sieve ...
        # ... the middle one (y 0): its look-at point takes out the lean towards the working sieve (0.12 down)
        self.assertAlmostEqual(o["fly"]["ty"], 0.12, delta=0.06)
        self.assertNotIn("d", o["dblReset"])                               # a double-click on nothing resets
        self.assertEqual((o["k1"], o["k2"]), (a["tip0"], a["tip1"]))       # the keys step through the sieves
        self.assertAlmostEqual(o["kfly"]["d"], 0.42 * d0, delta=0.01)
        self.assertNotIn("d", o["kreset"])
        self.assertAlmostEqual(o["kzoom"], 0.8 * d0, delta=0.01)
        self.assertGreater(o["korbit"], 0.1)
        self.assertEqual(o["menu"], 1)                                     # the right button pans, no menu
        self.assertLess(o["ms"], 50)

    def test_a_lost_release_never_leaves_a_drag_stuck(self):
        drive = r"""
function step(ms){now+=ms;const q=rafQ;rafQ=[];q.forEach(f=>f(now));}
for(let i=0;i<STEPS;i++)step(16);
function ev(k,x,y,o){const e=Object.assign({clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1,preventDefault(){}},o||{});(cvL[k]||(()=>{}))(e);}
function mem(){if(listeners['w:beforeunload'])listeners['w:beforeunload']();return JSON.parse(store['jevscreen-sand:'+ATTRS.key]||'{}');}
const out={};
// released elsewhere (no pointerup here): the next plain move, with no button held, ends the press
ev('pointerdown',300,150);ev('pointermove',302,150);for(let i=1;i<=20;i++){now+=16;ev('pointermove',302+i*12,150,{buttons:0});step(0);}
for(let i=0;i<60;i++)step(16);out.moved=mem().oy||0;
// the capture lost (the window left while held): the same
ev('pointerdown',300,150);(cvL.lostpointercapture||(()=>{}))({pointerId:1});for(let i=1;i<=20;i++){now+=16;ev('pointermove',300+i*12,150,{buttons:0});step(0);}
for(let i=0;i<60;i++)step(16);out.lost=mem().oy||0;
console.log(JSON.stringify(out));
"""
        o = self.play(self.attrs("screening", "en"), mem={"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3}, drive=drive)
        self.assertEqual((o["moved"], o["lost"]), (0, 0))

    def test_reduced_motion_keeps_tips_rows_and_the_controls_without_motion(self):
        a = self.attrs("screening", "en")
        out = self.play(a, reduced=True)
        self.assertIn(a["tip1"], out["seen"])
        self.assertGreater(out["mem"]["oy"], 0.2)          # a drag still turns the view (it jumps, no glide)
        self.assertNotIn("px", out["mem"])                 # no tilt under reduced motion
        d = self.play(self.attrs("done", "en"), reduced=True)
        self.assertEqual(len(d["rows"]), 1)                # the grains still open their rows


CAMT = r"""
const C=SandCam.make(),P=SandCam.P,o={},out={};
function prj(x,y,z,F,ox,oy){SandCam.basis(P.y0+C.a,P.p0+C.b,o);const cx=C.x-o.fx*C.c,cy=C.y-o.fy*C.c,cz=C.z-o.fz*C.c;
 const dx=x-cx,dy=y-cy,dz=z-cz,zc=dx*o.fx+dy*o.fy+dz*o.fz,s=F/zc;return [ox+(dx*o.rx+dy*o.ry+dz*o.rz)*s,oy-(dx*o.ux+dy*o.uy+dz*o.uz)*s];}
const F=500,OX=400,OY=300;
// a world point under the pointer at (520, 240): after a zoom towards the pointer it is still under it
SandCam.basis(P.y0,P.p0,o);const ax=(520-OX)/F,ay=-(240-OY)/F;
const Pw=[C.x+C.c*(ax*o.rx+ay*o.ux)+0.0*o.fx,C.y+C.c*(ax*o.ry+ay*o.uy),C.z+C.c*(ax*o.rz+ay*o.uz)];
SandCam.zoom(C,0.5,ax,ay,o);SandCam.snap(C);out.zoom=prj(Pw[0],Pw[1],Pw[2],F,OX,OY);out.d=C.d;
// ... and about any world point (a sieve's surface nearer or deeper than the look-at point): it stays where it is

// a pan: the point under the pointer follows it
SandCam.basis(P.y0+C.a,P.p0+C.b,o);const q0=prj(Pw[0],Pw[1],Pw[2],F,OX,OY);SandCam.pan(C,30,-20,F,o);out.pan=[prj(Pw[0],Pw[1],Pw[2],F,OX,OY),q0];
const Q=[0.4,-0.9,0.35],q1=prj(Q[0],Q[1],Q[2],F,OX,OY);SandCam.zoomAbout(C,0.6,Q[0],Q[1],Q[2]);SandCam.snap(C);out.about=[prj(Q[0],Q[1],Q[2],F,OX,OY),q1,C.d];
SandCam.reset(C);SandCam.snap(C);
// limits: the zoom clamps; an orbit far past its limit springs back inside once let go
SandCam.zoom(C,100,0,0,o);out.dmax=C.d;SandCam.zoom(C,1e-4,0,0,o);out.dmin=C.d;
SandCam.reset(C);for(let i=0;i<40;i++)SandCam.orbit(C,40,0,1/60);out.far=C.oy;for(let i=0;i<600;i++)SandCam.step(C,1/60,false);out.back=C.oy;
// inertia: a quick release glides on a little, then stops
SandCam.reset(C);SandCam.snap(C);for(let i=0;i<5;i++)SandCam.orbit(C,10,0,1/60);const r0=C.oy;for(let i=0;i<300;i++)SandCam.step(C,1/60,false);out.glide=[r0,C.oy,C.vy];
// fly and reset glide there and settle; the state a reload keeps is clamped and survives garbage
SandCam.fly(C,0,-2,0.1,2.7);let n=0;while(!SandCam.settled(C)&&n<2000){SandCam.step(C,1/60,false);n++;}out.fly=[C.x,C.y,C.c,n];
SandCam.reset(C);n=0;while(!SandCam.settled(C)&&n<2000){SandCam.step(C,1/60,false);n++;}out.reset=[SandCam.state(C),n];
const L=SandCam.make();SandCam.load(L,{oy:9,op:'x',d:-3,tx:1e9,ty:NaN});out.load=SandCam.state(L);
SandCam.pan(L,1e6,1e6,1,o);out.box=[L.tx,L.ty,L.tz];
console.log(JSON.stringify(out));
"""

# the framing (owner review 2026-09-28): the stack dominates a phone's height too, sits centred, and at the resting
# view no sieve hides any of the one below it
FRAMET = r"""
const P=SandCam.P,o={},out={};
function quads(w,h,iw,ih){const f=SandCam.frame(w,h,iw,ih,0,0);SandCam.basis(P.y0,P.p0,o);
 const cx=P.tx-o.fx*P.d0,cy=P.ty-o.fy*P.d0,cz=P.tz-o.fz*P.d0,pt={};
 function prj(k,u,v){SandCam.spot(k,u,v,pt);const dx=pt.x-cx,dy=pt.y-cy,dz=pt.z-cz,zc=dx*o.fx+dy*o.fy+dz*o.fz;
  return [f.ox+f.Fc*(dx*o.rx+dy*o.ry+dz*o.rz)/zc,f.oy-f.Fc*(dx*o.ux+dy*o.uy+dz*o.uz)/zc];}
 const Q=[0,1,2].map(k=>[[-1,-1],[1,-1],[1,1],[-1,1]].map(([u,v])=>prj(k,u,v)));
 function inq(q,x,y){let s=0;for(let i=0;i<4;i++){const [ax,ay]=q[i],[bx,by]=q[(i+1)%4],c=(bx-ax)*(y-ay)-(by-ay)*(x-ax),g=c>0?1:-1;if(s&&g!==s)return false;s=g;}return true;}
 let hid=0;for(let k=1;k<3;k++)for(let i=0;i<=20;i++)for(let j=0;j<=20;j++){const [x,y]=prj(k,-1+i/10,-1+j/10);if(Q.slice(0,k).some(q=>inq(q,x,y)))hid++;}
 return {h:(f.y1-f.y0)/(h-ih),w:(f.x1-f.x0)/(w-iw),cx:(f.x0+f.x1)/2-(w-iw)/2,hid:hid,gap:P.gap,d0:P.d0};}
for(const [w,h,iw,ih] of [[390,844,0,0],[360,740,0,0],[1280,800,0,0],[1280,800,435,0],[768,1024,0,0],[1920,1080,0,0]]){
 SandCam.world(w,h);out[w+'x'+h+(iw?'p':'')]=quads(w,h,iw,ih);}
// ... and none while the view drifts (the slow orbit and the tilt: yaw +-0.15, pitch +-0.06, the lean 0.12 down)
function hidAt(yaw,pit,lean){SandCam.basis(yaw,pit,o);const d=P.d0,cx=P.tx-o.fx*d,cy=P.ty+lean-o.fy*d,cz=P.tz-o.fz*d,pt={};
 function prj(k,u,v){SandCam.spot(k,u,v,pt);const dx=pt.x-cx,dy=pt.y-cy,dz=pt.z-cz,zc=dx*o.fx+dy*o.fy+dz*o.fz;return [(dx*o.rx+dy*o.ry+dz*o.rz)/zc,-(dx*o.ux+dy*o.uy+dz*o.uz)/zc];}
 const Q=[0,1,2].map(k=>[[-1,-1],[1,-1],[1,1],[-1,1]].map(([u,v])=>prj(k,u,v)));
 function inq(q,x,y){let s=0;for(let i=0;i<4;i++){const [ax,ay]=q[i],[bx,by]=q[(i+1)%4],c=(bx-ax)*(y-ay)-(by-ay)*(x-ax),g=c>0?1:-1;if(s&&g!==s)return false;s=g;}return true;}
 let n=0;for(let k=1;k<3;k++)for(let i=0;i<=20;i++)for(let j=0;j<=20;j++){const [x,y]=prj(k,-1+i/10,-1+j/10);if(Q.slice(0,k).some(q=>inq(q,x,y)))n++;}return n;}
out.drift=0;for(const [w,h] of [[1280,800],[390,844]]){SandCam.world(w,h);for(const dy of [-0.15,0,0.15])for(const dp of [-0.06,0,0.06])out.drift+=hidAt(P.y0+dy,P.p0+dp,-0.12);}
console.log(JSON.stringify(out));
"""

# the camera never enters a sieve's slab: from the resting view and from each sieve's flight, orbits, pans, zooms,
# inertia and reloaded views all keep it at least P.clr from every plane it is over, and never carry it through one
CLEART = r"""
const P=SandCam.P,o={},out={bad:0,cross:0,states:0,refused:0,flights:[]};SandCam.world(1280,800);
function check(C,prev){const s=SandCam.side(C);out.states++;if(s.bad)out.bad++;
 if(prev)for(let k=0;k<3;k++)if(prev[k]&&s.s[k]&&prev[k]!==s.s[k])out.cross++;return s.s.slice();}
const starts=[null,0,1,2];
for(const k of starts){for(let r=0;r<40;r++){const C=SandCam.make();
 if(k!==null){const c=P.sc[k];SandCam.fly(C,c[0],c[1],c[2],P.d0*0.42);if(r===0)out.flights.push(SandCam.side(C).bad);}
 SandCam.snap(C);let prev=check(C,null);let seed=r*7+(k===null?0:k+1)*101;
 function rnd(){seed=(seed*9301+49297)%233280;return seed/233280;}
 for(let i=0;i<260;i++){const m=rnd();SandCam.basis(P.y0+C.a,P.p0+C.b,o);let ok=true;
  if(m<0.35)ok=SandCam.orbit(C,(rnd()-0.5)*120,(rnd()-0.5)*160,1/60);
  else if(m<0.6)ok=SandCam.pan(C,(rnd()-0.5)*160,(rnd()-0.5)*160,500,o);
  else if(m<0.9)ok=SandCam.zoom(C,Math.exp((rnd()-0.6)*0.9),(rnd()-0.5)*0.6,(rnd()-0.5)*0.6,o);
  else{for(let j=0;j<20;j++)SandCam.step(C,1/60,false);}
  if(ok===false)out.refused++;SandCam.snap(C);prev=check(C,prev);}}}
// the reviewer's cases: fly to the middle sieve, then look up; pan and zoom into the middle sieve's plane
const A=SandCam.make();SandCam.fly(A,0,0,0.1,P.d0*0.42);SandCam.snap(A);let pv=check(A,null);
for(let i=0;i<60;i++){SandCam.orbit(A,0,-12,1/60);pv=check(A,pv);}for(let i=0;i<60;i++){SandCam.orbit(A,0,12,1/60);pv=check(A,pv);}
const B=SandCam.make();SandCam.basis(P.y0,P.p0,o);pv=check(B,null);for(let i=0;i<80;i++){SandCam.zoom(B,0.85,0,-0.05,o);pv=check(B,pv);SandCam.pan(B,0,6,500,o);pv=check(B,pv);}
// a reloaded view inside a slab comes back as the resting one
const L=SandCam.make();SandCam.load(L,{d:0.8,ty:-0.16,tx:0,tz:0.1,oy:0,op:-0.5});out.loaded=[SandCam.side(L).bad,SandCam.state(L)];
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestCamera(unittest.TestCase):
    def node(self, js: str) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "c.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_the_camera_is_pure_and_in_the_page(self):
        self.assertIn(page_sand.CAM, page_sand.JS)
        for bad in ("document", "window", "Date", "performance", "Math.random"):
            self.assertNotIn(bad, page_sand.CAM, bad)

    def test_zoom_pan_limits_inertia_fly_reset(self):
        o = self.node(page_sand.CAM + CAMT)
        self.assertAlmostEqual(o["zoom"][0], 520, places=6)              # the point under the pointer stays
        self.assertAlmostEqual(o["zoom"][1], 240, places=6)
        self.assertAlmostEqual(o["d"], 3.2)                              # half the resting 6.4
        (x1, y1), (x0, y0), d = o["about"]
        self.assertAlmostEqual(x1, x0, places=6)
        self.assertAlmostEqual(y1, y0, places=6)
        self.assertAlmostEqual(d, 0.6 * 3.2)
        (x1, y1), (x0, y0) = o["pan"]
        self.assertAlmostEqual(x1 - x0, 30, places=6)                    # a pan carries the scene with the pointer
        self.assertAlmostEqual(y1 - y0, -20, places=6)
        self.assertEqual((o["dmax"], o["dmin"]), (14, 0.8))
        self.assertGreater(o["far"], 1.1)                                # dragged past the limit (resisting) ...
        self.assertLessEqual(o["back"], 1.1 + 1e-3)                      # ... it springs back inside
        r0, r1, v = o["glide"]
        self.assertGreater(r1, r0)                                       # a release glides on a little ...
        self.assertLess(r1 - r0, 0.6)
        self.assertEqual(v, 0)                                           # ... and stops
        x, y, d, n = o["fly"]
        self.assertAlmostEqual(x, 0, delta=1e-3)
        self.assertAlmostEqual(y, -2, delta=1e-3)
        self.assertAlmostEqual(d, 2.7, delta=1e-3)
        self.assertLess(n, 120)                                          # settles within ~2 s
        self.assertIsNone(o["reset"][0])                                 # back at the resting view: nothing kept
        self.assertEqual(o["load"], {"oy": 1.1, "op": 0, "d": 0.8, "tx": 1.4, "ty": 0, "tz": 0.1})
        self.assertEqual([abs(v) for v in o["box"]], [1.4, 2.6, 1.4])      # the look-at point stays near the stack

    def test_the_stack_dominates_and_nothing_hides_a_sieve(self):
        o = self.node(page_sand.CAM + FRAMET)
        self.assertEqual(o.pop("drift"), 0)                              # nor while the view drifts
        for k, f in o.items():
            self.assertEqual(f["hid"], 0, k)                              # every sieve fully in view at rest
            self.assertLess(abs(f["cx"]), 2, k)                           # centred in the free part
        self.assertGreaterEqual(o["390x844"]["h"], 0.7)                   # a phone: ~3/4 of its height
        self.assertGreaterEqual(o["360x740"]["h"], 0.7)
        self.assertGreater(o["390x844"]["gap"], o["1280x800"]["gap"])     # ... by a wider gap on a portrait screen
        for k in ("1280x800", "1280x800p", "768x1024", "1920x1080"):
            self.assertGreaterEqual(o[k]["h"], 0.8, k)

    def test_the_camera_never_enters_a_sieve(self):
        o = self.node(page_sand.CAM + CLEART)
        self.assertGreater(o["states"], 30000)
        self.assertEqual((o["bad"], o["cross"]), (0, 0))
        self.assertGreater(o["refused"], 0)                               # the sweep did press against the sieves
        self.assertEqual(o["flights"], [False, False, False])              # each sieve's flight lands clear
        self.assertEqual(o["loaded"], [False, None])                      # a view inside a slab is not restored


if __name__ == "__main__":
    unittest.main()

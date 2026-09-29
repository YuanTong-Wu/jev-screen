"""The breathing cycle and the reading log of the one page's sand scene (jevscreen.page_sand, owner request
2026-09-29 after tianzhi.live): the pure breathing state machine (idle -> flat grid -> hold -> the 3D stack again,
damped only, the deeper sieves slower, any use or a rush cancels it, reduced motion never runs it, it survives a
reload), the pure typing of the log (a constant rate, a bounded burst, a slow replay when nothing is new), the page
data (a bounded list of the run's real per-company answers, one language per page, never a path or a secret), the
worker recording them as Jev answers them, and the whole page in node with a stand-in DOM. Synthetic data only."""
from __future__ import annotations

import html as _html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)
from jevscreen import jev, page, page_sand, pagestatus  # noqa: E402
import test_jev as TJ  # noqa: E402
import test_page_sand as TPS  # noqa: E402
import test_quickstart as TQ  # noqa: E402

NODE = shutil.which("node")


def node(js: str) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "t.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------------------------------------ the breathing (pure)

BREATHT = r"""
const H=1/30,out={};
function run(B,sec,o,f){const n=Math.round(sec/H);for(let i=0;i<n;i++){SandBreath.step(B,H,o||{});if(f)f(B,i);}}
// idle: nothing before P.idle s; then the camera rises and the sieves come down, smoothly (no jump in speed), the
// deeper ones slower; flat and held; the 3D stack again; the cycle repeats
let B=SandBreath.make(),P=SandBreath.P,maxStep=0,prev=[0,0,0,0],firstV=null,order=true;
run(B,P.idle-0.1);out.beforeIdle={p:B.p,f:B.f.slice()};
run(B,0.2+0.1,{},(b)=>{if(firstV===null&&b.p>=0)firstV=b.f[3];});
const trace=[];prev=B.f.slice();
run(B,P.cyc*2,{},(b,i)=>{for(let k=0;k<4;k++){maxStep=Math.max(maxStep,Math.abs(b.f[k]-prev[k]));prev[k]=b.f[k];}
 if(b.p>0.5&&b.p<P.flat&&!(b.f[0]>=b.f[1]&&b.f[1]>=b.f[2]))order=false;if(i%15===0)trace.push([+b.p.toFixed(2),+b.f[3].toFixed(4),+b.f[2].toFixed(4)]);});
out.firstV=firstV;out.maxStep=maxStep;out.order=order;out.trace=trace;
// the value at a few points of the cycle (p from its start)
function at(sec){const b=SandBreath.make();run(b,P.idle+sec);return b.f.slice();}
out.flat=at(6.3);out.mid=at(P.flat+2.2);out.back=at(P.cyc-0.5);out.again=at(P.cyc+6.3);
// a brief hold: how long every sieve is at least 0.9 flat, how long the 3D stack shows (all at most 0.1), per cycle
const T=SandBreath.make();run(T,P.idle+P.cyc);let hold=0,stack=0;
run(T,P.cyc,{},(b)=>{if(Math.min(b.f[0],b.f[1],b.f[2],b.f[3])>=0.9)hold+=H;if(Math.max(b.f[0],b.f[1],b.f[2],b.f[3])<=0.1)stack+=H;});
out.hold=hold;out.stack=stack;
// a press mid-flat (hand): the camera's lift is dropped at once, the sieves glide back
const Hh=SandBreath.make();run(Hh,P.idle+5);SandBreath.hand(Hh);out.hand0=Hh.f.slice();run(Hh,0.3);out.hand1=Hh.f.slice();
// a use of the page mid-flat: the cycle hands back (a faster glide, never a jump) and waits for idle again
const C=SandBreath.make();run(C,P.idle+8);SandBreath.poke(C);let mono=true,last=C.f[3],cs=0;
run(C,2.2,{},(b)=>{if(b.f[3]>last+1e-9)mono=false;cs=Math.max(cs,Math.abs(b.f[3]-last));last=b.f[3];});
out.cancel={f:C.f.slice(),p:C.p,mono:mono,step:cs};run(C,P.idle-2.4);out.cancelWait=C.p;run(C,0.5);out.cancelAgain=C.p;
// a rush takes priority (the same as a use); busy (a tooltip, a press) holds it off
const R=SandBreath.make();run(R,P.idle+8);run(R,3,{rush:true});out.rush={f:R.f[3],p:R.p};
const U=SandBreath.make();run(U,P.idle*3,{busy:true});out.busy={f:U.f[3],p:U.p};
// reduced motion never runs it
const M=SandBreath.make();run(M,P.idle+10,{reduced:true});out.reduced={f:M.f.slice(),p:M.p};
// a reload: the state saved, the reload's seconds stepped through, the same motion as without a reload
const A=SandBreath.make(),Bb=SandBreath.make();run(A,P.idle+5.4);const saved=JSON.parse(JSON.stringify(SandBreath.state(A)));
run(A,0.6);SandBreath.load(Bb,saved);SandBreath.advance(Bb,0.6,{});out.reload={a:A.f.slice(),b:Bb.f.slice()};
// garbage in the memory changes nothing
const G=SandBreath.make();SandBreath.load(G,{p:'x',i:-5,g:[2,null],f:[NaN,0.5,9]});out.garbage=G;
out.cyc=P.cyc;out.idle=P.idle;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node not installed")
class TestBreathingCore(unittest.TestCase):
    def test_the_breathing_is_pure_and_in_the_page(self):
        for bad in ("document", "window", "Date.now", "performance"):
            self.assertNotIn(bad, page_sand.BREATH)
        self.assertIn(page_sand.BREATH, page_sand.JS)

    def test_the_cycle(self):
        o = node(page_sand.BREATH + BREATHT)
        self.assertTrue(20 <= o["cyc"] <= 24)                     # one cycle ~20-24 s
        self.assertEqual(o["idle"], 6)                            # after ~6 s without any use
        self.assertEqual((o["beforeIdle"]["p"], o["beforeIdle"]["f"]), (-1, [0, 0, 0, 0]))
        self.assertLess(o["firstV"], 3e-3)                        # a damped start: no jump in speed (1st order: 3 %)
        self.assertLess(o["maxStep"], 0.03)                       # never a snap (a frame moves at most 3 %)
        self.assertTrue(o["order"])                               # the deeper a sieve, the slower it follows
        self.assertGreater(min(o["flat"]), 0.93)                  # the flat grid, from above
        self.assertTrue(0.15 < o["mid"][3] < 0.85, o["mid"])      # expanding back
        self.assertLess(max(o["back"]), 0.02)                     # the 3D stack (with its slow orbit)
        self.assertGreater(min(o["again"]), 0.93)                 # and again
        self.assertTrue(1.5 <= o["hold"] <= 4.0, o["hold"])       # held briefly (~2-3 s) ...
        self.assertGreater(o["stack"], o["cyc"] / 2)              # ... the 3D stack fills more than half the cycle

    def test_a_press_hands_the_camera_over_at_once(self):
        o = node(page_sand.BREATH + BREATHT)
        self.assertEqual(o["hand0"][3], 0)                        # the camera is the user's from the first frame
        self.assertGreater(min(o["hand0"][:3]), 0.8)              # the sieves are still flat ...
        self.assertTrue(0.2 < min(o["hand1"][:3]) < min(o["hand0"][:3]))   # ... and glide back (no snap)
        self.assertEqual(o["hand1"][3], 0)

    def test_any_use_a_rush_and_reduced_motion(self):
        o = node(page_sand.BREATH + BREATHT)
        c = o["cancel"]
        self.assertEqual(c["p"], -1)
        self.assertTrue(c["mono"])                                # glides back, never overshoots
        self.assertLess(c["step"], 0.06)                          # smoothly (a faster glide, no jump)
        self.assertLess(max(c["f"]), 0.08)                        # the user has the 3D view again within ~2 s
        self.assertEqual(o["cancelWait"], -1)                     # and the idle count starts again
        self.assertGreaterEqual(o["cancelAgain"], 0)
        self.assertLess(o["rush"]["f"], 0.05)
        self.assertEqual(o["rush"]["p"], -1)
        self.assertEqual((o["busy"]["f"], o["busy"]["p"]), (0, -1))
        self.assertEqual((o["reduced"]["f"], o["reduced"]["p"]), ([0, 0, 0, 0], -1))

    def test_a_reload_continues_the_motion(self):
        o = node(page_sand.BREATH + BREATHT)
        for a, b in zip(o["reload"]["a"], o["reload"]["b"]):
            self.assertAlmostEqual(a, b, delta=2e-3)
        self.assertEqual(o["garbage"], {"p": -1, "idle": 0, "g": [0, 0, 0, 0], "f": [0, 0.5, 0, 0]})


# ------------------------------------------------------------------------------------------ the log's typing (pure)

LOGT = r"""
const out={},H=1/60;
function run(L,sec,f){const n=Math.round(sec/H);let ch=0;for(let i=0;i<n;i++){const r=SandLog.step(L,H);if(r)ch++;if(f)f(L,i);}return ch;}
const P=SandLog.P,ev=(n,s)=>[n,s];
// a constant rate: a 30-character line takes 1 s at 30 characters a second, then a short pause
const L=SandLog.make();SandLog.feed(L,[ev(1,'x'.repeat(30))]);const seen=[];run(L,0.5,(l)=>seen.push(SandLog.shown(l).length));
out.half=SandLog.shown(L).length;run(L,0.55);out.done={lines:L.lines.slice(),cur:L.cur};
// a burst: only the newest P.back lines are typed, oldest first; an old sequence number is never typed again
const B=SandLog.make();const burst=[];for(let i=1;i<=60;i++)burst.push(ev(i,'line '+i));SandLog.feed(B,burst);out.queued=B.q.slice();
SandLog.feed(B,burst);out.again=B.q.length;SandLog.feed(B,burst.concat([ev(61,'line 61')]));out.more=B.q[B.q.length-1];
// the first view: all but the newest few at once, those typed
const F=SandLog.make();SandLog.feed(F,burst,3);out.first={lines:F.lines.length,last:F.lines[F.lines.length-1],q:F.q.slice()};
// capped column; nothing new for P.idle s: the recent lines come again, one by one, slowly
const I=SandLog.make();SandLog.feed(I,burst.slice(0,20),0);out.capped=I.lines.length;const n0=I.lines.length;
let typedAt=[],was='';run(I,P.idle+3*P.every+2,(l,i)=>{if(l.cur&&!was)typedAt.push(+(i*H).toFixed(2));was=l.cur;});
out.replay={at:typedAt,rep:I.rep,last:I.lines[I.lines.length-1]};
// a reload: the state saved and loaded goes on where it was
const A=SandLog.make();SandLog.feed(A,burst.slice(0,5),2);run(A,0.3);const st=JSON.parse(JSON.stringify(SandLog.state(A)));
const Bb=SandLog.load(SandLog.make(),st);out.reload={a:SandLog.shown(A),b:SandLog.shown(Bb),q:Bb.q,lines:Bb.lines,n:Bb.n};
// garbage never enters: non-strings dropped, long strings cut
const G=SandLog.load(SandLog.make(),{n:'x',lines:[1,{},'ok','y'.repeat(500)],cur:5,q:null});out.garbage={n:G.n,lines:G.lines.map(s=>s.length),cur:G.cur};
const T=SandLog.make();SandLog.feed(T,[[1,'<b>x</b>'],['2','no'],[3,7],null,[4,'z'.repeat(300)]],0);out.feedBad=T.lines.map(s=>s.length);
// a finished page left alone: many quiet rounds never put a line in the column twice; quiet lines only (rep)
function dupes(L){return L.lines.length-new Set(L.lines).size;}
function soak(evs,sec){const Z=SandLog.make();SandLog.feed(Z,evs,3);let d=0,loud=0,inplace=0,seen=new Set();
 run(Z,sec,(l)=>{d=Math.max(d,dupes(l)+(l.cur&&l.ri<0&&l.lines.indexOf(l.cur)>=0?1:0));if(l.cur&&!l.rep&&l.quiet>0)loud++;if(l.ri>=0)inplace++;if(l.cur&&l.rep)seen.add(l.cur);});
 return {d:d,loud:loud,inplace:inplace,lines:Z.lines.length,seen:seen.size};}
out.soak60=soak(burst,400);out.soak20=soak(burst.slice(0,20),300);out.soak5=soak(burst.slice(0,5),200);
// the numbers run backwards (another job for the same idea; the rows before a job's first answers): typed again
const S=SandLog.load(SandLog.make(),{n:40,lines:['old 1'],og:'r'});SandLog.feed(S,[1,2,3,4,5].map(i=>ev(i,'new '+i)),undefined,'r');out.back={q:S.q.slice(),n:S.n};
const S2=SandLog.load(SandLog.make(),{n:3,og:'r'});SandLog.feed(S2,[1,2,3,4,5].map(i=>ev(i,'job '+i)),undefined,'jabc');out.src={q:S2.q.slice(),og:S2.og};
const S3=SandLog.load(SandLog.make(),{n:3,og:'jabc'});SandLog.feed(S3,[1,2,3,4,5].map(i=>ev(i,'job '+i)),undefined,'jabc');out.same=S3.q.slice();
// a real answer never waits behind a quiet line
const Qq=SandLog.make();SandLog.feed(Qq,burst.slice(0,20),0,"j");run(Qq,P.idle+0.5);out.quietBefore=Qq.rep;SandLog.feed(Qq,burst.slice(0,21),undefined,"j");
out.quietAfter={rep:Qq.rep,cur:Qq.cur,q:Qq.q.slice()};
// cheap: a minute of typing and breathing, stepped at 60 Hz
const t0=Date.now();const K=SandLog.make(),Q=SandBreath.make();SandLog.feed(K,burst);for(let i=0;i<3600;i++){SandLog.step(K,H);SandBreath.step(Q,H,{});}
out.ms=Date.now()-t0;out.P=P;
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node not installed")
class TestLogCore(unittest.TestCase):
    def setUp(self):
        self.o = node(page_sand.BREATH + page_sand.READLOG + LOGT)

    def test_the_log_is_pure_and_in_the_page(self):
        for bad in ("document", "window", "Date.now", "innerHTML"):
            self.assertNotIn(bad, page_sand.READLOG)
        self.assertIn(page_sand.READLOG, page_sand.JS)

    def test_a_constant_rate_and_a_bounded_burst(self):
        o = self.o
        self.assertIn(o["half"], (14, 15))                        # 30 chars/s: half a line in half a second
        self.assertEqual((o["done"]["lines"], o["done"]["cur"]), (["x" * 30], ""))
        self.assertEqual(o["queued"], [f"line {i}" for i in range(51, 61)])    # the newest 10, oldest first
        self.assertEqual(o["again"], 10)                          # an event is queued once
        self.assertEqual(o["more"], "line 61")
        self.assertEqual(o["first"], {"lines": 14, "last": "line 57", "q": ["line 58", "line 59", "line 60"]})

    def test_idle_replays_the_recent_lines_slowly(self):
        o, P = self.o, self.o["P"]
        self.assertEqual(o["capped"], P["max"])
        at = o["replay"]["at"]
        self.assertGreaterEqual(len(at), 3)
        self.assertGreaterEqual(at[0], P["idle"] - 0.05)           # only after a quiet while
        for a, b in zip(at, at[1:]):
            self.assertGreater(b - a, P["every"] - 0.1)            # one line every few seconds
        self.assertTrue(o["replay"]["rep"])
        self.assertTrue(o["replay"]["last"].startswith("line "))

    def test_quiet_rounds_never_repeat_a_line(self):
        o = self.o
        for k in ("soak60", "soak20", "soak5"):
            self.assertEqual(o[k]["d"], 0, k)                     # never the same line twice in the column
            self.assertEqual(o[k]["loud"], 0, k)                  # a quiet line never has the bright edge
            self.assertGreaterEqual(o[k]["seen"], 3, k)           # it does go over the run's lines
        self.assertEqual(o["soak5"]["lines"], 5)                  # all in view: typed again in their own rows
        self.assertGreater(o["soak5"]["inplace"], 0)
        self.assertEqual(o["soak60"]["lines"], o["P"]["max"])

    def test_the_numbers_start_again(self):
        o = self.o
        self.assertEqual(o["back"], {"q": ["new 1", "new 2", "new 3", "new 4", "new 5"], "n": 5})
        self.assertEqual(o["src"], {"q": ["job 1", "job 2", "job 3", "job 4", "job 5"], "og": "jabc"})
        self.assertEqual(o["same"], ["job 4", "job 5"])            # the same source: only what is new
        self.assertTrue(o["quietBefore"])
        self.assertEqual(o["quietAfter"], {"rep": False, "cur": "", "q": ["line 21"]})

    def test_reload_and_garbage(self):
        o = self.o
        self.assertEqual(o["reload"]["a"], o["reload"]["b"])
        self.assertEqual(o["reload"]["n"], 5)
        self.assertEqual(o["garbage"], {"n": 0, "lines": [2, 80], "cur": ""})
        self.assertEqual(o["feedBad"], [8, 80])                  # strings only, cut to 80 (shown as text, never markup)
        self.assertLess(o["ms"], 200)                            # a minute of both at 60 Hz: well under 1 ms a frame


# ------------------------------------------------------------------------------------------ the log's data

CJK = re.compile(r"[㐀-鿿]")


class TestLogData(TPS.Case):
    def test_a_line_carries_only_a_security_id_and_fixed_words(self):
        self.assertEqual(page_sand.log_line({"layer": "l2", "sid": "SZSE:300990", "label": "partial",
                                             "doc": "annual_report"}, "zh"), "300990 · 相关 · 读年报")
        self.assertEqual(page_sand.log_line({"layer": "l1", "sid": "NASDAQ:OKTA", "label": "core"}, "en"),
                         "OKTA · core · profile")
        self.assertEqual(page_sand.log_line({"layer": "l2", "sid": "TSE:4203", "label": "insufficient",
                                             "doc": "profile"}, "en"), "4203 · thin · reread")
        for bad in ({"layer": "l1", "sid": "/Users/me/.jevscreen/keys/openrouter", "label": "core"},
                    {"layer": "l1", "sid": "C:\\Users\\me\\key.txt", "label": "core"},
                    {"layer": "l1", "sid": "sk-" + "or-v1-0123456789abcdef0123", "label": "core"},
                    {"layer": "l1", "sid": "NYSE:ROBO", "label": "sk-or-v1-0123"},
                    {"layer": "l1", "sid": "NYSE:ROBO", "label": "explicit"},        # an L2 word on an L1 answer
                    {"layer": "l3", "sid": "NYSE:ROBO", "label": "core"},
                    {"layer": "l1", "sid": "NYSE:ROBO VERY LONG NAME", "label": "core"}, None, "x"):
            self.assertIsNone(page_sand.log_line(bad, "en"), bad)

    def test_every_word_in_the_page_language(self):
        for lang, W in page_sand.LOG_WORDS.items():
            for w in W.values():
                self.assertEqual(bool(CJK.search(w)), lang == "zh", w)
        # all combinations: a zh line is the ticker and Chinese words, an en line has no CJK
        for layer, labels in (("l1", ("core", "adjacent", "unrelated", "insufficient")),
                              ("l2", ("explicit", "partial", "contradicted", "insufficient"))):
            for lab in labels:
                for doc in (None, "annual_report", "profile"):
                    zh = page_sand.log_line({"layer": layer, "sid": "NYSE:ROBO", "label": lab, "doc": doc}, "zh")
                    en = page_sand.log_line({"layer": layer, "sid": "NYSE:ROBO", "label": lab, "doc": doc}, "en")
                    tk, lb, st = zh.split(" · ")
                    self.assertEqual(tk, "ROBO")
                    self.assertRegex(st + lb, r"^[㐀-鿿]+$")
                    self.assertNotRegex(en, CJK)

    def test_bounded_and_ordered(self):
        evs = [{"n": i, "layer": "l1", "sid": f"SZSE:{300000 + i}", "label": "unrelated"} for i in range(1, 200)]
        out = page_sand.log_lines({"progress": {"events": evs}}, "zh")
        self.assertEqual(len(out), page_sand.LOG_MAX)
        self.assertEqual(page_sand.LOG_MAX, 60)
        self.assertEqual([n for n, _ in out], list(range(140, 200)))
        self.assertEqual(page_sand.log_lines(None, "en"), [])
        self.assertEqual(page_sand.log_lines({"progress": {"events": "x"}}, "en"), [])

    def test_the_page_data_carries_the_log(self):
        for lang in ("zh", "en"):
            html = self.page("screening", lang)
            blob = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html,
                                        re.S).group(1))
            log = blob["live"]["log"]
            self.assertEqual([n for n, _ in log], [1, 2, 3, 4, 5])        # the malformed 6 and 7 never reach it
            want = {"zh": ["399101 · 核心 · 读简介", "699102 · 相邻 · 读简介", "399103 · 无关 · 读简介",
                           "399101 · 写明 · 读年报", "699102 · 相关 · 复读简介"],
                    "en": ["399101 · core · profile", "699102 · adjacent · profile",
                           "399103 · off-topic · profile", "399101 · stated · report",
                           "699102 · related · reread"]}[lang]
            self.assertEqual([s for _, s in log], want)
            a = TPS.attrs_of(html)
            self.assertEqual(json.loads(a["log"]), log)                    # the scene reads it from its element
            for bad in ("/home/me", "keys", "openrouter", "sk-or", ".jevscreen"):
                self.assertNotIn(bad, a["log"])
            self.assertIn('<div class="sandlog" aria-hidden="true"></div>', html)
            self.assertRegex(a["log-from"], r"^j[0-9a-f]{0,8}$")          # the job's numbers
            self.assertEqual(TPS.attrs_of(self.page("done", lang))["log-from"], "r")   # the rows' own

    def test_a_finished_page_logs_its_rows_and_nothing_else(self):
        for lang in ("zh", "en"):
            a = TPS.attrs_of(self.page("done", lang))
            log = json.loads(a["log"])
            self.assertTrue(1 <= len(log) <= 60)
            for _, s in log:
                self.assertRegex(s, r"^\S+ · ")
                (self.assertRegex if lang == "zh" else self.assertNotRegex)(s.split(" · ", 1)[1], CJK)
        self.assertEqual(page_sand.log_from_rows({"rows": [{"security_id": "../../etc/passwd",
                                                            "details": {"l2": "explicit"}}]}), [])

    def test_the_status_block_carries_the_log(self):
        job = {"state": "running", "steps": [], "progress": {"events": [
            {"n": 3, "layer": "l2", "sid": "HKEX:0700", "label": "explicit", "doc": "annual_report"}]}}
        live = pagestatus.build(self.cfg, lang="en", job=job)
        self.assertEqual(live["log"], [[3, "0700 · stated · report"]])
        self.assertEqual(pagestatus.build(self.cfg, lang="zh", job=None)["log"], [])

    def test_css_hides_the_log_on_a_narrow_screen_and_in_print(self):
        css = page_sand.CSS
        self.assertIn("@media (max-width:759px){.sandlog{display:none}}", css)
        self.assertIn("@media print{.sandlog{display:none}}", css)
        m = re.search(r"\.sandlog\{[^}]*\}", css).group(0)
        self.assertIn("pointer-events:none", m)
        self.assertIn("mask-image", m)                     # the top and the right fade out
        op = float(re.search(r"opacity:(\.\d+)", m).group(1))
        self.assertTrue(0.25 <= op <= 0.4, op)
        self.assertIn("textContent", page_sand.JS)
        self.assertNotIn("innerHTML", page_sand.JS)


# ------------------------------------------------------------------------------------------ the worker's events

class TestJevOnItem(TJ.JevTestBase):
    def test_each_answer_is_told_once_known_new_or_cached(self):
        seen = []
        c = self.client(TJ.FakeTransport(), run_id="run-1")
        c.on_item = lambda i, lab, ca: seen.append((i, lab, ca))
        res = c.classify(TJ.items(5), TJ.QUESTION)
        self.assertEqual(sorted(seen), sorted((r["item_id"], "core", False) for r in res))
        seen.clear()
        c2 = self.client(TJ.FakeTransport(), run_id="run-2")
        c2.on_item = lambda i, lab, ca: seen.append((i, lab, ca))
        c2.classify(TJ.items(5), TJ.QUESTION)
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(ca for _, _, ca in seen))                 # cached answers are told as well

    def test_a_failing_callback_never_stops_a_run(self):
        c = self.client(TJ.FakeTransport(), run_id="run-3")

        def boom(*a):
            raise RuntimeError("page gone")
        c.on_item = boom
        res = c.classify(TJ.items(3), TJ.QUESTION)
        self.assertEqual([r["status"] for r in res], ["ok"] * 3)


class TestWorkerEvents(TQ.QuickCase):
    def test_a_run_records_its_answers_for_the_log(self):
        out = self.done_flow()
        self.assertEqual(out["status"], "done", out.get("text_en"))
        p = self.job()["progress"]
        evs = p["events"]
        self.assertTrue(evs)
        self.assertLessEqual(len(evs), 60)
        self.assertEqual({e["layer"] for e in evs} <= {"l1", "l2"}, True)
        self.assertIn("l2", {e["layer"] for e in evs})
        ns = [e["n"] for e in evs]
        self.assertEqual(ns, sorted(ns))
        self.assertEqual(p["event_n"], ns[-1])
        for e in evs:
            self.assertEqual(set(e), {"n", "layer", "sid", "label", "doc"})     # nothing else is kept
            self.assertRegex(e["sid"], r"^[A-Z]+:\w+$")
        # the page of the finished job carries them (security id, stage and label words)
        html = page.stable_path(self.cfg, TQ.IDEA).read_text(encoding="utf-8")
        log = json.loads(TPS.attrs_of(html)["log"])
        self.assertTrue(log)
        self.assertTrue(all(" · " in s for _, s in log))

    def test_the_log_is_bounded_and_survives_a_progress_line(self):
        from jevscreen import quickstart as qs
        job = {"progress": {}, "worker": {}, "steps": []}
        w = qs.Worker.__new__(qs.Worker)
        import threading
        w.mu, w.job = threading.RLock(), job
        w.last_progress = w.last_page = 1e18
        w.save = w.page = lambda: None
        for i in range(150):
            w.event({"layer": "l1", "security_id": f"SZSE:{300000 + i}", "label": "core", "evidence": None,
                     "cached": False})
        w.event({"layer": "fetch", "security_id": "SZSE:1"})            # not an answer: ignored
        w.event({"layer": "l1", "security_id": ""})
        self.assertEqual(len(job["progress"]["events"]), qs.EVENTS_MAX)
        self.assertEqual(job["progress"]["event_n"], 150)
        w.progress(4, "zh", "en", 1, 2, stage="l1")
        self.assertEqual(len(job["progress"]["events"]), 60)            # a new progress line keeps them
        self.assertEqual(job["progress"]["events"][-1]["sid"], "SZSE:300149")


# ------------------------------------------------------------------------------------------ the whole page (node)

PAGEDRIVE = r"""
function step(ms){now+=ms;const q=rafQ;rafQ=[];q.forEach(f=>f(now));}
function run(sec){for(let i=0;i<Math.round(sec*1000/16);i++)step(16);}
function mem(){if(listeners['w:beforeunload'])listeners['w:beforeunload']();return JSON.parse(store['jevscreen-sand:'+ATTRS.key]||'{}');}
const out={};
run(RUN1);out.m1=mem();out.log1=LOGN.kids.map(k=>[k.textContent,k.className]);
if(POKE){(listeners['w:'+POKE]||(()=>{}))({clientX:10,clientY:10,key:'a'});run(0.5);out.m2=mem();}
out.ms=box.__ms;out.texts=calls.fillText.slice();
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(NODE, "node not installed")
class TestPageBreathingAndLog(TPS.Case):
    def play(self, state="screening", lang="zh", mem=None, reduced=False, run1=13.0, poke="", now=1_000_000,
             attrs=None):
        a = attrs or TPS.attrs_of(self.page(state, lang))
        drive = PAGEDRIVE.replace("RUN1", str(run1)).replace("POKE", json.dumps(poke))
        js = TPS.js_head(a, mem, reduced, now, 1280, 0) + TPS.HARNESS + page_sand.JS + drive
        return node(js)

    def old(self):
        return {"t": 50.0, "at": 1_000_000 - 600_000, "stage": 3}      # a page seen long ago: no opening, no rush

    def test_idle_the_page_breathes_and_any_use_hands_back(self):
        o = self.play(mem=self.old())
        br = o["m1"]["br"]
        self.assertGreaterEqual(br["p"], 0)                               # 13 s idle: the cycle runs ...
        self.assertGreater(br["f"][3], 0.5)                               # ... the view rises towards the grid
        self.assertEqual(o["texts"], [])                                  # no word drawn on the canvas
        self.assertLess(o["ms"], 50)
        for ev in ("pointerdown", "wheel", "keydown"):
            p = self.play(mem=self.old(), poke=ev)
            self.assertEqual(p["m2"]["br"]["p"], -1, ev)                  # a use anywhere on the page stops it
            self.assertEqual(p["m2"]["br"]["f"][3], 0, ev)                # the camera is handed over at once ...
            self.assertGreater(p["m2"].get("op", 0), 0.3, ev)             # ... as it was seen (from above)
            self.assertGreater(p["m2"]["br"]["f"][2], 0.2, ev)            # the sieves glide back

    def test_a_rush_takes_priority_and_reduced_motion_never_breathes(self):
        live = {"t": 50.0, "at": 1_000_000 - 3000, "stage": 2,
                "br": {"p": 8.0, "i": 14.0, "g": [1, 1, 1, 1], "f": [0.97, 0.96, 0.94, 0.97]}}
        o = self.play(mem=live, run1=1.0)                                 # the first read completes: a rush
        self.assertEqual(o["m1"]["br"]["p"], -1)
        self.assertLess(o["m1"]["br"]["f"][3], 0.5)
        r = self.play(mem=self.old(), reduced=True)
        self.assertNotIn("br", r["m1"])

    def test_the_breathing_goes_on_across_a_reload(self):
        a = self.play(mem=self.old(), run1=10.0)
        m = a["m1"]
        b = self.play(mem=m, run1=0.1, now=m["at"] + 250)                 # the page reloaded 0.25 s later
        for x, y in zip(m["br"]["f"], b["m1"]["br"]["f"]):
            self.assertAlmostEqual(x, y, delta=0.04)
        self.assertGreater(b["m1"]["br"]["p"], m["br"]["p"])

    def test_the_log_types_real_lines_and_goes_on_across_a_reload(self):
        o = self.play(mem=self.old(), run1=0.5)
        lines = [t for t, _ in o["log1"]]
        # the first view: all but the newest three at once, the newest typed
        self.assertEqual(lines[:2], ["399101 · 核心 · 读简介", "699102 · 相邻 · 读简介"])
        self.assertEqual(o["log1"][-1][1], "cur")
        self.assertTrue("399103 · 无关 · 读简介".startswith(lines[-1]))
        done = self.play(mem=self.old(), run1=6.0)
        self.assertEqual([t for t, _ in done["log1"]], ["399101 · 核心 · 读简介", "699102 · 相邻 · 读简介",
                                                        "399103 · 无关 · 读简介", "399101 · 写明 · 读年报",
                                                        "699102 · 相关 · 复读简介"])
        m = done["m1"]
        self.assertEqual(m["log"]["n"], 5)
        again = self.play(mem=m, run1=0.2, now=m["at"] + 300)
        self.assertEqual([t for t, _ in again["log1"]], [t for t, _ in done["log1"]])   # nothing typed twice
        en = self.play(lang="en", mem=self.old(), run1=6.0)
        self.assertTrue(en["log1"])
        self.assertFalse(any(CJK.search(t) for t, _ in en["log1"]))

    def run_drive(self, drive, state="screening", mem=None, hud=None, now=1_000_000):
        a = TPS.attrs_of(self.page(state, "zh"))
        harness = TPS.HARNESS.replace("if(k==='clearRect')return ()=>{calls.draw++;};",
                                      "if(k==='clearRect')return ()=>{calls.draw++;FS.length=0;};").replace(
            "set(t,k,v){t[k]=v;return true;}", "set(t,k,v){if(k==='fillStyle')FS.push(String(v));t[k]=v;return true;}")
        self.assertNotEqual(harness, TPS.HARNESS)
        js = (TPS.js_head(a, mem, False, now, 1280, 0, hud) + "const FS=[];\n" + harness + page_sand.JS +
              "function step(ms){now+=ms;const q=rafQ;rafQ=[];q.forEach(f=>f(now));}\n"
              "function run(sec){for(let i=0;i<Math.round(sec*1000/16);i++)step(16);}\n"
              "function mem(){if(listeners['w:beforeunload'])listeners['w:beforeunload']();"
              "return JSON.parse(store['jevscreen-sand:'+ATTRS.key]||'{}');}\nconst out={};\n" + drive +
              "\nconsole.log(JSON.stringify(out));")
        return node(js)

    def test_flat_the_final_list_is_drawn_over_every_sieve(self):
        # a finished page: flat (the breathing's grid), the amber grains come after the last sieve's veil; in the 3D
        # stack they lie under the sieves above them, as before
        drive = ("run(0.2);const i=(p)=>FS.map((v,j)=>v.indexOf(p)===0?j:-1).filter(j=>j>=0);"
                 "out.amber=i('rgba(239,159,39,');out.veil=i('rgba(7,7,7,');")
        flat = {"t": 50.0, "at": 1_000_000 - 1000, "stage": 4,
                "br": {"p": 3.0, "i": 9.0, "g": [1, 1, 1, 1], "f": [1, 1, 1, 1]}}
        o = self.run_drive(drive, "done", mem=flat)
        self.assertTrue(o["amber"] and o["veil"])
        self.assertGreater(min(o["amber"][len(o["amber"]) // 2:]), max(o["veil"]))
        d3 = self.run_drive(drive, "done", mem=self.old())
        self.assertLess(max(d3["amber"]), max(d3["veil"]))              # 3D: drawn once, under the upper sieves
        self.assertEqual(len(o["amber"]), 2 * len(d3["amber"]))          # flat: once more on top

    def test_a_resting_hand_does_not_stop_the_breathing(self):
        mv = ("function mv(x,y){cvL.pointermove({pointerId:1,pointerType:'mouse',clientX:x,clientY:y,buttons:0});}"
              "run(13);out.p0=mem().br.p;")
        o = self.run_drive(mv + "for(let k=0;k<25;k++){mv(450+(k%2),150);run(0.1);}out.p1=mem().br.p;",
                           mem=self.old())
        self.assertGreaterEqual(o["p0"], 0)
        self.assertGreater(o["p1"], o["p0"])                               # 1 px of jitter over the grid: it goes on
        s = self.run_drive(mv + "for(let k=0;k<12;k++){mv(200+k*25,150);step(16);}run(0.2);out.p1=mem().br.p;",
                           mem=self.old())
        self.assertEqual(s["p1"], -1)                                      # a move with intent hands back

    def test_the_log_steps_aside_of_the_stack(self):
        drive = "run(0.2);out.st=LOGN.style;"
        wide = self.run_drive(drive, mem=self.old())
        self.assertNotEqual(wide["st"].get("visibility"), "hidden")
        self.assertGreaterEqual(int(wide["st"]["width"].rstrip("px")), 150)
        narrow = self.run_drive(drive, mem=self.old(), hud={"r": 560})      # the panel leaves ~340 px of scene
        self.assertEqual(narrow["st"]["visibility"], "hidden")

    def test_reduced_motion_shows_the_lines_at_once(self):
        o = self.play(mem=self.old(), reduced=True, run1=0.2)
        self.assertEqual(len(o["log1"]), 5)
        self.assertNotIn("cur", [c for _, c in o["log1"]])

    def test_no_log_element_without_lines(self):
        a = TPS.attrs_of(self.page("downloading", "en"))
        self.assertEqual(a["log"], "[]")
        o = self.play(attrs=a, mem=self.old(), run1=1.0)
        self.assertEqual(o["log1"], [])


if __name__ == "__main__":
    unittest.main()

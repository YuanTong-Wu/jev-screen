"""The HUD of the one page (jevscreen.page_hud): its elements in every state (the status dot, the instrument, the
results panel, the text-view toggle), one language per page, the CSP untouched, nothing fetched, the text view (the
full plain page; `page --text` unchanged), and its script's wiring in node with a small stand-in DOM built from the
real markup: panel open/close and insets, the text view, rows and grains lighting each other, the live finish, the
status popover, a stopped worker, and the plain page coming back when the scene cannot run. Synthetic jobs only."""
from __future__ import annotations

import html as _html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)
from jevscreen import config, page, page_hud, page_sand  # noqa: E402
import onepage_states as S  # noqa: E402
from test_one_page import ZH_OK_WORDS  # noqa: E402
from test_page_sand import CSP_0928  # noqa: E402

CJK = re.compile(r"[㐀-鿿]")


def hud_of(html: str) -> str:
    m = re.search(r'<div id="hud" class="hud".*?\n</div>\n', html, re.S)
    return m.group(0) if m else ""


class Tree(HTMLParser):
    """HTML -> [tag, {attrs}, [children]] (text as str), for the node stand-in DOM and the language checks."""
    VOID = {"meta", "br", "img", "input", "link", "path"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ["root", {}, []]
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        n = [tag, {k: (v if v is not None else "") for k, v in attrs}, []]
        self.stack[-1][2].append(n)
        if tag not in self.VOID:
            self.stack.append(n)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1][2].append([tag, {k: (v if v is not None else "") for k, v in attrs}, []])

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        if data.strip():
            self.stack[-1][2].append(data)


def tree(html: str) -> list:
    t = Tree()
    t.feed(html)
    return t.root[2]


def words(node, skip=("hud-title",)) -> list[str]:
    """Every text and every title / aria-label in a tree (the idea's own title left out: it is the user's)."""
    out = []
    if isinstance(node, str):
        return [node.strip()]
    tag, attrs, kids = node
    if any(c in (attrs.get("class") or "").split() for c in skip):
        return []
    for k in ("title", "aria-label"):
        if attrs.get(k):
            out.append(attrs[k])
    for c in kids:
        out += words(c, skip)
    return out


class Case(unittest.TestCase):
    def page(self, state: str, lang: str = "zh") -> str:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JEVSCREEN_HOME": tmp}):
            cfg = config.Config(home=Path(tmp)).ensure()
            return S.write_state_page(cfg, state, lang=lang).read_text(encoding="utf-8")


class TestMarkup(Case):
    WANT = {  # state: (status dot, cost, time, stage zh, stage en, panel, open)
        "fresh": ("bad", "—", "—", "等待", "Waiting", "0", "0"),
        "downloading": ("bad", "—", "—", "2 / 3", "2 / 3", "0", "0"),        # the key is still missing
        "waiting_key": ("bad", "—", "—", "等待", "Waiting", "0", "0"),
        "key_rejected": ("bad", "$0.0001 / $1.00", "—", "已停", "Stopped", "0", "0"),
        "blocked": ("bad", "$0.0001 / $1.00", "—", "已停", "Stopped", "0", "0"),
        "screening": ("ok", "$0.18 / $1.00", "—", "3 / 3", "3 / 3", "0", "0"),        # all ready, it runs
        "done": ("ok", "$0.28 / $1.00", "00:30", "完成", "Done", "1", "1"),
    }

    def test_every_state_has_its_hud(self):
        for state in S.STATES:
            for lang in ("zh", "en"):
                html = self.page(state, lang)
                hud = hud_of(html)
                self.assertTrue(hud, (state, lang))
                body = html[html.index("<body>"):]
                # the HUD, then the scene, then the page; the text-view toggle is the first control of the page
                self.assertLess(body.index('<div id="hud"'), body.index('<div id="sand"'))
                self.assertLess(body.index('<div id="sand"'), body.index('<div id="app">'))
                self.assertTrue(body[body.index("<button"):].startswith('<button type="button" class="hud-tv"'))
                dot, cost, time, zh, en, has, opened = self.WANT[state]
                self.assertIn(f'class="hud-stat s-{dot}"', hud, (state, lang))
                vals = re.findall(r'<span class="v" id="hud-(\w+)">([^<]*)</span>', hud)
                self.assertEqual(vals, [("cost", cost), ("time", time), ("stage", zh if lang == "zh" else en)],
                                 (state, lang))
                a = dict(re.findall(r'data-([\w-]+)="([^"]*)"', hud.split(">")[0]))
                self.assertEqual((a["panel"], a["open"]), (has, opened), state)
                self.assertEqual(bool(a["since"]), state in ("downloading", "screening"), state)
                # a problem is said in ONE plain sentence (with its fix) beside the dot; nothing else is written
                says = re.findall(r'<span class="say">([^<]*)</span>', hud)
                self.assertEqual(len(says), 1 if dot == "bad" else 0, state)

    def test_the_dot_is_grey_only_while_a_prerequisite_is_prepared(self):
        live = {"checks": [{"id": "python", "state": "ok", "text": "a"}, {"id": "universe", "state": "run", "text": "b"},
                           {"id": "ready", "state": "wait", "text": "c"}], "alerts": []}
        self.assertEqual(page_hud.status(live, "en")["state"], "run")
        live["checks"][1]["state"] = "ok"
        live["checks"][2]["state"] = "run"
        self.assertEqual(page_hud.status(live, "en"), {"state": "ok", "text": "All set · c", "fix": None})

    def test_the_instrument_and_the_panel_name_their_costs_apart(self):
        # the instrument is the job's total against its cap; the panel's cost is this version's run only
        for lang, k, own in (("zh", "合计", "只算这一版"), ("en", "Total", "this version's only")):
            hud = hud_of(self.page("done", lang))
            row = re.search(r'<div title="([^"]*)"><span class="k">([^<]*)</span><span class="v" id="hud-cost">', hud)
            self.assertIsNotNone(row, lang)
            self.assertEqual(row.group(2), k)
            self.assertIn(own, _html.unescape(row.group(1)))
            self.assertNotEqual(row.group(2), {"zh": "花费", "en": "Cost"}[lang])

    def test_printing_uses_the_light_palette(self):
        css = page_hud.CSS
        pr = css[css.index("@media print{"):]
        self.assertIn("html.hud-on #app>main,#app>main{", pr)
        self.assertIn("--fg:#1d1d1b", pr)                              # the plain page's light tokens ...
        self.assertIn("color:#000!important", pr)                      # ... win over the panel's dark ones
        # the panel's dark skin is for the screen only
        dark = [m.start() for m in re.finditer(r"--fg:#e6e4dc", css)]
        screen = css.index("@media screen{")
        self.assertTrue(all(i > screen or css.rfind(".hud-pop{", 0, i) > css.rfind("}", 0, css.rfind(".hud-pop{", 0, i) + 1) - 1
                            for i in dark))
        self.assertLess(screen, css.index(page_hud.P + "{position:fixed"))
        self.assertLess(css.index(page_hud.P + "{position:fixed"), css.index("@media print{"))

    def test_the_text_views_back_button_is_in_the_flow(self):
        css = page_hud.CSS
        self.assertIn("html.hud-text .hud>.hud-back{display:block;position:static", css)
        self.assertIn("html.hud-text .hud{display:flex;justify-content:flex-end", css)

    def test_one_problem_one_sentence(self):
        html = self.page("waiting_key", "en")
        hud = hud_of(html)
        say = _html.unescape(re.search(r'<span class="say">([^<]*)</span>', hud).group(1))
        fix = _html.unescape(re.search(r'<span class="fix">([^<]*)</span>', hud).group(1))
        self.assertIn("Jev key", say)
        self.assertTrue(fix)
        blocked = hud_of(self.page("blocked", "zh"))
        self.assertIn("TradingView", _html.unescape(re.search(r'<span class="say">([^<]*)</span>', blocked).group(1)))

    def test_one_language_per_page(self):
        for state in S.STATES:
            for lang in ("zh", "en"):
                texts = [t for t in words(["div", {}, tree(hud_of(self.page(state, lang)))]) if t]
                self.assertTrue(texts)
                for t in texts:
                    if lang == "en":
                        self.assertNotRegex(t, CJK, (state, t))
                    else:
                        self.assertLessEqual(set(re.findall(r"[A-Za-z][A-Za-z.]*", t)), ZH_OK_WORDS, (state, t))
                        self.assertRegex(t, r"[㐀-鿿]|^[\d\s.,:/$—×%]+$", (state, t))
        for lang, L in page_hud.STRINGS.items():
            for k, t in L.items():
                self.assertEqual(bool(CJK.search(t)), lang == "zh", (lang, k, t))
                if lang == "zh":
                    self.assertLessEqual(set(re.findall(r"[A-Za-z][A-Za-z.]*", re.sub(r"\{\w+\}", "", t))),
                                         ZH_OK_WORDS, t)

    def test_csp_one_script_order_and_nothing_fetched(self):
        html = self.page("done", "en")
        self.assertIn(f'<meta http-equiv="Content-Security-Policy" content="{CSP_0928}">', html)
        self.assertEqual(len(re.findall(r"<script>", html)), 1)
        js = re.search(r"<script>(.*)</script>", html, re.S).group(1)
        # the reload first, the page, then the HUD (it needs #app), then the scene (it asks the HUD for its insets)
        order = [js.index(page.REFRESH_JS), js.index(page.JS), js.index(page_hud.JS), js.index(page_sand.JS)]
        self.assertEqual(order, sorted(order))
        code = page_hud.JS + page_hud.CSS + hud_of(html)
        for bad in ("http:", "https:", "//cdn", "fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "import(",
                    "sendBeacon", "url(", "@import", "<img", "src=", "localStorage", "eval(", "new Function",
                    "innerHTML"):
            self.assertNotIn(bad, code, bad)

    def test_the_text_view_is_the_whole_plain_page(self):
        css = page_hud.CSS
        # the scene and the HUD's pieces hide; the page flows again with its own colours; the header, the checklist
        # and the progress come back (they live behind the dot and the instrument in the 3D view)
        self.assertIn("html.hud-text,html.hud-text body{height:auto;overflow:visible;background:var(--bg)}", css)
        self.assertIn("html.hud-text .hud>*{display:none}", css)
        self.assertIn("html.hud-on:not(.hud-text) #app section.ready", css)
        self.assertIn("html.hud-text .sand,html.hud-text .sand.on{display:none}", page_sand.CSS)
        self.assertIn("@media print{.hud,.sand,.sandtip{display:none!important}", css)   # printing: the text view
        self.assertIn("'#text'", page_hud.JS)                                           # an address that opens it
        # `page --text` (and the <noscript> copy) never depends on the HUD
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JEVSCREEN_HOME": tmp}):
            cfg = config.Config(home=Path(tmp)).ensure()
            data = page.read_page_data(S.write_state_page(cfg, "done", lang="zh"))
        plain = page.render_text(data)
        data["live"]["hud"] = {"usd": 9.0, "cap": 10.0, "since": None}
        self.assertEqual(page.render_text(data), plain)
        for k in ("tv", "tv_title", "back", "reset", "controls", "hide", "show"):
            self.assertNotIn(page_hud.STRINGS["zh"][k], plain)

    def test_no_scene_no_hud(self):
        self.assertEqual(page_hud.markup({"lang": "zh", "live": None}), "")
        self.assertEqual(page_hud.markup({"lang": "zh", "live": {"sand": None}}), "")
        self.assertIsNone(page_hud.status(None, "zh"))

    def test_controls_are_wired(self):
        js = page_sand.JS
        for want in ("addEventListener('wheel',onWheel,{passive:false})", "addEventListener('dblclick',onDbl)",
                     "addEventListener('contextmenu'", "addEventListener('keydown',onKey)",
                     "addEventListener('pointerdown',onDown)", "W.__jevSand={light:light,reset:resetView,pause:pause"):
            self.assertIn(want, js)
        self.assertIn("touch-action:none", page_sand.CSS)            # pinch and two-finger pan stay in the scene
        self.assertIn('tabindex="0"', page_sand.markup({"lang": "en", "live": {"sand": page_sand.facts(
            {"state": "running", "steps": []})}}))

    def test_clock_words(self):
        self.assertEqual([page_hud.clock_words(v) for v in (None, 0, 59.9, 271, 3727)],
                         ["—", "00:00", "00:59", "04:31", "1:02:07"])


# ------------------------------------------------------------------------------------------ the script (node)

DOM = r"""
class El{constructor(tag,attrs,kids){this.tagName=String(tag).toUpperCase();this.attrs=Object.assign({},attrs||{});this.childNodes=[];
 this.parentNode=null;this.ls={};const self=this;this.style={setProperty(k,v){this[k]=v;},getPropertyValue(k){return this[k]||'';}};
 this.hidden='hidden' in this.attrs;this.open=false;this._text='';this.scrollTop=0;this.scrolled=null;
 (kids||[]).forEach(k=>{if(typeof k==='string'){this._text+=k;}else this.appendChild(k);});}
 get id(){return this.attrs.id||'';}get className(){return this.attrs['class']||'';}set className(v){this.attrs['class']=String(v);}
 get textContent(){return this._text+this.childNodes.map(c=>c.textContent).join('');}set textContent(v){this._text=String(v);this.childNodes=[];}
 get offsetWidth(){return this.tagName==='MAIN'?420:100;}get offsetHeight(){return this.tagName==='MAIN'?480:(this.id==='hud-ph'?44:20);}
 get offsetTop(){return 300;}
 getAttribute(k){return k in this.attrs?String(this.attrs[k]):null;}setAttribute(k,v){this.attrs[k]=String(v);}removeAttribute(k){delete this.attrs[k];}
 appendChild(n){if(n.parentNode)n.parentNode.removeChild(n);n.parentNode=this;this.childNodes.push(n);return n;}
 insertBefore(n,r){if(n.parentNode)n.parentNode.removeChild(n);n.parentNode=this;const i=r?this.childNodes.indexOf(r):-1;if(i<0)this.childNodes.push(n);else this.childNodes.splice(i,0,n);return n;}
 removeChild(n){const i=this.childNodes.indexOf(n);if(i>=0)this.childNodes.splice(i,1);n.parentNode=null;return n;}
 get firstChild(){return this.childNodes[0]||null;}
 addEventListener(k,f){(this.ls[k]=this.ls[k]||[]).push(f);}
 fire(k,e){const ev=Object.assign({target:this,pointerType:'mouse',key:'',preventDefault(){},stopPropagation(){}},e||{});let n=this;
  while(n){(n.ls[k]||[]).forEach(f=>f(ev));if(['pointerenter','pointerleave','focus','blur'].indexOf(k)>=0)break;n=n.parentNode;}}
 all(){let o=[];for(const c of this.childNodes){o.push(c);o=o.concat(c.all());}return o;}
 is(one){const m=one.match(/^([a-z0-9]*)((?:[.#][\w-]+|\[[^\]]+\])*)$/i);if(!m)return false;if(m[1]&&this.tagName!==m[1].toUpperCase())return false;
  const parts=m[2].match(/[.#][\w-]+|\[[^\]]+\]/g)||[];for(const p of parts){if(p[0]==='.'){if((' '+this.className+' ').indexOf(' '+p.slice(1)+' ')<0)return false;}
   else if(p[0]==='#'){if(this.id!==p.slice(1))return false;}else{const a=p.slice(1,-1).split('=');const v=this.getAttribute(a[0]);if(v===null)return false;
    if(a.length>1&&v!==a[1].replace(/^"|"$/g,''))return false;}}return true;}
 matches(sel){const ps=sel.trim().split(/\s+/);if(!this.is(ps[ps.length-1]))return false;let n=this.parentNode,i=ps.length-2;
  while(i>=0&&n){if(n.is&&n.is(ps[i]))i--;n=n.parentNode;}return i<0;}
 querySelectorAll(sel){const ss=sel.split(',');return this.all().filter(n=>ss.some(s=>n.matches(s)));}
 querySelector(sel){return this.querySelectorAll(sel)[0]||null;}
 cloneNode(deep){const c=new El(this.tagName.toLowerCase(),this.attrs,[]);c._text=this._text;c.open=this.open;if(deep)this.childNodes.forEach(k=>c.appendChild(k.cloneNode(true)));return c;}
 getBoundingClientRect(){return {left:10,top:30,bottom:52,right:200,width:190,height:22};}
 focus(){focused=this.id;}scrollTo(o){this.scrolled=o;}}
let focused=null;
function build(t){return typeof t==='string'?t:new El(t[0],t[1],t[2].map(build));}
"""

HUDRUN = r"""
let now=1759000000000;Date.now=()=>now;
const DE=new El('html',{});const body=new El('body',{});DE.appendChild(body);
TREE.forEach(t=>{const n=build(t);if(typeof n!=='string')body.appendChild(n);});
// the page's #app as page.JS renders it (a header, the checklist, the progress, the results with rows and the top table)
const app=body.querySelector('#app');const main=new El('main',{},[
 ['header',{},[['h1',{},['idea']]]],
 ['section',{class:'box ready'},[['details',{class:'okline',id:'ready-list'},[['summary',{},['ready']],['div',{class:'chk chk-ok'},['python ok']]]]]],
 ['section',{class:'box progress'},[['div',{class:'alert'},['blocked words']],['div',{class:'pr s-run'},['bar']]]],
 ['section',{class:'results'},[['table',{class:'top10'},[['tr',{'data-rank':'1'},[['td',{},['#1']]]],['tr',{'data-rank':'2'},[['td',{},['#2']]]]]],
  ['details',{class:'row',id:'r1'},[['summary',{},[['span',{class:'name'},['one']]]]]],['details',{class:'row',id:'r2'},[['summary',{},[['span',{class:'name'},['two']]]]]]]]
].map(build));app.appendChild(main);
const store={};if(MEM)store['jevscreen-hud:'+KEYV]=JSON.stringify(MEM);
const timers=[];const sand={calls:[],light(r){this.calls.push('light:'+r);},pause(p){this.calls.push('pause:'+p);},relayout(){this.calls.push('relayout');},reset(){this.calls.push('reset');}};
global.document={documentElement:DE,body:body,getElementById:(id)=>id===''?null:(DE.all().find(n=>n.id===id)||null),
 createElement:(t)=>new El(t,{}),addEventListener:(k,f)=>{(DE.ls['d:'+k]=DE.ls['d:'+k]||[]).push(f);}};
const WL={},IV=[],hist=[];
global.window={requestAnimationFrame:()=>1,matchMedia:(q)=>({matches:(q.indexOf('max-width')>=0&&PHONE)||(q.indexOf('reduced')>=0&&false)}),
 sessionStorage:{getItem:(k)=>store[k]===undefined?null:store[k],setItem:(k,v)=>{store[k]=v;}},
 addEventListener(k,f){(WL[k]=WL[k]||[]).push(f);},location:{hash:HASH,pathname:'/p.html',search:''},scrollTo(){},
 history:{replaceState(a,b,u){hist.push(u);window.location.hash=u.indexOf('#')>=0?u.slice(u.indexOf('#')):'';}},
 innerWidth:PHONE?390:1280,innerHeight:800,__jevD:DATA};
global.setTimeout=(f)=>{timers.push(f);return timers.length;};global.clearTimeout=()=>{};global.setInterval=(f)=>{f();IV.push(f);return 1;};
function tickIv(){IV.forEach(f=>f());}
const canvas=body.querySelector('#sand canvas');canvas.getContext=()=>({});
function flush(withSand){if(withSand)window.__jevSand=sand;const t=timers.splice(0);t.forEach(f=>f());}
function cls(){return DE.className.split(/\s+/).filter(Boolean).sort().join(' ');}
function $(id){return document.getElementById(id);}
const out={};
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class TestScript(Case):
    def run_hud(self, state: str, lang: str, drive: str, mem: dict | None = None, phone: bool = False,
                hash_: str = "", stale: bool = False) -> dict:
        html = self.page(state, lang)
        data = json.loads(re.search(r'<script type="application/json" id="data">(.*?)</script>', html, re.S).group(1))
        if stale:
            data["live"]["stale"] = {"since": "2026-01-01T00:00:00Z", "after_s": 90, "text": "worker stopped"}
            data["live"]["phase"] = "failed"
        body = html[html.index("<body>") + 6:html.index('<script type="application/json"')]
        body = re.sub(r"<noscript>.*?</noscript>", "", body, flags=re.S)
        key = re.search(r'<div id="hud" class="hud" data-key="([^"]*)"', html).group(1)
        head = (f"const TREE={json.dumps(tree(body), ensure_ascii=False)};const MEM={json.dumps(mem)};"
                f"const KEYV={json.dumps(key)};const PHONE={json.dumps(phone)};const HASH={json.dumps(hash_)};"
                f"const DATA={json.dumps(data, ensure_ascii=False)};\n")
        js = head + DOM + HUDRUN + page_hud.JS + drive + "\nconsole.log(JSON.stringify(out));"
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "h.js"
            f.write_text(js, encoding="utf-8")
            r = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_done_opens_the_panel_and_the_text_view_toggles(self):
        o = self.run_hud("done", "zh", r"""
flush(true);out.cls=cls();out.pr=DE.style['--hud-pr'];out.tab=$('hud-tab').hidden;out.ph=main.firstChild.id;
out.inset=window.__jevHud.inset();
$('hud-tv').fire('click');out.tv={cls:cls(),pressed:$('hud-tv').getAttribute('aria-pressed'),sand:sand.calls.slice(),
 mem:JSON.parse(store['jevscreen-hud:'+KEYV]),inset:window.__jevHud.inset(),focus:focused,pick:window.__jevHud.pick(1)};
sand.calls=[];$('hud-back').fire('click');out.back={cls:cls(),sand:sand.calls.slice(),focus:focused};
$('hud-hide').fire('click');out.closed={cls:cls(),tab:$('hud-tab').hidden,pr:DE.style['--hud-pr'],po:JSON.parse(store['jevscreen-hud:'+KEYV]).po};
$('hud-tab').fire('click');out.reopened=cls();sand.calls=[];$('hud-reset').fire('click');out.reset=sand.calls.slice();""")
        self.assertEqual(o["cls"], "hud-on hud-open hud-panel")
        self.assertEqual((o["pr"], o["tab"], o["ph"]), ("420px", True, "hud-ph"))   # the panel's head leads it
        self.assertEqual(o["inset"], {"r": 420, "b": 0})
        tv = o["tv"]
        self.assertEqual(tv["cls"], "hud-on hud-open hud-panel hud-text")
        self.assertEqual((tv["pressed"], tv["mem"]["tv"], tv["focus"], tv["pick"]), ("true", 1, "hud-back", False))
        self.assertIn("pause:true", tv["sand"])                  # the scene stops behind the text view
        self.assertEqual(tv["inset"], {"r": 0, "b": 0})
        self.assertEqual(o["back"]["cls"], "hud-on hud-open hud-panel")
        self.assertIn("pause:false", o["back"]["sand"])
        self.assertEqual(o["back"]["focus"], "hud-tv")
        self.assertEqual(o["closed"], {"cls": "hud-on hud-panel", "tab": False, "pr": "0px", "po": 0})
        self.assertEqual(o["reopened"], "hud-on hud-open hud-panel")
        self.assertEqual(o["reset"], ["reset"])

    def test_rows_and_grains_light_each_other(self):
        o = self.run_hud("done", "en", r"""
flush(true);const r2=$('r2');r2.childNodes[0].childNodes[0].fire('pointerover');out.light=sand.calls.filter(c=>c.indexOf('light')===0);
main.querySelector('tr[data-rank="1"]').childNodes[0].fire('pointerover');out.light2=sand.calls.filter(c=>c.indexOf('light')===0);
window.__jevHud.hover(1);out.hov=[$('r1').className,main.querySelector('tr[data-rank="1"]').className,$('r2').className];
window.__jevHud.hover(null);out.unhov=[$('r1').className,main.querySelector('tr[data-rank="1"]').className];
$('hud-hide').fire('click');out.pick=window.__jevHud.pick(2);out.after={cls:cls(),open:r2.open,lit:r2.className,scrolled:main.scrolled};
out.none=window.__jevHud.pick(9);""")
        self.assertEqual(o["light"], ["light:2"])
        self.assertEqual(o["light2"], ["light:2", "light:1"])
        self.assertEqual(o["hov"], ["row sandhov", "sandhov", "row"])
        self.assertEqual(o["unhov"], ["row", ""])
        self.assertTrue(o["pick"])                               # a grain's click: the panel opens on its row
        self.assertEqual(o["after"]["cls"], "hud-on hud-open hud-panel")
        self.assertTrue(o["after"]["open"])
        self.assertIn("sandlit", o["after"]["lit"])
        self.assertEqual(o["after"]["scrolled"]["top"], 300 - 44 - 12)
        self.assertFalse(o["none"])

    def test_a_live_finish_and_a_panel_the_user_closed(self):
        o = self.run_hud("done", "zh", r"""
window.__jevHud.hold();out.held=cls();flush(true);window.__jevHud.finished();out.fin=cls();""")
        self.assertEqual((o["held"], o["fin"]), ("hud-on hud-panel", "hud-on hud-open hud-panel"))
        o = self.run_hud("done", "zh", r"""
window.__jevHud.hold();flush(true);window.__jevHud.finished();out.fin=cls();""", mem={"po": 0})
        self.assertEqual(o["fin"], "hud-on hud-panel")           # closed by the user: it stays closed

    def test_running_without_results_has_no_panel_and_the_time_ticks(self):
        o = self.run_hud("screening", "en", r"""
flush(true);out.cls=cls();out.inset=window.__jevHud.inset();out.tab=$('hud-tab').hidden;out.time=$('hud-time').textContent;""")
        self.assertEqual(o["cls"], "hud-on")
        self.assertEqual((o["inset"], o["tab"]), ({"r": 0, "b": 0}, True))
        self.assertRegex(o["time"], r"^\d+:\d\d(:\d\d)?$")       # ticking from the worker's start

    def test_the_status_dot_opens_the_checklist(self):
        o = self.run_hud("screening", "zh", r"""
flush(true);const st=$('hud-stat'),pop=$('hud-pop');st.fire('click');
out.open={hidden:pop.hidden,exp:st.getAttribute('aria-expanded'),kids:pop.childNodes.map(n=>n.className),
 ids:pop.all().filter(n=>n.id).length,okOpen:pop.querySelector('details.okline').open};
DE.ls['d:keydown'].forEach(f=>f({key:'Escape'}));out.esc={hidden:pop.hidden,exp:st.getAttribute('aria-expanded')};
st.fire('keydown',{key:'Enter'});out.key=pop.hidden;""")
        self.assertEqual(o["open"], {"hidden": False, "exp": "true", "kids": ["alert", "box ready"], "ids": 0,
                                     "okOpen": True})             # the page's own checklist, cloned, no duplicate id
        self.assertEqual(o["esc"], {"hidden": True, "exp": "false"})
        self.assertFalse(o["key"])

    def test_a_stopped_worker_turns_the_dot_red(self):
        o = self.run_hud("screening", "en", r"""
flush(true);out.stat=$('hud-stat').className;out.say=$('hud-stat').querySelector('.say').textContent;out.stage=$('hud-stage').textContent;""",
                         stale=True)
        self.assertEqual((o["stat"], o["say"], o["stage"]), ("hud-stat s-bad", "worker stopped", "Stopped"))

    def test_a_phone_sheet_and_the_text_address(self):
        o = self.run_hud("done", "en", r"""
flush(true);const t=$('hud-pt');out.peek=[cls(),window.__jevHud.inset(),t.getAttribute('aria-expanded'),t.getAttribute('tabindex')];
t.fire('click');out.full=[cls(),window.__jevHud.inset()];t.fire('click');out.back=cls();
$('hud-hide').fire('click');out.closed=[cls(),window.__jevHud.inset(),t.getAttribute('aria-expanded')];
t.fire('click');out.again=cls();
// a drag on the head: up opens it further, down closes it (the click that follows is swallowed)
const h=$('hud-ph');h.fire('pointerdown',{clientY:600,pointerId:3});h.fire('pointerup',{clientY:500,pointerId:3});h.fire('click');out.up=cls();
h.fire('pointerdown',{clientY:300,pointerId:3});h.fire('pointerup',{clientY:420,pointerId:3});out.down=cls();
h.fire('pointerdown',{clientY:300,pointerId:3});h.fire('pointerup',{clientY:420,pointerId:3});out.down2=cls();""", phone=True)
        self.assertEqual(o["peek"], ["hud-on hud-open hud-panel", {"r": 0, "b": 320}, "true", "0"])   # ~40 % of 800
        self.assertEqual(o["full"], ["hud-full hud-on hud-open hud-panel", {"r": 0, "b": 320}])      # the scene keeps its frame
        self.assertEqual(o["back"], "hud-on hud-open hud-panel")
        self.assertEqual(o["closed"], ["hud-on hud-panel", {"r": 0, "b": 44}, "false"])             # only its head
        self.assertEqual(o["again"], "hud-on hud-open hud-panel")                                    # a real button opens it
        self.assertEqual((o["up"], o["down"], o["down2"]), ("hud-full hud-on hud-open hud-panel", "hud-on hud-open hud-panel",
                                                            "hud-on hud-panel"))
        self.assertIn(":not(.hud-open):not(.hud-text) #app>main>:not(.hud-ph){visibility:hidden}", page_hud.CSS)
        d = self.run_hud("done", "en", "flush(true);out.t=$('hud-pt').getAttribute('tabindex');")
        self.assertEqual(d["t"], "-1")                                   # on a desktop the head is not a control
        o = self.run_hud("done", "en", r"""flush(true);out.cls=cls();
$('hud-back').fire('click');out.left=[cls(),window.location.hash,hist.slice()];
$('hud-tv').fire('click');out.again=[cls(),window.location.hash];
window.location.hash='';WL.hashchange.forEach(f=>f());out.change=cls();""", hash_="#text")
        self.assertEqual(o["cls"], "hud-on hud-open hud-panel hud-text")
        self.assertEqual(o["left"], ["hud-on hud-open hud-panel", "", ["/p.html"]])      # leaving drops #text ...
        self.assertEqual(o["again"], ["hud-on hud-open hud-panel hud-text", "#text"])   # ... entering writes it
        self.assertEqual(o["change"], "hud-on hud-open hud-panel")                      # the address leads both ways

    def test_the_refresh_waits_while_the_checklist_or_the_panel_is_read(self):
        o = self.run_hud("done", "zh", r"""
flush(true);window.__jevSandBusy=0;$('hud-stat').fire('pointerenter');tickIv();out.pop=window.__jevSandBusy===now;
now+=10000;window.__jevSandBusy=0;tickIv();out.still=window.__jevSandBusy===now;       // a still mouse: 15 s
now+=6000;window.__jevSandBusy=0;tickIv();out.later=window.__jevSandBusy;
window.__jevSave();out.saved=JSON.parse(store['jevscreen-hud:'+KEYV]).pop;
DE.ls['d:keydown'].forEach(f=>f({key:'Escape'}));window.__jevSandBusy=0;main.fire('pointerenter');tickIv();out.panel=window.__jevSandBusy===now;
main.fire('pointerleave');now+=1000;window.__jevSandBusy=0;tickIv();out.left=window.__jevSandBusy;""")
        self.assertEqual((o["pop"], o["still"], o["later"], o["saved"]), (True, True, 0, 1))
        self.assertEqual((o["panel"], o["left"]), (True, 0))
        again = self.run_hud("done", "zh", "flush(true);out.hidden=$('hud-pop').hidden;out.exp=$('hud-stat').getAttribute('aria-expanded');",
                             mem={"pop": 1})
        self.assertEqual((again["hidden"], again["exp"]), (False, "true"))     # the reload keeps it open

    def test_no_scene_brings_the_plain_page_back(self):
        o = self.run_hud("done", "zh", "out.before=cls();$('sand').className='sand on';flush(false);out.after=cls();out.box=$('sand').className;")
        self.assertEqual(o["before"], "hud-on hud-open hud-panel")
        self.assertEqual(o["box"], "sand")                              # no black box left over the plain page
        self.assertIn("html.hud-on .sand.on{display:block}", page_sand.CSS)
        self.assertNotIn("hud-on", o["after"])
        self.assertNotIn("hud-text", o["after"])


if __name__ == "__main__":
    unittest.main()

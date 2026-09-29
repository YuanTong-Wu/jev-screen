#!/usr/bin/env python3
"""Render the README hero: the one page's sand panning (jevscreen.page_sand) as a seamless animated loop.

    python3 tools/make_hero.py [--out docs/assets] [--lang en,zh] [--fps 12.5] [--keep-frames DIR]

Writes hero-<lang>.gif (800x400, 100 frames of 80 ms = an 8 s loop) and hero-<lang>.png (the frame with the final
list and its caption, for a social preview), then records each file's size and sha256 in <out>/MANIFEST.json (origin: own-output),
which lets tools/release_check.py accept these images despite its size rule (an earlier version's sha256 is kept as
previous_sha256, so the published history still passes). The GIF uses a fixed palette: warm greys for the drawing
plus amber shades pinned in, no dithering, so the one accent colour always survives; the build fails if a frame that
shows the final list has no amber pixel, or if a GIF is over 2.5 MB. Needs Pillow and ffmpeg; no network. Fonts come
from the system (Helvetica / Menlo / Hiragino Sans GB on macOS, DejaVu / Noto CJK on Linux).

The drawing is the page's, with the same 3D math and the same restraint (owner, 2026-09-28: the sieve stack dominant,
no label column, no leader lines, no names; a tiny corner wordmark): the three sieves on one vertical axis, none
hiding another, faintly there from the first frame; sand (every listed company) pours onto the first sieve from a
narrow column just above it; each sieve's progress runs around its rim as a thin stroke; the
market-cap sieve shakes once, its rim closes, its numbers roll up beside it and fade, and the passing sand rushes
down to the next; the AI reads the profiles there (a scan line), its sieve shakes; the annual-report check scans the
last sieve, which shakes and keeps a few amber grains joined by one thin line. The one-line caption fades in only
while the gold links up, then out. Then the scene dips to about a third (the wordmark stays) and grows back from
there: the loop has no black flash.

The page's breathing (owner, 2026-09-29), subtly: the loop starts on the flat grid (the three sieves on one plane,
seen a little more from above) and the stack expands along its axis while the sand pours in; near the end the stack
comes down onto that plane again while the scene dips, so the seam is the flat grid. A faint reading log in the
lower left types a few per-company answers (illustrative codes, as on the page: "699104 · 写明 · 读年报"), three or
four lines, the oldest fading at the top.

The numbers are illustrative (about how a real run narrows), not a real run's output; the README's alt text says so.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image, ImageChops, ImageDraw, ImageFont
except ImportError as e:            # pragma: no cover - a dev tool
    sys.exit(f"make_hero: needs Pillow ({e})")

W, H, SS = 800, 400, 2              # output size and supersampling
BG = (7, 7, 7)
INK = (242, 237, 224)               # warm white: the drawing
AMBER = (239, 159, 39)              # the final list only
GREYS = 22                          # levels of the INK ramp in the GIF palette
SECONDS = 8.0
MAX_GIF = 2_560_000
CAPTION = {"en": "20,000 companies, sifted layer by layer into a short list — each pick with its evidence",
           "zh": "两万家公司，一层层淘出一份短名单，每一家都附证据"}
WORDMARK = "JEV-SCREEN"
NUMS = [20000, 3100, 2150, 120, 5]                 # illustrative
# each sieve's numbers, shown beside its rim for a moment when its stage completes (as on the page): the market-cap
# pool of every listed company, what passed the first read of the profiles read, the list of what was checked
RIMS = [(NUMS[1], NUMS[0]), (NUMS[3], NUMS[2]), (NUMS[4], NUMS[3])]
FONTS = {
    "label_en": ["/System/Library/Fonts/HelveticaNeue.ttc", "/System/Library/Fonts/Helvetica.ttc",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "label_zh": ["/System/Library/Fonts/Hiragino Sans GB.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"],
    "mono": ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/SFNSMono.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"],
}

# ---- the page's world (jevscreen.page_sand.JS): keep these in step with it
NM, GR, RES = 1600, 12, 320
GAP = 1.95                          # SandCam.world() for a landscape frame: three sieves on one vertical axis
SC = [(0.0, GAP, 0.1), (0.0, 0.0, 0.1), (0.0, -GAP, 0.1)]
SA = [0.82, 0.79, 0.76]
STL = [0.07, 0.04, 0.01]
SRL = [-0.03, 0.02, -0.02]
TGT = (0.0, 0.0, 0.1)               # the resting look-at point (the middle sieve)
SVOF = {1: 0, 3: 1, 4: 2}           # the sieve that shakes when a stage is reached
SDONE = [1, 3, 4]                   # the stage at which each sieve's work is done
RC = [(-1, -1), (1, -1), (1, 1), (-1, 1)]          # a rim's corners in order: its progress runs around them
# the camera well above the top sieve, looking down at ~37 degrees: no sieve hides any of the one below it. The
# opening flies in from only half again as far as on the page, so the stack is legible from the first frame
YAW0, PIT0, D0, FLY, HOP = 0.4, 0.64, 3.2 * GAP, 1.15, 0.62
FLYD = 0.5 * D0
ORB = 0.06                          # the camera's orbit (yaw, radians) over one loop: the planes turn a little
# ---- the loop's script (seconds)
HOPS = [(0, 1, 1.6), (2, 3, 3.0), (3, 4, 4.25)]    # (from, to, start): three shakes
READ, CHECK = (2.2, 2.9), (3.6, 4.2)               # the first read's scan on sieve 2, the report check's on sieve 3
FADE = (7.6, 8.0)                                  # the scene dips to ~35% (the words stay), then the stars grow in
DIP = 0.35
GROW = 0.36                                        # the scene grows back from the seam's dip over ~4 frames
PILE = 0.54                                        # a resting grain's brightness (a quarter below the page's)
GOLD_T = HOPS[-1][2] + HOP                         # the final list is in place
FLD = 1.2                                          # how long a completed stage's numbers stay beside its rim
CAP_T = (GOLD_T, GOLD_T + 0.45, GOLD_T + 2.0, GOLD_T + 2.6)   # the caption: in, held, out (while the gold links up)
# the breathing: flat at the seam, expanding over BR_IN s, coming down again from BR_OUT (the deeper sieves a little
# later: BR_LAG s each); the camera rises only part of the way to the page's view from above (subtle)
BR_IN, BR_OUT, BR_LAG, BR_LIFT, BR_PIT, BR_NEAR = 1.3, 6.7, 0.08, 0.5, 1.42, 0.12
# the reading log: (start second, layer, code, label); eight lines a loop (the ones of the loop before are still in
# the column at its start: the seam is seamless); typed at LOG_CPS characters a second, LOG_N lines shown
LOG_LINES = [(0.5, "l2", "699231", "explicit", "annual_report"), (1.45, "l1", "399218", "unrelated", None),
             (2.3, "l1", "699104", "core", None), (2.95, "l1", "399342", "adjacent", None),
             (3.65, "l2", "699104", "explicit", "annual_report"), (4.35, "l2", "399342", "partial", "profile"),
             (5.3, "l2", "699187", "insufficient", "annual_report"), (6.3, "l2", "399276", "partial", "annual_report")]
LOG_CPS, LOG_N = 30, 4
LOG_WORDS = {"zh": {"l1": "读简介", "l2_annual_report": "读年报", "l2_profile": "复读简介", "core": "核心",
                    "adjacent": "相邻", "unrelated": "无关", "insufficient": "说不清", "explicit": "写明",
                    "partial": "相关", "contradicted": "不符"},
             "en": {"l1": "profile", "l2_annual_report": "report", "l2_profile": "reread",
                    "core": "core", "adjacent": "adjacent", "unrelated": "off-topic", "insufficient": "thin",
                    "explicit": "stated", "partial": "related", "contradicted": "no fit"}}


def font(kind: str, size: int):
    for p in FONTS[kind]:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def rnd(s: float) -> float:
    x = math.sin(s * 127.1 + 311.7) * 43758.5453
    return x - math.floor(x)


def gauss(s: float) -> float:
    a, b = max(1e-6, rnd(s)), rnd(s + 0.37)
    return math.sqrt(-2 * math.log(a)) * math.cos(6.2832 * b)


def cl(v: float, a: float, b: float) -> float:
    return a if v < a else (b if v > b else v)


def ease(u: float) -> float:
    u = cl(u, 0.0, 1.0)
    return 4 * u ** 3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2


def smooth(u: float) -> float:
    u = cl(u, 0.0, 1.0)
    return u * u * (3 - 2 * u)


def flat_at(t: float, k: int) -> float:
    """How flat sieve k is (k = 3: the camera's rise) at second t of the loop: 1 at the seam, 0 in the 3D stack."""
    lag = BR_LAG * (k if k < 3 else 0)
    if t < BR_IN + lag:
        return 1 - smooth((t - lag) / BR_IN)
    return smooth((t - BR_OUT - lag) / (SECONDS - BR_OUT - lag))


def log_text(line: tuple, lang: str) -> str:
    _, layer, code, label, doc = line
    W_ = LOG_WORDS[lang]
    return f"{code} · {W_[label]} · {W_['l1'] if layer == 'l1' else W_['l2_' + doc]}"


def eout(u: float) -> float:
    u = cl(u, 0.0, 1.0)
    return 1 - (1 - u) ** 3


def ink(a: float) -> tuple[int, int, int, int]:
    return INK + (int(round(255 * cl(a, 0.0, 1.0))),)


def amber(a: float) -> tuple[int, int, int, int]:
    return AMBER + (int(round(255 * cl(a, 0.0, 1.0))),)


class Scene:
    def __init__(self, lang: str):
        self.lang = lang
        self.gu = [[0.0] * NM for _ in range(3)]
        self.gv = [[0.0] * NM for _ in range(3)]
        self.gh = [[0.0] * NM for _ in range(3)]
        for i in range(NM):
            for k in range(3):
                u = cl(gauss(i * 7.13 + k * 101.7) * 0.37, -0.93, 0.93)
                v = cl(gauss(i * 3.71 + k * 57.3 + 0.5) * 0.37, -0.93, 0.93)
                self.gu[k][i], self.gv[k][i] = u, v
                self.gh[k][i] = 0.012 + 0.075 * math.exp(-(u * u + v * v) / 0.28) * rnd(i * 3.3 + k)
        self.gd = [rnd(i * 5.7) * 0.34 for i in range(NM)]
        self.gz = [0.6 + 0.8 * rnd(i * 9.1) for i in range(NM)]
        n1 = round(cl(NM * math.sqrt(NUMS[1] / NUMS[0]), 60, NM))
        self.N = [NM, n1, round(cl(n1 * math.sqrt(NUMS[3] / NUMS[1]), 16, n1))]
        self.NG = NUMS[4]
        # the amber grains' places on the last sieve (as on the page): a loose chain from its far side to its near side
        self.glu = [0.12 * math.sin(j * 2.3 + 0.6) for j in range(self.NG)]
        self.glv = [0.55 - 1.1 * j / (self.NG - 1) for j in range(self.NG)]
        self.G = [dict(x=0.0, y=0.0, z=0.0, ax=1.0, ay=0.0, az=0.0, bx=0.0, by=0.0, bz=1.0, a=SA[k]) for k in range(3)]
        self.HL, self.FOC = [0.12, 0.12, 0.12], 0.0
        self.Fc, self.ox, self.oy = 1.0, 0.0, 0.0
        self.fit()
        # the star sea in the camera's first view, and far faint stars
        # the pour: a narrow column of grains falling in from above the top sieve (not a screen-filling starfield)
        self.stars = []
        for i in range(NM):
            u, v = cl(gauss(i * 7.13 + 101.7) * 0.37, -0.93, 0.93), cl(gauss(i * 3.71 + 57.8) * 0.37, -0.93, 0.93)
            h = 0.15 + rnd(i * 1.9 + 4) * 0.55
            self.stars.append((SC[0][0] + SA[0] * u * 0.55, SC[0][1] + h, SC[0][2] + SA[0] * v * 0.55))
        self.cam(YAW0, PIT0, D0, *TGT)
        self.far = []
        for j in range(150):
            d = D0 + 9 + rnd(j * 6.1) * 16
            self.far.append(self.world(d, (rnd(j * 8.3) * 2 - 1) * d * 0.5, (rnd(j * 9.7) * 2 - 1) * d * 0.3)
                            + ((0.07 + 0.16 * rnd(j * 3.7)),))
        self.f_num = font("mono", 10 * SS)
        self.f_log = font("mono", 9 * SS)
        self.f_logc = font("label_zh", 9 * SS) if lang == "zh" else self.f_log
        self.f_cap = font("label_" + lang, 12 * SS)
        self.chrome_img, self.caption_img = self.chrome()

    # -- camera and projection (as the page's prj)
    def cam(self, yaw, pit, dist, tx, ty, tz):
        cp, sp, cy, sy = math.cos(pit), math.sin(pit), math.cos(yaw), math.sin(yaw)
        self.f = (-sy * cp, -sp, cy * cp)
        self.C = (tx - self.f[0] * dist, ty - self.f[1] * dist, tz - self.f[2] * dist)
        self.r = (cy, 0.0, sy)
        fx, fy, fz = self.f
        rx, ry, rz = self.r
        self.u = (fy * rz - fz * ry, fz * rx - fx * rz, fx * ry - fy * rx)
        self.dist = dist

    def world(self, zc, px, py):
        return tuple(self.C[a] + self.f[a] * zc + self.r[a] * px + self.u[a] * py for a in range(3))

    def prj(self, x, y, z):
        dx, dy, dz = x - self.C[0], y - self.C[1], z - self.C[2]
        zc = dx * self.f[0] + dy * self.f[1] + dz * self.f[2]
        if zc < 0.15:
            return None
        s = self.Fc / zc
        xc = dx * self.r[0] + dy * self.r[1] + dz * self.r[2]
        yc = dx * self.u[0] + dy * self.u[1] + dz * self.u[2]
        return self.ox + xc * s, self.oy - yc * s, zc, s, xc, yc

    def fog(self, z):
        return cl(0.5 - (z - self.dist) / (1.9 * GAP), 0.0, 1.0)

    def camat(self, t, rest=False):
        yaw = YAW0 + (0 if rest else ORB * math.sin(t * 6.2832 / SECONDS))
        pit = PIT0 + (0 if rest else 0.02 * math.sin(t * 6.2832 / SECONDS * 2))
        tp = 0.0 if rest else BR_LIFT * flat_at(t, 3)
        pit += (BR_PIT - pit) * tp
        dist = (D0 + (0 if rest else self.flyd(t))) * (1 - BR_NEAR * tp / BR_LIFT)
        self.cam(yaw, pit, dist, TGT[0], TGT[1] + (0 if rest else self.FOC) * (1 - tp), TGT[2])

    def fit(self):
        """Fc and the offset that frame the three sieves wherever the orbit takes the camera."""
        self.Fc, self.ox, self.oy = 1.0, 0.0, 0.0
        self.sieves(0, True)
        xs, ys = [], []
        for yo in (-ORB, 0.0, ORB):
            self.cam(YAW0 + yo, PIT0, D0, *TGT)
            for k in range(3):
                for c in range(5):
                    p = self.spt(k, 1 if c & 1 else -1, 1 if c & 2 else -1, 0) if c < 4 else self.spt(k, 0, 0, 0.3)
                    q = self.prj(*p)
                    if q:
                        xs.append(q[0])
                        ys.append(q[1])
        self.camat(0, True)
        # the stack dominates: ~80 % of the picture's height (the limiting side), centred, clear of the caption's band
        a0, a1, b0, b1 = 0.1 * W * SS, 0.9 * W * SS, 0.06 * H * SS, 0.87 * H * SS
        self.Fc = min((a1 - a0) / (max(xs) - min(xs)), (b1 - b0) / (max(ys) - min(ys)))
        self.ox = (a0 + a1) / 2 - self.Fc * (min(xs) + max(xs)) / 2
        self.oy = (b0 + b1) / 2 - self.Fc * (min(ys) + max(ys)) / 2

    # -- the script
    @staticmethod
    def flyd(t):
        e = t / FLY
        return 0.0 if e >= 1 else FLYD * (1 - e) ** 3

    @staticmethod
    def hop(t):
        for a, b, s in HOPS:
            if s <= t < s + HOP:
                return a, b, (t - s) / HOP
        return None

    @staticmethod
    def reached(t):
        st = 0
        for a, b, s in HOPS:
            if t >= s:
                st = a
            if t >= s + HOP:
                st = b
        if READ[0] <= t < HOPS[1][2]:
            st = 2                                      # the profiles are in: the first read runs
        return st

    @staticmethod
    def active(t):
        if FLY <= t < HOPS[0][2]:
            return 0, None
        if READ[0] <= t < READ[1] + 0.1:
            return 2, cl((t - READ[0]) / (READ[1] - READ[0]), 0, 1)
        if CHECK[0] <= t < CHECK[1] + 0.05:
            return 3, cl((t - CHECK[0]) / (CHECK[1] - CHECK[0]), 0, 1)
        return None, None

    # -- sieves
    def sieves(self, t, rest=False):
        hp = None if rest else self.hop(t)
        for k in range(3):
            g, dep = self.G[k], 1 - 0.3 * k
            br = 0 if rest else 1
            w = 6.2832 / SECONDS
            tl = STL[k] + br * 0.014 * dep * math.sin(t * w * 2 + k * 1.3)
            rl = SRL[k] + br * 0.01 * dep * math.sin(t * w + k * 2.1)
            sh = 0.0
            if hp and SVOF.get(hp[1]) == k and hp[2] < 0.34:
                q = hp[2] / 0.34
                sh = 0.075 * math.sin(q * 9.4248) * (1 - q)
            fk = 0.0 if rest else flat_at(t, k)          # the breathing: down onto the middle sieve's plane
            tl, rl = tl * (1 - fk), rl * (1 - fk)
            g.update(ax=math.cos(rl), ay=math.sin(rl), az=0.0, bx=0.0, by=math.sin(tl), bz=math.cos(tl))
            g.update(x=SC[k][0] + g["ax"] * sh, y=(SC[k][1] + br * 0.022 * dep * math.sin(t * w * 3 + k * 1.9)) *
                     (1 - fk) + SC[1][1] * fk + g["ay"] * sh, z=SC[k][2], a=SA[k] + (SA[1] - SA[k]) * fk)

    def spt(self, k, u, v, hg):
        g = self.G[k]
        a = g["a"]
        return (g["x"] + a * (u * g["ax"] + v * g["bx"]), g["y"] + a * (u * g["ay"] + v * g["by"]) + hg,
                g["z"] + a * (u * g["az"] + v * g["bz"]))

    def rest(self, k, i):
        return self.spt(k, self.gu[k][i], self.gv[k][i], self.gh[k][i])

    # -- grains: (layer, world point, brightness, trail from)
    def cnt(self, st):
        return self.NG if st >= 4 else (self.N[2] if st >= 3 else (self.N[1] if st >= 1 else self.N[0]))

    @staticmethod
    def lev(st):
        return 2 if st >= 3 else (1 if st >= 1 else 0)

    def calm(self, i, st, t, act, frac):
        N = self.N
        if i < self.cnt(st):
            if st >= 4:
                return None
            k = self.lev(st)
            p, ga, lay = self.rest(k, i), PILE, k
            if act == 0 and st == 0 and i >= NM - 110:
                q = (t * 0.5 + self.gd[i] * 3) % 1
                if q < 0.72:
                    e = q / 0.72
                    p = (p[0], p[1] + 1.3 * (1 - e * e), p[2])
                    ga, lay = 0.3 + 0.5 * e, 0
            elif act in (2, 3) and k == act - 1:
                ga = PILE if self.gv[k][i] < -1 + 2 * frac else 0.36       # read: as bright as calm, never more
            return lay, p, ga, None
        if i >= N[1]:
            return (0, self.rest(0, i), 0.2, None) if i < N[1] + RES else None
        if i >= N[2]:
            return (1, self.rest(1, i), 0.2, None) if i < N[2] + RES else None
        return None

    def fall(self, i, ka, kb, u, lay):
        """A grain dropping from sieve ka to sieve kb (kb < 0: through the last sieve into the dark): it falls first
        and glides sideways only a little (mostly straight down through the mesh), with a short trail."""
        q = cl((u - self.gd[i]) / 0.6, 0, 1)
        A = self.rest(ka, i)
        B = self.spt(2, self.gu[2][i], self.gv[2][i], -1.5) if kb < 0 else self.rest(kb, i)

        def at(q):
            e, m = q * q, q ** 2.3
            return A[0] + (B[0] - A[0]) * m, A[1] + (B[1] - A[1]) * e, A[2] + (B[2] - A[2]) * m
        p = at(q)
        tr = at(max(0.0, q - 0.1)) if 0 < q < 1 else None
        return (lay if q > 0 else ka), p, (0.8 * (1 - q) if kb < 0 else 0.8), tr

    def moving(self, i, hp, t):
        N, (_, b, u) = self.N, hp
        if b == 1:
            if i < N[1]:
                return self.fall(i, 0, 1, u, 1)
            return 0, self.rest(0, i), (PILE - 0.4 * ease(u) if i < N[1] + RES else PILE * (1 - ease(u))), None
        if b == 3:
            if i < N[2]:
                return self.fall(i, 1, 2, u, 2)
            if i < N[1]:
                return 1, self.rest(1, i), (PILE - 0.4 * ease(u) if i < N[2] + RES else PILE * (1 - ease(u))), None
            return self.calm(i, 3, t, None, None)
        if b == 4:
            if i < self.NG:
                return None
            if i < N[2]:
                return self.fall(i, 2, -1, u, 3)
            return self.calm(i, 4, t, None, None)
        return self.calm(i, b, t, None, None)

    # -- drawing
    def rims(self, t, st, hp, act, frac):
        """Each sieve's rim this frame: (share done, unknown amount, brightening as it completes), as on the page."""
        out = []
        for k in range(3):
            if st >= SDONE[k]:
                out.append((1.0, False, 0.0))
            elif hp and SVOF.get(hp[1]) == k:
                out.append((eout(hp[2] / 0.5), False, 1 - hp[2]))
            elif act is None or hp:
                out.append((0.0, False, 0.0))
            elif k == 0 and act == 0:
                out.append((0.0, frac is None, 0.0) if frac is None else (frac, False, 0.0))
            elif k == 1 and act == 2:
                out.append((0.15 + 0.85 * frac, False, 0.0))
            elif k == 2 and act == 3:
                out.append((frac, False, 0.0))
            else:
                out.append((0.0, False, 0.0))
        return out

    def rim_pts(self, k, s0, s1):
        pts = []

        def at(s):
            e, f = int(math.floor(s)), s - math.floor(s)
            c0, c1 = RC[e % 4], RC[(e + 1) % 4]
            p = self.prj(*self.spt(k, c0[0] + (c1[0] - c0[0]) * f, c0[1] + (c1[1] - c0[1]) * f, 0.002))
            if p:
                pts.append(p[:2])
        at(s0)
        c = math.floor(s0) + 1
        while c < s1:
            at(c)
            c += 1
        at(s1)
        return pts

    def flashes(self, d, t):
        """A completed stage's numbers under its sieve's nearest corner (as on the page: numbers only, the small
        letter-spaced monospace at low opacity), beside it where the caption's band would be: rolling up, held,
        gone after FLD seconds."""
        for (a, b, s), k in zip(HOPS, (0, 1, 2)):
            e = t - (s + HOP * 0.35)
            if e < 0 or e > FLD:
                continue
            al = min(1.0, e / 0.25) * min(1.0, (FLD - e) / 0.5)
            n, of = RIMS[k]
            text = f"{round(n * eout(e / 0.45)):,} / {of:,}"
            cs = [self.prj(*self.spt(k, 1 if c & 1 else -1, 1 if c & 2 else -1, 0)) for c in range(4)]
            cs = [c for c in cs if c]
            if not cs:
                continue
            near = max(cs, key=lambda c: c[1])
            tw_ = text_width(d, text, self.f_num, 0.14)
            x, y = near[0] - tw_ / 2, near[1] + 20 * SS
            if y > (H - 44) * SS:           # the caption's band: beside the sieve's right corner instead
                right = max(cs, key=lambda c: c[0])
                x, y = right[0] + 14 * SS, right[1] + 4 * SS
            x = cl(x, 16 * SS, (W - 16) * SS - tw_)
            d.rectangle([x - 4 * SS, y - 10 * SS, x + tw_ + 4 * SS, y + 4 * SS], fill=BG + (int(140 * al),))
            spaced(d, (x, y), text, self.f_num, ink(0.6 * al), 0.14, baseline=True)

    def readlog(self, img, t):
        """The reading log: the last LOG_N lines typed so far (the loop before's still there at its start), a
        constant LOG_CPS, the oldest line fading at the top; small monospace (CJK in the page's sans), faint."""
        shown = []
        for rep_ in (-1, 0):
            for ln in LOG_LINES:
                t0 = ln[0] + rep_ * SECONDS
                if t0 <= t:
                    s = log_text(ln, self.lang)
                    shown.append(s[:max(0, min(len(s), int((t - t0) * LOG_CPS)))])
        shown = [s for s in shown if s][-LOG_N:]
        lh, x0, yb = 13 * SS, 18 * SS, (H - 52) * SS
        # drawn as a mask (Pillow's text ignores an RGBA fill's alpha), then the ink through it
        mask = Image.new("L", img.size, 0)
        md = ImageDraw.Draw(mask)
        for j, s in enumerate(shown):
            y = yb - (len(shown) - 1 - j) * lh
            a = 0.36 if j == len(shown) - 1 else 0.28 * (0.45 + 0.55 * (j + 1) / len(shown))
            if j == 0 and len(shown) == LOG_N:
                a *= 0.5                         # the top line fades out
            spaced(md, (x0, y), s, self.f_logc, int(round(255 * a)), 0.0, latin=self.f_log, baseline=True)
        if shown:
            img.paste(INK, mask=mask)

    def free(self, x, y):
        """False inside the wordmark's box: no star is drawn over it."""
        for x0, y0, x1, y1 in self.boxes:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return False
        return True

    def quads(self):
        out = []
        for k in range(3):
            c = [self.prj(*self.spt(k, 1 if j in (1, 2) else -1, 1 if j >= 2 else -1, 0)) for j in range(4)]
            out.append(None if any(p is None for p in c) else [(p[0], p[1]) for p in c])
        return out

    def frame(self, t: float, dt: float) -> Image.Image:
        img = Image.new("RGB", (W * SS, H * SS), BG)
        d = ImageDraw.Draw(img, "RGBA")
        st, hp = self.reached(t), self.hop(t)
        act, frac = self.active(t)
        fl = t / FLY if t < FLY else -1.0
        grow = 1.0 if t >= GROW else DIP + (1 - DIP) * (lambda u: u * u * (3 - 2 * u))(t / GROW)   # the rims show from frame 0
        work = self.lev(st)
        for k in range(3):
            g = 0.5 if k == work and st < 4 else (0.35 if st >= 4 and k == 2 else 0.12)
            if hp and SVOF.get(hp[1]) == k:
                g = 1.0
            self.HL[k] += (g - self.HL[k]) * (1 - math.exp(-(14 if hp else 9) * dt))
        self.FOC += ((-0.06 * work if st < 4 else 0.0) - self.FOC) * (1 - math.exp(-4 * dt))
        self.camat(t)
        self.sieves(t)
        for x, y, z, a in self.far:
            p = self.prj(x, y, z)
            if p and self.free(p[0], p[1]):
                d.rectangle([p[0], p[1], p[0] + SS - 1, p[1] + SS - 1], fill=ink(a))
        layers: list[list] = [[], [], [], []]
        trails: list[list] = [[], [], [], []]
        m = 1.0 if fl < 0 else ease((fl - 0.42) / 0.58)
        for i in range(NM):
            g = self.moving(i, hp, t) if hp else self.calm(i, st, t, act, frac)
            if fl >= 0:
                sx = self.stars[i]
                if g is None:
                    lay, p, ga, tr = 0, sx, 0.0, None
                else:
                    lay, p, ga, tr = g
                # the pour falls straight down into its place (height first), sparse and faint until it lands
                p = (sx[0] + (p[0] - sx[0]) * m, sx[1] + (p[1] - sx[1]) * (m ** 0.7), sx[2] + (p[2] - sx[2]) * m)
                if g is None or i % 2:
                    ga = ga * m
                ga = 0.3 * (1 - m) + ga * m
                if m < 0.6:
                    lay = 0
                q = self.prj(*p)
                if q is None or ga <= 0.01 or (m < 0.6 and not self.free(q[0], q[1])):
                    continue
                layers[lay].append((q, ga, self.gz[i]))
                continue
            if g is None:
                continue
            lay, p, ga, tr = g
            q = self.prj(*p)
            if q is None:
                continue
            layers[lay].append((q, ga, self.gz[i]))
            if tr:
                q0 = self.prj(*tr)
                if q0:
                    trails[lay].append(clip_trail(q0[0], q0[1], q[0], q[1], 10 * SS))

        def flush(lay):
            for q, ga, sz in layers[lay]:
                # near grains larger and brighter, far ones smaller and dimmer (fog), whatever stage is active
                a = ga * (0.3 + 0.7 * self.fog(q[2]))
                r = cl(sz * q[3] / SS * 0.019, 0.8, 2.4) * SS / 2
                d.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=ink(a))
            for x0, y0, x1, y1 in trails[lay]:
                d.line([(x0, y0), (x1, y1)], fill=ink(0.3), width=SS)

        quads = self.quads()
        rims = self.rims(t, st, hp, act, frac)
        flush(3)
        img = self.plane(img, d, 2, self.HL[2], frac if (act == 3 and not hp) else None, quads, rims[2], t)
        d = ImageDraw.Draw(img, "RGBA")
        flush(2)
        self.gold(d, t, st, hp)
        img = self.plane(img, d, 1, self.HL[1], frac if (act == 2 and not hp) else None, quads, rims[1], t)
        d = ImageDraw.Draw(img, "RGBA")
        flush(1)
        img = self.plane(img, d, 0, self.HL[0], None, quads, rims[0], t)
        d = ImageDraw.Draw(img, "RGBA")
        flush(0)
        self.flashes(d, t)
        self.readlog(img, t)
        out = img.resize((W, H), Image.LANCZOS)
        # the loop's seam: only the scene dips (to ~35%), the wordmark stays; then the star field grows in
        fade = grow if t < FADE[0] else 1 - (1 - DIP) * ease((t - FADE[0]) / (FADE[1] - FADE[0]))
        if fade < 1:
            out = Image.blend(Image.new("RGB", out.size, BG), out, max(0.0, fade))
        # the caption: only while the gold links up, then gone
        c0, c1, c2, c3 = CAP_T
        ca = ease((t - c0) / (c1 - c0)) if t < c2 else 1 - ease((t - c2) / (c3 - c2))
        if ca > 0:
            out = ImageChops.lighter(out, Image.blend(Image.new("RGB", out.size, BG), self.caption_img, min(1.0, ca)))
        return ImageChops.lighter(out, self.chrome_img)

    def plane(self, img, d, k, hl, scan, quads, rim, t):
        """A sieve: a dark translucent plane that veils what lies below it, a fine mesh and its rim (a faint track,
        its progress as a brighter stroke around it, the whole rim once done). Where a plane above covers it, its
        mesh and its rim are both hidden (one rule, so an overlap reads as one plane in front)."""
        quad = quads[k]
        if quad is None:
            return img
        d.polygon(quad, fill=BG + (int(158 * (1 - 0.8 * flat_at(t, k))),))
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        A = 0.14 + 0.2 * hl
        for dd in range(2):
            for j in range(GR + 1):
                s = -1 + 2 * j / GR
                for q in range(3):
                    v0 = -1 + 2 * q / 3
                    v1 = v0 + 2 / 3
                    p0 = self.prj(*(self.spt(k, v0, s, 0) if dd else self.spt(k, s, v0, 0)))
                    p1 = self.prj(*(self.spt(k, v1, s, 0) if dd else self.spt(k, s, v1, 0)))
                    if p0 and p1:
                        b = min(2, int(self.fog((p0[2] + p1[2]) / 2) * 3))
                        ld.line([p0[:2], p1[:2]], fill=ink(A * (0.35 + 0.325 * b)), width=SS if b == 2 else 1)
        p, unknown, bright = rim
        done = p >= 1
        # depth: each edge dimmer the further it lies, a hairline under the rim for thickness; a done stage keeps
        # its whole rim, brighter than a waiting one's faint track
        ez = []
        for e_ in range(4):
            c0, c1 = RC[e_], RC[(e_ + 1) % 4]
            q_ = self.prj(*self.spt(k, (c0[0] + c1[0]) / 2, (c0[1] + c1[1]) / 2, 0))
            ez.append(q_[2] if q_ else 0.0)
        z0, z1 = min(ez), max(ez)
        ez = [1 - 0.62 * ((z - z0) / (z1 - z0) if z1 > z0 else 0) for z in ez]
        track = (0.56 + 0.25 * bright) if done else min(0.24, 0.1 + 0.05 * hl)
        for e_ in range(4):
            pts = self.rim_pts(k, e_, e_ + 1)
            if len(pts) > 1:
                ld.line(pts, fill=ink(track * ez[e_]), width=SS + (1 if done else 0))
            hp_ = [self.prj(*self.spt(k, *RC[c % 4], -0.035 * SA[k])) for c in (e_, e_ + 1)]
            if all(hp_):
                ld.line([hp_[0][:2], hp_[1][:2]], fill=ink(track * 0.4 * ez[e_]), width=1)
        if (p > 0.001 or unknown) and not done:
            s0 = (t * 0.35) % 4 if unknown else 0.0
            s1 = s0 + 0.55 if unknown else 4 * p
            pts = self.rim_pts(k, s0, s1)
            if len(pts) > 1:
                ld.line(pts, fill=ink(min(0.95, 0.78 + 0.25 * bright)), width=2 * SS, joint="curve")
            if not done and not unknown and pts:
                x, y = pts[-1]
                ld.ellipse([x - 1.9 * SS, y - 1.9 * SS, x + 1.9 * SS, y + 1.9 * SS], fill=ink(0.95))
        if scan is not None:
            vs = -1 + 2 * scan
            pts = [self.prj(*self.spt(k, -1 + e / 4, vs, 0.004)) for e in range(9)]
            ld.line([p[:2] for p in pts if p], fill=ink(0.35), width=SS)
        above = [quads[j] for j in range(k) if quads[j]]
        if above:
            mask = Image.new("L", img.size, 0)
            md = ImageDraw.Draw(mask)
            for qd in above:
                md.polygon(qd, fill=255)
            layer.paste((0, 0, 0, 0), mask=mask)
        img.paste(layer, (0, 0), layer)
        return img

    def gold(self, d, t, st, hp):
        gh = hp is not None and hp[1] == 4
        if st < 4 and not gh:
            return []
        pts = []
        for j in range(self.NG):
            q = eout((hp[2] - 0.2) / 0.8) if gh else 1.0
            a = self.rest(2, j)
            b = self.spt(2, self.glu[j], self.glv[j], 0.03)
            p = self.prj(*(a[x] + (b[x] - a[x]) * q for x in range(3)))
            if p:
                pts.append((p[0], p[1], cl(0.03 * p[3] / SS, 1.4, 3.2) * SS, 0.35 + 0.65 * q))
        lk = eout((t - GOLD_T) / 0.55)
        if len(pts) > 1 and lk > 0:
            tot = (len(pts) - 1) * lk
            line = [pts[0][:2]]
            for s in range(1, len(pts)):
                if s <= tot:
                    line.append(pts[s][:2])
                else:
                    f = tot - (s - 1)
                    if f > 0:
                        line.append((pts[s - 1][0] + (pts[s][0] - pts[s - 1][0]) * f,
                                     pts[s - 1][1] + (pts[s][1] - pts[s - 1][1]) * f))
                    break
            d.line(line, fill=amber(0.8), width=SS)
        for x, y, r, a in pts:
            d.ellipse([x - r, y - r, x + r, y + r], fill=amber(a))
        return pts

    def chrome(self):
        """The tiny corner wordmark (it stays across the loop's seam; its box is kept free of stars) and the one-line
        caption (drawn once, faded in and out by frame())."""
        img = Image.new("RGB", (W * SS, H * SS), BG)
        d = ImageDraw.Draw(img, "RGBA")
        fw = font("mono", 8 * SS)
        spaced(d, (18 * SS, 16 * SS), WORDMARK, fw, ink(0.3), 0.3)
        ww = text_width(d, WORDMARK, fw, 0.3)
        pad = 5 * SS
        self.boxes = [(18 * SS - pad, 12 * SS, 18 * SS + ww + pad, 30 * SS)]
        cap = Image.new("RGB", (W * SS, H * SS), BG)
        cd = ImageDraw.Draw(cap, "RGBA")
        latin = font("label_en", 12 * SS) if self.lang == "zh" else None
        cem = 0.01 if self.lang == "en" else 0.0
        cw = text_width(cd, CAPTION[self.lang], self.f_cap, cem) if self.lang == "en" else \
            sum(cd.textlength(ch, font=latin if (ord(ch) < 0x2000 and ch != " ") else self.f_cap) for ch in CAPTION["zh"])
        cx = (W * SS - cw) / 2
        cd.rectangle([cx - 10 * SS, (H - 36) * SS, cx + cw + 10 * SS, (H - 12) * SS], fill=BG + (230,))
        spaced(cd, (cx, (H - 30) * SS), CAPTION[self.lang], self.f_cap, ink(0.7), cem, latin=latin)
        return img.resize((W, H), Image.LANCZOS), cap.resize((W, H), Image.LANCZOS)


def clip_trail(x0, y0, x1, y1, most):
    """A trail from (x0, y0) to the grain at (x1, y1), at most `most` long (short trails, never long arcs)."""
    dx, dy = x0 - x1, y0 - y1
    n = math.hypot(dx, dy)
    if n > most > 0:
        x0, y0 = x1 + dx * most / n, y1 + dy * most / n
    return x0, y0, x1, y1


def text_width(d: ImageDraw.ImageDraw, text: str, f, em: float) -> float:
    if not em:
        return d.textlength(text, font=f)
    size = getattr(f, "size", 10)
    return sum(d.textlength(ch, font=f) + em * size for ch in text)


def spaced(d: ImageDraw.ImageDraw, xy, text: str, f, fill, em: float, latin=None, baseline: bool = False) -> None:
    """Letter-spaced text (em: extra space per character as a share of the font size). Unspaced text is drawn in
    runs, Latin letters and digits in `latin` when given (a CJK font's wide digits split "10" into "1 0")."""
    x, y = xy
    asc = f.getmetrics()[0] if hasattr(f, "getmetrics") else 0
    base = y if baseline else y + asc
    if not em:
        runs: list[tuple[bool, str]] = []
        for ch in text:
            lat = latin is not None and (ord(ch) < 0x2000 or ch in "≥≤") and ch != " "
            if runs and runs[-1][0] == lat:
                runs[-1] = (lat, runs[-1][1] + ch)
            else:
                runs.append((lat, ch))
        for lat, run in runs:
            ff = latin if lat else f
            d.text((x, base), run, font=ff, fill=fill, anchor="ls")
            x += d.textlength(run, font=ff)
        return
    size = getattr(f, "size", 10)
    for ch in text:
        d.text((x, base), ch, font=f, fill=fill, anchor="ls")
        x += d.textlength(ch, font=f) + em * size


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def mixc(col, a):
    a = cl(a, 0.0, 1.0)
    return tuple(int(round(b + (c - b) * a)) for b, c in zip(BG, col))


def palette_colors() -> list[tuple[int, int, int]]:
    """The GIF's fixed palette: the warm-white ramp from the ground up (denser in the faint range, where the thin
    lines live) and amber shades from faint to full. Everything the scene draws is near one of these two mixes."""
    greys = [mixc(INK, (k / (GREYS - 1)) ** 1.7) for k in range(GREYS)]
    ambers = [mixc(AMBER, 0.12 + 0.88 * k / 11) for k in range(12)]
    cols = list(dict.fromkeys(greys + ambers))
    return cols + [cols[-1]] * (256 - len(cols))


def amber_pixels(img: Image.Image) -> int:
    px = img.convert("RGB").load()
    w, h = img.size
    return sum(1 for y in range(0, h, 2) for x in range(0, w, 2)
               if px[x, y][0] > 110 and px[x, y][0] - px[x, y][2] > 60)


def check(gif: Path, fps: float) -> None:
    """The GIF stays under the size cap, and every frame that shows the final list keeps its amber."""
    if gif.stat().st_size > MAX_GIF:
        sys.exit(f"make_hero: {gif.name} is {gif.stat().st_size / 1e6:.2f} MB (cap 2.5 MB)")
    with Image.open(gif) as im:
        for k in range(getattr(im, "n_frames", 1)):
            t = k / fps
            if GOLD_T + 0.1 <= t < FADE[0]:
                im.seek(k)
                if amber_pixels(im) == 0:
                    sys.exit(f"make_hero: {gif.name} frame {k} shows the final list without amber")


def render(lang: str, out: Path, fps: float, keep: Path | None) -> tuple[Path, Path]:
    if not shutil.which("ffmpeg"):
        sys.exit("make_hero: ffmpeg not found")
    sc = Scene(lang)
    n = int(round(fps * SECONDS))
    still = int(round(fps * (GOLD_T + 1.3)))        # the social preview: the final list with its caption
    work = Path(tempfile.mkdtemp(prefix=f"hero-{lang}-"))
    try:
        for k in range(n):
            sc.frame(k / fps, 1 / fps if k else 1.0).save(work / f"f{k:04d}.png")
        png = out / f"hero-{lang}.png"
        shutil.copy(work / f"f{still:04d}.png", png)
        gif = out / f"hero-{lang}.gif"
        pal = work / "palette.png"
        pimg = Image.new("RGB", (16, 16))
        pimg.putdata(palette_colors())
        pimg.save(pal)
        inp = ["-framerate", f"{fps:g}", "-i", str(work / "f%04d.png")]
        subprocess.run(["ffmpeg", "-v", "error", "-y", *inp, "-i", str(pal), "-lavfi",
                        "paletteuse=dither=none:diff_mode=rectangle", "-loop", "0", str(gif)], check=True)
        check(gif, fps)
        if keep:
            keep.mkdir(parents=True, exist_ok=True)
            for f in work.glob("f*.png"):
                shutil.copy(f, keep / f"{lang}-{f.name}")
        return gif, png
    finally:
        shutil.rmtree(work, ignore_errors=True)


def write_manifest(out: Path, files: list[Path]) -> Path:
    mp = out / "MANIFEST.json"
    try:
        man = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        man = {}
    entries = man.get("files") if isinstance(man.get("files"), dict) else {}
    for f in files:
        old = entries.get(f.name) if isinstance(entries.get(f.name), dict) else {}
        digest = sha256(f)
        prev = [h for h in (old.get("previous_sha256") or []) if isinstance(h, str)]
        if old.get("sha256") and old["sha256"] != digest:
            prev.append(old["sha256"])      # the published history keeps the earlier version
        prev = [h for h in dict.fromkeys(prev) if h != digest][-6:]
        entries[f.name] = {"origin": "own-output", "bytes": f.stat().st_size, "sha256": digest,
                           "made_by": "tools/make_hero.py",
                           "what": "README hero: sand panned through three sieves (illustrative numbers)"}
        if prev:
            entries[f.name]["previous_sha256"] = prev
    man = {"about": "Images under docs/assets made by this repository's own tools. tools/release_check.py accepts "
                    "a listed file over its size limit when its bytes and sha256 match (and it stays under the "
                    "hero cap); in the git history, a blob whose sha256 is listed or in previous_sha256.",
           "files": dict(sorted(entries.items()))}
    mp.write_text(json.dumps(man, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return mp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    root = Path(__file__).resolve().parents[1]
    ap.add_argument("--out", default=str(root / "docs" / "assets"))
    ap.add_argument("--lang", default="en,zh")
    ap.add_argument("--fps", type=float, default=12.5, help="12.5 = 80 ms per frame, exact in a GIF")
    ap.add_argument("--keep-frames", default=None, help="also copy every frame here (for review)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for lang in [x.strip() for x in a.lang.split(",") if x.strip()]:
        if lang not in CAPTION:
            print(f"make_hero: unknown language {lang!r}", file=sys.stderr)
            return 2
        gif, png = render(lang, out, a.fps, Path(a.keep_frames) if a.keep_frames else None)
        made += [gif, png]
        n = int(round(a.fps * SECONDS))
        print(f"{gif} {gif.stat().st_size / 1024:.0f} KB ({n} frames x {1000 / a.fps:.0f} ms = {n / a.fps:.2f} s); "
              f"{png} {png.stat().st_size / 1024:.0f} KB")
    print(write_manifest(out, made))
    return 0


if __name__ == "__main__":
    sys.exit(main())

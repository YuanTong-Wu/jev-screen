"""The text view's skin (page.CSS) and the HUD's (page_hud.CSS) keep the rules of the 2026-09-29 restyle: one set of
colour settings shared by the text view, the results panel and printing; body text at >= 4.5:1 and small labels and
component edges at >= 3:1 on the black; print keeps the light palette at the same floors; the five evidence tags differ
by line style, not by colour alone; a chosen answer is marked by more than colour; nothing fetched (no url(), @import or
@font-face: the strict CSP allows inline styles only)."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)
from jevscreen import page, page_hud  # noqa: E402


def tokens(decls: str) -> dict[str, str]:
    out = {}
    for part in decls.split(";"):
        if part.strip().startswith("--") and ":" in part:
            k, v = part.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def rgb(value: str, bg: tuple[float, float, float]) -> tuple[float, float, float]:
    """A #rrggbb or rgba(r,g,b,a) colour composited over bg."""
    value = value.strip()
    if value.startswith("#"):
        h = value[1:]
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    m = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    assert m, value
    parts = [float(x) for x in m.group(1).split(",")]
    a = parts[3] if len(parts) > 3 else 1.0
    return tuple(c * a + b * (1 - a) for c, b in zip(parts[:3], bg))  # type: ignore[return-value]


def lum(c: tuple[float, float, float]) -> float:
    def f(v: float) -> float:
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (f(x) for x in c)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(fg: str, bg: str) -> float:
    b = rgb(bg, (0, 0, 0))
    la, lb = sorted([lum(rgb(fg, b)), lum(b)], reverse=True)
    return (la + 0.05) / (lb + 0.05)


def rule(css: str, selector: str) -> str:
    m = re.search(r"(?:^|[}\n])" + re.escape(selector) + r"\{([^}]*)\}", css)
    assert m, selector
    return m.group(1)


class TokenTest(unittest.TestCase):
    def test_root_and_print_come_from_the_shared_tokens(self):
        self.assertIn(":root{" + page.TOKENS_DARK + ";", page.CSS)
        self.assertIn("@media print{:root{" + page.TOKENS_LIGHT + ";", page.CSS)

    def test_panel_and_print_from_3d_use_the_same_tokens(self):
        self.assertTrue(page_hud._DARK.startswith(page.TOKENS_DARK))
        self.assertEqual(page_hud._LIGHT, page.TOKENS_LIGHT)
        self.assertIn(page.TOKENS_DARK, page_hud.CSS)
        self.assertIn(page.TOKENS_LIGHT, page_hud.CSS)
        # the panel's inks read the shared ink, not their own copy of it
        self.assertNotIn("rgba(242,237,224,", page_hud.CSS.replace(page.TOKENS_DARK, ""))
        self.assertNotIn("#ef9f27", page_hud.CSS.lower().replace(page.TOKENS_DARK, ""))

    def test_dark_contrast(self):
        t = tokens(page.TOKENS_DARK)
        for k in ("--fg", "--fg2", "--muted", "--fact", "--gap", "--user", "--ai"):
            self.assertGreaterEqual(ratio(t[k], t["--bg"]), 4.5, k)
        for k in ("--dim", "--infer", "--line2"):
            self.assertGreaterEqual(ratio(t[k], t["--bg"]), 3.0, k)

    def test_print_contrast(self):
        t = tokens(page.TOKENS_LIGHT)
        for bg in ("#ffffff", t["--bg"]):
            for k in ("--fg", "--fg2", "--muted", "--infer", "--fact", "--gap", "--user", "--ai"):
                self.assertGreaterEqual(ratio(t[k], bg), 4.5, (k, bg))
            self.assertGreaterEqual(ratio(t["--dim"], bg), 3.0, bg)


class SkinTest(unittest.TestCase):
    def test_tags_differ_by_line_style(self):
        css = page.CSS
        self.assertIn("border-style:dashed", rule(css, ".t-gap"))
        self.assertIn("double", rule(css, ".t-user"))
        self.assertIn("border-style:dotted", rule(css, ".t-ai"))
        self.assertIn("var(--infer)", css[css.index(".t-inference{"):css.index("}", css.index(".t-inference{"))])
        fact = css[css.index(".t-fact{"):css.index("}", css.index(".t-fact{"))]
        self.assertNotIn("dashed", fact)
        self.assertNotIn("dotted", fact)

    def test_chosen_answer_is_not_colour_only(self):
        self.assertIn('button.on::before{content:"\\2713', page.CSS)
        self.assertIn("aria-pressed", page.JS)

    def test_mono_only_on_codes(self):
        # the words (tags, toggles, the country line, the footer) are in the sans; codes keep the monospace
        for sel in (".tag", ".more-link", "button.more", "footer", ".badge"):
            m = re.search(r"(?:^|[}\n])" + re.escape(sel) + r"\{([^}]*)\}", page.CSS)
            self.assertTrue(m, sel)
            self.assertNotIn("var(--mono)", m.group(1), sel)
        self.assertIn("var(--mono)", rule(page.CSS, ".tk"))

    def test_nothing_fetched(self):
        for css in (page.CSS, page_hud.CSS):
            low = css.lower()
            for bad in ("url(", "@import", "@font-face"):
                self.assertNotIn(bad, low)


if __name__ == "__main__":
    unittest.main()

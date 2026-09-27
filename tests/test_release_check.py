"""tools/release_check.py: every category fires on a hand-made bad tree, a clean tree passes, and this repo passes."""
from __future__ import annotations

import contextlib
import gzip
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("release_check", ROOT / "tools" / "release_check.py")
rc = importlib.util.module_from_spec(_spec)
sys.modules["release_check"] = rc          # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(rc)  # type: ignore[union-attr]

# Built from parts so this test file itself stays clean for the scan of the repository.
HOME = "/" + "Users/" + "alice"
MAIL = "alice.smith" + "@" + "gmail.com"
REAL_LOOKING_KEY = "sk-or-v1-" + "9f3c2b7a1d8e4f6091b2c3d4e5f6a7b8c9d0e1f2"


def manifest(files: dict[str, str]) -> str:
    return json.dumps({"files": {k: {"origin": v, "how": "test"} for k, v in files.items()}})


class Tree:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def write(self, rel: str, data: str | bytes) -> None:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def scan(self, **kw) -> list:
        with mock.patch.object(rc, "personal_markers", return_value=[]):
            return rc.scan(self.root, **kw)

    def close(self) -> None:
        self._tmp.cleanup()


class ReleaseCheck(unittest.TestCase):
    def setUp(self) -> None:
        self.t = Tree()
        self.addCleanup(self.t.close)
        self.t.write("tests/fixtures/MANIFEST.json", manifest({"ok.json": "synthetic"}))
        self.t.write("tests/fixtures/ok.json", '{"name": "Invented Co", "contact": "ir@example.com"}')

    def cats(self, **kw) -> dict[str, list]:
        out: dict[str, list] = {}
        for f in self.t.scan(**kw):
            out.setdefault(f.category, []).append(f)
        return out

    def test_clean_tree_passes(self):
        self.t.write("src/a.py", 'UA = "Test Person test@example.org"\nKEY = "sk-or-v1-FAKE-test-key-0000"\n'
                                 'assert "/Users/" not in s\nPATH = "/Users/<name>/data"\n')
        self.assertEqual(self.t.scan(), [])

    def test_email_home_old_project_and_key(self):
        self.t.write("src/a.py", f'OWNER = "{MAIL}"\nP = "{HOME}/Documents/x"\n'
                                 'OLD = "old/ws/.sec' + 'rets/openrouter_api_key"\n'
                                 f'K = "{REAL_LOOKING_KEY}"\n')
        c = self.cats()
        self.assertEqual([f.line for f in c["email"]], [1])
        self.assertEqual([f.line for f in c["home-path"]], [2])
        self.assertEqual([f.line for f in c["old-project"]], [3])
        self.assertEqual([f.line for f in c["api-key"]], [4])
        text = "\n".join(str(f) for f in self.t.scan())
        self.assertNotIn(MAIL, text)                      # evidence is redacted
        self.assertNotIn(REAL_LOOKING_KEY[12:], text)

    def test_ignore_pragma(self):
        self.t.write("src/a.py", f'X = "{HOME}"  # release-check: ignore\n')
        self.assertEqual(self.t.scan(), [])

    def test_private_key_and_key_assignment(self):
        self.t.write("docs/k.md", "-----BEGIN OPENSSH " + "PRIVATE KEY-----\napi_" + "key = 'Q7w9Zr2Lk5Vb8Nx3Mc6Tp1Hs4Jd0Fg'\n")
        self.assertEqual([f.line for f in self.cats()["api-key"]], [1, 2])

    def test_secret_files(self):
        for rel in ("data/jevscreen.duckdb", "data/openrouter_api_key", ".secrets/x", ".env", "sec_user_agent"):
            self.t.write(rel, "x")
        # walk mode skips data/ and .secrets/ entirely; the rest are flagged by name
        self.assertEqual(sorted(f.path for f in self.cats()["secret-file"]), [".env", "sec_user_agent"])

    def test_large_and_binary_files(self):
        self.t.write("docs/big.txt", "a" * (300 * 1024))
        self.t.write("docs/blob.bin", b"\0" * (130 * 1024))
        self.t.write("docs/small.bin", b"\0" * 1024)
        self.assertEqual(sorted(f.path for f in self.cats()["large-file"]), ["docs/big.txt", "docs/blob.bin"])

    def test_fixture_manifest(self):
        self.t.write("tests/fixtures/new.html", "<html></html>")
        self.t.write("tests/fixtures/MANIFEST.json", manifest({"ok.json": "synthetic", "gone.json": "synthetic",
                                                               "cap.json": "captured"}))
        self.t.write("tests/fixtures/cap.json", "{}")
        c = self.cats()
        self.assertEqual(sorted((f.path, f.evidence) for f in c["fixture"]), [
            ("tests/fixtures/cap.json", "origin 'captured'"),
            ("tests/fixtures/gone.json", "declared in MANIFEST.json but missing"),
            ("tests/fixtures/new.html", "not declared in MANIFEST.json")])

    def test_missing_manifest(self):
        (self.t.root / "tests/fixtures/MANIFEST.json").unlink()
        self.assertEqual(len(self.cats()["fixture"]), 1)

    def test_gray_domain_in_fixtures_including_gz_and_zip(self):
        page = '<a href="https://www.tradingview.com/symbols/X/">x</a>'
        self.t.write("tests/fixtures/dump.html.gz", gzip.compress(page.encode()))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("inner.html", "see https://finance.yahoo.com/quote/X")
        self.t.write("tests/fixtures/dump.zip", buf.getvalue())
        self.t.write("tests/fixtures/hand.html", page)
        self.t.write("tests/fixtures/big.html", page + "<p>" + "z" * (200 * 1024))
        self.t.write("tests/fixtures/MANIFEST.json", manifest({
            "ok.json": "synthetic", "dump.html.gz": "own-output", "dump.zip": "own-output",
            "hand.html": "hand-made", "big.html": "hand-made"}))
        self.t.write("src/adapter.py", 'URL = "https://www.tradingview.com/symbols/"\n')   # code may name the domain
        got = sorted(f.path for f in self.cats()["gray-domain"])
        self.assertEqual(got, ["tests/fixtures/big.html", "tests/fixtures/dump.html.gz", "tests/fixtures/dump.zip"])

    def test_gray_domain_covers_every_twse_host_and_tpex(self):
        urls = {"doc.json": "https://doc.twse.com.tw/server-java/t57sb01",   # serves the MOPS annual reports
                "mops.json": "https://mops.twse.com.tw/mops/api/t05st03",
                "mopsov.json": "https://mopsov.twse.com.tw/mops/web/t05st03",
                "openapi.json": "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
                "tpex.json": "https://www.tpex.org.tw/openapi/v1/x"}
        for name, url in urls.items():
            self.t.write(f"tests/fixtures/{name}", json.dumps({"u": url}))
        self.t.write("tests/fixtures/MANIFEST.json",
                     manifest({"ok.json": "synthetic", **{name: "own-output" for name in urls}}))
        got = sorted(f.path for f in self.cats()["gray-domain"])
        self.assertEqual(got, sorted(f"tests/fixtures/{name}" for name in urls))

    def test_email_inside_gz_member(self):
        self.t.write("tests/fixtures/ok.json.gz", gzip.compress(f"contact {MAIL}\n".encode()))
        self.t.write("tests/fixtures/MANIFEST.json", manifest({"ok.json": "synthetic", "ok.json.gz": "synthetic"}))
        self.assertEqual([f.path for f in self.cats()["email"]], ["tests/fixtures/ok.json.gz"])

    def test_personal_markers_whole_word(self):
        self.t.write("src/a.py", "who = 'jdoe42'\nwhat = 'xjdoe42y'\n")
        import re
        mk = [re.compile(r"(?<![A-Za-z0-9])jdoe42(?![A-Za-z0-9])", re.I)]
        with mock.patch.object(rc, "personal_markers", return_value=mk):
            found = rc.scan(self.t.root)
        self.assertEqual([(f.category, f.line) for f in found], [("home-path", 1)])

    def test_main_exit_codes_and_json(self):
        with mock.patch.object(rc, "personal_markers", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rc.main([str(self.t.root), "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), {"ok": True, "findings": []})
        self.t.write("src/a.py", f'P = "{HOME}/x"\n')
        with mock.patch.object(rc, "personal_markers", return_value=[]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rc.main([str(self.t.root)]), 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(rc.main([str(self.t.root / "nope")]), 2)


class GitHistory(unittest.TestCase):
    """--git-history reads every blob reachable from any ref, not only commit e-mails and deleted fixture paths."""

    NEUTRAL = ("jev-screen contributors", "12345+jev@users.noreply.github.com")

    def setUp(self) -> None:
        self.t = Tree()
        self.addCleanup(self.t.close)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, who: tuple[str, str] | None = None) -> None:
        import os
        import subprocess
        name, mail = who or self.NEUTRAL
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": mail,
               "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": mail}
        subprocess.run(["git", "-C", str(self.t.root), "-c", "commit.gpgsign=false", *args],
                       check=True, capture_output=True, env=env)

    def commit(self, msg: str, who: tuple[str, str] | None = None) -> None:
        self.git("add", "-A", who=who)
        self.git("commit", "-q", "-m", msg, who=who)

    def history(self, names: list[str] | None = None) -> list:
        with mock.patch.object(rc, "personal_names", return_value=names or []):
            return [f for f in self.t.scan(git_history=True) if f.category == "history"]

    def test_squashed_neutral_history_is_clean(self):
        self.t.write("tests/fixtures/MANIFEST.json", manifest({"f.json": "synthetic"}))
        self.t.write("tests/fixtures/f.json", '{"name": "Invented Co"}')
        self.commit("one")
        self.assertEqual(self.t.scan(git_history=True), [])

    def test_overwritten_fixture_old_blob_is_reported(self):
        self.t.write("tests/fixtures/MANIFEST.json", manifest({"f.json": "synthetic"}))
        self.t.write("tests/fixtures/f.json", '{"url": "https://www.tradingview.com/symbols/TSE-7203/"}')
        self.commit("real dump")
        self.t.write("tests/fixtures/f.json", '{"name": "Invented Co"}')
        self.commit("synthetic")
        self.assertEqual(self.t.scan(), [])            # the tree itself is clean
        got = self.history()
        self.assertEqual(len(got), 1, [str(f) for f in got])
        self.assertTrue(got[0].path.startswith("tests/fixtures/f.json@"))
        self.assertIn("tradingview.com", got[0].evidence)
        self.assertIn("earlier version", got[0].evidence)

    def test_removed_fixture_is_reported(self):
        self.t.write("tests/fixtures/MANIFEST.json", manifest({"f.json": "synthetic"}))
        self.t.write("tests/fixtures/f.json", "{}")
        self.t.write("tests/fixtures/gone.json", '{"old": 1}')
        self.commit("one")
        (self.t.root / "tests/fixtures/gone.json").unlink()
        self.commit("two")
        got = self.history()
        self.assertEqual([f.path.split("@")[0] for f in got], ["tests/fixtures/gone.json"])
        self.assertIn("removed", got[0].evidence)

    def test_home_path_and_email_in_old_non_fixture_blob(self):
        self.t.write("src/config.py", f'KEY = "{HOME}/work/key"\nOWNER = "{MAIL}"\n')
        self.commit("one")
        self.t.write("src/config.py", 'KEY = None\n')
        self.commit("two")
        got = sorted((f.path.split("@")[0], f.line, f.evidence.split(":")[0]) for f in self.history())
        self.assertEqual(got, [("src/config.py", 1, "home-path"), ("src/config.py", 2, "email")])
        self.assertNotIn(MAIL, "\n".join(str(f) for f in self.history()))

    def test_old_secret_file_in_history(self):
        self.t.write("data/openrouter_api_key", "x")
        self.commit("oops")
        (self.t.root / "data/openrouter_api_key").unlink()
        self.commit("remove")
        got = self.history()
        self.assertEqual([f.path.split("@")[0] for f in got], ["data/openrouter_api_key"])

    def test_personal_author_name(self):
        self.t.write("README.md", "hello\n")
        self.commit("one", who=("Alice Smith", "12345+alice@users.noreply.github.com"))
        got = self.history(names=["alice smith"])
        self.assertEqual(len(got), 1)
        self.assertIn("name", got[0].evidence)
        self.assertNotIn("Alice Smith", got[0].evidence)
        self.assertEqual(self.history(names=["Bob Jones"]), [])
        # the running machine's user name also counts as personal
        import re
        with mock.patch.object(rc, "personal_names", return_value=[]), \
                mock.patch.object(rc, "personal_markers", return_value=[re.compile("alice", re.I)]):
            self.assertTrue(any("name" in f.evidence for f in rc.scan(self.t.root, git_history=True)
                                if f.category == "history"))


class PrivatePatterns(unittest.TestCase):
    """Maintainer-only workspace patterns live in an untracked file, never in the scanner itself."""

    def test_private_patterns_file_extends_the_generic_pattern(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools").mkdir()
            self.assertIsNone(rc.old_project_re(root).search("x/my-old-workspace/notes"))
            (root / rc.PRIVATE_PATTERNS_FILE).write_text("# comment\nmy-old-workspace\n", encoding="utf-8")
            self.assertIsNotNone(rc.old_project_re(root).search("x/my-old-workspace/notes"))
            self.assertIsNotNone(rc.old_project_re(root).search("a/.sec" + "rets/foo_api_key"))

    def test_scanner_source_names_no_private_path(self):
        src = Path(rc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("referenced-" + "chatgpt", src)
        self.assertNotIn("serenity" + ".duckdb", src)


class ExportIgnore(unittest.TestCase):
    """Paths marked export-ignore are never published (git archive drops them), so they are not scanned."""

    def test_export_ignored_folder_is_skipped(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "docs" / "dev").mkdir(parents=True)
            (root / "docs" / "dev" / "notes.md").write_text("P = \"/" + "Users/alice/x\"\n", encoding="utf-8")
            (root / "src.py").write_text("x = 1\n", encoding="utf-8")
            self.assertIn("docs/dev/notes.md", rc.list_files(root))
            (root / ".gitattributes").write_text("docs/dev/ export-ignore\n", encoding="utf-8")
            self.assertNotIn("docs/dev/notes.md", rc.list_files(root))
            self.assertIn("src.py", rc.list_files(root))


class ThisRepository(unittest.TestCase):
    def test_repository_is_clean(self):
        findings = rc.scan(ROOT)
        self.assertEqual([str(f) for f in findings], [])

    def test_third_party_licences_ship_their_text(self):
        # Apache-2.0 s.4(a): redistributing the embedded font subset means giving recipients a copy of the licence
        import re
        doc = (ROOT / "DATA_LICENSES.md").read_text(encoding="utf-8")
        table = doc.split("## Third-party material inside the repository", 1)[1].split("\n## ", 1)[0]
        rows = [r for r in table.splitlines() if r.startswith("| `")]
        self.assertTrue(rows)
        for row in rows:
            links = re.findall(r"\]\((LICENSES/[^)]+)\)", row)
            self.assertTrue(links, row)
            for link in links:
                self.assertTrue((ROOT / link).is_file(), link)
        how = json.loads((ROOT / "tests/fixtures/MANIFEST.json").read_text(encoding="utf-8"))["files"]
        for name, entry in how.items():
            if "Apache-2.0" in entry["how"]:
                self.assertIn("LICENSES/Apache-2.0.txt", entry["how"], name)


if __name__ == "__main__":
    unittest.main()

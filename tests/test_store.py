"""Tests for jevscreen.store schema migrations. Temp DuckDB files only; the live database is never opened."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # run without `pip install -e .`
import safe_env  # noqa: E402,F401  (suite-wide network kill switch: tests/safe_env.py)

from jevscreen import jev, store  # noqa: E402

# jev_items / screen_results exactly as created before the migrations existed (HEAD 7c80218).
OLD_TABLES = r"""
CREATE TABLE jev_items (
    item_key VARCHAR, request_id VARCHAR, run_id VARCHAR, layer VARCHAR, position INTEGER, status VARCHAR,
    label VARCHAR, probs_json VARCHAR, error VARCHAR, created_at TIMESTAMP, PRIMARY KEY (item_key, request_id));
CREATE TABLE screen_results (
    run_id VARCHAR, company_key VARCHAR, security_id VARCHAR, layer VARCHAR,
    label VARCHAR, probs_json VARCHAR, p_top DOUBLE, request_id VARCHAR,
    input_source VARCHAR, input_tier VARCHAR, evidence_url VARCHAR, evidence_excerpt VARCHAR,
    status VARCHAR, error VARCHAR,
    PRIMARY KEY (run_id, company_key, layer));
"""
NEW_COLS = {"jev_items": [("read_index", "INTEGER"), ("confidence", "DOUBLE")],
            "screen_results": [("reads_json", "VARCHAR"), ("p_pos", "DOUBLE"), ("p_pos_sd", "DOUBLE")]}


def columns(con, table: str) -> list[tuple[str, str]]:
    return [(r[0], r[1]) for r in con.execute(f"DESCRIBE {table}").fetchall()]


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "old.duckdb")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_alters_are_idempotent_on_an_old_schema_db(self):
        con = duckdb.connect(self.path)
        con.execute(OLD_TABLES)
        con.execute("INSERT INTO jev_items VALUES ('k1', 'r1', 'run', 'l2', 0, 'ok', 'explicit', '{}', NULL, "
                    "TIMESTAMP '2026-09-26 00:00:00')")
        con.execute("INSERT INTO screen_results (run_id, company_key, security_id, layer, label, status) "
                    "VALUES ('run', 'isin:X', 'NYSE:X', 'l2', 'partial', 'ok')")
        old = {t: columns(con, t) for t in NEW_COLS}
        store.init(con)
        store.init(con)                                   # twice in one connection
        con.close()
        con = duckdb.connect(self.path)
        store.init(con)                                   # and again after a reopen
        for table, added in NEW_COLS.items():
            self.assertEqual(columns(con, table), old[table] + added, table)
        self.assertEqual(con.execute("SELECT item_key, label, read_index, confidence FROM jev_items").fetchall(),
                         [("k1", "explicit", None, None)])
        self.assertEqual(con.execute("SELECT label, reads_json, p_pos, p_pos_sd FROM screen_results").fetchall(),
                         [("partial", None, None, None)])
        # the client's full column list now writes into the migrated table
        store.upsert_many(con, "jev_items", jev.ITEM_COLS, [
            ("k2", "r2", "run", "l2", 1, "ok", "partial", "{}", None, store.now_utc(), 2, 0.5)])
        self.assertEqual(con.execute("SELECT read_index, confidence FROM jev_items WHERE item_key = 'k2'").fetchone(),
                         (2, 0.5))
        con.close()

    def test_new_db_matches_migrated_db(self):
        con = duckdb.connect(self.path)
        store.init(con)
        store.init(con)
        fresh = {t: columns(con, t) for t in NEW_COLS}
        con.close()
        old_path = str(Path(self.tmp.name) / "old2.duckdb")
        con = duckdb.connect(old_path)
        con.execute(OLD_TABLES)
        store.init(con)
        self.assertEqual({t: columns(con, t) for t in NEW_COLS}, fresh)
        con.close()


if __name__ == "__main__":
    unittest.main()

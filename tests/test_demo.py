"""Demo mode: generated data goes through the real pipeline and the dashboard opens without a password."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import App  # noqa: E402
from config import load_config  # noqa: E402
from demo_data import seed  # noqa: E402
from report import build_report  # noqa: E402

NOW = 1791800000


class DemoTest(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(env_file="nonexistent.env", overrides={"DEMO_MODE": "1", "AMO_TOKEN": "x",
                                                                     "ACCESS_TOKEN": "y"})
        self.app = App(self.cfg)

    def test_demo_config_is_offline(self):
        self.assertTrue(self.cfg.demo_mode)
        self.assertEqual(self.cfg.db_path, ":memory:")
        self.assertFalse(self.app.fetcher.enabled)   # no Meta requests
        self.assertFalse(self.app.amo.enabled)       # no amoCRM requests
        self.assertTrue(self.app.authorized(""))     # no password

    def test_seed_is_plausible_and_deterministic(self):
        n = seed(self.app.db, self.cfg, days=30, seed=7, now=NOW)
        self.assertGreater(n, 300)
        rep = build_report(self.app.db, "2026-08-01", "2026-12-31", self.cfg.tz_offset_hours)
        t = rep["totals"]
        self.assertEqual(t["all"], t["ad"] + t["organic"])
        self.assertTrue(0.3 < t["ad"] / t["all"] < 0.9)
        self.assertGreater(len([c for c in rep["creatives"] if c["source"] == "ad"]), 4)
        other = App(self.cfg)
        self.assertEqual(seed(other.db, self.cfg, days=30, seed=7, now=NOW), n)
        phones = {r["wa_id"] for r in rep["inquiries"]}
        self.assertTrue(all(p.startswith("7700000") for p in phones))


if __name__ == "__main__":
    unittest.main()

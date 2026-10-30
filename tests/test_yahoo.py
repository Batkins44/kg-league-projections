"""Tests for yahoo_fetch.py's parsing. No network. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yahoo_fetch as yf  # noqa: E402

ROSTER = json.loads((ROOT / "tests" / "fixtures" / "yahoo_roster.json").read_text())["fantasy_content"]


class Helpers(unittest.TestCase):
    def test_merge_flattens_nested_fragments(self):
        self.assertEqual(yf.merge([[{"a": 1}, {"b": 2}], {"c": 3}, "skip"]), {"a": 1, "b": 2, "c": 3})

    def test_items_orders_numeric_keys_and_skips_count(self):
        coll = {"10": {"x": "k"}, "2": {"x": "c"}, "0": {"x": "a"}, "count": 3}
        self.assertEqual(yf.items(coll, "x"), ["a", "c", "k"])

    def test_items_on_empty_list(self):
        # Yahoo sends [] for an empty collection
        self.assertEqual(yf.items([], "player"), [])

    def test_items_on_plain_list(self):
        # player_stats.stats is a list of {"stat": {...}}, not a numbered dict
        self.assertEqual(yf.items([{"stat": 1}, {"stat": 2}, "x"], "stat"), [1, 2])

    def test_num_treats_dash_as_zero(self):
        self.assertEqual((yf.num("-"), yf.num(""), yf.num(None), yf.num("28")), (0, 0, 0, 28))

    def test_weekly_add_limit(self):
        self.assertEqual(yf.weekly_add_limit({"max_weekly_adds": "5"}), 5)
        self.assertEqual(yf.weekly_add_limit({"max_weekly_adds": "7"}), 7)
        self.assertEqual(yf.weekly_add_limit({}), 5)
        self.assertEqual(yf.weekly_add_limit({"max_weekly_adds": "0"}), 5)


class Roster(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(yf, "get", return_value=ROSTER)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_parse_player_fields(self):
        p = yf.roster("k", date(2026, 10, 28), "t")[0]
        self.assertEqual(p["name"], "Skater One")
        self.assertEqual(p["nhl_team"], "TOR")
        self.assertEqual(p["slot"], "C")
        self.assertEqual(p["eligible"], ["C", "Util"])
        self.assertEqual(p["status"], "DTD")
        self.assertEqual(p["stats"], {})

    def test_parse_goalie_stats_list(self):
        g = yf.roster("k", date(2026, 10, 28), "t")[1]
        self.assertEqual(g["stats"], {"25": "28", "23": "2"})

    def test_goalie_appearances_count_only_g_slot_with_shots(self):
        got = yf.goalie_appearances("k", date(2026, 10, 26), date(2026, 10, 27), "25", "23", "t")
        # Goalie Two started from the G slot both days; Goalie Three sat on the bench.
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["by_day"][0], {"date": "2026-10-26", "goalies": ["Goalie Two"]})
        self.assertEqual(got["stats_missing"], [])

    def test_goalie_with_no_stats_is_flagged_not_counted(self):
        bare = json.loads(json.dumps(ROSTER))
        bare["team"][1]["roster"]["0"]["players"]["1"]["player"].pop()
        with mock.patch.object(yf, "get", return_value=bare):
            got = yf.goalie_appearances("k", date(2026, 10, 26), date(2026, 10, 26), "25", "23", "t")
        self.assertEqual((got["count"], got["stats_missing"]), (0, ["2026-10-26"]))

    def test_goalie_with_dash_stats_did_not_play(self):
        dash = json.loads(json.dumps(ROSTER))
        for s in dash["team"][1]["roster"]["0"]["players"]["1"]["player"][2]["player_stats"]["stats"]:
            s["stat"]["value"] = "-"
        with mock.patch.object(yf, "get", return_value=dash):
            got = yf.goalie_appearances("k", date(2026, 10, 26), date(2026, 10, 26), "25", "23", "t")
        self.assertEqual((got["count"], got["stats_missing"]), (0, []))

    def test_no_appearances_before_monday_games(self):
        got = yf.goalie_appearances("k", date(2026, 10, 26), date(2026, 10, 25), "25", "23", "t")
        self.assertEqual((got["count"], got["by_day"]), (0, []))


class Main(unittest.TestCase):
    def test_missing_secrets_exit_clean(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(sys, "argv", ["yahoo_fetch.py"]):
            self.assertEqual(yf.main(), 0)


if __name__ == "__main__":
    unittest.main()

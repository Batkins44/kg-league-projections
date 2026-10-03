"""Tests for build.py. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
CFG = build.load_cfg(ROOT / "scoring.json")
PRIOR = build.load_prior(ROOT / "prior" / "projections_preseason.csv")


def by_name(players, name):
    return next(p for p in players if p["player"] == name)


class BlendMath(unittest.TestCase):
    def test_blend_weights_prior_by_k(self):
        # prior 0.5/game worth 20 games; 3 in 10 actual games -> (10 + 3) / 30
        self.assertAlmostEqual(build.blend(0.5, 20, 3, 10), 13 / 30)

    def test_blend_no_games_returns_prior(self):
        self.assertAlmostEqual(build.blend(0.42, 20, 0, 0), 0.42)

    def test_norm_strips_accents_and_punctuation(self):
        self.assertEqual(build.norm("Tim Stützle"), "tim stutzle")
        self.assertEqual(build.norm("Charle-Edouard D'Astous"), "charle edouard dastous")
        self.assertEqual(build.norm("J.J. Moser"), "jj moser")

    def test_current_team_takes_last_listed(self):
        self.assertEqual(build.current_team("CBJ,TOR"), "TOR")
        self.assertEqual(build.current_team("TBL"), "TBL")


class PriorOnly(unittest.TestCase):
    """With no NHL data, the blend must reproduce the preseason numbers under the configured scoring."""

    def setUp(self):
        raw = {"sk_sum": [], "sk_rt": [], "go_sum": [], "sk_l14": [], "go_l14": [], "standings": [], "meta": {}}
        self.result = build.build(CFG, PRIOR, raw)
        self.players = self.result["players"]

    def test_counts(self):
        self.assertEqual(len(self.players), len(PRIOR))
        self.assertEqual(self.result["n_new"], 0)
        self.assertEqual(len(self.result["unmatched_prior"]), len(PRIOR))

    def test_kucherov_matches_preseason(self):
        k = by_name(self.players, "Nikita Kucherov")
        # (32.7*4.5 + 74.2*3 + 0.6*2 + 211.4*0.5 + 29.1*0.25 + 28.1*0.5) / 71.0
        self.assertAlmostEqual(k["fp_per_game"], 497.975 / 71.0, places=4)
        self.assertAlmostEqual(k["fp_per_game"], k["fp_prior_pg"], places=9)
        self.assertAlmostEqual(k["proj_games_rest"], 71.0, places=6)   # whole season still ahead

    def test_goalie_uses_configured_ga(self):
        d = by_name(self.players, "Jakub Dobes")
        w = CFG["goalie"]
        expect = (31.2 * w["w"] + 1260 * w["sv"] + 142 * w["ga"] + 1.3 * w["so"]) / 50.7
        self.assertAlmostEqual(d["fp_per_game"], expect, places=6)

    def test_ranks_are_a_permutation(self):
        for n in (8, 10, 12):
            ranks = sorted(p[f"rank_{n}t"] for p in self.players)
            self.assertEqual(ranks, list(range(1, len(self.players) + 1)))

    def test_top_of_board_is_sane(self):
        top = sorted(self.players, key=lambda p: p["rank_10t"])[:5]
        self.assertIn("Nathan MacKinnon", [p["player"] for p in top])
        self.assertIn("Connor McDavid", [p["player"] for p in top])


class WithFixtures(unittest.TestCase):
    def setUp(self):
        self.raw = build.load_raw(FIX)
        self.result = build.build(CFG, PRIOR, self.raw)
        self.players = self.result["players"]

    def test_actual_points_skater(self):
        h = by_name(self.players, "Dougie Hamilton")
        self.assertEqual(h["gp"], 1)
        self.assertAlmostEqual(h["fp_actual_pg"], 4.5 + 6 * 0.5 + 3 * 0.25 + 1 * 0.5)
        # blended value sits between prior and actual after one game
        self.assertGreater(h["fp_per_game"], h["fp_prior_pg"])
        self.assertLess(h["fp_per_game"], h["fp_actual_pg"])

    def test_actual_points_goalie(self):
        v = by_name(self.players, "Karel Vejmelka")
        w = CFG["goalie"]
        self.assertAlmostEqual(v["fp_actual_pg"], w["w"] + 15 * w["sv"] + w["so"])
        self.assertEqual(v["starts"], 1)
        self.assertAlmostEqual(v["sv_pct"], 1.0)

    def test_name_matching_paths(self):
        s = by_name(self.players, "Tim Stützle")
        self.assertEqual(s["match"], "exact")
        self.assertIn("no hit/block", s["note"])        # left out of the realtime fixture on purpose
        self.assertEqual(by_name(self.players, "John-Jason Peterka")["match"], "alias")
        self.assertEqual(by_name(self.players, "Tony DeAngelo")["match"], "alias")
        # three Hugheses: exact matches only, nobody cross-wired
        self.assertEqual(by_name(self.players, "Jack Hughes")["gp"], 1)
        self.assertEqual(by_name(self.players, "Luke Hughes")["totals"]["blk"], 2)
        self.assertEqual(by_name(self.players, "Quinn Hughes")["team"], "MIN")

    def test_duplicate_names_resolve_by_position(self):
        c = by_name(self.players, "Elias Pettersson")          # the preseason row is the center
        self.assertEqual(c["pos"], "C")
        self.assertEqual(c["totals"]["g"], 2)
        dman = [p for p in self.players if p["player"] == "Elias Pettersson" and p["in_prior"] == "N"]
        self.assertEqual(len(dman), 1)
        self.assertEqual(dman[0]["pos"], "D")
        aho = by_name(self.players, "Sebastian Aho")             # preseason row is the Carolina center
        self.assertEqual(aho["match"], "unmatched")              # CAR's Aho isn't in the fixture; NYI's D must not be taken
        self.assertEqual(self.result["n_new"], 4)

    def test_traded_player_gets_current_team(self):
        self.assertEqual(by_name(self.players, "Kirill Marchenko")["team"], "TOR")

    def test_new_player_gets_position_default_prior(self):
        p = by_name(self.players, "Cole Perfetti")
        self.assertEqual(p["in_prior"], "N")
        self.assertEqual(p["pos"], "RW")
        self.assertIn("no preseason projection", p["note"])
        self.assertGreater(p["fp_per_game"], 2.0)
        self.assertLess(p["fp_per_game"], 4.0)
        g = by_name(self.players, "Dylan Garand")
        self.assertEqual(g["in_prior"], "N")
        self.assertTrue(g["is_g"])
        self.assertEqual(self.result["n_new"], 4)

    def test_ir_stash_keeps_prior_games(self):
        # Bedard: preseason 55.3 GP (shoulder), CHI has played 2, he has 0. The prior already priced the
        # absence in, so he keeps the games it still owes him instead of shrinking further.
        b = by_name(self.players, "Connor Bedard")
        self.assertAlmostEqual(b["proj_games_rest"], 55.3 / 82 * 82, places=6)
        # an unflagged healthy player who has missed games does shrink
        m = by_name(self.players, "Nathan MacKinnon")       # COL played 1, he isn't in the fixture
        self.assertLess(m["proj_games_rest"], 72.2 / 84 * 83)

    def test_new_player_prior_is_below_replacement(self):
        d = build.default_priors(PRIOR, CFG)
        self.assertLess(build.fp(d["F"], CFG["skater"]), 3.6)
        self.assertLess(build.fp(d["D"], CFG["skater"]), 2.8)
        self.assertGreater(build.fp(d["F"], CFG["skater"]), 2.5)

    def test_unplayed_prior_player_flagged(self):
        b = by_name(self.players, "Connor Bedard")
        self.assertEqual(b["gp"], 0)
        self.assertEqual(b["match"], "unmatched")
        self.assertIn("Connor Bedard", self.result["unmatched_prior"])
        self.assertIsNone(b["fp_actual_pg"])

    def test_missed_games_note(self):
        # CHI has played 2 in the fixture, so nobody is 3+ behind yet; force it through tgp lookup
        raw = json.loads(json.dumps(self.raw))
        raw["standings"] = [dict(s, gp=6) if s["team"] == "CHI" else s for s in raw["standings"]]
        res = build.build(CFG, PRIOR, raw)
        b = by_name(res["players"], "Connor Bedard")
        self.assertIn("no games yet (team has played 6)", b["note"])

    def test_l14_column(self):
        k = by_name(self.players, "Nikita Kucherov")
        self.assertAlmostEqual(k["fp_l14_pg"], 4.5 + 0.5)

    def test_rest_of_season_shrinks_with_games_played(self):
        k = by_name(self.players, "Nikita Kucherov")
        self.assertLess(k["proj_games_rest"], 71.0)
        self.assertGreater(k["proj_games_rest"], 60.0)


class EndToEnd(unittest.TestCase):
    def test_cli_writes_all_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            r = subprocess.run([sys.executable, str(ROOT / "build.py"), "--raw-dir", str(FIX), "--out-dir", str(out)],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            for name in ("projections.csv", "actuals_skaters.csv", "actuals_goalies.csv", "summary.md", "last_run.json"):
                self.assertTrue((out / name).exists(), name)
            with open(out / "projections.csv", encoding="utf-8", newline="") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), len(PRIOR) + 4)
            self.assertEqual(rows[0]["rank_10t"], "1")
            self.assertEqual(set(build.OUT_COLS), set(rows[0].keys()))
            ham = next(r for r in rows if r["player"] == "Dougie Hamilton")
            self.assertEqual(ham["fp_actual_pg"], "8.75")
            self.assertEqual(ham["hit"], "3")
            summary = (out / "summary.md").read_text(encoding="utf-8")
            self.assertIn("Top 30 skaters", summary)
            self.assertIn("Cole Perfetti", summary)
            meta = json.loads((out / "last_run.json").read_text())
            self.assertEqual(meta["new_players"], 4)

    def test_cli_refuses_empty_raw_without_flag(self):
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([sys.executable, str(ROOT / "build.py"), "--raw-dir", td, "--out-dir", td],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            r = subprocess.run([sys.executable, str(ROOT / "build.py"), "--raw-dir", td, "--out-dir", td, "--allow-empty"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()

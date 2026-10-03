#!/usr/bin/env python3
"""Blend preseason projections with this season's NHL stats under KG's league scoring.

Reads:  scoring.json, prior/projections_preseason.csv, data/raw/*.json (from nhl_fetch.py)
Writes: data/projections.csv      one row per player, blended per-game value + rest-of-season value + ranks
        data/actuals_skaters.csv  season-to-date totals with fantasy points
        data/actuals_goalies.csv
        data/summary.md           what moved, who's new, who's missing games
        data/last_run.json

How the blend works (per stat, per player):
    rate = (K * prior_rate + actual_total) / (K + games_played)
K is the number of games the preseason projection is "worth" (scoring.json -> prior_strength_games).
After K games the actuals carry half the weight; the prior fades as games pile up.
Shots, hits and blocks stabilize fast (small K); goals and assists are noisy (big K).

Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SK_STATS = ["g", "a", "shp", "sog", "hit", "blk"]
GO_STATS = ["w", "sv", "ga", "so"]
POS_MAP = {"C": "C", "L": "LW", "R": "RW", "D": "D", "G": "G"}

# Preseason (Yahoo-style) names that differ from NHL API names. Both sides normalized.
ALIASES = {
    "john jason peterka": "jj peterka",
    "tony deangelo": "anthony deangelo",
    "mitch marner": "mitchell marner",
    "alex ovechkin": "alexander ovechkin",
    "zach werenski": "zachary werenski",
    "matt boldy": "matthew boldy",
    "sam malinski": "samuel malinski",
    "jj moser": "janis moser",
    "mackenzie blackwood": "mackenzie blackwood",
}


# ----------------------------------------------------------------------------- helpers
def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = s.lower().replace(".", "").replace("'", "").replace("-", " ")
    return " ".join(s.split())


def fnum(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def blend(prior_rate: float, k: float, actual_total: float, n: float) -> float:
    return (k * prior_rate + actual_total) / (k + n) if (k + n) > 0 else prior_rate


def fp(rates: dict, weights: dict) -> float:
    return sum(rates.get(s, 0.0) * w for s, w in weights.items())


def current_team(team_abbrevs: str) -> str:
    parts = [t.strip() for t in (team_abbrevs or "").split(",") if t.strip()]
    return parts[-1] if parts else ""


def elig_from_pos(pos: str) -> set[str]:
    return {p.strip() for p in pos.split(",") if p.strip()}


def pos_bucket(elig: set[str]) -> str:
    if "G" in elig:
        return "G"
    return "D" if elig == {"D"} else "F"


# ----------------------------------------------------------------------------- loading
def load_cfg(path: Path) -> dict:
    return json.loads(path.read_text())


def load_prior(path: Path) -> list[dict]:
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            pos = r["pos"].strip()
            is_g = pos == "G"
            gp = fnum(r["proj_gp_or_starts"])
            stats = GO_STATS if is_g else SK_STATS
            totals = {s: fnum(r.get(s)) for s in stats}
            out.append({
                "player": r["player"].strip(),
                "team": r["team"].strip(),
                "pos": pos,
                "elig": elig_from_pos(pos),
                "is_g": is_g,
                "proj_gp": gp,
                "rank_10t_pre": int(fnum(r.get("rank_10t"), 9999)),
                "ir_flag": (r.get("ir_stash_8t") or "").strip().upper() == "Y",
                "totals": totals,
                "rates": {s: (v / gp if gp else 0.0) for s, v in totals.items()},
            })
    return out


def load_raw(raw: Path) -> dict:
    def rd(name, default):
        p = raw / name
        return json.loads(p.read_text()) if p.exists() else default
    return {
        "sk_sum": rd("skaters_summary.json", []),
        "sk_rt": rd("skaters_realtime.json", []),
        "go_sum": rd("goalies_summary.json", []),
        "sk_l14": rd("skaters_l14.json", []),
        "go_l14": rd("goalies_l14.json", []),
        "standings": rd("standings.json", []),
        "meta": rd("meta.json", {}),
    }


def skater_actuals(raw: dict) -> dict[int, dict]:
    rt = {int(r["playerId"]): r for r in raw["sk_rt"]}
    out = {}
    for r in raw["sk_sum"]:
        pid = int(r["playerId"])
        x = rt.get(pid)
        out[pid] = {
            "id": pid,
            "name": r.get("skaterFullName", ""),
            "team": current_team(r.get("teamAbbrevs", "")),
            "pos_nhl": POS_MAP.get(r.get("positionCode", ""), r.get("positionCode", "")),
            "gp": int(fnum(r.get("gamesPlayed"))),
            "g": fnum(r.get("goals")), "a": fnum(r.get("assists")),
            "shp": fnum(r.get("shPoints")), "sog": fnum(r.get("shots")),
            "hit": fnum(x.get("hits")) if x else 0.0,
            "blk": fnum(x.get("blockedShots")) if x else 0.0,
            "has_rt": x is not None,
        }
    return out


def goalie_actuals(raw: dict) -> dict[int, dict]:
    out = {}
    for r in raw["go_sum"]:
        pid = int(r["playerId"])
        out[pid] = {
            "id": pid,
            "name": r.get("goalieFullName", ""),
            "team": current_team(r.get("teamAbbrevs", "")),
            "pos_nhl": "G",
            "gp": int(fnum(r.get("gamesPlayed"))),
            "starts": int(fnum(r.get("gamesStarted"))),
            "w": fnum(r.get("wins")), "sv": fnum(r.get("saves")),
            "ga": fnum(r.get("goalsAgainst")), "so": fnum(r.get("shutouts")),
            "sa": fnum(r.get("shotsAgainst")),
        }
    return out


def l14_fp(raw: dict, cfg: dict) -> dict[int, float]:
    """Fantasy points per game over the last-14-day window, by playerId (hits/blocks not in this feed)."""
    out = {}
    w = cfg["skater"]
    for r in raw["sk_l14"]:
        gp = fnum(r.get("gamesPlayed"))
        if gp <= 0:
            continue
        pts = fnum(r.get("goals")) * w["g"] + fnum(r.get("assists")) * w["a"] + \
            fnum(r.get("shPoints")) * w["shp"] + fnum(r.get("shots")) * w["sog"]
        out[int(r["playerId"])] = pts / gp
    w = cfg["goalie"]
    for r in raw["go_l14"]:
        gp = fnum(r.get("gamesPlayed"))
        if gp <= 0:
            continue
        pts = fnum(r.get("wins")) * w["w"] + fnum(r.get("saves")) * w["sv"] + \
            fnum(r.get("goalsAgainst")) * w["ga"] + fnum(r.get("shutouts")) * w["so"]
        out[int(r["playerId"])] = pts / gp
    return out


# ----------------------------------------------------------------------------- matching
class Matcher:
    """Pair a preseason row with an NHL stats row by name, with position and team as tie-breakers
    (the NHL has two Elias Petterssons on one team and two Sebastian Ahos)."""

    def __init__(self, actuals: dict[int, dict]):
        self.by_norm: dict[str, list] = defaultdict(list)
        self.by_last_team: dict[tuple, list] = defaultdict(list)
        self.by_last: dict[str, list] = defaultdict(list)
        self.used: set[int] = set()
        for a in actuals.values():
            n = norm(a["name"])
            a["_norm"] = n
            a["_bucket"] = pos_bucket({a["pos_nhl"]})
            self.by_norm[n].append(a)
            last = n.split()[-1] if n else ""
            self.by_last_team[(last, a["team"])].append(a)
            self.by_last[last].append(a)

    def _pick(self, pool: list, bucket: str, team: str):
        pool = [a for a in pool if a["_bucket"] == bucket and a["id"] not in self.used]
        if len(pool) > 1:
            same_team = [a for a in pool if a["team"] == team]
            pool = same_team or pool
        return pool[0] if len(pool) == 1 else None

    def match(self, name: str, team: str, elig: set[str]):
        n = norm(name)
        bucket = pos_bucket(elig)
        cand, how = self._pick(self.by_norm.get(n, []), bucket, team), "exact"
        if cand is None and n in ALIASES:
            cand, how = self._pick(self.by_norm.get(ALIASES[n], []), bucket, team), "alias"
        if cand is None:
            parts = n.split()
            last, first_i = (parts[-1], parts[0][:1]) if parts else ("", "")
            for pool, h in ((self.by_last_team.get((last, team), []), "last+team"),
                            (self.by_last.get(last, []), "last")):
                cand = self._pick([a for a in pool if a["_norm"].startswith(first_i)], bucket, team)
                if cand is not None:
                    how = h
                    break
        if cand is None:
            return None, "unmatched"
        self.used.add(cand["id"])
        return cand, how


# ----------------------------------------------------------------------------- build
def default_priors(prior: list[dict], cfg: dict) -> dict[str, dict]:
    """Per-game rates for a player we have no preseason projection for, by bucket F / D / G."""
    lo, hi = cfg["default_prior"]["skater_rank_window_10t"]
    glo, ghi = cfg["default_prior"]["goalie_rank_window_10t"]
    buckets: dict[str, list] = {"F": [], "D": [], "G": []}
    for p in prior:
        b = pos_bucket(p["elig"])
        r = p["rank_10t_pre"]
        if (b == "G" and glo <= r <= ghi) or (b != "G" and lo <= r <= hi):
            buckets[b].append(p)
    for b in buckets:                      # fall back to everyone in the bucket if the window is empty
        if not buckets[b]:
            buckets[b] = [p for p in prior if pos_bucket(p["elig"]) == b]
    out = {}
    for b, rows in buckets.items():
        stats = GO_STATS if b == "G" else SK_STATS
        out[b] = {s: (sum(p["rates"][s] for p in rows) / len(rows) if rows else 0.0) for s in stats}
    return out


def build(cfg: dict, prior: list[dict], raw: dict) -> dict:
    G = cfg["games_per_team"]
    wsk, wgo = cfg["skater"], cfg["goalie"]
    ksk, kgo = cfg["prior_strength_games"]["skater"], cfg["prior_strength_games"]["goalie"]
    k_avail = cfg["prior_strength_games"]["skater_availability"]
    k_share = cfg["prior_strength_games"]["goalie_start_share"]
    defaults = default_priors(prior, cfg)
    d_avail = cfg["default_prior"]["availability"]
    d_share = cfg["default_prior"]["goalie_start_share"]
    k_scale = cfg["default_prior"].get("k_scale", 1.0)

    team_gp = {s["team"]: s["gp"] for s in raw["standings"] if s.get("team")}
    tgp_vals = sorted(team_gp.values()) or [0]
    median_tgp = tgp_vals[len(tgp_vals) // 2]
    def tgp(team):
        return team_gp.get(team, median_tgp)

    sk_act, go_act = skater_actuals(raw), goalie_actuals(raw)
    l14 = l14_fp(raw, cfg)
    msk, mgo = Matcher(sk_act), Matcher(go_act)

    players: list[dict] = []
    unmatched_prior: list[str] = []
    match_how: dict[str, int] = defaultdict(int)

    def expected_share(prior_total: float, played: float, t_gp: int, remaining: int, k: float,
                       flagged: bool, default_rate: float, in_prior: bool) -> float:
        """Share of the team's remaining games a player is expected to play (or start, for goalies).

        The preseason number already priced in known injuries, so a flagged stash who hasn't
        played yet keeps the games the prior still owes him. Everyone else: the prior's rate,
        capped by what it still owes, blended with games played vs team games so far.
        """
        if not in_prior:
            return blend(default_rate, k, played, t_gp)
        owed = max(0.0, prior_total - played)
        prior_rest = min(1.0, owed / remaining) if remaining else 0.0
        if flagged and played == 0:
            return prior_rest
        return blend(min(prior_total / G, prior_rest), k, played, t_gp)

    def make_row(p, act, in_prior, how):
        is_g = p["is_g"]
        team = act["team"] if act else p["team"]
        t_gp = tgp(team)
        remaining = max(0, G - t_gp)
        gp = act["gp"] if act else 0
        kscale = 1.0 if in_prior else k_scale
        row = {
            "player": p["player"], "team": team, "pos": p["pos"], "elig": p["elig"], "is_g": is_g,
            "gp": gp, "team_gp": t_gp, "in_prior": "Y" if in_prior else "N", "match": how,
            "fp_l14_pg": l14.get(act["id"]) if act else None,
        }
        notes = []
        if not in_prior:
            notes.append("no preseason projection; position default used as prior")
        if act is None and t_gp >= 3:
            notes.append(f"no games yet (team has played {t_gp})")
        elif act and t_gp >= 3 and gp <= t_gp - 3:
            notes.append(f"missed {t_gp - gp} of {t_gp} team games")

        if is_g:
            rates = {s: blend(p["rates"][s], kgo[s] * kscale, act[s] if act else 0.0, gp) for s in GO_STATS}
            share = expected_share(p["proj_gp"], act["starts"] if act else 0, t_gp, remaining, k_share,
                                   p.get("ir_flag", False), d_share, in_prior)
            row.update({
                "fp_per_game": fp(rates, wgo),
                "fp_prior_pg": fp(p["rates"], wgo),
                "fp_actual_pg": (fp({s: act[s] for s in GO_STATS}, wgo) / gp) if act and gp else None,
                "proj_games_rest": share * remaining,
                "start_share": share,
                "starts": act["starts"] if act else 0,
                "rates": rates,
                "totals": {s: act[s] for s in GO_STATS} if act else {s: 0.0 for s in GO_STATS},
                "sv_pct": (act["sv"] / act["sa"]) if act and act["sa"] else None,
            })
        else:
            rates = {s: blend(p["rates"][s], ksk[s] * kscale, act[s] if act else 0.0, gp) for s in SK_STATS}
            avail = expected_share(p["proj_gp"], gp, t_gp, remaining, k_avail, p.get("ir_flag", False),
                                   d_avail[pos_bucket(p["elig"])], in_prior)
            if act and not act["has_rt"]:
                notes.append("no hit/block data this run")
            row.update({
                "fp_per_game": fp(rates, wsk),
                "fp_prior_pg": fp(p["rates"], wsk),
                "fp_actual_pg": (fp({s: act[s] for s in SK_STATS}, wsk) / gp) if act and gp else None,
                "proj_games_rest": avail * remaining,
                "availability": avail,
                "rates": rates,
                "totals": {s: act[s] for s in SK_STATS} if act else {s: 0.0 for s in SK_STATS},
            })
        row["proj_fp_rest"] = row["fp_per_game"] * row["proj_games_rest"]
        row["note"] = "; ".join(notes)
        return row

    for p in prior:
        act, how = (mgo if p["is_g"] else msk).match(p["player"], p["team"], p["elig"])
        match_how[how] += 1
        if act is None:
            unmatched_prior.append(p["player"])
        players.append(make_row(p, act, True, how))

    # Players with NHL games but no preseason projection.
    for actuals, matcher, is_g in ((sk_act, msk, False), (go_act, mgo, True)):
        for a in actuals.values():
            if a["id"] in matcher.used:
                continue
            pos = a["pos_nhl"]
            elig = {pos}
            bucket = "G" if is_g else pos_bucket(elig)
            p = {"player": a["name"], "team": a["team"], "pos": pos, "elig": elig, "is_g": is_g,
                 "proj_gp": 0.0, "rank_10t_pre": 9999, "ir_flag": False, "totals": {},
                 "rates": dict(defaults[bucket])}
            players.append(make_row(p, a, False, "new"))

    for n in (8, 10, 12):
        vor_ranks(players, n, cfg["roster_slots"])

    return {
        "players": players,
        "unmatched_prior": unmatched_prior,
        "match_how": dict(match_how),
        "team_gp": team_gp,
        "n_new": sum(1 for p in players if p["in_prior"] == "N"),
    }


def vor_ranks(players: list[dict], n_teams: int, roster: dict) -> None:
    """Value over replacement for an n-team league, on rest-of-season points. Greedy starter fill, then Util."""
    slots = {pos: roster[pos] * n_teams for pos in ("C", "LW", "RW", "D", "G")}
    util = roster.get("UTIL", 0) * n_teams
    order = sorted(players, key=lambda p: p["proj_fp_rest"], reverse=True)
    assigned: set[int] = set()
    for p in order:
        placed = False
        for pos in sorted(p["elig"], key=lambda x: -slots.get(x, 0)):
            if slots.get(pos, 0) > 0:
                slots[pos] -= 1
                placed = True
                break
        if not placed and not p["is_g"] and util > 0:
            util -= 1
            placed = True
        if placed:
            assigned.add(id(p))
    repl = {}
    for pos in slots:
        cands = [p["proj_fp_rest"] for p in order if id(p) not in assigned and pos in p["elig"]]
        repl[pos] = cands[0] if cands else 0.0
    key = f"vor_{n_teams}t"
    for p in players:
        p[key] = p["proj_fp_rest"] - min(repl[e] for e in p["elig"] if e in repl)
    for i, p in enumerate(sorted(players, key=lambda p: p[key], reverse=True), 1):
        p[f"rank_{n_teams}t"] = i


# ----------------------------------------------------------------------------- output
def r2(v):
    return "" if v is None else f"{v:.2f}"


def r3(v):
    return "" if v is None else f"{v:.3f}"


OUT_COLS = ["rank_10t", "rank_8t", "rank_12t", "player", "team", "pos", "gp", "team_gp",
            "fp_per_game", "fp_actual_pg", "fp_prior_pg", "fp_l14_pg", "proj_games_rest", "proj_fp_rest",
            "vor_10t", "g_pg", "a_pg", "shp_pg", "sog_pg", "hit_pg", "blk_pg", "g", "a", "shp", "sog", "hit", "blk",
            "starts", "start_share", "w_pg", "sv_pg", "ga_pg", "so_pg", "w", "sv", "ga", "so", "sv_pct",
            "in_prior", "note"]


def write_projections(players: list[dict], path: Path) -> None:
    rows = sorted(players, key=lambda p: p["rank_10t"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(OUT_COLS)
        for p in rows:
            sk = not p["is_g"]
            w.writerow([
                p["rank_10t"], p["rank_8t"], p["rank_12t"], p["player"], p["team"], p["pos"], p["gp"], p["team_gp"],
                r2(p["fp_per_game"]), r2(p["fp_actual_pg"]), r2(p["fp_prior_pg"]), r2(p["fp_l14_pg"]),
                r2(p["proj_games_rest"]), r2(p["proj_fp_rest"]), r2(p["vor_10t"]),
                *[r3(p["rates"][s]) if sk else "" for s in SK_STATS],
                *[f"{p['totals'][s]:g}" if sk else "" for s in SK_STATS],
                "" if sk else p["starts"], "" if sk else r3(p["start_share"]),
                *[r3(p["rates"][s]) if not sk else "" for s in GO_STATS],
                *[f"{p['totals'][s]:g}" if not sk else "" for s in GO_STATS],
                "" if sk else r3(p["sv_pct"]),
                p["in_prior"], p["note"],
            ])


def write_actuals(raw: dict, cfg: dict, out_dir: Path) -> None:
    wsk, wgo = cfg["skater"], cfg["goalie"]
    with open(out_dir / "actuals_skaters.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["player", "team", "pos", "gp", "g", "a", "shp", "sog", "hit", "blk", "fp", "fp_pg"])
        for a in sorted(skater_actuals(raw).values(), key=lambda a: a["name"]):
            pts = fp({s: a[s] for s in SK_STATS}, wsk)
            w.writerow([a["name"], a["team"], a["pos_nhl"], a["gp"], *[f"{a[s]:g}" for s in SK_STATS],
                        r2(pts), r2(pts / a["gp"]) if a["gp"] else ""])
    with open(out_dir / "actuals_goalies.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["player", "team", "gp", "starts", "w", "sv", "ga", "so", "sv_pct", "fp", "fp_pg"])
        for a in sorted(goalie_actuals(raw).values(), key=lambda a: a["name"]):
            pts = fp({s: a[s] for s in GO_STATS}, wgo)
            w.writerow([a["name"], a["team"], a["gp"], a["starts"], *[f"{a[s]:g}" for s in GO_STATS],
                        r3(a["sv"] / a["sa"]) if a["sa"] else "", r2(pts), r2(pts / a["gp"]) if a["gp"] else ""])


def central_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago"))
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc)


def write_summary(result: dict, raw: dict, cfg: dict, path: Path) -> None:
    ps = result["players"]
    sk = [p for p in ps if not p["is_g"]]
    go = [p for p in ps if p["is_g"]]
    tg = result["team_gp"].values()
    now_c = central_now()
    fetched = raw["meta"].get("fetched_at_utc", "unknown")
    lines = []
    L = lines.append
    L("# Projections summary")
    L("")
    L(f"Built {now_c:%a %b %d, %Y %I:%M %p} Central (data fetched {fetched} UTC). "
      f"Team games played: {min(tg) if tg else 0} to {max(tg) if tg else 0} of {cfg['games_per_team']}. "
      f"Goalie GA scored at {cfg['goalie']['ga']:g}.")
    L(f"Players: {len(ps)} ({result['n_new']} without a preseason projection). "
      f"Prior matches: {result['match_how']}.")
    L("")

    def tbl(title, rows, cols):
        L(f"## {title}")
        L("")
        L("| " + " | ".join(c[0] for c in cols) + " |")
        L("|" + "---|" * len(cols))
        for p in rows:
            L("| " + " | ".join(str(c[1](p)) for c in cols) + " |")
        L("")

    pg = lambda p: r2(p["fp_per_game"])  # noqa: E731
    base = [("Player", lambda p: p["player"]), ("Team", lambda p: p["team"]), ("Pos", lambda p: p["pos"]),
            ("GP", lambda p: p["gp"]), ("FP/G", pg), ("Pre", lambda p: r2(p["fp_prior_pg"])),
            ("Actual", lambda p: r2(p["fp_actual_pg"])), ("L14", lambda p: r2(p["fp_l14_pg"])),
            ("Rest", lambda p: f"{p['proj_fp_rest']:.0f}"), ("Rk10", lambda p: p["rank_10t"])]

    tbl("Top 30 skaters, blended points per game", sorted(sk, key=lambda p: -p["fp_per_game"])[:30], base)
    tbl("Top 12 goalies by rest-of-season value (workload counts; FP/G is per appearance)",
        sorted(go, key=lambda p: -p["proj_fp_rest"])[:12],
        base[:5] + [("Pre", lambda p: r2(p["fp_prior_pg"])), ("Actual", lambda p: r2(p["fp_actual_pg"])),
                    ("Starts", lambda p: p["starts"]), ("Share", lambda p: r2(p["start_share"])),
                    ("Rest", lambda p: f"{p['proj_fp_rest']:.0f}"), ("Rk10", lambda p: p["rank_10t"])])

    min_gp = 3 if max(tg, default=0) < 15 else 5
    movers = [p for p in ps if p["in_prior"] == "Y" and p["gp"] >= min_gp]
    delta = [("Player", lambda p: p["player"]), ("Team", lambda p: p["team"]), ("Pos", lambda p: p["pos"]),
             ("GP", lambda p: p["gp"]), ("FP/G now", pg), ("Pre", lambda p: r2(p["fp_prior_pg"])),
             ("Change", lambda p: f"{p['fp_per_game'] - p['fp_prior_pg']:+.2f}"), ("Rk10", lambda p: p["rank_10t"])]
    tbl(f"Risers vs preseason (min {min_gp} GP)", sorted(movers, key=lambda p: -(p["fp_per_game"] - p["fp_prior_pg"]))[:15], delta)
    tbl(f"Fallers vs preseason (min {min_gp} GP)", sorted(movers, key=lambda p: (p["fp_per_game"] - p["fp_prior_pg"]))[:15], delta)

    new = sorted([p for p in ps if p["in_prior"] == "N" and p["gp"] >= min_gp], key=lambda p: -p["fp_per_game"])[:20]
    tbl(f"Not in the preseason file, min {min_gp} GP (prior = position default, so treat with care)", new, base)

    missing = sorted([p for p in ps if p["note"].startswith(("missed", "no games")) and p["rank_10t"] <= 200],
                     key=lambda p: p["rank_10t"])
    tbl("Top-200 players missing games", missing,
        [("Player", lambda p: p["player"]), ("Team", lambda p: p["team"]), ("Pos", lambda p: p["pos"]),
         ("GP", lambda p: p["gp"]), ("Team GP", lambda p: p["team_gp"]), ("Rk10", lambda p: p["rank_10t"]),
         ("Note", lambda p: p["note"])])

    L("## Preseason names that didn't match an NHL player")
    L("")
    L("Expected for players who haven't played yet. A name that keeps showing up here after the player has "
      "games probably needs an alias in build.py.")
    L("")
    L(", ".join(result["unmatched_prior"]) if result["unmatched_prior"] else "(none)")
    L("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    ap.add_argument("--out-dir", default=str(ROOT / "data"))
    ap.add_argument("--prior", default=str(ROOT / "prior" / "projections_preseason.csv"))
    ap.add_argument("--config", default=str(ROOT / "scoring.json"))
    ap.add_argument("--allow-empty", action="store_true", help="build even with no NHL data (preseason)")
    args = ap.parse_args()

    cfg = load_cfg(Path(args.config))
    prior = load_prior(Path(args.prior))
    raw = load_raw(Path(args.raw_dir))
    if not args.allow_empty and (not raw["sk_sum"] or not raw["standings"]):
        print("error: no NHL data in raw dir (run nhl_fetch.py first, or pass --allow-empty)", file=sys.stderr)
        return 1

    result = build(cfg, prior, raw)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_projections(result["players"], out / "projections.csv")
    write_actuals(raw, cfg, out)
    write_summary(result, raw, cfg, out / "summary.md")
    tg = list(result["team_gp"].values())
    (out / "last_run.json").write_text(json.dumps({
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched_at_utc": raw["meta"].get("fetched_at_utc"),
        "players": len(result["players"]),
        "new_players": result["n_new"],
        "unmatched_prior": result["unmatched_prior"],
        "match_how": result["match_how"],
        "team_gp_min": min(tg) if tg else 0,
        "team_gp_max": max(tg) if tg else 0,
        "goalie_ga_points": cfg["goalie"]["ga"],
    }, indent=1))
    print(f"wrote {len(result['players'])} players; unmatched prior: {len(result['unmatched_prior'])}; "
          f"new: {result['n_new']}; matches: {result['match_how']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Snapshot Ben's Yahoo team for the hockey routines (stdlib only).

Writes data/yahoo/snapshot.json: today's and tomorrow's lineup (slot, eligible
positions, NHL team, injury status), this week's matchup score, adds used, and
goalie appearances so far this matchup week. The routines read it from
raw.githubusercontent.com, so they no longer need Chrome to see the roster.

Needs three repo secrets (see README, "Yahoo snapshot"):
  YAHOO_CLIENT_ID, YAHOO_CLIENT_SECRET, YAHOO_REFRESH_TOKEN
Without them it prints a notice and exits 0, so the workflow stays green.

Usage: python yahoo_fetch.py [--out data/yahoo/snapshot.json]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
LEAGUE_ID = "145812"
TEAM_ID = "4"
TZ = ZoneInfo("America/Chicago")
API = "https://fantasysports.yahooapis.com/fantasy/v2"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
UA = "kg-league-projections/1.0 (+https://github.com/Batkins44/kg-league-projections)"
MIN_GOALIE_APPEARANCES = 3
DEFAULT_WEEKLY_ADDS = 5


def access_token(client_id: str, secret: str, refresh: str) -> str:
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh,
                                   "redirect_uri": "oob"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]


def get(path: str, token: str, tries: int = 3) -> dict:
    url = f"{API}/{path}{'&' if '?' in path else '?'}format=json"
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)["fantasy_content"]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {path}: {last}")


# Yahoo's JSON wraps objects as lists of one-key dicts, and collections as
# {"0": {...}, "1": {...}, "count": N}. These two helpers undo that.

def merge(node) -> dict:
    """Flatten a Yahoo list-of-fragments into one dict."""
    out: dict = {}
    if isinstance(node, dict):
        return dict(node)
    for part in node or []:
        if isinstance(part, list):
            out.update(merge(part))
        elif isinstance(part, dict):
            out.update(part)
    return out


def items(coll, key: str) -> list:
    """The entries of a Yahoo collection, e.g. items(players, 'player').

    Most collections come as {"0": {...}, "1": {...}, "count": N}, but some
    (player_stats.stats) come as a plain list of {"stat": {...}}; take both.
    """
    if isinstance(coll, list):
        return [c[key] for c in coll if isinstance(c, dict) and key in c]
    if not isinstance(coll, dict):
        return []
    return [coll[k][key] for k in sorted((k for k in coll if k.isdigit()), key=int)]


def num(v) -> float:
    """A Yahoo stat value as a number; '-' and blanks (no game) are 0."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_player(raw) -> dict:
    p = merge(raw)
    slot = merge(p.get("selected_position", [])).get("position")
    elig = [e.get("position") for e in p.get("eligible_positions", []) if isinstance(e, dict)]
    stats = {}
    for s in items(p.get("player_stats", {}).get("stats", {}) if isinstance(p.get("player_stats"), dict) else {}, "stat"):
        stats[str(s.get("stat_id"))] = s.get("value")
    return {
        "name": (p.get("name") or {}).get("full"),
        "player_key": p.get("player_key"),
        "nhl_team": (p.get("editorial_team_abbr") or "").upper(),
        "position_type": p.get("position_type"),
        "eligible": elig,
        "slot": slot,
        "status": p.get("status") or None,
        "injury_note": p.get("injury_note") or None,
        "stats": stats,
    }


def roster(team_key: str, day: date, token: str, with_stats: bool = False) -> list[dict]:
    path = f"team/{team_key}/roster;date={day.isoformat()}"
    if with_stats:
        path += f"/players/stats;type=date;date={day.isoformat()}"
    team = get(path, token)["team"]
    ros = team[1]["roster"]
    players = ros["0"]["players"] if "0" in ros else ros.get("players", {})
    return [parse_player(p) for p in items(players, "player")]


def goalie_appearances(team_key: str, start: date, through: date, sv_id: str, ga_id: str, token: str) -> dict:
    """Goalies in a G slot who faced a shot, per day, Monday through yesterday.

    stats_missing lists days where Yahoo sent a G-slot goalie with no stats at
    all; on those days the count can't be trusted.
    """
    days, total, missing, d = [], 0, [], start
    while d <= through:
        names = []
        for p in roster(team_key, d, token, with_stats=True):
            if p["slot"] != "G":
                continue
            if not p["stats"]:
                if d.isoformat() not in missing:
                    missing.append(d.isoformat())
                continue
            if num(p["stats"].get(sv_id)) + num(p["stats"].get(ga_id)) > 0:
                names.append(p["name"])
        total += len(names)
        days.append({"date": d.isoformat(), "goalies": names})
        d += timedelta(days=1)
    return {"count": total, "minimum": MIN_GOALIE_APPEARANCES, "by_day": days, "stats_missing": missing}


def matchup(league_key: str, week: int, token: str) -> dict:
    board = merge(get(f"league/{league_key}/scoreboard;week={week}", token)["league"])["scoreboard"]
    for m in items(board["0"]["matchups"], "matchup"):
        teams = [merge(t) for t in items(m["0"]["teams"], "team")]
        if any(t.get("team_id") == TEAM_ID for t in teams):
            def side(t):
                return {"name": t.get("name"),
                        "points": (t.get("team_points") or {}).get("total"),
                        "projected": (t.get("team_projected_points") or {}).get("total")}
            me = next(t for t in teams if t.get("team_id") == TEAM_ID)
            opp = next((t for t in teams if t.get("team_id") != TEAM_ID), {})
            return {"week_start": m.get("week_start"), "week_end": m.get("week_end"),
                    "status": m.get("status"), "me": side(me), "opponent": side(opp)}
    return {}


def league_settings(league_key: str, token: str) -> dict:
    return merge(merge(get(f"league/{league_key}/settings", token)["league"])["settings"])


def weekly_add_limit(settings: dict) -> int:
    """The league's weekly add cap; 5 per the Yahoo settings page (Oct 6) if the API omits it."""
    v = settings.get("max_weekly_adds")
    return int(v) if str(v or "").isdigit() and int(v) > 0 else DEFAULT_WEEKLY_ADDS


def stat_ids(settings: dict) -> dict:
    cats = settings.get("stat_categories", {}).get("stats", [])
    out = {}
    for c in cats:
        s = c.get("stat", {})
        out[f"{s.get('position_type')}:{s.get('display_name')}"] = str(s.get("stat_id"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "data" / "yahoo" / "snapshot.json"))
    args = ap.parse_args()
    creds = [os.environ.get(k, "") for k in ("YAHOO_CLIENT_ID", "YAHOO_CLIENT_SECRET", "YAHOO_REFRESH_TOKEN")]
    if not all(creds):
        print("notice: Yahoo secrets not set; skipping the Yahoo snapshot")
        return 0

    token = access_token(*creds)
    game_key = merge(get("game/nhl", token)["game"])["game_key"]
    league_key = f"{game_key}.l.{LEAGUE_ID}"
    team_key = f"{league_key}.t.{TEAM_ID}"

    team = merge(get(f"team/{team_key}", token)["team"])
    league = merge(get(f"league/{league_key}", token)["league"])
    week = int(league.get("current_week") or team.get("roster_adds", {}).get("coverage_value") or 0)
    settings = league_settings(league_key, token)
    ids = stat_ids(settings)

    now = datetime.now(TZ)
    today = now.date()
    snap = {
        "fetched_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fetched_at_central": now.isoformat(timespec="minutes"),
        "league_key": league_key,
        "team": {"name": team.get("name"), "waiver_priority": team.get("waiver_priority"),
                 "adds_this_week": (team.get("roster_adds") or {}).get("value"), "adds_max": weekly_add_limit(settings)},
        "week": week,
        "matchup": {},
        "lineup": {},
        "goalie_appearances": None,
        "errors": [],
    }
    try:
        snap["matchup"] = matchup(league_key, week, token)
    except Exception as e:  # noqa: BLE001
        snap["errors"].append(f"matchup: {e}")
    for day in (today, today + timedelta(days=1)):
        try:
            snap["lineup"][day.isoformat()] = [{k: v for k, v in p.items() if k != "stats"}
                                               for p in roster(team_key, day, token)]
        except Exception as e:  # noqa: BLE001
            snap["errors"].append(f"lineup {day}: {e}")
    try:
        start = date.fromisoformat(snap["matchup"]["week_start"]) if snap["matchup"].get("week_start") \
            else today - timedelta(days=today.weekday())
        sv_id, ga_id = ids.get("G:SV"), ids.get("G:GA")
        if not (sv_id and ga_id):
            raise RuntimeError(f"SV/GA stat ids not in league settings: {sorted(ids)}")
        snap["goalie_appearances"] = goalie_appearances(team_key, start, today - timedelta(days=1),
                                                        sv_id, ga_id, token)
        snap["goalie_appearances"]["note"] = "Monday through yesterday; today's games aren't counted yet"
        if snap["goalie_appearances"]["stats_missing"]:
            snap["errors"].append("goalie appearances: Yahoo sent no stats for a G-slot goalie on "
                                  + ", ".join(snap["goalie_appearances"]["stats_missing"])
                                  + "; the count may be low")
    except Exception as e:  # noqa: BLE001
        snap["errors"].append(f"goalie appearances: {e}")

    if not snap["lineup"]:
        print("error: no lineup read; refusing to write", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap, indent=1, ensure_ascii=False) + "\n")
    ga = snap["goalie_appearances"] or {}
    print(json.dumps({"week": week, "days": list(snap["lineup"]), "adds": [snap["team"]["adds_this_week"],
                      snap["team"]["adds_max"]], "goalie_appearances": ga.get("count"),
                      "goalies_by_day": {d["date"]: d["goalies"] for d in ga.get("by_day", [])},
                      "errors": snap["errors"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

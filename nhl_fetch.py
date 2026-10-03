#!/usr/bin/env python3
"""Pull current-season NHL stats into data/raw/ (stdlib only).

Sources (all public, no login):
  api.nhle.com/stats/rest/en/skater/summary   goals, assists, shots, SH points
  api.nhle.com/stats/rest/en/skater/realtime  hits, blocked shots
  api.nhle.com/stats/rest/en/goalie/summary   starts, wins, saves, GA, shutouts
  api-web.nhle.com/v1/standings/now           games played per team

Usage: python nhl_fetch.py [--raw-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "scoring.json").read_text())
SEASON = CFG["season"]
STATS = "https://api.nhle.com/stats/rest/en"
WEB = "https://api-web.nhle.com/v1"
UA = "Mozilla/5.0 (compatible; kg-league-projections/1.0; +https://github.com/Batkins44/kg-league-projections)"
PAGE = 100


def get_json(url: str, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {last}")


def report(kind: str, name: str, extra_exp: str = "", aggregate: bool = False) -> list[dict]:
    """Page through one stats-API report. kind is 'skater' or 'goalie'."""
    exp = f"seasonId={SEASON} and gameTypeId=2"
    if extra_exp:
        exp += f" and {extra_exp}"
    rows: list[dict] = []
    start, total = 0, None
    while total is None or start < total:
        q = {
            "isAggregate": "true" if aggregate else "false",
            "isGame": "false",
            "start": start,
            "limit": PAGE,
            "sort": json.dumps([{"property": "playerId", "direction": "ASC"}]),
            "cayenneExp": exp,
        }
        payload = get_json(f"{STATS}/{kind}/{name}?" + urllib.parse.urlencode(q))
        data = payload.get("data", [])
        total = int(payload.get("total", 0))
        rows.extend(data)
        if not data:
            break
        start += len(data)
    return rows


def standings() -> list[dict]:
    rows = get_json(f"{WEB}/standings/now").get("standings", [])
    out = []
    for r in rows:
        abbrev = r.get("teamAbbrev", {})
        abbrev = abbrev.get("default") if isinstance(abbrev, dict) else abbrev
        out.append({"team": abbrev, "gp": int(r.get("gamesPlayed", 0)),
                    "w": int(r.get("wins", 0)), "l": int(r.get("losses", 0)), "otl": int(r.get("otLosses", 0))})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default=str(ROOT / "data" / "raw"))
    args = ap.parse_args()
    raw = Path(args.raw_dir)
    raw.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    l14_end = (now - timedelta(hours=10)).date()          # games through last night
    l14_start = l14_end - timedelta(days=CFG["last14_days"] - 1)
    window = f'gameDate>="{l14_start.isoformat()}" and gameDate<="{l14_end.isoformat()} 23:59:59"'

    out = {}
    out["skaters_summary"] = report("skater", "summary")
    out["skaters_realtime"] = report("skater", "realtime")
    out["goalies_summary"] = report("goalie", "summary")
    out["standings"] = standings()
    for key, kind in (("skaters_l14", "skater"), ("goalies_l14", "goalie")):
        try:
            out[key] = report(kind, "summary", window, aggregate=True)
        except Exception as e:  # noqa: BLE001
            print(f"warning: {key} failed ({e}); continuing without it", file=sys.stderr)
            out[key] = []

    if not out["skaters_summary"] or not out["standings"]:
        print("error: empty skater summary or standings; refusing to write", file=sys.stderr)
        return 1

    for key, rows in out.items():
        (raw / f"{key}.json").write_text(json.dumps(rows, separators=(",", ":")))
    meta = {
        "fetched_at_utc": now.isoformat(timespec="seconds"),
        "season": SEASON,
        "l14_window": [l14_start.isoformat(), l14_end.isoformat()],
        "counts": {k: len(v) for k, v in out.items()},
    }
    (raw / "meta.json").write_text(json.dumps(meta, indent=1))
    print(json.dumps(meta["counts"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())

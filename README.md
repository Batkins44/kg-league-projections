# kg-league-projections

Self-updating fantasy hockey projections for KG's league (Yahoo, 2026-27, head-to-head points).
A GitHub Action pulls the NHL's public stats every morning, blends them with the preseason
projections, and commits the result here. Apart from `data/yahoo/` (see "Yahoo snapshot"), nothing
in this repo is personal; it is NHL stats and math.

## The files that matter

| File | What it is |
|---|---|
| `data/projections.csv` | One row per player: blended points per game, rest-of-season value, ranks for 8/10/12-team leagues, per-stat rates and totals. Sorted by `rank_10t`. |
| `data/summary.md` | Human-readable: top players, risers and fallers vs preseason, players not in the preseason file, players missing games, names that failed to match. |
| `data/actuals_skaters.csv`, `data/actuals_goalies.csv` | Season-to-date totals with fantasy points under this league's scoring. |
| `data/last_run.json` | When it last ran and the counts. If `fetched_at_utc` is more than two days old, the Action is broken. |
| `prior/projections_preseason.csv` | The preseason projections (frozen Sep 30, 2026). The blend fades these out as games pile up. |
| `scoring.json` | League scoring, roster slots, and the blend settings. Edit this if the commissioner changes scoring. |

Always-fresh links:

- https://raw.githubusercontent.com/Batkins44/kg-league-projections/main/data/projections.csv
- https://raw.githubusercontent.com/Batkins44/kg-league-projections/main/data/summary.md
- https://raw.githubusercontent.com/Batkins44/kg-league-projections/main/data/last_run.json

## How the blend works

For every stat, per player:

```
rate = (K * preseason_rate + actual_total) / (K + games_played)
```

`K` is how many games the preseason projection is worth (`scoring.json` -> `prior_strength_games`).
After `K` games the actuals carry half the weight. Shots, hits and blocks stabilize fast, so they
get a small `K` (10 to 15 games); goals and assists are noisy, so they get a big one (35 to 40).
Goalies work the same way per appearance, plus a separate blend for their share of the team's
starts, which is most of a goalie's value in this scoring.

Then:

- `fp_per_game` = blended rates x scoring weights. This is the number to use for weekly lineup and
  waiver calls.
- `proj_games_rest` = expected remaining games = (84 - team games played) x availability, where
  availability blends the preseason games projection with actual games played vs team games.
  Players the preseason file flagged as injured stashes (Bedard, Jarvis, Faber, etc.) keep the games
  the preseason still owes them until they play; the absence was already priced in.
- `proj_fp_rest` = `fp_per_game` x `proj_games_rest`. This is the number for trade value.
- `rank_10t` / `vor_10t` = value over replacement in a 10-team league with this roster
  (2C, 2LW, 2RW, 4D, 1 Util, 2G), on rest-of-season points. 8- and 12-team ranks are there too.

Players who have NHL games but no preseason projection (rookies, call-ups, guys the preseason file
skipped) get a bottom-of-roster prior for their position, a prior that fades twice as fast, and
`in_prior = N`. Treat their early numbers with care.

## Scoring

Skaters: G 4.5, A 3, SHP 2 (bonus on top of the point), SOG 0.5, HIT 0.25, BLK 0.5.
Goalies: W 3, SV 0.3, SO 3, GA -1.

The preseason file was built with GA at -1.5; the Oct 1 league notes say -1. Every number here is
recomputed from component stats under `scoring.json`, so changing one value there and re-running
fixes everything.

## Yahoo snapshot

`yahoo_fetch.py` writes `data/yahoo/snapshot.json`: today's and tomorrow's lineup (slot, eligible
positions, NHL team, injury status), this week's matchup score, adds used out of 4, and goalie
appearances so far this matchup week against the 3-start minimum. The hockey routines read it from
https://raw.githubusercontent.com/Batkins44/kg-league-projections/main/data/yahoo/snapshot.json, so they
can see the roster without a browser. The "Yahoo snapshot" workflow refreshes it six times a day.

This repo is public, so the snapshot is too: anyone with the link can see the lineup.

One-time setup:

1. At https://developer.yahoo.com/apps/create/ make an app with Fantasy Sports read access and
   redirect URI `oob`. Note the client ID and secret.
2. On your own machine: `python yahoo_auth.py CLIENT_ID CLIENT_SECRET`, open the link, approve,
   paste the code back. It prints a refresh token.
3. Under Settings > Secrets and variables > Actions, add `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`
   and `YAHOO_REFRESH_TOKEN`.
4. Actions > "Yahoo snapshot" > Run workflow, and check the file it commits.

Until the secrets exist the workflow skips the fetch and stays green. Anything that failed on a
run is listed in the snapshot's `errors`; goalie appearances count G-slot goalies who faced a shot,
Monday through yesterday.

## Running it by hand

```
python nhl_fetch.py      # pulls to data/raw/ (not committed)
python build.py          # writes data/
python -m unittest discover -s tests -v
```

Or open the Actions tab and press "Run workflow" on "Refresh projections".

## Sources

- https://api.nhle.com/stats/rest/en/skater/summary, `.../skater/realtime`, `.../goalie/summary`
- https://api-web.nhle.com/v1/standings/now

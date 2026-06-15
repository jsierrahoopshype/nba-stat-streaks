"""
export_active_state.py — Task 2 / Stage 1.

One-time seed of the daily pipeline's state file. Runs the full engine and, for
every streak type x scope, finds each player's CURRENT ACTIVE streak — the run of
consecutive qualifying appearances ending at the player's most recent game — and
keeps the ones that (a) reach into the current 2025-26 season and (b) clear a
per-type floor (so the file stays small).

Writes active-state.json. Build the nightly job separately.
"""
import os
import json

import numpy as np

import build_streaks as E
import build_site as BS

BASE = r"C:\nba-stat-streaks"


def nba_season(d):
    """NBA season-start year for a date (season runs Oct–Jun, so Jan-Jun belong to
    the prior calendar year). 2026-01-15 -> 2025 (the 2025-26 season)."""
    return d.year if d.month >= 7 else d.year - 1


# per-type floors (keep the state file small)
FLOORS = {
    "pts10": 10, "pts20": 5, "pts30": 3, "pts40": 2,
    "reb10": 3, "reb15": 3, "reb20": 3,
    "ast5": 5, "ast10": 5, "ast15": 5,
    "stl2": 5, "stl3": 5, "blk2": 5, "blk3": 5, "tpm3": 5, "tpm5": 5,
    "dd": 3, "td": 3, "p20r10": 3, "p30r5a5": 3,
}


def trailing_active(sub, gkey, cond_col, floor, current_season):
    """Each player's current trailing run of consecutive True in cond_col, i.e. the
    run that ends at their most recent appearance — IF that last game qualifies and
    falls in the current season and the run length clears the floor."""
    c = sub[cond_col].to_numpy(dtype=bool)
    n = len(c)
    if n == 0:
        return []
    same_prev = np.empty(n, bool); same_prev[0] = False; same_prev[1:] = gkey[1:] == gkey[:-1]
    prev_c = np.empty(n, bool); prev_c[0] = False; prev_c[1:] = c[:-1]
    run_start = c & ~(prev_c & same_prev)
    run_id = np.cumsum(run_start)
    counts = np.bincount(run_id[c]) if c.any() else np.array([0])

    is_last = np.empty(n, bool); is_last[-1] = True; is_last[:-1] = gkey[1:] != gkey[:-1]
    out = []
    for i in np.where(is_last)[0]:
        if not c[i]:
            continue
        d = sub["gameDate"].iloc[i].date()
        if nba_season(d) != current_season:
            continue
        length = int(counts[run_id[i]])
        if length < floor:
            continue
        # the trailing run occupies contiguous positions i-length+1 .. i
        started = sub["gameDate"].iloc[i - length + 1].date().isoformat()
        out.append((int(sub["personId"].iloc[i]), length, d.isoformat(), str(sub["team_disp"].iloc[i]), started))
    return out


def main():
    print("Loading appearances…", flush=True)
    df = E.load_appearances()
    current_season = nba_season(df["gameDate"].max().date())
    data_through = df["gameDate"].max().date().isoformat()
    season_str = f"{current_season}-{(current_season + 1) % 100:02d}"
    print(f"Current season: {season_str}  (data through {data_through})", flush=True)

    pid2name = df.drop_duplicates("personId").set_index("personId")["name"].to_dict()
    label_by_id = {s["id"]: s["label"] for s in E.STREAKS}

    streaks = []
    for scope_key, _ in BS.SCOPES:
        sub = E.scope_df(df, scope_key).sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True)
        gkey = sub["personId"].to_numpy()
        for s in E.STREAKS:
            sid = s["id"]
            for pid, length, last_date, team, started in trailing_active(sub, gkey, sid, FLOORS[sid], current_season):
                name = pid2name.get(pid, f"Player {pid}")
                streaks.append({
                    "personId": pid, "player": name, "slug": BS.slugify(name, pid),
                    "type": sid, "label": label_by_id[sid], "scope": scope_key,
                    "length": length, "last_date": last_date, "started": started, "team": team,
                })

    streaks.sort(key=lambda r: r["length"], reverse=True)
    state = {
        "season": season_str,
        "current_season_year": current_season,
        "data_through": data_through,
        "floors": FLOORS,
        "count": len(streaks),
        "streaks": streaks,
    }
    path = os.path.join(BASE, "active-state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

    size = os.path.getsize(path)
    print(f"\nWrote active-state.json: {len(streaks)} active streaks, {size:,} bytes "
          f"({size/1024:.1f} KB)")
    by_scope = {}
    for r in streaks:
        by_scope[r["scope"]] = by_scope.get(r["scope"], 0) + 1
    print("  by scope:", by_scope)
    print("\n  Top 10 active streaks (player | type | scope | current length | last game):")
    for r in streaks[:10]:
        print(f"    {r['player']:<24} {r['label']:<22} {r['scope']:<9} {r['length']:>4}  {r['last_date']}  ({r['team']})")


if __name__ == "__main__":
    main()

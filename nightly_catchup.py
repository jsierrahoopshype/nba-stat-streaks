"""
nightly_catchup.py — apply every night active-state.json is missing, in order.

fetch_nightly.py applies ONE night per run and does not track which nights were
already applied: running it twice for the same night double-counts, skipping a day
silently drops that night's games. This wrapper reads data_through from
active-state.json and walks forward to last night (US Eastern):

  night with Regular Season / Playoff games -> python fetch_nightly.py --date D
  night without them (offseason, preseason, All-Star, play-in only)
                                            -> only advance data_through, so the
                                               Active Streaks page is not replaced
                                               by an empty night
  games not Final yet / fetch error         -> stop; state stays at the last good night

  python nightly_catchup.py              # normal daily run
  python nightly_catchup.py --allow-gap  # needed once when more than MAX_GAP_DAYS behind

Exit codes: 0 = done or already up to date, 1 = failed, 3 = big gap, rerun with --allow-gap.
"""
import os
import sys
import json
import argparse
import datetime
import subprocess
from datetime import timedelta

import fetch_nightly as F

MAX_GAP_DAYS = 7
# stats.nba.com GAME_ID prefixes that fetch_nightly actually pulls lines for
COUNTED_PREFIXES = ("002", "004")   # 002 = Regular Season, 004 = Playoffs


def read_state():
    with open(F.STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def write_state(state):
    # same format fetch_nightly writes
    with open(F.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def counted_games(d):
    rs = F.scoreboard_check(d)["raw"]
    gi = rs["headers"].index("GAME_ID")
    return sum(1 for row in rs["rowSet"] if str(row[gi]).startswith(COUNTED_PREFIXES))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-gap", action="store_true",
                    help=f"process a gap longer than {MAX_GAP_DAYS} days")
    a = ap.parse_args()

    last_night = F.eastern_yesterday()
    through = datetime.date.fromisoformat(read_state()["data_through"])
    if through >= last_night:
        print(f"Already up to date: active-state.json is through {through.isoformat()} "
              f"(last night = {last_night.isoformat()}). Nothing to fetch.")
        return 0

    gap = (last_night - through).days
    print(f"active-state.json is through {through.isoformat()}; last night = "
          f"{last_night.isoformat()} -> {gap} night(s) to process.")
    if gap > MAX_GAP_DAYS and not a.allow_gap:
        print(f"GAP: more than {MAX_GAP_DAYS} nights behind. Rerun with --allow-gap to catch up.")
        return 3

    d = through + timedelta(days=1)
    while d <= last_night:
        try:
            n = counted_games(d)
        except Exception as e:
            print(f"ERROR: scoreboard check for {d.isoformat()} failed: {e}")
            return 1
        if n == 0:
            state = read_state()
            state["data_through"] = d.isoformat()
            write_state(state)
            print(f"  {d.isoformat()}: no regular-season/playoff games -> skipped (state advanced)")
        else:
            print(f"  {d.isoformat()}: {n} game(s) -> running fetch_nightly.py", flush=True)
            rc = subprocess.call([sys.executable, os.path.join(F.BASE, "fetch_nightly.py"),
                                  "--date", d.isoformat()], cwd=F.BASE)
            if rc != 0:
                print(f"ERROR: fetch_nightly.py failed for {d.isoformat()} (exit {rc}).")
                return 1
            if read_state()["data_through"] != d.isoformat():
                print(f"STOPPED: {d.isoformat()} was not applied (games not all Final yet?). "
                      f"Try again later.")
                return 1
        d += timedelta(days=1)

    print(f"Caught up through {last_night.isoformat()}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

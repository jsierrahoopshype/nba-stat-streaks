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
  season file not republished for the night yet (fetch_nightly.NotReady)
                                            -> stop with exit 4; state stays at the
                                               last good night, the next run retries
  fetch error                               -> stop; state stays at the last good night

Game counts come from the nba-editorial-bots season file (fetch_nightly.games_on),
not the stats.nba.com scoreboard (blocked through nba-proxy since Oct 2026).

  python nightly_catchup.py              # normal daily run
  python nightly_catchup.py --allow-gap  # needed once when more than MAX_GAP_DAYS behind

Exit codes: 0 = done or already up to date, 1 = failed, 3 = big gap, rerun with --allow-gap,
            4 = season file not ready for the next night yet (try again later).
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
def read_state():
    with open(F.STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def write_state(state):
    # same format fetch_nightly writes
    with open(F.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def counted_games(d):
    """Regular Season / Playoff games on night d (the season file holds only those)."""
    return F.games_on(d)


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
        except F.NotReady as e:
            print(f"NOT READY: {e}. State stays through {read_state()['data_through']}; "
                  f"the next run picks it up.")
            return 4
        except Exception as e:
            print(f"ERROR: game count for {d.isoformat()} failed: {e}")
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

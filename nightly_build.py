"""
nightly_build.py — rebuild the whole Stat Streaks section from history + this season.

Every run recomputes everything from scratch, so streaks and droughts that began this
season are picked up the same way as old ones (no incremental state to drift):

  0. readiness: last night (US Eastern) must be covered by the season file
     (fetch_nightly.night_rows; published after 07:00 UTC the next morning, every game
     with both teams' lines). Not ready -> exit 4, nothing is written.
  1. prepare_data.py       history/ + season file -> data/build/ (Kaggle format)
  2. export_active_state   active-state.json (every current trailing run over its floor)
  3. build_site.py         leaderboards, feats, droughts, player pages, data .js files
  4. build_teams.py --all  franchise pages + teams index
  5. active-streaks.html   every current active streak, by scope / family / threshold
  6. build_sitemap.py      sitemap.xml; only pages whose content changed get a new lastmod

  python nightly_build.py                 # normal run (CI and the .bat)
  python nightly_build.py --no-ready-check

Exit codes: 0 = built, 1 = failed, 4 = season file not ready for last night yet.
"""
import os
import sys
import argparse
import subprocess

BASE = os.path.dirname(os.path.abspath(__file__))
BUILD_DATA = os.path.join(BASE, "data", "build")


def step(label, args, env):
    print(f"\n=== {label} ===", flush=True)
    rc = subprocess.call([sys.executable, *args], cwd=BASE, env=env)
    if rc != 0:
        print(f"ERROR: {label} failed (exit {rc}).")
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ready-check", action="store_true",
                    help="build even if the season file may not cover last night yet")
    a = ap.parse_args()

    import fetch_nightly as F
    last_night = F.eastern_yesterday()
    if not a.no_ready_check:
        try:
            n = F.games_on(last_night)
            print(f"Season file covers {last_night.isoformat()}: {n} game(s).")
        except F.NotReady as e:
            print(f"NOT READY: {e}. Nothing built; the next run retries.")
            return 4
        except RuntimeError as e:          # no file for the season (offseason before day 1)
            print(f"Season file: {e}. Building from what is published.")

    env = dict(os.environ, NBA_STREAKS_DATA=BUILD_DATA, PYTHONIOENCODING="utf-8")
    step("1/6 prepare data", ["prepare_data.py"], env)
    step("2/6 active state", ["export_active_state.py"], env)
    step("3/6 site pages", ["build_site.py"], env)
    step("4/6 team pages", ["build_teams.py", "--all"], env)
    step("5/6 active streaks page", ["fetch_nightly.py", "--active-page"], env)
    step("6/6 sitemap", ["build_sitemap.py"], env)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

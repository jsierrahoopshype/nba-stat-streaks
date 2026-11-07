"""
prepare_data.py — build the engine's input files without the local Kaggle download.

The engine (build_streaks.load_appearances, build_site.load_flags) reads
PlayerStatistics.csv, Players.csv and nationalities.csv from NBA_STREAKS_DATA.
This writes those three files to data/build/ (gitignored) from:

  history/PlayerStatistics_hist.csv.gz   Kaggle (eoinamoore/historical-nba-data-and-
                                         player-box-scores) Regular Season + Playoffs rows
                                         through 2025-26, only the columns the engine reads
  history/Players.csv.gz                 Kaggle Players.csv (personId, names, country)
  history/nationalities.csv.gz           nationality lookup used for flags (Alberto (HoopsMatic),
                                         own compilation; see history/SOURCES.md)
  history/patch_missing_games.csv.gz     6 postponed 2024-25 games the Kaggle file lacks
                                         (stats.nba.com box lines, season-file columns)
  season files from 2026-27 on           player_games_<season>.csv published by
                                         nba-editorial-bots (cdn.nba.com box lines),
                                         converted to the Kaggle columns

NBA Cup games: Kaggle labels them with their own gameType ("NBA Emirates Cup", ...).
Group and knockout games (gameId 002...) count as Regular Season, as the NBA and
Basketball-Reference count them and as the season file already does; the Cup final
(gameId 006...) does not count. Rows with no gameType and a 002 gameId are Regular Season.

Converted rows take the player's name from the history when he is already there, so
slugs and display names stay the same as before; new players use the cdn.nba.com name.
Team city / nickname come from the history's latest row for that team id.

  python prepare_data.py            # writes data/build/, prints the data range
Then run the builders with NBA_STREAKS_DATA=data/build (nightly_build.py does this).
"""
import os
import sys
import gzip
import shutil
import datetime

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(BASE, "history")
OUT = os.path.join(BASE, "data", "build")
FIRST_FILE_SEASON = 2026          # 2026-27: first season read from the season file

COLS = ["firstName", "lastName", "personId", "gameId", "gameType", "gameDate", "numMinutes", "points",
        "assists", "blocks", "steals", "threePointersMade", "reboundsTotal", "fieldGoalsAttempted",
        "freeThrowsAttempted", "playerteamId", "playerteamName", "playerteamCity", "comment"]
GAME_TYPE = {"RS": "Regular Season", "PO": "Playoffs"}
KEEP_TYPES = ("Regular Season", "Playoffs")


def normalize_game_types(hist):
    """NBA Cup (and unlabeled) games -> Regular Season when the gameId is a regular-season
    id (002...); everything else that is not Regular Season / Playoffs is dropped."""
    first = hist["gameId"].astype(str).str.lstrip("0").str[:1]
    other = ~hist["gameType"].isin(KEEP_TYPES)
    hist.loc[other & (first == "2"), "gameType"] = "Regular Season"
    return hist[hist["gameType"].isin(KEEP_TYPES)].copy()


def nba_season(d):
    return d.year if d.month >= 7 else d.year - 1


def season_label(y):
    return f"{y}-{(y + 1) % 100:02d}"


def load_season_files(today):
    """All published season files from 2026-27 through the season in progress."""
    import fetch_nightly as F
    frames = []
    for y in range(FIRST_FILE_SEASON, nba_season(today) + 1):
        df, published = F.load_season(season_label(y))
        df = df[df["SEASON_TYPE"].isin(GAME_TYPE)]
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def convert(season, hist):
    """Season-file rows -> Kaggle PlayerStatistics columns."""
    if season.empty:
        return pd.DataFrame(columns=COLS)
    hist = hist.sort_values("gameDate")
    last_name = hist.drop_duplicates("personId", keep="last").set_index("personId")[["firstName", "lastName"]]
    teams = hist.dropna(subset=["playerteamId"]).drop_duplicates("playerteamId", keep="last")
    teams = teams.set_index("playerteamId")[["playerteamCity", "playerteamName"]]

    pid = season["PLAYER_ID"].astype(int)
    tid = season["TEAM_ID"].astype(int)
    full = season["PLAYER_NAME"].fillna("").astype(str)
    split = full.str.split(" ", n=1, expand=True).reindex(columns=[0, 1]).fillna("")
    first = pid.map(last_name["firstName"]).fillna(split[0])
    last = pid.map(last_name["lastName"]).fillna(split[1])
    out = pd.DataFrame({
        "firstName": first.values,
        "lastName": last.values,
        "personId": pid.values,
        "gameId": season["GAME_ID"].astype(str).str.lstrip("0").values,
        "gameType": season["SEASON_TYPE"].map(GAME_TYPE).values,
        "gameDate": season["GAME_DATE"].astype(str).values,
        "numMinutes": pd.to_numeric(season["MIN"], errors="coerce").astype(float).values,
        "points": pd.to_numeric(season["PTS"], errors="coerce").values,
        "assists": pd.to_numeric(season["AST"], errors="coerce").values,
        "blocks": pd.to_numeric(season["BLK"], errors="coerce").values,
        "steals": pd.to_numeric(season["STL"], errors="coerce").values,
        "threePointersMade": pd.to_numeric(season["FG3M"], errors="coerce").values,
        "reboundsTotal": pd.to_numeric(season["REB"], errors="coerce").values,
        "fieldGoalsAttempted": pd.to_numeric(season["FGA"], errors="coerce").values,
        "freeThrowsAttempted": pd.to_numeric(season["FTA"], errors="coerce").values,
        "playerteamId": tid.values,
        "playerteamName": tid.map(teams["playerteamName"]).fillna(season["TEAM_ABBREVIATION"]).values,
        "playerteamCity": tid.map(teams["playerteamCity"]).fillna("").values,
        "comment": "",
    })
    # The season file lists only players who played (DNPs are left out). Someone on
    # the floor for a few seconds with no stat has MIN 0 there; give him a minute
    # fraction so the engine's appearance rule (numMinutes > 0) still counts him.
    out.loc[out["numMinutes"].fillna(0) <= 0, "numMinutes"] = 0.1
    return out[COLS]


def main(today=None):
    today = today or datetime.date.today()
    os.makedirs(OUT, exist_ok=True)
    hist = pd.read_csv(os.path.join(HIST, "PlayerStatistics_hist.csv.gz"), low_memory=False)
    n0 = len(hist)
    hist = normalize_game_types(hist)
    print(f"history: {n0 - len(hist):,} rows dropped (Cup finals); NBA Cup games counted as Regular Season")
    hist_dates = pd.to_datetime(hist["gameDate"].astype(str).str[:10], errors="coerce")
    print(f"history: {len(hist):,} rows, {hist_dates.min().date()} to {hist_dates.max().date()}")
    # gameDate is written as YYYY-MM-DD for every row: one format for the whole column
    hist["gameDate"] = hist_dates.dt.strftime("%Y-%m-%d")

    patch = pd.read_csv(os.path.join(HIST, "patch_missing_games.csv.gz"), dtype=str)
    patch_rows = convert(patch, hist)
    print(f"patch: {len(patch_rows)} rows, {patch['GAME_ID'].nunique()} games missing from the Kaggle file")
    hist = pd.concat([hist[COLS], patch_rows], ignore_index=True)

    season = load_season_files(today)
    new = convert(season, hist)
    if len(new):
        print(f"season files: {len(new):,} rows, {new['gameDate'].min()} to {new['gameDate'].max()}, "
              f"{season['GAME_ID'].nunique()} games")
    allrows = pd.concat([hist[COLS], new], ignore_index=True)
    for c in ("personId", "playerteamId"):
        allrows[c] = pd.to_numeric(allrows[c], errors="coerce").astype("Int64")
    allrows.to_csv(os.path.join(OUT, "PlayerStatistics.csv"), index=False)

    pl = pd.read_csv(os.path.join(HIST, "Players.csv.gz"), low_memory=False)
    known = set(pl["personId"].astype(int))
    add = new[~new["personId"].isin(known)].drop_duplicates("personId")
    if len(add):
        pl = pd.concat([pl, pd.DataFrame({"personId": add["personId"], "firstName": add["firstName"],
                                          "lastName": add["lastName"], "country": ""})], ignore_index=True)
        print(f"players: {len(add)} new this season (no country on file -> no flag)")
    pl.to_csv(os.path.join(OUT, "Players.csv"), index=False)

    with gzip.open(os.path.join(HIST, "nationalities.csv.gz"), "rb") as src, \
            open(os.path.join(OUT, "nationalities.csv"), "wb") as dst:
        shutil.copyfileobj(src, dst)
    print(f"wrote {OUT}/PlayerStatistics.csv ({len(allrows):,} rows), Players.csv, nationalities.csv")
    return OUT


if __name__ == "__main__":
    main()
    sys.exit(0)

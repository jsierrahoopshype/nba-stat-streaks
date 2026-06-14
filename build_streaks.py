"""
build_streaks.py — config-driven NBA statistical-streak engine.

A "streak" is a run of CONSECUTIVE APPEARANCES in which the player meets a
statistical condition. Missed games (DNPs / no row) are NEUTRAL: they neither
extend nor break a streak. Only an appearance that FAILS the condition breaks it.

Appearance rule (Phase-0 decision, Option 1 — recovers pre-1973 eras where
numMinutes was not tracked):
    appearance = numMinutes > 0
              OR (numMinutes is blank/NaN  AND  points is not null
                  AND comment does not indicate a DNP)

Every streak is computed at two LEVELS and three SCOPES:
  level  = player            (career-spanning, survives trades)
         = player-team       (resets on team change → franchise records)
  scope  = Regular Season | Playoffs | Combined (Regular+Playoffs)
"""

import re
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")  # Windows console: allow arrows/unicode

DATA = r"C:\nba-stat-streaks\data"
PLAYERSTATS = DATA + r"\PlayerStatistics.csv"

REGULAR, PLAYOFFS = "Regular Season", "Playoffs"

# --------------------------------------------------------------------------- #
# Streak definitions — the whole engine is driven by this list.
# --------------------------------------------------------------------------- #
STREAKS = [
    {"id": "pts10", "stat": "points", "op": ">=", "threshold": 10, "label": "10+ points", "family": "Scoring"},
    {"id": "pts20", "stat": "points", "op": ">=", "threshold": 20, "label": "20+ points", "family": "Scoring"},
    {"id": "pts30", "stat": "points", "op": ">=", "threshold": 30, "label": "30+ points", "family": "Scoring"},
    {"id": "pts40", "stat": "points", "op": ">=", "threshold": 40, "label": "40+ points", "family": "Scoring"},
    {"id": "reb10", "stat": "reboundsTotal", "op": ">=", "threshold": 10, "label": "10+ rebounds", "family": "Rebounding"},
    {"id": "reb15", "stat": "reboundsTotal", "op": ">=", "threshold": 15, "label": "15+ rebounds", "family": "Rebounding"},
    {"id": "reb20", "stat": "reboundsTotal", "op": ">=", "threshold": 20, "label": "20+ rebounds", "family": "Rebounding"},
    {"id": "ast5", "stat": "assists", "op": ">=", "threshold": 5, "label": "5+ assists", "family": "Playmaking"},
    {"id": "ast10", "stat": "assists", "op": ">=", "threshold": 10, "label": "10+ assists", "family": "Playmaking"},
    {"id": "ast15", "stat": "assists", "op": ">=", "threshold": 15, "label": "15+ assists", "family": "Playmaking"},
    {"id": "stl2", "stat": "steals", "op": ">=", "threshold": 2, "label": "2+ steals", "family": "Defense"},
    {"id": "stl3", "stat": "steals", "op": ">=", "threshold": 3, "label": "3+ steals", "family": "Defense"},
    {"id": "blk2", "stat": "blocks", "op": ">=", "threshold": 2, "label": "2+ blocks", "family": "Defense"},
    {"id": "blk3", "stat": "blocks", "op": ">=", "threshold": 3, "label": "3+ blocks", "family": "Defense"},
    {"id": "tpm3", "stat": "threePointersMade", "op": ">=", "threshold": 3, "label": "3+ threes", "family": "Shooting"},
    {"id": "tpm5", "stat": "threePointersMade", "op": ">=", "threshold": 5, "label": "5+ threes", "family": "Shooting"},
    # combos (computed via a custom predicate)
    {"id": "dd", "combo": "double_double", "label": "Double-double", "family": "Combo"},
    {"id": "td", "combo": "triple_double", "label": "Triple-double", "family": "Combo"},
    {"id": "p20r10", "combo": "p20r10", "label": "20+ pts & 10+ reb", "family": "Combo"},
    {"id": "p30r5a5", "combo": "p30r5a5", "label": "30+ pts, 5+ reb, 5+ ast", "family": "Combo"},
]

DNP_RE = re.compile(r"\b(?:dnp|dnd|nwt|dna|did not (?:play|dress)|not with team|inactive|suspend)", re.I)


def condition_columns(df):
    """Add one boolean column per streak id to df."""
    pts, reb, ast = df["points"], df["reboundsTotal"], df["assists"]
    stl, blk, tpm = df["steals"], df["blocks"], df["threePointersMade"]
    for s in STREAKS:
        if "stat" in s:
            df[s["id"]] = df[s["stat"]] >= s["threshold"]
        else:
            c = s["combo"]
            if c == "double_double":
                df[s["id"]] = ((pts >= 10).astype(int) + (reb >= 10).astype(int) + (ast >= 10).astype(int)) >= 2
            elif c == "triple_double":
                df[s["id"]] = ((pts >= 10).astype(int) + (reb >= 10).astype(int) + (ast >= 10).astype(int)
                               + (stl >= 10).astype(int) + (blk >= 10).astype(int)) >= 3
            elif c == "p20r10":
                df[s["id"]] = (pts >= 20) & (reb >= 10)
            elif c == "p30r5a5":
                df[s["id"]] = (pts >= 30) & (reb >= 5) & (ast >= 5)
    return df


def load_appearances():
    cols = ["firstName", "lastName", "personId", "gameId", "gameType", "gameDate",
            "numMinutes", "points", "assists", "blocks", "steals", "threePointersMade",
            "reboundsTotal", "fieldGoalsAttempted", "freeThrowsAttempted",
            "playerteamId", "playerteamName", "playerteamCity", "comment"]
    df = pd.read_csv(PLAYERSTATS, usecols=cols, low_memory=False)
    df = df[df["gameType"].isin([REGULAR, PLAYOFFS])].copy()
    df["gameDate"] = pd.to_datetime(df["gameDate"], errors="coerce")
    df = df.dropna(subset=["gameDate", "personId", "gameId"])

    mins = pd.to_numeric(df["numMinutes"], errors="coerce")
    for c in ("points", "assists", "blocks", "steals", "threePointersMade",
              "reboundsTotal", "fieldGoalsAttempted", "freeThrowsAttempted"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    comment = df["comment"].fillna("").astype(str)
    dnp_comment = comment.str.contains(DNP_RE)

    # Appearance rule.  The reliable "did he play" signal is a STAT LINE, not the
    # minutes value: old-era box scores store minutes inconsistently (blank/NaN for
    # untracked games, and even an explicit '0.0' on real 40-point games — e.g. Wilt's
    # Feb-1965 stretch). A true DNP, by contrast, has NO production at all (this is how
    # LeBron's Oct-2021 injury rows — minutes blank, every stat 0 — stay excluded).
    #   appearance = numMinutes > 0
    #             OR (a real stat line exists  AND  comment is not a DNP)
    statline = ((df["points"] > 0) | (df["reboundsTotal"] > 0) | (df["assists"] > 0)
                | (df["steals"] > 0) | (df["blocks"] > 0)
                | (df["fieldGoalsAttempted"] > 0) | (df["freeThrowsAttempted"] > 0))
    appeared = (mins > 0) | (statline & ~dnp_comment)
    df = df[appeared].copy()

    # fill stat NaNs (e.g. steals/blocks pre-1974, threes pre-1980) with 0 for conditions
    for c in ("points", "assists", "blocks", "steals", "threePointersMade", "reboundsTotal"):
        df[c] = df[c].fillna(0.0)
    df["playerteamId"] = pd.to_numeric(df["playerteamId"], errors="coerce").fillna(-1).astype(int)
    df["playerteamName"] = df["playerteamName"].fillna("").astype(str)
    df["gameId"] = df["gameId"].astype(str)
    df["personId"] = df["personId"].astype(int)
    df["name"] = df["firstName"].astype(str) + " " + df["lastName"].astype(str)

    # team_disp: CITY as recorded at the time (so Seattle-era games say "Seattle",
    # not "Oklahoma City"), with the two Los Angeles clubs disambiguated by nickname.
    city = df["playerteamCity"].fillna("").astype(str).str.strip()
    nm = df["playerteamName"]
    disp = city.where(city != "", nm)                       # fall back to nickname if no city
    la = city.isin(["Los Angeles", "LA"])
    disp = disp.mask(la & nm.str.contains("Laker", case=False), "LA Lakers")
    disp = disp.mask(la & nm.str.contains("Clipper", case=False), "LA Clippers")
    df["team_disp"] = disp
    df = condition_columns(df)
    return df


def compute_streaks(sub, group_key, cond_col, top=25):
    """sub: appearance rows pre-sorted by [group_key, gameDate, gameId].
    group_key: integer-encoded group id array (same length as sub).
    Returns the top runs of consecutive True in cond_col, with details."""
    g = group_key
    c = sub[cond_col].to_numpy(dtype=bool)
    n = len(c)
    if n == 0:
        return []
    same_prev = np.empty(n, dtype=bool); same_prev[0] = False; same_prev[1:] = g[1:] == g[:-1]
    prev_c = np.empty(n, dtype=bool); prev_c[0] = False; prev_c[1:] = c[:-1]
    run_start = c & ~(prev_c & same_prev)
    run_id = np.cumsum(run_start)
    rid_true = run_id[c]
    counts = np.bincount(rid_true)
    top_ids = [r for r in np.argsort(counts)[::-1] if counts[r] > 0][:top]
    top_set = set(int(r) for r in top_ids)

    idx = np.where(c)[0]
    dft = sub.iloc[idx].copy()
    dft["_rid"] = rid_true
    dft = dft[dft["_rid"].isin(top_set)]
    out = []
    for rid, gg in dft.groupby("_rid"):
        gg = gg.sort_values(["gameDate", "gameId"])
        teams = list(dict.fromkeys(t for t in gg["playerteamName"] if t))
        out.append({
            "length": int(len(gg)),
            "player": gg["name"].iloc[0],
            "personId": int(gg["personId"].iloc[0]),
            "start": gg["gameDate"].iloc[0].date(),
            "end": gg["gameDate"].iloc[-1].date(),
            "teams": teams,
        })
    out.sort(key=lambda r: r["length"], reverse=True)
    return out[:top]


def scope_df(df, scope):
    if scope == "combined":
        return df
    return df[df["gameType"] == (REGULAR if scope == "regular" else PLAYOFFS)]


def leaderboard(df, streak_id, level, scope, top=25):
    sub = scope_df(df, scope)
    if level == "player":
        sub = sub.sort_values(["personId", "gameDate", "gameId"])
        gkey = sub["personId"].to_numpy()
    else:  # player-team
        sub = sub.sort_values(["personId", "playerteamId", "gameDate", "gameId"])
        gkey = pd.factorize(list(zip(sub["personId"], sub["playerteamId"])))[0]
    return compute_streaks(sub, gkey, streak_id, top=top)


# --------------------------------------------------------------------------- #
# Phase-1 verification
# --------------------------------------------------------------------------- #
def era(d):
    return d.year


def fmt_rows(rows, n):
    out = []
    for i, r in enumerate(rows[:n], 1):
        teams = ", ".join(r["teams"]) or "—"
        out.append(f"  {i:>2}. {r['length']:>4}  {r['player']:<24} {r['start']} → {r['end']}  ({r['start'].year}-{r['end'].year})  [{teams}]")
    return "\n".join(out)


def verify():
    print("Loading appearances (Option-1 rule)…", flush=True)
    df = load_appearances()
    print(f"Appearance rows: {len(df):,}  |  players: {df['personId'].nunique():,}  |  "
          f"date range {df['gameDate'].min().date()} → {df['gameDate'].max().date()}\n", flush=True)

    SCOPE = "regular"   # known records (Jordan 866, Wilt 65) are regular-season
    print(f"=== Regular-season, PLAYER level ===\n")

    for sid, label, n, rec in [("pts10", "10+ points", 10, ("Michael Jordan", 866)),
                               ("pts20", "20+ points", 10, None),
                               ("pts30", "30+ points", 5, ("Wilt Chamberlain", 65)),
                               ("dd", "Double-double", 5, ("Wilt Chamberlain", "50s+"))]:
        rows = leaderboard(df, sid, "player", SCOPE, top=max(n, 10))
        print(f"--- Top {n}: {label} ---")
        print(fmt_rows(rows, n))
        if rec:
            who, num = rec
            top = rows[0] if rows else None
            if top:
                flag = "MATCH" if (top["player"] == who and (num == "50s+" or abs(top["length"] - num) <= 3)) else "DIFFERS"
                print(f"  >> known record: {who} {num}.  ours: {top['player']} {top['length']}  => {flag}")
        print()

    # Wilt coverage probe
    wilt = df[df["name"].str.lower().str.contains("wilt chamberlain")]
    if len(wilt):
        pid = wilt["personId"].iloc[0]
        all_rows_full = pd.read_csv(PLAYERSTATS, usecols=["personId", "gameDate", "gameType", "numMinutes", "points"], low_memory=False)
        all_rows_full["gameDate"] = pd.to_datetime(all_rows_full["gameDate"], errors="coerce")
        wfull = all_rows_full[(all_rows_full["personId"] == pid) & all_rows_full["gameType"].isin([REGULAR, PLAYOFFS])]
        w6162 = wfull[(wfull["gameDate"] >= "1961-07-01") & (wfull["gameDate"] < "1962-07-01")]
        m = pd.to_numeric(w6162["numMinutes"], errors="coerce")
        print("=== Wilt Chamberlain 1961-62 coverage probe ===")
        print(f"  personId {pid}: total rows in PlayerStatistics (reg+playoff) = {len(wfull)}")
        print(f"  1961-62 rows present = {len(w6162)} (a full season ≈ 80 games)")
        print(f"    of those: numMinutes>0 = {int((m>0).sum())}, minutes blank = {int(m.isna().sum())}, "
              f"points-present = {int(pd.to_numeric(w6162['points'],errors='coerce').notna().sum())}")
        wapp = df[df["personId"] == pid]
        w6162app = wapp[(wapp["gameDate"] >= "1961-07-01") & (wapp["gameDate"] < "1962-07-01")]
        print(f"  counted as APPEARANCES under Option 1 = {len(w6162app)}")


if __name__ == "__main__":
    verify()

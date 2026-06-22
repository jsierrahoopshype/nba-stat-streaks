"""
build_droughts.py — NEGATIVE-streak ("drought") engine, parallel to the positive one.

A DROUGHT is a run of CONSECUTIVE APPEARANCES in which the player stays STRICTLY
UNDER a threshold. It uses the EXACT same appearance basis and continuity rule as
the positive streaks (build_streaks.load_appearances):
  * DNPs / missed games are SKIPPED — they neither extend nor break a drought.
  * The drought breaks only on an APPEARANCE that is AT OR ABOVE the threshold.
  * "strictly under" — under-10 points = 0..9; a game of exactly 10 BREAKS it.
    under-1 (== zero) = exactly 0.

This is the identical run-length machinery used for positive streaks
(build_site.compute_all / competition_positions); only the boolean condition column
flips from `stat >= T` to `stat < T` (and, for the combo, "did NOT record a DD").

GATING
  Universal floors (under-10 pts, under-10 reb, under-5 ast, under-1 stl, under-1
  blk, under-1 tpm) are always computed/shown for every player.
  Gated floors are shown for a player ONLY IF that player has 100+ career
  appearances (all scopes combined) hitting the corresponding POSITIVE mark
  (e.g. the under-20 pts drought needs 100+ career 20-point games; "no double-double"
  needs 100+ career double-doubles). For gated droughts the gate is applied to the
  GLOBAL ranking too — non-qualifiers are excluded entirely, so the under-20 board
  isn't swamped by non-scorers who never clear 20.

This module is ADDITIVE: it imports and reuses build_streaks / build_site / franchises
without modifying them, and does not rebuild the existing positive site.
"""
import sys

import numpy as np
import pandas as pd

import build_streaks as E
import build_site as S
import franchises as FR

sys.stdout.reconfigure(encoding="utf-8")

GATE = 100  # career appearances at the positive mark required to unlock a gated drought

# Era gate (B1.5): steals & blocks were not recorded before the 1973-74 season,
# three-pointers not before 1979-80. The source fills those untracked games with 0,
# which would otherwise read as "zero steals/blocks/threes" and inflate those droughts
# with meaningless pre-tracking runs. We treat pre-tracking games as SKIPS (exactly
# like a DNP — they neither extend nor break the drought) by dropping them from the
# population for the affected stats, BEFORE the run-length pass. This applies the gate
# identically to per-player runs and the global all-time ranking. season = nba_season
# start year (e.g. 1973 == the 1973-74 season), already on df["season"] via
# franchises.add_franchise. points / rebounds / assists / no-DD are tracked in every
# era and are NOT gated.
ERA_SINCE = {"steals": 1973, "blocks": 1973, "threePointersMade": 1979}

# --------------------------------------------------------------------------- #
# Drought ladder.  `gate` names the POSITIVE streak-id (already a boolean column
# added by build_streaks.condition_columns) whose career count must reach GATE.
# universal=True  -> always shown, gate=None.
# --------------------------------------------------------------------------- #
DROUGHTS = [
    # Scoring
    {"id": "u_pts10", "stat": "points",            "threshold": 10, "label": "Under 10 points",  "family": "Scoring",    "universal": True,  "gate": None},
    {"id": "u_pts20", "stat": "points",            "threshold": 20, "label": "Under 20 points",  "family": "Scoring",    "universal": False, "gate": "pts20"},
    {"id": "u_pts30", "stat": "points",            "threshold": 30, "label": "Under 30 points",  "family": "Scoring",    "universal": False, "gate": "pts30"},
    # Rebounding
    {"id": "u_reb10", "stat": "reboundsTotal",     "threshold": 10, "label": "Under 10 rebounds","family": "Rebounding", "universal": True,  "gate": None},
    {"id": "u_reb15", "stat": "reboundsTotal",     "threshold": 15, "label": "Under 15 rebounds","family": "Rebounding", "universal": False, "gate": "reb15"},
    {"id": "u_reb20", "stat": "reboundsTotal",     "threshold": 20, "label": "Under 20 rebounds","family": "Rebounding", "universal": False, "gate": "reb20"},
    # Playmaking (assists)
    {"id": "u_ast5",  "stat": "assists",           "threshold": 5,  "label": "Under 5 assists",  "family": "Playmaking", "universal": True,  "gate": None},
    {"id": "u_ast10", "stat": "assists",           "threshold": 10, "label": "Under 10 assists", "family": "Playmaking", "universal": False, "gate": "ast10"},
    {"id": "u_ast15", "stat": "assists",           "threshold": 15, "label": "Under 15 assists", "family": "Playmaking", "universal": False, "gate": "ast15"},
    # Defense (steals)
    {"id": "u_stl1",  "stat": "steals",            "threshold": 1,  "label": "Zero steals",      "family": "Defense",    "universal": True,  "gate": None},
    {"id": "u_stl2",  "stat": "steals",            "threshold": 2,  "label": "Under 2 steals",   "family": "Defense",    "universal": False, "gate": "stl2"},
    {"id": "u_stl3",  "stat": "steals",            "threshold": 3,  "label": "Under 3 steals",   "family": "Defense",    "universal": False, "gate": "stl3"},
    # Defense (blocks)
    {"id": "u_blk1",  "stat": "blocks",            "threshold": 1,  "label": "Zero blocks",      "family": "Defense",    "universal": True,  "gate": None},
    {"id": "u_blk2",  "stat": "blocks",            "threshold": 2,  "label": "Under 2 blocks",   "family": "Defense",    "universal": False, "gate": "blk2"},
    {"id": "u_blk3",  "stat": "blocks",            "threshold": 3,  "label": "Under 3 blocks",   "family": "Defense",    "universal": False, "gate": "blk3"},
    # Shooting (threes)
    {"id": "u_tpm1",  "stat": "threePointersMade", "threshold": 1,  "label": "Zero threes",      "family": "Shooting",   "universal": True,  "gate": None},
    {"id": "u_tpm3",  "stat": "threePointersMade", "threshold": 3,  "label": "Under 3 threes",   "family": "Shooting",   "universal": False, "gate": "tpm3"},
    {"id": "u_tpm5",  "stat": "threePointersMade", "threshold": 5,  "label": "Under 5 threes",   "family": "Shooting",   "universal": False, "gate": "tpm5"},
    # Combo — "no double-double" (gated only)
    {"id": "u_dd",    "combo": "no_double_double",                  "label": "No double-double", "family": "Combo",      "universal": False, "gate": "dd"},
]

DROUGHT_META = [{"id": d["id"], "label": d["label"], "family": d["family"],
                 "universal": d["universal"], "gate": d["gate"]} for d in DROUGHTS]


def drought_condition_columns(df):
    """Add one boolean column per drought id: True when the appearance is UNDER the
    threshold (i.e. the drought continues on this game)."""
    for d in DROUGHTS:
        if "stat" in d:
            df[d["id"]] = df[d["stat"]] < d["threshold"]      # strictly under
        elif d["combo"] == "no_double_double":
            df[d["id"]] = ~df["dd"]                            # dd column from build_streaks
    return df


def gate_eligibility(df):
    """For each gated drought, the set of personIds with GATE+ career appearances at
    the corresponding positive mark (career = all scopes combined)."""
    elig = {}
    for d in DROUGHTS:
        if d["universal"]:
            continue
        counts = df.groupby("personId")[d["gate"]].sum()       # gate col is a bool/0-1 column
        elig[d["id"]] = set(counts.index[counts >= GATE].astype(int))
    return elig


def _compute(sub_scope, cond_col, eligible=None, since=None):
    """Run the positive engine's run-length pass on a drought condition column.
    `eligible` (a pid set) restricts the population for a gated drought.
    `since` (a season-start year) drops pre-tracking games so they act as SKIPS."""
    sub = sub_scope
    if since is not None:
        sub = sub[sub["season"] >= since]          # era gate: pre-tracking games skipped
    if eligible is not None:
        sub = sub[sub["personId"].isin(eligible)]
    sub = sub.reset_index(drop=True)
    if len(sub) == 0:
        return [], {}, {}
    gkey = sub["personId"].to_numpy()
    return S.compute_all(sub, gkey, cond_col, topn=S.TOPN)


# --------------------------------------------------------------------------- #
# Build drought data for a set of "needed" players (LeBron, a probe low-scorer).
# Returns the global top-N lists + length ranks (for ranking/probes), and the
# per-player top-10 runs only for the needed pids (keeps memory small).
# --------------------------------------------------------------------------- #
def build_drought_data(df, need_pids):
    elig = gate_eligibility(df)
    NEG = {}        # (did, scope) -> {"top": [...top100...], "rank": {len:rank}, "pk": {pid:[runs]}}
    for scope_key, _ in S.SCOPES:
        sub = (E.scope_df(df, scope_key)
               .sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True))
        for d in DROUGHTS:
            eligible = None if d["universal"] else elig[d["id"]]
            since = ERA_SINCE.get(d.get("stat"))   # None for points/reb/ast/no-DD
            top, topk, lrank = _compute(sub, d["id"], eligible, since)
            pk = {pid: topk.get(pid, []) for pid in need_pids}
            NEG[(d["id"], scope_key)] = {"top": top, "rank": lrank, "pk": pk}
    return NEG, elig


def build_positive_data(df, need_pids):
    """Same pass over the POSITIVE streak ids — used only to render the preview's
    'Streaks' tab (the existing view) for the needed player(s)."""
    POS = {}
    for scope_key, _ in S.SCOPES:
        sub = (E.scope_df(df, scope_key)
               .sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True))
        gkey = sub["personId"].to_numpy()
        for s in E.STREAKS:
            top, topk, lrank = S.compute_all(sub, gkey, s["id"], topn=S.TOPN)
            pk = {pid: topk.get(pid, []) for pid in need_pids}
            POS[(s["id"], scope_key)] = {"rank": lrank, "pk": pk}
    return POS


def build_full(df, min_len=2):
    """Full drought build for SITE INTEGRATION (B2). Returns, per (drought-id, scope):
        top_lists    {(did,scope): [global top-100 runs]}
        player_top   {(did,scope): {pid: [runs length-desc, length>=min_len]}}
        alltime_rank {(did,scope): {length: competition rank}}   (era-gated)
        elig         {did: set(pid)}                              (gated-floor eligibility)
    Reuses the SAME B1/B1.5 machinery (gate_eligibility, _compute with the era gate);
    no engine logic changes. player_top is pruned to runs >= min_len to bound memory
    (length-1 droughts are never displayed)."""
    elig = gate_eligibility(df)
    top_lists, player_top, alltime_rank = {}, {}, {}
    for scope_key, _ in S.SCOPES:
        sub = (E.scope_df(df, scope_key)
               .sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True))
        for d in DROUGHTS:
            eligible = None if d["universal"] else elig[d["id"]]
            since = ERA_SINCE.get(d.get("stat"))
            top, topk, lrank = _compute(sub, d["id"], eligible, since)
            top_lists[(d["id"], scope_key)] = top
            alltime_rank[(d["id"], scope_key)] = lrank
            pruned = {}
            for pid, runs in topk.items():
                rr = [r for r in runs if r["length"] >= min_len]
                if rr:
                    pruned[pid] = rr
            player_top[(d["id"], scope_key)] = pruned
    return top_lists, player_top, alltime_rank, elig


def build_leaderboard_ungated(df):
    """UNGATED global drought leaderboards for the PUBLIC droughts.html page.

    The 100-game player gate is REMOVED here (eligible=None for every type), so the
    under-20/30 boards include career non-scorers whose whole careers are sub-threshold
    droughts — "longest droughts, period." The ERA gate is still applied (steals/blocks
    pre-1973-74, threes pre-1979-80 skipped) because that's data correctness, not a
    player gate. This is SEPARATE from build_full's gated rankings (used by the
    per-player / per-team Droughts tabs); neither affects the other.

    Returns {(did, scope): (top_list, length_rank)}."""
    out = {}
    for scope_key, _ in S.SCOPES:
        sub = (E.scope_df(df, scope_key)
               .sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True))
        for d in DROUGHTS:
            since = ERA_SINCE.get(d.get("stat"))
            top, _topk, lrank = _compute(sub, d["id"], eligible=None, since=since)  # eligible=None => ungated
            out[(d["id"], scope_key)] = (top, lrank)
    return out


def load():
    """Appearances with positive + drought condition columns and franchise info."""
    df = E.load_appearances()
    df = drought_condition_columns(df)
    df = FR.add_franchise(df)
    return df

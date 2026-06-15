"""
fetch_nightly.py — Task 2 / Stage 2: nightly active-streak updater.

Flow (run after games are Final):
  1. "Last night" = previous day, US Eastern.
  2. scoreboardv2 pre-check (via the proxy) that the date's games are Final.
  3. Pull that date's player lines via leaguegamelog (Regular Season + Playoffs)
     through the proxy, URL-encoded.
  4. For each streak in active-state.json: extend if the player met the threshold,
     break if they appeared and missed, leave unchanged if they didn't play.
     Threshold logic is the ENGINE's own (build_streaks.condition_columns) — not
     reimplemented here.
  5. Write the raw pull to data/daily/YYYY-MM-DD.json and update active-state.json.

Usage:
  python fetch_nightly.py                       # last night (US Eastern)
  python fetch_nightly.py --date 2026-01-15     # a specific date
  python fetch_nightly.py --date 2026-01-15 --dry-run   # compute, write nothing
  python fetch_nightly.py --validate 2026-01-15 # compare proxy vs our historical data
"""
import os
import sys
import json
import copy
import argparse
import datetime
import urllib.parse
import urllib.request
from datetime import timedelta

import numpy as np
import pandas as pd

import build_streaks as E
import build_site as BS   # read-only: reuse the design system (CSS/nav/search) for lastgame.html
from export_active_state import FLOORS   # per-type floors (single source of truth)

FAMILY_BY_ID = {s["id"]: s["family"] for s in E.STREAKS}
LABEL_BY_ID = {s["id"]: s["label"] for s in E.STREAKS}

PROXY = "https://nba-proxy.thejorgesierra.workers.dev/"
BASE = r"C:\nba-stat-streaks"
DAILY_DIR = os.path.join(BASE, "data", "daily")
STATE_PATH = os.path.join(BASE, "active-state.json")
STREAK_IDS = [s["id"] for s in E.STREAKS]

# stats.nba.com team_id -> display city (Iron Man convention; LA disambiguated)
TEAM_CITY = {
    1610612737: "Atlanta", 1610612738: "Boston", 1610612739: "Cleveland", 1610612740: "New Orleans",
    1610612741: "Chicago", 1610612742: "Dallas", 1610612743: "Denver", 1610612744: "Golden State",
    1610612745: "Houston", 1610612746: "LA Clippers", 1610612747: "LA Lakers", 1610612748: "Miami",
    1610612749: "Milwaukee", 1610612750: "Minnesota", 1610612751: "Brooklyn", 1610612752: "New York",
    1610612753: "Orlando", 1610612754: "Indiana", 1610612755: "Philadelphia", 1610612756: "Phoenix",
    1610612757: "Portland", 1610612758: "Sacramento", 1610612759: "San Antonio", 1610612760: "Oklahoma City",
    1610612761: "Toronto", 1610612762: "Utah", 1610612763: "Memphis", 1610612764: "Washington",
    1610612765: "Detroit", 1610612766: "Charlotte",
}


def nba_season(d):
    return d.year if d.month >= 7 else d.year - 1


def season_str(d):
    y = nba_season(d)
    return f"{y}-{(y + 1) % 100:02d}"


def eastern_yesterday():
    try:
        from zoneinfo import ZoneInfo
        now_et = datetime.datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now_et = datetime.datetime.utcnow() - timedelta(hours=5)  # EST fallback
    return now_et.date() - timedelta(days=1)


# --------------------------------------------------------------------------- #
# Proxy fetch
# --------------------------------------------------------------------------- #
def stats_url(endpoint, params):
    """Build a stats.nba.com URL with a properly query-encoded string (spaces -> '+',
    slashes -> %2F). The proxy decodes its ?url= value ONCE before fetching, so the
    target must already be valid-encoded or it 520s (e.g. a raw space in
    'Regular Season')."""
    return f"https://stats.nba.com/stats/{endpoint}?" + urllib.parse.urlencode(params)


def proxy_json(target_url, timeout=45, retries=2):
    u = PROXY + "?" + urllib.parse.urlencode({"url": target_url})
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "nba-stat-streaks/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
    raise last


def scoreboard_check(d):
    target = stats_url("scoreboardv2", {"GameDate": d.isoformat(), "LeagueID": "00", "DayOffset": "0"})
    js = proxy_json(target)
    rs = next(x for x in js["resultSets"] if x["name"] == "GameHeader")
    h, rows = rs["headers"], rs["rowSet"]
    si = h.index("GAME_STATUS_ID")
    games = len(rows)
    final = sum(1 for row in rows if int(row[si]) == 3)
    return {"games": games, "final": final, "all_final": games > 0 and final == games, "raw": rs}


def _idx(headers):
    return {h: i for i, h in enumerate(headers)}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fetch_lines(d):
    """Pull the date's player lines for Regular Season and Playoffs. Returns
    ({'regular':{pid:line}, 'playoffs':{pid:line}}, raw_resultsets)."""
    out = {"regular": {}, "playoffs": {}}
    raw = {}
    for scope, season_type in (("regular", "Regular Season"), ("playoffs", "Playoffs")):
        target = stats_url("leaguegamelog", {
            "Counter": "1000", "Direction": "DESC", "LeagueID": "00", "PlayerOrTeam": "P",
            "Season": season_str(d), "SeasonType": season_type, "Sorter": "DATE",
            "DateFrom": d.strftime("%m/%d/%Y"), "DateTo": d.strftime("%m/%d/%Y")})
        js = proxy_json(target)
        rs = js["resultSets"][0]
        raw[scope] = {"headers": rs["headers"], "rowSet": rs["rowSet"]}
        ix = _idx(rs["headers"])
        for row in rs["rowSet"]:
            pid = int(row[ix["PLAYER_ID"]])
            tid = int(row[ix["TEAM_ID"]])
            out[scope][pid] = {
                "personId": pid, "name": row[ix["PLAYER_NAME"]],
                "team": TEAM_CITY.get(tid, row[ix["TEAM_NAME"]]), "team_id": tid,
                "game_id": row[ix["GAME_ID"]], "matchup": row[ix["MATCHUP"]], "min": row[ix["MIN"]],
                "points": _num(row[ix["PTS"]]), "reboundsTotal": _num(row[ix["REB"]]),
                "assists": _num(row[ix["AST"]]), "steals": _num(row[ix["STL"]]),
                "blocks": _num(row[ix["BLK"]]), "threePointersMade": _num(row[ix["FG3M"]]),
            }
    return out, raw


def historical_lines(df, d):
    """Same shape as fetch_lines but from our own PlayerStatistics dataset (for the
    validation harness)."""
    sub = df[df["gameDate"].dt.date == d]
    out = {"regular": {}, "playoffs": {}}
    for r in sub.itertuples():
        scope = "regular" if r.gameType == E.REGULAR else "playoffs"
        out[scope][int(r.personId)] = {
            "personId": int(r.personId), "name": r.name, "team": r.team_disp,
            "points": float(r.points), "reboundsTotal": float(r.reboundsTotal),
            "assists": float(r.assists), "steals": float(r.steals),
            "blocks": float(r.blocks), "threePointersMade": float(r.threePointersMade),
        }
    return out


# --------------------------------------------------------------------------- #
# Classification — reuse the engine's condition logic, never reimplement it
# --------------------------------------------------------------------------- #
def classify(lines_for_scope):
    """{pid: set(streak_ids the player met)} using build_streaks.condition_columns."""
    if not lines_for_scope:
        return {}
    df = pd.DataFrame(list(lines_for_scope.values()))
    for c in ("points", "reboundsTotal", "assists", "steals", "blocks", "threePointersMade"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df = E.condition_columns(df)
    met = {}
    for row in df.itertuples(index=False):
        d = row._asdict()
        met[int(d["personId"])] = set(sid for sid in STREAK_IDS if bool(d[sid]))
    return met


def _game_summary(line):
    return {"matchup": line.get("matchup", ""), "points": line["points"],
            "reboundsTotal": line["reboundsTotal"], "assists": line["assists"],
            "steals": line["steals"], "blocks": line["blocks"], "threePointersMade": line["threePointersMade"]}


def apply_night(state, lines, date_iso):
    """Mutates `state` in place (extends/drops streaks). Returns (extended, broken,
    unchanged_count), where extended/broken are descriptive dicts for the page."""
    met = {"regular": classify(lines["regular"]), "playoffs": classify(lines["playoffs"])}
    extended, broken, unchanged, kept = [], [], 0, []
    fields = ("personId", "player", "slug", "type", "label", "scope", "team")
    for st in state["streaks"]:
        pid, typ, scope = st["personId"], st["type"], st["scope"]
        if scope == "combined":             # a combined streak rides reg OR playoff games
            src = "regular" if pid in lines["regular"] else ("playoffs" if pid in lines["playoffs"] else None)
        else:
            src = scope if pid in lines[scope] else None
        if src is None:                      # didn't play in a game of this scope -> untouched
            unchanged += 1
            kept.append(st)
            continue
        line = lines[src][pid]
        if typ in met[src].get(pid, set()):  # met threshold -> extend
            old = st["length"]
            st["length"] = old + 1
            st["last_date"] = date_iso
            st["team"] = line.get("team", st["team"])
            kept.append(st)
            extended.append({**{k: st[k] for k in fields}, "old": old, "new": st["length"],
                             "started": st.get("started"), "game": _game_summary(line)})
        else:                                # appeared and missed -> streak ends, drop it
            broken.append({**{k: st[k] for k in fields}, "had": st["length"], "game": _game_summary(line)})
    state["streaks"] = kept
    return extended, broken, unchanged


# --------------------------------------------------------------------------- #
# Milestones + all-time leaderboard movement (reads streaks-data.js, read-only)
# --------------------------------------------------------------------------- #
def is_milestone(n):
    return n == 10 or (n >= 25 and n % 25 == 0)


def load_alltime():
    """{type: {scope: [(slug, length), ...]}} from the built streaks-data.js."""
    path = os.path.join(BASE, "streaks-data.js")
    if not os.path.exists(path):
        return {}
    txt = open(path, encoding="utf-8").read()
    key = "window.STREAK_DATA="
    i = txt.index(key) + len(key)
    j = txt.index(";\nwindow.STREAK_PLAYERS", i)
    data = json.loads(txt[i:j])
    out = {}
    for typ, scopes in data.items():
        out[typ] = {sc: [(r[1], r[2]) for r in rows] for sc, rows in scopes.items()}
    return out


def milestones_and_movers(extended, alltime):
    milestones = [e for e in extended if is_milestone(e["new"])]
    movers = []
    for e in extended:
        rows = alltime.get(e["type"], {}).get(e["scope"], [])
        if not rows:
            continue
        rank = lambda L: 1 + sum(1 for slug, ln in rows if slug != e["slug"] and ln > L)
        nr, orr = rank(e["new"]), rank(e["old"])
        if nr <= 100 and nr < orr:           # climbed into / up the all-time top 100
            movers.append({**e, "rank": nr, "old_rank": orr})
    return milestones, movers


# --------------------------------------------------------------------------- #
# Render lastgame.html — three scope sections, each split by stat family
# --------------------------------------------------------------------------- #
_PLAYERS_FLAGS = None


def _players_flags():
    """slug -> [name, iso, country] from the built streaks-data.js (for flags)."""
    global _PLAYERS_FLAGS
    if _PLAYERS_FLAGS is None:
        p = os.path.join(BASE, "streaks-data.js")
        _PLAYERS_FLAGS = {}
        if os.path.exists(p):
            txt = open(p, encoding="utf-8").read()
            k = "window.STREAK_PLAYERS="
            i = txt.index(k) + len(k)
            j = txt.index(";\nwindow.STREAK_META", i)
            _PLAYERS_FLAGS = json.loads(txt[i:j])
    return _PLAYERS_FLAGS


def _flag(slug):
    p = _players_flags().get(slug)
    return BS.flag_html(p[1], p[2]) if p and p[1] else ""


def _plink(e):
    return f'<a class="plink" href="players/{e["slug"]}.html">{BS.esc(e["player"])}</a>' + _flag(e["slug"])


def _lgboard(cap, headers, rows):
    if not rows:
        return ""
    th = "".join(f'<th class="{c}">{h}</th>' for h, c in headers)
    return (f'<div class="tcap">{cap}</div><div class="table-card"><table class="board"><thead><tr>{th}</tr>'
            f'</thead><tbody>{rows}</tbody></table></div>\n')


def _endgame(g):
    mu = (BS.esc(g["matchup"]) + " · ") if g.get("matchup") else ""
    return (f'{mu}{BS.fmt_iso(g.get("date", ""))} · {int(g["points"])} pts · '
            f'{int(g["reboundsTotal"])} reb · {int(g["assists"])} ast')


def render_lastgame(label, extended, ended=None):
    """Active (trailing) streaks only, grouped scope -> stat family -> threshold,
    one sub-table per threshold (ascending). 'ended' is accepted but not shown."""
    title = "Last Game — NBA Statistical Streaks"
    nmile = sum(1 for e in extended if e.get("milestone"))
    desc = (f"NBA active statistical-streak status at the {label}: {len(extended)} streaks still active "
            f"({nmile} at a milestone) — by scope (regular season / playoffs / combined), stat family, and threshold.")

    by_scope_type = {}      # (scope, type) -> [entries]
    for e in extended:
        by_scope_type.setdefault((e["scope"], e["type"]), []).append(e)

    summ = (f'<p class="subtitle">Season-end state ({label}). <b>{len(extended)}</b> streaks still active going '
            f'into the offseason · <b>{nmile}</b> sitting at a milestone length. Grouped by scope, then stat '
            f'family, then threshold.</p>\n')
    sections = summ
    for scope_key, scope_label in BS.SCOPES:
        sections += f'<h2 class="scopeh">{scope_label}</h2>\n'
        shown = False
        cur_family = None
        for s in E.STREAKS:                 # already ascending within each family
            sid, fam = s["id"], s["family"]
            entries = by_scope_type.get((scope_key, sid))
            if not entries:
                continue
            if fam != cur_family:
                sections += f'<h3 class="famh">{fam}</h3>\n'
                cur_family = fam
            shown = True
            rows = "".join(
                f'<tr><td class="col-player" data-label="Player">{_plink(e)}'
                + ('<span class="mbadge">★ milestone</span>' if e.get("milestone") else '')
                + f'</td><td class="col-streak" data-label="Length">{e["length"]}</td>'
                f'<td class="col-date" data-label="Started">'
                f'{BS.fmt_iso(e.get("started") or e.get("last_date", ""))}</td></tr>'
                for e in sorted(entries, key=lambda x: -x["length"]))
            sections += _lgboard(s["label"], [("Player", "col-player"),
                                              ("Length", "col-streak"), ("Started", "col-date")], rows)
        if not shown:
            sections += '<p class="subtitle">No active streaks in this scope.</p>\n'

    body = (
        f'<div class="wrap">\n<a class="backtop" href="index.html">← All streak leaderboards</a>\n'
        f'{BS.search_box()}\n'
        f'<header><span class="brand">HoopsHype · NBA Statistical Streaks</span>'
        f'<h1>🏀 Last <span class="accent">Game</span></h1>'
        f'<p class="subtitle">Active-streak status at the {label}.</p></header>\n'
        f'{sections}\n'
        f'{BS.search_box()}\n'
        f'<a class="backtop" href="index.html">← All streak leaderboards</a>\n</div>\n'
    )
    extra_css = ('<style>.scopeh{border-bottom:2px solid var(--text);padding-bottom:.3rem;margin-top:2rem;}'
                 '.famh{font-size:.82rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;'
                 'font-family:"JetBrains Mono",monospace;margin:1.1rem 0 .3rem;}'
                 '.tcap{font-size:.78rem;font-weight:700;margin:.4rem 0 .2rem;}'
                 '.mbadge{display:inline-block;margin-left:.4rem;font-size:.58rem;font-weight:700;color:var(--accent);'
                 'background:var(--accent-dim);border-radius:10px;padding:.1rem .4rem;vertical-align:middle;'
                 'font-family:"JetBrains Mono",monospace;}</style>\n')
    return BS.head(title, desc) + extra_css + BS.nav("lastgame") + body + BS.scripts_for("")


def season_end_status(df, current_season):
    """Season-end demo: for each (scope, streak type, player), the run ending at the
    player's most-recent game in that scope — ACTIVE if that game met the threshold,
    ENDED if it failed a run that was already at/above the floor."""
    extended, ended = [], []
    for scope_key, _ in BS.SCOPES:
        sub = E.scope_df(df, scope_key).sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True)
        n = len(sub)
        if n == 0:
            continue
        gkey = sub["personId"].to_numpy()
        dates = sub["gameDate"].dt.date.to_numpy()
        names = sub["name"].to_numpy(); teams = sub["team_disp"].to_numpy(); pids = sub["personId"].to_numpy()
        stat = {k: sub[k].to_numpy() for k in
                ("points", "reboundsTotal", "assists", "steals", "blocks", "threePointersMade")}
        is_last = np.empty(n, bool); is_last[-1] = True; is_last[:-1] = gkey[1:] != gkey[:-1]
        last_idx = [i for i in np.where(is_last)[0] if nba_season(dates[i]) == current_season]
        for sid in FAMILY_BY_ID:
            floor = FLOORS[sid]
            c = sub[sid].to_numpy(dtype=bool)
            same_prev = np.empty(n, bool); same_prev[0] = False; same_prev[1:] = gkey[1:] == gkey[:-1]
            prev_c = np.empty(n, bool); prev_c[0] = False; prev_c[1:] = c[:-1]
            run_id = np.cumsum(c & ~(prev_c & same_prev))
            counts = np.bincount(run_id[c]) if c.any() else np.array([0])
            for i in last_idx:
                if c[i]:
                    L = int(counts[run_id[i]])
                    if L >= floor:
                        nm, pid = str(names[i]), int(pids[i])
                        # the trailing run occupies contiguous positions i-L+1 .. i,
                        # so the run STARTED on the game at i-L+1.
                        started = dates[i - L + 1].isoformat()
                        extended.append({"personId": pid, "player": nm, "slug": BS.slugify(nm, pid),
                                         "type": sid, "label": LABEL_BY_ID[sid], "family": FAMILY_BY_ID[sid],
                                         "scope": scope_key, "length": L, "last_date": dates[i].isoformat(),
                                         "started": started, "team": str(teams[i]), "milestone": is_milestone(L)})
                elif i - 1 >= 0 and gkey[i - 1] == gkey[i] and c[i - 1]:
                    L = int(counts[run_id[i - 1]])
                    if L >= floor:
                        nm, pid = str(names[i]), int(pids[i])
                        ended.append({"personId": pid, "player": nm, "slug": BS.slugify(nm, pid),
                                      "type": sid, "label": LABEL_BY_ID[sid], "family": FAMILY_BY_ID[sid],
                                      "scope": scope_key, "length": L, "team": str(teams[i]),
                                      "game": {"date": dates[i].isoformat(),
                                               **{k: float(stat[k][i]) for k in stat}}})
    return extended, ended


def season_end_demo():
    print("Building season-end Last Game page from historical data…", flush=True)
    df = E.load_appearances()
    cur = nba_season(df["gameDate"].max().date())
    label = f"{season_str(df['gameDate'].max().date())} season's end"
    extended, _ended = season_end_status(df, cur)
    html = render_lastgame(label, extended)
    with open(os.path.join(BASE, "lastgame.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  active={len(extended)}  milestones={sum(1 for e in extended if e['milestone'])}"
          f"  -> wrote lastgame.html (ended tables dropped)")


# --------------------------------------------------------------------------- #
# Main nightly run
# --------------------------------------------------------------------------- #
def run(d, source="proxy", dry_run=False, force=False, write_state=True):
    print(f"Nightly update for {d.isoformat()}  (season {season_str(d)}, source={source})")
    games = None
    raw = None
    if source == "historical":
        print("  source=historical — loading our own dataset…", flush=True)
        df = E.load_appearances()
        lines = historical_lines(df, d)
        games = len(set(l["personId"] for l in {**lines["regular"], **lines["playoffs"]}.values())) and \
            (1 if (lines["regular"] or lines["playoffs"]) else 0)
        games = 1 if (lines["regular"] or lines["playoffs"]) else 0
    else:
        sc = scoreboard_check(d)
        games = sc["games"]
        print(f"  scoreboard: {sc['games']} games, {sc['final']} Final")
        if sc["games"] == 0:
            print("  no games that date — nothing to do.")
        elif not sc["all_final"] and not force:
            print("  not all games Final — aborting (use --force to override)."); return
        lines, raw = fetch_lines(d)

    nreg, npo = len(lines["regular"]), len(lines["playoffs"])
    print(f"  player lines: {nreg} regular, {npo} playoff")

    with open(STATE_PATH, encoding="utf-8") as f:
        state = json.load(f)
    extended, broken, unchanged = apply_night(state, lines, d.isoformat())
    state["count"] = len(state["streaks"])
    state["data_through"] = d.isoformat()
    milestones, movers = milestones_and_movers(extended, load_alltime())

    print(f"\n  SUMMARY: extended {len(extended)} | broken {len(broken)} | unchanged {unchanged} | "
          f"milestones {len(milestones)} | all-time movers {len(movers)}"
          f"{'  (dry-run, nothing written)' if dry_run else ''}")
    for e in sorted(extended, key=lambda r: -r["new"])[:5]:
        print(f"    extend  {e['player']:<22} {e['label']:<14} {e['scope']:<8} {e['old']} -> {e['new']}")
    for e in broken[:5]:
        g = e["game"]
        print(f"    end     {e['player']:<22} {e['label']:<14} {e['scope']:<8} had {e['had']} "
              f"({g['matchup']} {int(g['points'])}p/{int(g['reboundsTotal'])}r/{int(g['assists'])}a)")
    if dry_run:
        return

    # adapt one night's extends into the lastgame.html entry shape (active only)
    mileset = {(e["personId"], e["type"], e["scope"]) for e in milestones}
    ext_entries = [{"personId": e["personId"], "player": e["player"], "slug": e["slug"], "type": e["type"],
                    "label": e["label"], "family": FAMILY_BY_ID[e["type"]], "scope": e["scope"],
                    "length": e["new"], "last_date": d.isoformat(), "started": e.get("started"),
                    "milestone": (e["personId"], e["type"], e["scope"]) in mileset} for e in extended]

    # outputs (always lastgame.html; state/daily only when write_state)
    html = render_lastgame(f"game on {BS.fmt_iso(d.isoformat())}", ext_entries)
    with open(os.path.join(BASE, "lastgame.html"), "w", encoding="utf-8") as f:
        f.write(html)
    print("  wrote lastgame.html")
    if write_state:
        if source == "proxy" and raw is not None:
            os.makedirs(DAILY_DIR, exist_ok=True)
            with open(os.path.join(DAILY_DIR, f"{d.isoformat()}.json"), "w", encoding="utf-8") as f:
                json.dump({"date": d.isoformat(), "season": season_str(d), "leaguegamelog": raw}, f, ensure_ascii=False)
            print(f"  wrote data/daily/{d.isoformat()}.json")
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        print("  updated active-state.json")
    else:
        print("  (--no-state) active-state.json and data/daily left untouched")


# --------------------------------------------------------------------------- #
# Validation harness: proxy vs our historical dataset for a known past date
# --------------------------------------------------------------------------- #
def validate(d):
    print(f"VALIDATION for {d.isoformat()}: live proxy  vs  our historical dataset\n")
    print("Loading historical appearances…", flush=True)
    df = E.load_appearances()

    px_lines, _ = fetch_lines(d)
    hi_lines = historical_lines(df, d)

    px_all = {**px_lines["regular"], **px_lines["playoffs"]}
    hi_all = {**hi_lines["regular"], **hi_lines["playoffs"]}
    print(f"players that played {d.isoformat()}:  proxy={len(px_all)}  historical={len(hi_all)}")
    only_px = set(px_all) - set(hi_all)
    only_hi = set(hi_all) - set(px_all)
    if only_px: print(f"  only in proxy: {len(only_px)} (e.g. {[px_all[p]['name'] for p in list(only_px)[:3]]})")
    if only_hi: print(f"  only in historical: {len(only_hi)} (e.g. {[hi_all[p]['name'] for p in list(only_hi)[:3]]})")

    # raw stat agreement
    common = set(px_all) & set(hi_all)
    stat_mismatch = []
    for pid in common:
        a, b = px_all[pid], hi_all[pid]
        for k in ("points", "reboundsTotal", "assists", "steals", "blocks", "threePointersMade"):
            if int(a[k]) != int(b[k]):
                stat_mismatch.append((b["name"], k, a[k], b[k]))
    print(f"\nraw stat lines: {len(common)} players compared, {len(stat_mismatch)} stat mismatches")
    for nm, k, pv, hv in stat_mismatch[:8]:
        print(f"    {nm}: {k} proxy={pv} hist={hv}")

    # met-condition agreement (the actual streak-change decisions)
    px_met = {**classify(px_lines["regular"]), **classify(px_lines["playoffs"])}
    hi_met = {**classify(hi_lines["regular"]), **classify(hi_lines["playoffs"])}
    cond_mismatch = [pid for pid in common if px_met.get(pid, set()) != hi_met.get(pid, set())]
    print(f"\nmet-condition sets: {len(cond_mismatch)} of {len(common)} players differ")
    for pid in cond_mismatch[:8]:
        print(f"    {hi_all[pid]['name']}: proxy={sorted(px_met.get(pid,set()))} hist={sorted(hi_met.get(pid,set()))}")

    # streak-change decisions on the CURRENT active-state (proxy vs historical)
    with open(STATE_PATH, encoding="utf-8") as f:
        base = json.load(f)
    sp = copy.deepcopy(base); ext_p, brk_p, unc_p = apply_night(sp, px_lines, d.isoformat())
    sh = copy.deepcopy(base); ext_h, brk_h, unc_h = apply_night(sh, hi_lines, d.isoformat())
    key = lambda lst: sorted((s["personId"], s["type"], s["scope"]) for s in lst)
    same_ext = key(ext_p) == key(ext_h)
    same_brk = key(brk_p) == key(brk_h)
    print(f"\nstreak-change decisions on active-state.json ({base['count']} active streaks):")
    print(f"  proxy   : extended {len(ext_p)} | broken {len(brk_p)} | unchanged {unc_p}")
    print(f"  history : extended {len(ext_h)} | broken {len(brk_h)} | unchanged {unc_h}")
    print(f"  extended sets match: {same_ext}    broken sets match: {same_brk}")
    ok = (not stat_mismatch) and (not cond_mismatch) and same_ext and same_brk and not only_px and not only_hi
    print(f"\n{'✅ MATCH — fetcher is trustworthy' if ok else '❌ MISMATCH — investigate above'}")
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: last night US Eastern)")
    ap.add_argument("--source", choices=["proxy", "historical"], default="proxy",
                    help="proxy = live stats.nba.com (default); historical = our own dataset (offline testing)")
    ap.add_argument("--dry-run", action="store_true", help="compute + print, write nothing")
    ap.add_argument("--no-state", action="store_true",
                    help="write lastgame.html only; leave active-state.json and data/daily untouched")
    ap.add_argument("--force", action="store_true", help="run even if not all games Final")
    ap.add_argument("--validate", metavar="YYYY-MM-DD", help="compare proxy vs historical for a past date")
    ap.add_argument("--season-end", action="store_true",
                    help="build lastgame.html as the season-end state from our historical data (offline demo)")
    a = ap.parse_args()
    if a.validate:
        validate(datetime.date.fromisoformat(a.validate))
        return
    if a.season_end:
        season_end_demo()
        return
    d = datetime.date.fromisoformat(a.date) if a.date else eastern_yesterday()
    run(d, source=a.source, dry_run=a.dry_run, force=a.force, write_state=not a.no_state)


if __name__ == "__main__":
    main()

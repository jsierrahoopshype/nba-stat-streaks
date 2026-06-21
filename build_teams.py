"""
build_teams.py — team-level pages (Stage 2: all teams + index).

"Team level" = each team's PLAYERS' streaks, grouped by the team they were COMPILED
WITH, city-era convention (team_disp). Each team page has two sections, both nested
scope -> stat family -> threshold, each sub-table ranked 1..N on the left:
  A) "Best ever"  — career-best player-team(-city) runs on the team (top 25 per table,
                    with the league-wide all-time rank where the run IS the player's
                    ranked all-time run).
  B) "Active into the offseason" — current season-end trailing runs filtered to the team.

  python build_teams.py --all            # every city-era team + teams.html index
  python build_teams.py --team "Boston"  # one team
  python build_teams.py --list           # available teams by appearances
"""
import os
import re
import sys
import json
import argparse

import numpy as np
import pandas as pd

import build_streaks as E
import build_site as BS
import fetch_nightly as F   # season_end_status, nba_season
import franchises as FR     # city-era -> modern-franchise map

sys.stdout.reconfigure(encoding="utf-8")

BASE = r"C:\nba-stat-streaks"
TEAMS_DIR = os.path.join(BASE, "teams")
LABEL_BY_ID = {s["id"]: s["label"] for s in E.STREAKS}


def team_slug(t):
    return FR.team_slug(t)


# --------------------------------------------------------------------------- #
# read the already-built leaderboard data (all-time ranks + flags)
# --------------------------------------------------------------------------- #
def load_streakdata():
    txt = open(os.path.join(BASE, "streaks-data.js"), encoding="utf-8").read()

    def block(key, end):
        i = txt.index(key) + len(key)
        return json.loads(txt[i:txt.index(end, i)])

    data = block("window.STREAK_DATA=", ";\nwindow.STREAK_PLAYERS")
    players = block("window.STREAK_PLAYERS=", ";\nwindow.STREAK_META")
    # (type,scope) -> {length: rank} — STANDARD COMPETITION RANKING by length: every run
    # of a given length shares the same all-time rank (the leaderboard rows already carry
    # competition ranks, so equal lengths map to one rank).
    ranks = {}
    for typ, scopes in data.items():
        for sc, rows in scopes.items():
            m = {}
            for r in rows:                 # r = [rank, slug, length, ...]
                m.setdefault(r[2], r[0])
            ranks[(typ, sc)] = m
    return ranks, players


def flagger(players):
    def f(slug):
        p = players.get(slug)
        return BS.flag_html(p[1], p[2]) if p and p[1] else ""
    return f


# --------------------------------------------------------------------------- #
# ONE pass over the data: best player-team(-city) runs for EVERY team
# --------------------------------------------------------------------------- #
def all_teams_best(df, top=25):
    """{franchise: {(scope, type): [ {player, slug, length, start, end} sorted desc ]}}"""
    out = {}
    for scope_key, _ in BS.SCOPES:
        sub = E.scope_df(df, scope_key).sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True)
        n = len(sub)
        if n == 0:
            continue
        # group = (player, franchise); a run breaks on a trade to a DIFFERENT franchise,
        # but continues across a relocation/rename within the same franchise (Seattle->OKC)
        gkey = pd.factorize(sub["personId"].astype(str).str.cat(sub["franchise"], sep="|"))[0]
        team_arr = sub["franchise"].to_numpy(); names = sub["name"].to_numpy(); pids = sub["personId"].to_numpy()
        dates = sub["gameDate"].dt.date.to_numpy()
        same = np.empty(n, bool); same[0] = False; same[1:] = gkey[1:] == gkey[:-1]
        for s in E.STREAKS:
            sid = s["id"]
            c = sub[sid].to_numpy(dtype=bool)
            if not c.any():
                continue
            prev = np.empty(n, bool); prev[0] = False; prev[1:] = c[:-1]
            run_start = c & ~(prev & same)
            run_id = np.cumsum(run_start)
            counts = np.bincount(run_id[c])
            starts = np.where(run_start)[0]          # first row of each run (all True, contiguous)
            run_len = counts[run_id[starts]]
            run_end = starts + run_len - 1           # runs occupy contiguous positions
            rdf = pd.DataFrame({"team": team_arr[starts], "len": run_len,
                                "player": names[starts], "pid": pids[starts], "sp": starts, "ep": run_end})
            for team, grp in rdf.groupby("team", sort=False):
                rows = []
                for r in grp.nlargest(top, "len").itertuples(index=False):
                    rows.append({"player": str(r.player), "slug": BS.slugify(str(r.player), int(r.pid)),
                                 "length": int(r.len), "start": dates[int(r.sp)].isoformat(),
                                 "end": dates[int(r.ep)].isoformat()})
                rows.sort(key=lambda x: -x["length"])
                out.setdefault(team, {})[(scope_key, sid)] = rows
    return out


def all_teams_active(df, current_season):
    """{franchise: {(scope, type): [entries sorted desc]}} from the Last-Game trailing runs."""
    extended, _ended = F.season_end_status(df, current_season)
    out = {}
    for e in extended:
        fr = FR.franchise_of(e["team"], current_season)   # current-season team -> unambiguous
        out.setdefault(fr, {}).setdefault((e["scope"], e["type"]), []).append(e)
    for team in out:
        for k in out[team]:
            out[team][k].sort(key=lambda x: -x["length"])
    return out


# --------------------------------------------------------------------------- #
# Render a team page (scope -> family -> threshold; each sub-table ranked 1..N)
# --------------------------------------------------------------------------- #
def _render_section(by_scope_type, columns, row_fn, empty):
    html = ""
    for scope_key, scope_label in BS.SCOPES:
        present = any(by_scope_type.get((scope_key, s["id"])) for s in E.STREAKS)
        html += f'<h3 class="scopeh">{scope_label}</h3>\n'
        if not present:
            html += f'<p class="subtitle">{empty}</p>\n'
            continue
        cur_fam = None
        for s in E.STREAKS:
            entries = by_scope_type.get((scope_key, s["id"]))
            if not entries:
                continue
            if s["family"] != cur_fam:
                html += f'<h4 class="famh">{s["family"]}</h4>\n'
                cur_fam = s["family"]
            ts = (s["id"], scope_key)
            th = "".join(f'<th class="{c}">{h}</th>' for h, c in columns)
            positions = BS.competition_positions([e["length"] for e in entries])
            rows = "".join(row_fn(pos, e, ts) for pos, e in zip(positions, entries))
            html += (f'<div class="tcap">{BS.esc(s["label"])}</div><div class="table-card">'
                     f'<table class="board"><thead><tr>{th}</tr></thead><tbody>{rows}</tbody></table></div>\n')
    return html


def eras_line(eras):
    """eras = [(label, smin, smax)] -> human 'Seattle (1967–2008), Oklahoma City (2005–2026)'."""
    parts = []
    for label, smin, smax in eras:
        parts.append(f'{BS.esc(label)} <span class="erayr">{smin}–{smax + 1}</span>')
    return " · ".join(parts)


def build_team_page(target, best, active, ranks, players, eras=None):
    flag = flagger(players)

    def plink(e):
        return (f'<a class="plink" href="../players/{e["slug"]}.html">{BS.esc(e["player"])}</a>'
                + flag(e["slug"]))

    def best_row(i, e, ts):
        r = ranks.get(ts, {}).get(e["length"])
        rank = f'#{r}' if r else "—"
        return (f'<tr><td class="col-rank" data-label="#">{i}</td>'
                f'<td class="col-player" data-label="Player">{plink(e)}</td>'
                f'<td class="col-streak" data-label="Length">{e["length"]}</td>'
                f'<td class="col-date" data-label="Dates">{BS.fmt_iso(e["start"])} – {BS.fmt_iso(e["end"])}</td>'
                f'<td data-label="All-time rank">{rank}</td></tr>')

    def active_row(i, e, ts):
        badge = '<span class="mbadge">★ milestone</span>' if e.get("milestone") else ''
        return (f'<tr><td class="col-rank" data-label="#">{i}</td>'
                f'<td class="col-player" data-label="Player">{plink(e)}{badge}</td>'
                f'<td class="col-streak" data-label="Length">{e["length"]}</td>'
                f'<td class="col-date" data-label="Started">'
                f'{BS.fmt_iso(e.get("started") or e.get("last_date", ""))}</td></tr>')

    best_html = _render_section(
        best, [("#", "col-rank"), ("Player", "col-player"), ("Length", "col-streak"),
               ("Dates", "col-date"), ("All-time rank", "")], best_row, "No career-best runs of note.")
    active_html = _render_section(
        active, [("#", "col-rank"), ("Player", "col-player"), ("Length", "col-streak"),
                 ("Started", "col-date")], active_row, "No active streaks into the offseason.")

    n_best = sum(len(v) for v in best.values())
    n_active = sum(len(v) for v in active.values())
    title = f"{target} — Player Streaks by Franchise | NBA Statistical Streaks"
    desc = (f"{target} players' consecutive-game statistical streaks across the full franchise lineage: "
            f"career-best runs and current runs active into the offseason, by scope and stat family.")
    multi = eras and len(eras) > 1
    eras_html = (f'<p class="erasln"><span class="tl-label">Eras</span>{eras_line(eras)}</p>'
                 if multi else "")
    body = (
        f'<div class="wrap">\n<a class="backtop" href="../teams.html">← All franchises</a>\n'
        f'{BS.search_box()}\n'
        f'<header><span class="brand">HoopsHype · NBA Statistical Streaks</span>'
        f'<h1>{BS.esc(target)}</h1>'
        f'{eras_html}</header>\n'
        f'<h2 class="sech">🏆 Best ever <span class="note">— career-best runs on this team ({n_best})</span></h2>\n'
        f'{best_html}'
        f'<h2 class="sech">🔥 Active into the offseason <span class="note">— CURRENT trailing runs, not bests ({n_active})</span></h2>\n'
        f'{active_html}'
        f'{BS.search_box()}\n'
        f'<a class="backtop" href="../teams.html">← All franchises</a>\n</div>\n'
    )
    return BS.head(title, desc, prefix="../") + TEAM_CSS + BS.nav("teams", prefix="../") + body + BS.scripts_for("../")


# --------------------------------------------------------------------------- #
# Teams index (teams.html)
# --------------------------------------------------------------------------- #
def teams_index_html(best_all, active_all, appcounts, players, active_set):
    flag = flagger(players)

    def row(t):
        b = best_all.get(t, {})
        top_len, top = 0, None
        for (sc, sid), rows in b.items():
            if rows and rows[0]["length"] > top_len:
                top_len, top = rows[0]["length"], (rows[0], sid)
        n_best = sum(len(v) for v in b.values())
        n_active = sum(len(v) for v in active_all.get(t, {}).values())
        hl = (f'{top_len:,}-game {BS.esc(LABEL_BY_ID[top[1]])} · '
              f'<a class="plink" href="players/{top[0]["slug"]}.html">{BS.esc(top[0]["player"])}</a>'
              f'{flag(top[0]["slug"])}') if top else "—"
        return (f'<tr><td class="col-player" data-label="Team">'
                f'<a class="plink" href="teams/{team_slug(t)}.html">{BS.esc(t)}</a></td>'
                f'<td class="col-hl" data-label="Headline streak">{hl}</td>'
                f'<td class="col-date" data-label="Top runs">{n_best}</td>'
                f'<td class="col-date" data-label="Active">{n_active}</td></tr>')

    def table(team_list):
        rows = "".join(row(t) for t in sorted(team_list))
        return ('<div class="table-card"><table class="board"><thead><tr>'
                '<th>Team</th><th>Headline streak</th><th class="col-date">Top runs</th>'
                '<th class="col-date">Active</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>\n')

    teams = set(best_all) | set(active_all)
    active = [t for t in teams if t in active_set]
    defunct = [t for t in teams if t not in active_set]
    defunct_block = (f'<h2 class="sech">Defunct franchises <span class="note">— no modern successor '
                     f'({len(defunct)})</span></h2>\n{table(defunct)}') if defunct else ""
    body = (
        f'<div class="wrap">\n'
        f'<header><span class="brand">HoopsHype · NBA Statistical Streaks</span>'
        f'<h1>NBA <span class="accent">Franchises</span></h1></header>\n'
        f'{BS.search_box()}\n'
        f'<h2 class="sech">Franchises <span class="note">— all 30 active NBA teams ({len(active)})</span></h2>\n'
        f'{table(active)}'
        f'{defunct_block}'
        f'{BS.search_box()}\n</div>\n'
    )
    desc = ("Every NBA franchise's player streak leaders, merged by lineage (Seattle→Oklahoma City, "
            "New Jersey→Brooklyn, Vancouver→Memphis) — best consecutive-game runs and current active streaks.")
    return (BS.head("NBA Franchises — Player Streaks by Team", desc) + TEAM_CSS
            + BS.nav("teams") + body + BS.scripts_for(""))


TEAM_CSS = (
    '<style>'
    '.sech{border-bottom:2px solid var(--text);padding-bottom:.4rem;margin-top:2.4rem;font-size:1.3rem;}'
    '.sech .note{font-weight:400;font-size:.78rem;color:var(--muted);text-transform:none;letter-spacing:0;}'
    '.scopeh{font-size:1rem;margin:1.4rem 0 .3rem;color:var(--text);}'
    '.famh{font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;'
    'font-family:"JetBrains Mono",monospace;margin:1rem 0 .2rem;}'
    '.tcap{font-size:.78rem;font-weight:700;margin:.4rem 0 .2rem;}'
    '.erasln{display:flex;flex-wrap:wrap;align-items:baseline;gap:.4rem;margin:.5rem 0 0;'
    'font-size:.82rem;color:var(--text);font-family:"JetBrains Mono",monospace;}'
    '.erasln .tl-label{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;'
    'color:var(--muted);}'
    '.erayr{color:var(--muted);}'
    'table.board td.col-hl{font-family:"DM Sans",sans-serif;font-size:.8rem;white-space:normal;}'
    '.mbadge{display:inline-block;margin-left:.4rem;font-size:.58rem;font-weight:700;color:var(--accent);'
    'background:var(--accent-dim);border-radius:10px;padding:.1rem .4rem;vertical-align:middle;'
    'font-family:"JetBrains Mono",monospace;}'
    '.teamgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:.7rem;margin-top:1rem;}'
    '.teamcard{display:block;background:var(--surface);border:1px solid var(--border);border-radius:12px;'
    'padding:.9rem 1rem;color:var(--text);transition:.15s;}'
    '.teamcard:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim);}'
    '.tc-nm{font-weight:700;font-size:1.05rem;}'
    '.tc-hl{font-size:.8rem;color:var(--accent);margin-top:.3rem;font-family:"JetBrains Mono",monospace;}'
    '.tc-meta{font-size:.72rem;color:var(--muted);margin-top:.35rem;font-family:"JetBrains Mono",monospace;}'
    '</style>\n')


# --------------------------------------------------------------------------- #
# Legacy city-era URLs -> franchise pages (so old links / bookmarks still work)
# --------------------------------------------------------------------------- #
def write_redirects():
    labels = list(FR.CLEAN_MAP) + list(FR.SPLITS)
    n = 0
    for label in labels:
        old = team_slug(label)
        target = team_slug(FR.redirect_target(label))
        if old == target:
            continue
        url = f"{target}.html"
        html = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta http-equiv="refresh" content="0; url={url}">'
            f'<link rel="canonical" href="{url}">'
            '<meta name="robots" content="noindex">'
            f'<title>Redirecting… | NBA Statistical Streaks</title>'
            f'<script>location.replace("{url}");</script></head>'
            f'<body>This franchise page moved. <a href="{url}">Continue →</a></body></html>\n')
        with open(os.path.join(TEAMS_DIR, f"{old}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        n += 1
    return n


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", default="LA Lakers")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()

    print("Loading appearances…", flush=True)
    df = FR.add_franchise(E.load_appearances())
    appcounts = df["franchise"].value_counts().to_dict()

    # constituent city-eras per franchise (for the page subtitle), chronological
    eras_by_fr = {}
    er = (df.groupby(["franchise", "team_disp"])["season"].agg(["min", "max"]).reset_index()
            .sort_values("min"))
    for r in er.itertuples(index=False):
        eras_by_fr.setdefault(r.franchise, []).append((r.team_disp, int(r.min), int(r.max)))

    if a.list:
        print(f"{len(appcounts)} modern franchises:")
        for t, n in sorted(appcounts.items(), key=lambda kv: -kv[1]):
            eras = ", ".join(lbl for lbl, _, _ in eras_by_fr.get(t, []))
            print(f"  {t:<24} {n:>7,}  -> teams/{team_slug(t)}.html   [{eras}]")
        return

    current_season = F.nba_season(df["gameDate"].max().date())
    # active franchise = one that played in the current (2025-26) season (all 30 do)
    last_season = df.groupby("franchise")["season"].max()
    active_set = set(last_season[last_season == current_season].index)

    ranks, players = load_streakdata()
    os.makedirs(TEAMS_DIR, exist_ok=True)
    print("Computing best runs for all franchises (one pass)…", flush=True)
    best_all = all_teams_best(df)
    print("Computing active trailing runs…", flush=True)
    active_all = all_teams_active(df, current_season)

    if a.all:
        teams = sorted(set(best_all) | set(active_all))
        for t in teams:
            html = build_team_page(t, best_all.get(t, {}), active_all.get(t, {}),
                                   ranks, players, eras_by_fr.get(t))
            with open(os.path.join(TEAMS_DIR, f"{team_slug(t)}.html"), "w", encoding="utf-8") as f:
                f.write(html)
        with open(os.path.join(BASE, "teams.html"), "w", encoding="utf-8") as f:
            f.write(teams_index_html(best_all, active_all, appcounts, players, active_set))
        n_redir = write_redirects()
        print(f"Wrote teams.html ({len(active_set)} active / {len(teams)-len(active_set)} defunct) "
              f"+ {len(teams)} franchise pages + {n_redir} legacy redirects.")
    else:
        t = a.team
        if t not in appcounts:
            cand = FR.CLEAN_MAP.get(t) or FR.REDIRECT_MODERN.get(t)
            if cand:
                t = cand
            else:
                print(f"'{a.team}' not found. Try --list.")
                return
        html = build_team_page(t, best_all.get(t, {}), active_all.get(t, {}),
                               ranks, players, eras_by_fr.get(t))
        with open(os.path.join(TEAMS_DIR, f"{team_slug(t)}.html"), "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Wrote teams/{team_slug(t)}.html")


if __name__ == "__main__":
    main()

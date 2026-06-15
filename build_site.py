"""
build_site.py — Phase 2 site builder for NBA Statistical Streaks.

Imports the validated engine (build_streaks.py) and emits a static site:
  index.html            family-sectioned streak leaderboards (chips picker + scope tabs)
  feats.html            rarest single-game feats ranked by career COUNT
  players/<slug>.html    one page per player (best streak per type, feat counts, ranks)
  streaks-data.js / feats-data.js / search-index.js

Design system mirrors the Media Vote Tracker / Iron Man sibling tools.
"""
import os
import re
import json
import html
import unicodedata
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

import build_streaks as E

BASE = r"C:\nba-stat-streaks"
DATA = os.path.join(BASE, "data")
PLAYERS_DIR = os.path.join(BASE, "players")
PLAYERS_CSV = os.path.join(DATA, "Players.csv")
NATIONALITIES_CSV = os.path.join(DATA, "nationalities.csv")

SCOPES = [("regular", "Regular Season"), ("playoffs", "Playoffs"), ("combined", "Combined")]
SCOPE_LABEL = dict(SCOPES)
FAMILIES = ["Scoring", "Rebounding", "Playmaking", "Defense", "Shooting", "Combo"]
TOPN = 100

esc = lambda s: html.escape(str(s), quote=True)

KNOWN_RECORDS = {
    "pts10": ("LeBron James", 1290, "consecutive games scoring in double figures — the live all-time record (broke Jordan's 866)"),
    "pts30": ("Wilt Chamberlain", 65, "consecutive 30-point games (1961-62)"),
    "dd":    ("Wilt Chamberlain", 227, "consecutive double-doubles (1964-67)"),
}

# single-game feats, ranked by career count (regular season)
FEATS = [
    {"id": "td",   "label": "Triple-doubles"},
    {"id": "f5x5", "label": "5×5 games"},
    {"id": "p40",  "label": "40-point games"},
    {"id": "p50",  "label": "50-point games"},
    {"id": "p60",  "label": "60-point games"},
    {"id": "r20",  "label": "20-rebound games"},
    {"id": "a20",  "label": "20-assist games"},
]

# --------------------------------------------------------------------------- #
# Player meta + flags
# --------------------------------------------------------------------------- #
COUNTRY_ISO = {
    "USA": "us", "United States": "us", "United States of America": "us", "US": "us",
    "Canada": "ca", "France": "fr", "Serbia": "rs", "Australia": "au", "Croatia": "hr",
    "Spain": "es", "Brazil": "br", "Lithuania": "lt", "Argentina": "ar", "Germany": "de",
    "Senegal": "sn", "Turkey": "tr", "Türkiye": "tr", "Slovenia": "si", "Nigeria": "ng",
    "Greece": "gr", "Italy": "it", "Russia": "ru", "Ukraine": "ua", "Puerto Rico": "pr",
    "United Kingdom": "gb", "England": "gb", "Great Britain": "gb", "Georgia": "ge",
    "Latvia": "lv", "Montenegro": "me", "Bosnia and Herzegovina": "ba", "Bosnia": "ba",
    "Dominican Republic": "do", "Cameroon": "cm", "Switzerland": "ch", "Mexico": "mx",
    "China": "cn", "Japan": "jp", "Israel": "il", "Czech Republic": "cz", "Czechia": "cz",
    "Poland": "pl", "Netherlands": "nl", "Sweden": "se", "Finland": "fi", "Austria": "at",
    "Belgium": "be", "Angola": "ao", "Sudan": "sd", "South Sudan": "ss",
    "Democratic Republic of the Congo": "cd", "Congo": "cg", "Mali": "ml",
    "Ivory Coast": "ci", "Tunisia": "tn", "Egypt": "eg", "Morocco": "ma",
    "New Zealand": "nz", "Jamaica": "jm", "Bahamas": "bs", "Venezuela": "ve",
    "Colombia": "co", "Panama": "pa", "Haiti": "ht", "Cape Verde": "cv", "Guinea": "gn",
    "Gabon": "ga", "Ghana": "gh", "Iran": "ir", "Lebanon": "lb", "Philippines": "ph",
    "South Korea": "kr", "Korea": "kr", "India": "in", "Estonia": "ee", "Hungary": "hu",
    "Romania": "ro", "Bulgaria": "bg", "Slovakia": "sk", "Portugal": "pt", "Ireland": "ie",
    "Norway": "no", "Denmark": "dk", "Uruguay": "uy", "North Macedonia": "mk",
    "Macedonia": "mk", "Kazakhstan": "kz", "U.S. Virgin Islands": "vi",
    "Virgin Islands": "vi", "Trinidad and Tobago": "tt",
}


def normname(s):
    s = unicodedata.normalize("NFKD", str(s).lower()).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def slugify(name, pid):
    s = unicodedata.normalize("NFKD", str(name).lower().replace("'", "")).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return f"{s}-{pid}"


def country_to_flag(country):
    if not country:
        return None
    c = str(country).strip()
    return COUNTRY_ISO.get(c) or COUNTRY_ISO.get(c.title())


def load_flags():
    pl = pd.read_csv(PLAYERS_CSV, usecols=["personId", "firstName", "lastName", "country"], low_memory=False)
    country_by_pid, name_by_pid = {}, {}
    for r in pl.itertuples(index=False):
        pid = int(r.personId)
        name_by_pid[pid] = f"{r.firstName} {r.lastName}"
        if isinstance(r.country, str) and r.country.strip():
            country_by_pid[pid] = r.country.strip()
    nat = {}
    try:
        nd = pd.read_csv(NATIONALITIES_CSV, low_memory=False)
        cols = {c.lower(): c for c in nd.columns}
        pcol, ncol = cols.get("player"), cols.get("nationality")
        if pcol and ncol:
            for r in nd[[pcol, ncol]].itertuples(index=False):
                if isinstance(r[0], str) and isinstance(r[1], str) and r[0].strip() and r[1].strip():
                    nat[normname(r[0])] = r[1].strip()
    except Exception:
        pass
    return country_by_pid, name_by_pid, nat


# --------------------------------------------------------------------------- #
# Single pass: global top-N runs AND per-player best run, from one run_id pass
# --------------------------------------------------------------------------- #
def compute_all(sub, gkey, cond_col, topn=TOPN):
    c = sub[cond_col].to_numpy(dtype=bool)
    n = len(c)
    if n == 0:
        return [], {}
    same_prev = np.empty(n, bool); same_prev[0] = False; same_prev[1:] = gkey[1:] == gkey[:-1]
    prev_c = np.empty(n, bool); prev_c[0] = False; prev_c[1:] = c[:-1]
    run_start = c & ~(prev_c & same_prev)
    run_id = np.cumsum(run_start)
    idx = np.where(c)[0]
    rid_true = run_id[c]
    counts = np.bincount(rid_true)
    d = pd.DataFrame({"pid": gkey[idx], "len": counts[rid_true], "rid": rid_true, "pos": idx})

    best_rows = d.loc[d.groupby("pid")["len"].idxmax()]      # one row per player (their best run)
    best_rids = set(int(r) for r in best_rows["rid"])
    top_ids = [int(r) for r in np.argsort(counts)[::-1] if counts[r] > 0][:topn]
    care = set(top_ids) | best_rids
    dc = d[d["rid"].isin(care)]
    g = dc.groupby("rid")
    fp = g["pos"].min(); lp = g["pos"].max()

    def detail(rid):
        f, l = int(fp[rid]), int(lp[rid])
        sl = sub.iloc[f:l + 1]
        teams = list(dict.fromkeys(t for t in sl["team_disp"] if t))
        return {"length": l - f + 1, "player": sl["name"].iloc[0], "personId": int(sl["personId"].iloc[0]),
                "start": sl["gameDate"].iloc[0].date().isoformat(),
                "end": sl["gameDate"].iloc[-1].date().isoformat(), "teams": ", ".join(teams)}

    top_list = [detail(rid) for rid in top_ids]
    best_by_pid = {}
    for rid in best_rids:
        de = detail(rid)
        best_by_pid[de["personId"]] = de
    return top_list, best_by_pid


# --------------------------------------------------------------------------- #
# Build all data
# --------------------------------------------------------------------------- #
def build_all():
    print("Loading appearances…", flush=True)
    df = E.load_appearances()
    country_by_pid, name_by_pid, nat = load_flags()

    players = {}        # slug -> [name, iso, country]
    slug_by_pid = {}
    name_by_pid_app = {}

    def reg_player(pid, name):
        if pid in slug_by_pid:
            return slug_by_pid[pid]
        slug = slugify(name, pid)
        key = normname(name_by_pid.get(pid, name))
        country = nat.get(key) or country_by_pid.get(pid)
        iso = country_to_flag(country) or ""
        players[slug] = [name, iso, country if iso else ""]
        slug_by_pid[pid] = slug
        name_by_pid_app[pid] = name
        return slug

    STREAK_DATA = {s["id"]: {} for s in E.STREAKS}
    player_best = {}     # (sid, scope) -> {pid: detail}
    rank_map = {}        # (sid, scope) -> {slug: rank}

    for scope_key, _ in SCOPES:
        print(f"  streaks scope: {scope_key}", flush=True)
        sub = E.scope_df(df, scope_key).sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True)
        gkey = sub["personId"].to_numpy()
        for s in E.STREAKS:
            top, best = compute_all(sub, gkey, s["id"], topn=TOPN)
            out, rmap = [], {}
            for i, r in enumerate(top, 1):
                slug = reg_player(r["personId"], r["player"])
                out.append([i, slug, r["length"], r["start"], r["end"], r["teams"]])
                rmap.setdefault(slug, i)
            STREAK_DATA[s["id"]][scope_key] = out
            rank_map[(s["id"], scope_key)] = rmap
            player_best[(s["id"], scope_key)] = best

    # ----- feats (regular season career counts) -----
    print("  feats (regular season)…", flush=True)
    reg = E.scope_df(df, "regular")
    pts, reb, ast = reg["points"], reg["reboundsTotal"], reg["assists"]
    stl, blk = reg["steals"], reg["blocks"]
    feat_bool = {
        "td": ((pts >= 10).astype(int) + (reb >= 10).astype(int) + (ast >= 10).astype(int)
               + (stl >= 10).astype(int) + (blk >= 10).astype(int)) >= 3,
        "f5x5": (pts >= 5) & (reb >= 5) & (ast >= 5) & (stl >= 5) & (blk >= 5),
        "p40": pts >= 40, "p50": pts >= 50, "p60": pts >= 60,
        "r20": reb >= 20, "a20": ast >= 20,
    }
    FEAT_DATA = {}
    feat_player = {}     # pid -> {feat_id: [count, firstYear, lastYear]}
    yr = reg["gameDate"].dt.year
    for f in FEATS:
        m = reg[feat_bool[f["id"]]]
        if len(m) == 0:
            FEAT_DATA[f["id"]] = []
            continue
        grp = m.groupby("personId")
        agg = grp.agg(count=("points", "size"), name=("name", "first"))
        years = m.groupby("personId")["gameDate"].agg(["min", "max"])
        agg["fy"] = years["min"].dt.year; agg["ly"] = years["max"].dt.year
        agg = agg.sort_values("count", ascending=False)
        out = []
        for i, (pid, row) in enumerate(agg.head(TOPN).iterrows(), 1):
            slug = reg_player(int(pid), row["name"])
            out.append([i, slug, int(row["count"]), int(row["fy"]), int(row["ly"])])
        FEAT_DATA[f["id"]] = out
        for pid, row in agg.iterrows():
            feat_player.setdefault(int(pid), {})[f["id"]] = [int(row["count"]), int(row["fy"]), int(row["ly"])]

    STREAK_META = [{"id": s["id"], "label": s["label"], "family": s["family"]} for s in E.STREAKS]
    STREAK_RECORDS = {k: {"holder": v[0], "num": v[1], "note": v[2]} for k, v in KNOWN_RECORDS.items()}

    # ----- assemble per-player page payloads -----
    page_pids = set()
    for (sid, scope), best in player_best.items():
        for pid, de in best.items():
            if de["length"] >= 2:
                page_pids.add(pid)
    page_pids |= set(feat_player.keys())

    # register every page player (some have a best streak/feat but never cracked a
    # top-100 leaderboard, so reg_player() wasn't called for them yet).
    pid2name = df.drop_duplicates("personId").set_index("personId")["name"].to_dict()
    for pid in page_pids:
        reg_player(int(pid), pid2name.get(int(pid), f"Player {pid}"))

    label_by_id = {s["id"]: s["label"] for s in E.STREAKS}
    feat_label = {f["id"]: f["label"] for f in FEATS}

    # ----- headline (for cards) + similar players (rabbit-hole hooks) -----
    headline = {}   # slug -> {"text":..., "val":...}
    for pid in page_pids:
        slug = slug_by_pid[pid]
        best = None
        for (sid, scope), pb in player_best.items():
            de = pb.get(pid)
            if de and de["length"] >= 2 and (best is None or de["length"] > best[0]):
                best = (de["length"], label_by_id[sid])
        if best:
            headline[slug] = {"text": f"{best[0]:,}-game {best[1].lower()} streak", "val": best[0]}
        else:
            fp = feat_player.get(pid, {})
            if fp:
                fid = max(fp, key=lambda k: fp[k][0])
                headline[slug] = {"text": f"{fp[fid][0]} {feat_label[fid].lower()}", "val": fp[fid][0]}
            else:
                headline[slug] = {"text": "", "val": 0}

    # which leaderboards each player sits on, and each board's slug order (by rank)
    boards_of = defaultdict(list)            # slug -> [(sid, scope, rank), ...]
    for (sid, scope), rmap in rank_map.items():
        for s, rank in rmap.items():
            boards_of[s].append((sid, scope, rank))
    board_order = {(sid, scope): [row[1] for row in rows]
                   for sid, scopes in STREAK_DATA.items() for scope, rows in scopes.items()}

    slugs_by_val = sorted(headline, key=lambda s: headline[s]["val"])
    pos = {s: i for i, s in enumerate(slugs_by_val)}

    def similar_for(slug):
        # primary heuristic: players who share the most all-time leaderboards with you,
        # nearest to your rank on each.
        counts, prox = Counter(), {}
        for (sid, scope, rank) in boards_of.get(slug, []):
            order = board_order[(sid, scope)]
            i = rank - 1
            for j in range(max(0, i - 6), min(len(order), i + 7)):
                o = order[j]
                if o == slug:
                    continue
                counts[o] += 1
                prox[o] = min(prox.get(o, 99), abs(j - i))
        cands = sorted(counts, key=lambda o: (-counts[o], prox[o]))[:6]
        # fallback: fill from nearest headline value (so every page has 6)
        if len(cands) < 6 and slug in pos:
            seen = set(cands) | {slug}
            lo, hi = pos[slug] - 1, pos[slug] + 1
            while len(cands) < 6 and (lo >= 0 or hi < len(slugs_by_val)):
                for j in ([lo] if lo >= 0 else []) + ([hi] if hi < len(slugs_by_val) else []):
                    o = slugs_by_val[j]
                    if o not in seen and len(cands) < 6:
                        cands.append(o); seen.add(o)
                lo -= 1; hi += 1
        return cands

    similar = {slug_by_pid[pid]: similar_for(slug_by_pid[pid]) for pid in page_pids}

    return dict(df=df, players=players, slug_by_pid=slug_by_pid, STREAK_DATA=STREAK_DATA,
                STREAK_META=STREAK_META, STREAK_RECORDS=STREAK_RECORDS, FEAT_DATA=FEAT_DATA,
                FEATS=FEATS, feat_label=feat_label, label_by_id=label_by_id,
                player_best=player_best, rank_map=rank_map, feat_player=feat_player,
                page_pids=page_pids, headline=headline, similar=similar)


# --------------------------------------------------------------------------- #
# Shared HTML scaffolding
# --------------------------------------------------------------------------- #
def css():
    return CSS


def head(title, desc, prefix=""):
    return (
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{esc(title)}</title>\n"
        f"<meta name=\"description\" content=\"{esc(desc)}\">\n"
        f"<meta property=\"og:title\" content=\"{esc(title)}\">\n"
        f"<meta property=\"og:description\" content=\"{esc(desc)}\">\n"
        "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">\n"
        "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>\n"
        "<link href=\"https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap\" rel=\"stylesheet\">\n"
        f"<style>{CSS}</style>\n</head>\n<body>\n"
    )


def nav(active, prefix=""):
    def a(href, label, key):
        cls = ' class="active"' if key == active else ''
        return f'<a href="{prefix}{href}"{cls}>{label}</a>'
    return (f'<nav class="nav">{a("index.html","Leaderboards","lb")}'
            f'{a("feats.html","Rarest Feats","feats")}'
            f'{a("lastgame.html","Last Game","lastgame")}</nav>\n')


def search_box():
    return ('<div class="psearch-wrap"><input class="psearch" type="search" '
            'placeholder="Search any player by name…" autocomplete="off" spellcheck="false">'
            '<div class="ac" role="listbox"></div></div>')


def scripts_for(prefix, extra=""):
    return (f'<script>var PLAYER_PREFIX="{prefix}players/";</script>\n'
            f'<script src="{prefix}search-index.js"></script>\n{extra}{GLOBAL_SEARCH_JS}\n')


# --------------------------------------------------------------------------- #
# index.html (leaderboards)
# --------------------------------------------------------------------------- #
def family_chips_html(meta):
    blocks = []
    for fam in FAMILIES:
        chips = "".join(f'<button class="chip" data-streak="{m["id"]}">{esc(m["label"])}</button>'
                        for m in meta if m["family"] == fam)
        if chips:
            blocks.append(f'<div class="fam"><span class="flabel">{fam}</span>{chips}</div>')
    return f'<div class="fams">{"".join(blocks)}</div>'


def build_index_html(meta):
    tabs = "".join(f'<button class="tab" data-scope="{k}">{lbl}</button>' for k, lbl in SCOPES)
    body = (
        f'<div class="wrap">\n'
        f'<header><span class="brand">HoopsHype · NBA Statistical Streaks</span>'
        f'<h1>NBA <span class="accent">Statistical Streaks</span></h1>'
        f'<p class="subtitle">Longest runs of consecutive games hitting a statistical mark — skipping missed games, '
        f'breaking only on an appearance that falls short. 1946–present.</p></header>\n'
        # Tracker panel — links to lastgame.html (written by the nightly job); the
        # subtitle is filled in client-side from active-state.json so the panel never needs
        # the build to re-run.
        f'<a class="lnpanel" href="lastgame.html"><span class="lnp-ic">🏀</span>'
        f'<span class="lnp-tx"><b>Last Game</b><span class="lnp-sub" id="lnp-sub">'
        f'Active-streak movement — see what extended, ended, or hit a milestone</span></span>'
        f'<span class="lnp-go">→</span></a>\n'
        f'<script>fetch("active-state.json").then(function(r){{return r.json();}}).then(function(s){{'
        f'document.getElementById("lnp-sub").textContent=s.count+" active streaks tracked · updated "+s.data_through;'
        f'}}).catch(function(){{}});</script>\n'
        f'{search_box()}\n'
        f'<div class="controls"><div class="tabs">{tabs}</div></div>\n'
        f'{family_chips_html(meta)}\n'
        f'<div class="record" id="record-flag" style="display:none"></div>\n'
        f'<h2 id="active-label">10+ points</h2>\n'
        f'<div class="table-card"><table class="board"><thead><tr>'
        f'<th class="col-rank">Rank</th><th>Player</th><th class="col-streak">Streak</th>'
        f'<th class="col-date">Start</th><th class="col-date">End</th><th class="col-team">Team(s)</th>'
        f'</tr></thead><tbody id="tbody"></tbody></table></div>\n'
        f'{search_box()}\n'
        f'<div class="foot">Engine validated vs official records — Wilt 65 (30+), Wilt 227 (double-doubles), '
        f'LeBron 1,290 &amp; Jordan 866 (10+). Appearance = a game played; missed games are skipped, not breaks.</div>\n'
        f'</div>\n'
    )
    desc = ("All-time NBA consecutive-game statistical streak leaderboards: 10+/20+/30+ points, double-doubles, "
            "rebounds, assists, steals, blocks and threes in a row — regular season, playoffs and combined.")
    return (head("NBA Statistical Streaks — Consecutive-Game Leaderboards", desc)
            + nav("lb") + body
            + scripts_for("", '<script src="streaks-data.js"></script>\n' + RENDER_JS + "\n"))


# --------------------------------------------------------------------------- #
# feats.html
# --------------------------------------------------------------------------- #
def build_feats_html(feats):
    chips = "".join(f'<button class="chip" data-feat="{f["id"]}">{esc(f["label"])}</button>' for f in feats)
    body = (
        f'<div class="wrap">\n'
        f'<header><span class="brand">HoopsHype · NBA Statistical Streaks</span>'
        f'<h1>Rarest <span class="accent">Feats</span></h1>'
        f'<p class="subtitle">Single-game feats ranked by career count — regular-season totals, 1946–present.</p></header>\n'
        f'{search_box()}\n'
        f'<div class="fams"><div class="fam"><span class="flabel">Feat</span>{chips}</div></div>\n'
        f'<h2 id="active-label">Triple-doubles</h2>\n'
        f'<div class="table-card"><table class="board"><thead><tr>'
        f'<th class="col-rank">Rank</th><th>Player</th><th class="col-streak">Count</th>'
        f'<th class="col-date">Span</th></tr></thead><tbody id="tbody"></tbody></table></div>\n'
        f'{search_box()}\n'
        f'<div class="foot">Triple-double = 10+ in three of pts/reb/ast/stl/blk. 5×5 = 5+ in all five '
        f'(steals &amp; blocks tracked from 1973-74).</div>\n'
        f'</div>\n'
    )
    desc = ("NBA single-game feat leaders by career count: triple-doubles, 5×5 games, 40/50/60-point games, "
            "20-rebound and 20-assist games — regular-season totals.")
    return (head("NBA Rarest Feats — Triple-Doubles, 50-Point Games & More", desc)
            + nav("feats") + body
            + scripts_for("", '<script src="feats-data.js"></script>\n' + FEATS_RENDER_JS + "\n"))


# --------------------------------------------------------------------------- #
# player pages
# --------------------------------------------------------------------------- #
def flag_html(iso, country, big=False):
    if not iso:
        return ""
    h = "h40" if big else "h20"
    cls = "flag-img flag-lg" if big else "flag-img"
    ht = "" if big else ' height="14"'
    return (f' <img class="{cls}" src="https://flagcdn.com/{h}/{iso}.png" '
            f'srcset="https://flagcdn.com/h40/{iso}.png 2x"{ht} alt="{esc(country)}" title="{esc(country)}">')


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_iso(s):
    if not s:
        return ""
    y, m, d = s.split("-")
    return f"{MONTHS[int(m) - 1]} {int(d)}, {y}"


def build_player_page(pid, ctx):
    players = ctx["players"]; slug = ctx["slug_by_pid"][pid]
    name, iso, country = players[slug]
    label_by_id = ctx["label_by_id"]; feat_label = ctx["feat_label"]

    # one table per scope: every streak type where this player has a 2+ run in that
    # scope, sorted by length desc. (Scope column dropped — it's now the table header.)
    headline = None

    def scope_table(scope_key, scope_label):
        rrows = []
        for s in ctx["STREAK_META"]:
            sid = s["id"]
            de = ctx["player_best"].get((sid, scope_key), {}).get(pid)
            if de and de["length"] >= 2:
                rank = ctx["rank_map"].get((sid, scope_key), {}).get(slug)
                rrows.append({"label": label_by_id[sid], "len": de["length"], "start": de["start"],
                              "end": de["end"], "teams": de["teams"], "rank": rank})
        rrows.sort(key=lambda r: r["len"], reverse=True)
        if not rrows:
            return ""
        body_rows = "".join(
            f'<tr><td class="col-player" data-label="Streak">{esc(r["label"])}</td>'
            f'<td class="col-streak" data-label="Best">{r["len"]}</td>'
            f'<td class="col-date" data-label="Dates">{fmt_iso(r["start"])} – {fmt_iso(r["end"])}</td>'
            f'<td class="col-team" data-label="Team(s)">{esc(r["teams"] or "—")}</td>'
            f'<td data-label="All-time rank">{("#"+str(r["rank"])) if r["rank"] else "—"}</td></tr>'
            for r in rrows)
        return (f'<h2>{scope_label}</h2>'
                '<div class="table-card"><table class="board"><thead><tr>'
                '<th>Streak</th><th class="col-streak">Best</th>'
                '<th class="col-date">Dates</th><th class="col-team">Team(s)</th><th>All-time rank</th>'
                f'</tr></thead><tbody>{body_rows}</tbody></table></div>\n')

    # headline = single longest streak across every type/scope (for the SEO blurb)
    for s in ctx["STREAK_META"]:
        for scope_key, _ in SCOPES:
            de = ctx["player_best"].get((s["id"], scope_key), {}).get(pid)
            if de and de["length"] >= 2 and (headline is None or de["length"] > headline["len"]):
                headline = {"len": de["length"], "label": label_by_id[s["id"]]}

    streak_tbl = (scope_table("regular", "Regular Season")
                  + scope_table("playoffs", "Playoffs")
                  + scope_table("combined", "Combined"))

    # feats
    fp = ctx["feat_player"].get(pid, {})
    feat_tbl = ""
    if fp:
        frows = "".join(
            f'<tr><td class="col-player" data-label="Feat">{esc(feat_label[fid])}</td>'
            f'<td class="col-streak" data-label="Count">{c[0]}</td>'
            f'<td class="col-date" data-label="Span">{c[1]}–{c[2]}</td></tr>'
            for fid, c in sorted(fp.items(), key=lambda kv: -kv[1][0]))
        feat_tbl = (
            '<h2>Career Feats <span class="note">(regular season)</span></h2>'
            '<div class="table-card"><table class="board"><thead><tr>'
            '<th>Feat</th><th class="col-streak">Count</th><th class="col-date">Span</th>'
            f'</tr></thead><tbody>{frows}</tbody></table></div>\n')

    if not streak_tbl and not feat_tbl:
        streak_tbl = '<p class="subtitle">No qualifying streaks or feats on record.</p>'

    # Similar Players — rabbit-hole hooks (players who share your all-time leaderboards).
    sim = ctx["similar"].get(slug, [])
    similar_html = ""
    if sim:
        pm, hm = ctx["players"], ctx["headline"]
        cards = "".join(
            f'<a class="simcard" href="{o}.html">'
            f'<div class="sim-nm">{esc(pm[o][0])}{flag_html(pm[o][1], pm[o][2])}</div>'
            f'<div class="sim-hl">{esc(hm.get(o, {}).get("text", ""))}</div></a>'
            for o in sim)
        similar_html = ('<h2>Similar Players <span class="note">(share your all-time leaderboards)</span></h2>'
                        f'<div class="simgrid">{cards}</div>\n')

    hl = (f"longest streak of {headline['len']} {headline['label'].lower()} in a row"
          if headline else "career consecutive-game streaks and feats")
    title = f"{name} NBA Streaks: Consecutive Games Stats and Career Feats"
    desc = (f"{name}'s NBA consecutive-game streaks and career feats — {hl}. "
            f"Regular season, playoffs and combined, with all-time ranks.")

    body = (
        f'<div class="wrap">\n'
        f'<a class="backtop" href="../index.html">← All streak leaderboards</a>\n'
        f'{search_box()}\n'
        f'<header><h1>{esc(name)}{flag_html(iso, country, big=True)}</h1>'
        f'<p class="subtitle">{esc(hl[0].upper() + hl[1:])}.</p></header>\n'
        f'{streak_tbl}{feat_tbl}{similar_html}'
        f'{search_box()}\n'
        f'<a class="backtop" href="../index.html">← All streak leaderboards</a>\n'
        f'</div>\n'
    )
    return head(title, desc, prefix="../") + nav("lb", prefix="../") + body + scripts_for("../")


# --------------------------------------------------------------------------- #
# CSS / JS
# --------------------------------------------------------------------------- #
CSS = r"""
:root{--bg:#f5f5f7;--surface:#fff;--surface-hover:#f0f0f2;--border:#d1d1d6;--text:#1d1d1f;
--muted:#6e6e73;--accent:#3b82f6;--accent-dim:rgba(59,130,246,.15);--green:#34c759;--gold:#b8860b;}
*{box-sizing:border-box;}html,body{margin:0;padding:0;}
body{background:var(--bg);color:var(--text);line-height:1.5;-webkit-font-smoothing:antialiased;
font-family:'DM Sans',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}
a{color:var(--accent);text-decoration:none;}
.flag-img{vertical-align:middle;border-radius:2px;box-shadow:0 0 0 .5px rgba(0,0,0,.18);margin-left:.25rem;}
.flag-lg{height:22px;}
.nav{display:flex;gap:.3rem;flex-wrap:wrap;max-width:1280px;margin:0 auto;padding:1.5rem 1.5rem .3rem;}
.nav a{font-family:'JetBrains Mono',monospace;font-size:.72rem;font-weight:600;padding:.4rem .8rem;
border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--muted);}
.nav a:hover{border-color:var(--accent);color:var(--accent);}
.nav a.active{background:var(--accent);border-color:var(--accent);color:#fff;}
.wrap{max-width:1280px;margin:0 auto;padding:.5rem 1.5rem 4rem;}
.brand{font-family:'JetBrains Mono',monospace;font-size:.7rem;color:var(--muted);text-transform:uppercase;
letter-spacing:.08em;display:block;margin-bottom:.3rem;}
h1{font-size:clamp(1.4rem,3.5vw,1.7rem);font-weight:700;letter-spacing:-.03em;margin:0 0 .3rem;
display:flex;align-items:center;gap:.3rem;flex-wrap:wrap;}
h1 .accent{color:var(--accent);}
h2{font-size:1.15rem;font-weight:700;letter-spacing:-.02em;margin:1.6rem 0 .7rem;}
h2 .note{font-weight:400;font-size:.8rem;color:var(--muted);}
.subtitle{color:var(--muted);font-size:.9rem;margin:0;max-width:60rem;}
.backtop{display:inline-block;font-family:'JetBrains Mono',monospace;font-size:.78rem;color:var(--muted);margin:.3rem 0;}
.backtop:hover{color:var(--accent);}
.psearch-wrap{position:relative;margin:1rem 0;}
.psearch{width:100%;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);
font-size:.9rem;font-family:inherit;padding:.6rem .8rem;outline:none;transition:.15s;}
.psearch:hover{border-color:var(--accent);}.psearch:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim);}
.ac{position:absolute;top:100%;left:0;right:0;margin-top:.25rem;background:var(--surface);border:1px solid var(--border);
border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.12);max-height:20rem;overflow-y:auto;z-index:60;display:none;}
.ac.open{display:block;}
.ac-item{display:block;padding:.55rem .8rem;border-bottom:1px solid var(--border);font-size:.88rem;color:var(--text);}
.ac-item:last-child{border-bottom:none;}.ac-item:hover,.ac-item.sel{background:var(--surface-hover);color:var(--accent);}
.ac-empty{padding:.7rem .8rem;color:var(--muted);font-size:.8rem;text-align:center;}
.controls{display:flex;flex-wrap:wrap;gap:.6rem;align-items:center;margin:1rem 0 .4rem;}
.tabs{display:flex;gap:.3rem;flex-wrap:wrap;}
.tab{font-family:'JetBrains Mono',monospace;font-size:.72rem;font-weight:600;padding:.45rem .85rem;cursor:pointer;
border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--muted);}
.tab:hover{border-color:var(--accent);color:var(--accent);}
.tab.active{background:var(--accent);border-color:var(--accent);color:#fff;}
.fams{display:flex;flex-direction:column;gap:.5rem;margin:.6rem 0 1rem;}
.fam{display:flex;flex-wrap:wrap;align-items:center;gap:.4rem;}
.fam .flabel{font-family:'JetBrains Mono',monospace;font-size:.62rem;font-weight:700;text-transform:uppercase;
letter-spacing:.06em;color:var(--muted);min-width:6.5rem;}
.chip{font-size:.76rem;font-weight:600;padding:.32rem .7rem;cursor:pointer;border:1px solid var(--border);
border-radius:20px;background:var(--surface);color:var(--text);}
.chip:hover{border-color:var(--accent);color:var(--accent);}
.chip.active{background:var(--accent-dim);border-color:var(--accent);color:var(--accent);}
.record{display:flex;gap:.5rem;align-items:flex-start;background:var(--surface);border:1px solid var(--border);
border-left:3px solid var(--gold);border-radius:8px;padding:.6rem .9rem;margin:.4rem 0 .8rem;font-size:.82rem;}
.record b{color:var(--text);}.record .ok{color:var(--green);font-weight:700;}.record .no{color:var(--accent);font-weight:700;}
.lnpanel{display:flex;align-items:center;gap:.7rem;background:var(--surface);border:1px solid var(--border);
border-left:3px solid var(--accent);border-radius:10px;padding:.7rem .9rem;margin:.2rem 0 .4rem;color:var(--text);transition:.15s;}
.lnpanel:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim);}
.lnp-ic{font-size:1.3rem;}.lnp-tx{display:flex;flex-direction:column;flex:1;min-width:0;}
.lnp-tx b{font-size:.95rem;}.lnp-sub{font-size:.76rem;color:var(--muted);}
.lnp-go{font-family:'JetBrains Mono',monospace;color:var(--accent);font-weight:700;}
.simgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:.6rem;}
.simcard{display:block;background:var(--surface);border:1px solid var(--border);border-radius:10px;
padding:.7rem .8rem;color:var(--text);transition:.15s;}
.simcard:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-dim);}
.sim-nm{font-weight:600;font-size:.92rem;display:flex;align-items:center;gap:.2rem;}
.sim-hl{font-size:.76rem;color:var(--accent);margin-top:.25rem;font-family:'JetBrains Mono',monospace;}
.table-card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow-x:auto;}
table.board{width:100%;border-collapse:collapse;font-size:.86rem;}
table.board thead th{background:var(--surface-hover);color:var(--muted);text-align:left;font-weight:600;font-size:.66rem;
letter-spacing:.04em;text-transform:uppercase;padding:.6rem .55rem;white-space:nowrap;border-bottom:1px solid var(--border);}
table.board th.col-rank,table.board td.col-rank,table.board th.col-streak,table.board td.col-streak,
table.board th.col-date,table.board td.col-date{text-align:center;}
table.board tbody td{padding:.5rem .55rem;border-bottom:1px solid var(--border);white-space:nowrap;
font-family:'JetBrains Mono',monospace;font-weight:500;font-variant-numeric:tabular-nums;}
table.board tbody td.col-player{font-family:'DM Sans',sans-serif;text-align:left;font-weight:600;}
table.board td.col-team{font-size:.76rem;}
table.board tbody tr:last-child td{border-bottom:none;}
table.board tbody tr:hover{background:var(--surface-hover);}
.col-rank{color:var(--muted);width:3rem;font-size:.78rem;}
.col-streak{font-weight:700;color:var(--accent);}
.col-date{color:var(--muted);}
.plink{color:var(--text);text-decoration:none;font-weight:600;}.plink:hover{color:var(--accent);}
.foot{text-align:center;font-size:.72rem;color:var(--muted);margin-top:1.6rem;font-family:'JetBrains Mono',monospace;line-height:1.7;}
@media(max-width:720px){
.wrap{padding:.5rem .9rem 3rem;}.nav{padding:1rem .9rem .3rem;flex-wrap:nowrap;overflow-x:auto;}
.nav a{flex:0 0 auto;}
.table-card{overflow:visible;border:none;background:transparent;border-radius:0;}
table.board,table.board tbody{display:block;width:100%;}
table.board thead{display:none;}
table.board tbody tr{display:block;position:relative;background:var(--surface);border:1px solid var(--border);
border-radius:12px;padding:.7rem 3rem .7rem .9rem;margin-bottom:.6rem;}
table.board tbody tr:hover{background:var(--surface);}
table.board tbody td{display:block;position:relative;border:none;white-space:normal;text-align:left;
padding:.14rem 0 .14rem 7rem;font-size:.86rem;line-height:1.4;min-height:1.5em;overflow-wrap:anywhere;}
table.board tbody td::before{content:attr(data-label);position:absolute;left:0;top:.18rem;width:6.5rem;
font-family:'DM Sans',sans-serif;font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);}
table.board tbody td.col-rank{position:absolute;top:.55rem;right:.85rem;padding:0;font-size:.66rem;color:var(--muted);min-height:0;}
table.board tbody td.col-rank::before{display:none;}
table.board tbody td.col-player{padding:0 0 .3rem;font-family:'DM Sans',sans-serif;font-size:1.02rem;font-weight:600;}
table.board tbody td.col-player::before{display:none;}
table.board tbody td.col-streak{font-size:1.1rem;font-weight:700;color:var(--accent);}
}
"""

GLOBAL_SEARCH_JS = r"""
<script>
(function(){
  var idx=window.PLAYER_INDEX||[];
  function esc(s){return String(s).replace(/[&<>"]/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]);});}
  function build(q){q=q.toLowerCase();var st=[],co=[];
    for(var i=0;i<idx.length;i++){var n=idx[i][0].toLowerCase(),p=n.indexOf(q);
      if(p===0)st.push(idx[i]);else if(p>0)co.push(idx[i]);if(st.length>=15)break;}
    return st.concat(co).slice(0,12);}
  function wire(inp){var ac=inp.parentNode.querySelector('.ac');if(!ac)return;var sel=-1,items=[];
    function render(){var q=inp.value.trim();if(q.length<2){ac.classList.remove('open');items=[];sel=-1;return;}
      items=build(q);if(!items.length){ac.innerHTML='<div class="ac-empty">No players found</div>';ac.classList.add('open');return;}
      ac.innerHTML=items.map(function(m,i){var fl=m[2]?' <img class="flag-img" src="https://flagcdn.com/h20/'+m[2]+'.png" srcset="https://flagcdn.com/h40/'+m[2]+'.png 2x" height="13" alt="'+esc(m[3]||'')+'" loading="lazy">':'';return '<a class="ac-item'+(i===sel?' sel':'')+'" href="'+PLAYER_PREFIX+m[1]+'.html">'+esc(m[0])+fl+'</a>';}).join('');
      ac.classList.add('open');}
    inp.addEventListener('input',function(){sel=-1;render();});
    inp.addEventListener('keydown',function(e){if(!items.length)return;
      if(e.key==='ArrowDown'){e.preventDefault();sel=(sel+1)%items.length;render();}
      else if(e.key==='ArrowUp'){e.preventDefault();sel=(sel-1+items.length)%items.length;render();}
      else if(e.key==='Enter'&&sel>=0){e.preventDefault();location.href=PLAYER_PREFIX+items[sel][1]+'.html';}
      else if(e.key==='Escape'){ac.classList.remove('open');}});
    inp.addEventListener('blur',function(){setTimeout(function(){ac.classList.remove('open');},150);});}
  document.querySelectorAll('.psearch').forEach(wire);
})();
</script>
"""

RENDER_JS = r"""
<script>
var DATA=window.STREAK_DATA,PLAYERS=window.STREAK_PLAYERS,META=window.STREAK_META,RECORDS=window.STREAK_RECORDS;
var activeStreak='pts10',activeScope='regular';
var MM=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
function eh(s){return String(s).replace(/[&<>"]/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]);});}
function fd(s){if(!s)return'';var p=s.split('-');return MM[(+p[1])-1]+' '+(+p[2])+', '+p[0];}
function metaOf(id){for(var i=0;i<META.length;i++)if(META[i].id===id)return META[i];return null;}
function flagImg(iso,c){if(!iso)return'';return ' <img class="flag-img" src="https://flagcdn.com/h20/'+iso+'.png" srcset="https://flagcdn.com/h40/'+iso+'.png 2x" height="14" alt="'+eh(c)+'" title="'+eh(c)+'" loading="lazy">';}
function render(){
  var m=metaOf(activeStreak);document.getElementById('active-label').textContent=m?m.label:activeStreak;
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('active',t.dataset.scope===activeScope);});
  document.querySelectorAll('.chip').forEach(function(c){c.classList.toggle('active',c.dataset.streak===activeStreak);});
  var rf=document.getElementById('record-flag'),rec=RECORDS[activeStreak],rows=(DATA[activeStreak]&&DATA[activeStreak][activeScope])||[];
  if(rec&&activeScope==='regular'&&rows.length){var tn=PLAYERS[rows[0][1]][0],tl=rows[0][2],ok=(tn===rec.holder);
    rf.style.display='';rf.innerHTML='<span>🏆</span><div>Official record: <b>'+eh(rec.holder)+' '+rec.num.toLocaleString()+'</b> ('+eh(rec.note)+'). Our engine: <b>'+eh(tn)+' '+tl.toLocaleString()+'</b> — '+(ok?'<span class="ok">MATCH</span>':'<span class="no">differs</span>')+'.</div>';
  }else{rf.style.display='none';}
  var tb=document.getElementById('tbody');
  if(!rows.length){tb.innerHTML='<tr><td class="col-player">No streaks of this type in '+activeScope+' play.</td></tr>';return;}
  var out=[];for(var i=0;i<rows.length;i++){var r=rows[i],pl=PLAYERS[r[1]];
    out.push('<tr><td class="col-rank" data-label="Rank">'+r[0]+'</td>'+
     '<td class="col-player" data-label="Player"><a class="plink" href="players/'+r[1]+'.html">'+eh(pl[0])+'</a>'+flagImg(pl[1],pl[2])+'</td>'+
     '<td class="col-streak" data-label="Streak">'+r[2]+'</td>'+
     '<td class="col-date" data-label="Start">'+fd(r[3])+'</td>'+
     '<td class="col-date" data-label="End">'+fd(r[4])+'</td>'+
     '<td class="col-team" data-label="Team(s)">'+eh(r[5]||'—')+'</td></tr>');}
  tb.innerHTML=out.join('');
}
document.querySelectorAll('.tab').forEach(function(t){t.addEventListener('click',function(){activeScope=t.dataset.scope;render();});});
document.querySelectorAll('.chip').forEach(function(c){c.addEventListener('click',function(){activeStreak=c.dataset.streak;render();});});
render();
</script>
"""

FEATS_RENDER_JS = r"""
<script>
var FD=window.FEAT_DATA,PLAYERS=window.STREAK_PLAYERS,LBL=window.FEAT_LABELS;
var active='td';
function eh(s){return String(s).replace(/[&<>"]/g,function(c){return({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]);});}
function flagImg(iso,c){if(!iso)return'';return ' <img class="flag-img" src="https://flagcdn.com/h20/'+iso+'.png" srcset="https://flagcdn.com/h40/'+iso+'.png 2x" height="14" alt="'+eh(c)+'" title="'+eh(c)+'" loading="lazy">';}
function render(){
  document.getElementById('active-label').textContent=LBL[active]||active;
  document.querySelectorAll('.chip').forEach(function(c){c.classList.toggle('active',c.dataset.feat===active);});
  var rows=FD[active]||[],tb=document.getElementById('tbody'),out=[];
  if(!rows.length){tb.innerHTML='<tr><td class="col-player">No data.</td></tr>';return;}
  for(var i=0;i<rows.length;i++){var r=rows[i],pl=PLAYERS[r[1]];
    out.push('<tr><td class="col-rank" data-label="Rank">'+r[0]+'</td>'+
     '<td class="col-player" data-label="Player"><a class="plink" href="players/'+r[1]+'.html">'+eh(pl[0])+'</a>'+flagImg(pl[1],pl[2])+'</td>'+
     '<td class="col-streak" data-label="Count">'+r[2]+'</td>'+
     '<td class="col-date" data-label="Span">'+r[3]+'–'+r[4]+'</td></tr>');}
  tb.innerHTML=out.join('');
}
document.querySelectorAll('.chip').forEach(function(c){c.addEventListener('click',function(){active=c.dataset.feat;render();});});
render();
</script>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    os.makedirs(PLAYERS_DIR, exist_ok=True)
    ctx = build_all()
    players = ctx["players"]

    # data files
    with open(os.path.join(BASE, "streaks-data.js"), "w", encoding="utf-8") as f:
        f.write("window.STREAK_DATA=" + json.dumps(ctx["STREAK_DATA"], separators=(",", ":")) + ";\n")
        f.write("window.STREAK_PLAYERS=" + json.dumps(players, separators=(",", ":"), ensure_ascii=False) + ";\n")
        f.write("window.STREAK_META=" + json.dumps(ctx["STREAK_META"], separators=(",", ":"), ensure_ascii=False) + ";\n")
        f.write("window.STREAK_RECORDS=" + json.dumps(ctx["STREAK_RECORDS"], separators=(",", ":"), ensure_ascii=False) + ";\n")
    with open(os.path.join(BASE, "feats-data.js"), "w", encoding="utf-8") as f:
        f.write("window.FEAT_DATA=" + json.dumps(ctx["FEAT_DATA"], separators=(",", ":")) + ";\n")
        f.write("window.STREAK_PLAYERS=" + json.dumps(players, separators=(",", ":"), ensure_ascii=False) + ";\n")
        f.write("window.FEAT_LABELS=" + json.dumps(ctx["feat_label"], separators=(",", ":"), ensure_ascii=False) + ";\n")
    index_rows = sorted(([players[s][0], s, players[s][1], players[s][2]] for s in players),
                        key=lambda r: r[0].lower())
    with open(os.path.join(BASE, "search-index.js"), "w", encoding="utf-8") as f:
        f.write("window.PLAYER_INDEX=" + json.dumps(index_rows, separators=(",", ":"), ensure_ascii=False) + ";\n")

    with open(os.path.join(BASE, "index.html"), "w", encoding="utf-8") as f:
        f.write(build_index_html(ctx["STREAK_META"]))
    with open(os.path.join(BASE, "feats.html"), "w", encoding="utf-8") as f:
        f.write(build_feats_html(ctx["FEATS"]))

    # player pages
    print(f"Writing {len(ctx['page_pids'])} player pages…", flush=True)
    n = 0
    for pid in ctx["page_pids"]:
        slug = ctx["slug_by_pid"][pid]
        with open(os.path.join(PLAYERS_DIR, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(build_player_page(pid, ctx))
        n += 1
    print(f"\nDone. index.html + feats.html + {n} player pages + data files "
          f"({len(players)} players in search index).")


if __name__ == "__main__":
    main()

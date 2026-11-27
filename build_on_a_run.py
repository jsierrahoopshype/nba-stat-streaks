"""
build_on_a_run.py — the daily "On a run" page + the per-player "against a number" chart.

Runs inside nightly_build.py after the site pages are written (same data, same run):

  reads   NBA_STREAKS_DATA/PlayerStatistics.csv   (prepare_data.py output: Kaggle history
                                                   + this season's cdn.nba.com box lines)
  writes  on-a-run.html                            the page (one URL, filters are in-page)
          data/on-a-run.json                       the same cards as public JSON (README)
          data/player-meta-cache.json              position + headshot file per player
          players/<slug>.html                      adds the "Against a number" block to the
                                                   pages of players who played this season
                                                   or last season (other pages untouched)

Qualifying rule (per stat, ladder below, HIGHEST number that passes):
  * his last 10 games this season (RS + PO) all have 15+ minutes,
  * he cleared the number in 8+ of those 10,
  * that L10 rate is 30+ percentage points above his rate at the same number in his
    baseline: earlier games this season (before the last 10) if there are 10+, else
    last season's games; baseline games count only with 15+ minutes too (like for like),
    and the baseline needs 10+ games or the stat is skipped,
  * the number is at or above his average for that stat in the same baseline games, i.e.
    before this 10-game stretch (no trivial runs: a 25-ppg scorer clearing 10 or 20 can
    never show up).
Ranked by the gap (L10 rate minus baseline rate), then the current straight streak.

Metadata, for the players on the cards, cached in data/player-meta-cache.json: positions
from NBA.com rosters, and whether the HoopsMatic headshot set (nba-headshots face-fill, as
served on hoopsmatic.com/img/, the one every HoopsMatic section uses) has a file for him.
Checked at build time only: visitors' browsers load just that hoopsmatic.com file, and no
image at all when there is none. A failed fetch never fails the build.

  python build_on_a_run.py                # normal (nightly_build runs this)
  python build_on_a_run.py --no-network   # use the cache only, fetch nothing
"""
import os
import re
import sys
import json
import time
import argparse
import datetime
import urllib.error
import urllib.request
from html import escape as _esc

import pandas as pd

import build_streaks as E
import build_site as BS
from franchises import franchise_of, team_slug

BASE = os.path.dirname(os.path.abspath(__file__))
OUT_HTML = os.path.join(BASE, "on-a-run.html")
OUT_JSON = os.path.join(BASE, "data", "on-a-run.json")
META_CACHE = os.path.join(BASE, "data", "player-meta-cache.json")
PLAYERS_DIR = os.path.join(BASE, "players")
# A file URL like active-streaks.html: the hoopsmatic.com worker serves *.html paths in
# this section (folder URLs such as on-a-run/ return 404 there).
PAGE_URL = BS.SITE_URL + "on-a-run.html"
JSON_URL = BS.SITE_URL + "data/on-a-run.json"

# Stat order is fixed everywhere: 3-pointers last.
STATS = [
    # key, engine column, singular, plural, short label
    ("PTS", "points", "point", "points"),
    ("REB", "reboundsTotal", "rebound", "rebounds"),
    ("AST", "assists", "assist", "assists"),
    ("STL", "steals", "steal", "steals"),
    ("BLK", "blocks", "block", "blocks"),
    ("3PM", "threePointersMade", "3-pointer", "3-pointers"),
]
STAT_COL = {k: c for k, c, _, _ in STATS}
STAT_PL = {k: p for k, _, _, p in STATS}
LADDER = {
    "PTS": [10, 15, 20, 25, 30, 35, 40],
    "REB": [5, 8, 10, 12, 15],
    "AST": [5, 8, 10, 12],
    "STL": [2, 3],
    "BLK": [2, 3, 4],
    "3PM": [2, 3, 4, 5],
}
WINDOW, NEED_HITS, MIN_GAP, MIN_MINUTES, MIN_BASE_GAMES, MAX_CARDS = 10, 8, 0.30, 15, 10, 40
CHART_GAMES = 20

# HoopsMatic headshots (nba-headshots repo, served on hoopsmatic.com): face-fill/<id>-<name slug>.webp
HEADSHOT = "https://hoopsmatic.com/img/nba-headshots/players/headshots/face-fill/{stem}.webp"
NBA_PLAYER = "https://www.nba.com/player/{pid}"
UA = {"User-Agent": "Mozilla/5.0 (compatible; hoopsmatic-streaks/1.0)"}
POS_SHORT = {"Guard": "G", "Forward": "F", "Center": "C", "Guard-Forward": "G-F",
             "Forward-Guard": "F-G", "Forward-Center": "F-C", "Center-Forward": "C-F"}
POS_GROUPS = [("G", "Guards"), ("F", "Forwards"), ("C", "Centers")]
MONTHS = BS.MONTHS


def esc(s):
    return _esc(str(s), quote=True)


def season_of(d):
    return d.year if d.month >= 7 else d.year - 1


def fmt_day(d):
    return f"{MONTHS[d.month - 1]} {d.day}"


def fmt_full(d):
    return f"{MONTHS[d.month - 1]} {d.day}, {d.year}"


def num(v):
    """Box-score value as int when whole (they always are), for display and JSON."""
    f = float(v)
    return int(f) if f.is_integer() else round(f, 1)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
def load_recent():
    """Engine appearances for this season and last season, plus opponents."""
    df = E.load_appearances()
    through = df["gameDate"].max()
    cur = season_of(through)
    df["season"] = df["gameDate"].dt.year.where(df["gameDate"].dt.month >= 7, df["gameDate"].dt.year - 1)
    df = df[df["season"] >= cur - 1].copy()
    df["franchise"] = [franchise_of(t, s) for t, s in zip(df["team_disp"], df["season"])]
    # opponent = the other team in the same game id
    teams = df.drop_duplicates(["gameId", "playerteamId"])[["gameId", "playerteamId", "franchise"]]
    pairs = teams.merge(teams, on="gameId", suffixes=("", "_opp"))
    pairs = pairs[pairs["playerteamId"] != pairs["playerteamId_opp"]]
    pairs = pairs.drop_duplicates(["gameId", "playerteamId"])
    df = df.merge(pairs[["gameId", "playerteamId", "franchise_opp"]], on=["gameId", "playerteamId"], how="left")
    df["franchise_opp"] = df["franchise_opp"].fillna("")
    df["mins"] = pd.to_numeric(df["numMinutes"], errors="coerce").fillna(0.0)
    df = df.sort_values(["personId", "gameDate", "gameId"]).reset_index(drop=True)
    return df, through.date(), cur


def page_slugs():
    """pid -> slug of the player's existing page (players/<slug>-<pid>.html)."""
    out = {}
    for fn in os.listdir(PLAYERS_DIR):
        m = re.match(r"^(.*-(\d+))\.html$", fn)
        if m:
            out[int(m.group(2))] = m.group(1)
    return out


TEAM_PAGES = None


def team_href(franchise, prefix=""):
    global TEAM_PAGES
    if TEAM_PAGES is None:
        TEAM_PAGES = set(os.listdir(os.path.join(BASE, "teams")))
    s = team_slug(franchise)
    return f"{prefix}teams/{s}.html" if f"{s}.html" in TEAM_PAGES else None


def short_team(franchise):
    """'Detroit Pistons' -> 'Pistons', 'Portland Trail Blazers' -> 'Trail Blazers'."""
    if franchise.startswith("Portland "):
        return "Trail Blazers"
    return franchise.rsplit(" ", 1)[-1] if franchise else ""


# --------------------------------------------------------------------------- #
# Qualifying
# --------------------------------------------------------------------------- #
def rate(vals, n):
    return (sum(1 for v in vals if v >= n) / len(vals)) if len(vals) else None


def trailing_streak(vals, n):
    k = 0
    for v in reversed(vals):
        if v >= n:
            k += 1
        else:
            break
    return k


def longest_streak(vals, n):
    best = k = 0
    for v in vals:
        k = k + 1 if v >= n else 0
        best = max(best, k)
    return best


def find_cards(df, cur):
    cards = []
    for pid, g in df.groupby("personId", sort=False):
        season_g = g[g["season"] == cur]
        if len(season_g) < WINDOW:
            continue
        last = season_g.tail(WINDOW)
        if (last["mins"] < MIN_MINUTES).any():
            continue
        earlier = season_g.iloc[:-WINDOW]
        earlier = earlier[earlier["mins"] >= MIN_MINUTES]
        if len(earlier) >= MIN_BASE_GAMES:
            base, base_src = earlier, "earlier"
        else:
            prev = g[(g["season"] == cur - 1) & (g["mins"] >= MIN_MINUTES)]
            base, base_src = prev, "last"
        if len(base) < MIN_BASE_GAMES:
            continue
        for key, col, _, _ in STATS:
            vals10 = [float(v) for v in last[col]]
            svals = [float(v) for v in season_g[col]]
            avg = sum(svals) / len(svals)
            bvals = [float(v) for v in base[col]]
            pre_avg = sum(bvals) / len(bvals)                # his normal: the baseline games
            pick = None
            for n in sorted(LADDER[key], reverse=True):      # highest number first
                if n < pre_avg:
                    break                                    # below his pre-stretch average: trivial
                hits = sum(1 for v in vals10 if v >= n)
                if hits < NEED_HITS:
                    continue
                br = rate(bvals, n)
                if hits / WINDOW - br >= MIN_GAP - 1e-9:
                    pick = (n, hits, br)
                    break
            if not pick:
                continue
            n, hits, br = pick
            cards.append({
                "pid": int(pid), "g": season_g, "last": last, "base": base, "base_src": base_src,
                "stat": key, "n": n, "hits10": hits, "base_rate": br, "gap": hits / WINDOW - br,
                "streak": trailing_streak(svals, n), "season_long": longest_streak(svals, n),
                "avg": avg, "pre_avg": pre_avg, "svals": svals, "vals10": vals10, "bvals": bvals,
            })
    cards.sort(key=lambda c: (-round(c["gap"], 6), -c["streak"], -c["hits10"], c["pid"], c["stat"]))
    return cards


# --------------------------------------------------------------------------- #
# Player metadata (NBA.com position, HoopsMatic headshot file) — cached, fail-soft
# --------------------------------------------------------------------------- #
def _get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def headshot_stems(pid, name):
    """Candidate file stems: <id>-<name slug> (apostrophes dropped, as the set names them),
    then the same with apostrophes as hyphens."""
    out = []
    for nm in (str(name).replace("'", ""), str(name).replace("'", " ")):
        stem = f"{pid}-" + BS.slugify(nm, pid)[: -len(f"-{pid}")]
        if stem not in out:
            out.append(stem)
    return out


def refresh_meta(pids, cache, today, names=None, network=True):
    """Fill cache[pid] = {"pos": "G-F"|"", "hs": "<file stem>"|"" (none), "hs_checked": date}."""
    for ent in cache.values():                      # drop the old cdn.nba.com keys
        ent.pop("headshot", None)
        ent.pop("checked", None)
    if not network:
        return cache
    fetched = 0
    for pid in pids:
        k = str(pid)
        ent = cache.get(k, {})
        # positions: fetched once per player (they don't change); headshots: re-checked
        # while missing, since new players get one a few weeks in.
        if not ent.get("pos"):
            try:
                html = _get(NBA_PLAYER.format(pid=pid)).decode("utf-8", "replace")
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
                pp = json.loads(m.group(1))["props"]["pageProps"]["player"]
                pos = (pp.get("info") or {}).get("POSITION") or ""
                ent["pos"] = POS_SHORT.get(pos, pos)
                # the page lists his whole roster: cache the teammates too (fewer fetches)
                for r in pp.get("roster") or []:
                    tk = str(r.get("PLAYER_ID"))
                    if r.get("POSITION") and not cache.get(tk, {}).get("pos"):
                        cache.setdefault(tk, {})["pos"] = r["POSITION"]
                fetched += 1
                time.sleep(0.4)
            except Exception as e:
                print(f"  position for {pid}: fetch failed ({type(e).__name__})")
        # headshot: found once = kept; missing = re-checked (new files get added)
        if not ent.get("hs") and names and pid in names:
            found, failed = "", False
            for stem in headshot_stems(pid, names[pid]):
                try:
                    req = urllib.request.Request(HEADSHOT.format(stem=stem), headers=UA, method="HEAD")
                    with urllib.request.urlopen(req, timeout=20) as r:
                        if r.status == 200 and "image" in (r.headers.get("Content-Type") or ""):
                            found = stem
                            break
                except urllib.error.HTTPError as e:
                    if e.code != 404:
                        failed = True
                except Exception:
                    failed = True
            if found or not failed:                 # a network failure leaves the cache as it was
                if ent.get("hs") != found or "hs_checked" not in ent:
                    ent["hs_checked"] = today.isoformat()
                ent["hs"] = found
        cache[k] = {**cache.get(k, {}), **ent}
    print(f"  player metadata: {fetched} NBA.com page(s) fetched")
    return cache


def pos_groups(pos):
    return [g for g, _ in POS_GROUPS if g in (pos or "").split("-")]


# --------------------------------------------------------------------------- #
# Notes: several templates and phrasings; neighbours never share one
# --------------------------------------------------------------------------- #
def note_facts(c, name):
    last = name.split(" ", 1)[-1] if " " in name else name
    if last.split(" ")[-1] in ("Jr.", "Sr.", "II", "III", "IV"):
        last = last.rsplit(" ", 1)[0]
    key, n = c["stat"], c["n"]
    pl = STAT_PL[key]
    vals = c["vals10"]
    best_i = max(range(len(vals)), key=lambda i: (vals[i], i))
    lastg = c["last"]
    best_row = lastg.iloc[best_i]
    misses = [i for i, v in enumerate(vals) if v < n]
    when = "earlier this season" if c["base_src"] == "earlier" else "last season"
    avgb = sum(c["bvals"]) / len(c["bvals"])
    avg10 = sum(vals) / len(vals)
    return {
        "last": last, "n": n, "pl": pl, "k": c["hits10"], "base": round(c["base_rate"] * 100),
        "pct10": c["hits10"] * 10, "gap": round(c["gap"] * 100), "when": when,
        "streak": c["streak"], "season_long": c["season_long"],
        "avg10": f"{avg10:.1f}", "avgb": f"{avgb:.1f}", "diff": f"{avg10 - avgb:.1f}",
        "best": num(vals[best_i]), "best_opp": short_team(best_row["franchise_opp"]),
        "best_date": fmt_day(best_row["gameDate"]),
        "miss_n": len(misses),
        "miss_val": num(vals[misses[-1]]) if misses else None,
        "miss_date": fmt_day(lastg.iloc[misses[-1]]["gameDate"]) if misses else None,
        "start": fmt_day(lastg.iloc[0]["gameDate"]),
        "l5": sum(1 for v in vals[-5:] if v >= n),
        "avgdiff_pos": avg10 - avgb >= 0.5,
    }


# (id, eligible(f), text) — plain sentences, no em-dashes, no time-of-day words.
TEMPLATES = [
    ("a", lambda f: True,
     "{last} has reached {n} {pl} in {k} of his last 10 games. {When_cap}, that happened in {base}% of his games."),
    ("b", lambda f: True,
     "{k} of the last 10 at {n} or more {pl}, up from a {base}% clip {when}."),
    ("c", lambda f: True,
     "Before this stretch he got to {n} {pl} in {base}% of games {when}. Now it is {k} out of 10."),
    ("d", lambda f: f["avgdiff_pos"],
     "He is averaging {avg10} {pl} across the last 10, compared with {avgb} {when}."),
    ("e", lambda f: f["streak"] >= 3 and f["streak"] == f["season_long"],
     "This is his longest run of the season at {n}+ {pl}: {streak} games and counting."),
    ("f", lambda f: f["best_opp"] != "",
     "The high point so far: {best} {pl} against the {best_opp} on {best_date}."),
    ("g", lambda f: f["miss_n"] == 1,
     "One miss in the last 10, and even that was {miss_val} {pl} on {miss_date}."),
    ("h", lambda f: f["miss_n"] == 0,
     "A perfect 10 for 10 at {n} or more {pl}."),
    ("i", lambda f: True,
     "That is a jump of {gap} points in hit rate at {n}+ {pl} compared with {when}."),
    ("j", lambda f: f["l5"] == 5,
     "All five of his most recent games got there, and {k} of the last 10 did."),
    ("k", lambda f: f["base"] <= 30,
     "{n}+ {pl} used to be the exception for {last} ({base}% {when}). It has turned into the norm."),
    ("l", lambda f: True,
     "Since {start} he has {k} games with {n} or more {pl} in 10 tries."),
    ("m", lambda f: f["avgdiff_pos"],
     "{last} is putting up {diff} more {pl} a game than {when} over this 10-game stretch."),
    ("n", lambda f: f["streak"] >= 4,
     "{streak} straight games at {n}+ {pl} for {last}, with {k} hits in the last 10."),
    ("o", lambda f: f["miss_n"] >= 2 and f["miss_val"] is not None,
     "Only {miss_n} games under {n} in the last 10; the latest was {miss_val} {pl} on {miss_date}."),
]


def render_note(tid, f):
    text = dict((t[0], t[2]) for t in TEMPLATES)[tid]
    w = f["when"]
    vals = {**f, "When_cap": w[0].upper() + w[1:]}
    out = text.format(**vals)
    return out[0].upper() + out[1:]


def assign_notes(cards, names, metas):
    """Greedy: each card takes the first eligible template (rotating start) that differs
    from the template of the previous card overall and of the previous card in each
    filter group (stat, team, position) it belongs to, so neighbours read differently
    in the default view and in filtered views."""
    prev = {}
    start = 0
    for c in cards:
        f = note_facts(c, names[c["pid"]])
        groups = ["all", "stat:" + c["stat"], "team:" + c["team"]] + \
                 ["pos:" + p for p in pos_groups(metas.get(str(c["pid"]), {}).get("pos"))]
        avoid = {prev[gk] for gk in groups if gk in prev}
        order = TEMPLATES[start:] + TEMPLATES[:start]
        pick = next((t for t in order if t[1](f) and t[0] not in avoid), None) \
            or next(t for t in order if t[1](f))
        c["note_id"] = pick[0]
        c["note"] = render_note(pick[0], f)
        for gk in groups:
            prev[gk] = pick[0]
        start = (TEMPLATES.index(pick) + 1) % len(TEMPLATES)


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #
def pct_str(h, g):
    return f"{round(100 * h / g)}%" if g else "—"


def hit_rates(svals, n):
    out = {}
    for lab, w in (("L5", 5), ("L10", 10), ("L15", 15)):
        v = svals[-w:]
        out[lab] = {"hits": sum(1 for x in v if x >= n), "games": len(v)}
    out["Season"] = {"hits": sum(1 for x in svals if x >= n), "games": len(svals)}
    for v in out.values():
        v["pct"] = round(100 * v["hits"] / v["games"]) if v["games"] else None
    return out


def bars_html(c, uid):
    n, vals, last = c["n"], c["vals10"], c["last"]
    top = max(max(vals), n * 1.6, n + 2)
    line = 100 * n / top
    bars, dets = [], []
    pl = STAT_PL[c["stat"]]
    for i, (v, (_, r)) in enumerate(zip(vals, last.iterrows())):
        hit = v >= n
        h = max(6.0, 100 * v / top)   # a zero still shows as a red stub
        opp = r["franchise_opp"]
        href = team_href(opp) if opp else None
        opp_html = (f'<a class="plink" href="{esc(href)}">{esc(opp)}</a>' if href else esc(opp or "?"))
        bars.append(f'<span class="bar {"hit" if hit else "miss"}" style="height:{h:.1f}%" '
                    f'data-i="{i}"></span>')
        dets.append(f'<span class="gdet" data-i="{i}" hidden>{esc(fmt_full(r["gameDate"]))} vs. '
                    f'{opp_html}: <b>{num(v)} {esc(pl)}</b> ({"cleared" if hit else "missed"} {n})</span>')
    return (f'<div class="bars" id="{uid}" tabindex="0" role="group" '
            f'aria-label="Last 10 games against {n}. Tap or use arrow keys for each game.">'
            f'<span class="nline" style="bottom:{line:.1f}%"></span>{"".join(bars)}</div>'
            f'<p class="gdets" aria-live="polite"><span class="ghint">Tap a bar for the game.</span>'
            f'{"".join(dets)}</p>')


def card_html(i, c, name, slug, meta):
    pid = c["pid"]
    pos = meta.get("pos") or ""
    hs = meta.get("hs")
    img = ""
    if hs:   # a HoopsMatic headshot file exists; none (or not checked yet) = no image at all
        lazy = '' if i < 3 else ' loading="lazy"'
        img = (f'<img class="hs" src="{HEADSHOT.format(stem=esc(hs))}" width="56" height="56" alt=""{lazy} '
               f'decoding="async">')
    name_html = (f'<a class="plink" href="players/{esc(slug)}.html">{esc(name)}</a>' if slug else esc(name))
    thref = team_href(c["team"])
    team_html = (f'<a class="tmname" href="{esc(thref)}">{esc(c["team"])}</a>' if thref else esc(c["team"]))
    pos_html = f'<span class="pos">{esc(pos)}</span>' if pos else ""
    hr = c["rates"]
    rates = "".join(f'<div class="rt"><span class="rl">{lab}</span><span class="rv">{pct_str(v["hits"], v["games"])}'
                    f'</span><span class="rg">{v["hits"]}/{v["games"]}</span></div>'
                    for lab, v in hr.items())
    stl = f'{c["n"]}+ {STAT_PL[c["stat"]]}'
    streak = c["streak"]
    streak_txt = f'{streak} straight' if streak else "none (missed his last game)"
    attrs = (f'data-stat="{c["stat"]}" data-team="{esc(team_slug(c["team"]))}" '
             f'data-pos="{" ".join(pos_groups(pos))}"')
    return (
        f'<article class="ocard" {attrs}>'
        f'<div class="ohd">{img}<div class="oid"><h2 class="onm">{name_html}</h2>'
        f'<p class="otm">{team_html}{pos_html}</p></div></div>'
        f'<p class="ostat"><span class="olab">{esc(stl)}</span>'
        f'<span class="ostreak">Streak: {esc(streak_txt)}</span></p>'
        f'<div class="rates">{rates}</div>'
        f'{bars_html(c, f"b{i}")}'
        f'<p class="onote">{esc(c["note"])}</p>'
        f'</article>')


PAGE_CSS = r"""
<style>
.or-hd{margin:.2rem 0 .2rem;}
.or-upd{color:var(--muted);font-size:.82rem;margin:.15rem 0 .5rem;font-family:'JetBrains Mono',monospace;}
.or-filters{display:flex;flex-direction:column;gap:.45rem;margin:.6rem 0 1rem;}
.or-row{display:flex;flex-wrap:wrap;gap:.35rem;align-items:center;}
.or-row .flabel{font-family:'JetBrains Mono',monospace;font-size:.62rem;font-weight:700;text-transform:uppercase;
letter-spacing:.06em;color:var(--muted);min-width:4.6rem;}
.or-btn{min-height:44px;min-width:44px;font-family:'JetBrains Mono',monospace;font-size:.76rem;font-weight:600;
padding:0 .8rem;cursor:pointer;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);}
.or-btn:hover{border-color:var(--accent);color:var(--accent);}
.or-btn[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff;}
.or-btn:focus-visible,.bars:focus-visible,.or-team summary:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.or-team{border:1px solid var(--border);border-radius:8px;background:var(--surface);}
.or-team summary{min-height:44px;display:flex;align-items:center;padding:0 .8rem;cursor:pointer;font-size:.84rem;font-weight:600;}
.or-team summary .flabel{min-width:4.6rem;}
.or-chev{margin-left:auto;color:var(--muted);transition:transform .15s;}
.or-team[open] .or-chev{transform:rotate(180deg);}
.or-team .or-row{padding:.2rem .5rem .6rem;}
.or-count{color:var(--muted);font-size:.8rem;margin:0 0 .6rem;}
.ogrid{display:grid;grid-template-columns:1fr;gap:.8rem;}
@media(min-width:760px){.ogrid{grid-template-columns:repeat(2,minmax(0,1fr));}}
@media(min-width:1100px){.ogrid{grid-template-columns:repeat(3,minmax(0,1fr));}}
.ocard{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.8rem .85rem .75rem;min-width:0;}
.ocard[hidden]{display:none;}
.ohd{display:flex;align-items:center;gap:.65rem;min-width:0;}
.hs{width:56px;height:56px;object-fit:cover;background:#f0f0f2;border-radius:50%;flex:0 0 auto;}
.oid{min-width:0;}
.onm{font-size:1.02rem;font-weight:700;margin:0;letter-spacing:-.01em;line-height:1.25;overflow-wrap:anywhere;}
.otm{margin:0;font-size:.8rem;color:var(--muted);display:flex;gap:.45rem;align-items:center;flex-wrap:wrap;}
.onm a,.tmname,.gdet a{display:inline-flex;align-items:center;min-height:44px;}
.tmname{color:var(--muted);}.tmname:hover{color:var(--accent);}
.pos{font-family:'JetBrains Mono',monospace;font-size:.66rem;font-weight:700;border:1px solid var(--border);border-radius:6px;padding:0 .3rem;}
.ostat{display:flex;justify-content:space-between;align-items:baseline;gap:.5rem;flex-wrap:wrap;margin:.6rem 0 .45rem;}
.olab{font-size:1.15rem;font-weight:700;color:var(--accent);letter-spacing:-.01em;}
.ostreak{font-family:'JetBrains Mono',monospace;font-size:.74rem;font-weight:600;color:var(--text);}
.rates{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.3rem;margin-bottom:.6rem;}
.rt{background:var(--bg);border-radius:8px;padding:.3rem .2rem;text-align:center;display:flex;flex-direction:column;}
.rl{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);}
.rv{font-family:'JetBrains Mono',monospace;font-size:.92rem;font-weight:700;font-variant-numeric:tabular-nums;}
.rg{font-family:'JetBrains Mono',monospace;font-size:.64rem;color:var(--muted);}
.bars{position:relative;display:flex;align-items:flex-end;gap:3px;height:88px;padding:0;border-bottom:1px solid var(--border);
cursor:pointer;touch-action:manipulation;-webkit-tap-highlight-color:transparent;border-radius:4px 4px 0 0;}
.bar{flex:1 1 0;min-width:0;border-radius:3px 3px 0 0;opacity:.85;}
.bar.hit{background:#16a34a;}.bar.miss{background:#dc2626;}
.bar.sel{opacity:1;box-shadow:0 0 0 2px var(--text);}
.nline{position:absolute;left:0;right:0;border-top:2px dashed var(--text);opacity:.55;pointer-events:none;z-index:1;}
.gdets{min-height:1.5em;margin:.4rem 0 0;font-size:.8rem;}
.ghint{color:var(--muted);}
.onote{margin:.5rem 0 0;font-size:.86rem;line-height:1.45;}
.or-empty{color:var(--muted);}
.or-how{color:var(--muted);font-size:.8rem;max-width:60rem;margin:1.4rem 0 0;}
</style>
"""

PAGE_JS = r"""
<script>
(function(){
  var st={stat:'all',pos:'all',team:'all'};
  var cards=[].slice.call(document.querySelectorAll('.ocard'));
  var count=document.getElementById('or-count'),empty=document.getElementById('or-empty');
  function apply(){
    var n=0;
    cards.forEach(function(c){
      var ok=(st.stat==='all'||c.dataset.stat===st.stat)&&(st.team==='all'||c.dataset.team===st.team)&&
             (st.pos==='all'||(' '+c.dataset.pos+' ').indexOf(' '+st.pos+' ')>=0);
      c.hidden=!ok;if(ok)n++;});
    count.textContent=n+(n===1?' player run':' player runs')+' shown';
    empty.hidden=n>0;
  }
  document.querySelectorAll('.or-btn').forEach(function(b){
    b.addEventListener('click',function(){
      var k=b.dataset.k;st[k]=b.dataset.v;
      document.querySelectorAll('.or-btn[data-k="'+k+'"]').forEach(function(o){o.setAttribute('aria-pressed',o===b?'true':'false');});
      if(k==='team'){var s=document.getElementById('or-team-cur');if(s)s.textContent=b.textContent;}
      apply();});});
  // headshot missing -> no picture at all
  document.querySelectorAll('img.hs').forEach(function(im){
    function drop(){im.remove();}
    if(im.complete&&im.naturalWidth===0)drop();else im.addEventListener('error',drop);});
  // bars: tap (anywhere in the strip; nearest bar by x) or arrow keys -> inline game line
  document.querySelectorAll('.bars').forEach(function(strip){
    var bars=strip.querySelectorAll('.bar'),dets=strip.nextElementSibling.querySelectorAll('.gdet');
    var hint=strip.nextElementSibling.querySelector('.ghint'),cur=-1;
    function show(i){cur=i;bars.forEach(function(b,j){b.classList.toggle('sel',j===i);});
      dets.forEach(function(d,j){d.hidden=j!==i;});if(hint)hint.hidden=i>=0;}
    strip.addEventListener('click',function(e){var r=strip.getBoundingClientRect();
      var i=Math.floor((e.clientX-r.left)/r.width*bars.length);i=Math.max(0,Math.min(bars.length-1,i));
      show(i===cur?-1:i);});
    strip.addEventListener('keydown',function(e){
      if(e.key==='ArrowRight'){e.preventDefault();show(Math.min(bars.length-1,cur+1));}
      else if(e.key==='ArrowLeft'){e.preventDefault();show(cur<=0?0:cur-1);}
      else if(e.key==='Escape'){show(-1);}});
  });
  apply();
})();
</script>
"""


def build_page(cards, names, slugs, metas, through):
    title = "NBA players on a hot streak right now: points, rebounds, assists | HoopsMatic"
    desc = ("NBA players on a hot streak right now: who is clearing 20 points, 10 rebounds, 8 assists or "
            "3 made threes far more often than usual over his last 10 games. Updated every day.")
    hd = BS.head(title, desc, path="on-a-run.html")
    hd = hd.replace(f'<link rel="canonical" href="{PAGE_URL}">\n',
                    f'<link rel="canonical" href="{PAGE_URL}">\n'
                    f'<meta property="og:url" content="{PAGE_URL}">\n'
                    '<meta property="og:type" content="website">\n', 1)
    assert 'og:url' in hd, "canonical line not found in BS.head output"

    def btns(k, items):
        return "".join(f'<button type="button" class="or-btn" data-k="{k}" data-v="{esc(v)}" '
                       f'aria-pressed="{"true" if v == "all" else "false"}">{esc(lab)}</button>'
                       for v, lab in items)
    stats_present = [k for k, *_ in STATS if any(c["stat"] == k for c in cards)]
    stat_row = btns("stat", [("all", "All")] + [(k, k) for k in stats_present])
    pos_present = [(g, lab) for g, lab in POS_GROUPS
                   if any(g in pos_groups(metas.get(str(c["pid"]), {}).get("pos")) for c in cards)]
    pos_row = btns("pos", [("all", "All")] + pos_present)
    teams = sorted({c["team"] for c in cards})
    team_row = btns("team", [("all", "All teams")] + [(team_slug(t), t) for t in teams])

    filters = (
        '<div class="or-filters" aria-label="Filters">'
        f'<div class="or-row" role="group" aria-label="Stat"><span class="flabel">Stat</span>{stat_row}</div>'
        + (f'<div class="or-row" role="group" aria-label="Position"><span class="flabel">Position</span>{pos_row}</div>'
           if pos_present else "")
        + '<details class="or-team"><summary><span class="flabel">Team</span>'
          '<span id="or-team-cur">All teams</span><span class="or-chev" aria-hidden="true">▾</span></summary>'
          f'<div class="or-row" role="group" aria-label="Team">{team_row}</div></details>'
        '</div>')
    if cards:
        grid = "".join(card_html(i, c, names[c["pid"]], slugs.get(c["pid"]), metas.get(str(c["pid"]), {}))
                       for i, c in enumerate(cards))
    else:
        grid = ""
    body = (
        '<div class="wrap">\n'
        '<a class="backtop" href="index.html">← All streak leaderboards</a>\n'
        '<header class="or-hd"><h1>NBA players <span class="accent">on a run</span></h1>'
        f'<p class="or-upd">Updated {esc(fmt_full(through))} (games through that night)</p>'
        '<p class="subtitle">Players clearing a stat number in at least 8 of their last 10 games, '
        'far more often than they usually do. Each card shows the highest number he is clearing.</p></header>\n'
        f'{filters}\n'
        '<p class="or-count" id="or-count" aria-live="polite"></p>\n'
        f'<div class="ogrid">{grid}</div>\n'
        f'<p class="or-empty" id="or-empty"{" hidden" if cards else ""}>No player runs match these filters.</p>\n'
        '<p class="or-how">How a run qualifies: 15+ minutes in each of his last 10 games (regular season '
        'and playoffs, this season), the number cleared in 8 or more of them, and a hit rate at least 30 points '
        'above his rate at that number in his earlier games this season (or last season when he has fewer '
        'than 10 earlier games). The number is never below his average before this 10-game stretch (same baseline games). Ranked by how far the run '
        f'sits above his normal, then by the current streak. The same cards are published as '
        f'<a href="data/on-a-run.json">JSON</a>.</p>\n'
        '</div>\n'
    )
    return hd + PAGE_CSS + BS.nav("onrun") + body + PAGE_JS + "</body></html>\n"


def card_json(c, name, slug, meta):
    pl = STAT_PL[c["stat"]]
    games = []
    for v, (_, r) in zip(c["vals10"], c["last"].iterrows()):
        games.append({"date": r["gameDate"].date().isoformat(), "game_id": str(r["gameId"]),
                      "opponent": r["franchise_opp"], "value": num(v), "minutes": num(r["mins"]),
                      "cleared": v >= c["n"]})
    return {
        "player_id": c["pid"], "name": name,
        "player_url": f"{BS.SITE_URL}players/{slug}.html" if slug else None,
        "team": c["team"], "team_url": (BS.SITE_URL + team_href(c["team"], prefix="")) if team_href(c["team"]) else None,
        "position": meta.get("pos") or None,
        "headshot": HEADSHOT.format(stem=meta["hs"]) if meta.get("hs") else None,
        "stat": c["stat"], "stat_label": pl, "number": c["n"], "label": f'{c["n"]}+ {pl}',
        "last10": games,
        "hit_rates": {k.lower(): v for k, v in c["rates"].items()},
        "baseline": {"source": "earlier_this_season" if c["base_src"] == "earlier" else "last_season",
                     "games": len(c["bvals"]), "hits": sum(1 for x in c["bvals"] if x >= c["n"]),
                     "pct": round(100 * c["base_rate"])},
        "gap_points": round(100 * c["gap"]),
        "streak": c["streak"],
        "season_average": round(c["avg"], 1),
        "pre_stretch_average": round(c["pre_avg"], 1),   # baseline games: the guard
        "note": c["note"],
    }


# --------------------------------------------------------------------------- #
# Player pages: "Against a number" (last 20 games)
# --------------------------------------------------------------------------- #
CH_START, CH_END = "<!--hm-anum-->", "<!--/hm-anum-->"
CH_ANCHOR = '<div class="seg" id="player-seg">'

CHART_CSS = r"""<style>
.anum{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:.85rem .9rem .8rem;margin:1rem 0 1.2rem;}
.anum h2{margin:0 0 .5rem;}
.anum-row{display:flex;flex-wrap:wrap;gap:.35rem;align-items:center;margin-bottom:.5rem;}
.anum-b{min-height:44px;min-width:44px;font-family:'JetBrains Mono',monospace;font-size:.76rem;font-weight:600;padding:0 .75rem;
cursor:pointer;border:1px solid var(--border);border-radius:8px;background:var(--surface);color:var(--text);}
.anum-b:hover{border-color:var(--accent);color:var(--accent);}
.anum-b[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:#fff;}
.anum-b:focus-visible{outline:2px solid var(--accent);outline-offset:2px;}
.anum-step{display:inline-flex;align-items:center;gap:.35rem;}
.anum-val{min-width:4.5rem;text-align:center;font-family:'JetBrains Mono',monospace;font-weight:700;font-size:.95rem;}
.anum-chart{position:relative;display:flex;align-items:flex-end;gap:2px;height:120px;border-bottom:1px solid var(--border);margin:.3rem 0 .2rem;}
.anum-bar{flex:1 1 0;min-width:0;border-radius:2px 2px 0 0;}
.anum-bar.hit{background:#16a34a;}.anum-bar.miss{background:#dc2626;}
.anum-line{position:absolute;left:0;right:0;border-top:2px dashed var(--text);opacity:.55;pointer-events:none;}
.anum-axis{display:flex;justify-content:space-between;font-size:.66rem;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.anum-rates{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.3rem;margin-top:.6rem;}
.anum-rt{background:var(--bg);border-radius:8px;padding:.3rem .2rem;text-align:center;display:flex;flex-direction:column;}
.anum-rl{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);}
.anum-rv{font-family:'JetBrains Mono',monospace;font-size:.92rem;font-weight:700;}
.anum-rg{font-family:'JetBrains Mono',monospace;font-size:.64rem;color:var(--muted);}
.anum-sum{font-size:.86rem;margin:.5rem 0 0;}
.anum-note{font-size:.76rem;color:var(--muted);margin:.5rem 0 0;}
</style>"""

CHART_JS = r"""<script>
(function(){
  var root=document.getElementById('anum');if(!root)return;
  var D=JSON.parse(document.getElementById('anum-data').textContent);
  var ORDER=['PTS','REB','AST','STL','BLK','3PM'],LAD=D.ladder,PL=D.pl;
  var stat='PTS',n=0;
  function el(t,c,txt){var e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e;}
  function def(k){var s=D.season[k],v=s.length?s:D.last[k];if(!v.length)return LAD[k][0];
    var a=0;v.forEach(function(x){a+=x;});a/=v.length;var best=LAD[k][0];
    LAD[k].forEach(function(x){if(Math.abs(x-a)<Math.abs(best-a))best=x;});return best;}
  var chart=root.querySelector('.anum-chart'),val=root.querySelector('.anum-val'),rates=root.querySelector('.anum-rates');
  var sumEl=root.querySelector('.anum-sum');
  function rate(v){var h=0;v.forEach(function(x){if(x>=n)h++;});return [h,v.length];}
  function draw(){
    val.textContent=n+'+ '+PL[stat];
    var v=D.last[stat],top=Math.max(Math.max.apply(null,v.concat([0])),n*1.6,n+2);
    chart.replaceChildren();
    var ln=el('span','anum-line');ln.style.bottom=(100*n/top)+'%';chart.appendChild(ln);
    v.forEach(function(x,i){var b=el('span','anum-bar '+(x>=n?'hit':'miss'));
      b.style.height=Math.max(5,100*x/top)+'%';b.title=D.dates[i]+': '+x+' '+PL[stat];chart.appendChild(b);});
    rates.replaceChildren();
    var s=D.season[stat],all=D.last[stat];
    [['L5',all.slice(-5)],['L10',all.slice(-10)],['L15',all.slice(-15)],['Season',s]].forEach(function(p){
      var r=rate(p[1]),d=el('div','anum-rt');d.appendChild(el('span','anum-rl',p[0]));
      d.appendChild(el('span','anum-rv',r[1]?Math.round(100*r[0]/r[1])+'%':'—'));
      d.appendChild(el('span','anum-rg',r[0]+'/'+r[1]));rates.appendChild(d);});
    var h=rate(all);sumEl.textContent='Cleared '+n+' '+PL[stat]+' in '+h[0]+' of his last '+h[1]+' games.';
  }
  root.querySelectorAll('.anum-b[data-stat]').forEach(function(b){b.addEventListener('click',function(){
    stat=b.dataset.stat;n=def(stat);
    root.querySelectorAll('.anum-b[data-stat]').forEach(function(o){o.setAttribute('aria-pressed',o===b?'true':'false');});
    draw();});});
  root.querySelector('.anum-minus').addEventListener('click',function(){if(n>1){n--;draw();}});
  root.querySelector('.anum-plus').addEventListener('click',function(){n++;draw();});
  n=def(stat);draw();
})();
</script>"""


def chart_block(g, cur):
    """g = one player's rows (this season + last), sorted by date."""
    last = g.tail(CHART_GAMES)
    season = g[g["season"] == cur]
    data = {
        "ladder": LADDER, "pl": STAT_PL,
        "dates": [fmt_full(d) for d in last["gameDate"]],
        "last": {k: [num(v) for v in last[c]] for k, c, *_ in STATS},
        "season": {k: [num(v) for v in season[c]] for k, c, *_ in STATS},
    }
    js = json.dumps(data, separators=(",", ":"), ensure_ascii=True).replace("<", "\\u003c")
    btns = "".join(f'<button type="button" class="anum-b" data-stat="{k}" '
                   f'aria-pressed="{"true" if k == "PTS" else "false"}">{k}</button>' for k, *_ in STATS)
    first, lastd = last["gameDate"].iloc[0], last["gameDate"].iloc[-1]
    return (
        f'{CH_START}{CHART_CSS}<section class="anum" id="anum" aria-labelledby="anum-h">'
        f'<h2 id="anum-h">Last {len(last)} games against a number</h2>'
        f'<div class="anum-row" role="group" aria-label="Stat">{btns}</div>'
        '<div class="anum-row anum-step" role="group" aria-label="Number">'
        '<button type="button" class="anum-b anum-minus" aria-label="Lower the number">−</button>'
        '<output class="anum-val" aria-live="polite"></output>'
        '<button type="button" class="anum-b anum-plus" aria-label="Raise the number">+</button></div>'
        '<div class="anum-chart" aria-hidden="true"></div>'
        f'<div class="anum-axis"><span>{esc(fmt_full(first))}</span><span>{esc(fmt_full(lastd))}</span></div>'
        '<p class="anum-sum"></p><div class="anum-rates"></div>'
        '<p class="anum-note">Green = at or over the number, red = under. Regular season and playoffs. '
        'Season = this season\'s games.</p>'
        f'<script type="application/json" id="anum-data">{js}</script></section>{CHART_JS}{CH_END}\n'
    )


def inject_charts(df, cur, slugs):
    pat = re.compile(re.escape(CH_START) + r".*?" + re.escape(CH_END) + r"\n?", re.S)
    done = 0
    for pid, g in df.groupby("personId", sort=False):
        slug = slugs.get(int(pid))
        if not slug:
            continue
        path = os.path.join(PLAYERS_DIR, f"{slug}.html")
        with open(path, encoding="utf-8") as f:
            html = f.read()
        html = pat.sub("", html)
        if CH_ANCHOR not in html:
            continue
        html = html.replace(CH_ANCHOR, chart_block(g, cur) + CH_ANCHOR, 1)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(html)
        done += 1
    print(f"  'against a number' chart on {done} player pages")


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true")
    a = ap.parse_args()
    today = datetime.datetime.now(datetime.timezone.utc).date()

    df, through, cur = load_recent()
    print(f"  data through {through}, season {cur}-{(cur + 1) % 100:02d}: "
          f"{df[df['season'] == cur]['personId'].nunique()} players this season")
    slugs = page_slugs()
    names = df.drop_duplicates("personId", keep="last").set_index("personId")["name"].to_dict()

    cards = find_cards(df, cur)[:MAX_CARDS]
    for c in cards:
        c["team"] = c["g"]["franchise"].iloc[-1]
        c["rates"] = hit_rates(c["svals"], c["n"])

    try:
        with open(META_CACHE, encoding="utf-8") as f:
            metas = json.load(f)
    except FileNotFoundError:
        metas = {}
    metas = refresh_meta([c["pid"] for c in cards], metas, today, names=names, network=not a.no_network)
    os.makedirs(os.path.dirname(META_CACHE), exist_ok=True)
    with open(META_CACHE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dict(sorted(metas.items(), key=lambda kv: int(kv[0]))), f, indent=0, sort_keys=True)
        f.write("\n")

    assign_notes(cards, names, metas)

    with open(OUT_HTML, "w", encoding="utf-8", newline="\n") as f:
        f.write(build_page(cards, names, slugs, metas, through))
    out = {
        "updated": through.isoformat(),
        "season": f"{cur}-{(cur + 1) % 100:02d}",
        "page": PAGE_URL,
        "count": len(cards),
        "rules": {"window": WINDOW, "min_hits": NEED_HITS, "min_gap_points": int(MIN_GAP * 100),
                  "min_minutes": MIN_MINUTES, "min_baseline_games": MIN_BASE_GAMES,
                  "ladder": LADDER, "stat_order": [k for k, *_ in STATS]},
        "cards": [card_json(c, names[c["pid"]], slugs.get(c["pid"]), metas.get(str(c["pid"]), {}))
                  for c in cards],
    }
    with open(OUT_JSON, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"  on-a-run: {len(cards)} cards -> on-a-run.html, data/on-a-run.json")

    inject_charts(df, cur, slugs)
    return 0


if __name__ == "__main__":
    sys.exit(main())

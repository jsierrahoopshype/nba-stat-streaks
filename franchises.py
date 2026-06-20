"""
franchises.py — single source of truth for the city-era -> modern-franchise map.

47 city-era `team_disp` labels collapse into 30 modern NBA franchises. 42 labels map
cleanly; 5 labels are shared by two franchises and are partitioned by SEASON (clean era
gaps in the data, no player spans both eras):

  Philadelphia : <=1962 Warriors  -> Golden State | >=1963 76ers        -> Philadelphia 76ers
  San Diego    : <=1971 Rockets   -> Houston      | >=1978 Clippers     -> LA Clippers
  New Orleans  : <=1979 Jazz      -> Utah         | >=2002 Hornets/Pels -> New Orleans Pelicans
  Milwaukee    : <=1955 Hawks     -> Atlanta      | >=1968 Bucks        -> Milwaukee Bucks
  Chicago      : <=1963 Packers/Zephyrs -> Washington | >=1966 Bulls    -> Chicago Bulls

All 30 franchises are current NBA teams (no defunct franchises).
"""
import re

# --- 42 cleanly-mapped city-era labels -------------------------------------- #
CLEAN_MAP = {
    "Atlanta": "Atlanta Hawks", "Tri-Cities": "Atlanta Hawks", "St. Louis": "Atlanta Hawks",
    "Boston": "Boston Celtics",
    "New Jersey": "Brooklyn Nets", "Brooklyn": "Brooklyn Nets",
    "Charlotte": "Charlotte Hornets",
    "Cleveland": "Cleveland Cavaliers",
    "Dallas": "Dallas Mavericks",
    "Denver": "Denver Nuggets",
    "Ft. Wayne Zollner": "Detroit Pistons", "Detroit": "Detroit Pistons",
    "San Francisco": "Golden State Warriors", "Golden State": "Golden State Warriors",
    "Houston": "Houston Rockets",
    "Indiana": "Indiana Pacers",
    "Buffalo": "LA Clippers", "LA Clippers": "LA Clippers",
    "Minneapolis": "Los Angeles Lakers", "LA Lakers": "Los Angeles Lakers",
    "Vancouver": "Memphis Grizzlies", "Memphis": "Memphis Grizzlies",
    "Miami": "Miami Heat",
    "Minnesota": "Minnesota Timberwolves",
    "New York": "New York Knicks",
    "Seattle": "Oklahoma City Thunder", "Oklahoma City": "Oklahoma City Thunder",
    "Orlando": "Orlando Magic",
    "Syracuse": "Philadelphia 76ers",
    "Phoenix": "Phoenix Suns",
    "Portland": "Portland Trail Blazers",
    "Rochester": "Sacramento Kings", "Cincinnati": "Sacramento Kings",
    "Kansas City-Omaha": "Sacramento Kings", "Kansas City": "Sacramento Kings",
    "Sacramento": "Sacramento Kings",
    "San Antonio": "San Antonio Spurs",
    "Toronto": "Toronto Raptors",
    "Utah": "Utah Jazz",
    "Baltimore": "Washington Wizards", "Capital": "Washington Wizards", "Washington": "Washington Wizards",
}

# --- 5 ambiguous labels: (label) -> [(season_max, franchise), ... fallthrough] #
# evaluated in order; the first whose season <= season_max wins; last entry is the tail
SPLITS = {
    "Philadelphia": [(1962, "Golden State Warriors"), (9999, "Philadelphia 76ers")],
    "San Diego":    [(1974, "Houston Rockets"),       (9999, "LA Clippers")],
    "New Orleans":  [(1990, "Utah Jazz"),             (9999, "New Orleans Pelicans")],
    "Milwaukee":    [(1960, "Atlanta Hawks"),         (9999, "Milwaukee Bucks")],
    "Chicago":      [(1964, "Washington Wizards"),    (9999, "Chicago Bulls")],
}

# old city-era URL -> franchise it should redirect to (ambiguous labels -> modern successor)
REDIRECT_MODERN = {
    "Philadelphia": "Philadelphia 76ers", "San Diego": "LA Clippers",
    "New Orleans": "New Orleans Pelicans", "Milwaukee": "Milwaukee Bucks",
    "Chicago": "Chicago Bulls",
}

# the 30 modern franchises (all currently active)
FRANCHISES = sorted(set(CLEAN_MAP.values()))


def team_slug(t):
    return re.sub(r"[^a-z0-9]+", "-", str(t).lower()).strip("-")


def nba_season(label, year, month):
    """season-start year for a (year, month); July+ belongs to that calendar year."""
    return year if month >= 7 else year - 1


def franchise_of(team_disp, season):
    if team_disp in SPLITS:
        for smax, fr in SPLITS[team_disp]:
            if season <= smax:
                return fr
    return CLEAN_MAP[team_disp]


def add_franchise(df):
    """Return a copy of df with `season` and `franchise` columns (vectorized)."""
    gd = df["gameDate"]
    season = gd.dt.year.where(gd.dt.month >= 7, gd.dt.year - 1)
    fr = df["team_disp"].map(CLEAN_MAP)          # NaN for the 5 ambiguous labels
    for label, rules in SPLITS.items():
        m = df["team_disp"].eq(label)
        for smax, frname in rules:
            fr = fr.mask(m & (season <= smax) & fr.isna(), frname)
    out = df.copy()
    out["season"] = season
    out["franchise"] = fr
    if out["franchise"].isna().any():
        bad = sorted(out.loc[out["franchise"].isna(), "team_disp"].unique())
        raise ValueError(f"Unmapped team_disp labels: {bad}")
    return out


def redirect_target(team_disp):
    """franchise a legacy city-era slug should redirect to."""
    return REDIRECT_MODERN.get(team_disp, CLEAN_MAP.get(team_disp))

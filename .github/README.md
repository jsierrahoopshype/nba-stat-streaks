# NBA Stat Streaks and Droughts

Live on HoopsMatic: https://hoopsmatic.com/streaks-droughts

This repo holds the code and data behind that page. GitHub Pages builds it, and hoopsmatic.com serves it, so please link to the hoopsmatic.com address.

## On a run (daily)

Page: https://hoopsmatic.com/streaks-droughts/on-a-run/ shows players clearing a stat number far more often than usual right now. Rebuilt by the nightly job (`build_on_a_run.py`, step 6 of `nightly_build.py`) from the same data as the rest of the section: the committed Kaggle history plus this season's cdn.nba.com box lines.

**Rule.** Stats in this order: PTS, REB, AST, STL, BLK, 3PM. Ladder: PTS 10/15/20/25/30/35/40, REB 5/8/10/12/15, AST 5/8/10/12, STL 2/3, BLK 2/3/4, 3PM 2/3/4/5. A player qualifies for a stat at the highest ladder number where:

- he played 15+ minutes in each of his last 10 games this season (regular season + playoffs),
- he cleared the number in 8+ of those 10,
- that rate is 30+ points above his rate at the same number in his baseline: his earlier games this season (before the last 10) when there are 10+, otherwise last season. Baseline games count only with 15+ minutes, and the baseline needs 10+ games,
- the number is at or above his season average for that stat (so a 25-ppg scorer clearing 10 or 20 never shows up).

Ranked by the gap (L10 rate minus baseline rate), then by the current straight streak (this season). Up to 40 cards.

### Public JSON: `data/on-a-run.json`

URL: https://hoopsmatic.com/streaks-droughts/data/on-a-run.json (same cards, same order as the page).

```json
{
  "updated": "2026-11-26",          // games through this date
  "season": "2026-27",
  "page": "https://hoopsmatic.com/streaks-droughts/on-a-run/",
  "count": 40,
  "rules": { "window": 10, "min_hits": 8, "min_gap_points": 30, "min_minutes": 15,
             "min_baseline_games": 10, "ladder": { "PTS": [10, 15, ...], ... },
             "stat_order": ["PTS", "REB", "AST", "STL", "BLK", "3PM"] },
  "cards": [
    {
      "player_id": 1641709,                 // NBA.com person id
      "name": "Ausar Thompson",
      "player_url": "https://hoopsmatic.com/streaks-droughts/players/ausar-thompson-1641709.html",
      "team": "Detroit Pistons",            // team of his latest game this season
      "team_url": "https://hoopsmatic.com/streaks-droughts/teams/detroit-pistons.html",
      "position": "G-F",                    // NBA.com roster position, or null
      "headshot": "https://cdn.nba.com/headshots/nba/latest/260x190/1641709.png",  // null when NBA.com has none
      "stat": "REB", "stat_label": "rebounds", "number": 8, "label": "8+ rebounds",
      "last10": [                           // oldest first, most recent last
        { "date": "2026-11-05", "game_id": "22600123", "opponent": "Miami Heat",
          "value": 9, "minutes": 31, "cleared": true }
      ],
      "hit_rates": {                        // at "number", this season
        "l5":  { "hits": 5,  "games": 5,  "pct": 100 },
        "l10": { "hits": 9,  "games": 10, "pct": 90 },
        "l15": { "hits": 11, "games": 15, "pct": 73 },
        "season": { "hits": 12, "games": 17, "pct": 71 }
      },
      "baseline": { "source": "last_season", "games": 60, "hits": 21, "pct": 35 },  // or "earlier_this_season"
      "gap_points": 55,                     // L10 pct minus baseline pct
      "streak": 6,                          // current straight games at "number" this season
      "season_average": 8.7,
      "note": "One plain-language sentence shown on the card."
    }
  ]
}
```

`data/player-meta-cache.json` is a build cache (NBA.com roster position and whether cdn.nba.com has a headshot) for the players on the cards; it is fetched only for players missing from it, and a failed fetch never fails the build.

### Player pages: "against a number"

Pages of players who played this season or last season get a block with their last 20 games as bars (green at or over, red under), a dashed line at the number, stat buttons (PTS, REB, AST, STL, BLK, 3PM), a +/− number stepper, and the L5 / L10 / L15 / season hit rate. The default number is his season average rounded to the nearest ladder step. Other player pages are not changed.

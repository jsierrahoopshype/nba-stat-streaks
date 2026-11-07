# Sources of the files in `history/`

| File | Source | Terms |
|---|---|---|
| `PlayerStatistics_hist.csv.gz` | Kaggle, eoinamoore/historical-nba-data-and-player-box-scores (`PlayerStatistics.csv`): Regular Season, Playoffs and NBA Cup games through 2025-26, only the columns the engine reads | CC0 |
| `Players.csv.gz` | Same Kaggle dataset (`Players.csv`): personId, names, country | CC0 |
| `nationalities.csv.gz` | Alberto (HoopsMatic), own compilation | Approved for use on HoopsMatic |
| `patch_missing_games.csv.gz` | 6 postponed 2024-25 games the Kaggle file lacks: NBA box lines (stats.nba.com, via nba-editorial-bots) | NBA.com |

The season in progress (2026-27 on) is read each night from the box-score file nba-editorial-bots publishes on GitHub Pages (cdn.nba.com box scores, NBA.com).

@echo off
rem ==========================================================================
rem  streaks-update-and-publish.bat  -  double-click to update and publish.
rem
rem  1. nightly_catchup.py   apply every missing night via fetch_nightly.py
rem  2. build_site.py        leaderboards, feats, droughts, player pages
rem  3. build_teams.py --all team pages
rem  4. build_sitemap.py     sitemap.xml + sitemap-lastmod.json
rem  5. git add (build outputs only) + commit + push
rem
rem  Any failure stops the script BEFORE anything is committed or pushed.
rem ==========================================================================
setlocal EnableExtensions
title Streaks update and publish
cd /d "%~dp0"
echo Repo: %CD%
echo.

rem ---- Python with pandas (python, then the py launcher) --------------------
set "PY="
python -c "import pandas, numpy" >nul 2>nul && set "PY=python"
if not defined PY py -3 -c "import pandas, numpy" >nul 2>nul && set "PY=py -3"
if not defined PY (set "MSG=Python with pandas and numpy not found. Install Python, then run: pip install pandas numpy" & goto fail)

rem ---- Git ------------------------------------------------------------------
git --version >nul 2>nul || (set "MSG=Git not found on PATH. Install Git for Windows from git-scm.com" & goto fail)

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
if /i not "%BRANCH%"=="main" (set "MSG=You are on branch %BRANCH%. Switch to main first." & goto fail)

git diff --cached --quiet || (set "MSG=There are already staged changes. Commit or unstage them first so they are not published by accident." & goto fail)

echo [0/5] Pulling latest main from GitHub...
git pull --ff-only || (set "MSG=git pull failed. Local changes may conflict with GitHub; nothing was changed." & goto fail)
echo.

rem ---- Kaggle data: repo data\ first, then the old C:\nba-stat-streaks\data --
if exist "data\PlayerStatistics.csv" goto data_ok
if exist "C:\nba-stat-streaks\data\PlayerStatistics.csv" (
    set "NBA_STREAKS_DATA=C:\nba-stat-streaks\data"
    echo Using data from C:\nba-stat-streaks\data
    goto data_ok
)
set "MSG=PlayerStatistics.csv not found in %CD%\data or C:\nba-stat-streaks\data"
goto fail
:data_ok

rem ---- 1. Fetch -------------------------------------------------------------
echo [1/5] Fetching last night's games...
%PY% nightly_catchup.py
if "%ERRORLEVEL%"=="3" goto gap
if errorlevel 1 (set "MSG=Step 1 fetch failed. Nothing was committed or pushed." & goto fail)
goto build
:gap
echo.
choice /C YN /M "The streak state is more than a week behind. Catch up every missing night now"
if errorlevel 2 (set "MSG=Catch-up declined. Nothing was committed or pushed." & goto fail)
%PY% nightly_catchup.py --allow-gap
if errorlevel 1 (set "MSG=Step 1 catch-up failed. Nothing was committed or pushed." & goto fail)

:build
echo.
echo [2/5] Building site pages - this takes a while...
%PY% build_site.py
if errorlevel 1 (set "MSG=Step 2 build_site.py failed. Nothing was committed or pushed." & goto fail)

echo.
echo [3/5] Building team pages...
%PY% build_teams.py --all
if errorlevel 1 (set "MSG=Step 3 build_teams.py failed. Nothing was committed or pushed." & goto fail)

echo.
echo [4/5] Updating sitemap...
%PY% build_sitemap.py
if errorlevel 1 (set "MSG=Step 4 build_sitemap.py failed. Nothing was committed or pushed." & goto fail)

rem ---- 5. Commit + push (build outputs only, never scripts or data CSVs) -----
echo.
echo [5/5] Publishing...
git add -- active-state.json active-streaks.html lastgame.html index.html feats.html droughts.html teams.html players teams streaks-data.js feats-data.js search-index.js droughts-data.js droughts-leaderboard-data.js sitemap.xml sitemap-lastmod.json
if errorlevel 1 (set "MSG=git add failed. Nothing was committed or pushed." & goto fail)
if exist "data\daily" git add -- data/daily

git diff --cached --quiet
if not errorlevel 1 (
    echo Nothing changed since the last publish. Done.
    goto done
)

rem UTC date for the commit message (temp file avoids cmd paren-parsing issues)
%PY% -c "import datetime; print(datetime.datetime.now(datetime.timezone.utc).date())" > "%TEMP%\streaks_today.txt"
set /p TODAY=<"%TEMP%\streaks_today.txt"
del "%TEMP%\streaks_today.txt" >nul 2>nul
git commit -m "Streaks update (%TODAY%)"
if errorlevel 1 (set "MSG=git commit failed. Nothing was pushed." & goto fail)
git push
if errorlevel 1 (set "MSG=The commit was made on this PC but git push failed. Check your connection or GitHub login, then run: git push" & goto fail)

echo.
echo ==== Done: published to GitHub. ====
:done
echo.
pause
exit /b 0

:fail
echo.
echo ==== STOPPED: %MSG% ====
echo.
pause
exit /b 1

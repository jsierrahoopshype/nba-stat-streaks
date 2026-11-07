@echo off
rem ==========================================================================
rem  streaks-update-and-publish.bat  -  double-click to update and publish.
rem
rem  FALLBACK ONLY: the "Nightly streak update" GitHub Action does this daily.
rem
rem  1-4. nightly_build.py   history\ + season file -> every page, active
rem                          streaks, team pages, sitemap (same as the Action)
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

rem ---- 1-4. Rebuild everything (same script as the daily GitHub Action) ------
rem  nightly_build.py builds from history\ (committed Kaggle extract) plus the
rem  season file nba-editorial-bots publishes, so no local Kaggle download is
rem  needed. Fallback only: the Action normally does this every day.
echo [1/5] Rebuilding streaks, droughts, player, team and Active Streaks pages - this takes a while...
%PY% nightly_build.py --no-ready-check
if errorlevel 1 (set "MSG=nightly_build.py failed. Nothing was committed or pushed." & goto fail)

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

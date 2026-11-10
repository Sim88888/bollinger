@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Recuperation des cours Yahoo depuis cet ordinateur...
set REFRESH_MIN=0
set MAX_PER_RUN=60
set BUDGET_S=600
py fetch_data.py
if errorlevel 9009 python fetch_data.py
echo.
echo ============================================================
echo Termine. Ouvrez votre depot GitHub : Add file ^> Upload files,
echo glissez data.js et data.json de ce dossier, puis Commit changes.
echo ============================================================
pause
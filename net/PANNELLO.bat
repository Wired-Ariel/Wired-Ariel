@echo off
rem ============================================================
rem  IL PANNELLO: e' l'unica cosa da lanciare.
rem  Si apre da solo nel browser. Da li' si fa tutto: relay,
rem  caricamento del gioco nel GBA, partita, sblocco.
rem  Lascia aperta questa finestra nera: e' il motore.
rem ============================================================
cd /d "%~dp0"
set PY=python
if exist "D:\Progettini\Python313\python.exe" set "PY=D:\Progettini\Python313\python.exe"
"%PY%" pannello.py
pause

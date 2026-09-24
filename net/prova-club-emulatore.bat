@echo off
rem Cable Club: GBA fisico contro EMULATORE (banco di prova, senza rete)
rem Prima: mGBA con la ROM + Tools ^> Scripting ^> mgba\club-emulatore.lua
cd /d "%~dp0"
python club_mgba.py
pause

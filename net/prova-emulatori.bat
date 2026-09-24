@echo off
rem Prova a DUE emulatori in locale (procedura E-1/E-2 in NOTES.md).
rem Compila i due script Lua e apre relay + due bridge in tre finestre.
rem Poi, con due mGBA GIA' IN PARTITA (ROM italiana):
rem   1a istanza -> Tools / Scripting / Load script -> mgba\inject.p1.lua
rem   2a istanza -> Tools / Scripting / Load script -> mgba\inject.p2.lua
rem Il testo ROSSO "LOAD segment with RWX permissions" e' un avviso del
rem linker, NON un errore: si ignora.
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0run-local.ps1" -Syms it
pause

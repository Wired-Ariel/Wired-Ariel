@echo off
rem Lato Lain: carica il payload nel GBA via cavo, modo MultiPlay 16 bit.
rem Prerequisiti fisici, in quest'ordine: GBA SPENTO, cartuccia FUORI,
rem SW1 su 3,3 V, cavo GBA nel verso marcato, Pico su USB, POI accendi il GBA.
rem
rem L'interprete e' chiamato per percorso pieno: sta sull'HDD e non e' sul
rem PATH, quindi "python" qui prenderebbe il segnaposto del Microsoft Store.
cd /d "%~dp0"
python mb_multi.py ..\hw\mbstub\build\mbstub.gba
pause

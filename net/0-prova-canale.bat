@echo off
rem Lato Lain: prova del canale USB+cavo+GBA, senza internet e senza relay.
rem Da usare col gioco gia' avviato e il payload dentro (dopo 1-multiboot e
rem la cartuccia a caldo). Cammina per 30 secondi.
rem Criterio: almeno 15 eventi in 30 secondi.
cd /d "%~dp0"
python usb_link.py --ascolta 30 --timing 7400
pause

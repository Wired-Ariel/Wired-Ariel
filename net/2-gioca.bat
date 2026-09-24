@echo off
rem Lato Lain: la partita. Da lanciare DOPO 1-multiboot.bat, DOPO aver
rem inserito la cartuccia a caldo e DOPO aver caricato il salvataggio con
rem il personaggio nell'overworld. Il relay deve essere gia' su.
rem
rem Stanza 4242. Il PEER-ID va chiesto perche' DEVE essere diverso da quello
rem di ogni amico nella stanza: due client con lo stesso numero si buttano
rem fuori a vicenda a ogni battito (relay.py, move_to_room) e non si vedono
rem MAI - senza nessun errore da nessuna parte. Si puo' anche passare da riga
rem di comando: 2-gioca.bat 3
cd /d "%~dp0"
set PEER=%1
if "%PEER%"=="" set /p PEER=Peer-id (1 per Lain, 2/3/4 per gli amici, INVIO per 1):
if "%PEER%"=="" set PEER=1
python client.py --transport usb --room 4242 --peer-id %PEER% --usb-timing 7400 --relay 127.0.0.1:9000
pause

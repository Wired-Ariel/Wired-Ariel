@echo off
rem =============================================================================
rem La partita CON GLI AMICI, attraverso il relay sulla VPS (2026-08-25).
rem =============================================================================
rem
rem Differenza da 2-gioca.bat: quello si collega al relay sul PC di casa
rem (127.0.0.1:9000) e serve per le prove in locale. Questo va sul relay in
rem rete, e ci va via WebSocket - la porta UDP 9000 della VPS NON e' raggiungibile
rem da internet (filtro nella Security List della VCN Oracle), mentre la 443
rem passa da qualunque rete.
rem
rem Da lanciare DOPO 1-multiboot.bat, DOPO aver inserito la cartuccia a caldo
rem e DOPO essere in partita nell'overworld.
rem
rem La STANZA la chiede: un numero fra 1 e 65535 da concordare con gli amici. Il PEER-ID lo chiede, e non e' pignoleria: 1 per Lain, 2/3/4
rem per gli amici, e DEVONO essere diversi fra loro. Due client con lo stesso
rem numero si espellono a vicenda dalla stanza a ogni battito e non si vedono
rem MAI, senza nessun errore - dal 2026-08-28 il relay lo grida nel log ("peer
rem N RIMBALZA fra due indirizzi"). Si puo' anche passare da riga di comando:
rem 2-gioca-internet.bat 3
cd /d "%~dp0"
set STANZA=%2
if "%STANZA%"=="" set /p STANZA=Numero della stanza (lo stesso degli amici): 
set PEER=%1
if "%PEER%"=="" set /p PEER=Peer-id (1..4, DIVERSO per ogni giocatore, INVIO per 1):
if "%PEER%"=="" set PEER=1
python client.py --transport usb --room %STANZA% --peer-id %PEER% --usb-timing 7400 --relay wss://gbcatrade.wired-ariel.it/passotile/ws
pause

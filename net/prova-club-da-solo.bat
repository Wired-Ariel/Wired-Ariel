@echo off
rem Il PARTNER FINTO: il Cable Club si prova DA SOLI (procedura V).
rem
rem Prima di lanciarlo devono essere gia' su, dal pannello o dai .bat:
rem   - il relay (avvia-relay.bat o il pannello)
rem   - la partita (2-gioca.bat o il pannello), col GBA che cammina
rem Poi doppio clic qui, e col GBA vai dalla signorina degli SCAMBI.
rem Il finto fa la parte dell'amico: scala, blocco giocatore, schede.
rem Criterio di vittoria: "LINKUP COMPLETO" in questa finestra e il tuo
rem GBA che entra nella saletta.
cd /d "%~dp0"
python -u finto_club.py --relay 127.0.0.1:9000 --room 4242 --peer-id 2
pause

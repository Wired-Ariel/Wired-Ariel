@echo off
rem =========================================================================
rem avvia-relay.bat - il relay della partita, sul PC di Lain.
rem
rem Da lanciare PRIMA che chiunque (tu o l'amico) avvii il proprio client.
rem La finestra deve restare aperta per tutta la partita: chiuderla butta
rem giu' la sessione di tutti.
rem
rem Prerequisiti una-tantum (procedura R-0 in NOTES.md):
rem   1. regola firewall: da un prompt AMMINISTRATORE, una volta sola:
rem        netsh advfirewall firewall add rule name="GBA overworld relay" dir=in action=allow protocol=UDP localport=9000
rem   2. port forwarding UDP 9000 sul router verso questo PC.
rem =========================================================================
rem L'interprete si chiama per percorso pieno: sta sull'HDD (regola fissa,
rem niente installazioni sull'SSD di sistema) e NON e' sul PATH, quindi
rem scrivere "python" qui prenderebbe il segnaposto del Microsoft Store.
python "%~dp0relay.py" --port 9000 --verbose
pause

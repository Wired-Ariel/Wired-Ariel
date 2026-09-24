@echo off
rem =========================================================================
rem avvia-relay-ws.bat - il frontale WebSocket del relay, sul PC di Lain.
rem
rem Serve ai giocatori DAL BROWSER (pannello web, web\index.html): i browser
rem non fanno UDP, quindi parlano con questo, e questo gira tutto a relay.py
rem (che deve essere gia' acceso: avvia-relay.bat). Browser ed emulatori
rem finiscono nelle stesse stanze.
rem
rem Ascolta su TCP 9001 (ws://). Per l'amico remoto dal browser serve wss://
rem (la pagina e' https): o un tunnel (cloudflared, vedi web\README.md) o un
rem VPS con Caddy. Aprire la 9001 sul router da sola NON basta.
rem =========================================================================
python "%~dp0relay_ws.py" --port 9001 --relay 127.0.0.1:9000 --verbose
pause

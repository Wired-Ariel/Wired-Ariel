@echo off
rem Lato Lain: SBLOCCA l'adattatore quando il client dice "endpoint USB del
rem Pico inceppato" oppure "il comando 'Cancel' non e' partito".
rem
rem NON SPEGNERE IL GBA: il payload vive nella RAM della console e sopravvive
rem a questo riavvio. Spegnendolo perderesti il multiboot e dovresti rifare
rem tutto da 1-multiboot.bat.
rem
rem Dopo che ha detto "ricomparso", rilancia 2-gioca.bat.
cd /d "%~dp0"
python usb_link.py --riavvia
pause

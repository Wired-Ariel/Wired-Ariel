#!/bin/bash
# =============================================================================
# build-pokeemerald.sh - build matching di pokeemerald in WSL
# =============================================================================
#
# Produce pokeemerald.gba + pokeemerald.map. Il .map e' l'obiettivo vero: e'
# la rubrica degli indirizzi di ogni funzione del gioco.
#
# Build MODERN=0 (agbcc), non "make modern": solo la matching riproduce gli
# indirizzi della ROM retail. La modern compila piu' in fretta ma produrrebbe
# indirizzi inutili sull'hardware.
#
# Prerequisiti, da installare a mano una volta sola (chiede la password):
#   sudo apt update
#   sudo apt install -y build-essential binutils-arm-none-eabi git libpng-dev pkg-config
#
# Uso, da Windows:
#   wsl -d Ubuntu -- bash /mnt/d/Progettini/GBA-USB/overworld-link/tools/build-pokeemerald.sh

set -e

POKE=/mnt/d/Progettini/GBA-USB/repo-studio/pokeemerald
AGBCC=$HOME/agbcc
EXPECTED_SHA1=f3ae088181bf583e55daf962a92bb46f4f1d07b7

echo "=== verifica prerequisiti ==="
missing=0
for c in gcc g++ make git arm-none-eabi-as arm-none-eabi-ld arm-none-eabi-objcopy; do
    if ! command -v "$c" >/dev/null 2>&1; then
        echo "MANCA $c"
        missing=1
    fi
done
if [ ! -f /usr/include/png.h ]; then
    echo "MANCA libpng-dev (png.h)"
    missing=1
fi
if [ "$missing" -ne 0 ]; then
    echo
    echo "Installa i prerequisiti con:"
    echo "  sudo apt update && sudo apt install -y build-essential binutils-arm-none-eabi git libpng-dev pkg-config"
    exit 1
fi
echo "ok"

echo
echo "=== agbcc ==="
# Si compila nella home di WSL, non su /mnt/d: il filesystem montato e' molto
# piu' lento e agbcc fa migliaia di file piccoli.
if [ ! -d "$AGBCC" ]; then
    git clone https://github.com/pret/agbcc "$AGBCC"
fi
cd "$AGBCC"
if [ ! -f build/agbcc ] && [ ! -f release/bin/agbcc ]; then
    ./build.sh
fi
./install.sh "$POKE"
echo "agbcc installato in $POKE/tools/agbcc"

echo
echo "=== build pokeemerald (MODERN=0, matching) ==="
cd "$POKE"
make -j"$(nproc)"

echo
echo "=== verifica sha1 ==="
ACTUAL=$(sha1sum pokeemerald.gba | cut -d' ' -f1)
echo "atteso  : $EXPECTED_SHA1"
echo "ottenuto: $ACTUAL"
if [ "$ACTUAL" = "$EXPECTED_SHA1" ]; then
    echo "BUILD MATCHING OK"
else
    echo "BUILD NON MATCHING: gli indirizzi del .map non valgono sulla ROM retail"
    exit 1
fi

echo
echo "=== artefatti ==="
ls -l pokeemerald.gba pokeemerald.map

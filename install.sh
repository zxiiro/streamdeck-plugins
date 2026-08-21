#!/bin/bash
# Install plugins into the Elgato Stream Deck plugins directory.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/Library/Application Support/com.elgato.StreamDeck/Plugins"

mkdir -p "$DEST"

for plugin in "$ROOT"/*.sdPlugin; do
  [ -d "$plugin" ] || continue
  name="$(basename "$plugin")"
  target="$DEST/$name"

  if [ ! -x "$plugin/venv/bin/python3" ]; then
    python3 -m venv "$plugin/venv"
    "$plugin/venv/bin/pip" install -r "$plugin/requirements.txt"
  fi

  if [ -e "$target" ] && [ ! -L "$target" ]; then
    echo "Moving existing $name aside"
    mv "$target" "$target.bak.$(date +%Y%m%d%H%M%S)"
  fi
  ln -sfn "$plugin" "$target"
  echo "Linked $name -> $plugin"
done

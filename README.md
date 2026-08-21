# Stream Deck plugins

Personal Elgato Stream Deck plugins.

Local settings (Home Assistant URL, tokens, shortcut names) are **not**
in this repo. They live in `~/.config/streamdeck/` and are tracked by
the `streamdeck` vcsh set (`dotfiles-streamdeck`).

## Desk Lamp

`com.zxiiro.desklamp.sdPlugin` toggles an Apple Home desk lamp from a
Stream Deck key and shows a yellow lamp when on, or a white outline
when off.

Pressing the key runs the Shortcuts shortcut named in local config
(default: `Toggle Desk Lamp`). Optional Home Assistant polling can keep
the icon in sync when the lamp is changed elsewhere.

## Install

```bash
./install.sh
```

That creates a venv, installs Python deps, and symlinks each `*.sdPlugin`
into `~/Library/Application Support/com.elgato.StreamDeck/Plugins/`.

Then copy the example config and restart Stream Deck:

```bash
mkdir -p ~/.config/streamdeck
cp com.zxiiro.desklamp.sdPlugin/config.json.example \
  ~/.config/streamdeck/desklamp.json
chmod 600 ~/.config/streamdeck/desklamp.json
```

Edit `desklamp.json` for shortcut name, and optionally Home Assistant
`ha_url`, `ha_token`, and `entity_id`.

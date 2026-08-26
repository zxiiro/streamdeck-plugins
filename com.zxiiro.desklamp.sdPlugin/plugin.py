#!/usr/bin/env python3
"""Stream Deck plugin: toggle Desk Lamp and show on/off lamp icons."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import websockets

DIR = Path(__file__).resolve().parent
XDG_DIR = Path.home() / ".config" / "streamdeck"
XDG_CONFIG = XDG_DIR / "desklamp.json"
STATE_PATH = XDG_DIR / "desklamp-state.json"
LOCAL_CONFIG = DIR / "config.json"
LOG = logging.getLogger("desklamp")

DEFAULTS = {
    "shortcut": "Toggle Desk Lamp",
    "get_shortcut": "",
    "ha_url": "",
    "ha_token": "",
    "entity_id": "",
}
BOOT_RETRIES = (0, 2, 5, 10, 20, 40)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for path in (XDG_CONFIG, LOCAL_CONFIG):
        if not path.is_file():
            continue
        try:
            cfg.update(json.loads(path.read_text()))
            break
        except Exception:
            LOG.exception("config read failed: %s", path)
    return cfg


def load_persisted_state() -> bool | None:
    try:
        data = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        LOG.exception("state read failed")
        return None
    if "is_on" not in data:
        return None
    return bool(data["is_on"])


def save_persisted_state(is_on: bool) -> None:
    try:
        XDG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps({"is_on": is_on}) + "\n")
    except Exception:
        LOG.exception("state write failed")


def parse_on_off(text: str) -> bool | None:
    value = text.strip().lower()
    if value in {"on", "true", "1", "1.0", "yes", "power on"}:
        return True
    if value in {"off", "false", "0", "0.0", "no", "power off"}:
        return False
    return None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("-port", dest="port", required=True)
    p.add_argument("-pluginUUID", dest="plugin_uuid", required=True)
    p.add_argument("-registerEvent", dest="register_event", required=True)
    p.add_argument("-info", dest="info", default="{}")
    return p.parse_args()


class Plugin:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ws = None
        self.cfg = load_config()
        self.contexts: dict[str, bool] = {}  # context -> is_on
        self._poll_task: asyncio.Task | None = None

    async def send(self, payload: dict) -> None:
        if self.ws is None:
            return
        await self.ws.send(json.dumps(payload))

    async def set_state(self, context: str, is_on: bool) -> None:
        self.contexts[context] = is_on
        save_persisted_state(is_on)
        await self.send(
            {
                "event": "setState",
                "context": context,
                "payload": {"state": 1 if is_on else 0},
            }
        )
        await self.send(
            {"event": "setSettings", "context": context, "payload": {"is_on": is_on}}
        )

    async def reassert_state(self, context: str, is_on: bool) -> None:
        for delay in (0.05, 0.25):
            await asyncio.sleep(delay)
            if self.contexts.get(context) != is_on:
                return
            await self.set_state(context, is_on)

    def ha_enabled(self) -> bool:
        return bool(self.cfg.get("ha_token") and self.cfg.get("ha_url"))

    def ha_request(self, path: str, method: str = "GET", body: dict | None = None) -> dict | list | None:
        url = self.cfg["ha_url"].rstrip("/") + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.cfg['ha_token']}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=4) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else None
        except Exception as exc:
            LOG.debug("HA request failed %s %s: %s", method, path, exc)
            return None

    def resolve_entity_id(self) -> str | None:
        if self.cfg.get("entity_id"):
            return self.cfg["entity_id"]
        states = self.ha_request("/api/states")
        if not isinstance(states, list):
            return None
        matches = []
        for st in states:
            eid = str(st.get("entity_id", ""))
            name = str((st.get("attributes") or {}).get("friendly_name", "")).lower()
            if "desk lamp" in name or eid.endswith(".desk_lamp") or "desk_lamp" in eid:
                matches.append(eid)
        # Prefer switch/light
        for prefix in ("switch.", "light.", "input_boolean."):
            for eid in matches:
                if eid.startswith(prefix):
                    self.cfg["entity_id"] = eid
                    return eid
        if matches:
            self.cfg["entity_id"] = matches[0]
            return matches[0]
        return None

    def read_ha_state(self) -> bool | None:
        if not self.ha_enabled():
            return None
        eid = self.resolve_entity_id()
        if not eid:
            return None
        st = self.ha_request(f"/api/states/{eid}")
        if not isinstance(st, dict):
            return None
        state = str(st.get("state", "")).lower()
        if state in {"on", "true", "open", "home"}:
            return True
        if state in {"off", "false", "closed", "unavailable", "unknown"}:
            return False
        return None

    def run_toggle(self) -> None:
        shortcut = self.cfg.get("shortcut") or "Toggle Desk Lamp"
        subprocess.Popen(
            ["/usr/bin/shortcuts", "run", shortcut],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    def read_shortcut_state(self) -> bool | None:
        shortcut = (self.cfg.get("get_shortcut") or "").strip()
        if not shortcut:
            return None
        out = Path("/tmp/desklamp-get-state.txt")
        try:
            result = subprocess.run(
                [
                    "/usr/bin/shortcuts",
                    "run",
                    shortcut,
                    "--output-path",
                    str(out),
                    "--output-type",
                    "public.plain-text",
                ],
                capture_output=True,
                text=True,
                timeout=12,
                stdin=subprocess.DEVNULL,
            )
        except Exception:
            LOG.debug("get shortcut failed", exc_info=True)
            return None
        chunks = [result.stdout or "", result.stderr or ""]
        if out.is_file():
            chunks.append(out.read_text(errors="ignore"))
        for chunk in chunks:
            parsed = parse_on_off(chunk)
            if parsed is not None:
                return parsed
        return None

    def read_lamp_state(self) -> bool | None:
        self.cfg = load_config()
        for reader in (self.read_ha_state, self.read_shortcut_state):
            try:
                value = reader()
            except Exception:
                LOG.debug("state reader failed", exc_info=True)
                continue
            if value is not None:
                return value
        return None

    def fallback_state(self, settings: dict | None = None) -> bool | None:
        live = None
        settings = settings or {}
        if "is_on" in settings:
            live = bool(settings["is_on"])
        persisted = load_persisted_state()
        if persisted is not None:
            return persisted
        return live

    def can_poll_live(self) -> bool:
        self.cfg = load_config()
        return self.ha_enabled() or bool((self.cfg.get("get_shortcut") or "").strip())

    async def apply_state(self, is_on: bool, contexts: list[str] | None = None) -> None:
        targets = contexts if contexts is not None else list(self.contexts)
        for ctx in targets:
            await self.set_state(ctx, is_on)

    async def sync_from_home(
        self,
        contexts: list[str] | None = None,
        retries: tuple[float, ...] = (0, 2, 5, 10),
        expected: bool | None = None,
    ) -> None:
        last_live = None
        for delay in retries:
            if delay:
                await asyncio.sleep(delay)
            is_on = await asyncio.to_thread(self.read_lamp_state)
            if is_on is None:
                continue
            last_live = is_on
            if expected is not None and is_on != expected:
                continue
            await self.apply_state(is_on, contexts)
            return
        if last_live is not None:
            await self.apply_state(last_live, contexts)
            return
        # Keep the optimistic or persisted icon if HomeKit/HA never answered.

    async def ensure_poll_loop(self) -> None:
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(self.poll_loop())

    async def poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(5)
                if self.contexts and self.can_poll_live():
                    await self.sync_from_home(retries=(0,))
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("poll failed")

    async def handle(self, msg: dict) -> None:
        event = msg.get("event")
        context = msg.get("context")
        if event == "willAppear" and context:
            payload = msg.get("payload") or {}
            settings = payload.get("settings") or {}
            fallback = self.fallback_state(settings)
            if fallback is not None:
                await self.set_state(context, fallback)
            await self.ensure_poll_loop()
            # Recheck live state: after reboot HomeKit/HA may not be up yet,
            # and Stream Deck two-state keys often come back inverted.
            asyncio.create_task(
                self.sync_from_home(contexts=[context], retries=BOOT_RETRIES)
            )
        elif event == "willDisappear" and context:
            self.contexts.pop(context, None)
        elif event in {"systemDidWakeUp", "deviceDidConnect"}:
            await self.ensure_poll_loop()
            asyncio.create_task(self.sync_from_home(retries=BOOT_RETRIES))
        elif event == "keyDown" and context:
            current = self.contexts.get(context)
            if current is None:
                current = self.fallback_state() or False
            desired = not current
            await self.set_state(context, desired)
            await asyncio.to_thread(self.run_toggle)
            asyncio.create_task(self.reassert_state(context, desired))
            asyncio.create_task(
                self.sync_from_home(
                    contexts=[context],
                    retries=(2.5, 5, 8),
                    expected=desired,
                )
            )
        elif event == "didReceiveSettings" and context:
            settings = (msg.get("payload") or {}).get("settings") or {}
            if "is_on" in settings and context not in self.contexts:
                await self.set_state(context, bool(settings["is_on"]))

    async def run(self) -> None:
        uri = f"ws://127.0.0.1:{self.args.port}"
        async with websockets.connect(uri, max_size=None) as ws:
            self.ws = ws
            await self.send(
                {"event": self.args.register_event, "uuid": self.args.plugin_uuid}
            )
            asyncio.create_task(self.sync_from_home(retries=BOOT_RETRIES))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                try:
                    await self.handle(msg)
                except Exception:
                    LOG.exception("handle failed: %s", msg.get("event"))


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    plugin = Plugin(parse_args())
    asyncio.run(plugin.run())


if __name__ == "__main__":
    main()

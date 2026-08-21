#!/usr/bin/env python3
"""Stream Deck plugin: toggle Desk Lamp and show on/off lamp icons."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import websockets

DIR = Path(__file__).resolve().parent
XDG_CONFIG = Path.home() / ".config" / "streamdeck" / "desklamp.json"
LOCAL_CONFIG = DIR / "config.json"
LOG = logging.getLogger("desklamp")

DEFAULTS = {
    "shortcut": "Toggle Desk Lamp",
    "ha_url": "",
    "ha_token": "",
    "entity_id": "",
}


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
        await self.send(
            {
                "event": "setState",
                "context": context,
                "payload": {"state": 1 if is_on else 0},
            }
        )

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

    async def refresh_from_ha(self) -> None:
        is_on = await asyncio.to_thread(self.read_ha_state)
        if is_on is None:
            return
        for ctx in list(self.contexts):
            await self.set_state(ctx, is_on)

    async def poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(3)
                if self.ha_enabled() and self.contexts:
                    await self.refresh_from_ha()
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
            if "is_on" in settings:
                is_on = bool(settings["is_on"])
            else:
                is_on = False
                ha = await asyncio.to_thread(self.read_ha_state)
                if ha is not None:
                    is_on = ha
            await self.set_state(context, is_on)
            if self._poll_task is None:
                self._poll_task = asyncio.create_task(self.poll_loop())
        elif event == "willDisappear" and context:
            self.contexts.pop(context, None)
        elif event == "keyDown" and context:
            current = self.contexts.get(context, False)
            # Optimistic flip so the key reacts immediately.
            await self.set_state(context, not current)
            await asyncio.to_thread(self.run_toggle)
            await self.send({"event": "setSettings", "context": context, "payload": {"is_on": not current}})

            async def confirm() -> None:
                await asyncio.sleep(1.5)
                ha = await asyncio.to_thread(self.read_ha_state)
                if ha is not None:
                    await self.set_state(context, ha)
                    await self.send(
                        {"event": "setSettings", "context": context, "payload": {"is_on": ha}}
                    )

            asyncio.create_task(confirm())
        elif event == "didReceiveSettings" and context:
            settings = (msg.get("payload") or {}).get("settings") or {}
            if "is_on" in settings:
                await self.set_state(context, bool(settings["is_on"]))

    async def run(self) -> None:
        uri = f"ws://127.0.0.1:{self.args.port}"
        async with websockets.connect(uri, max_size=None) as ws:
            self.ws = ws
            await self.send(
                {"event": self.args.register_event, "uuid": self.args.plugin_uuid}
            )
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

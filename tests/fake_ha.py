"""Minimal HA + Velair stand-in for blackbox apply tests."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse


TOKEN = "test-token"
CLIMATES = [
    "climate.hc1",
    "climate.hc2",
    "climate.hc3",
    "climate.hc4",
    "climate.heating_eh1",
    "climate.heating_eh2",
    "climate.hvac",
    "climate.pool",
]
HVAC_PRESETS = {"away", "home", "high", "fireplace"}
DAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def snap_temp(value: float, minimum: float, maximum: float, step: float) -> float:
    """Clamp and snap a target to a minimum-anchored grid.

    Args:
        value: Desired temperature.
        minimum: Lower bound.
        maximum: Upper bound.
        step: Grid step.
    Returns:
        Snapped temperature.
    """
    bounded = max(minimum, min(maximum, float(value)))
    count = math.floor(((bounded - minimum) / step) + 0.5 + 1e-9)
    last = minimum + math.floor(((maximum - minimum) / step) + 1e-9) * step
    return round(max(minimum, min(last, minimum + count * step)), 6)


class FakeHaState:
    """In-memory HA/Velair state for the fake server.

    Attributes:
        zones: entity -> day -> blocks.
        steps: entity -> target_temp_step.
        revision: configuration revision.
        automations: id -> config.
        writes: mutating call log.
        turn_off_calls: switch entities turned off.
    """

    def __init__(self) -> None:
        self.zones: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
            eid: {day: [] for day in DAYS} for eid in CLIMATES
        }
        self.steps: Dict[str, float] = {eid: 1.0 for eid in CLIMATES}
        self.revision = 0
        self.automations: Dict[str, Dict[str, Any]] = {
            "pool_gate_velair": {
                "id": "pool_gate_velair",
                "alias": "Pool heat only if outdoor at least 5 C",
                "mode": "single",
                "triggers": [
                    {"trigger": "state", "entity_id": "climate.pool"},
                    {
                        "trigger": "numeric_state",
                        "entity_id": "sensor.outdoor_temperature",
                        "below": 5,
                    },
                ],
                "conditions": [],
                "actions": [],
            },
            "1760162526412": {
                "id": "1760162526412",
                "alias": "tuv_charge",
                "mode": "single",
                "triggers": [],
                "conditions": [],
                "actions": [{"action": "switch.turn_on"}],
            },
        }
        self.writes: List[str] = []
        self.turn_off_calls: List[str] = []

    def schedule_payload(self) -> Dict[str, Any]:
        """Build velair/get_schedule result.

        Returns:
            Schedule snapshot.
        """
        zones = {}
        for eid, days in self.zones.items():
            zones[eid] = {
                "enabled": True,
                "schedule": {day: list(blocks) for day, blocks in days.items()},
                "day_layout_key": "individual",
            }
        return {
            "configuration_revision": self.revision,
            "configured_entities": list(CLIMATES),
            "room_groups": {},
            "control_units": list(CLIMATES),
            "global": {"mode": "paused"},
            "zones": zones,
            "settings": {
                "min_temperature": 5.0,
                "max_temperature": 35.0,
            },
        }

    def snap_blocks(self, entity_id: str, blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Snap temperatures for one entity.

        Args:
            entity_id: Climate id.
            blocks: Incoming blocks.
        Returns:
            Stored blocks.
        """
        step = self.steps.get(entity_id, 1.0)
        out = []
        for block in blocks:
            item = dict(block)
            if item.get("temperature") is not None:
                item["temperature"] = snap_temp(float(item["temperature"]), 5.0, 35.0, step)
            out.append(item)
        return out


class FakeHaServer:
    """Threading HTTP + websocket fake HA.

    Attributes:
        state: Shared FakeHaState.
        host: Bind host.
        port: Bound port after start.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.state = FakeHaState()
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        """Base HTTP URL.

        Returns:
            http://host:port
        """
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        """Start the server thread."""
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            """Serve REST and upgrade websockets."""

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _auth_ok(self) -> bool:
                auth = self.headers.get("Authorization", "")
                return auth == f"Bearer {TOKEN}"

            def _json(self, code: int, body: Any) -> None:
                raw = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if raw:
                    self.wfile.write(raw)

            def do_GET(self) -> None:
                if self.headers.get("Upgrade", "").lower() == "websocket":
                    self._websocket()
                    return
                if not self._auth_ok():
                    self._json(401, {"message": "Unauthorized"})
                    return
                path = urlparse(self.path).path
                if path in ("/api/", "/api"):
                    self._json(200, {"message": "API running."})
                    return
                if path == "/api/config":
                    self._json(200, {"version": "2026.11.1", "location_name": "fake"})
                    return
                if path == "/api/services":
                    self._json(
                        200,
                        [
                            {
                                "domain": "velair",
                                "services": {
                                    "apply_schedule": {},
                                    "set_daily_schedule": {},
                                    "activate_profile": {},
                                },
                            },
                            {
                                "domain": "switch",
                                "services": {"turn_on": {}, "turn_off": {}},
                            },
                        ],
                    )
                    return
                if path.startswith("/api/states/"):
                    entity_id = path[len("/api/states/") :]
                    if entity_id in CLIMATES:
                        attrs: Dict[str, Any] = {
                            "min_temp": 5.0,
                            "max_temp": 35.0,
                            "hvac_modes": ["heat", "off"],
                        }
                        if entity_id == "climate.hvac":
                            attrs = {
                                "min_temp": 10,
                                "max_temp": 30,
                                "hvac_modes": ["off", "fan_only"],
                                "preset_modes": sorted(HVAC_PRESETS),
                                "target_temp_step": 0.5,
                            }
                        self._json(
                            200,
                            {
                                "entity_id": entity_id,
                                "state": "heat" if entity_id != "climate.hvac" else "fan_only",
                                "attributes": attrs,
                            },
                        )
                        return
                    if entity_id.startswith("switch."):
                        self.send_response(404)
                        self.end_headers()
                        return
                    self.send_response(404)
                    self.end_headers()
                    return
                if path.startswith("/api/config/automation/config/"):
                    aid = path.rsplit("/", 1)[-1]
                    conf = state.automations.get(aid)
                    if conf is None:
                        self.send_response(404)
                        self.end_headers()
                        return
                    self._json(200, conf)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:
                if not self._auth_ok():
                    self._json(401, {"message": "Unauthorized"})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(body.decode() or "{}")
                except json.JSONDecodeError:
                    self._json(400, {"message": "bad json"})
                    return
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                if path.startswith("/api/config/automation/config/"):
                    aid = path.rsplit("/", 1)[-1]
                    if "trigger" in payload and "triggers" in payload:
                        self._json(
                            400,
                            {
                                "message": (
                                    "Message malformed: Cannot specify both "
                                    "'trigger' and 'triggers'. Please use 'triggers' only."
                                )
                            },
                        )
                        return
                    state.writes.append(f"automation {aid}")
                    conf = dict(payload)
                    conf["id"] = aid
                    state.automations[aid] = conf
                    self._json(200, {"result": "ok"})
                    return

                if path.startswith("/api/services/"):
                    parts = path.strip("/").split("/")
                    # api services domain service
                    if len(parts) < 4:
                        self._json(404, {"message": "not found"})
                        return
                    domain, service = parts[2], parts[3]
                    if qs.get("return_response"):
                        self._json(
                            400,
                            {
                                "message": (
                                    "Service does not support responses. "
                                    "Remove return_response from request."
                                )
                            },
                        )
                        return
                    if domain == "switch" and service == "turn_off":
                        eid = payload.get("entity_id")
                        state.turn_off_calls.append(str(eid))
                        state.writes.append(f"switch.turn_off {eid}")
                        self._json(200, [])
                        return
                    if domain == "velair" and service == "set_daily_schedule":
                        eid = str(payload.get("entity_id"))
                        weekday = str(payload.get("weekday"))
                        blocks = list(payload.get("blocks") or [])
                        for block in blocks:
                            if eid == "climate.hvac" and block.get("preset_mode") == "boost":
                                self._json(
                                    500,
                                    {
                                        "message": (
                                            "Configuration is invalid for one or more "
                                            "destinations: climate.hvac: Options-only "
                                            "schedule block has no supported climate options"
                                        )
                                    },
                                )
                                return
                            if (
                                eid == "climate.hvac"
                                and block.get("preset_mode")
                                and block["preset_mode"] not in HVAC_PRESETS
                                and block.get("action") == "set_climate_options"
                            ):
                                self._json(
                                    500,
                                    {
                                        "message": (
                                            "Configuration is invalid for one or more "
                                            "destinations: climate.hvac: Options-only "
                                            "schedule block has no supported climate options"
                                        )
                                    },
                                )
                                return
                        stored = state.snap_blocks(eid, blocks)
                        state.zones.setdefault(eid, {day: [] for day in DAYS})
                        state.zones[eid][weekday] = stored
                        state.revision += 1
                        state.writes.append(f"set_daily_schedule {eid} {weekday}")
                        self._json(200, [])
                        return
                    self._json(400, {"message": f"unknown service {domain}.{service}"})
                    return
                self.send_response(404)
                self.end_headers()

            def _websocket(self) -> None:
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(
                    hashlib.sha1(
                        (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                    ).digest()
                ).decode()
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                sock = self.connection
                self._ws_send(sock, {"type": "auth_required", "ha_version": "2026.11.1"})
                while True:
                    msg = self._ws_recv(sock)
                    if msg is None:
                        return
                    if msg.get("type") == "auth":
                        if msg.get("access_token") != TOKEN:
                            self._ws_send(sock, {"type": "auth_invalid", "message": "bad"})
                            return
                        self._ws_send(
                            sock, {"type": "auth_ok", "ha_version": "2026.11.1"}
                        )
                        continue
                    msg_id = msg.get("id")
                    mtype = msg.get("type")
                    if mtype == "velair/get_schedule":
                        self._ws_send(
                            sock,
                            {
                                "id": msg_id,
                                "type": "result",
                                "success": True,
                                "result": state.schedule_payload(),
                            },
                        )
                        continue
                    if mtype == "velair/update_zone_target_temp_step":
                        eid = str(msg.get("entity_id"))
                        step = float(msg.get("target_temp_step"))
                        state.steps[eid] = step
                        state.revision += 1
                        state.writes.append(f"temp_step {eid}={step}")
                        self._ws_send(
                            sock,
                            {
                                "id": msg_id,
                                "type": "result",
                                "success": True,
                                "result": state.schedule_payload(),
                            },
                        )
                        continue
                    self._ws_send(
                        sock,
                        {
                            "id": msg_id,
                            "type": "result",
                            "success": False,
                            "error": {"code": "unknown_command", "message": str(mtype)},
                        },
                    )

            def _ws_send(self, sock: socket.socket, obj: Dict[str, Any]) -> None:
                payload = json.dumps(obj).encode()
                header = bytearray([0x81])
                ln = len(payload)
                if ln < 126:
                    header.append(ln)
                elif ln < 65536:
                    header.append(126)
                    header.extend(ln.to_bytes(2, "big"))
                else:
                    header.append(127)
                    header.extend(ln.to_bytes(8, "big"))
                sock.sendall(bytes(header) + payload)

            def _ws_recv(self, sock: socket.socket) -> Optional[Dict[str, Any]]:
                def read_exact(n: int) -> bytes:
                    data = b""
                    while len(data) < n:
                        chunk = sock.recv(n - len(data))
                        if not chunk:
                            raise ConnectionError("closed")
                        data += chunk
                    return data

                try:
                    hdr = read_exact(2)
                except ConnectionError:
                    return None
                opcode = hdr[0] & 0x0F
                masked = hdr[1] & 0x80
                ln = hdr[1] & 0x7F
                if ln == 126:
                    ln = int.from_bytes(read_exact(2), "big")
                elif ln == 127:
                    ln = int.from_bytes(read_exact(8), "big")
                mask = read_exact(4) if masked else b""
                data = read_exact(ln)
                if masked:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                if opcode == 8:
                    return None
                if opcode != 1:
                    return {}
                return json.loads(data.decode())

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the server thread."""
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

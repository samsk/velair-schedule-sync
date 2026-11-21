"""Home Assistant REST and WebSocket client."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import socket
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse


@dataclass
class HaClient:
    """Minimal HA API client.

    Attributes:
        url: Base HA URL without trailing slash.
        token: Long-lived access token.
        writes: Count of mutating calls (for dry-run asserts).
    """

    url: str
    token: str
    writes: int = 0
    allow_writes: bool = True
    _write_log: List[str] = field(default_factory=list)

    def _headers(self) -> Dict[str, str]:
        """Auth headers.

        Returns:
            Header map.
        """
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        *,
        write: bool = False,
    ) -> Any:
        """HTTP request to HA.

        Args:
            method: HTTP method.
            path: API path starting with /api.
            body: Optional JSON body.
            write: Mark as mutating call.
        Returns:
            Parsed JSON or None.
        """
        if write:
            if not self.allow_writes:
                raise RuntimeError(f"write blocked in dry-run: {method} {path}")
            self.writes += 1
            self._write_log.append(f"{method} {path}")
        url = self.url.rstrip("/") + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
                if not raw:
                    return None
                return json.loads(raw.decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"HA {method} {path} -> {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"HA {method} {path} failed: {exc}") from exc

    def get(self, path: str) -> Any:
        """GET helper.

        Args:
            path: API path.
        Returns:
            JSON body.
        """
        return self.request("GET", path)

    def post(self, path: str, body: Optional[Dict[str, Any]] = None, *, write: bool = True) -> Any:
        """POST helper.

        Args:
            path: API path.
            body: JSON body.
            write: Mutating flag.
        Returns:
            JSON body.
        """
        return self.request("POST", path, body, write=write)

    def call_service(
        self,
        domain: str,
        service: str,
        data: Optional[Dict[str, Any]] = None,
        *,
        write: bool = True,
        return_response: bool = False,
    ) -> Any:
        """Call an HA service.

        Args:
            domain: Service domain.
            service: Service name.
            data: Service data.
            write: Mutating flag.
            return_response: Request service response payload.
        Returns:
            Service response.
        """
        path = f"/api/services/{domain}/{service}"
        if return_response:
            path += "?return_response"
        return self.post(path, data or {}, write=write)

    def get_state(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """Fetch one entity state.

        Args:
            entity_id: Entity id.
        Returns:
            State dict or None if missing.
        """
        try:
            return self.get(f"/api/states/{entity_id}")
        except RuntimeError as exc:
            if "404" in str(exc):
                return None
            raise

    def history(
        self,
        entity_id: str,
        start: str,
        end: str,
        *,
        significant_changes_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch recorder history for one entity.

        Args:
            entity_id: Entity id.
            start: ISO start timestamp.
            end: ISO end timestamp.
            significant_changes_only: Pass-through HA query flag.
        Returns:
            Flat list of history state dicts (empty when absent).
        """
        path = (
            f"/api/history/period/{quote(start, safe='')}"
            f"?filter_entity_id={quote(entity_id, safe='')}"
            f"&end_time={quote(end, safe='')}"
        )
        if significant_changes_only:
            path += "&significant_changes_only=1"
        raw = self.get(path)
        if not isinstance(raw, list) or not raw:
            return []
        first = raw[0]
        if isinstance(first, list):
            return [row for row in first if isinstance(row, dict)]
        return [row for row in raw if isinstance(row, dict)]

    def get_services(self) -> Dict[str, Any]:
        """List available services.

        Returns:
            Services map.
        """
        return self.get("/api/services")

    def get_config(self) -> Dict[str, Any]:
        """HA root config/version.

        Returns:
            Config dict.
        """
        return self.get("/api/config")

    def get_automation_config(self, automation_id: str) -> Any:
        """Fetch automation config by id.

        Args:
            automation_id: Automation unique id.
        Returns:
            Config JSON.
        """
        return self.get(f"/api/config/automation/config/{automation_id}")

    def set_automation_config(self, automation_id: str, config: Dict[str, Any]) -> Any:
        """Write automation config.

        Args:
            automation_id: Automation unique id.
            config: Full automation config.
        Returns:
            Response.
        """
        return self.request(
            "POST",
            f"/api/config/automation/config/{automation_id}",
            config,
            write=True,
        )

    def find_automation_entity(self, automation_id: str) -> Optional[str]:
        """Find automation entity_id by unique id attribute.

        Args:
            automation_id: Automation unique id.
        Returns:
            Entity id or None.
        """
        states = self.get("/api/states")
        if not isinstance(states, list):
            return None
        for st in states:
            if not isinstance(st, dict):
                continue
            eid = str(st.get("entity_id") or "")
            if not eid.startswith("automation."):
                continue
            attrs = st.get("attributes") or {}
            if str(attrs.get("id") or "") == str(automation_id):
                return eid
        return None

    def list_labels(self) -> List[Dict[str, Any]]:
        """List HA label registry entries.

        Returns:
            Label dicts.
        """
        result = self.ws_command("config/label_registry/list")
        return list(result or [])

    def create_label(self, name: str, *, icon: Optional[str] = None) -> Dict[str, Any]:
        """Create one HA label.

        Args:
            name: Label display name (also becomes label_id when slug-safe).
            icon: Optional mdi icon.
        Returns:
            Created label dict.
        """
        fields: Dict[str, Any] = {"name": name}
        if icon:
            fields["icon"] = icon
        return self.ws_command("config/label_registry/create", fields, write=True) or {}

    def get_entity_labels(self, entity_id: str) -> List[str]:
        """Read labels for one entity.

        Args:
            entity_id: Entity id.
        Returns:
            Label ids.
        """
        entry = self.ws_command(
            "config/entity_registry/get", {"entity_id": entity_id}
        )
        if not isinstance(entry, dict):
            return []
        return [str(x) for x in (entry.get("labels") or [])]

    def set_entity_labels(self, entity_id: str, labels: List[str]) -> Any:
        """Replace entity labels.

        Args:
            entity_id: Entity id.
            labels: Full label id list.
        Returns:
            Registry update result.
        """
        return self.ws_command(
            "config/entity_registry/update",
            {"entity_id": entity_id, "labels": labels},
            write=True,
        )

    async def ws_statistics(
        self,
        statistic_ids: List[str],
        start: str,
        end: Optional[str] = None,
        period: str = "hour",
    ) -> Dict[str, Any]:
        """Fetch recorder statistics over websocket.

        Args:
            statistic_ids: Statistic entity ids.
            start: ISO start.
            end: Optional ISO end.
            period: Bucket period.
        Returns:
            Statistics map.
        """
        import websockets

        base = self.url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
        uri = base + "/api/websocket"
        ssl_ctx = ssl.create_default_context() if uri.startswith("wss://") else None
        async with websockets.connect(uri, ssl=ssl_ctx) as ws:
            hello = json.loads(await ws.recv())
            if hello.get("type") != "auth_required":
                raise RuntimeError(f"unexpected ws hello: {hello}")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"ws auth failed: {auth}")
            msg_id = 1
            payload: Dict[str, Any] = {
                "id": msg_id,
                "type": "recorder/statistics_during_period",
                "start_time": start,
                "statistic_ids": statistic_ids,
                "period": period,
                "types": ["state", "sum", "change", "mean"],
            }
            if end:
                payload["end_time"] = end
            await ws.send(json.dumps(payload))
            while True:
                raw = json.loads(await ws.recv())
                if raw.get("id") == msg_id:
                    if not raw.get("success"):
                        raise RuntimeError(f"statistics failed: {raw}")
                    return raw.get("result") or {}

    def statistics(
        self,
        statistic_ids: List[str],
        start: str,
        end: Optional[str] = None,
        period: str = "hour",
    ) -> Dict[str, Any]:
        """Sync wrapper for ws statistics.

        Args:
            statistic_ids: Ids.
            start: ISO start.
            end: ISO end.
            period: Period.
        Returns:
            Result map.
        """
        return asyncio.run(self.ws_statistics(statistic_ids, start, end, period))

    def ws_command(self, msg_type: str, fields: Optional[Dict[str, Any]] = None, *, write: bool = False) -> Any:
        """Send one HA websocket command.

        Args:
            msg_type: Command type, e.g. velair/get_schedule.
            fields: Extra command fields.
            write: Count as a mutating call.
        Returns:
            Result payload.
        """
        if write:
            if not self.allow_writes:
                raise RuntimeError(f"write blocked in dry-run: {msg_type}")
            self.writes += 1
            self._write_log.append(f"WS {msg_type}")
        return _ws_command(self.url, self.token, msg_type, fields or {})


def _ws_command(url: str, token: str, msg_type: str, fields: Dict[str, Any]) -> Any:
    """Authenticated HA websocket command over stdlib sockets.

    Args:
        url: HA base URL.
        token: Access token.
        msg_type: Command type.
        fields: Extra command fields.
    Returns:
        Result object.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = (parsed.path.rstrip("/") or "") + "/api/websocket"
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    raw = socket.create_connection((host, port), timeout=20)
    sock: socket.socket = raw
    if parsed.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    try:
        sock.sendall(request.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("websocket handshake closed")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status:
            raise RuntimeError(f"websocket handshake failed: {status}")

        def recv_exact(n: int) -> bytes:
            data = b""
            while len(data) < n:
                part = sock.recv(n - len(data))
                if not part:
                    raise RuntimeError("websocket closed")
                data += part
            return data

        def recv_json() -> Dict[str, Any]:
            parts: List[bytes] = []
            while True:
                hdr = recv_exact(2)
                opcode = hdr[0] & 0x0F
                fin = hdr[0] & 0x80
                ln = hdr[1] & 0x7F
                if ln == 126:
                    ln = int.from_bytes(recv_exact(2), "big")
                elif ln == 127:
                    ln = int.from_bytes(recv_exact(8), "big")
                data = recv_exact(ln)
                if opcode == 8:
                    raise RuntimeError(f"websocket closed: {data.decode(errors='replace')}")
                if opcode == 9:
                    continue
                if opcode in (0, 1):
                    parts.append(data)
                    if fin:
                        return json.loads(b"".join(parts).decode())

        def send_text(text: str) -> None:
            payload = text.encode()
            mask = os.urandom(4)
            ln = len(payload)
            header = bytearray([0x81])
            if ln < 126:
                header.append(0x80 | ln)
            elif ln < 65536:
                header.append(0x80 | 126)
                header.extend(ln.to_bytes(2, "big"))
            else:
                header.append(0x80 | 127)
                header.extend(ln.to_bytes(8, "big"))
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            sock.sendall(bytes(header) + mask + masked)

        hello = recv_json()
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected ws hello: {hello}")
        send_text(json.dumps({"type": "auth", "access_token": token}))
        auth = recv_json()
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"ws auth failed: {auth.get('type')}")
        send_text(json.dumps({"id": 1, "type": msg_type, **fields}))
        reply = recv_json()
        if not reply.get("success"):
            raise RuntimeError(f"{msg_type} failed: {reply.get('error') or reply}")
        return reply.get("result")
    finally:
        sock.close()


def load_connection(
    url_flag: Optional[str] = None,
    token_flag: Optional[str] = None,
    env_dir: Optional[Path] = None,
) -> HaClient:
    """Resolve HA URL/token from flags, env, .env, .ha_token.

    Args:
        url_flag: CLI --url.
        token_flag: CLI --token.
        env_dir: Directory containing .env.
    Returns:
        Configured HaClient.
    """
    env_dir = env_dir or Path.cwd()
    file_env: Dict[str, str] = {}
    env_path = env_dir / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            file_env[k.strip()] = v.strip().strip('"').strip("'")

    legacy = env_dir / ".ha_token"
    legacy_token = legacy.read_text().strip() if legacy.exists() else None

    url = url_flag or os.environ.get("HA_URL") or file_env.get("HA_URL")
    token = (
        token_flag
        or os.environ.get("HA_TOKEN")
        or file_env.get("HA_TOKEN")
        or legacy_token
    )
    if not url:
        raise SystemExit("HA_URL missing (flag, env, or .env)")
    if not token:
        raise SystemExit("HA_TOKEN missing (flag, env, .env, or .ha_token)")
    return HaClient(url=url.rstrip("/"), token=token)

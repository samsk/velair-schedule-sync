"""Home Assistant REST and WebSocket client."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        return self.get("/api/")

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

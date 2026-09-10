"""Minimal Home Assistant REST + WebSocket client (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct
import urllib.error
import urllib.request
from urllib.parse import urlparse

from config import Config


class WebSocket:
    """Raw RFC6455 client socket: connect, handshake, framed JSON in/out."""

    def __init__(self, url: str, timeout: float = 15.0):
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("WebSocket URL must begin with ws:// or wss://")
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/api/websocket"
        if parsed.query:
            path += "?" + parsed.query

        raw = socket.create_connection((host, port), timeout=timeout)
        if parsed.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw

        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_until(b"\r\n\r\n")
        header, self.buffer = response.split(b"\r\n\r\n", 1)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket upgrade failed: {header.decode(errors='replace')}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        if expected.lower() not in header.lower():
            raise RuntimeError("WebSocket server returned an invalid handshake")

    def _read_until(self, marker: bytes) -> bytes:
        data = b""
        while marker not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket connection closed during handshake")
            data += chunk
        return data

    def _read_exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self.buffer)))
            if not chunk:
                raise ConnectionError("WebSocket connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def send_json(self, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        mask = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x81])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def receive_json(self) -> dict:
        fragments = bytearray()
        while True:
            first, second = self._read_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else b""
            payload = self._read_exact(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("WebSocket server closed the connection")
            if opcode == 0x9:
                self._send_control(0xA, payload)
                continue
            if opcode in {0x0, 0x1}:
                fragments.extend(payload)
                if final:
                    return json.loads(fragments.decode("utf-8"))

    def _send_control(self, opcode: int, payload: bytes) -> None:
        mask = secrets.token_bytes(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask + masked)

    def close(self) -> None:
        try:
            self._send_control(0x8, b"")
        finally:
            self.sock.close()


class HAClient:
    """Authenticated REST + WebSocket access to one Home Assistant instance."""

    def __init__(self, config: Config):
        self.config = config
        self._ws: WebSocket | None = None
        self._next_id = 1

    # -- REST -----------------------------------------------------------
    def _rest_get(self, path: str) -> object:
        request = urllib.request.Request(
            f"{self.config.rest_base_url}{path}",
            headers={
                "Authorization": f"Bearer {self.config.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"HA REST request failed ({error.code}): {path}") from error

    def get_states(self) -> list[dict]:
        return self._rest_get("/api/states")

    # -- WebSocket --------------------------------------------------------
    def _connect(self) -> WebSocket:
        if self._ws is not None:
            return self._ws
        ws = WebSocket(self.config.ws_url)
        hello = ws.receive_json()
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected authentication greeting: {hello}")
        ws.send_json({"type": "auth", "access_token": self.config.token})
        auth = ws.receive_json()
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant authentication failed: {auth}")
        self._ws = ws
        return ws

    def call(self, type_: str, **kwargs) -> dict:
        ws = self._connect()
        message_id = self._next_id
        self._next_id += 1
        ws.send_json({"id": message_id, "type": type_, **kwargs})
        while True:
            response = ws.receive_json()
            if response.get("id") == message_id:
                if response.get("type") == "result" and not response.get("success", True):
                    raise RuntimeError(f"HA WS call {type_!r} failed: {response.get('error')}")
                return response

    def get_entity_registry(self) -> list[dict]:
        return self.call("config/entity_registry/list").get("result", [])

    def get_device_registry(self) -> list[dict]:
        return self.call("config/device_registry/list").get("result", [])

    def get_config_entries(self) -> list[dict]:
        """Every configured integration instance and whether it's currently loaded."""
        return self.call("config_entries/get").get("result", [])

    def get_entity(self, entity_id: str) -> dict | None:
        """Full registry record for one entity, or None if it doesn't exist."""
        try:
            return self.call("config/entity_registry/get", entity_id=entity_id).get("result")
        except RuntimeError:
            return None

    def update_entity(self, entity_id: str, **changes) -> dict:
        return self.call("config/entity_registry/update", entity_id=entity_id, **changes)

    def remove_entity(self, entity_id: str) -> dict:
        return self.call("config/entity_registry/remove", entity_id=entity_id)

    def reload_config_entry(self, entry_id: str) -> dict:
        """Ask an integration instance to re-setup, which re-registers its entities."""
        return self.call("config_entries/reload", entry_id=entry_id)

    def get_backup_agents(self) -> list[dict]:
        return self.call("backup/agents/info").get("result", {}).get("agents", [])

    def generate_backup(self, name: str) -> dict:
        agent_ids = [agent["agent_id"] for agent in self.get_backup_agents()]
        if not agent_ids:
            raise RuntimeError("No backup agents configured on this Home Assistant instance")
        return self.call(
            "backup/generate",
            agent_ids=agent_ids,
            name=name,
            include_homeassistant=True,
            include_database=True,
            include_folders=[],
            include_addons=[],
        ).get("result", {})

    def close(self) -> None:
        if self._ws is not None:
            self._ws.close()
            self._ws = None

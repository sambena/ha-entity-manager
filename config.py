"""Load ha-entity-manager configuration from config.json (+ CLI overrides)."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "config.json"
EXAMPLE_CONFIG_PATH = HERE / "config.example.json"


@dataclass
class Config:
    ha_host: str
    ha_port: int
    ha_use_ssl: bool
    yaml_root: Path
    inactive_days: int
    server_port: int
    token: str

    @property
    def rest_base_url(self) -> str:
        scheme = "https" if self.ha_use_ssl else "http"
        return f"{scheme}://{self.ha_host}:{self.ha_port}"

    @property
    def ws_url(self) -> str:
        scheme = "wss" if self.ha_use_ssl else "ws"
        return f"{scheme}://{self.ha_host}:{self.ha_port}/api/websocket"


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_config(argv: list[str] | None = None) -> Config:
    parser = argparse.ArgumentParser(description="ha-entity-manager configuration")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--host", help="Override ha_host")
    parser.add_argument("--port", type=int, help="Override ha_port")
    parser.add_argument("--root", type=Path, help="Override yaml_root")
    parser.add_argument("--server-port", type=int, help="Override server_port")
    parser.add_argument("--token", help="Override HOME_ASSISTANT_TOKEN")
    args = parser.parse_args(argv)

    if args.config.exists():
        data = _read_json(args.config)
    elif EXAMPLE_CONFIG_PATH.exists():
        print(
            f"WARNING: {args.config} not found, falling back to "
            f"{EXAMPLE_CONFIG_PATH.name} defaults. Copy it to config.json to customize."
        )
        data = _read_json(EXAMPLE_CONFIG_PATH)
    else:
        parser.error(
            f"No config file found at {args.config} and no {EXAMPLE_CONFIG_PATH.name} to fall back to."
        )

    token = args.token or os.environ.get("HOME_ASSISTANT_TOKEN")
    if not token:
        parser.error("HOME_ASSISTANT_TOKEN env var (or --token) is required")

    return Config(
        ha_host=args.host or data["ha_host"],
        ha_port=args.port or int(data["ha_port"]),
        ha_use_ssl=bool(data.get("ha_use_ssl", False)),
        yaml_root=(args.root or Path(data["yaml_root"])).resolve(),
        inactive_days=int(data.get("inactive_days", 30)),
        server_port=args.server_port or int(data.get("server_port", 8765)),
        token=token,
    )

"""Local log of entities removed through this tool, so they can be reviewed/restored.

Home Assistant's registry API has no "undelete" -- removing an entity just deletes its
registry row. This store keeps a snapshot of that row (name, area, config_entry_id,
etc.) from just before deletion, so a later restore attempt can reload the owning
integration and reapply the saved customizations on top of whatever it re-registers.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

STORE_PATH = Path(__file__).resolve().parent / "data" / "deleted_entities.json"
_lock = threading.Lock()


def _load() -> list[dict]:
    if not STORE_PATH.exists():
        return []
    with STORE_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def _save(records: list[dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STORE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(records, handle, indent=2)


def list_deleted() -> list[dict]:
    with _lock:
        return _load()


def record_deleted(record: dict) -> None:
    with _lock:
        records = _load()
        records = [r for r in records if r["entity_id"] != record["entity_id"]]
        records.append(record)
        _save(records)


def remove_record(entity_id: str) -> None:
    with _lock:
        records = _load()
        records = [r for r in records if r["entity_id"] != entity_id]
        _save(records)


def get_record(entity_id: str) -> dict | None:
    with _lock:
        for record in _load():
            if record["entity_id"] == entity_id:
                return record
    return None

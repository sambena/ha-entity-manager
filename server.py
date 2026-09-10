"""Local dashboard server for reviewing HA entity duplicates/inactive/drift."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import deleted_store
from analysis import run_scan
from config import Config, load_config
from ha_client import HAClient

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Registry fields worth reapplying after a restore -- the ones a user is likely to
# have customized, as opposed to integration-computed fields like unique_id/platform.
RESTORABLE_FIELDS = ("name", "area_id", "disabled_by", "hidden_by", "icon", "labels")


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Not JSON serializable: {value!r}")


class Handler(BaseHTTPRequestHandler):
    config: Config

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        print(f"{self.address_string()} - {format % args}")

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        body = path.read_bytes()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        if parsed.path == "/api/scan":
            self._handle_scan()
        elif parsed.path == "/api/deleted":
            self._send_json(200, {"deleted": deleted_store.list_deleted()})
        elif parsed.path in {"/", "/index.html"}:
            self._send_file(STATIC_DIR / "index.html", "text/html")
        elif parsed.path == "/app.js":
            self._send_file(STATIC_DIR / "app.js", "application/javascript")
        elif parsed.path == "/style.css":
            self._send_file(STATIC_DIR / "style.css", "text/css")
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw_body or b"{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if parsed.path == "/api/actions/disable":
            self._handle_action(body, self._do_disable)
        elif parsed.path == "/api/actions/remove":
            self._handle_action(body, self._do_remove)
        elif parsed.path == "/api/actions/restore":
            self._handle_action(body, self._do_restore)
        elif parsed.path == "/api/actions/backup":
            self._handle_backup(body)
        elif parsed.path == "/api/actions/bulk_remove":
            self._handle_bulk_remove(body)
        else:
            self._send_json(404, {"error": "not found"})

    def _handle_scan(self) -> None:
        client = HAClient(self.config)
        try:
            result = run_scan(client, self.config.yaml_root, self.config.inactive_days)
            self._send_json(200, asdict(result))
        except Exception as error:
            self._send_json(500, {"error": str(error)})
        finally:
            client.close()

    def _handle_action(self, body: dict, action) -> None:
        entity_id = body.get("entity_id")
        if not entity_id:
            self._send_json(400, {"error": "entity_id is required"})
            return
        if not body.get("confirm"):
            self._send_json(400, {"error": "confirm: true is required to apply this action"})
            return

        client = HAClient(self.config)
        try:
            result = action(client, entity_id)
            self._send_json(200, {"ok": True, "entity_id": entity_id, **result})
        except Exception as error:
            self._send_json(500, {"error": str(error)})
        finally:
            client.close()

    def _handle_backup(self, body: dict) -> None:
        if not body.get("confirm"):
            self._send_json(400, {"error": "confirm: true is required to start a backup"})
            return
        client = HAClient(self.config)
        try:
            name = body.get("name") or f"ha-entity-manager {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            result = client.generate_backup(name)
            self._send_json(200, {"ok": True, "name": name, "result": result})
        except Exception as error:
            self._send_json(500, {"error": str(error)})
        finally:
            client.close()

    def _handle_bulk_remove(self, body: dict) -> None:
        entity_ids = body.get("entity_ids")
        if not entity_ids or not isinstance(entity_ids, list):
            self._send_json(400, {"error": "entity_ids (non-empty list) is required"})
            return
        if not body.get("confirm"):
            self._send_json(400, {"error": "confirm: true is required to apply this action"})
            return

        client = HAClient(self.config)
        results = []
        try:
            for entity_id in entity_ids:
                try:
                    outcome = self._do_remove(client, entity_id)
                    results.append({"entity_id": entity_id, "ok": True, **outcome})
                except Exception as error:
                    # One failure shouldn't abort the rest of the batch -- report it
                    # per-entity so the caller sees exactly what succeeded/failed.
                    results.append({"entity_id": entity_id, "ok": False, "error": str(error)})
            removed = sum(1 for r in results if r["ok"])
            self._send_json(200, {"ok": True, "removed_count": removed, "failed_count": len(results) - removed, "results": results})
        except Exception as error:
            self._send_json(500, {"error": str(error)})
        finally:
            client.close()

    @staticmethod
    def _do_disable(client: HAClient, entity_id: str) -> dict:
        result = client.update_entity(entity_id, disabled_by="user")
        return {"result": result}

    @staticmethod
    def _do_remove(client: HAClient, entity_id: str) -> dict:
        # Snapshot the full registry record before deleting it -- HA's API has no
        # "undelete", so this snapshot is the only way a later restore can reapply
        # the entity's customizations once the owning integration re-registers it.
        snapshot = client.get_entity(entity_id)
        result = client.remove_entity(entity_id)
        if snapshot is not None:
            deleted_store.record_deleted(
                {
                    "entity_id": entity_id,
                    "removed_at": datetime.now(timezone.utc).isoformat(),
                    "registry_snapshot": snapshot,
                }
            )
        return {"result": result, "snapshotted": snapshot is not None}

    @staticmethod
    def _do_restore(client: HAClient, entity_id: str) -> dict:
        record = deleted_store.get_record(entity_id)
        if record is None:
            return {"status": "no_record", "message": "No removal snapshot found for this entity."}

        snapshot = record["registry_snapshot"]
        saved_fields = {k: snapshot.get(k) for k in RESTORABLE_FIELDS if snapshot.get(k) not in (None, [], {})}
        # config/entity_registry/update only accepts disabled_by="user" (or clearing it) --
        # "integration"/"config_entry"/"device" are set internally by HA itself and the
        # API rejects them here, so only ever reapply a user-initiated disable.
        if saved_fields.get("disabled_by") != "user":
            saved_fields.pop("disabled_by", None)

        current = client.get_entity(entity_id)
        if current is not None:
            if saved_fields:
                client.update_entity(entity_id, **saved_fields)
            deleted_store.remove_record(entity_id)
            return {
                "status": "restored",
                "message": "Entity already exists in the registry again; reapplied saved customizations.",
                "reapplied": saved_fields,
            }

        config_entry_id = snapshot.get("config_entry_id")
        if not config_entry_id:
            return {
                "status": "manual_required",
                "message": (
                    "This entity wasn't backed by an integration (e.g. a YAML helper or "
                    "template sensor), so Home Assistant's API can't recreate it. "
                    "Re-add it in the corresponding YAML file to restore it."
                ),
                "snapshot": snapshot,
            }

        client.reload_config_entry(config_entry_id)
        time.sleep(3)
        current = client.get_entity(entity_id)
        if current is not None:
            if saved_fields:
                client.update_entity(entity_id, **saved_fields)
            deleted_store.remove_record(entity_id)
            return {
                "status": "restored",
                "message": "Reloaded the owning integration, which re-registered this entity; reapplied saved customizations.",
                "reapplied": saved_fields,
            }
        return {
            "status": "reload_attempted",
            "message": (
                "Reloaded the owning integration but the entity hasn't reappeared yet. "
                "It may need the physical device to report state again, or the integration "
                "may no longer create this entity. Try Re-scan in a bit, or restore again."
            ),
        }


def main() -> int:
    config = load_config()
    Handler.config = config

    server = ThreadingHTTPServer(("127.0.0.1", config.server_port), Handler)
    print(f"ha-entity-manager listening on http://127.0.0.1:{config.server_port}")
    print(f"Target Home Assistant: {config.rest_base_url}")
    print(f"YAML root: {config.yaml_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

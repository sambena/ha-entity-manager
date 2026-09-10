"""Duplicate / inactive / YAML-drift analysis over a live HA registry + config bundle."""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

from ha_client import HAClient


def _load_validate_bundle(yaml_root: Path) -> ModuleType:
    """Import tools/validate_bundle.py from the HA config repo for its YAML helpers."""
    path = yaml_root / "tools" / "validate_bundle.py"
    if not path.exists():
        raise FileNotFoundError(f"validate_bundle.py not found under {yaml_root}/tools")
    spec = importlib.util.spec_from_file_location("validate_bundle", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@dataclass
class EntityContext:
    """Extra signal to judge whether a flagged entity is safe to touch."""

    entity_id: str
    state: str | None
    last_changed: str | None
    used_in: list[str] = field(default_factory=list)
    integration_status: str = "n/a"
    yaml_defined: bool | None = None
    friendly_name: str = ""


@dataclass
class DuplicateGroup:
    device_id: str
    device_name: str
    capability: str
    reason: str
    entities: list[EntityContext] = field(default_factory=list)


@dataclass
class InactiveEntity:
    entity_id: str
    friendly_name: str
    reason: str
    used_in: list[str] = field(default_factory=list)
    integration_status: str = "n/a"
    yaml_defined: bool | None = None


@dataclass
class DriftIssue:
    kind: str
    detail: str
    location: str


@dataclass
class ScanResult:
    duplicates: list[DuplicateGroup]
    inactive: list[InactiveEntity]
    drift: list[DriftIssue]
    good: list[EntityContext]


class YamlBundle:
    """Parsed YAML config tree plus the entity-id references found in it.

    Built once per scan and reused by duplicate/inactive "used in" lookups and by
    the YAML-drift check, instead of re-parsing the whole bundle for each.
    """

    def __init__(self, yaml_root: Path):
        self.yaml_root = yaml_root
        self.module = _load_validate_bundle(yaml_root)
        self.parse_errors: list[DriftIssue] = []
        self.documents: dict[Path, object] = {}
        for path in sorted(yaml_root.rglob("*.yaml")):
            try:
                self.documents[path] = self.module.load_yaml(path)
            except Exception as error:
                self.parse_errors.append(
                    DriftIssue("yaml_parse_error", str(error), str(path.relative_to(yaml_root)))
                )

        self.referenced: dict[str, list[Path]] = {}
        self.unique_ids: dict[str, list[Path]] = {}
        for path, document in self.documents.items():
            for value in self.module.collect_strings(document):
                for entity_id in self.module.ENTITY_PATTERN.findall(value):
                    self.referenced.setdefault(entity_id, []).append(path)
            for unique_id in self.module.collect_key_values(document, "unique_id"):
                if isinstance(unique_id, str):
                    self.unique_ids.setdefault(unique_id, []).append(path)

        # Entities HA can derive directly from YAML (helpers, template sensors,
        # scripts, ...) -- reused both for the "stale reference" drift check and
        # for the yaml_defined() lookup below.
        self.generated_entity_ids: set[str] = self.module.generated_entities(self.yaml_root)

        # automations.yaml blocks key on "id", not "unique_id" (HA treats an
        # automation's id as its unique_id), and its own alias/entity_id can differ
        # from that id -- so this can't be found via generated_entities() or a
        # simple entity_id match, only by comparing the registry's unique_id field.
        self.automation_ids: set[str] = set()
        for path, document in self.documents.items():
            if path.name != "automations.yaml" or not isinstance(document, list):
                continue
            for item in document:
                if isinstance(item, dict) and "id" in item:
                    self.automation_ids.add(str(item["id"]))

    def used_in(self, entity_id: str) -> list[str]:
        paths = self.referenced.get(entity_id, [])
        return sorted({str(path.relative_to(self.yaml_root)) for path in paths})

    # Domains HA lets you hand-author directly in YAML (as opposed to entities
    # created by an integration's config-entry setup).
    _YAML_AUTHORED_DOMAINS = {
        "input_boolean", "input_number", "input_text", "input_select",
        "input_datetime", "alarm_control_panel", "script",
    }

    def yaml_defined(self, entity: dict) -> bool | None:
        """Whether this entity still has a matching definition in the live YAML.

        Returns None when the check doesn't apply (the entity is backed by an
        integration's config entry, not hand-authored YAML) -- only meaningful for
        entities already flagged "n/a (not config-entry backed)" under Integration.
        """
        if entity.get("config_entry_id"):
            return None
        domain = entity["entity_id"].split(".", 1)[0]
        if domain == "automation":
            return entity.get("unique_id") in self.automation_ids
        if domain in self._YAML_AUTHORED_DOMAINS:
            return entity["entity_id"] in self.generated_entity_ids
        return None


def _capability_signature(entity_id: str, state_by_id: dict[str, dict]) -> str:
    domain = entity_id.split(".", 1)[0]
    state = state_by_id.get(entity_id, {})
    attributes = state.get("attributes", {})
    device_class = attributes.get("device_class", "")
    unit = attributes.get("unit_of_measurement", "")
    return f"{domain}: device_class={device_class or '-'}, unit={unit or '-'}"


def _exact_name(entity: dict) -> str:
    """The entity's effective friendly name, normalized for comparison.

    `name` is the user's override in the registry (e.g. renaming "Unnamed Zone Zone"
    to "zone 1" in the HA UI) and takes precedence over `original_name`, the
    integration-supplied default -- otherwise a device whose zones were individually
    renamed would still be flagged as duplicates by their stale original names.

    Registry data shows same-device + same-effective-name is the reliable duplicate
    signal: two entities on the same device with an identical name (e.g. two "Motion"
    binary_sensors on a re-added Frigate camera, or a WLAN's "Clients" sensor
    duplicated when its network got re-added) are true duplicates. Entities that
    merely share a numeric suffix pattern (switch.bryanfamily_port_1..port_5, each
    named "Port 1".."Port 5") are NOT duplicates -- their names are genuinely
    distinct, so no suffix-stripping is done here on purpose.
    """
    entity_id = entity["entity_id"]
    object_id = entity_id.split(".", 1)[1]
    name = entity.get("name") or entity.get("original_name") or object_id
    return name.strip().lower()


def _integration_status(entity: dict, config_entries_by_id: dict[str, dict]) -> str:
    """Whether the integration that owns this entity is currently loaded and enabled.

    Entities without a config_entry_id are YAML-defined or otherwise not backed by a
    config-entry integration (helpers, template sensors, etc.) -- reported as "n/a"
    rather than treated as a red flag.
    """
    entry_id = entity.get("config_entry_id")
    if not entry_id:
        return "n/a (not config-entry backed)"
    entry = config_entries_by_id.get(entry_id)
    if entry is None:
        return "unknown (config entry not found)"
    if entry.get("disabled_by"):
        return f"integration disabled ({entry['disabled_by']})"
    state = entry.get("state", "unknown")
    if state != "loaded":
        return f"integration {state}"
    return "active"


def find_duplicates(
    entity_registry: list[dict],
    device_registry: list[dict],
    states: list[dict],
    config_entries_by_id: dict[str, dict],
    bundle: YamlBundle,
) -> list[DuplicateGroup]:
    device_names = {device["id"]: device.get("name_by_user") or device.get("name") or device["id"] for device in device_registry}
    state_by_id = {state["entity_id"]: state for state in states}

    by_device: dict[str, list[dict]] = {}
    for entity in entity_registry:
        device_id = entity.get("device_id")
        if not device_id:
            continue
        by_device.setdefault(device_id, []).append(entity)

    groups: list[DuplicateGroup] = []
    for device_id, entities in by_device.items():
        # Same device + same domain + identical name is the actual duplicate signal;
        # domain is included so e.g. a "Motion" binary_sensor and a "Motion" switch
        # (detection state vs. its enable/disable toggle) aren't flagged against each other.
        by_key: dict[tuple[str, str], list[dict]] = {}
        for entity in entities:
            domain = entity["entity_id"].split(".", 1)[0]
            by_key.setdefault((domain, _exact_name(entity)), []).append(entity)
        for (domain, name), group_entities in by_key.items():
            if len(group_entities) <= 1:
                continue
            group_entities = sorted(group_entities, key=lambda entity: entity["entity_id"])
            capabilities = {_capability_signature(e["entity_id"], state_by_id) for e in group_entities}
            contexts = []
            for entity in group_entities:
                entity_id = entity["entity_id"]
                state = state_by_id.get(entity_id, {})
                contexts.append(
                    EntityContext(
                        entity_id=entity_id,
                        state=state.get("state"),
                        last_changed=state.get("last_changed"),
                        used_in=bundle.used_in(entity_id),
                        integration_status=_integration_status(entity, config_entries_by_id),
                        yaml_defined=bundle.yaml_defined(entity),
                    )
                )
            groups.append(
                DuplicateGroup(
                    device_id=device_id,
                    device_name=device_names.get(device_id, device_id),
                    capability="; ".join(sorted(capabilities)),
                    reason=f"{len(contexts)}x {domain} entities named '{name}' on this device",
                    entities=contexts,
                )
            )
    return sorted(groups, key=lambda group: (group.device_name, group.reason))


def find_inactive(
    entity_registry: list[dict],
    states: list[dict],
    inactive_days: int,
    config_entries_by_id: dict[str, dict],
    bundle: YamlBundle,
) -> list[InactiveEntity]:
    state_by_id = {state["entity_id"]: state for state in states}
    now = datetime.now(timezone.utc)
    result: list[InactiveEntity] = []

    for entity in entity_registry:
        entity_id = entity["entity_id"]
        friendly_name = entity.get("name") or entity.get("original_name") or entity_id
        used_in = bundle.used_in(entity_id)
        integration_status = _integration_status(entity, config_entries_by_id)
        yaml_defined = bundle.yaml_defined(entity)

        if entity.get("disabled_by"):
            result.append(
                InactiveEntity(
                    entity_id=entity_id,
                    friendly_name=friendly_name,
                    reason=f"disabled by {entity['disabled_by']}",
                    used_in=used_in,
                    integration_status=integration_status,
                    yaml_defined=yaml_defined,
                )
            )
            continue

        state = state_by_id.get(entity_id)
        if state is None:
            result.append(
                InactiveEntity(entity_id, friendly_name, "not present in current states", used_in, integration_status, yaml_defined)
            )
            continue
        if state.get("state") in {"unavailable", "unknown"}:
            result.append(
                InactiveEntity(entity_id, friendly_name, f"state is {state['state']}", used_in, integration_status, yaml_defined)
            )
            continue

        last_changed = state.get("last_changed")
        if last_changed:
            try:
                changed_at = datetime.fromisoformat(last_changed.replace("Z", "+00:00"))
                age_days = (now - changed_at).days
                if age_days >= inactive_days:
                    result.append(
                        InactiveEntity(
                            entity_id,
                            friendly_name,
                            f"unchanged for {age_days} days (threshold {inactive_days})",
                            used_in,
                            integration_status,
                            yaml_defined,
                        )
                    )
            except ValueError:
                pass

    return sorted(result, key=lambda item: item.entity_id)


def find_good(
    entity_registry: list[dict],
    states: list[dict],
    config_entries_by_id: dict[str, dict],
    bundle: YamlBundle,
    flagged_ids: set[str],
) -> list[EntityContext]:
    """Every entity not already flagged as a duplicate or disabled/inactive."""
    state_by_id = {state["entity_id"]: state for state in states}
    result: list[EntityContext] = []
    for entity in entity_registry:
        entity_id = entity["entity_id"]
        if entity_id in flagged_ids:
            continue
        state = state_by_id.get(entity_id, {})
        result.append(
            EntityContext(
                entity_id=entity_id,
                state=state.get("state"),
                last_changed=state.get("last_changed"),
                used_in=bundle.used_in(entity_id),
                integration_status=_integration_status(entity, config_entries_by_id),
                yaml_defined=bundle.yaml_defined(entity),
                friendly_name=entity.get("name") or entity.get("original_name") or entity_id,
            )
        )
    return sorted(result, key=lambda item: item.entity_id)


def find_drift(bundle: YamlBundle, registry_entity_ids: set[str]) -> list[DriftIssue]:
    issues: list[DriftIssue] = list(bundle.parse_errors)

    for unique_id, paths in bundle.unique_ids.items():
        if len(paths) > 1:
            locations = ", ".join(str(path.relative_to(bundle.yaml_root)) for path in paths)
            issues.append(DriftIssue("duplicate_unique_id", unique_id, locations))

    available = registry_entity_ids | bundle.generated_entity_ids
    for entity_id, paths in sorted(bundle.referenced.items()):
        if entity_id in available or entity_id in bundle.module.SERVICE_REFERENCES:
            continue
        locations = ", ".join(str(path.relative_to(bundle.yaml_root)) for path in paths)
        issues.append(DriftIssue("stale_reference", entity_id, locations))

    return issues


def run_scan(client: HAClient, yaml_root: Path, inactive_days: int) -> ScanResult:
    entity_registry = client.get_entity_registry()
    device_registry = client.get_device_registry()
    states = client.get_states()
    config_entries = client.get_config_entries()
    config_entries_by_id = {entry["entry_id"]: entry for entry in config_entries}

    bundle = YamlBundle(yaml_root)

    duplicates = find_duplicates(entity_registry, device_registry, states, config_entries_by_id, bundle)
    inactive = find_inactive(entity_registry, states, inactive_days, config_entries_by_id, bundle)

    flagged_ids = {entity.entity_id for group in duplicates for entity in group.entities}
    flagged_ids |= {item.entity_id for item in inactive}
    good = find_good(entity_registry, states, config_entries_by_id, bundle, flagged_ids)

    registry_entity_ids = {entity["entity_id"] for entity in entity_registry}
    drift = find_drift(bundle, registry_entity_ids)

    return ScanResult(duplicates=duplicates, inactive=inactive, drift=drift, good=good)

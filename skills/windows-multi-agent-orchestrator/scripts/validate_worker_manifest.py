#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_WORKER_KEYS = {
    "worker_id",
    "worker_type",
    "display_name",
    "role",
    "capabilities",
    "cost_tier",
    "quality_tier",
    "speed_tier",
    "local_only",
    "api_cost",
    "supports_workspace_write",
}

REQUIRED_PLATFORM_KEYS = {
    "framework_label",
    "backend_label",
    "target_label",
    "mode_family",
}

REQUIRED_EXTERNAL_CONFIG_KEYS = {
    "binary",
    "invoke_command",
}

REQUIRED_EXTENSION_BRIDGE_CONFIG_KEYS = {
    "binary",
    "bridge_command",
}

REQUIRED_SERVICE_MESH_CONFIG_KEYS = {
    "base_url",
    "invoke_path",
}


def _load_payload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_workers(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("workers"), list):
        return [item for item in payload["workers"] if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [payload]
    raise ValueError("Manifest must be a worker object or an object with a 'workers' array.")


def _ensure(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_worker(worker: dict[str, Any], index: int, errors: list[str]) -> None:
    prefix = f"worker[{index}]"
    missing = sorted(REQUIRED_WORKER_KEYS - set(worker))
    if missing:
        errors.append(f"{prefix}: missing keys: {', '.join(missing)}")
    platform = worker.get("platform", {})
    _ensure(isinstance(platform, dict), f"{prefix}: platform must be an object", errors)
    if isinstance(platform, dict):
        missing_platform = sorted(REQUIRED_PLATFORM_KEYS - set(platform))
        if missing_platform:
            errors.append(f"{prefix}: platform missing keys: {', '.join(missing_platform)}")
    if worker.get("worker_type") == "external_cli_agent":
        config = worker.get("config", {})
        _ensure(isinstance(config, dict), f"{prefix}: config must be an object", errors)
        if isinstance(config, dict):
            missing_config = sorted(REQUIRED_EXTERNAL_CONFIG_KEYS - set(config))
            if missing_config:
                errors.append(f"{prefix}: external_cli_agent config missing keys: {', '.join(missing_config)}")
            invoke_command = config.get("invoke_command")
            _ensure(isinstance(invoke_command, list) and bool(invoke_command), f"{prefix}: invoke_command must be a non-empty list", errors)
    if worker.get("worker_type") == "extension_bridge_agent":
        config = worker.get("config", {})
        _ensure(isinstance(config, dict), f"{prefix}: config must be an object", errors)
        if isinstance(config, dict):
            missing_config = sorted(REQUIRED_EXTENSION_BRIDGE_CONFIG_KEYS - set(config))
            if missing_config:
                errors.append(f"{prefix}: extension_bridge_agent config missing keys: {', '.join(missing_config)}")
            bridge_command = config.get("bridge_command")
            _ensure(isinstance(bridge_command, list) and bool(bridge_command), f"{prefix}: bridge_command must be a non-empty list", errors)
    if worker.get("worker_type") == "service_mesh_agent":
        config = worker.get("config", {})
        _ensure(isinstance(config, dict), f"{prefix}: config must be an object", errors)
        if isinstance(config, dict):
            missing_config = sorted(REQUIRED_SERVICE_MESH_CONFIG_KEYS - set(config))
            if missing_config:
                errors.append(f"{prefix}: service_mesh_agent config missing keys: {', '.join(missing_config)}")
            _ensure(isinstance(config.get("base_url"), str) and bool(str(config.get("base_url")).strip()), f"{prefix}: base_url must be a non-empty string", errors)
            _ensure(isinstance(config.get("invoke_path"), str) and bool(str(config.get("invoke_path")).strip()), f"{prefix}: invoke_path must be a non-empty string", errors)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: validate_worker_manifest.py <path-to-json>", file=sys.stderr)
        return 2

    path = Path(argv[1]).resolve()
    if not path.exists():
        print(f"Manifest not found: {path}", file=sys.stderr)
        return 2

    try:
        payload = _load_payload(path)
        workers = _iter_workers(payload)
    except Exception as exc:
        print(f"Invalid manifest: {exc}", file=sys.stderr)
        return 1

    errors: list[str] = []
    if not workers:
        errors.append("No worker definitions found.")
    for index, worker in enumerate(workers):
        validate_worker(worker, index, errors)

    if errors:
        print("Manifest validation failed:", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        return 1

    print(f"Manifest valid: {path} ({len(workers)} worker(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

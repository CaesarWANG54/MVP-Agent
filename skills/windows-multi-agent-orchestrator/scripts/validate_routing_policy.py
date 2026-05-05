#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_ROOT_KEYS = {
    "planner_roles",
    "builder_roles",
    "reviewer_roles",
    "fallback_lanes",
    "budget_modes",
    "quick_ping_strategy",
    "deep_ping_strategy",
}

REQUIRED_BUDGET_MODES = {"cheap", "balanced", "premium"}


def _load_payload(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def _ensure_string_list(value: Any, label: str, errors: list[str]) -> None:
    _ensure(isinstance(value, list) and bool(value), f"{label} must be a non-empty array", errors)
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure(isinstance(item, str) and bool(item.strip()), f"{label}[{index}] must be a non-empty string", errors)


def validate_policy(payload: Any, errors: list[str]) -> None:
    routing = payload.get("routing_policy") if isinstance(payload, dict) else None
    _ensure(isinstance(routing, dict), "routing_policy must be an object", errors)
    if not isinstance(routing, dict):
        return
    missing = sorted(REQUIRED_ROOT_KEYS - set(routing))
    if missing:
        errors.append(f"routing_policy missing keys: {', '.join(missing)}")
    _ensure_string_list(routing.get("planner_roles"), "routing_policy.planner_roles", errors)
    _ensure_string_list(routing.get("builder_roles"), "routing_policy.builder_roles", errors)
    _ensure_string_list(routing.get("reviewer_roles"), "routing_policy.reviewer_roles", errors)
    _ensure_string_list(routing.get("quick_ping_strategy"), "routing_policy.quick_ping_strategy", errors)
    _ensure_string_list(routing.get("deep_ping_strategy"), "routing_policy.deep_ping_strategy", errors)

    fallback_lanes = routing.get("fallback_lanes")
    _ensure(isinstance(fallback_lanes, list) and bool(fallback_lanes), "routing_policy.fallback_lanes must be a non-empty array", errors)
    if isinstance(fallback_lanes, list):
        for index, lane in enumerate(fallback_lanes):
            prefix = f"routing_policy.fallback_lanes[{index}]"
            _ensure(isinstance(lane, dict), f"{prefix} must be an object", errors)
            if not isinstance(lane, dict):
                continue
            for key in ("name", "when"):
                _ensure(isinstance(lane.get(key), str) and bool(str(lane.get(key)).strip()), f"{prefix}.{key} must be a non-empty string", errors)
            _ensure_string_list(lane.get("workers"), f"{prefix}.workers", errors)

    budget_modes = routing.get("budget_modes")
    _ensure(isinstance(budget_modes, dict), "routing_policy.budget_modes must be an object", errors)
    if isinstance(budget_modes, dict):
        missing_modes = sorted(REQUIRED_BUDGET_MODES - set(budget_modes))
        if missing_modes:
            errors.append(f"routing_policy.budget_modes missing modes: {', '.join(missing_modes)}")
        for mode_name in REQUIRED_BUDGET_MODES:
            mode = budget_modes.get(mode_name)
            prefix = f"routing_policy.budget_modes.{mode_name}"
            _ensure(isinstance(mode, dict), f"{prefix} must be an object", errors)
            if not isinstance(mode, dict):
                continue
            _ensure(isinstance(mode.get("prefer_local"), bool), f"{prefix}.prefer_local must be a boolean", errors)
            _ensure(isinstance(mode.get("max_remote_hops"), int) and int(mode.get("max_remote_hops")) >= 0, f"{prefix}.max_remote_hops must be a non-negative integer", errors)
            _ensure(isinstance(mode.get("notes"), str) and bool(str(mode.get("notes")).strip()), f"{prefix}.notes must be a non-empty string", errors)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: validate_routing_policy.py <path-to-json>", file=sys.stderr)
        return 2
    path = Path(argv[1]).resolve()
    if not path.exists():
        print(f"Routing policy not found: {path}", file=sys.stderr)
        return 2
    try:
        payload = _load_payload(path)
    except Exception as exc:
        print(f"Invalid routing policy JSON: {exc}", file=sys.stderr)
        return 1
    errors: list[str] = []
    validate_policy(payload, errors)
    if errors:
        print("Routing policy validation failed:", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        return 1
    print(f"Routing policy valid: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

from __future__ import annotations

from pathlib import Path

from ..models import WorkerSpec
from .base import BaseWorker
from .claude_cli import ClaudeCliWorker
from .codex_cli import CodexCliWorker
from .extension_bridge_agent import ExtensionBridgeAgentWorker
from .external_cli_agent import ExternalCliAgentWorker
from .ollama_api import OllamaApiWorker
from .openclaw_agent import OpenClawAgentWorker
from .service_mesh_agent import ServiceMeshAgentWorker


class WorkerRegistry:
    def __init__(self, worker_specs: dict[str, WorkerSpec], workspace_root: Path, runs_dir: Path) -> None:
        self.worker_specs = worker_specs
        self.workspace_root = workspace_root
        self.runs_dir = runs_dir
        self._workers: dict[str, BaseWorker] = {}

    def get(self, worker_id: str) -> BaseWorker:
        if worker_id in self._workers:
            return self._workers[worker_id]

        spec = self.worker_specs[worker_id]
        if spec.worker_type == "ollama_api":
            worker: BaseWorker = OllamaApiWorker(spec, self.workspace_root, self.runs_dir)
        elif spec.worker_type == "openclaw_agent":
            worker = OpenClawAgentWorker(spec, self.workspace_root, self.runs_dir)
        elif spec.worker_type == "codex_cli":
            worker = CodexCliWorker(spec, self.workspace_root, self.runs_dir)
        elif spec.worker_type == "claude_cli":
            worker = ClaudeCliWorker(spec, self.workspace_root, self.runs_dir)
        elif spec.worker_type == "external_cli_agent":
            worker = ExternalCliAgentWorker(spec, self.workspace_root, self.runs_dir)
        elif spec.worker_type == "extension_bridge_agent":
            worker = ExtensionBridgeAgentWorker(spec, self.workspace_root, self.runs_dir)
        elif spec.worker_type == "service_mesh_agent":
            worker = ServiceMeshAgentWorker(spec, self.workspace_root, self.runs_dir)
        else:
            raise ValueError(f"Unsupported worker type: {spec.worker_type}")

        self._workers[worker_id] = worker
        return worker

    def all_enabled_specs(self) -> list[WorkerSpec]:
        return [spec for spec in self.worker_specs.values() if spec.enabled]

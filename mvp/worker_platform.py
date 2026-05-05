from __future__ import annotations

from .models import WorkerSpec


_TYPE_DEFAULTS: dict[str, dict[str, str]] = {
    "ollama_api": {
        "framework_label": "Ollama",
        "backend_label": "Ollama API",
        "mode_family": "local_llm",
        "security_label": "纯文本模型调用",
        "security_detail": "通过本地 HTTP 调用模型，只返回文本，不直接获得文件系统或命令执行权限。",
    },
    "openclaw_agent": {
        "framework_label": "OpenClaw",
        "backend_label": "OpenClaw Agent",
        "mode_family": "agent_cli",
        "security_label": "本地嵌入运行",
        "security_detail": "通过嵌入式 agent CLI 执行；实际权限取决于底层 agent 的运行模式与授权配置。",
    },
    "codex_cli": {
        "framework_label": "Codex",
        "backend_label": "Codex CLI",
        "mode_family": "agent_cli",
    },
    "claude_cli": {
        "framework_label": "Claude",
        "backend_label": "Claude Code CLI",
        "mode_family": "agent_cli",
    },
    "external_cli_agent": {
        "framework_label": "External Agent",
        "backend_label": "External CLI Agent",
        "mode_family": "agent_cli",
        "security_label": "配置驱动 CLI",
        "security_detail": "通过可配置的外部 CLI 调用 agent；权限边界取决于该 CLI 自身的沙箱与授权设置。",
    },
    "extension_bridge_agent": {
        "framework_label": "Extension Bridge",
        "backend_label": "IDE Extension Bridge",
        "mode_family": "extension_bridge",
        "security_label": "桥接控制",
        "security_detail": "通过本地桥接命令与扩展或 IDE agent 通信；权限边界取决于桥接器和宿主工具的授权设置。",
    },
    "service_mesh_agent": {
        "framework_label": "Service Mesh",
        "backend_label": "Agent Service API",
        "mode_family": "service_mesh",
        "security_label": "服务策略控制",
        "security_detail": "通过 HTTP 或守护进程 API 调用 agent 服务；权限、工作区访问和执行边界由服务端策略决定。",
    },
}


def worker_mode_family(spec: WorkerSpec) -> str:
    platform = spec.platform if isinstance(spec.platform, dict) else {}
    if str(platform.get("mode_family", "")).strip():
        return str(platform.get("mode_family")).strip().lower()
    return _TYPE_DEFAULTS.get(spec.worker_type, {}).get("mode_family", "default")


def _platform_target(spec: WorkerSpec, *fallbacks: str) -> str:
    platform = spec.platform if isinstance(spec.platform, dict) else {}
    for value in (
        platform.get("target_label"),
        spec.config.get("target_label"),
        spec.config.get("model"),
        *fallbacks,
        spec.worker_id,
    ):
        text = str(value or "").strip()
        if text:
            return text
    return spec.worker_id


def describe_worker_platform(spec: WorkerSpec) -> dict[str, str]:
    platform = spec.platform if isinstance(spec.platform, dict) else {}
    defaults = _TYPE_DEFAULTS.get(spec.worker_type, {})

    if spec.worker_type == "ollama_api":
        return {
            "framework_label": str(platform.get("framework_label") or defaults.get("framework_label") or "Ollama"),
            "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or "Ollama API"),
            "target_label": _platform_target(spec),
            "security_label": str(platform.get("security_label") or defaults.get("security_label") or "纯文本模型调用"),
            "security_detail": str(platform.get("security_detail") or defaults.get("security_detail") or ""),
        }

    if spec.worker_type == "openclaw_agent":
        agent_name = str(spec.config.get("agent", "main")).strip() or "main"
        expected_model = str(
            platform.get("target_label")
            or spec.config.get("target_label")
            or spec.config.get("expected_model")
            or ""
        ).strip()
        framework_label = str(platform.get("framework_label") or "").strip()
        if not framework_label:
            framework_label = "Hermes" if "hermes" in spec.worker_id.lower() or "hermes" in agent_name.lower() else "OpenClaw"
        return {
            "framework_label": framework_label,
            "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or "OpenClaw Agent"),
            "target_label": f"{agent_name} @ {expected_model}" if expected_model else agent_name,
            "security_label": str(platform.get("security_label") or defaults.get("security_label") or "本地嵌入运行"),
            "security_detail": str(platform.get("security_detail") or defaults.get("security_detail") or ""),
        }

    if spec.worker_type == "codex_cli":
        read_sandbox = str(spec.config.get("read_sandbox", "read-only"))
        write_sandbox = str(spec.config.get("write_sandbox", "workspace-write"))
        return {
            "framework_label": str(platform.get("framework_label") or defaults.get("framework_label") or "Codex"),
            "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or "Codex CLI"),
            "target_label": _platform_target(spec),
            "security_label": str(
                platform.get("security_label")
                or f"读={read_sandbox} / 写={write_sandbox if spec.supports_workspace_write else read_sandbox}"
            ),
            "security_detail": str(
                platform.get("security_detail")
                or "Codex 按任务切换沙箱模式；默认不是整盘完全访问，除非明确配置为 danger-full-access。"
            ),
        }

    if spec.worker_type == "claude_cli":
        read_mode = str(spec.config.get("read_permission_mode", "plan"))
        write_mode = str(spec.config.get("write_permission_mode", "acceptEdits"))
        return {
            "framework_label": str(platform.get("framework_label") or defaults.get("framework_label") or "Claude"),
            "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or "Claude Code CLI"),
            "target_label": _platform_target(spec),
            "security_label": str(
                platform.get("security_label")
                or f"read={read_mode} / write={write_mode if spec.supports_workspace_write else read_mode}"
            ),
            "security_detail": str(
                platform.get("security_detail")
                or "Claude Code 使用非交互 print 模式。读任务保持 plan 模式，写任务使用配置的 permission mode。"
            ),
        }

    if spec.worker_type == "external_cli_agent":
        return {
            "framework_label": str(platform.get("framework_label") or defaults.get("framework_label") or "External Agent"),
            "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or "External CLI Agent"),
            "target_label": _platform_target(spec, spec.config.get("binary", "")),
            "security_label": str(platform.get("security_label") or defaults.get("security_label") or "配置驱动 CLI"),
            "security_detail": str(platform.get("security_detail") or defaults.get("security_detail") or ""),
        }

    if spec.worker_type == "extension_bridge_agent":
        return {
            "framework_label": str(platform.get("framework_label") or defaults.get("framework_label") or "Extension Bridge"),
            "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or "IDE Extension Bridge"),
            "target_label": _platform_target(spec, spec.config.get("binary", "")),
            "security_label": str(platform.get("security_label") or defaults.get("security_label") or "桥接控制"),
            "security_detail": str(platform.get("security_detail") or defaults.get("security_detail") or ""),
        }

    if spec.worker_type == "service_mesh_agent":
        return {
            "framework_label": str(platform.get("framework_label") or defaults.get("framework_label") or "Service Mesh"),
            "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or "Agent Service API"),
            "target_label": _platform_target(spec, spec.config.get("base_url", "")),
            "security_label": str(platform.get("security_label") or defaults.get("security_label") or "服务策略控制"),
            "security_detail": str(platform.get("security_detail") or defaults.get("security_detail") or ""),
        }

    return {
        "framework_label": str(platform.get("framework_label") or defaults.get("framework_label") or spec.worker_type),
        "backend_label": str(platform.get("backend_label") or defaults.get("backend_label") or spec.worker_type),
        "target_label": _platform_target(spec),
        "security_label": str(platform.get("security_label") or defaults.get("security_label") or "未知"),
        "security_detail": str(platform.get("security_detail") or defaults.get("security_detail") or "未定义权限说明。"),
    }

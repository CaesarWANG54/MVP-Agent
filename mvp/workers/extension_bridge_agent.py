from __future__ import annotations

from .external_cli_agent import ExternalCliAgentWorker


class ExtensionBridgeAgentWorker(ExternalCliAgentWorker):
    INVOKE_COMMAND_KEY = "bridge_command"
    HEALTH_COMMAND_KEY = "health_command"
    QUICK_PING_COMMAND_KEY = "quick_ping_command"
    DEEP_PING_COMMAND_KEY = "deep_ping_command"

"""Project provider connection settings over the client's live shared settings.

Ownership follows CC Switch 4.0.29's src-tauri/src/live/floor.rs. Keep
credentials/routes out of the shared layer, even when the selected row omits
them. MCP is a launcher extension: explicit per-provider declarations survive.
"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any


CLAUDE_CONNECTION_TOP = frozenset({
    "apiKeyHelper", "apiBaseUrl", "primaryModel", "smallFastModel", "apiKey",
    "model", "fallbackModel", "modelOverrides", "modelPicker", "advisorModel",
    "awsAuthRefresh", "awsCredentialExport", "gcpAuthRefresh",
})
CLAUDE_CONNECTION_ENV = frozenset({
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
    "CLOUD_ML_REGION", "GOOGLE_APPLICATION_CREDENTIALS", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    *("CLAUDE_CODE_USE_" + name for name in (
        "BEDROCK", "VERTEX", "FOUNDRY", "GATEWAY", "MANTLE",
        "ANTHROPIC_AWS", "ANTHROPIC_GOOGLE_CLOUD",
    )),
})
CLAUDE_EXCLUSIVE_ENV = frozenset({
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS", "CLAUDE_CODE_DISABLE_ARTIFACT",
    "ENABLE_TOOL_SEARCH", "CLAUDE_CODE_DISABLE_THINKING",
    "DISABLE_INTERLEAVED_THINKING", "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT",
    "CLAUDE_CODE_EXTRA_BODY", "CLAUDE_CODE_ENABLE_FINE_GRAINED_TOOL_STREAMING",
    "CLAUDE_CODE_AUTO_MODE_SERVER", "CLAUDE_CODE_MAX_CONTEXT_TOKENS",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_DISABLE_1M_CONTEXT", "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
})


def claude_connection_env(key: str) -> bool:
    return (
        key.startswith(("ANTHROPIC_", "AWS_", "VERTEX_REGION_"))
        or key in CLAUDE_CONNECTION_ENV
        or (key.startswith("CLAUDE_CODE_SKIP_") and key.endswith("_AUTH"))
    )


def claude_provider_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(value) for key, value in settings.items()
              if key in CLAUDE_CONNECTION_TOP or key == "mcpServers"}
    result["env"] = {
        key: deepcopy(value) for key, value in settings.get("env", {}).items()
        if claude_connection_env(key) or key in CLAUDE_EXCLUSIVE_ENV
    }
    return result


def claude_shared_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: deepcopy(value) for key, value in settings.items()
              if key not in CLAUDE_CONNECTION_TOP and key != "env"}
    # Exclusive compatibility/window settings can belong to the active provider;
    # never carry them from a different live provider into this launch.
    if "env" in settings:
        result["env"] = {
            key: deepcopy(value) for key, value in settings["env"].items()
            if not claude_connection_env(key) and key not in CLAUDE_EXCLUSIVE_ENV
            and key not in {"CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR"}
        }
    return result


CODEX_CONNECTION_TOP = frozenset({
    "model_provider", "openai_base_url", "model", "review_model",
    "model_reasoning_effort", "plan_mode_reasoning_effort", "model_catalog_json",
    "experimental_bearer_token", "base_url", "wire_api",
})
CODEX_EXCLUSIVE_TOP = frozenset({
    "web_search", "model_context_window", "model_auto_compact_token_limit",
    "model_supports_reasoning_summaries", "model_verbosity",
})
CODEX_CONNECTION_NESTED = {
    "agents": ("default_subagent_model", "default_subagent_reasoning_effort"),
    "memories": ("extract_model", "consolidation_model"),
}


def codex_settings(
    provider: Mapping[str, Any], shared: Mapping[str, Any]
) -> dict[str, Any]:
    owned = CODEX_CONNECTION_TOP | CODEX_EXCLUSIVE_TOP
    result = {key: deepcopy(value) for key, value in shared.items()
              if key not in owned and key not in {
                  "model_providers", "profile", "profiles", "disable_response_storage",
              }}
    result.update({key: deepcopy(value) for key, value in provider.items()
                   if key in owned})
    selected = provider.get("model_provider")
    tables = provider.get("model_providers", {})
    if isinstance(selected, str) and isinstance(tables, Mapping) and selected in tables:
        result["model_providers"] = {selected: deepcopy(tables[selected])}
    for table, keys in CODEX_CONNECTION_NESTED.items():
        if table in result and not isinstance(result[table], Mapping):
            raise ValueError("invalid shared Codex table")
        target = result.get(table, {})
        source = provider.get(table, {})
        if not isinstance(source, Mapping):
            raise ValueError("invalid provider Codex table")
        for key in keys:
            target.pop(key, None)
            if key in source:
                target[key] = deepcopy(source[key])
        if target:
            result[table] = target
        else:
            result.pop(table, None)
    # Replace a named MCP server as a unit, avoiding credentials from the live
    # server leaking into a different provider-specific server with that name.
    if "mcp_servers" in provider:
        global_servers = result.get("mcp_servers", {})
        servers = provider["mcp_servers"]
        if not isinstance(global_servers, Mapping) or not isinstance(servers, Mapping):
            raise ValueError("invalid MCP servers")
        result["mcp_servers"] = {**global_servers, **deepcopy(dict(servers))}
    return result

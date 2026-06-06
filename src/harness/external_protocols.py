from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from harness.config import default_config, load_config
from harness.paths import resolve_project_root
from harness.protocol_adapters import build_default_protocol_adapter_registry
from harness.session_tools import model_visible_session_tool_ids, session_tool_catalog_projection


EXTERNAL_PROTOCOL_CATALOG_SCHEMA_VERSION = "harness.external_protocol_catalog/v1"
EXTERNAL_PROTOCOL_DESCRIPTOR_SCHEMA_VERSION = "harness.external_protocol_descriptor/v1"
EXTERNAL_PROTOCOL_AUTHORITY_SCHEMA_VERSION = "harness.external_protocol_authority/v1"
LOCAL_SERVER_OPENAPI_SCHEMA_VERSION = "harness.local_server.openapi/v1"

ExternalProtocolStatus = Literal["implemented", "metadata_only", "cached_resource_only", "fail_closed"]


class ExternalProtocolAuthority(BaseModel):
    schema_version: str = EXTERNAL_PROTOCOL_AUTHORITY_SCHEMA_VERSION
    read_only_projection: bool = True
    process_start_allowed: bool = False
    network_allowed: bool = False
    tool_execution_allowed: bool = False
    agent_execution_allowed: bool = False
    filesystem_mutation_allowed: bool = False
    model_context_allowed: bool = False
    credential_access_allowed: bool = False
    permission_granting: bool = False
    requires_explicit_permission: bool = True


class ExternalProtocolDescriptor(BaseModel):
    schema_version: str = EXTERNAL_PROTOCOL_DESCRIPTOR_SCHEMA_VERSION
    id: str
    title: str
    protocol: str
    category: Literal["local", "http", "extension", "agent_to_agent", "rpc", "model_provider"]
    status: ExternalProtocolStatus
    boundary_kind: str
    runtime_enabled: bool = False
    default_model_visible: bool = False
    source_surfaces: list[str] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    authority: ExternalProtocolAuthority = Field(default_factory=ExternalProtocolAuthority)
    reference_patterns: list[str] = Field(default_factory=list)
    telemetry_contracts: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class ExternalProtocolCatalog(BaseModel):
    schema_version: str = EXTERNAL_PROTOCOL_CATALOG_SCHEMA_VERSION
    ok: bool = True
    project_root: Path
    initialized: bool
    protocols: list[ExternalProtocolDescriptor]
    registered_model_protocols: list[str]
    model_visible_tool_ids: list[str]
    safety: dict[str, bool]
    summary: dict[str, int]


def build_external_protocol_catalog(project_root: Path) -> ExternalProtocolCatalog:
    """Return a read-only compatibility projection for external protocol surfaces."""

    project_root = resolve_project_root(project_root)
    cfg, initialized = _load_config_or_default(project_root)
    tool_catalog = session_tool_catalog_projection(project_root=project_root)
    tool_ids = {str(tool.get("id")) for tool in tool_catalog.get("tools", []) if isinstance(tool, dict)}
    visible = model_visible_session_tool_ids(project_root=project_root)
    model_protocols = build_default_protocol_adapter_registry().list_protocols()
    mcp_servers = getattr(cfg.mcp, "servers", {})
    mcp_resource_count = sum(len(server.resources) for server in mcp_servers.values())
    mcp_enabled_server_count = sum(1 for server in mcp_servers.values() if cfg.mcp.enabled and server.enabled)

    protocols = [
        _model_provider_protocols_descriptor(model_protocols),
        _local_server_openapi_descriptor(),
        _session_tool_protocol_descriptor(tool_ids),
        _mcp_tool_descriptor(
            mcp_enabled=bool(cfg.mcp.enabled),
            server_count=len(mcp_servers),
            enabled_server_count=mcp_enabled_server_count,
            default_visible="mcp" in visible,
        ),
        _mcp_cached_resource_descriptor(
            mcp_enabled=bool(cfg.mcp.enabled),
            resource_count=mcp_resource_count,
            default_visible="mcp-resource" in visible,
        ),
        _external_openapi_tool_descriptor(),
        _a2a_remote_agent_descriptor(),
        _grpc_remote_tool_descriptor(),
    ]
    summary: dict[str, int] = {
        "protocol_count": len(protocols),
        "implemented_count": sum(1 for item in protocols if item.status == "implemented"),
        "metadata_only_count": sum(1 for item in protocols if item.status == "metadata_only"),
        "cached_resource_only_count": sum(1 for item in protocols if item.status == "cached_resource_only"),
        "fail_closed_count": sum(1 for item in protocols if item.status == "fail_closed"),
        "runtime_enabled_count": sum(1 for item in protocols if item.runtime_enabled),
        "default_model_visible_count": sum(1 for item in protocols if item.default_model_visible),
        "mcp_server_count": len(mcp_servers),
        "mcp_enabled_server_count": mcp_enabled_server_count,
        "mcp_cached_resource_count": mcp_resource_count,
        "registered_model_protocol_count": len(model_protocols),
    }
    return ExternalProtocolCatalog(
        project_root=project_root,
        initialized=initialized,
        protocols=protocols,
        registered_model_protocols=model_protocols,
        model_visible_tool_ids=visible,
        safety={
            "read_only": True,
            "process_started": False,
            "network_called": False,
            "tool_execution_started": False,
            "agent_execution_started": False,
            "filesystem_modified": False,
            "credential_accessed": False,
            "permission_granting": False,
            "model_context_allowed": False,
        },
        summary=summary,
    )


def get_external_protocol_descriptor(project_root: Path, protocol_id: str) -> ExternalProtocolDescriptor:
    for descriptor in build_external_protocol_catalog(project_root).protocols:
        if descriptor.id == protocol_id:
            return descriptor
    raise KeyError(f"External protocol not found: {protocol_id}")


def _model_provider_protocols_descriptor(model_protocols: list[str]) -> ExternalProtocolDescriptor:
    return ExternalProtocolDescriptor(
        id="model_provider_protocols",
        title="Model provider protocol adapters",
        protocol="model-provider",
        category="model_provider",
        status="implemented",
        boundary_kind="hosted_provider_or_local_model",
        runtime_enabled=True,
        source_surfaces=["harness models protocols", "SessionRuntimeManager protocol adapter registry"],
        authority=ExternalProtocolAuthority(
            network_allowed=False,
            tool_execution_allowed=True,
            model_context_allowed=True,
            credential_access_allowed=True,
            requires_explicit_permission=True,
        ),
        reference_patterns=["openai_agents", "google_adk", "microsoft_agent_framework"],
        telemetry_contracts=["opentelemetry.semconv.gen_ai", "w3c_trace_context"],
        notes=[
            "Runtime remains gated by provider/model selection, data-boundary approvals, credential resolution, and selected protocol adapter.",
            f"Registered model protocols: {', '.join(model_protocols)}.",
        ],
    )


def _local_server_openapi_descriptor() -> ExternalProtocolDescriptor:
    return ExternalProtocolDescriptor(
        id="local_server_openapi",
        title="Local server OpenAPI",
        protocol="openapi",
        category="http",
        status="metadata_only",
        boundary_kind="local_only",
        source_surfaces=["harness serve --openapi", "GET /openapi.json"],
        blocked_reasons=["openapi_document_only", "bearer_auth_required_for_server_routes"],
        authority=ExternalProtocolAuthority(requires_explicit_permission=False),
        reference_patterns=["modelcontextprotocol", "openai_agents"],
        telemetry_contracts=["opentelemetry.trace"],
        notes=[
            f"OpenAPI schema version: {LOCAL_SERVER_OPENAPI_SCHEMA_VERSION}.",
            "The document describes local bearer-auth routes; reading it does not start the server or execute routes.",
        ],
    )


def _session_tool_protocol_descriptor(tool_ids: set[str]) -> ExternalProtocolDescriptor:
    implemented = {"read", "glob", "grep", "artifact-read"}.issubset(tool_ids)
    return ExternalProtocolDescriptor(
        id="local_session_tools",
        title="Local session tool interface",
        protocol="harness-session-tools",
        category="local",
        status="implemented" if implemented else "metadata_only",
        boundary_kind="local_only",
        runtime_enabled=implemented,
        default_model_visible=implemented,
        source_surfaces=["harness session tools", "provider-native model-visible tool schemas"],
        authority=ExternalProtocolAuthority(
            tool_execution_allowed=implemented,
            model_context_allowed=implemented,
            requires_explicit_permission=False,
        ),
        reference_patterns=["openai_agents", "microsoft_agent_framework"],
        telemetry_contracts=["opentelemetry.semconv.gen_ai"],
        notes=[
            "Default exposure is limited to low-risk local read/session surfaces by policy.exposure.model_visible.",
            "Permission-gated shell, write, web, MCP, plugin, and task-spawning tools stay out of the default model-visible set.",
        ],
    )


def _mcp_tool_descriptor(
    *,
    mcp_enabled: bool,
    server_count: int,
    enabled_server_count: int,
    default_visible: bool,
) -> ExternalProtocolDescriptor:
    return ExternalProtocolDescriptor(
        id="mcp_tool",
        title="MCP tool execution",
        protocol="mcp",
        category="extension",
        status="fail_closed",
        boundary_kind="mcp",
        default_model_visible=default_visible,
        source_surfaces=["harness mcp status", "POST /mcp/{name}/connect", "session tool catalog:mcp"],
        blocked_reasons=["mcp_tool_execution_disabled", "mcp_process_launch_disabled", "mcp_network_connection_disabled"],
        authority=ExternalProtocolAuthority(),
        reference_patterns=["modelcontextprotocol", "openai_agents", "microsoft_agent_framework"],
        telemetry_contracts=["opentelemetry.semconv.gen_ai.mcp", "w3c_trace_context"],
        notes=[
            f"Configured MCP servers: {server_count}; enabled servers: {enabled_server_count}; global MCP enabled: {mcp_enabled}.",
            "MCP tool execution remains disabled until origin, tool name, arguments, replay policy, permission evidence, and MCP client span semantics are implemented.",
        ],
        next_actions=[
            "Implement exact MCP tool permission records, W3C trace context propagation, MCP client spans, and evidence before enabling process launch, network connection, or tool execution.",
        ],
    )


def _mcp_cached_resource_descriptor(
    *,
    mcp_enabled: bool,
    resource_count: int,
    default_visible: bool,
) -> ExternalProtocolDescriptor:
    return ExternalProtocolDescriptor(
        id="mcp_cached_resource",
        title="Cached MCP resource reads",
        protocol="mcp-resource",
        category="extension",
        status="cached_resource_only",
        boundary_kind="mcp",
        runtime_enabled=resource_count > 0 and mcp_enabled,
        default_model_visible=default_visible,
        source_surfaces=["harness mcp resources", "GET /mcp/resources", "session tool catalog:mcp-resource"],
        blocked_reasons=["mcp_connection_disabled", "mcp_resource_read_requires_permission"],
        authority=ExternalProtocolAuthority(tool_execution_allowed=resource_count > 0 and mcp_enabled),
        reference_patterns=["modelcontextprotocol"],
        telemetry_contracts=["opentelemetry.semconv.gen_ai.mcp", "w3c_trace_context"],
        notes=[
            f"Configured cached MCP resources: {resource_count}; global MCP enabled: {mcp_enabled}.",
            "Cached resource reads use project-configured files and explicit permission; they do not start MCP processes or network connections.",
        ],
    )


def _external_openapi_tool_descriptor() -> ExternalProtocolDescriptor:
    return ExternalProtocolDescriptor(
        id="external_openapi_tool",
        title="External OpenAPI tool import",
        protocol="openapi",
        category="http",
        status="fail_closed",
        boundary_kind="external_network",
        source_surfaces=["planned external protocol adapter"],
        blocked_reasons=["external_openapi_import_disabled", "external_network_approval_required"],
        authority=ExternalProtocolAuthority(),
        reference_patterns=["openai_agents", "microsoft_agent_framework"],
        telemetry_contracts=["opentelemetry.semconv.gen_ai", "w3c_trace_context"],
        notes=[
            "External OpenAPI imports are not model-visible or executable by default.",
            "Use Harness session/network policy and quarantine evidence before adding external OpenAPI tool execution.",
        ],
        next_actions=[
            "Add schema ingestion, host allowlist, approval, request logging, W3C trace context propagation, and replay policy before enabling."
        ],
    )


def _a2a_remote_agent_descriptor() -> ExternalProtocolDescriptor:
    return ExternalProtocolDescriptor(
        id="a2a_remote_agent",
        title="A2A remote agent interoperability",
        protocol="a2a",
        category="agent_to_agent",
        status="fail_closed",
        boundary_kind="external_network",
        source_surfaces=["planned remote-agent adapter"],
        blocked_reasons=["a2a_discovery_disabled", "a2a_auth_disabled", "a2a_task_exchange_disabled"],
        authority=ExternalProtocolAuthority(),
        reference_patterns=["A2A", "google_adk", "microsoft_agent_framework"],
        telemetry_contracts=["opentelemetry.semconv.gen_ai.agent", "w3c_trace_context"],
        notes=[
            "A2A is tracked as an interoperability target, not an enabled execution path.",
            "Remote agent discovery, authentication, message integrity, retry semantics, and trace propagation must be explicit before use.",
        ],
        next_actions=["Add signed task envelopes, identity policy, idempotency, trace propagation, and approval boundaries first."],
    )


def _grpc_remote_tool_descriptor() -> ExternalProtocolDescriptor:
    return ExternalProtocolDescriptor(
        id="grpc_remote_tool",
        title="gRPC remote tool or service adapter",
        protocol="grpc",
        category="rpc",
        status="fail_closed",
        boundary_kind="external_network",
        source_surfaces=["planned remote service adapter"],
        blocked_reasons=["grpc_channel_disabled", "remote_tool_execution_disabled"],
        authority=ExternalProtocolAuthority(),
        reference_patterns=["dapr", "opentelemetry"],
        telemetry_contracts=["opentelemetry.trace", "w3c_trace_context"],
        notes=[
            "gRPC is a candidate transport for typed low-latency services, but no generic remote execution adapter is enabled.",
            "Remote service calls require identity, host allowlists, deadlines, retry policy, and telemetry first.",
        ],
        next_actions=["Add typed stubs, host policy, request deadlines, retry/idempotency, and span propagation before enabling."],
    )


def _load_config_or_default(project_root: Path):
    try:
        return load_config(project_root), True
    except FileNotFoundError:
        return default_config(), False

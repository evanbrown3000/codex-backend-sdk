"""
codex-backend-sdk — Unofficial Python SDK for the ChatGPT Codex backend API.

The package intentionally exposes existing execution surfaces without creating
parallel provider, transport, or memory authorities.
"""

__version__ = "0.9.5"

from .agent_memory import AgentMemoryClient
from .remote_shell import RemoteShellClient
from .capability_registry import Capability, CapabilityRegistry

from .oauth import (
    DeviceCode,
    complete_device_code_login,
    refresh_access_token,
    request_device_code,
    revoke_oauth_token,
    run_oauth_flow,
)
from .storage import load_tokens, save_tokens, TokenStore
from .resources.remote_control import (
    RemoteControlClient,
    RemoteControlClientPage,
    RemoteControlConnection,
    RemoteControlConnectionClosed,
    RemoteControlDesktop,
    RemoteControlEnrollment,
    RemoteControlEnvironment,
    RemoteControlEnvironmentPage,
    RemoteControlPairing,
    RemoteControlPairingStatus,
)
from .resources.chatgpt_apps import ChatGPTAppsProtocolError, HostedMCPConnection
from .resources.chatgpt_connectors import ConnectorAuthenticationRequiredError
from .resources.responses_websocket import (
    ResponsesWebSocketConnection,
    ResponsesWebSocketError,
)
from .codex_client import *
from .provider_actuation import (
    ParentOperation,
    ProviderPromptRequest,
    ProviderResultEnvelope,
    UnifiedProviderActuator,
    configure_default_actuator,
    prompt,
)
from .provider_actuation_client import ProviderActuationClient

__all__ = [
    "AgentMemoryClient",
    "RemoteShellClient",
    "Capability",
    "CapabilityRegistry",
    "CodexClient",
    "OpenAI",
    "CodexBackendUnsupportedParameterError",
    "CodexBaseModel",
    "ChatGPTSpeech",
    "ChatGPTAppsProtocolError",
    "ConnectorAuthenticationRequiredError",
    "HostedMCPConnection",
    "DeviceCode",
    "RemoteControlClient",
    "RemoteControlClientPage",
    "RemoteControlConnection",
    "RemoteControlConnectionClosed",
    "RemoteControlDesktop",
    "RemoteControlEnrollment",
    "RemoteControlEnvironment",
    "RemoteControlEnvironmentPage",
    "RemoteControlPairing",
    "RemoteControlPairingStatus",
    "ResponsesWebSocketConnection",
    "ResponsesWebSocketError",
    "run_oauth_flow",
    "refresh_access_token",
    "request_device_code",
    "complete_device_code_login",
    "revoke_oauth_token",
    "load_tokens",
    "save_tokens",
    "TokenStore",
    "ParentOperation",
    "ProviderPromptRequest",
    "ProviderResultEnvelope",
    "UnifiedProviderActuator",
    "configure_default_actuator",
    "prompt",
    "ProviderActuationClient",
]

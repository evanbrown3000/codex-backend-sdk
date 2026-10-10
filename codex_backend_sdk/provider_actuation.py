"""Singular first-order actuation for subscription-backed .com agents.

This module owns provider mutation and nothing above it.  Schedulers supply an
already-authored prompt and an opaque custody lease.  The actuator delegates to
the recovered B4PT0R ChatGPT mini-loop or to the existing ComputerUseX hosted
provider adapter, normalizes their receipts, commits returned artifacts, and
publishes operation events to Agent Memory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import importlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid

from .agent_memory import AgentMemoryClient
from .attachment_custody import commit_returned_artifact
from .generation_fence import require_generation
from .provider_leases import ProviderLeaseAuthority


PROVIDERS = ("chatgpt.com", "gemini.com", "claude.com", "anthropic.com")
NORMAL_STATES = (
    "accepted", "streaming", "terminal", "artifacts_collected", "provider_error"
)


def normalize_provider(value: str) -> str:
    aliases = {
        "chatgpt": "chatgpt.com", "chatgpt.com": "chatgpt.com",
        "gemini": "gemini.com", "gemini.com": "gemini.com",
        "claude": "claude.com", "claude.com": "claude.com",
        "anthropic": "anthropic.com", "anthropic.com": "anthropic.com",
    }
    try:
        return aliases[value.strip().casefold()]
    except KeyError as exc:
        raise ValueError("unsupported .com provider: " + value) from exc


@dataclass(frozen=True)
class ParentOperation:
    operation_id: str
    automation_order: int
    origin: str
    taskflow_node: str | None = None

    def validate(self) -> None:
        if not self.operation_id or not self.origin:
            raise ValueError("higher-order parent provenance is incomplete")
        if self.automation_order < 2:
            raise ValueError("provider actuation requires order-2-or-higher parent provenance")


@dataclass(frozen=True)
class ProviderPromptRequest:
    provider: str
    prompt: str
    operation_id: str
    capability_lease: str
    conversation_id: str | None = None
    parent_message_id: str | None = None
    mode: str = "create"
    attachments: tuple[str, ...] = ()
    model: str | None = None
    reasoning_effort: str | None = None
    account_id: str | None = None
    artifact_directory: str | None = None
    parent_operation: ParentOperation | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def normalized(self) -> "ProviderPromptRequest":
        provider = normalize_provider(self.provider)
        text = self.prompt.strip()
        if not text:
            raise ValueError("provider prompt must not be empty")
        if self.mode not in {"create", "continue", "resume", "fork"}:
            raise ValueError("mode must be create, continue, resume, or fork")
        if self.mode in {"continue", "resume"} and not self.conversation_id:
            raise ValueError(f"{self.mode} requires conversation_id")
        source_provider = str(self.metadata.get("source_provider") or provider)
        if (
            provider == "chatgpt.com"
            and self.mode in {"continue", "resume"}
            and source_provider == "chatgpt.com"
            and not self.parent_message_id
        ):
            raise ValueError("ChatGPT continuation requires exact parent_message_id from Agent Memory")
        if self.parent_operation is not None:
            self.parent_operation.validate()
        elif os.environ.get("B4PT0R_REQUIRE_HIGHER_ORDER_PARENT", "1") != "0":
            raise ValueError("provider mutation rejected without higher-order parent provenance")
        for path in self.attachments:
            source = Path(path).expanduser()
            if not source.is_file():
                raise ValueError("attachment is not a physical file: " + str(source))
        return ProviderPromptRequest(
            provider=provider,
            prompt=text,
            operation_id=self.operation_id,
            capability_lease=self.capability_lease,
            conversation_id=self.conversation_id,
            parent_message_id=self.parent_message_id,
            mode=self.mode,
            attachments=tuple(str(Path(p).expanduser().resolve()) for p in self.attachments),
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            account_id=self.account_id,
            artifact_directory=self.artifact_directory,
            parent_operation=self.parent_operation,
            metadata=dict(self.metadata),
        )


@dataclass
class ProviderResultEnvelope:
    schema: str
    operation_id: str
    provider: str
    account_id: str
    state: str
    accepted: bool
    terminal: bool
    artifacts_collected: bool
    conversation_id: str | None
    user_message_id: str | None
    assistant_message_id: str | None
    assistant_text: str
    artifacts: list[dict[str, Any]]
    model_receipt: dict[str, Any]
    provider_error: dict[str, Any] | None
    provider_receipt: dict[str, Any]
    event_ids: list[str]
    started_at: str
    completed_at: str
    parent_operation: dict[str, Any] | None
    automation_order: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderAdapter(Protocol):
    def capabilities(self) -> Mapping[str, Any]: ...
    def prompt(self, request: ProviderPromptRequest, custody: Mapping[str, Any]) -> Mapping[str, Any]: ...


class AgentMemoryEventPublisher:
    def __init__(
        self, memory: AgentMemoryClient | None = None,
        outbox: str | Path | None = None,
    ) -> None:
        self.memory = memory or AgentMemoryClient()
        self.outbox = Path(outbox or os.environ.get(
            "B4PT0R_PROVIDER_EVENT_OUTBOX", "/runtime/provider-events/outbox.jsonl"
        ))

    def _publish(
        self, observation: Mapping[str, Any], *, require_admission: bool = False,
    ) -> dict[str, Any]:
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(dict(observation), ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.outbox.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + (float(os.environ.get(
            "B4PT0R_AGENT_MEMORY_ADMISSION_SECONDS", "120"
        )) if require_admission else 0.0)
        last: Exception | None = None
        while True:
            try:
                operational = str(observation.get("schema") or "").startswith("cognilode.provider_")
                receipt = (self.memory.admit_inventory(observation) if operational
                           else self.memory.ingest(observation))
                return {"ingested": True, "receipt": receipt}
            except Exception as exc:
                last = exc
                response = getattr(exc, "response", None)
                status = int(getattr(response, "status_code", 0) or 0)
                # Schema/auth rejection is definitive. Capacity and network
                # failures are retried here so terminal provider completion
                # carries an actual Agent Memory admission receipt rather than
                # merely leaving an outbox entry for a later operator.
                if status and status < 500:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(5)
        return {"ingested": False, "error": type(last).__name__ if last else "admission_failed",
                "outbox": str(self.outbox)}

    def emit(
        self,
        *,
        request: ProviderPromptRequest,
        state: str,
        payload: Mapping[str, Any],
        conversation_id: str | None = None,
    ) -> str:
        if state not in NORMAL_STATES:
            raise ValueError("invalid provider operation state")
        event_id = "provider-event-" + uuid.uuid4().hex
        observed_at = datetime.now(timezone.utc).isoformat()
        observation = {
            "schema": "cognilode.provider_operation_event.v1",
            "observation_kind": "provider_operation_event",
            "event_id": event_id,
            "operation_id": request.operation_id,
            "provider": request.provider,
            "provider_conversation_id": conversation_id,
            "conversation_id": conversation_id or "operation:" + request.operation_id,
            "observed_at": observed_at,
            "state": state,
            "source": {
                "type": "b4pt0r_provider_actuator",
                "provenance": "credential_custody_operation",
            },
            "parent_operation": asdict(request.parent_operation) if request.parent_operation else None,
            "payload": dict(payload),
        }
        self._publish(observation)
        return event_id


def _last_json(stdout: str, stderr: str = "") -> dict[str, Any]:
    for line in reversed((stdout + "\n" + stderr).splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("provider actuator returned no JSON receipt")


class ChatGPTB4PT0RAdapter:
    """Adapter over the recovered send/attachment/SSE/artifact mini-loop."""

    def __init__(self, command: Sequence[str] | None = None) -> None:
        configured = os.environ.get("B4PT0R_CHATGPT_ACTUATOR_COMMAND", "").strip()
        if command is not None:
            self.command = tuple(command)
        elif configured:
            self.command = tuple(json.loads(configured) if configured.startswith("[") else shlex.split(configured))
        else:
            script = Path(__file__).resolve().parent.parent / "scripts" / "cognilode-b4pt0r-chatmode"
            self.command = (sys.executable, str(script))

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider": "chatgpt.com", "create": True, "continue": True,
            "resume": True, "fork": "logical_context_attachment", "attachments": True,
            "terminal_stream": True, "returned_files": True,
            "transport": "b4pt0r_desktop_observed_http_sse",
            "ordinary_browser_observation": False,
        }

    def prompt(self, request: ProviderPromptRequest, custody: Mapping[str, Any]) -> Mapping[str, Any]:
        temporary: tempfile.TemporaryDirectory[str] | None = None
        prompt_text = request.prompt
        conversation_id = request.conversation_id
        parent_message_id = request.parent_message_id
        attachments = list(request.attachments)
        logical_fork = request.mode == "fork" or (
            request.mode == "resume"
            and str(request.metadata.get("source_provider") or "chatgpt.com") != "chatgpt.com"
        )
        if logical_fork and request.conversation_id:
            rendered = AgentMemoryClient(use_broker=False).render_markdown(request.conversation_id)
            temporary = tempfile.TemporaryDirectory(prefix="b4pt0r-logical-fork-")
            context = Path(temporary.name) / "conversation.md"
            context.write_text(rendered, encoding="utf-8")
            attachments.insert(0, str(context))
            prompt_text = "First, read the attached conversation.md in full, then " + prompt_text
            conversation_id = None
            parent_message_id = None
        # Public workforce names are stable across the unified API; translate
        # them only at the provider adapter boundary to ChatGPT's current wire
        # identifiers.  The queue and provenance continue to retain the
        # caller-requested model and effort.
        wire_model = {
            "gpt-5.6-sol": "gpt-5-6-thinking",
        }.get(str(request.model or ""), str(request.model or ""))
        wire_effort = {
            "max": "xhigh",
            "extra-high": "xhigh",
        }.get(str(request.reasoning_effort or ""), str(request.reasoning_effort or "xhigh"))
        args = [*self.command, "--auth-source", "codex", "send", "--prompt", prompt_text,
                "--queue-job-id", request.operation_id,
                "--effort", wire_effort]
        if conversation_id:
            args += ["--conversation-id", conversation_id]
        if parent_message_id:
            args += ["--parent-message-id", parent_message_id]
        if wire_model:
            args += ["--model", wire_model]
        if request.artifact_directory:
            args += ["--output-dir", request.artifact_directory]
        for path in attachments:
            args += ["--attach", path]
        env = os.environ.copy()
        env["B4PT0R_PROVIDER_ACCOUNT_ID"] = str(custody["account_id"])
        env["B4PT0R_CUSTODY_REF"] = str(custody["custody_ref"])
        env["B4PT0R_PROVIDER_CAPABILITY_REDEEMED"] = request.operation_id
        env["B4PT0R_QUEUE_AUTHORIZED_RETRY_ONLY"] = "1"
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True,
                timeout=int(request.metadata.get("timeout_seconds") or 2100),
                env=env, check=False,
            )
            try:
                receipt = _last_json(completed.stdout, completed.stderr)
            except RuntimeError as error:
                # Preserve the process outcome in the central queue event. A
                # bare "no JSON receipt" erased the distinction between an
                # internal exception, a source-generation signal, and a hard
                # runtime exit, forcing repeated foreground diagnosis.
                stderr_tail = completed.stderr[-1000:].replace("\n", " ")
                raise RuntimeError(
                    f"{error}; returncode={completed.returncode}; stderr={stderr_tail}"
                ) from error
            if completed.returncode and not receipt.get("provider_acceptance_observed"):
                receipt.setdefault("ok", False)
            return receipt
        finally:
            if temporary is not None:
                temporary.cleanup()


class ComputerUseXProviderAdapter:
    """Thin adapter to the existing domain-owned Gemini/Claude implementation."""

    def __init__(self, provider: str) -> None:
        self.provider = normalize_provider(provider)

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        # This adapter imports only the provider-facing ComputerUseX surface.
        # ChatGPT browser guard installation belongs to the ChatGPT actuator,
        # not to Gemini/Claude actuation and pulls unrelated optional domains.
        os.environ.setdefault("COMPUTERUSEX_SKIP_IMPORT_GUARDS", "1")
        source = Path(os.environ.get(
            "COMPUTERUSEX_SOURCE", "/runtime/source/current/automation-computeruse-vision/src"
        ))
        legacy = Path("/workspace/cognilode/source/current/automation-computeruse-vision/src")
        local = Path.home() / "Projects/automation-computeruse-vision/src"
        for candidate in (source, legacy, local):
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        module = importlib.import_module("computerusex.web_agent_mcp_server")
        result = module._semantic_call(name, dict(arguments))
        if not isinstance(result, dict):
            raise RuntimeError("ComputerUseX returned a non-object result")
        return result

    def _modified_codex_recovery(self, request: ProviderPromptRequest,
                                 error: BaseException) -> dict[str, Any]:
        """Resume one durable ComputerUseX operator after native actuation fails."""
        source = Path(os.environ.get(
            "COMPUTERUSEX_SOURCE", "/runtime/source/current/automation-computeruse-vision/src"
        ))
        for candidate in (source, Path("/workspace/cognilode/source/current/automation-computeruse-vision/src")):
            if candidate.is_dir() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        dispatch = importlib.import_module("computerusex.agent_dispatch").dispatch_agent
        recovery_root = Path(os.environ.get(
            "COMPUTERUSEX_RECOVERY_ROOT",
            "/home/worker/.local/share/cognilode/computerusex/provider-recovery",
        ))
        recovery_root.mkdir(parents=True, exist_ok=True)
        response_file = recovery_root / f"{request.operation_id}.json"
        attachments = "\n".join(f"- {path}" for path in request.attachments) or "- none"
        goal = (
            "Use the installed ComputerUseX tools to complete this provider operation. "
            "Do not merely describe how to do it. Open the provider, submit the exact prompt, "
            "wait for the terminal response, collect returned files, and write the complete "
            "ComputerUseX HAR when prompting fails, and write the complete "
            f"ComputerUseX provider receipt as JSON to {response_file}.\n"
            f"Provider: {self.provider}\nMode: {request.mode}\n"
            f"Conversation: {request.conversation_id or 'new'}\n"
            f"Native adapter failure: {type(error).__name__}: {error}\n"
            f"Attachments:\n{attachments}\nExact prompt follows:\n{request.prompt}"
        )
        durable_job_id = request.operation_id.split(":lease:", 1)[0]
        recovery = dispatch(
            provider_kind="modified_codex", prompt=goal,
            request_id=f"computerusex-provider-recovery:{durable_job_id}",
            role="provider_operator",
            attachment=str(request.attachments[0]) if request.attachments else "",
            response_file=str(response_file),
            timeout_seconds=int(request.metadata.get("recovery_timeout_seconds", 2100)),
        )
        if response_file.is_file():
            parsed = json.loads(response_file.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                parsed.setdefault("recovery", recovery)
                parsed.setdefault("recovery_transport", "computerusex_modified_codex")
                return parsed
        return {**recovery, "ok": False,
                "status": "modified_codex_recovery_missing_provider_receipt",
                "recovery_transport": "computerusex_modified_codex"}

    def capabilities(self) -> Mapping[str, Any]:
        return {
            "provider": self.provider, "create": True, "continue": True,
            "resume": True, "fork": "logical", "attachments": True,
            "terminal_stream": False, "terminal_collection": True,
            "returned_files": True, "transport": "computerusex_native_web",
            "browser_observation": "mutation_bound_terminal_collection_only",
        }

    def prompt(self, request: ProviderPromptRequest, custody: Mapping[str, Any]) -> Mapping[str, Any]:
        conversation_id = request.conversation_id
        browser_turn = bool(request.attachments or request.metadata.get("browser_transport"))
        # Browser-backed providers create/fork as part of the same visible
        # mutation.  Calling the optional MCP façade first both duplicated the
        # operation and made the core actuator depend on the `mcp` package.
        # Keep that façade only for non-browser transports.
        if not browser_turn and request.mode == "fork" and conversation_id:
            forked = self._call("conversation.fork", {
                "provider": self.provider, "conversation_id": conversation_id,
                "request_id": request.operation_id,
            })
            conversation_id = str(forked.get("conversation_id") or "") or None
        if browser_turn:
            source = Path(os.environ.get(
                "COMPUTERUSEX_SOURCE", "/runtime/source/current/automation-computeruse-vision/src"
            ))
            for candidate in (source, Path("/workspace/cognilode/source/current/automation-computeruse-vision/src"),
                              Path.home() / "Projects/automation-computeruse-vision/src"):
                if candidate.is_dir() and str(candidate) not in sys.path:
                    sys.path.insert(0, str(candidate))
            runtime = importlib.import_module("computerusex.web_agent_runtime")
            try:
                result = runtime.run_web_agent(
                    self.provider,
                    request.prompt,
                    attachment=list(request.attachments),
                    timeout_seconds=float(request.metadata.get("timeout_seconds", 900)),
                    stable_seconds=float(request.metadata.get("stable_seconds", 2.5)),
                    mutation_authority={
                        "route": "b4pt0r-unified-provider",
                        "operation_id": request.operation_id,
                        "custody_ref": custody["custody_ref"],
                        "account_id": custody["account_id"],
                        "lease_id": custody["lease_id"],
                    },
                conversation_url=(conversation_id or "") if request.mode in {"continue", "resume"} else "",
                )
            except Exception as exc:
                return self._modified_codex_recovery(request, exc)
            if result.get("ok") is False:
                return self._modified_codex_recovery(
                    request,
                    RuntimeError(str(result.get("status") or result.get("error") or "native provider failure")),
                )
            urls = list(result.get("discovered_conversation_urls") or [])
            observed_url = str(result.get("observed_url") or "")
            exact_url = next((str(value) for value in reversed(urls) if value), observed_url)
            result.setdefault("conversation_id", exact_url or conversation_id)
            # The browser receipt is the exact mutation identity when a
            # provider does not expose stable message UUIDs in its DOM.
            result.setdefault("user_message_id", str(result.get("run_id") or request.operation_id))
            response_sha = str(result.get("response_sha256") or "")
            if response_sha:
                result.setdefault("assistant_message_id", f"{exact_url or self.provider}#assistant-{response_sha}")
            result.setdefault("complete_response_read", bool(result.get("ok") and result.get("response")))
            submission = result.get("submission") if isinstance(result.get("submission"), Mapping) else {}
            result.setdefault("provider_acceptance_observed", bool(
                submission.get("submit_attempted") and submission.get("provider_visible_reconciliation")
            ))
            result["downloaded_files"] = list(result.get("response_downloaded_files") or [])
            return result
        result = self._call("conversation.prompt", {
            "provider": self.provider, "conversation_id": conversation_id,
            "prompt": request.prompt, "request_id": request.operation_id,
            "transport": "auto",
            "timeout_seconds": request.metadata.get("timeout_seconds", 900),
        })
        return result


class UnifiedProviderActuator:
    def __init__(
        self,
        *,
        lease_authority: ProviderLeaseAuthority,
        publisher: AgentMemoryEventPublisher | None = None,
        adapters: Mapping[str, ProviderAdapter] | None = None,
    ) -> None:
        self.leases = lease_authority
        self.publisher = publisher or AgentMemoryEventPublisher()
        self.adapters: dict[str, ProviderAdapter] = {
            "chatgpt.com": ChatGPTB4PT0RAdapter(),
            "gemini.com": ComputerUseXProviderAdapter("gemini.com"),
            "claude.com": ComputerUseXProviderAdapter("claude.com"),
            "anthropic.com": ComputerUseXProviderAdapter("anthropic.com"),
        }
        self.adapters.update(dict(adapters or {}))

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema": "cognilode.provider_capabilities.v1",
            "providers": {name: dict(adapter.capabilities()) for name, adapter in self.adapters.items()},
        }

    def publish_capabilities(self) -> dict[str, Any]:
        value = self.capabilities()
        receipts = {
            provider: self.publisher._publish({
                "schema": value["schema"],
                "observation_kind": "provider_capability_inventory",
                "provider": provider,
                "conversation_id": "inventory:provider-capabilities:" + provider,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": {"type": "b4pt0r_provider_actuator", "provenance": "runtime_capabilities"},
                "capabilities": capabilities,
            })
            for provider, capabilities in value["providers"].items()
        }
        return {**value, "agent_memory": receipts}

    def prompt(self, request: ProviderPromptRequest) -> ProviderResultEnvelope:
        request = request.normalized()
        require_generation({"capability": request.capability_lease})
        started = datetime.now(timezone.utc).isoformat()
        event_ids: list[str] = []
        custody = self.leases.redeem(
            request.capability_lease, operation_id=request.operation_id, operation="prompt"
        )
        if request.account_id and custody["account_id"] != request.account_id:
            raise ValueError("leased account differs from requested account")
        adapter = self.adapters[request.provider]
        try:
            raw = dict(adapter.prompt(request, custody))
            accepted = bool(raw.get("provider_acceptance_observed", raw.get("ok")))
            terminal = bool(raw.get("assistant_terminal") or raw.get("terminal")
                            or raw.get("complete_response_read")
                            or raw.get("state") in {"completed", "response_collected"})
            conversation_id = str(raw.get("conversation_id") or request.conversation_id or "") or None
            user_message_id = str(raw.get("user_message_id") or raw.get("request_id") or "") or None
            assistant_message_id = str(
                raw.get("terminal_assistant_message_id") or raw.get("assistant_message_id") or ""
            ) or None
            assistant_text = str(
                raw.get("terminal_assistant_text") or raw.get("assistant_text")
                or raw.get("response") or ""
            )
            if accepted:
                event_ids.append(self.publisher.emit(
                    request=request, state="accepted",
                    payload={
                        "account_id": custody["account_id"],
                        "lease_id": custody["lease_id"],
                    }, conversation_id=conversation_id,
                ))
            stream_observed = bool(
                raw.get("raw_bytes") or raw.get("provider_summary") or raw.get("stream")
                or raw.get("complete_response_read") or assistant_text
            )
            if accepted and stream_observed:
                event_ids.append(self.publisher.emit(
                    request=request, state="streaming",
                    payload={"transport": adapter.capabilities().get("transport")},
                    conversation_id=conversation_id,
                ))
            artifacts: list[dict[str, Any]] = []
            candidates = (raw.get("downloaded_files") or raw.get("response_downloaded_files")
                          or raw.get("response_downloads") or raw.get("artifacts") or [])
            for item in candidates if isinstance(candidates, list) else []:
                row = dict(item) if isinstance(item, Mapping) else {"path": str(item)}
                path = str(row.get("path") or "")
                if path and Path(path).is_file():
                    try:
                        row["custody"] = commit_returned_artifact(path)
                        row["custody_state"] = "committed"
                    except Exception as exc:
                        row["custody_state"] = "pending"
                        row["custody_error"] = type(exc).__name__
                artifacts.append(row)
            if terminal:
                event_ids.append(self.publisher.emit(
                    request=request, state="terminal",
                    payload={
                        "user_message_id": user_message_id,
                        "assistant_message_id": assistant_message_id,
                        "assistant_sha256": hashlib.sha256(assistant_text.encode()).hexdigest(),
                    },
                    conversation_id=conversation_id,
                ))
            if artifacts:
                event_ids.append(self.publisher.emit(
                    request=request, state="artifacts_collected",
                    payload={"artifacts": artifacts}, conversation_id=conversation_id,
                ))
            error = None
            state = "artifacts_collected" if artifacts else "terminal" if terminal else "accepted"
            if not accepted:
                state = "provider_error"
                error = {
                    "code": str(raw.get("state") or "provider_not_accepted"),
                    "message": str(raw.get("error") or raw.get("error_preview") or "provider did not accept turn"),
                }
                event_ids.append(self.publisher.emit(
                    request=request, state="provider_error", payload=error,
                    conversation_id=conversation_id,
                ))
            envelope = ProviderResultEnvelope(
                schema="cognilode.provider_result.v1",
                operation_id=request.operation_id,
                provider=request.provider,
                account_id=str(custody["account_id"]),
                state=state,
                accepted=accepted,
                terminal=terminal,
                artifacts_collected=bool(artifacts),
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                assistant_message_id=assistant_message_id,
                assistant_text=assistant_text,
                artifacts=artifacts,
                model_receipt=dict(raw.get("model_receipt") or raw.get("provider_model_receipt") or {}),
                provider_error=error,
                provider_receipt=raw,
                event_ids=event_ids,
                started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
                parent_operation=asdict(request.parent_operation) if request.parent_operation else None,
            )
            # A prepare failure happened before a provider conversation or user
            # message existed.  It is already durably emitted as an operational
            # provider_error event above; fabricating an empty conversation and
            # waiting through the required-admission window only strands the
            # transport generation and delays the safe retry containing the
            # source fix.  Conversation admission begins at provider acceptance.
            if accepted or conversation_id or user_message_id:
                publication = self.publisher._publish({
                    "schema": "memory_stock.conversation_observation.v1",
                    "provider": request.provider,
                    "provider_conversation_id": conversation_id,
                    "conversation_id": conversation_id or "operation:" + request.operation_id,
                    "provider_user_message_id": user_message_id,
                    "provider_account_id": str(custody["account_id"]),
                    "operation_id": request.operation_id,
                    "observed_at": envelope.completed_at,
                    "source": {"type": "b4pt0r_provider_actuator", "provenance": "normalized_result"},
                    "messages": [
                        {"id": user_message_id, "author": {"role": "user"},
                         "content": {"content_type": "text", "parts": [request.prompt]}},
                        {"id": assistant_message_id, "author": {"role": "assistant"},
                         "content": {"content_type": "text", "parts": [assistant_text]}},
                    ],
                    "attachments": list(request.attachments),
                    "artifacts": artifacts,
                    "terminal": terminal,
                    "normalized_provider_result": envelope.to_dict(),
                }, require_admission=True)
            else:
                publication = {
                    "ingested": False,
                    "state": "preaccept_operational_event_only",
                    "event_ids": list(event_ids),
                }
            envelope.provider_receipt["agent_memory_publication"] = publication
            return envelope
        except Exception as exc:
            error = {"code": type(exc).__name__, "message": str(exc)}
            try:
                event_ids.append(self.publisher.emit(
                    request=request, state="provider_error", payload=error,
                    conversation_id=request.conversation_id,
                ))
            except Exception:
                pass
            return ProviderResultEnvelope(
                schema="cognilode.provider_result.v1", operation_id=request.operation_id,
                provider=request.provider, account_id=str(custody["account_id"]),
                state="provider_error", accepted=False, terminal=False,
                artifacts_collected=False, conversation_id=request.conversation_id,
                user_message_id=None, assistant_message_id=None, assistant_text="",
                artifacts=[], model_receipt={}, provider_error=error,
                provider_receipt={}, event_ids=event_ids, started_at=started,
                completed_at=datetime.now(timezone.utc).isoformat(),
                parent_operation=asdict(request.parent_operation) if request.parent_operation else None,
            )


_DEFAULT_ACTUATOR: UnifiedProviderActuator | None = None


def configure_default_actuator(actuator: UnifiedProviderActuator) -> None:
    global _DEFAULT_ACTUATOR
    _DEFAULT_ACTUATOR = actuator


def prompt(
    provider: str,
    conversation: str | None,
    prompt: str,
    attachments: Sequence[str] = (),
    **options: Any,
) -> dict[str, Any]:
    """Public ``prompt(provider, conversation, prompt, attachments)`` operation."""
    if _DEFAULT_ACTUATOR is None:
        raise RuntimeError("provider actuator is not configured inside credential custody")
    parent = options.pop("parent_operation", None)
    if isinstance(parent, Mapping):
        parent = ParentOperation(**dict(parent))
    request = ProviderPromptRequest(
        provider=provider,
        conversation_id=conversation,
        prompt=prompt,
        attachments=tuple(attachments),
        parent_operation=parent,
        **options,
    )
    return _DEFAULT_ACTUATOR.prompt(request).to_dict()


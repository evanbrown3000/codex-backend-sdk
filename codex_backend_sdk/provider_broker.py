"""Credential-custody operation endpoint for the B4PT0R provider client.

The process is launched by the custody owner after it has materialized a
lease-scoped ``CODEX_HOME`` and connected the mandatory HTTP gate.  Callers
send one JSON operation on stdin and receive one JSON result on stdout; no
provider credential is returned across this boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping
import uuid
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import OpenAI
from .agent_memory import AgentMemoryClient
from .attachment_custody import commit_returned_artifact
from .provider_actuation import (
    AgentMemoryEventPublisher,
    ParentOperation,
    ProviderPromptRequest,
    UnifiedProviderActuator,
)
from .provider_leases import ProviderLeaseAuthority


def _operator_token() -> str:
    path = Path(
        os.environ.get(
            "COGNILODE_OPERATOR_TOKEN_FILE",
            "/home/worker/.config/cognilode/operator-bearer",
        )
    )
    return path.read_text(encoding="utf-8").strip().removeprefix("Bearer ").strip()


def _relay_request(url: str, method: str, body: Any = None, params: Any = None) -> Any:
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Accept": "application/json", "Authorization": "Bearer " + _operator_token()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=180) as response:
        return json.load(response)


def _lease_authority() -> ProviderLeaseAuthority:
    path = Path(os.environ.get(
        "B4PT0R_PROVIDER_LEASE_DB",
        "/runtime/custody/provider-capability-leases.sqlite3",
    ))
    authority = ProviderLeaseAuthority(path)
    descriptor: Any = None
    configured = os.environ.get("B4PT0R_PROVIDER_ACCOUNTS_JSON", "").strip()
    descriptor_path = os.environ.get("B4PT0R_PROVIDER_ACCOUNTS_FILE", "").strip()
    if configured:
        descriptor = json.loads(configured)
    elif descriptor_path and Path(descriptor_path).is_file():
        descriptor = json.loads(Path(descriptor_path).read_text(encoding="utf-8"))
    if not descriptor:
        descriptor = [
            {
                "provider": "chatgpt.com",
                "account_id": os.environ.get("B4PT0R_CHATGPT_ACCOUNT_ID", "central-custody"),
                "custody_ref": "runtime:codex-home",
                "capabilities": {
                    "attachments": True, "artifact_downloads": True,
                    "continuation": True,
                    "models": ["gpt-5-6-thinking", "gpt-5.6", "gpt-5.6-sol", "auto"],
                    "reasoning_modes": ["medium", "high", "xhigh", "max", "extended"],
                },
            },
        ] + [
            {
                "provider": provider, "account_id": "primary",
                "custody_ref": "runtime:managed-browser-profile",
                "capabilities": {
                    "attachments": True, "artifact_downloads": False,
                    "continuation": True, "models": [], "reasoning_modes": [],
                },
            }
            for provider in ("gemini.com", "claude.com", "anthropic.com")
        ]
    rows = descriptor.get("accounts", []) if isinstance(descriptor, Mapping) else descriptor
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        authority.register_account(
            provider=str(row.get("provider") or ""),
            account_id=str(row.get("account_id") or ""),
            custody_ref=str(row.get("custody_ref") or ""),
            capabilities=row.get("capabilities") if isinstance(row.get("capabilities"), Mapping) else {},
        )
    return authority


def _actuator(authority: ProviderLeaseAuthority) -> UnifiedProviderActuator:
    return UnifiedProviderActuator(
        lease_authority=authority,
        publisher=AgentMemoryEventPublisher(AgentMemoryClient(use_broker=False, timeout=20)),
    )


def _publish_lease(lease: Any) -> dict[str, Any]:
    observation = {
        "schema": "cognilode.provider_credential_lease_event.v1",
        "observation_kind": "provider_credential_lease",
        "conversation_id": "operation:" + lease.operation_id,
        "operation_id": lease.operation_id,
        "provider": lease.provider,
        "account_id": lease.account_id,
        "lease": lease.public(),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": {"type": "b4pt0r_provider_custody", "provenance": "capability_issued"},
    }
    try:
        return AgentMemoryClient(use_broker=False, timeout=10).ingest(observation)
    except Exception as exc:
        return {"ingested": False, "error": type(exc).__name__}


def _artifact_rows(turn: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = turn.get("artifacts")
    rows = [dict(row) for row in value] if isinstance(value, list) else []
    for row in rows:
        path = Path(str(row.get("path") or ""))
        if row.get("ref") or not path.is_file():
            continue
        try:
            receipt = commit_returned_artifact(path)
        except Exception as exc:
            row["custody_state"] = "pending"
            row["custody_error"] = type(exc).__name__
        else:
            row.update(receipt)
            row["custody_state"] = "committed"
    return rows


def _send(payload: Mapping[str, Any]) -> dict[str, Any]:
    client = OpenAI().authenticate()
    attachments = [str(value) for value in payload.get("attachments") or []]
    prompt = str(payload.get("prompt") or "")
    memory = AgentMemoryClient(use_broker=False)
    foreground: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="b4pt0r-operation-") as directory:
        root = Path(directory)
        memory_enabled = str(payload.get("memory_context", "required")).casefold() not in {
            "0", "false", "none", "off", "disabled",
        }
        if memory_enabled:
            try:
                foreground = memory.prompt_foreground(
                persona=str(payload.get("memory_persona") or payload.get("role") or "company"),
                    max_tokens=int(payload.get("memory_max_tokens") or 40_000),
                task=prompt,
                )
            except Exception as exc:
                # Prompt transport is the availability boundary.  Memory is an
                # enrichment layer and records its own liveness; an unavailable
                # foreground projection must not turn an otherwise valid,
                # rhythm-fenced provider operation into a permanent outage.
                foreground = {"state": "unavailable", "error": type(exc).__name__}
            else:
                foreground_path = root / "foreground.md"
                foreground_path.write_text(str(foreground["context"]), encoding="utf-8")
                actual = hashlib.sha256(foreground_path.read_bytes()).hexdigest()
                if actual != str(foreground.get("content_sha256") or ""):
                    raise RuntimeError("Agent Memory foreground content identity mismatch")
                attachments.insert(0, str(foreground_path))
        if payload.get("rendered_conversation"):
            rendered = root / "conversation.md"
            rendered.write_text(str(payload["rendered_conversation"]), encoding="utf-8")
            attachments.insert(0, str(rendered))
            prompt = "First, read the attached conversation.md in full, then " + prompt
        result = client.chatgpt.operations.send(
            prompt,
            conversation_id=payload.get("conversation_id"),
            parent_message_id=payload.get("parent_message_id"),
            model=str(payload.get("model") or "gpt-5-6-thinking"),
            effort=str(payload.get("effort") or "xhigh"),
            attachment_paths=attachments,
            user_message_id=payload.get("user_message_id"),
            readback=True,
            artifact_directory=payload.get("artifact_directory") or root / "artifacts",
        )
        turn = result.get("turn") if isinstance(result.get("turn"), Mapping) else {}
        artifact_rows = _artifact_rows(turn)
        try:
            memory_receipt = memory.record_chatgpt_turn(
                conversation_id=str(result.get("conversation_id") or ""),
                user_message_id=str(result.get("user_message_id") or ""),
                assistant_message_id=str(turn.get("assistant_message_id") or ""),
                prompt=prompt,
                response=str(turn.get("assistant_text") or ""),
                attachments=[dict(row) for row in result.get("attachments") or []],
                artifacts=artifact_rows,
                provider_receipt=turn.get("model_receipt") or result.get("stream") or {},
            )
        except Exception as exc:
            memory_receipt = {"ingested": False, "error": type(exc).__name__, "message": str(exc)}
        summary_admission: dict[str, Any] | None = None
        summary_job_ids = re.findall(r"(?m)^- Summary job:\s*(\S+)\s*$", prompt)
        assistant_text = str(turn.get("assistant_text") or "").strip()
        summary_attestation = "read_every_source_fragment_in_full_without_skipping"
        if (
            len(summary_job_ids) == 1
            and assistant_text
            and summary_attestation in assistant_text
        ):
            try:
                admitted = memory.admit_summary_result(
                    job_id=summary_job_ids[0],
                    summary=assistant_text,
                    worker_conversation_url=(
                        "https://chatgpt.com/c/" + str(result.get("conversation_id") or "")
                    ),
                    source_ref=(
                        "b4pt0r-provider-terminal:"
                        + str(result.get("user_message_id") or summary_job_ids[0])
                    ),
                )
                summary_admission = {"ok": True, "result": admitted}
            except Exception as exc:
                summary_admission = {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "job_id": summary_job_ids[0],
                }
        return {
            "ok": True,
            "conversation_id": result.get("conversation_id"),
            "user_message_id": result.get("user_message_id"),
            "assistant_message_id": turn.get("assistant_message_id"),
            "assistant_text": turn.get("assistant_text") or "",
            "provider_receipt": turn.get("model_receipt") or result.get("stream") or {},
            "artifacts": artifact_rows,
            "agent_memory": memory_receipt,
            "summary_admission": summary_admission,
            "memory_foreground": {
                key: foreground.get(key)
                for key in ("persona_id", "content_sha256", "selected_tokens", "foreground_handle", "selected")
                if foreground.get(key) is not None
            },
        }


def execute(payload: Mapping[str, Any]) -> Any:
    operation = str(payload.get("operation") or "")
    if operation == "managed_control_request":
        # Strict non-provider control-plane dispatch. It executes directly
        # with the custody bearer and never enters the provider queue/actuator.
        action = str(payload.get("action") or "")
        actor_id = str(payload.get("actor_id") or "default")
        args = payload.get("arguments")
        if not isinstance(args, Mapping):
            return {"ok": False, "error": "managed_control_arguments_must_be_object"}
        token = _operator_token()
        if not token:
            return {"ok": False, "error": "operator_custody_capability_unavailable"}
        if action in {"conversation_list", "conversation_search", "conversation_read"}:
            memory = AgentMemoryClient(token=token, timeout=45, use_broker=False)
            if action == "conversation_list":
                result = memory.list(limit=min(100, max(1, int(args.get("limit", 50)))),
                                     cursor=str(args["cursor"]) if args.get("cursor") else None,
                                     provider=str(args.get("provider") or "chatgpt.com"))
            elif action == "conversation_search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return {"ok": False, "error": "conversation_search_query_required"}
                result = memory.search(query, limit=min(100, max(1, int(args.get("limit", 50)))),
                                       provider=str(args.get("provider") or "chatgpt.com"))
            else:
                conversation_id = str(args.get("conversation_id") or "").strip()
                if not conversation_id:
                    return {"ok": False, "error": "conversation_read_id_required"}
                result = memory.read(
                    conversation_id, reader_id=str(args.get("reader_id") or actor_id),
                    after=str(args["after"]) if args.get("after") else None,
                    full=bool(args.get("full", False)), peek=bool(args.get("peek", False)),
                    projection=str(args.get("projection") or "read-model"))
            return {"ok": True, "action": action, "result": result}
        if action in {"environment_list", "environment_current", "environment_select"}:
            remote = RemoteShellClient(token=token, actor_id=actor_id, timeout=45)
            if action == "environment_list":
                result = remote.environments()
            elif action == "environment_current":
                # Do not let the SDK's interactive process cache masquerade
                # as a durable registry selection in this evidence path.
                result = remote.call("environment_current", {"actor_id": actor_id})
            else:
                environment_id = str(args.get("environment_id") or "").strip()
                if not environment_id:
                    return {"ok": False, "error": "environment_select_id_required"}
                result = remote.select(environment_id)
            return {"ok": True, "action": action, "actor_id": actor_id, "result": result}
        return {"ok": False, "error": "unsupported_managed_control_action"}
    if operation == "embedding":
        texts = payload.get("input")
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not texts or len(texts) > 128:
            return {"ok": False, "error": "embedding_input_must_contain_1_to_128_texts"}
        normalized = [str(value) for value in texts]
        if sum(len(value) for value in normalized) > 1_000_000:
            return {"ok": False, "error": "embedding_input_too_large"}
        model = str(payload.get("model") or "text-embedding-3-small")
        client = OpenAI().authenticate()
        response = client.embeddings.create(
            input=normalized,
            model=model,
            dimensions=payload.get("dimensions") or 1536,
        )
        return {
            "ok": True,
            "model": model,
            "vectors": [list(row.embedding) for row in response.data],
            "usage": response.usage.model_dump() if response.usage is not None else {},
        }
    if operation == "provider_capabilities":
        authority = _lease_authority()
        actuator = _actuator(authority)
        capabilities = actuator.capabilities()
        capabilities["accounts"] = {
            provider: authority.accounts(provider)
            for provider in ("chatgpt.com", "gemini.com", "claude.com", "anthropic.com")
        }
        capabilities["agent_memory"] = {
            provider: actuator.publisher._publish({
                "schema": "cognilode.provider_capabilities.v1",
                "observation_kind": "provider_capability_inventory",
                "provider": provider,
                "conversation_id": "inventory:provider-capabilities:" + provider,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "source": {"type": "b4pt0r_provider_custody", "provenance": "runtime_capabilities"},
                "capabilities": capabilities["providers"][provider],
                "accounts": capabilities["accounts"][provider],
            })
            for provider in capabilities["providers"]
        }
        return capabilities
    if operation == "provider_lease_issue":
        lease = _lease_authority().issue(
            provider=str(payload.get("provider") or ""),
            account_id=str(payload.get("account_id") or "primary"),
            operation_id=str(payload.get("operation_id") or ""),
            operation="prompt",
            ttl_seconds=int(payload.get("ttl_seconds") or 120),
        )
        memory_receipt = _publish_lease(lease)
        return {"ok": True, "lease": {**lease.public(), "capability": lease.token},
                "agent_memory": memory_receipt}
    if operation == "provider_prompt_with_lease":
        operation_id = str(payload.get("operation_id") or uuid.uuid4())
        provider = str(payload.get("provider") or "")
        account_id = str(payload.get("account_id") or "primary")
        authority = _lease_authority()
        lease = authority.issue(
            provider=provider, account_id=account_id,
            operation_id=operation_id, operation="prompt",
            ttl_seconds=int(payload.get("ttl_seconds") or 180),
        )
        _publish_lease(lease)
        forwarded = dict(payload)
        forwarded.update({
            "operation": "provider_prompt", "operation_id": operation_id,
            "capability_lease": lease.token, "account_id": account_id,
        })
        parent_value = forwarded.get("parent_operation")
        parent = ParentOperation(**dict(parent_value)) if isinstance(parent_value, Mapping) else None
        request = ProviderPromptRequest(
            provider=provider, prompt=str(forwarded.get("prompt") or ""),
            operation_id=operation_id, capability_lease=lease.token,
            conversation_id=str(forwarded.get("conversation_id") or "") or None,
            parent_message_id=str(forwarded.get("parent_message_id") or "") or None,
            mode=str(forwarded.get("mode") or ("continue" if forwarded.get("conversation_id") else "create")),
            attachments=tuple(str(value) for value in forwarded.get("attachments") or ()),
            model=str(forwarded.get("model") or "") or None,
            reasoning_effort=str(forwarded.get("reasoning_effort") or forwarded.get("effort") or "") or None,
            account_id=account_id,
            artifact_directory=str(forwarded.get("artifact_directory") or "") or None,
            parent_operation=parent,
            metadata=forwarded.get("metadata") if isinstance(forwarded.get("metadata"), Mapping) else {},
        )
        return _actuator(authority).prompt(request).to_dict()
    if operation == "provider_prompt":
        authority = _lease_authority()
        parent_value = payload.get("parent_operation")
        parent = ParentOperation(**dict(parent_value)) if isinstance(parent_value, Mapping) else None
        request = ProviderPromptRequest(
            provider=str(payload.get("provider") or ""),
            prompt=str(payload.get("prompt") or ""),
            operation_id=str(payload.get("operation_id") or ""),
            capability_lease=str(payload.get("capability_lease") or ""),
            conversation_id=str(payload.get("conversation_id") or "") or None,
            parent_message_id=str(payload.get("parent_message_id") or "") or None,
            mode=str(payload.get("mode") or ("continue" if payload.get("conversation_id") else "create")),
            attachments=tuple(str(value) for value in payload.get("attachments") or ()),
            model=str(payload.get("model") or "") or None,
            reasoning_effort=str(payload.get("reasoning_effort") or payload.get("effort") or "") or None,
            account_id=str(payload.get("account_id") or "") or None,
            artifact_directory=str(payload.get("artifact_directory") or "") or None,
            parent_operation=parent,
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {},
        )
        return _actuator(authority).prompt(request).to_dict()
    if operation == "agent_memory_request":
        explicit_url = str(payload.get("url") or "").strip()
        base = os.environ.get(
            "AGENT_MEMORY_ENDPOINT", "https://cognilode.com/api/operator/agent-memory"
        ).rstrip("/")
        return _relay_request(
            explicit_url or (base + "/" + str(payload.get("path") or "").lstrip("/")),
            str(payload.get("method") or "GET"),
            payload.get("body"),
            payload.get("params"),
        )
    if operation == "remote_shell_request":
        body = {
            "jsonrpc": "2.0",
            "id": "b4pt0r-broker",
            "method": "tools/call",
            "params": {
                "name": payload.get("name"),
                "arguments": payload.get("arguments") or {},
            },
        }
        raw = _relay_request(
            os.environ.get(
                "COGNILODE_REMOTE_SHELL_ENDPOINT",
                "https://cognilode.com/api/operator/remote-shell/mcp",
            ),
            "POST",
            body,
        )
        result = raw.get("result", raw) if isinstance(raw, Mapping) else raw
        if isinstance(result, Mapping) and result.get("structuredContent") is not None:
            return result["structuredContent"]
        return result
    if operation == "chatgpt_continue":
        if os.environ.get("B4PT0R_ALLOW_LEGACY_DIRECT_PROVIDER") == "1":
            return _send(payload)
        return {"ok": False, "error": "provider_prompt_requires_capability_lease_and_parent_provenance"}
    if operation == "conversation_continue":
        if payload.get("context_delivery") != "rendered_conversation_attachment":
            return {"ok": False, "error": "conversation continuation requires rendered attachment"}
        if not payload.get("rendered_conversation"):
            memory = AgentMemoryClient(use_broker=False)
            payload = dict(payload)
            payload["rendered_conversation"] = memory.render_markdown(
                str(payload.get("source_conversation_id") or "")
            )
        if os.environ.get("B4PT0R_ALLOW_LEGACY_DIRECT_PROVIDER") == "1":
            return _send(payload)
        return {"ok": False, "error": "provider_prompt_requires_capability_lease_and_parent_provenance"}
    return {"ok": False, "error": "unsupported broker operation: " + operation}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.readline())
        result = execute(payload)
    except Exception as exc:
        result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if not isinstance(result, Mapping) or result.get("ok") is not False else 2


if __name__ == "__main__":
    raise SystemExit(main())

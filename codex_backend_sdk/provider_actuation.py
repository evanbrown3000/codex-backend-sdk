"""Singular first-order actuation for subscription-backed .com agents.

This module owns provider mutation and nothing above it.  Schedulers supply an
already-authored prompt and an opaque custody lease.  The actuator delegates to
the recovered B4PT0R ChatGPT mini-loop or to the existing ComputerUseX hosted
provider adapter, normalizes their receipts, commits returned artifacts, and
publishes operation events to Agent Memory.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import http.cookiejar
import importlib
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
import uuid
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from .agent_memory import AgentMemoryClient
from .attachment_custody import commit_returned_artifact
from .generation_fence import require_generation
from .provider_leases import ProviderLeaseAuthority


PROVIDERS = ("chatgpt.com", "gemini.com", "claude.com", "anthropic.com", "codex.research")
NORMAL_STATES = (
    "accepted", "streaming", "terminal", "artifacts_collected", "provider_error"
)


def _verified_artifact(row: Mapping[str, Any]) -> bool:
    """An anchor, failed download or uncommitted path is not a returned file."""
    if row.get("downloaded") is False or row.get("custody_state") == "pending":
        return False
    receipt = row.get("custody")
    if isinstance(receipt, Mapping):
        return bool(receipt.get("ref") and receipt.get("sha256")
                    and int(receipt.get("size") or 0) > 0)
    if row.get("ref") and row.get("sha256"):
        # Includes pre-committed B4PT0R/Universe Storage receipts.
        return int(row.get("size") or row.get("bytes") or 0) > 0
    # A provider adapter may have independently read back a Drive-custodied
    # artifact, without retaining an extra local binary copy.
    if receipt == "google_drive" and row.get("uri"):
        return bool(row.get("sha256") and int(row.get("bytes") or 0) > 0)
    return False


def _semantic_result_defects(
    *, request: "ProviderPromptRequest", raw: Mapping[str, Any],
    terminal: bool, assistant_text: str, artifacts: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Consumer contract is independent of provider transport acceptance.

    Optional result_policy is explicit, not guessed from prose or a file
    extension appearing somewhere in a research prompt.
    """
    policy = request.metadata.get("result_policy")
    if not isinstance(policy, Mapping):
        policy = request.metadata
    defects: list[str] = []
    # Streaming/accepted-only receipts are not failed terminal deliverables.
    # Preserve their active fence; evaluate quality only at claimed terminal.
    if not terminal:
        return defects
    if not assistant_text.strip():
        defects.append("missing_substantive_terminal_assistant")
    minimum_chars = int(policy.get("minimum_assistant_chars") or 0)
    if minimum_chars > 0 and len(assistant_text.strip()) < minimum_chars:
        defects.append("assistant_below_declared_minimum")
    minimum_elapsed = float(policy.get("minimum_elapsed_seconds") or 0)
    if minimum_elapsed > 0:
        observed = raw.get("elapsed_seconds")
        if observed is None:
            observed = raw.get("provider_elapsed_seconds")
        if observed is None or float(observed) < minimum_elapsed:
            defects.append("provider_work_below_declared_minimum")
    expected = policy.get("required_artifacts") or policy.get("required_output_names") or ()
    if isinstance(expected, (str, Mapping)):
        expected = (expected,)
    for requirement in expected:
        if isinstance(requirement, Mapping):
            name = Path(str(requirement.get("filename") or requirement.get("name") or "")).name
            min_bytes = max(1, int(requirement.get("min_bytes") or 1))
        else:
            name = Path(str(requirement)).name
            min_bytes = 1
        if not name:
            defects.append("invalid_required_artifact_identity")
            continue
        matching = [row for row in artifacts
                    if Path(str(row.get("filename") or row.get("name")
                                or row.get("path") or "")).name == name]
        def recorded_size(row):
            custody = row.get("custody")
            size = row.get("bytes") or row.get("size")
            if not size and isinstance(custody, Mapping):
                size = custody.get("size")
            return int(size or 0)
        if not any(recorded_size(row) >= min_bytes for row in matching):
            defects.append("required_artifact_missing_or_too_small:" + name)
    if request.provider in {"gemini.com", "claude.com", "anthropic.com"}:
        turn = raw.get("user_message_identity")
        if isinstance(turn, Mapping) and turn.get("expected_text_match") is False:
            defects.append("submitted_prompt_not_observed")
        seen = raw.get("assistant_message_identity")
        if isinstance(seen, Mapping) and seen.get("observed") is False:
            defects.append("assistant_turn_not_observed")
        if request.attachments:
            uploaded = raw.get("attachment")
            if isinstance(uploaded, Mapping) and uploaded.get("observed_names") is not None:
                names = {str(n).casefold() for n in uploaded.get("observed_names") or ()}
                if not all(Path(f).name.casefold() in names for f in request.attachments):
                    defects.append("input_attachments_not_all_observed")
    return defects


def normalize_provider(value: str) -> str:
    aliases = {
        "chatgpt": "chatgpt.com", "chatgpt.com": "chatgpt.com",
        "gemini": "gemini.com", "gemini.com": "gemini.com",
        "claude": "claude.com", "claude.com": "claude.com",
        "anthropic": "anthropic.com", "anthropic.com": "anthropic.com",
        "codex": "codex.research", "codex.research": "codex.research",
    }
    try:
        return aliases[value.strip().casefold()]
    except KeyError as exc:
        raise ValueError("unsupported provider: " + value) from exc


@dataclass(frozen=True)
class ParentOperation:
    operation_id: str
    automation_order: int
    origin: str
    taskflow_node: str | None = None

    def validate(self) -> None:
        if not self.operation_id or not self.origin:
            raise ValueError("higher-order parent provenance is incomplete")
        # A direct, explicitly user-authorized first-order provider operation
        # is not a queue/RPE/TaskFlow child and must not be forced to invent a
        # higher-order parent.  Keep the exception narrow and explicit; every
        # other provider mutation retains the established order-2+ fence.
        if self.automation_order == 1 and self.origin == "user_explicit_first_order":
            if self.taskflow_node:
                raise ValueError("first-order provider authorization cannot name a TaskFlow node")
            return
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


class InstalledCodexResearchAdapter:
    """Run the managed Modified Codex release through the existing exec broker."""

    def capabilities(self) -> Mapping[str, Any]:
        return {"provider": "codex.research", "create": True, "continue": True,
                "resume": True, "terminal_stream": True, "returned_files": False,
                "transport": "managed_modified_codex_exec_broker",
                "system_instructions": "replaced_with_exact_blank",
                "developer_instructions": "replaced_with_exact_blank"}

    @staticmethod
    def _event_text(value: Mapping[str, Any]) -> str:
        item = value.get("item") if isinstance(value.get("item"), Mapping) else {}
        if str(item.get("type") or "") not in {"agent_message", "assistant_message"}:
            return ""
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(part.get("text") or "") for part in content
                           if isinstance(part, Mapping))
        return str(item.get("text") or "")

    def prompt(self, request: ProviderPromptRequest, custody: Mapping[str, Any]) -> Mapping[str, Any]:
        if request.conversation_id:
            raise ValueError("Codex exec resume requires a provider-native Codex thread identity")
        model = str(request.model or "gpt-6-luna")
        effort = str(request.reasoning_effort or "medium")
        # Always pass both instruction replacements. These exact TOML empty
        # strings override any system/developer content in the installed
        # Modified Codex profile rather than inheriting machine defaults.
        script = Path(__file__).resolve().parents[1] / "scripts/cognilode-codex-exec-client"
        if not script.is_file():
            script = Path("/runtime/source/current/codex-backend-sdk/scripts/cognilode-codex-exec-client")
        output_dir = Path("/runtime/worker/codex-research")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / (hashlib.sha256(request.operation_id.encode()).hexdigest() + ".txt")
        # ``--ask-for-approval`` is a global Modified Codex option.  The
        # current managed binary no longer accepts ``-a`` after the ``exec``
        # subcommand, so keep it before ``exec`` while the custody broker
        # continues to enforce the exact ``never`` value.
        args = [sys.executable, str(script), "-a", "never", "exec", "--json", "-m", model,
                "-C", "/runtime", "-o", str(output_file), "-s", "read-only",
                "-c", 'model_reasoning_effort="' + effort + '"',
                "-c", 'model_instructions=""',
                "-c", 'developer_instructions=""',
                "--skip-git-repo-check", "-"]
        env = os.environ.copy()
        environment_id = str(request.metadata.get("environment_id") or "").strip()
        if environment_id:
            env["COGNILODE_DEFAULT_REMOTE_ENVIRONMENT"] = environment_id
        env["COGNILODE_CODEX_EXEC_TIMEOUT"] = str(int(request.metadata.get("timeout_seconds") or 1200))
        completed = subprocess.run(args, input=request.prompt, capture_output=True,
                                   text=True, timeout=int(request.metadata.get("timeout_seconds") or 1200),
                                   env=env, check=False)
        events: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, Mapping):
                events.append(dict(value))
        thread_id = next((str(row.get("thread_id")) for row in events
                          if row.get("type") == "thread.started" and row.get("thread_id")), "")
        final = next((row for row in reversed(events) if row.get("type") == "turn.completed"), {})
        assistant = "".join(self._event_text(row) for row in events)
        terminal = bool(final) and completed.returncode == 0 and bool(assistant.strip())
        if not terminal:
            raise RuntimeError("managed Modified Codex exec did not produce terminal assistant output: "
                               + (completed.stderr[-800:] or "terminal event missing"))
        return {"ok": True, "provider_acceptance_observed": True, "assistant_terminal": True,
                "conversation_id": thread_id or ("codex-thread:" + request.operation_id),
                "user_message_id": request.operation_id,
                "terminal_assistant_message_id": str(final.get("turn_id") or
                    (thread_id + "#terminal")), "terminal_assistant_text": assistant,
                "assistant_text": assistant, "provider_thread_id": thread_id,
                "provider_terminal_event": final, "returncode": completed.returncode,
                "provider_elapsed_seconds": None}


class ComputerUseXProviderAdapter:
    """Thin adapter to the existing domain-owned Gemini/Claude implementation."""

    def __init__(self, provider: str) -> None:
        self.provider = normalize_provider(provider)

    @staticmethod
    def _google_refresh_tokens() -> list[str]:
        """Read company-custodied Chrome refresh tokens without exporting them."""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        database = Path(os.environ.get(
            "B4PT0R_GOOGLE_TOKEN_DB",
            "/custody/browser/profile/Default/Web Data",
        ))
        if not database.is_file():
            return []
        connection = sqlite3.connect(f"file:{database}?immutable=1", uri=True)
        try:
            rows = connection.execute(
                "select encrypted_token from token_service order by service"
            ).fetchall()
        finally:
            connection.close()
        key = hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)
        values: list[str] = []
        for (encrypted,) in rows:
            data = bytes(encrypted)
            if data.startswith((b"v10", b"v11")):
                data = data[3:]
            try:
                decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
                clear = decryptor.update(data) + decryptor.finalize()
                padding = clear[-1]
                if 1 <= padding <= 16:
                    clear = clear[:-padding]
                value = clear.decode("utf-8")
            except Exception:
                continue
            if value and value not in values:
                values.append(value)
        return values

    @staticmethod
    def _google_web_cookies(refresh_token: str) -> list[dict[str, Any]]:
        """Exchange a custody token for short-lived Google web cookies."""
        encoded = urlencode({
            "client_id": os.environ.get(
                "B4PT0R_GOOGLE_CLIENT_ID", "77185425430.apps.googleusercontent.com"
            ),
            "client_secret": os.environ.get(
                "B4PT0R_GOOGLE_CLIENT_SECRET", "OTJgUOQcT7lO7GsGZq2G4IlT"
            ),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode()
        with urlopen(Request("https://oauth2.googleapis.com/token", data=encoded), timeout=30) as response:
            access_token = str(json.load(response)["access_token"])
        request = Request(
            "https://www.google.com/accounts/OAuthLogin?source=ChromiumBrowser&issueuberauth=1",
            headers={"Authorization": "OAuth " + access_token},
        )
        with urlopen(request, timeout=30) as response:
            uberauth = response.read().decode("utf-8").strip()
        if not uberauth or "<" in uberauth:
            raise RuntimeError("Google did not issue a web-session capability")
        jar = http.cookiejar.CookieJar()
        opener = build_opener(HTTPCookieProcessor(jar))
        merge = "https://accounts.google.com/MergeSession?" + urlencode({
            "source": "ChromiumBrowser",
            "continue": "https://gemini.google.com/app",
            "uberauth": uberauth,
        })
        with opener.open(Request(merge), timeout=45) as response:
            response.read(1)
        cookies: list[dict[str, Any]] = []
        for cookie in jar:
            item: dict[str, Any] = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path or "/",
                "secure": bool(cookie.secure),
            }
            if cookie.expires:
                item["expires"] = float(cookie.expires)
            cookies.append(item)
        if not cookies:
            raise RuntimeError("Google web-session exchange returned no cookies")
        return cookies

    def _ensure_gemini_session(self) -> None:
        """Restore Gemini login inside each operation lease when it expires."""
        if self.provider != "gemini.com":
            return
        from playwright.sync_api import sync_playwright

        endpoint = os.environ.get("COMPUTERUSEX_CDP_ENDPOINT", "http://127.0.0.1:9333")
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(endpoint)
            if not browser.contexts:
                raise RuntimeError("provider browser has no persistent context")
            context = browser.contexts[0]
            page = next((candidate for candidate in context.pages
                         if "gemini.google.com" in candidate.url), None)
            if page is None:
                page = context.new_page()
                page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60_000)
            body = page.locator("body").inner_text(timeout=20_000)
            # Gemini changes its empty-composer slogan frequently and omits it
            # entirely on restored conversations.  The provider's explicit
            # sign-in affordance is the authentication boundary; requiring a
            # particular marketing phrase misclassifies a live paid session
            # and then demands refresh tokens that browser custody does not
            # need or expose.
            def provider_signed_out() -> bool:
                # Gemini currently exposes both a Google-account anchor and
                # provider buttons labelled "Sign in" while logged out.  Do
                # not use slogans as the boundary: they change independently
                # of authentication and previously let a logged-out composer
                # pass as a custodied session.
                return (
                    page.locator("body.viewer-signed-out").count() > 0
                    or page.locator(
                        'a[href*="accounts.google.com/ServiceLogin"]'
                    ).count() > 0
                    or page.get_by_role("button", name="Sign in", exact=True).count() > 0
                )

            signed_out = provider_signed_out()
            if not signed_out:
                return
            failures: list[str] = []
            # The managed provider profile can retain Google's account chooser
            # without exposing a refresh token to the worker.  Re-enter that
            # provider-owned session before asking central custody for a token;
            # this keeps authentication inside the leased browser profile and
            # never copies a personal-browser profile or password.
            try:
                sign_in = page.locator(
                    'a[href*="accounts.google.com/ServiceLogin"]'
                ).first
                if sign_in.count() == 0:
                    sign_in = page.get_by_role("button", name="Sign in", exact=True).first
                if sign_in.count() > 0:
                    sign_in.click(timeout=20_000)
                    page.wait_for_timeout(2_000)
                    # Google may reuse the same tab or open the chooser in a
                    # second provider-context page.
                    chooser = next(
                        (candidate for candidate in reversed(context.pages)
                         if "accounts.google.com" in candidate.url),
                        page,
                    )
                    account = chooser.locator(
                        '[data-identifier], [data-email], div[role="link"]:has-text("@")'
                    ).first
                    if account.count() > 0:
                        account.click(timeout=20_000)
                        chooser.wait_for_timeout(3_000)
                    page.goto(
                        "https://gemini.google.com/app",
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    page.wait_for_timeout(2_000)
                    if not provider_signed_out():
                        return
            except Exception as exc:
                failures.append("account_chooser_" + type(exc).__name__)
            for refresh_token in self._google_refresh_tokens():
                try:
                    context.add_cookies(self._google_web_cookies(refresh_token))
                    page.goto("https://gemini.google.com/app", wait_until="domcontentloaded", timeout=60_000)
                    page.wait_for_timeout(2_000)
                    body = page.locator("body").inner_text(timeout=20_000)
                    signed_out = provider_signed_out()
                    if not signed_out:
                        return
                except Exception as exc:
                    failures.append(type(exc).__name__)
            raise RuntimeError(
                "custodied Google tokens could not establish Gemini session: "
                + ",".join(failures or ["no_refresh_tokens"])
            )

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        # This adapter imports only the provider-facing ComputerUseX surface.
        # ChatGPT browser guard installation belongs to the ChatGPT actuator,
        # not to Gemini/Claude actuation and pulls unrelated optional domains.
        os.environ.setdefault("COMPUTERUSEX_SKIP_IMPORT_GUARDS", "1")
        # The immutable provider runtime is intentionally unprivileged and its
        # image HOME is not a state volume. ComputerUseX owns durable/browser
        # state beneath the shared runtime volume, so every provider generation
        # and every environment resolves the same writable domain root instead
        # of inventing an environment-local credential/state location.
        os.environ.setdefault("COMPUTERUSEX_RUNTIME_ROOT", "/runtime/worker/computerusex")
        os.environ.setdefault("XDG_DATA_HOME", "/runtime/worker/xdg-data")
        os.environ.setdefault("XDG_STATE_HOME", "/runtime/worker/xdg-state")
        os.environ.setdefault("XDG_CACHE_HOME", "/runtime/worker/xdg-cache")
        # Recovery is a desired-state policy.  Do not erase an explicitly
        # enabled ComputerUseX/Modified-Codex fallback inside the adapter.
        os.environ.setdefault("COGNILODE_PROVIDER_CODEX_RECOVERY", "0")
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
            "ComputerUseX HAR/network trace. Use that successful browser trace to recover the "
            "provider's public HTTP request method and move the existing provider adapter to "
            "that HTTP method without creating another queue, transport, or job identity. "
            "Preserve the unified prompt/readback API and write the complete "
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
        # This adapter is the domain-owned native-web implementation for the
        # non-ChatGPT .com providers.  A prompt without attachments is still a
        # browser-provider mutation; treating it as a generic MCP call made
        # text-only Gemini work fail before reaching the provider whenever the
        # optional MCP package was absent.  Keep every Gemini/Claude/Anthropic
        # turn inside the same custody-bound ComputerUseX transport regardless
        # of whether the work packet happens to contain a file.
        browser_turn = True
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
            # Gemini/Anthropic/Claude use the direct provider runtime. Importing
            # ComputerUseX's ChatGPT-only package guards here can invoke the
            # legacy Modified Codex repair path before the native browser turn.
            os.environ["COMPUTERUSEX_SKIP_IMPORT_GUARDS"] = "1"
            os.environ.setdefault("COGNILODE_PROVIDER_CODEX_RECOVERY", "0")
            os.environ.setdefault("COMPUTERUSEX_RUNTIME_ROOT", "/runtime/worker/computerusex")
            os.environ.setdefault("XDG_DATA_HOME", "/runtime/worker/xdg-data")
            os.environ.setdefault("XDG_STATE_HOME", "/runtime/worker/xdg-state")
            os.environ.setdefault("XDG_CACHE_HOME", "/runtime/worker/xdg-cache")
            source = Path(os.environ.get(
                "COMPUTERUSEX_SOURCE", "/runtime/source/current/automation-computeruse-vision/src"
            ))
            for candidate in (source, Path("/workspace/cognilode/source/current/automation-computeruse-vision/src"),
                              Path.home() / "Projects/automation-computeruse-vision/src"):
                if candidate.is_dir() and str(candidate) not in sys.path:
                    sys.path.insert(0, str(candidate))
            runtime = importlib.import_module("computerusex.web_agent_runtime")
            try:
                self._ensure_gemini_session()
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
                if os.environ.get("COGNILODE_PROVIDER_CODEX_RECOVERY", "0").strip().casefold() not in {
                    "1", "true", "yes", "on",
                }:
                    return {
                        "ok": False, "state": "native_provider_failure",
                        "error": f"{type(exc).__name__}: {exc}",
                        "provider_acceptance_observed": False,
                    }
                return self._modified_codex_recovery(request, exc)
            if result.get("ok") is False:
                if os.environ.get("COGNILODE_PROVIDER_CODEX_RECOVERY", "0").strip().casefold() not in {
                    "1", "true", "yes", "on",
                }:
                    return result
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
            "codex.research": InstalledCodexResearchAdapter(),
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
            # Every installed and .com worker receives the same task-selected
            # right-edge foreground.  This lives at the singular actuator so
            # new provider adapters cannot silently bypass organizational
            # memory and higher layers do not each reinvent prompt context.
            with tempfile.TemporaryDirectory(prefix="agent-memory-foreground-") as directory:
                foreground = AgentMemoryClient(use_broker=False).prompt_foreground(
                    persona=str(request.metadata.get("memory_persona") or "company"),
                    max_tokens=int(request.metadata.get("memory_max_tokens") or 40_000),
                    task=request.prompt,
                )
                context = str(foreground.get("context") or "")
                path = Path(directory) / "AGENT_MEMORY_FOREGROUND.md"
                path.write_text(context, encoding="utf-8")
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != str(foreground.get("content_sha256") or ""):
                    raise RuntimeError("Agent Memory foreground content identity mismatch")
                memory_meta = {
                    "persona_id": foreground.get("persona_id"),
                    "content_sha256": actual,
                    "selected_tokens": foreground.get("selected_tokens"),
                    "foreground_handle": foreground.get("foreground_handle"),
                    "task_context_handles": list(foreground.get("task_context_handles") or ()),
                }
                request = replace(
                    request,
                    prompt=(
                        "First read the attached AGENT_MEMORY_FOREGROUND.md in full. It is "
                        "provenance-labelled organizational memory, not a replacement for the "
                        "current instruction. Then perform the current instruction below.\n\n"
                        + request.prompt
                    ),
                    attachments=(str(path), *request.attachments),
                    metadata={**dict(request.metadata), "agent_memory_foreground": memory_meta},
                )
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
            # Only provider-returned bytes with confirmed custody are artifacts.
            # A DOM anchor, attempted download, or unresolved upload never earns
            # artifacts_collected. Preserve incomplete candidates in the raw receipt.
            artifacts: list[dict[str, Any]] = []
            excluded_artifacts: list[dict[str, str]] = []
            candidates = (raw.get("downloaded_files") or raw.get("response_downloaded_files")
                          or raw.get("response_downloads") or raw.get("artifacts") or [])
            for item in candidates if isinstance(candidates, list) else []:
                row = dict(item) if isinstance(item, Mapping) else {"path": str(item)}
                path = str(row.get("path") or "")
                if row.get("downloaded") is False:
                    excluded_artifacts.append({"name": str(row.get("filename") or ""), "reason": "download_not_completed"})
                    continue
                if path and Path(path).is_file() and not _verified_artifact(row):
                    try:
                        row["custody"] = commit_returned_artifact(path)
                        row["custody_state"] = "committed"
                    except Exception as exc:
                        row["custody_state"] = "pending"
                        row["custody_error"] = type(exc).__name__
                if _verified_artifact(row):
                    artifacts.append(row)
                else:
                    excluded_artifacts.append({
                        "name": Path(str(row.get("filename") or path or "")).name,
                        "reason": str(row.get("custody_error") or "no_verified_custody"),
                    })
            if excluded_artifacts:
                raw["uncollected_artifact_candidates"] = excluded_artifacts
            provider_terminal_observed = terminal
            defects = _semantic_result_defects(
                request=request, raw=raw, terminal=terminal,
                assistant_text=assistant_text, artifacts=artifacts,
            ) if accepted else []
            raw["result_quality_admission"] = {
                "accepted": bool(accepted and provider_terminal_observed and not defects),
                "provider_terminal_observed": provider_terminal_observed,
                "verified_artifact_count": len(artifacts),
                "defects": defects,
            }
            if defects:
                # Provider mutations may already have occurred. Never pretend
                # the requested deliverable completed, and never replay blindly.
                terminal = False
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
            if terminal and artifacts:
                event_ids.append(self.publisher.emit(
                    request=request, state="artifacts_collected",
                    payload={"artifacts": artifacts}, conversation_id=conversation_id,
                ))
            error = None
            state = "artifacts_collected" if terminal and artifacts else "terminal" if terminal else "accepted"
            if not accepted or defects:
                state = "provider_error"
                if defects:
                    error = {"code": "semantic_deliverable_incomplete",
                             "message": json.dumps({"defects": defects}, separators=(",", ":"))}
                else:
                    status = str(raw.get("state") or raw.get("status") or "provider_not_accepted")
                    diagnostic = raw.get("error") or raw.get("error_preview")
                    if not diagnostic:
                        diagnostic = status
                        markers = raw.get("auth_markers_observed")
                        observed_url = raw.get("observed_url")
                        if markers or observed_url:
                            diagnostic = json.dumps({
                                "status": status,
                                "auth_markers_observed": markers or [],
                                "observed_url": observed_url or "",
                            }, separators=(",", ":"))
                    error = {"code": status, "message": str(diagnostic)}
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
                artifacts_collected=bool(terminal and artifacts),
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


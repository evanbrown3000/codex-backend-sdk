# Native Codex session custody

`scripts/cognilode-codex-native-session` runs on the environment that owns the
original Codex rollout. It registers a SHA-bound mapping from the provider's
session ID to that environment, rollout path, CLI stderr header, executable,
and system/developer instruction bundle. Registration can be provisional while
the shared D1/Drive conversation ingester catches up. The session becomes
resumable only when D1 records the exact rollout SHA as a complete,
Drive-verified `openai-codex` conversation **and** a release activation binds
the actual system/developer replacement files, modified binary, and another
centrally observed same-session behavior test. A rollout-derived fingerprint
alone leaves `instruction_status=provisional` and `resume_ready=false`.

Register after the first native turn, using the real Codex CLI stderr and
original JSONL (the first event must be matching `session_meta`):

```sh
scripts/cognilode-codex-native-session register \
  --session-id SESSION_UUID --environment-id ENVIRONMENT_ID \
  --rollout-path /absolute/path/to/rollout.jsonl \
  --stderr-path /absolute/path/to/codex.stderr \
  --source-receipt-path /absolute/path/to/receipt.json \
  --binary-path /absolute/path/to/versioned/codex-native-binary \
  --instruction-bundle-path /absolute/path/to/instructions.json
```

`--binary-path` must name the versioned native ELF, Mach-O, or PE executable
itself. A shell launcher can switch targets while keeping the same hash, so
the client refuses it for new registrations, activation, and resume. An older
provisional locator registered with a launcher remains readable but cannot
be resumed through this client.

Once a modified Codex release has independently exercised both role
replacements and a native same-session continuation, activate its instruction
release on the registered host:

```sh
scripts/cognilode-codex-native-session activate \
  --session-id SESSION_UUID --environment-id ENVIRONMENT_ID \
  --binary-path /absolute/path/to/versioned/codex-native-binary \
  --instruction-bundle-path /absolute/path/to/instructions.json \
  --system-instructions-path /absolute/path/to/system.txt \
  --developer-instructions-path /absolute/path/to/developer.txt \
  --release-manifest-path /absolute/path/to/release.json \
  --behavior-receipt-path /absolute/path/to/behavior.json
```

`release.json` must have schema
`cognilode.codex.native_instruction_release.v1`, with SHA-256 fields
`binary_sha256`, `bundle_sha256`, `system_sha256`, `developer_sha256`, and
`behavior_receipt_sha256`. `behavior.json` must have schema
`cognilode.codex.native_instruction_behavior.v1`, the exact binary hash,
`system_replacement_observed=true`, `developer_replacement_observed=true`,
`same_session_resume_observed=true`, an `observed_session_id` distinct from
the target, `terminal_rollout_sha256`, and `initial_event_count`. The Site
independently requires that observed session's terminal rollout and increased
event count in D1/Drive before accepting activation. The CLI hashes the
actual local files and sends their exact release/evidence bytes; free-form
digest claims are insufficient.

On that same environment, resume with an operation ID that is stable across
retries. The local script verifies all registered hashes and the environment
before reserving the operation in D1. It invokes `codex exec resume` only if
the reservation says `execute=true`; a repeated or ambiguous operation is
never blindly sent to the provider again.

```sh
scripts/cognilode-codex-native-session resume \
  --session-id SESSION_UUID --environment-id ENVIRONMENT_ID \
  --operation-id STABLE_OPERATION_ID --prompt-file /absolute/prompt.txt \
  --stderr-path /absolute/path/to/codex.stderr \
  --source-receipt-path /absolute/path/to/receipt.json \
  --instruction-bundle-path /absolute/path/to/instructions.json
```

The returned turn is held in `awaiting_central_readback` until the changed
rollout reaches D1/Drive with more normalized events. Re-run `confirm
--result ~/.local/state/cognilode/codex-native-resume/OPERATION_ID/result.json`
after ingestion. An interrupted resume without a terminal local result remains
ambiguous and requires reconciliation of the native session; it is not safely
replayed from a normalized conversation rendering.

On each host, run `scripts/cognilode-codex-native-session install-confirm-timer`
once. Its user timer checks pending local result receipts every five minutes
and stops checking an operation after D1/Drive confirms the changed source and
new events. The timer never sends a Codex prompt and never retries an ambiguous
provider turn.

The instruction bundle hash is a continuity guard. The actual system and
developer replacement behavior must be implemented in the modified Codex
binary used for both turns; this CLI does not inject instruction text into
provider prompts. `scripts/cognilode-conversation-read --provider codex-d1
--conversation-id SESSION_UUID --remember --reader-id EMPLOYEE` reads the
centrally admitted events, full once and deltas afterward.

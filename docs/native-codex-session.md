# Native Codex session custody

`scripts/cognilode-codex-native-session` runs on the environment that owns the
original Codex rollout. It registers a SHA-bound mapping from the provider's
session ID to that environment, rollout path, CLI stderr header, executable,
and system/developer instruction bundle. Registration can be provisional while
the shared D1/Drive conversation ingester catches up. The session becomes
resumable only when D1 records the exact rollout SHA as a complete,
Drive-verified `openai-codex` conversation.

Register after the first native turn, using the real Codex CLI stderr and
original JSONL (the first event must be matching `session_meta`):

```sh
scripts/cognilode-codex-native-session register \
  --session-id SESSION_UUID --environment-id ENVIRONMENT_ID \
  --rollout-path /absolute/path/to/rollout.jsonl \
  --stderr-path /absolute/path/to/codex.stderr \
  --source-receipt-path /absolute/path/to/receipt.json \
  --binary-path /absolute/path/to/codex-cognilode \
  --instruction-bundle-path /absolute/path/to/instructions.json
```

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

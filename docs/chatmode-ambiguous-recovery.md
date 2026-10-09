# Chat-mode ambiguous-send recovery

An HTTP 502/503 or a truncated successful stream does not prove that a queued
ChatGPT Chat-mode turn was rejected. The queue must retain its lease/custody and
must not replay the POST. The sender's stable user-message ID is UUIDv5 of
`cognilode-chatmode-queue:<job-id>`, so the worker can search by that exact ID
using provider reads alone.

`cognilode-chatmode-queue-worker` first tries a known conversation ID from its
receipt or central send custody. If the receipt has an incomplete turn, the
existing collector attempts to hydrate it. The worker then invokes the sender's
`reconcile` command. That command hydrates a known conversation ID or lists
recent conversations and hydrates at most twelve candidates in one attempt.
It accepts only a conversation branch containing the exact user-message ID and
only a later assistant message with `end_turn: true` before another user turn.
On an accepted terminal response, it downloads linked sandbox files, admits the
conversation to central memory, verifies readback, and lets the worker complete
the leased queue job. A ZIP remains required by the queue's completion gate.

Provider 429 replies defer reads using `Retry-After` when supplied. The worker
persists the next attempt time and scan offset; it clamps retry intervals to
60–900 seconds. Auth 401/403 refreshes credentials once before a GET retry.
Recent-index 429 also closes a worker-wide, persisted rate gate under
`recent-index-gate.json`. One worker process holds a file lock while it checks
and updates that gate, so concurrent jobs cannot each send another index GET.
During the cooldown, jobs with a known conversation ID may still hydrate that
exact ID using `--known-only`; jobs without one defer to the shared deadline.
Other read failures also defer. An index miss only advances a bounded rotating
window of the latest 48 conversations and never authorizes another POST. This
window is a practical recent-turn recovery mechanism, not a complete historical
search. If the turn is older, another retrieval route or operator investigation
may still be needed; the queued mutation remains ambiguous meanwhile.

Read-only manual invocation:

```sh
cognilode-b4pt0r-chatmode reconcile \
  --queue-job-id JOB_ID --prompt-file ORIGINAL_PROMPT_FILE
```

This command does not submit a prompt. It may download attachments and write
local/central recovery records after it finds the exact accepted turn.

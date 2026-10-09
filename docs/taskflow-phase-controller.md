# TaskFlow phase controller

`scripts/cognilode-taskflow-phase-controller` advances a `.plan` step through research, ChatGPT sandbox, and external effect jobs in the existing D1 prompt queue. It runs once per invocation. The existing recurrence can invoke it again; it does not own a clock or a second queue.

Example:

```sh
scripts/cognilode-taskflow-phase-controller \
  --plan /home/evan/Projects/codex-backend-sdk/plans/critical-path-autonomy.plan \
  --role 'Elliot Mercer' \
  --external-employee 'Rina Hale' \
  --seed-step CP-4-6
```

The D1 API must support `get_job` by exact ID, `prompt_authority=taskflow_plan`, and the `codex.research` and `codex.external-effect` providers. Each phase enqueue includes the source `.plan` path, full plan text, exact SHA-256, step, phase, and assigned employee. A changed plan produces new phase IDs for work that has not started; a source change detected before phase materialization stops that run. In-flight steps retain their originating revision and phase IDs until their effect completes.

Each step's `owner:` selects its named Codex researcher. `config/employee-slack-roles.json` records the Slack role and source message that the controller gives to that employee; a missing role stops materialization. The `--role` argument is a fallback for plans without per-step owners. The external-effect employee is named separately with `--external-employee`.

Secretary dispatch is constrained to one installed Codex candidate through its documented `--candidates-json` interface. The candidate family is `installed_codex` unless the executable belongs to a modified release whose manifest SHA-256 matches the release binary; only then is it `modified_codex`. The receipt records the resolved executable and SHA-256 so a vanilla CLI is not reported as modified. The controller records the invocation result under the project's `.taskflow-state/` launchpad by default and only accepts a research ZIP or external-effect receipt after a verified Codex route. The research ZIP and returned ChatGPT ZIP must be physically readable by the corresponding actuator. The chat-mode queue worker continues to own authenticated sends, collection, and recovery.

For a local installed Codex run, keep `--state-root` inside the project repository so its workspace sandbox can write the research ZIP. A plan step must declare an absolute, read-only `effect_probe_command`. With `effect_probe_expected`, the controller requires that exact output and defaults to a 300-second timeout. Without an expected string, stdout must be a `cognilode.effect_probe.v1` JSON object with `verified: true` and a nonempty `evidence` list of kind, reference, and SHA-256 records; this mode defaults to 900 seconds for multi-object remote readbacks. A step can set `effect_probe_timeout_seconds` from 5 to 1800. The controller executes the probe independently after the external employee returns and again during reconciliation. A claimed effect receipt without a passing probe remains pending.

An `effect_pending` job is reconciled from its durable invocation and artifact/effect receipt. An uncertain external mutation is not sent again automatically. The external-effect receipt must describe a concrete applied effect and successful observed checks; a ZIP or source commit alone cannot complete the phase. Only completed `codex.external-effect` jobs with external-effect evidence release dependent `.plan` steps.

## Bounded recurrence service

`deploy/systemd/user/cognilode-taskflow-phase-controller.service` keeps a long-running, single-instance recurrence host active so CP-4-6 can progress without a human repeatedly invoking the controller. The service does **not** claim or time `chatgpt.com` jobs; it only reruns the phase reconciler/materializer. The hosted historical rhythm remains the sole ChatGPT clock.

Install `scripts/cognilode-taskflow-phase-controller-service` beside the controller, copy `taskflow-phase-controller.env.example` to `%h/.config/cognilode/taskflow-phase-controller.env`, and reconcile the named employees with the installed organization before enabling the unit. Research and external-effect employee names must be distinct. The recurrence process has a nonblocking singleton lock, a 10–3600 second bounded interval, a 30–7200 second per-run timeout, durable JSONL receipts, and no automatic replay of an `effect_pending` external mutation.

For this plan the acceptance probe is exactly:

```sh
/usr/bin/systemctl --user is-active cognilode-taskflow-phase-controller.service
```

An `active` service is necessary but not sufficient for CP-4-6 completion: a real `codex.external-effect` D1 job must also complete with deployment/service-change identity and central readback.

## Verified plan checkoff and revision continuity

After D1 records a completed external-effect job with its work ZIP, effect receipt, and independent readback probe, the controller invokes the named Codex external-effect employee again for a fenced `.plan` checkoff. That Codex invocation runs `--checkoff-only` with the exact prior plan SHA, step ID, and D1 effect job ID. The command checks the D1 job and evidence afresh; the checkbox alone has no effect authority.

The controller reads each active phase by its exact deterministic D1 job ID. A global queue page filled by unrelated projects cannot hide a pending research effect, completed ChatGPT artifact, or pending external effect. An ID returned with the wrong project revision, step, provider, or phase stops progression.

The checkoff commits only the `.plan` path and an immutable copy of the prior plan at `.taskflow-plan-revisions/<plan-name>/<old-sha>.plan`. Its `completed_effect STEP SOURCE_SHA JOB_ID` lines retain each verified effect reference and its originating plan revision. At startup, the controller rereads that committed source and the exact D1 job with effect, receipt, work ZIP, and independent probe evidence before accepting the checked state. Even after a checkbox is marked, new dependent jobs carry that prior effect job ID as a D1 dependency. Its `inflight_revision STEP OLD_SHA` lines retain the original phase IDs for independent work already in progress. Existing checked boxes become explicit `inherited_checked STEP` baseline entries for history, but they cannot release a dependent without D1 effect evidence. After the first automated revision, a checked box without either a verified effect reference or a baseline entry is rejected. A fresh controller process reads the committed snapshots, continues only their listed steps, and uses each old external-effect job ID for dependencies. Once an old step's effect is independently verified, a later checkoff removes its in-flight marker. A dependent step can then start under the latest plan revision.

The checkoff refuses a dirty plan path, a changed revision, a mismatched job identity, or missing probe evidence. The controller runs one checkoff per recurrence. A failure before Git commit restores the old plan only if the repository HEAD and plan bytes still match the expected state. A failure after commit leaves the committed plan and snapshot intact and reports the uncertain post-commit verification for recovery. A Git commit records the local plan revision. A separate repository synchronization process must publish that commit to remote collaborators.

## Private cross-device attachment launchpad

Set `COGNILODE_TASKFLOW_ATTACHMENT_S3_BUCKET` and `COGNILODE_AWS_CLI` in the controller's environment to publish each verified ZIP under `taskflow-artifacts/sha256/<sha256>.zip` in the private S3 bucket before queuing its next phase. The controller reads back object size and SHA-256 metadata. Queue attachments retain the local path and include the immutable `s3://` mirror. The chat-mode worker uses its own AWS credentials to stage a missing local ZIP from that mirror, checks the object metadata and downloaded SHA-256, and only then physically uploads it to ChatGPT. Do not use a public Pages asset path as an artifact mirror: TaskFlow workpacks can contain internal code and conversation context.

On a second Linux device, check out the SDK and give that device scoped read access to the launchpad prefix, a local authenticated ChatGPT/Codex identity, and a central D1 operator bearer in `%h/.config/cognilode/operator-bearer` (mode 0600). `scripts/cognilode-install-chatmode-device --device-id laptop --priority 50 --network-route home --aws-cli /path/to/aws --probe-sha256 <known-research-zip-sha256>` verifies provider health, D1 readback, and a private ZIP download before installing and starting the device-specific service. It writes only non-secret service settings. The hosted rhythm chooses among fresh eligible heartbeats; the D1 queue still fences each send. A phone needs a separate supervisor appropriate to its operating system; the Linux systemd installer does not claim to install one.

CP-7 requires a real controlled outage drill: EvanPC unavailable, an alternate-device claim at a hosted due slot, exactly one provider user-message ID, terminal in-chat report, hash-verified work ZIP, central conversation readback, and a recorded external effect. A second service installation or a synthetic heartbeat does not complete it.

## Conversation reads after the first view

`scripts/cognilode-conversation-read` reads admitted ChatGPT.com, Gemini, Claude/Anthropic, Codex D1, Google Drive Codex, and historical S3 conversations through one interface. To have a named employee see the full conversation once and only new events thereafter:

```sh
scripts/cognilode-conversation-read --provider chatgpt.com --conversation-id CONVERSATION_ID --reader-id "Elliot Mercer" --remember --format markdown
scripts/cognilode-conversation-read --job-id SHARED_QUEUE_JOB_ID --reader-id "Elliot Mercer" --remember --format json
scripts/cognilode-conversation-read --provider openai-codex --conversation-id CONVERSATION_ID --since NEXT_CURSOR --format json
```

`--remember` stores a private cursor per employee, provider, and conversation. A changed message prefix automatically shows the full conversation again and labels the reset; it never silently hides revised history. Explicit `--since` remains available for callers that store their own `next_cursor`. The response labels its source and coverage; admitted events do not imply every provider branch has been acquired. The Drive reader path can be set with `COGNILODE_DRIVE_READBACK_SCRIPT` when Memory Stock is installed elsewhere.

`--job-id` resolves a prompt in the shared D1 queue. While it is queued or in flight, the command returns its state and `conversation_ready: false`. Hosted provider jobs require matching provider and central-readback evidence after completion. TaskFlow `codex.research` and `codex.external-effect` jobs expose the Codex CLI session ID from their verified Secretary evidence, but remain unreadable until an exact `openai-codex` D1 conversation is complete and its active source SHA is bound to a verified Google Drive object. The result then uses `codex-d1` as its reader provider. Pending admission or Drive confirmation does not advance a `--remember` cursor.

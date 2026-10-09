# Root manager plan handoff

`scripts/cognilode-root-plan-handoff` is a low-compute bridge between a completed, rhythm-queued ChatGPT Chat-mode root-manager turn and the existing TaskFlow/Secretary controller. It does not send provider prompts, invent employee instructions, or create a separate conversation corpus. The root manager writes a company/project/team/employee **plan**, not prompts for other agents. The bridge preserves each instruction line in a `.plan` file and installs the existing TaskFlow controller, which manages Codex research, Chat-mode sandbox work, Codex external effects, and checkoff through the one D1 queue.

The historical mechanisms recovered for this seam are `LibriRenascendi/tools/root_allocation_authority.py` (root provider-visible plan readback before mechanical application) and `LibriRenascendi/tools/chatgpt_prompt_corpus_hierarchy.py` (source-linked multi-level conversation memory). These are implementation references, not authorities on the user’s requirements. The direct user requirement is a repeatedly prompted root agent with long-term, multi-year global memory, finer project/role memory lower in the hierarchy, and on-demand plan assignment. This bridge implements the provider-plan-to-TaskFlow assignment seam; recurring root turns and memory projection still require their own live proof.

Before installing a plan, the bridge pages the shared D1 conversation index and independently rereads distinct sources. A source counts only if its full user and assistant events, event completeness flags, prompt/response digests, source completion, and Drive verification are present. At least 500 distinct conversations must span 730 days. Index identities and unfinished Codex feed prefixes do not count. This census is computed from the shared store on each handoff; no local text corpus is written.

The selected root job must be completed on `chatgpt.com`, show a historical-rhythm tape, and have D1 provider-conversation and central-readback evidence. It must have physically uploaded a hash-verified ZIP with a `cognilode.root_memory_packet.v1` manifest. That manifest cites at least 500 exact `(provider, conversation_id, prompt_sha256, response_sha256)` shared sources that still pass the live census, and hash-covers nontrivial global, project, and role memory sections. D1's provider-structured upload record must independently show the same ZIP digest. On another device, a missing local ZIP is staged from its content-addressed private S3 mirror using the queue worker's existing exact-SHA staging path. This proves the root turn received layered multi-year memory input; it does not pretend the summaries' semantic quality is already proven.

Its final complete provider-visible assistant message must contain one exact `COGNILODE_ROOT_TASKFLOW_PLAN_JSON_BEGIN` / `COGNILODE_ROOT_TASKFLOW_PLAN_JSON_END` block with schema `cognilode.root_taskflow_plan.v1`. The JSON includes `project_id`, `project_name`, `research_employee`, `external_employee`, and `steps`. Each step has `id`, `title`, `owner`, `role`, `instructions` (literal plan lines), `depends_on`, and an independent `effect_probe` `{command, expected}`. The command must be an isolated Python script with a nonempty expected observation; shell snippets and trivial `true` probes are rejected. The TaskFlow parser checks graph validity. The resulting plan is committed to the existing Git project and reread through TaskFlow's revision verifier before installation. Existing different content at the same target is never overwritten.

The production CLI is:

```
python3 scripts/cognilode-root-plan-handoff \
  --root-job-id ROOT_D1_JOB_ID \
  --output-root /home/evan/Projects/codex-backend-sdk/plans/root-generated
```

On success it calls `scripts/cognilode-taskflow-install-project` and checks the active single-queue installation result. It can be invoked by the root terminal collector when one exists; it does not schedule or send a root turn itself.

At 2026-10-09 live D1 readback, the index had 1,094 identities across 19 pages, but **zero** met the combined complete and Drive-verified source gate. The CLI therefore exited 1 with `shared_multi_year_stock_incomplete`, created no plan, and made zero provider requests. This is a deliberate truth boundary, not evidence that root autonomy is already live. The missing joins are a ≥500 complete Drive-backed shared stock, an automated source-linked root-memory ZIP builder, a root job with this plan schema, a recurrent root turn launcher in the existing rhythm queue, and observed downstream TaskFlow/Secretary execution.

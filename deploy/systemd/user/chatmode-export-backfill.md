The authenticated Gmail discovery timer runs daily and keeps a newly discovered
OpenAI privacy-export URL as a separate private candidate. A genuinely new
candidate automatically attempts exact identity recovery of held legacy
Chat-mode jobs using the provider export and provider GET, without a Chat POST.
The bounded backfill
timer reads one ZIP shard per invocation by HTTP range, processes at most 100
conversations, records text and branch provenance in shared D1, and relies on
the existing single D1-to-Drive writer. It never sends a Chat-mode prompt.

Install on EvanPC with `install-chatmode-export-backfill.sh`. The authenticated
ChatGPT profile, private `privacy-export-url.txt`, live worker venv, D1 sender,
and singular Drive writer must already be configured. The URL file and state
files live under `~/.local/share/cognilode/b4pt0r-chatmode/` with private mode.
The service unit caps CPU at 50% and memory at 1.5 GiB. The requirements file
pins the streaming JSON parser used to avoid loading a whole shard.

Backfill states in `privacy-export-backfill/cursor.json` distinguish complete
ZIP traversal from D1 and Drive readback. `d1_exact_receipts.jsonl` contains
only source locators and hashes. The post-run retirement check runs only after
all shards and deferred offsets are complete. It verifies at most 100 receipts
per invocation against exact D1 identity and the singular Drive writer's
independent readback ledger. It never deletes the original ZIP or signed URL:
media bytes are absent from the text projection, so even complete D1/Drive
replication does not justify deleting the original export.

Use `systemctl --user status cognilode-chatmode-export-backfill.service` and
`journalctl --user -u cognilode-chatmode-export-backfill.service` for progress.
An expired URL or changed ETag stops with recoverable state. New exports remain
in `privacy-export-next-url.txt` until the older source is reconciled; the
installer does not silently replace custody.

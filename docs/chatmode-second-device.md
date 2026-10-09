# Enroll a second physical ChatGPT chat-mode worker

The second worker must run on a different laptop, phone, or other physical home/mobile device. A Docker container on EvanPC and an AWS relay do not survive EvanPC going offline, so they cannot pass CP-7 failover acceptance.

On a separate Linux laptop with this SDK checkout, a locally authenticated ChatGPT.com session, working AWS access to the private task ZIP bucket, and systemd user services, run:

```bash
scripts/cognilode-install-chatmode-device \
  --device-id home-laptop \
  --network-route home \
  --auth-source chrome \
  --chrome-profile "$HOME/.config/google-chrome/Default" \
  --probe-sha256 WORK_ZIP_SHA256
```

If this laptop has a local Codex desktop identity instead, use `--auth-source codex` and omit `--chrome-profile`. The installer validates the local provider identity, reads the central D1 rhythm, stages the private ZIP by hash when `--probe-sha256` is supplied, enables user-service lingering, starts the worker, and waits for its central heartbeat. It does not copy ChatGPT or AWS credentials from EvanPC. The worker uses the selected local auth source for send, exact-message reconciliation, terminal collection, and linked ZIP recovery.

The worker only claims D1 jobs assigned to its physical device at a historical rhythm slot. After enrollment, verify an EvanPC-offline slot was served by this worker, that the returned ChatGPT provider stream ended, and that the work ZIP and conversation were independently read back from central storage before checking off CP-7. A heartbeat alone is insufficient.

## Android phone via Termux

An Android phone cannot expose its Chrome app's authenticated cookies to Termux. The phone worker therefore uses an independent phone-local Codex OAuth device authorization and a persistent random provider device ID. The transport refreshes that phone-local token after a proven 401/403 while preserving the ChatGPT account ID; an ambiguous conversation POST remains under the existing D1 reconciliation fence. The ChatGPT computation still occurs in the remote Chat-mode agent's native sandbox. The phone only transports prompts, ZIPs, and responses.

There is one physical setup action: on the owner's Android phone, open Termux and run the checksum-verified [phone management bootstrap](https://gist.github.com/evanbrown3000/8c041a6ef3f5019f9de0be454610e844). It starts Termux SSH on port 8022 with the owner's published GitHub SSH keys, preserving any existing authorized keys. Confirm that the discovered Android device is the owner's phone before doing this. Termux:Boot must be installed and opened once for service restart after a full phone reboot; its absence cannot count as unattended failover.

Once SSH is reachable, the host-side `scripts/cognilode-enroll-phone-over-ssh` verifies an Android system fingerprint and Termux prefix, securely stages the existing D1 bearer and private ZIP bucket credentials over SSH, and installs the public SDK in an Ubuntu/aarch64 proot with a low-priority, 60-second transport service **disabled**. It never sends a ChatGPT turn or makes the phone D1-eligible. Example (substitute the actual Termux user and an existing research ZIP hash):

```bash
scripts/cognilode-enroll-phone-over-ssh --host Android.local --user u0_a123 \
  --probe-sha256 EXISTING_PRIVATE_RESEARCH_ZIP_SHA256 --prepare
```

From the phone's Termux shell, run `~/codex-backend-sdk/scripts/cognilode-install-chatmode-termux authorize`. Open the printed official device-authorization URL in the phone browser and approve the one-time code. The phone stores its own private `~/.codex/auth.json` inside its Ubuntu proot. The host can then run `verify` through SSH with `COGNILODE_PHONE_PROBE_SHA256` set to the same hash; this performs provider health, central rhythm readback, and exact private ZIP retrieval before any service activation. `activate` repeats those gates and starts the runit service. No vanilla ChatGPT scheduled task or Work-mode agent is involved.

**CP-7 acceptance is later:** while EvanPC is genuinely offline, a historical due slot must be claimed by this phone; the provider SSE must show an accepted, terminal Chat-mode turn with the requested model/reasoning, its linked ZIP must be downloaded and hash verified, its conversation must be read back centrally, and the returned work must pass its Codex external-effect step. Only then is the second-device failover proven. `status` and `deactivate` allow inspection and reversible stop.

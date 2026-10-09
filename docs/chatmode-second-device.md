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

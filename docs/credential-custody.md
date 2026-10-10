# ChatGPT credential custody and request containment

The company uses one active ChatGPT credential broker.  Provider clients do
not receive OAuth tokens, cookies, browser profiles, or paths to those assets.
They submit provider operations to the broker.  The broker passes an in-memory
`TokenStore` or its private runtime path to B4PT0R's existing transport.

The encrypted credential bundle and broker lease live behind the Universe
storage adapter.  This repository does not implement a Google Drive, GitHub,
Cloudflare, or local-filesystem storage client.  `CommandStorage` calls the
single unified storage CLI with `stat`, `read`, `write`, and `delete`
operations.  Writes include the prior object revision, so two brokers cannot
both acquire custody.

The encryption key is a generated 256-bit key supplied to the broker as a
server-side secret.  The owner password used to gate Cognilode's private relay
is not an encryption key and is never part of the bundle, command line, source
tree, inventory, or audit stream.

## Durable and runtime state

The durable bundle contains only company authentication state selected for
admission:

- Codex/ChatGPT OAuth state, including refresh capability;
- a company browser profile when browser state is required;
- provider-account metadata needed to select the correct account.

Bundle contents are archived, encrypted with AES-GCM, committed through a
conditional storage write, read back, authenticated, decrypted, and compared
with the source manifest.  A move removes the source only after that complete
readback.  Invalid or empty state is not admitted.

The active broker acquires a conditional lease before decrypting.  It
materializes into an isolated runtime directory, launches the existing
provider transport, renews the lease while that child is alive, terminates the
transport if the lease is fenced, and destroys the decrypted runtime on exit.
The provider transport cannot start through this entrypoint without the HTTP
gate socket.

## Inventory

`cognilode-credential-custody inventory` emits metadata only.  It records:

- credential and browser-state locations, modes, owners, sizes, and opaque
  fingerprints;
- processes and their launch executables;
- user services, timers, and sockets;
- Docker containers, compose sources, credential mounts, and secret
  environment-variable names;
- active GitHub Actions workflows, schedules, and referenced secret names;
- Cloudflare, AWS, and OpenAI Library requesters supplied by provider-specific
  inventory commands using the same `Requester` schema.

Token values, cookies, authorization headers, secret values, and browser
database contents are never written to the registry.  The fingerprint permits
duplicate detection without making the credential usable.

On the EvanPC host, the personal Codex authentication file and personal Chrome
or Chromium profiles are marked as exclusions.  The host exceptions are the
container starter, Evan Recorder, Modified Codex, the B4PT0R desktop app, the
personal browser, and temporary vanilla-Codex rollout production during the
bootstrap period.  No company sender, collector, observer, queue consumer, or
summary worker is an exception.

## Causal cutover

Containment follows one order:

1. Run the metadata inventory from every registered environment through the
   existing remote-shell route and publish each result through unified
   storage.
2. Select the freshest valid company authentication state and move it into the
   encrypted bundle.
3. Start the broker in an existing in-Universe container.  It must hold the
   current lease and expose the provider socket through the existing relay.
4. Route the singular prompt queue and approved provider operations to that
   broker.
5. Run `contain` with the active broker identity.  The command refuses to act
   unless both the bundle and an unexpired matching broker lease are readable.
6. Disable bypassing user units and GitHub workflows, stop bypassing company
   containers and company automation processes, and retain their useful source
   code.
7. Move or purge remaining duplicate company credential copies.  Personal
   EvanPC browser and Codex state remains untouched.

The HTTP gate classifies and records each request.  ChatGPT requests are
centrally authorized; observability is denied by policy.  Its relay credential
now comes from the mounted operator-token file and no longer requires AWS
Secrets Manager or a local AWS credential cache.

## Unified storage command contract

The CLI receives one JSON object on standard input and returns one JSON object
on standard output.  Bytes move through local materialization paths.

```json
{"operation":"read","locator":"gdrive://company/custody.enc","destination":"/tmp/object"}
```

```json
{"operation":"write","locator":"gdrive://company/custody.enc","source":"/tmp/object","expected_revision":"provider-revision"}
```

A successful result contains `ok`, `revision`, and `size`.  A missing `stat`
result contains `ok: true` and `exists: false`.  A failed conditional write
uses `error: revision_conflict`.

Provider-specific inventory commands receive:

```json
{"operation":"credential_request_inventory","environment_id":"..."}
```

They return `{"requesters": [...]}` using the stable `Requester` fields from
`credential_inventory.py`.  This keeps Cloudflare, AWS, and OpenAI Library
knowledge in their owning adapters while preserving one inventory algorithm.


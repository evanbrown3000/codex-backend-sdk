# Unified App Server bridge

`b4pt0r-app-server` is a transport and projection layer beneath the existing
B4PT0R Electron interface. It does not replace the Codex interface or provider
implementations.

## Process boundaries

`CODEX_APP_SERVER_EXECUTABLE` selects this bridge in Electron.
`CODEX_EXECUTABLE` identifies the native Codex binary. The arguments Electron
supplies to the bridge are passed to native Codex unchanged. Native App Server
requests, responses, and notifications pass through unchanged unless the
request addresses a normalized Agent Memory thread or one of the two explicit
environment operations.

The bridge owns no provider credentials. `B4PT0R_PROVIDER_BROKER_COMMAND` is a
JSON array containing the command for the singular credential-owning provider
broker. The broker accepts a JSON operation on stdin and returns its JSON result
on stdout.

Agent Memory remains the read path for normalized conversations. The bridge
does not hydrate ChatGPT directly. It projects the normalized event graph into
the existing App Server thread and turn shapes.

## Remote Codex

`cognilode/environment/list` returns the relay's environments and the current
selection. `cognilode/environment/select` records an actor-scoped selection and
starts the native App Server through the existing remote-shell relay. The actor
identity can be supplied with `B4PT0R_ACTOR_ID` or `COGNILODE_ACTOR_ID`.
Without either variable it uses the
stable `b4pt0r-desktop` identity. Local and remote selections are both recorded
through the relay so a bridge restart keeps the last explicit choice.

The remote transport starts `CODEX_EXECUTABLE app-server --stdio`, writes App
Server JSON lines to the remote process, and consumes response deltas through
the relay. Remote shell remains the only network execution abstraction.

## Conversation continuation

For a normalized ChatGPT conversation, a new turn invokes
`chatgpt_continue` with the provider conversation and parent-message identity.
For a different source/destination pairing, it invokes
`conversation_continue`. The latter requires the broker to render the source
conversation from Agent Memory, attach it as `conversation.md`, and prepend the
mechanical instruction to read it before continuing. This reuses B4PT0R's
existing attachment, provider-send, collection, and ingestion paths.

Successful turns are emitted through the ordinary App Server turn lifecycle.
Returned artifacts remain attached to the operation result and are also
announced with `cognilode/artifactsAvailable` for clients that consume artifact
metadata.

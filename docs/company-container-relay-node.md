# Container relay node

The company Docker stack runs the remote-shell MCP node, the Codex native
exec-server, and the Cloudflare tunnel in containers. The node's default
workspace is `/runtime/source/current`, where source-sync publishes real Git
checkouts. `/codex/bin/codex` is installed by the persistent pinned builder;
the node shares the company's Codex home and the central rollout helper.
The relay advertises native Codex readiness only while port 8799 is accepting
connections.

The node requires two AWS Secrets Manager values in `us-west-2`:

- `cognilode/remote-shell/node`: JSON with `schema`, `node_id`, `node_token`,
  `mcp_bearer`, and `central_url`.
- `cognilode/remote-shell/tunnel-token`: the named Cloudflare tunnel token.

`credential-sync` materializes these in the `company_relay_custody` Docker
volume. A new device can use different secret IDs through
`COGNILODE_RELAY_NODE_SECRET_ID` and `COGNILODE_RELAY_TUNNEL_SECRET_ID`, so each
environment has its own relay identity while using the same Compose stack.
The tunnel's remotely configured origin must point to `127.0.0.1:8899`; it
shares the remote-shell container's network namespace.

On EvanPC, the one permitted host startup unit seeds AWS once from the former
company-only `evanpc-host-node.env` if the AWS secrets are absent. After a
fresh container heartbeat reaches the central relay, that bootstrap retires
the old host units and removes the old company env file. It never reads or
modifies EvanPC's personal browser or Codex profile. Subsequent starts and
new devices read AWS custody directly and run indefinitely in Docker.

The native exec-server is local to the container network namespace. The public
Cognilode AppServer compatibility route can invoke Codex through the remote
shell. Native WebSocket AppServer protocol parity across the relay remains a
separate integration limit; this deployment does not claim that protocol is
fully tunneled to the public endpoint.

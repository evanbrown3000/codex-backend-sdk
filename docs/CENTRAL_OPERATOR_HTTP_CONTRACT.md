# Central operator HTTP contract

Company agents call `https://cognilode.com/api/operator/*` through one authenticated
operator client. They do not hydrate provider conversations or send provider
mutations directly. The SDK implementation is
[`scripts/cognilode_central_http.py`](../scripts/cognilode_central_http.py);
Modified Codex uses its `Operator` client and its pinned rollout helper uses
the same wire contract.

Every request carries a bearer operator token, `Accept:
application/json,text/plain,*/*`, `Content-Type: application/json` for JSON
bodies, a browser-compatible `User-Agent`, and the exact central origin and
referer. The registered company-node heartbeat uses this same request shape.
Cloudflare error 1010 (`browser_signature_banned`) occurs before Pages code
runs, so changing a Pages handler cannot repair a rejected client signature.

Client errors expose only bounded machine error codes, never response HTML,
tokens, or provider payloads. A failed or timed-out provider-operation `start`
is ambiguous once the request might have crossed the mutation boundary:
retain its operation ID and reconcile the same operation read-only. Ordinary
conversation reads use the central stock/rollout API and per-reader cursors;
they do not independently poll ChatGPT.com.

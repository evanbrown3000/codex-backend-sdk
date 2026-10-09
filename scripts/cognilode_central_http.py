"""Shared authenticated HTTP contract for the central operator API.

Use this for SDK container clients. The browser-compatible request headers
match the registered node heartbeat and are required before Cloudflare routes
the request to Pages. Provider-specific HTTP belongs behind the central relay.
"""
from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

USER_AGENT = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')


class CentralHTTPError(RuntimeError):
    pass


def request_json(base: str, path: str, token: str, *, method: str = 'POST',
                 body: dict | None = None, timeout: int = 45) -> dict:
    base = base.rstrip('/')
    data = json.dumps(body, separators=(',', ':')).encode() if body is not None else None
    request = Request(base + path, data=data, method=method, headers={
        'authorization': 'Bearer ' + token.removeprefix('Bearer ').strip(),
        'content-type': 'application/json',
        'accept': 'application/json,text/plain,*/*',
        'user-agent': USER_AGENT,
        'origin': base,
        'referer': base + '/',
    })
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            failure = json.loads(error.read(4096))
        except (ValueError, UnicodeDecodeError):
            failure = {}
        code = str(failure.get('error_code') or failure.get('error') or 'http_error')[:80]
        raise CentralHTTPError(f'central_http_{error.code}:{code}') from error
    if not isinstance(result, dict):
        raise CentralHTTPError('central_response_not_object')
    return result

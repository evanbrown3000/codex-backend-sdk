"""Load the company HTTP gate into Python request clients in standard images."""
from __future__ import annotations

import os

if os.environ.get("COGNILODE_HTTP_GATE_BYPASS") != "1":
    from cognilode_http_gate import require

    try:
        import requests.sessions
        _request = requests.sessions.Session.request

        def _gated_request(self, method, url, *args, **kwargs):
            require(str(method), str(url))
            return _request(self, method, url, *args, **kwargs)

        requests.sessions.Session.request = _gated_request
    except ImportError:
        pass

    try:
        import curl_cffi.requests.session
        _curl_request = curl_cffi.requests.session.Session.request

        def _gated_curl_request(self, method, url, *args, **kwargs):
            require(str(method), str(url))
            return _curl_request(self, method, url, *args, **kwargs)

        curl_cffi.requests.session.Session.request = _gated_curl_request
    except ImportError:
        pass

    import urllib.request
    _urlopen = urllib.request.urlopen

    def _gated_urlopen(url, *args, **kwargs):
        method = url.get_method() if isinstance(url, urllib.request.Request) else ("POST" if kwargs.get("data") is not None else "GET")
        target = url.full_url if isinstance(url, urllib.request.Request) else str(url)
        require(method, target)
        return _urlopen(url, *args, **kwargs)

    urllib.request.urlopen = _gated_urlopen

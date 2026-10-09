"""Read a provider account-export ZIP through authenticated, identity-fenced byte ranges.

The export can contain tens of gigabytes of media.  ZipFile needs only the
central directory and compressed bytes for a selected member, so this avoids
downloading unrelated account content while preserving CRC and source identity.
The signed URL is supplied from a private file and is never logged.
"""
from __future__ import annotations

import io
import re
import zipfile
from collections import OrderedDict
from pathlib import Path

_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


class ExportChanged(RuntimeError):
    pass


class AuthenticatedRangeFile(io.RawIOBase):
    def __init__(self, session, url: str, *, chunk_size: int = 2 << 20):
        self.session, self.url, self.chunk_size = session, url, chunk_size
        head = session.head(url, timeout=60, allow_redirects=False)
        head.raise_for_status()
        self.size = int(head.headers["Content-Length"])
        self.etag = head.headers.get("ETag")
        self.modified = head.headers.get("Last-Modified")
        if head.headers.get("Accept-Ranges", "").lower() != "bytes" or not self.etag:
            raise ExportChanged("export lacks byte ranges or immutable ETag")
        self.position = 0
        self.cache: OrderedDict[int, bytes] = OrderedDict()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.position

    def seek(self, offset, whence=io.SEEK_SET):
        position = (offset if whence == io.SEEK_SET else
                    self.position + offset if whence == io.SEEK_CUR else
                    self.size + offset if whence == io.SEEK_END else None)
        if position is None or position < 0:
            raise ValueError("invalid export seek")
        self.position = position
        return position

    def _chunk(self, start):
        if start in self.cache:
            self.cache.move_to_end(start)
            return self.cache[start]
        end = min(start + self.chunk_size, self.size) - 1
        response = self.session.get(self.url, headers={
            "Range": f"bytes={start}-{end}", "If-Range": self.etag,
        }, timeout=120, allow_redirects=False)
        expected = f"bytes {start}-{end}/{self.size}"
        if (response.status_code != 206 or response.headers.get("Content-Range") != expected
                or response.headers.get("ETag") != self.etag
                or len(response.content) != end - start + 1):
            raise ExportChanged("export range identity/length changed")
        self.cache[start] = response.content
        if len(self.cache) > 4:
            self.cache.popitem(last=False)
        return response.content

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.size - self.position
        remaining = min(size, max(0, self.size - self.position))
        parts = []
        while remaining:
            start = self.position // self.chunk_size * self.chunk_size
            chunk = self._chunk(start)
            offset = self.position - start
            part = chunk[offset:offset + remaining]
            if not part:
                raise ExportChanged("empty range before end of export")
            parts.append(part)
            self.position += len(part)
            remaining -= len(part)
        return b"".join(parts)


def members(session, url: str):
    source = AuthenticatedRangeFile(session, url)
    with zipfile.ZipFile(source) as archive:
        return source, archive.infolist()


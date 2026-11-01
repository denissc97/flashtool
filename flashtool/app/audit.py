"""Append-only audit log with a hash chain: editing or deleting a past entry breaks the chain."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import threading

_lock = threading.Lock()
GENESIS = "0" * 64


def _digest(prev: str, body: dict) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((prev + canonical).encode()).hexdigest()


def _last_hash(path: str) -> str:
    if not os.path.exists(path):
        return GENESIS
    last = GENESIS
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                last = json.loads(line)["hash"]
    return last


def append(path: str, entry: dict) -> dict:
    """Append one entry. Concurrent jobs are serialised with an exclusive lock file."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    body = dict(entry)
    body.setdefault("user", getpass.getuser())
    body.setdefault("host", socket.gethostname())
    with _lock, _FileLock(path + ".lock"):
        prev = _last_hash(path)
        record = {"prev": prev, "hash": _digest(prev, body), **body}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def verify(path: str) -> tuple[bool, int, str | None]:
    """(ok, number of entries, problem)."""
    prev = GENESIS
    count = 0
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            body = {k: v for k, v in record.items() if k not in ("prev", "hash")}
            if record.get("prev") != prev:
                return False, count, f"line {lineno}: previous hash does not match"
            if _digest(prev, body) != record.get("hash"):
                return False, count, f"line {lineno}: entry was modified"
            prev = record["hash"]
            count += 1
    return True, count, None


class _FileLock:
    """Minimal cross-process lock (O_EXCL lock file) without extra dependencies."""

    def __init__(self, path: str, timeout: float = 10.0):
        self.path, self.timeout = path, timeout

    def __enter__(self):
        import time

        end = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                if time.monotonic() > end:
                    raise TimeoutError(f"audit log is locked ({self.path}); remove the file if no job is running")
                time.sleep(0.02)

    def __exit__(self, *exc):
        os.close(self._fd)
        os.unlink(self.path)

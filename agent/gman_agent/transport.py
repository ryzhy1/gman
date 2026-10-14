"""Delivery to the API with retries, the on-disk spool of unsent snapshots and the run lock."""
import errno
import fcntl
import json
import os
import random
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .common import log


# --- Delivery -------------------------------------------------------------------------------------

class Retryable(Exception):
    """Transient failure (network, timeout, 5xx, 429): keep the snapshot and retry later."""


class Blocked(Exception):
    """The API or a proxy in front of it refuses the agent (401/403): retrying now is pointless, but the snapshot is kept."""


class Rejected(Exception):
    """The API permanently rejects this snapshot (e.g. 422): drop it so it does not block the spool."""


def endpoint():
    url = os.environ["GMAN_URL"].rstrip("/")
    if not url.startswith(("https://", "http://")):
        raise ValueError("GMAN_URL must be an HTTP(S) URL")
    return url + "/api/inventory"


def post(data, url):
    request = Request(url, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            # 200 with accepted=false means a duplicate or older snapshot: replay is idempotent.
            print(response.status, response.read(500).decode(errors="replace"), flush=True)
    except HTTPError as error:
        # The API's own error body contains no inventory or credentials.
        detail = f"HTTP {error.code} {error.read(1000).decode(errors='replace')}"
        if error.code in (408, 425, 429) or error.code >= 500:
            raise Retryable(detail)
        if error.code in (401, 403):
            raise Blocked(detail)
        raise Rejected(detail)
    except (URLError, OSError, HTTPException) as error:
        raise Retryable(type(error).__name__)


def backoff(attempt, base, cap, rng=random.random):
    # Exponential backoff with "equal jitter": at least half of the step, so delays still grow.
    step = min(cap, base * 2 ** attempt)
    return step / 2 + rng() * step / 2


def send_with_retry(data, deliver, retries=3, base=2.0, cap=30.0, sleep=time.sleep, rng=random.random):
    for attempt in range(retries + 1):
        try:
            return deliver(data)
        except Retryable as error:
            if attempt == retries:
                raise
            delay = backoff(attempt, base, cap, rng)
            log(f"delivery failed ({error}); retry {attempt + 1}/{retries} in {delay:.1f}s")
            sleep(delay)


# --- Spool ----------------------------------------------------------------------------------------

class Spool:
    """Unsent snapshots, one JSON file each, named by a monotonic sequence and sent oldest first.

    Bounded by file count and total bytes. The oldest snapshots are dropped first (the server
    ignores snapshots older than the latest accepted one anyway); the drop is reported in the
    errors of the snapshot being added, so the gap is visible in the portal.
    """

    def __init__(self, directory, max_files=100, max_bytes=50 * 1024 ** 2):
        self.directory, self.max_files, self.max_bytes = Path(directory), max(1, max_files), max_bytes
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for stale in self.directory.glob("*.tmp"):
            stale.unlink(missing_ok=True)

    def items(self):
        return sorted(p for p in self.directory.glob("*.json") if p.stem.isdigit())

    def put(self, data):
        files = self.items()
        sequence = max(time.time_ns(), int(files[-1].stem) + 1 if files else 0)
        sizes = [p.stat().st_size for p in files]
        payload = json.dumps(data).encode()
        dropped = 0
        # The newest snapshot is always kept, even if it alone exceeds max_bytes. Once something is
        # dropped, room is reserved for the overflow note appended below.
        while dropped < len(files) and (len(files) - dropped + 1 > self.max_files or sum(sizes[dropped:])
                                        + len(payload) + (128 if dropped else 0) > self.max_bytes):
            files[dropped].unlink(missing_ok=True)
            dropped += 1
        if dropped:
            log(f"spool full: dropped {dropped} oldest unsent snapshot(s)")
            data = dict(data, errors=list(data.get("errors") or [])[:49]
                        + [f"agent spool overflow: dropped {dropped} oldest unsent snapshot(s)"])
            payload = json.dumps(data).encode()
        path = self.directory / f"{sequence:020d}.json"
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        return path

    def flush(self, deliver):
        """Send spooled snapshots in order, stopping at the first that cannot be delivered now.

        Returns the number of snapshots still waiting.
        """
        items = self.items()
        for index, path in enumerate(items):
            try:
                data = json.loads(path.read_bytes())
            except (OSError, ValueError):
                log(f"dropping unreadable spool file {path.name}")
                path.unlink(missing_ok=True)
                continue
            try:
                deliver(data)
            except Rejected as error:
                log(f"snapshot {path.name} rejected, dropped: {error}")
            except (Retryable, Blocked) as error:
                log(f"delivery failed, {len(items) - index} snapshot(s) kept in spool: {error}")
                return len(items) - index
            path.unlink(missing_ok=True)
        return 0


# --- Run lock -------------------------------------------------------------------------------------

def acquire_lock(path):
    """Non-blocking exclusive flock. Returns the open fd (held until exit), or None if busy."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(fd)
        if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return None
        raise
    return fd

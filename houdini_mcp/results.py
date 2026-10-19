"""Where a result too large to read inline is kept, and for how long.

[LAW:single-enforcer] A server owns one directory of its own under the results
root, and only it removes a file there: it alone knows which of its results have
been handed to their callers. It keeps its newest results within
``RESULTS_BUDGET_BYTES``, the newest always.

[LAW:no-ambient-temporal-coupling] Whether a directory's server still runs is a
lock the operating system holds for that server's process, released however the
process ends. A starting server removes the directory of every server whose lock
it can take, so restarts are bounded too, and never a running server's results.
"""

import logging
import os
import re
import shutil
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

logger = logging.getLogger("houdini_mcp.results")

RESULTS_BUDGET_BYTES = 64 * 1024 * 1024
"""The most result text one server keeps on disk: its newest results that fit together, the newest always."""

# [LAW:one-source-of-truth] The removal rule as every caller is told it: at
# initialize, and by the read of a result that is not held.
RETENTION = (
    f"a result file is kept until this server's newer results fill "
    f"{RESULTS_BUDGET_BYTES // 2**20}MB; then it is removed. The newest result is "
    f"always kept, and a server's results are removed after it stops, when another starts"
)

# [LAW:parse-dont-validate] The one shape of name ``keep`` gives a file, and so the
# only names a read serves.
RESULT_NAME = re.compile(r"[a-z0-9_]+-[0-9a-f]{32}\.txt")

# The lock a running server holds, beside the directory of the same name.
OWNER_LOCK = re.compile(r"[0-9a-f]{32}\.lock")


if sys.platform == "win32":
    import msvcrt

    def taken(lock: BinaryIO) -> bool:
        """Whether this process now holds ``lock``; False while another process does."""
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except PermissionError:
            return False
        return True

else:
    import fcntl

    def taken(lock: BinaryIO) -> bool:
        """Whether this process now holds ``lock``; False while another process does."""
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


@dataclass(frozen=True)
class Kept:
    """A result written whole, and what keeping it within the budget removed."""

    path: Path
    removed: int
    removed_bytes: int
    failed: int


class Results:
    """One running server's results directory: the files it delivered, oldest first."""

    def __init__(self, directory: Path, lock: BinaryIO) -> None:
        self.directory = directory
        self._lock = lock
        self._guard = threading.Lock()
        self._kept: deque[tuple[Path, int]] = deque()
        self._held = 0

    @classmethod
    def claim(cls, root: Path) -> "Results":
        """A directory of this server's own under ``root``, held while it runs; the
        directories of servers that have stopped are removed.

        Raises:
            OSError: ``root`` cannot hold a directory of this server's own.
        """
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        owner = uuid.uuid4().hex
        # [LAW:no-ambient-temporal-coupling] the lock exists before the directory, so
        # a directory is never seen without a lock that says whether its server runs.
        lock = open(root / f"{owner}.lock", "a+b")  # noqa: SIM115 - held for the server's life
        if not taken(lock):
            lock.close()
            raise OSError(f"could not lock {root / owner}.lock for this server")
        directory = root / owner
        directory.mkdir(mode=0o700)
        swept = sweep(root, owner)
        logger.info(
            "results kept in %s; removed the results of %d stopped servers", directory, swept
        )
        return cls(directory, lock)

    def keep(self, tool: str, data: bytes) -> Kept:
        """``data`` written whole as ``tool``'s newest result, and older ones removed
        until this server's results fit the budget.

        A result still being written is not yet kept, so no other delivery removes it.

        Raises:
            OSError: ``data`` could not be written; nothing is removed.
        """
        path = self.directory / f"{tool}-{uuid.uuid4().hex}.txt"
        write_private(path, data)
        removed = removed_bytes = failed = 0
        with self._guard:
            self._kept.append((path, len(data)))
            self._held += len(data)
            while self._held > RESULTS_BUDGET_BYTES and len(self._kept) > 1:
                oldest, size = self._kept.popleft()
                self._held -= size
                try:
                    oldest.unlink(missing_ok=True)
                except OSError as e:
                    logger.warning(
                        "could not remove %s to keep results within the budget: %s", oldest, e
                    )
                    failed += 1
                    continue
                removed += 1
                removed_bytes += size
        return Kept(path, removed, removed_bytes, failed)

    def read(self, name: str) -> str:
        """The text of a result this server kept, by the name in its ``result_uri``.

        Raises:
            ValueError: ``name`` is not a name ``keep`` gives.
            FileNotFoundError: this server does not hold it.
        """
        if not RESULT_NAME.fullmatch(name):
            raise ValueError(f"not a delivered result name: {name!r}")
        try:
            return (self.directory / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.info("read of result %s missed: not in %s", name, self.directory)
            raise FileNotFoundError(
                f"result {name} is not held by this server: either it was removed, since "
                f"{RETENTION}; or another server wrote it."
            ) from None

    def close(self) -> None:
        """Release the lock: the next server to start removes these results."""
        self._lock.close()

    def __enter__(self) -> "Results":
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self.close()


def write_private(path: Path, data: bytes) -> None:
    """``data`` as a new file at ``path`` only its user can read, or nothing at ``path``."""
    with open(path, "xb", opener=lambda file, flags: os.open(file, flags, 0o600)) as out:
        try:
            out.write(data)
        except OSError:
            try:
                path.unlink()
            except OSError as left:
                # The write's error is the one the caller is told; this one is the operator's.
                logger.warning("could not remove the partly written %s: %s", path, left)
            raise


def sweep(root: Path, own: str) -> int:
    """Remove the directory of every server under ``root`` that has stopped; how many."""
    swept = 0
    for lock_path in root.iterdir():
        if not OWNER_LOCK.fullmatch(lock_path.name) or lock_path.stem == own:
            continue
        directory = root / lock_path.stem
        try:
            with open(lock_path, "a+b") as lock:
                if not taken(lock):
                    continue
                # A server that stopped between taking its lock and making its
                # directory leaves only the lock.
                if directory.is_dir():
                    shutil.rmtree(directory)
            lock_path.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("could not remove %s, a stopped server's results: %s", directory, e)
            continue
        swept += 1
    return swept

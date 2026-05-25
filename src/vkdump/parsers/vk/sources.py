"""Adapter that lets the rest of the parser treat a directory and a ZIP
archive interchangeably.

The whole parser layer talks to a `Source` via POSIX-style relative paths
("messages/<peer>/messages0.html") — the implementation decides whether
to read from the live filesystem or from the still-zipped archive.

ZIP support keeps the archive file handle open and streams entries on demand;
nothing is extracted to disk.
"""
from __future__ import annotations

import io
import posixpath
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path


class Source(ABC):
    """Read-only filesystem-shaped view rooted at the dump's canonical root."""

    @abstractmethod
    def read_bytes(self, rel: str) -> bytes: ...

    @abstractmethod
    def exists(self, rel: str) -> bool: ...

    @abstractmethod
    def is_file(self, rel: str) -> bool: ...

    @abstractmethod
    def is_dir(self, rel: str) -> bool: ...

    @abstractmethod
    def listdir(self, rel: str) -> list[str]:
        """Names of immediate children of `rel`. Subdirs returned without
        a trailing slash. Order is unspecified — callers sort if needed.
        """

    @abstractmethod
    def describe(self) -> str:
        """Human-readable label for logs (e.g. the filesystem path)."""

    def close(self) -> None:
        return None

    def __enter__(self) -> "Source":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _norm(rel: str) -> str:
    """Normalise a posix-style relative path; '' means the source root."""
    if not rel or rel == ".":
        return ""
    # Strip leading slashes; collapse './' and '..' segments defensively.
    parts: list[str] = []
    for seg in rel.replace("\\", "/").split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


# ---------- directory ----------


class DirectorySource(Source):
    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()
        if not self._root.is_dir():
            raise ValueError(f"Not a directory: {self._root}")

    def _abs(self, rel: str) -> Path:
        rel = _norm(rel)
        return self._root if not rel else self._root / rel

    def read_bytes(self, rel: str) -> bytes:
        return self._abs(rel).read_bytes()

    def exists(self, rel: str) -> bool:
        return self._abs(rel).exists()

    def is_file(self, rel: str) -> bool:
        return self._abs(rel).is_file()

    def is_dir(self, rel: str) -> bool:
        return self._abs(rel).is_dir()

    def listdir(self, rel: str) -> list[str]:
        return [p.name for p in self._abs(rel).iterdir()]

    def describe(self) -> str:
        return str(self._root)


# ---------- zip ----------


class ZipSource(Source):
    """A Source backed by an open `zipfile.ZipFile`.

    On init we walk the archive's name list once to:
    1. Detect whether the archive wraps its contents in a single top-level
       directory (most VK archives do) — that prefix becomes invisible to
       callers so paths look the same as in the directory case.
    2. Build a directory index so `listdir` / `is_dir` / `is_file` are O(1).
    """

    def __init__(self, zip_path: Path) -> None:
        self._zip_path = zip_path.expanduser().resolve()
        self._zf = zipfile.ZipFile(self._zip_path, mode="r")
        self._prefix = self._detect_inner_root_prefix()
        # _files: set of file rel-paths. _dirs: set of dir rel-paths.
        # _children: dir rel-path -> list[child basename].
        self._files: set[str] = set()
        self._dirs: set[str] = set([""])
        self._children: dict[str, list[str]] = {"": []}
        self._index()

    def _detect_inner_root_prefix(self) -> str:
        names = self._zf.namelist()
        if not names:
            return ""
        # All entries should share the same top-level segment for the wrap
        # case. Otherwise we operate at the archive root.
        first_segs: set[str] = set()
        for n in names:
            if not n:
                continue
            first_segs.add(n.split("/", 1)[0])
        if len(first_segs) != 1:
            return ""
        top = next(iter(first_segs))
        # Confirm there's at least one nested entry; a single bare file
        # named `foo` would also fall into first_segs={'foo'} but should
        # not be treated as a directory prefix.
        nested = any(n.startswith(top + "/") and n != top + "/" for n in names)
        if not nested:
            return ""
        return top + "/"

    def _strip_prefix(self, name: str) -> str | None:
        if self._prefix and not name.startswith(self._prefix):
            return None
        return name[len(self._prefix):]

    def _index(self) -> None:
        for info in self._zf.infolist():
            inner = self._strip_prefix(info.filename)
            if inner is None or inner == "":
                continue
            if info.is_dir():
                self._add_dir(inner.rstrip("/"))
            else:
                self._add_file(inner)

    def _add_dir(self, rel: str) -> None:
        rel = _norm(rel)
        if not rel:
            return
        if rel in self._dirs:
            return
        self._dirs.add(rel)
        self._children.setdefault(rel, [])
        parent, _, name = rel.rpartition("/")
        self._add_dir(parent) if parent else self._children[""].append(name)
        if parent:
            self._children.setdefault(parent, []).append(name)

    def _add_file(self, rel: str) -> None:
        rel = _norm(rel)
        if not rel:
            return
        self._files.add(rel)
        parent, _, name = rel.rpartition("/")
        if parent:
            self._add_dir(parent)
            self._children.setdefault(parent, []).append(name)
        else:
            self._children[""].append(name)

    def read_bytes(self, rel: str) -> bytes:
        rel = _norm(rel)
        if rel not in self._files:
            raise FileNotFoundError(rel)
        return self._zf.read(self._prefix + rel)

    def exists(self, rel: str) -> bool:
        rel = _norm(rel)
        return rel in self._files or rel in self._dirs

    def is_file(self, rel: str) -> bool:
        return _norm(rel) in self._files

    def is_dir(self, rel: str) -> bool:
        return _norm(rel) in self._dirs

    def listdir(self, rel: str) -> list[str]:
        rel = _norm(rel)
        if rel not in self._dirs:
            raise FileNotFoundError(rel)
        return list(self._children.get(rel, ()))

    def describe(self) -> str:
        suffix = f" (wrapped in {self._prefix.rstrip('/')!r})" if self._prefix else ""
        return f"{self._zip_path}{suffix}"

    def close(self) -> None:
        self._zf.close()


# ---------- factory ----------


def open_source(path: Path) -> Source:
    """Open a Source for the given filesystem path.

    Accepts:
    - a directory → DirectorySource rooted there;
    - a ZIP file → ZipSource holding the archive open (no extraction);
    - a single HTML file → DirectorySource rooted at the file's parent dir
      (the caller is expected to look at the original filename to decide
      how to interpret it).
    """
    p = path.expanduser().resolve()
    if not p.exists():
        raise ValueError(f"Source does not exist: {p}")
    if p.is_file() and zipfile.is_zipfile(p):
        return ZipSource(p)
    if p.is_file():
        return DirectorySource(p.parent)
    return DirectorySource(p)


def join(*parts: str) -> str:
    """Posix-style join; ignores empty leading segments."""
    cleaned = [p for p in parts if p]
    if not cleaned:
        return ""
    return _norm(posixpath.join(*cleaned))


__all__ = [
    "DirectorySource",
    "Source",
    "ZipSource",
    "join",
    "open_source",
]

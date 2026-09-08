"""Bounded, explicit client-directory uploads. Never follow local symlinks."""

import base64
from contextlib import contextmanager
import fnmatch
from functools import lru_cache
import gzip
import io
import os
import posixpath
import stat
import tarfile

COMPRESSED_MAX = 32 << 20
EXPANDED_MAX = 256 << 20
ENTRIES_MAX = 10_000
IGNORE_MAX = 64 << 10


class _BoundedWriter:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.count = stream, maximum, 0

    def write(self, data):
        self.count += len(data)
        if self.count > self.maximum:
            raise ValueError("Build context archive exceeds upload limits")
        return self.stream.write(data)


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


@contextmanager
def _open_entry(parent, name, expected, *, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        actual = os.fstat(descriptor)
        if _identity(actual) != _identity(expected):
            raise ValueError("Build context changed while opening an entry")
        yield descriptor
        if _identity(os.fstat(descriptor)) != _identity(expected):
            raise ValueError("Build context changed during upload preparation")
    finally:
        os.close(descriptor)


def _ignore_rules(root):
    try:
        entry = os.stat(".dockerignore", dir_fd=root, follow_symlinks=False)
    except FileNotFoundError:
        return []
    if not stat.S_ISREG(entry.st_mode) or entry.st_size > IGNORE_MAX:
        raise ValueError(".dockerignore must be a regular file of at most 64 KiB")
    with _open_entry(root, ".dockerignore", entry) as descriptor:
        with os.fdopen(os.dup(descriptor), "rb") as stream:
            raw = stream.read(IGNORE_MAX + 1)
    if len(raw) != entry.st_size:
        raise ValueError(".dockerignore changed during upload preparation")
    rules = []
    for line in raw.decode("utf-8-sig").split("\n"):
        if line.startswith("#") or not line.strip():
            continue
        pattern = line.strip()
        include = pattern.startswith("!")
        if include:
            pattern = pattern[1:].strip()
        if not pattern or len(pattern) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in pattern):
            raise ValueError("Invalid or oversized .dockerignore pattern")
        # Refuse syntax outside the qualified grammar rather than risk uploading
        # something that the user's Docker matcher would have excluded.
        if any(c in pattern for c in "[]\\"):
            raise ValueError(".dockerignore character classes and escapes are not supported")
        pattern = posixpath.normpath(pattern).lstrip("/")
        if pattern in ("", "."):
            continue
        parts = tuple(pattern.split("/"))
        if any("**" in part and part != "**" for part in parts):
            raise ValueError(".dockerignore ** must occupy a whole path segment")
        rules.append((parts, include))
        if len(rules) > 256:
            raise ValueError(".dockerignore contains more than 256 patterns")
    return rules


def _matches(pattern, path):
    @lru_cache(maxsize=None)
    def visit(p, n):
        if p == len(pattern):
            return n == len(path)
        if pattern[p] == "**":
            if p == len(pattern) - 1:
                return n < len(path) or p == 0
            return visit(p + 1, n) or (n < len(path) and visit(p, n + 1))
        return n < len(path) and fnmatch.fnmatchcase(path[n], pattern[p]) and visit(p + 1, n + 1)
    return visit(0, 0)


def _excluded(path, rules):
    parts = tuple(path.split("/"))
    excluded = False
    for pattern, include in rules:
        if include != excluded:
            continue
        if any(_matches(pattern, parts[:n]) for n in range(1, len(parts) + 1)):
            excluded = not include
    return excluded


def pack_context(context):
    """Encode a selected POSIX directory, excluding qualified .dockerignore rules.

    Symlinks and special files are rejected unless excluded. An upload is never
    sent if the directory changes while it is being read. Paths, entry count,
    archive size and recursion are bounded independently of compression ratio.
    """
    if not context:
        raise ValueError("Select a nonempty local build context path")
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("Safe local build-context upload requires POSIX descriptor support")
    try:
        root = os.open(context, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            root_before = os.fstat(root)
            rules = _ignore_rules(root)
            compressed = io.BytesIO()
            visited = 0
            with gzip.GzipFile(fileobj=_BoundedWriter(compressed, COMPRESSED_MAX), mode="wb", mtime=0) as zipped:
                with tarfile.open(fileobj=_BoundedWriter(zipped, EXPANDED_MAX), mode="w|", format=tarfile.PAX_FORMAT) as archive:
                    def walk(directory, prefix="", depth=0):
                        nonlocal visited
                        if depth > 64:
                            raise ValueError("Build context exceeds 64 directory levels")
                        # scandir is streamed so an oversized directory cannot
                        # allocate an unbounded list before the entry check.
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                visited += 1
                                if visited > ENTRIES_MAX:
                                    raise ValueError("Build context contains more than 10000 visited entries")
                                name = prefix + entry.name
                                if len(os.fsencode(name)) > 4096 or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name):
                                    raise ValueError("Invalid build context path")
                                if name == ".dockerignore":
                                    continue
                                ignored = _excluded(name, rules)
                                info = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
                                is_directory = stat.S_ISDIR(info.st_mode)
                                if ignored and not (is_directory and any(include for _, include in rules)):
                                    continue
                                if not (is_directory or stat.S_ISREG(info.st_mode)):
                                    raise ValueError("Build context symlinks and special files are not supported; exclude them explicitly")
                                member = tarfile.TarInfo(name)
                                member.mode = 0o755 if is_directory or info.st_mode & 0o111 else 0o644
                                if is_directory:
                                    member.type = tarfile.DIRTYPE
                                    if not ignored:
                                        archive.addfile(member)
                                    with _open_entry(directory, entry.name, info, directory=True) as child:
                                        walk(child, name + "/", depth + 1)
                                else:
                                    if info.st_size > EXPANDED_MAX:
                                        raise ValueError("Build context file exceeds upload limit")
                                    member.size = info.st_size
                                    with _open_entry(directory, entry.name, info) as descriptor:
                                        with os.fdopen(os.dup(descriptor), "rb") as stream:
                                            archive.addfile(member, stream)
                    walk(root)
            if _identity(os.fstat(root)) != _identity(root_before):
                raise ValueError("Build context root changed during upload preparation")
            return base64.b64encode(compressed.getvalue()).decode("ascii")
        finally:
            os.close(root)
    except (OSError, UnicodeError, tarfile.TarError) as error:
        raise ValueError("Cannot safely read the selected local build context") from error

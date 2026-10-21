"""Versioned text artifacts and deterministic, role-scoped candidate assembly."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import unicodedata


MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
_ROLES = {"builder", "verifier", "docs"}
_FORBIDDEN_NAMES = {"conftest.py", "sitecustomize.py", "usercustomize.py", "__pycache__"}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class PolicyError(ValueError):
    """A candidate or change violates the artifact boundary."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parts(relative: str) -> tuple[str, ...]:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise PolicyError("Artifact paths must be nonempty relative POSIX paths")
    parts = tuple(relative.split("/"))
    if any(
        not part
        or part in {".", ".."}
        or part.startswith(".")
        or part.casefold() in _FORBIDDEN_NAMES
        or any(ord(character) < 32 or ord(character) == 127 for character in part)
        for part in parts
    ):
        raise PolicyError(f"Forbidden artifact path: {relative!r}")
    try:
        relative.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PolicyError("Artifact paths must be valid UTF-8") from error
    return parts


def _file_role(relative: str) -> str:
    parts = _parts(relative)
    if len(parts) >= 3 and parts[:2] == ("service", "shortener") and parts[-1].endswith(".py"):
        return "builder"
    if len(parts) == 2 and parts[0] == "generated_tests" and parts[-1].startswith("test_") and parts[-1].endswith(".py"):
        return "verifier"
    if len(parts) == 2 and parts[0] == "docs" and parts[-1].endswith(".md"):
        return "docs"
    raise PolicyError(f"Unknown or protected artifact path: {relative}")


def _check_directory(relative: str) -> None:
    parts = _parts(relative)
    if parts in {("service",), ("generated_tests",), ("docs",)}:
        return
    if len(parts) >= 2 and parts[:2] == ("service", "shortener"):
        return
    raise PolicyError(f"Unknown or protected artifact directory: {relative}")


def _absolute(path: Path) -> Path:
    path = Path(path)
    if ".." in path.parts:
        raise PolicyError("Workspace paths cannot contain parent traversal")
    return path if path.is_absolute() else Path.cwd() / path


def _open_directory(path: Path, *, create: bool = False) -> int:
    """Walk every ancestor using directory handles, refusing symlink traversal."""
    path = _absolute(path)
    descriptor = os.open(path.anchor, _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            if create:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _text_bytes(content: str, path: str) -> bytes:
    if not isinstance(content, str):
        raise PolicyError(f"Content must be text; deletion is forbidden: {path}")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as error:
        raise PolicyError(f"Content must be valid UTF-8: {path}") from error
    if any(ord(character) < 32 and character not in "\t\n\r" for character in content) or "\x7f" in content:
        raise PolicyError(f"Binary or control-character content is forbidden: {path}")
    if len(encoded) > MAX_FILE_BYTES:
        raise PolicyError(f"Artifact exceeds the {MAX_FILE_BYTES}-byte file limit: {path}")
    return encoded


def _read_text(directory: int, name: str, relative: str) -> str:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PolicyError(f"Artifact must be a regular file with a single hard link: {relative}")
        if before.st_size > MAX_FILE_BYTES:
            raise PolicyError(f"Artifact exceeds the file limit: {relative}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise PolicyError(f"Artifact changed while being read: {relative}")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PolicyError(f"Artifact is not UTF-8 text: {relative}") from error
        _text_bytes(content, relative)
        return content
    finally:
        os.close(descriptor)


def _check_path_collisions(paths: list[str]) -> None:
    seen: dict[str, str] = {}
    files = set(paths)
    for path in paths:
        parts = _parts(path)
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            key = unicodedata.normalize("NFC", prefix).casefold()
            previous = seen.setdefault(key, prefix)
            if previous != prefix:
                raise PolicyError(f"Case or Unicode path collision: {previous!r} and {prefix!r}")
            if length < len(parts) and prefix in files:
                raise PolicyError(f"A file cannot also be an artifact directory: {prefix}")


def snapshot_files(root: Path) -> dict[str, str]:
    """Read only allowlisted regular UTF-8 files, rejecting every unknown entry."""
    result: dict[str, str] = {}
    paths: list[str] = []
    total = 0

    def visit(directory: int, prefix: str = "") -> None:
        nonlocal total
        with os.scandir(directory) as entries:
            names = sorted(entry.name for entry in entries)
        for name in names:
            relative = f"{prefix}/{name}" if prefix else name
            paths.append(relative)
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _check_directory(relative)
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(info.st_mode):
                _file_role(relative)
                content = _read_text(directory, name, relative)
                total += len(content.encode("utf-8"))
                if total > MAX_TOTAL_BYTES:
                    raise PolicyError(f"Candidate exceeds the {MAX_TOTAL_BYTES}-byte total limit")
                result[relative] = content
            else:
                raise PolicyError(f"Symlinks and special files are forbidden: {relative}")

    try:
        descriptor = _open_directory(root)
        try:
            visit(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise PolicyError(f"Cannot safely read candidate: {error.strerror}") from error
    # Include empty directories in case-collision checks without treating them as files.
    canonical: dict[str, str] = {}
    for path in paths:
        key = unicodedata.normalize("NFC", path).casefold()
        previous = canonical.setdefault(key, path)
        if previous != path:
            raise PolicyError(f"Case or Unicode path collision: {previous!r} and {path!r}")
    _check_path_collisions(list(result))
    return dict(sorted(result.items()))


def _manifest(files: dict[str, str]) -> dict:
    hashes = {path: sha256_bytes(content.encode("utf-8")) for path, content in sorted(files.items())}
    digest = sha256_bytes(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {"hash": digest, "files": hashes}


def manifest(root: Path) -> dict:
    return _manifest(snapshot_files(root))


def verify_manifest(root: Path, expected: dict) -> None:
    if not isinstance(expected, dict) or set(expected) != {"hash", "files"} or manifest(root) != expected:
        raise PolicyError("Candidate does not match its expected manifest")


def apply_changes(base: Path, destination: Path, changes: list[dict], role: str) -> dict:
    """Create a fresh candidate; retain all paths if assembly or validation fails."""
    if not isinstance(role, str) or role not in _ROLES:
        raise PolicyError(f"Unknown change role: {role}")
    if not isinstance(changes, list):
        raise PolicyError("Changes must be a list")
    base, destination = _absolute(base), _absolute(destination)
    if destination == base or base in destination.parents:
        raise PolicyError("The destination cannot be inside the base candidate")
    original = snapshot_files(base)
    combined = dict(original)
    changed: set[str] = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"path", "previous_hash", "content"}:
            raise PolicyError("Each change requires exactly path, previous_hash, and content")
        path = change["path"]
        if _file_role(path) != role:
            raise PolicyError(f"Role {role} cannot modify {path}")
        if path in changed:
            raise PolicyError(f"Duplicate change path: {path}")
        changed.add(path)
        expected_hash = sha256_bytes(original[path].encode("utf-8")) if path in original else None
        if change["previous_hash"] != expected_hash:
            raise PolicyError(f"Stale or incorrect previous content hash: {path}")
        _text_bytes(change["content"], path)
        combined[path] = change["content"]
    _check_path_collisions(list(combined))
    if sum(len(content.encode("utf-8")) for content in combined.values()) > MAX_TOTAL_BYTES:
        raise PolicyError(f"Candidate exceeds the {MAX_TOTAL_BYTES}-byte total limit")
    expected = _manifest(combined)
    if snapshot_files(base) != original:
        raise PolicyError("The base candidate changed before assembly")

    descriptor = None
    try:
        parent = _open_directory(destination.parent, create=True)
        try:
            os.mkdir(destination.name, dir_fd=parent)
            descriptor = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)
        for path, content in sorted(combined.items()):
            parts = _parts(path)
            directory = os.dup(descriptor)
            try:
                for component in parts[:-1]:
                    try:
                        os.mkdir(component, dir_fd=directory)
                    except FileExistsError:
                        pass
                    child = os.open(component, _DIRECTORY_FLAGS, dir_fd=directory)
                    os.close(directory)
                    directory = child
                output = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(output, "wb") as stream:
                    stream.write(content.encode("utf-8"))
            finally:
                os.close(directory)
    except OSError as error:
        raise PolicyError(f"Cannot safely assemble candidate; retained any partial output: {error.strerror}") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if snapshot_files(base) != original:
        raise PolicyError("The base candidate changed during assembly; output is retained but rejected")
    verify_manifest(destination, expected)
    return expected

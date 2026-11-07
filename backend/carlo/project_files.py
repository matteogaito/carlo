"""Read the visible, non-ignored working tree without exposing arbitrary paths."""

import os
import stat
import subprocess
from pathlib import Path

from fastapi import HTTPException


def _git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], input=input,
            capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HTTPException(503, "Project directory is unavailable") from error
    if result.returncode not in ({0, 1} if args[0] == "check-ignore" else {0}):
        raise HTTPException(503, "Project directory is unavailable")
    return result.stdout


def _visible_file(root: Path, name: str) -> bool:
    path = Path(name)
    if path.is_absolute() or not path.parts or any(part.startswith(".") for part in path.parts):
        return False
    candidate = root / path
    # Exclude symlinks, including intermediate directories, even within the project.
    return candidate.resolve() == candidate and candidate.is_file()


def list_files(root: Path) -> list[str]:
    root = root.resolve()
    names = _git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
    names = [name for name in names if name]
    if not names:
        return []
    ignored = set(_git(root, "check-ignore", "--no-index", "-z", "--stdin", input=b"\0".join(names) + b"\0").split(b"\0"))
    return sorted({os.fsdecode(name) for name in names if name not in ignored and _visible_file(root, os.fsdecode(name))})


def read_file(root: Path, path: str) -> dict[str, str]:
    root = root.resolve()
    if path not in list_files(root):
        raise HTTPException(404, "File is unavailable or excluded")
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = Path(path).parts
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise HTTPException(404, "File is unavailable or excluded")
            content = source.read(2 * 1024 * 1024 + 1)
    except OSError as error:
        raise HTTPException(404, "File is unavailable or excluded") from error
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(415, "Preview supports text files up to 2 MiB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(415, "Preview supports UTF-8 text files only") from error
    if any(ord(char) < 32 and char not in "\n\r\t\f" for char in text):
        raise HTTPException(415, "Binary files cannot be previewed")
    return {"path": path, "content": text}

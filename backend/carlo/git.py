import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Worktree:
    branch: str
    path: Path


class GitWorkspace:
    def __init__(
        self,
        repository: Path,
        worktree_root: Path,
        integration_branch: str,
    ) -> None:
        self.repository = repository.resolve()
        self.worktree_root = worktree_root.resolve()
        self.integration_branch = integration_branch

    async def prepare(
        self, task_id: str, title: str, *, rework_cycle: int = 0
    ) -> Worktree:
        await self._git("rev-parse", "--git-dir", cwd=self.repository)
        await self._ensure_integration_branch()
        suffix = f"-rework-{rework_cycle}" if rework_cycle else ""
        branch = f"{task_id}-{slug(title)}{suffix}"
        path = (self.worktree_root / branch).resolve()
        if path.parent != self.worktree_root:
            raise GitError("invalid worktree path")
        if path.exists():
            current = await self._git("branch", "--show-current", cwd=path)
            if current != branch:
                raise GitError(f"worktree path already belongs to {current}")
            return Worktree(branch, path)

        self.worktree_root.mkdir(parents=True, exist_ok=True)
        branch_exists = (
            await self._git_status(
                "show-ref", "--verify", f"refs/heads/{branch}", cwd=self.repository
            )
            == 0
        )
        args = ("worktree", "add", str(path), branch)
        if not branch_exists:
            args = ("worktree", "add", "-b", branch, str(path), self.integration_branch)
        await self._git(*args, cwd=self.repository)
        return Worktree(branch, path)

    async def _ensure_integration_branch(self) -> None:
        integration_ref = f"refs/heads/{self.integration_branch}"
        if (
            await self._git_status(
                "show-ref", "--verify", integration_ref, cwd=self.repository
            )
            == 0
        ):
            return
        if await self._git_status("rev-parse", "--verify", "HEAD", cwd=self.repository):
            raise GitError(
                f"integration branch '{self.integration_branch}' is missing and "
                "the repository has no commit at HEAD"
            )
        await self._git("branch", self.integration_branch, cwd=self.repository)

    async def checkpoint(self, worktree: Worktree, message: str) -> str:
        self._validate(worktree)
        await self._git("add", "-A", cwd=worktree.path)
        changed = await self._git_status(
            "diff", "--cached", "--quiet", cwd=worktree.path
        )
        if changed == 0:
            return await self._git("rev-parse", "HEAD", cwd=worktree.path)
        await self._git(
            "-c",
            "user.name=CARLO",
            "-c",
            "user.email=carlo@local",
            "commit",
            "-m",
            f"checkpoint: {message}",
            cwd=worktree.path,
        )
        return await self._git("rev-parse", "HEAD", cwd=worktree.path)

    async def diff_hash(self, worktree: Worktree) -> str:
        self._validate(worktree)
        await self._git("add", "-A", cwd=worktree.path)
        diff = await self._git("diff", "--cached", "--binary", cwd=worktree.path)
        return hashlib.sha256(diff.encode()).hexdigest()

    async def restore(self, worktree: Worktree, checkpoint: str) -> None:
        self._validate(worktree)
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", checkpoint):
            raise GitError("invalid checkpoint SHA")
        await self._git("cat-file", "-e", f"{checkpoint}^{{commit}}", cwd=worktree.path)
        await self._git("reset", "--hard", checkpoint, cwd=worktree.path)

    def _validate(self, worktree: Worktree) -> None:
        path = worktree.path.resolve()
        if path.parent != self.worktree_root or not (path / ".git").is_file():
            raise GitError("worktree is outside CARLO's managed root")

    async def _git(
        self, *args: str, cwd: Path
    ) -> str:
        returncode, stdout, stderr = await self._run(*args, cwd=cwd)
        if returncode:
            raise GitError(stderr)
        return stdout

    async def _git_status(self, *args: str, cwd: Path) -> int:
        returncode, _, _ = await self._run(*args, cwd=cwd)
        return returncode

    async def _run(self, *args: str, cwd: Path) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode or 0,
            stdout.decode().strip(),
            stderr.decode(errors="replace").strip(),
        )


def slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")[:60] or "task"

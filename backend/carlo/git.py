import asyncio
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Checkout:
    branch: str
    path: Path


class GitWorkspace:
    def __init__(
        self,
        repository: Path,
        integration_branch: str,
    ) -> None:
        self.repository = repository.resolve()
        self.integration_branch = integration_branch

    async def prepare(
        self,
        task_id: str,
        title: str,
        *,
        base_ref: str | None = None,
    ) -> Checkout:
        if not (self.repository / ".git").is_dir():
            raise GitError("registered project is not a direct Git checkout")
        await self._git("rev-parse", "--git-dir", cwd=self.repository)
        ensure_local_exclude(self.repository)
        await self._ensure_integration_branch()
        branch = parent_branch_name(task_id, title)
        current = await self._git("branch", "--show-current", cwd=self.repository)
        if current == branch:
            await self._require_checkpoint(base_ref)
            return Checkout(branch, self.repository)

        if await self._git("status", "--porcelain", cwd=self.repository):
            raise GitError("project checkout has uncommitted changes")
        branch_exists = (
            await self._git_status(
                "show-ref", "--verify", f"refs/heads/{branch}", cwd=self.repository
            )
            == 0
        )
        if branch_exists:
            await self._git("switch", branch, cwd=self.repository)
        else:
            await self._git(
                "switch",
                "-c",
                branch,
                base_ref or self.integration_branch,
                cwd=self.repository,
            )
        await self._require_checkpoint(base_ref)
        return Checkout(branch, self.repository)

    async def _require_checkpoint(self, base_ref: str | None) -> None:
        if base_ref is None:
            return
        if await self._git_status(
            "merge-base", "--is-ancestor", base_ref, "HEAD", cwd=self.repository
        ):
            raise GitError(
                "parent branch does not contain the previous subtask checkpoint"
            )

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

    async def checkpoint(self, checkout: Checkout, message: str) -> str:
        await self._validate(checkout)
        await self._git("add", "-A", cwd=checkout.path)
        changed = await self._git_status(
            "diff", "--cached", "--quiet", cwd=checkout.path
        )
        if changed == 0:
            return await self._git("rev-parse", "HEAD", cwd=checkout.path)
        await self._git(
            "-c",
            "user.name=CARLO",
            "-c",
            "user.email=carlo@local",
            "commit",
            "-m",
            f"checkpoint: {message}",
            cwd=checkout.path,
        )
        return await self._git("rev-parse", "HEAD", cwd=checkout.path)

    async def promote(self, checkpoint: str) -> str:
        if not (self.repository / ".git").is_dir():
            raise GitError("registered project is not a direct Git checkout")
        await self._git("rev-parse", "--git-dir", cwd=self.repository)
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", checkpoint):
            raise GitError("invalid checkpoint SHA")
        await self._git(
            "cat-file", "-e", f"{checkpoint}^{{commit}}", cwd=self.repository
        )
        await self._ensure_integration_branch()
        current = await self._git(
            "rev-parse",
            f"refs/heads/{self.integration_branch}",
            cwd=self.repository,
        )
        if await self._git_status(
            "merge-base",
            "--is-ancestor",
            current,
            checkpoint,
            cwd=self.repository,
        ):
            raise GitError(
                "integration branch has diverged from the validated checkpoint"
            )
        await self._git(
            "update-ref",
            f"refs/heads/{self.integration_branch}",
            checkpoint,
            current,
            cwd=self.repository,
        )
        return checkpoint

    async def diff_hash(self, checkout: Checkout) -> str:
        await self._validate(checkout)
        await self._git("add", "-A", cwd=checkout.path)
        diff = await self._git("diff", "--cached", "--binary", cwd=checkout.path)
        return hashlib.sha256(diff.encode()).hexdigest()

    async def changed_paths(self, checkout: Checkout) -> set[str]:
        await self._validate(checkout)
        await self._git("add", "-A", cwd=checkout.path)
        output = await self._git(
            "diff", "--cached", "--name-only", "-z", cwd=checkout.path
        )
        return {path for path in output.split("\0") if path}

    async def restore(self, checkout: Checkout, checkpoint: str) -> None:
        await self._validate(checkout)
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", checkpoint):
            raise GitError("invalid checkpoint SHA")
        await self._git("cat-file", "-e", f"{checkpoint}^{{commit}}", cwd=checkout.path)
        await self._git("reset", "--hard", checkpoint, cwd=checkout.path)

    async def archive_and_restore(
        self, checkout: Checkout, checkpoint: str | None, archive: Path,
    ) -> tuple[str, str | None]:
        """Keep unverified edits as a patch, then return to a clean checkpoint."""
        await self._validate(checkout)
        head = await self._git("rev-parse", "HEAD", cwd=checkout.path)
        target = checkpoint or head
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", target):
            raise GitError("invalid checkpoint SHA")
        if await self._git_status("merge-base", "--is-ancestor", target, head, cwd=checkout.path):
            raise GitError("validated checkpoint is not an ancestor of the checkout")
        untracked = (await self._git("ls-files", "--others", "--exclude-standard", "-z", cwd=checkout.path)).split("\0")
        await self._git("add", "-A", cwd=checkout.path)
        diff = await self._git("diff", "--cached", "--binary", target, cwd=checkout.path)
        if diff:
            archive.parent.mkdir(parents=True, exist_ok=True)
            patch = diff + "\n"
            if archive.exists() and archive.read_text() != patch:
                raise GitError("technical replan archive already contains different changes")
            archive.write_text(patch)
        await self.restore(checkout, target)
        remaining_untracked = set((await self._git(
            "ls-files", "--others", "--exclude-standard", "-z", cwd=checkout.path
        )).split("\0"))
        for name in filter(None, remaining_untracked.intersection(untracked)):
            path = checkout.path / name
            if not path.parent.resolve().is_relative_to(checkout.path.resolve()):
                raise GitError("untracked path escaped the checkout")
            if path.is_file() or path.is_symlink():
                path.unlink()
        if await self._git("status", "--porcelain", cwd=checkout.path):
            raise GitError("checkout is not clean after technical replan restore")
        return head, str(archive) if archive.exists() else None

    async def _validate(self, checkout: Checkout) -> None:
        if checkout.path.resolve() != self.repository or not (
            self.repository / ".git"
        ).is_dir():
            raise GitError("checkout is not the registered project")
        current = await self._git("branch", "--show-current", cwd=self.repository)
        if current != checkout.branch:
            raise GitError("registered project is on the wrong branch")

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


def parent_branch_name(task_id: str, title: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    )
    description = re.sub(r"[^a-z0-9]+", "", ascii_value.lower())[:60] or "task"
    return f"{task_id}_{description}"[:240]


def ensure_local_exclude(repository: Path) -> None:
    exclude = repository / ".git" / "info" / "exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if ".carlo/" not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("\n".join([*lines, ".carlo/"]) + "\n")


def slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")[:60] or "task"

import subprocess
from pathlib import Path

import pytest

from carlo.git import GitError, GitWorkspace


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.asyncio
async def test_worktree_checkpoint_restores_known_good_content(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.txt").write_text("base\n")
    git(repository, "add", "app.txt")
    git(repository, "commit", "-m", "base")
    git(repository, "branch", "carlo-Dev")

    workspace = GitWorkspace(repository, tmp_path / "worktrees", "carlo-Dev")
    worktree = await workspace.prepare("CAR-1", "Login Flow")

    assert worktree.branch == "CAR-1-login-flow"
    assert git(worktree.path, "merge-base", "--is-ancestor", "carlo-Dev", "HEAD") == ""
    (worktree.path / "app.txt").write_text("known good\n")
    checkpoint = await workspace.checkpoint(worktree, "validation improved")
    (worktree.path / "app.txt").write_text("worse\n")

    await workspace.restore(worktree, checkpoint)

    assert (worktree.path / "app.txt").read_text() == "known good\n"
    assert git(worktree.path, "rev-parse", "HEAD") == checkpoint


@pytest.mark.asyncio
async def test_prepare_creates_missing_integration_branch_from_head(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.txt").write_text("base\n")
    git(repository, "add", "app.txt")
    git(repository, "commit", "-m", "base")

    workspace = GitWorkspace(repository, tmp_path / "worktrees", "carlo-Dev")
    worktree = await workspace.prepare("CAR-2", "Create branch")

    assert git(repository, "rev-parse", "carlo-Dev") == git(repository, "rev-parse", "HEAD")
    assert git(worktree.path, "merge-base", "--is-ancestor", "carlo-Dev", "HEAD") == ""


@pytest.mark.asyncio
async def test_rework_prepares_clean_numbered_worktree_from_integration(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "main")
    git(repository, "config", "user.name", "Test")
    git(repository, "config", "user.email", "test@example.com")
    (repository / "app.txt").write_text("base\n")
    git(repository, "add", "app.txt")
    git(repository, "commit", "-m", "base")
    git(repository, "branch", "carlo-Dev")
    workspace = GitWorkspace(repository, tmp_path / "worktrees", "carlo-Dev")
    failed = await workspace.prepare("CAR-4", "Login Flow")
    (failed.path / "failed.txt").write_text("failed attempt\n")
    await workspace.checkpoint(failed, "failed attempt")

    rework = await workspace.prepare("CAR-4", "Login Flow", rework_cycle=1)

    assert rework.branch == "CAR-4-login-flow-rework-1"
    assert rework.path != failed.path
    assert not (rework.path / "failed.txt").exists()
    assert git(rework.path, "rev-parse", "HEAD") == git(repository, "rev-parse", "carlo-Dev")


@pytest.mark.asyncio
async def test_prepare_reports_repository_without_commits(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "other")
    workspace = GitWorkspace(repository, tmp_path / "worktrees", "carlo-Dev")

    with pytest.raises(GitError, match="repository has no commit at HEAD"):
        await workspace.prepare("CAR-3", "Missing branch")

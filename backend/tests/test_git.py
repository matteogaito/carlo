import subprocess
from pathlib import Path

import pytest

from carlo.git import GitError, GitWorkspace, parent_branch_name


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def repository_at(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Test")
    git(path, "config", "user.email", "test@example.com")
    (path / "app.txt").write_text("base\n")
    git(path, "add", "app.txt")
    git(path, "commit", "-m", "base")
    git(path, "branch", "carlo-Dev")
    return path


@pytest.mark.asyncio
async def test_direct_checkout_reuses_parent_branch_and_checkpoint(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")

    checkout = await workspace.prepare("CAR-1", "Login Flow")
    (repository / "app.txt").write_text("first child\n")
    checkpoint = await workspace.checkpoint(checkout, "first child")
    second = await workspace.prepare("CAR-1", "Login Flow", base_ref=checkpoint)

    assert parent_branch_name("PHOTODIGGER-1", "Implement skeleton") == (
        "PHOTODIGGER-1_implementskeleton"
    )
    assert checkout.path == repository.resolve()
    assert second == checkout
    assert checkout.branch == "CAR-1_loginflow"
    assert git(repository, "branch", "--show-current") == "CAR-1_loginflow"
    assert git(repository, "merge-base", "--is-ancestor", checkpoint, "HEAD") == ""
    assert ".carlo/" in (
        repository / ".git" / "info" / "exclude"
    ).read_text().splitlines()


@pytest.mark.asyncio
async def test_checkout_checkpoint_restores_known_good_content(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Login Flow")
    (repository / "app.txt").write_text("known good\n")
    checkpoint = await workspace.checkpoint(checkout, "validation improved")
    (repository / "app.txt").write_text("worse\n")

    await workspace.restore(checkout, checkpoint)

    assert (repository / "app.txt").read_text() == "known good\n"
    assert git(repository, "rev-parse", "HEAD") == checkpoint


@pytest.mark.asyncio
async def test_prepare_rejects_dirty_checkout_without_switching(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    (repository / "user-note.txt").write_text("mine\n")

    with pytest.raises(GitError, match="uncommitted changes"):
        await GitWorkspace(repository, "carlo-Dev").prepare("CAR-2", "Safe branch")

    assert git(repository, "branch", "--show-current") == "main"
    assert (repository / "user-note.txt").read_text() == "mine\n"


@pytest.mark.asyncio
async def test_prepare_rejects_parent_branch_without_previous_checkpoint(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path / "repo")
    git(repository, "branch", "CAR-3_parent", "HEAD")
    git(repository, "switch", "carlo-Dev")
    (repository / "previous.txt").write_text("previous child\n")
    git(repository, "add", "previous.txt")
    git(repository, "commit", "-m", "previous child")
    previous = git(repository, "rev-parse", "HEAD")

    with pytest.raises(
        GitError,
        match="parent branch does not contain the previous subtask checkpoint",
    ):
        await GitWorkspace(repository, "carlo-Dev").prepare(
            "CAR-3", "Parent", base_ref=previous
        )

    assert git(repository, "branch", "--show-current") == "CAR-3_parent"
    assert git(repository, "rev-parse", "HEAD") != previous


@pytest.mark.asyncio
async def test_prepare_creates_missing_integration_branch_from_head(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    git(repository, "branch", "-D", "carlo-Dev")

    checkout = await GitWorkspace(repository, "carlo-Dev").prepare(
        "CAR-4", "Create branch"
    )

    assert checkout.path == repository.resolve()
    assert git(repository, "merge-base", "--is-ancestor", "carlo-Dev", "HEAD") == ""


@pytest.mark.asyncio
async def test_prepare_reports_repository_without_commits(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-b", "other")

    with pytest.raises(GitError, match="repository has no commit at HEAD"):
        await GitWorkspace(repository, "carlo-Dev").prepare(
            "CAR-5", "Missing branch"
        )


@pytest.mark.asyncio
async def test_promote_fast_forwards_integration_without_switching(
    tmp_path: Path,
) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    (repository / "feature.txt").write_text("done\n")
    checkpoint = await workspace.checkpoint(checkout, "validated")

    promoted = await workspace.promote(checkpoint)

    assert promoted == checkpoint
    assert git(repository, "rev-parse", "carlo-Dev") == checkpoint
    assert git(repository, "branch", "--show-current") == "CAR-1_feature"
    assert git(repository, "rev-list", "--merges", "carlo-Dev") == ""


@pytest.mark.asyncio
async def test_promote_is_idempotent(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    checkpoint = await workspace.checkpoint(checkout, "validated")

    assert await workspace.promote(checkpoint) == checkpoint
    assert await workspace.promote(checkpoint) == checkpoint


@pytest.mark.asyncio
async def test_promote_refuses_diverged_integration_branch(tmp_path: Path) -> None:
    repository = repository_at(tmp_path / "repo")
    workspace = GitWorkspace(repository, "carlo-Dev")
    checkout = await workspace.prepare("CAR-1", "Feature")
    (repository / "feature.txt").write_text("done\n")
    checkpoint = await workspace.checkpoint(checkout, "validated")
    git(repository, "switch", "carlo-Dev")
    (repository / "other.txt").write_text("other\n")
    git(repository, "add", "other.txt")
    git(repository, "commit", "-m", "diverged")
    diverged = git(repository, "rev-parse", "HEAD")
    git(repository, "switch", "CAR-1_feature")

    with pytest.raises(
        GitError,
        match="integration branch has diverged from the validated checkpoint",
    ):
        await workspace.promote(checkpoint)

    assert git(repository, "rev-parse", "carlo-Dev") == diverged
    assert git(repository, "branch", "--show-current") == "CAR-1_feature"

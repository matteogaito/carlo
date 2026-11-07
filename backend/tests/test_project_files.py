import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from carlo.project_files import list_files, read_file


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("build/\nignored.txt\n")
    (tmp_path / "README.md").write_text("old")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    (tmp_path / "README.md").write_text("# Current working copy")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def hello():\n    pass\n")
    (tmp_path / ".secret").write_text("hidden")
    (tmp_path / ".private").mkdir()
    (tmp_path / ".private" / "secret.txt").write_text("hidden")
    (tmp_path / "ignored.txt").write_text("ignored")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "output.txt").write_text("ignored")
    (tmp_path / "linked.txt").symlink_to(tmp_path / ".secret")
    return tmp_path


def test_inventory_and_reads_use_working_copy(repository):
    assert list_files(repository) == ["README.md", "src/app.py"]
    assert read_file(repository, "README.md") == {"path": "README.md", "content": "# Current working copy"}
    (repository / "README.md").unlink()
    assert list_files(repository) == ["src/app.py"]


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", ".secret", ".private/secret.txt", "ignored.txt", "build/output.txt", "linked.txt", "missing.txt", "src"])
def test_unavailable_paths_cannot_be_read(repository, path):
    with pytest.raises(HTTPException) as error:
        read_file(repository, path)
    assert error.value.status_code == 404


@pytest.mark.parametrize("content", [b"a\x00b", b"\xff\xfe", b"x" * (2 * 1024 * 1024 + 1)], ids=["binary", "invalid-utf8", "oversized"])
def test_binary_and_oversized_files_are_not_served(repository, content):
    (repository / "unsupported").write_bytes(content)
    with pytest.raises(HTTPException) as error:
        read_file(repository, "unsupported")
    assert error.value.status_code == 415


def test_tracked_but_ignored_files_are_excluded(repository):
    subprocess.run(["git", "-C", str(repository), "add", "-f", "ignored.txt"], check=True)
    assert "ignored.txt" not in list_files(repository)
    with pytest.raises(HTTPException):
        read_file(repository, "ignored.txt")


def test_symlinked_directories_and_special_files_are_excluded(repository, tmp_path):
    import os
    external = tmp_path / "external"
    external.mkdir()
    (external / "secret.txt").write_text("secret")
    (repository / "linkdir").symlink_to(external, target_is_directory=True)
    os.mkfifo(repository / "pipe")
    assert "pipe" not in list_files(repository)
    with pytest.raises(HTTPException):
        read_file(repository, "linkdir/secret.txt")


@pytest.mark.asyncio
async def test_file_routes_require_authentication_and_return_current_content(repository, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from httpx import ASGITransport, AsyncClient
    from carlo.api import create_app
    from carlo.config import Settings
    from tests.fakes import FakeProvider

    class Session:
        async def get(self, model, id):
            return SimpleNamespace(repository_path=str(repository)) if id == 1 else None

    @asynccontextmanager
    async def factory():
        yield Session()

    async def resolve_session(factory, cookie):
        return SimpleNamespace(role="admin") if cookie == "test" else None

    monkeypatch.setattr("carlo.api.resolve_session", resolve_session)
    app = create_app(factory, FakeProvider(""), Settings(app_origin="http://test"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/projects/1/files")).status_code == 401
        assert (await client.get("/api/projects/1/file", params={"path": "README.md"})).status_code == 401
        client.cookies.set("carlo_session", "test")
        response = await client.get("/api/projects/1/files")
        assert response.status_code == 200
        assert response.json() == ["README.md", "src/app.py"]
        response = await client.get("/api/projects/1/file", params={"path": "README.md"})
        assert response.json()["content"] == "# Current working copy"
        assert (await client.get("/api/projects/2/files")).status_code == 404
        assert (await client.get("/api/projects/1/file", params={"path": ".secret"})).status_code == 404

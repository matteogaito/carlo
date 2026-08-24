import base64
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo.admin import bootstrap_admin
from carlo.api import create_app
from carlo.config import Settings
from carlo.model_providers import CredentialCipher
from carlo.models import AgentProfile, AvailableModel, Base, ModelProvider, Project, Task
from tests.fakes import FakeProvider


@pytest.fixture
async def settings_app() -> AsyncIterator[tuple[object, async_sessionmaker[AsyncSession], str]]:
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE notification_deliveries, notification_cursors, login_failures, "
                "user_sessions, project_memberships, users, events, validation_runs, "
                "escalations, attempts, plan_revisions, tasks, projects, agent_profiles, "
                "available_models, model_providers, pi_runtime_settings "
                "RESTART IDENTITY CASCADE"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO pi_runtime_settings "
                "(id, compaction_enabled, reserve_percent, keep_recent_percent) "
                "VALUES (1, true, 10, 20)"
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await bootstrap_admin(factory, "admin", "admin-password")
    key = base64.b64encode(b"k" * 32).decode()
    app = create_app(
        factory,
        FakeProvider("{}"),
        Settings(app_origin="http://test", credential_encryption_key=key),
    )
    yield app, factory, key
    await engine.dispose()


@pytest.mark.asyncio
async def test_model_provider_api_encrypts_and_never_returns_credentials(
    settings_app,
) -> None:
    app, factory, key = settings_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        response = await client.post(
            "/api/settings/model-providers",
            json={
                "name": "Local OMLX",
                "slug": "omlx",
                "kind": "openai-compatible",
                "base_url": "http://127.0.0.1:11435/v1",
                "api_key": "omlx-local",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["credential_configured"] is True
        assert body["credential_hint"] == "…ocal"
        assert "api_key" not in body
        assert "omlx-local" not in response.text

        listed = await client.get("/api/settings/model-providers")
        assert listed.status_code == 200
        assert listed.json() == [body]
        assert "omlx-local" not in listed.text

    async with factory() as session:
        provider = await session.scalar(select(ModelProvider))
        assert provider is not None
        assert provider.credential_ciphertext != b"omlx-local"
        assert CredentialCipher.from_base64(key).decrypt(
            provider.credential_ciphertext, provider.credential_nonce
        ) == "omlx-local"


@pytest.mark.asyncio
async def test_model_provider_api_accepts_keyless_local_endpoint(settings_app) -> None:
    app, factory, _ = settings_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        response = await client.post(
            "/api/settings/model-providers",
            headers={"Origin": "http://test"},
            json={
                "name": "Keyless OMLX",
                "slug": "keyless-omlx",
                "base_url": "http://127.0.0.1:11435/v1",
            },
        )

    assert response.status_code == 201
    assert response.json()["credential_configured"] is False
    async with factory() as session:
        provider = await session.scalar(
            select(ModelProvider).where(ModelProvider.slug == "keyless-omlx")
        )
        assert provider is not None
        assert provider.credential_ciphertext is None
        assert provider.credential_nonce is None


@pytest.mark.asyncio
async def test_model_and_pi_settings_api_exposes_effective_context_preview(
    settings_app,
) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        provider = ModelProvider(
            name="Local",
            slug="local",
            kind="openai-compatible",
            base_url="http://local.test/v1",
        )
        model = AvailableModel(
            model_provider=provider,
            external_id="qwen",
            status="AVAILABLE",
            discovered_context_window=65_536,
            discovered_max_tokens=16_384,
        )
        session.add_all([provider, model])
        await session.commit()
        model_id = model.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"

        listed = await client.get("/api/settings/models")
        assert listed.status_code == 200
        assert listed.json()[0]["effective_context_window"] == 65_536
        assert listed.json()[0]["reserve_tokens"] == 16_384
        assert listed.json()[0]["keep_recent_tokens"] == 13_107

        updated = await client.patch(
            f"/api/settings/models/{model_id}",
            json={"context_window_override": 131_072, "max_tokens_override": 32_768},
        )
        assert updated.status_code == 200
        assert updated.json()["effective_context_window"] == 131_072
        assert updated.json()["reserve_tokens"] == 32_768
        assert updated.json()["keep_recent_tokens"] == 26_214

        pi_settings = await client.patch(
            "/api/settings/pi",
            json={"reserve_percent": 15, "keep_recent_percent": 25},
        )
        assert pi_settings.status_code == 200
        assert pi_settings.json() == {
            "compaction_enabled": True,
            "reserve_percent": 15,
            "keep_recent_percent": 25,
        }


@pytest.mark.asyncio
async def test_available_model_without_reported_limits_uses_safe_defaults(
    settings_app,
) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        provider = ModelProvider(
            name="Compatible",
            slug="compatible",
            kind="openai-compatible",
            base_url="http://compatible.test/v1",
        )
        model = AvailableModel(
            model_provider=provider,
            external_id="unknown-limits",
            status="AVAILABLE",
        )
        profile = AgentProfile(name="brief", provider="pi")
        session.add_all([provider, model, profile])
        await session.commit()
        model_id = model.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        listed = await client.get("/api/settings/models")
        selected = await client.patch(
            "/api/agent-profiles/brief",
            json={"available_model_id": model_id},
        )

    entry = next(item for item in listed.json() if item["id"] == model_id)
    assert entry["effective_context_window"] == 65_536
    assert entry["effective_max_tokens"] == 16_384
    assert entry["selectable"] is True
    assert selected.status_code == 200


@pytest.mark.asyncio
async def test_invalid_reported_limits_do_not_break_the_model_catalog(
    settings_app,
) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        provider = ModelProvider(
            name="Broken metadata",
            slug="broken-metadata",
            kind="openai-compatible",
            base_url="http://broken.test/v1",
        )
        session.add(
            AvailableModel(
                model_provider=provider,
                external_id="one-token-context",
                status="AVAILABLE",
                discovered_context_window=1,
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        response = await client.get("/api/settings/models")

    assert response.status_code == 200
    assert response.json()[0]["selectable"] is False


@pytest.mark.asyncio
async def test_compaction_incompatible_model_cannot_be_selected(
    settings_app,
) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        provider = ModelProvider(
            name="Tight context",
            slug="tight-context",
            kind="openai-compatible",
            base_url="http://tight.test/v1",
        )
        model = AvailableModel(
            model_provider=provider,
            external_id="tight-model",
            status="AVAILABLE",
            discovered_context_window=100,
            discovered_max_tokens=90,
        )
        session.add_all([provider, model, AgentProfile(name="brief", provider="pi")])
        await session.commit()
        model_id = model.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        response = await client.patch(
            "/api/agent-profiles/brief",
            headers={"Origin": "http://test"},
            json={"available_model_id": model_id},
        )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_agent_profiles_select_known_skills_and_keep_core_skills(
    settings_app,
) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        session.add(AgentProfile(name="plan", provider="pi", default_skills=[]))
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        catalog = await client.get("/api/settings/skills")
        updated = await client.patch(
            "/api/agent-profiles/plan",
            json={"default_skills": ["carlo-ui-design"]},
        )
        unknown = await client.patch(
            "/api/agent-profiles/plan",
            json={"default_skills": ["not-installed"]},
        )
        null_skills = await client.patch(
            "/api/agent-profiles/plan",
            json={"default_skills": None},
        )

    skills = {item["name"]: item for item in catalog.json()}
    assert skills["carlo-ui-design"]["source"] == "carlo"
    assert skills["frontend-design"]["source"] == "managed"
    assert skills["carlo-planning"]["required_profiles"] == ["brief", "plan"]
    assert updated.status_code == 200
    assert updated.json()["default_skills"] == [
        "carlo-planning",
        "carlo-ui-design",
    ]
    assert unknown.status_code == 422
    assert null_skills.status_code == 422


@pytest.mark.asyncio
async def test_provider_update_retains_or_replaces_secret(
    settings_app,
) -> None:
    app, factory, key = settings_app
    cipher = CredentialCipher.from_base64(key)
    original = cipher.encrypt("original-secret")
    async with factory() as session:
        provider = ModelProvider(
            name="Local",
            slug="local",
            kind="openai-compatible",
            base_url="http://local.test/v1",
            credential_ciphertext=original.ciphertext,
            credential_nonce=original.nonce,
            credential_hint="…cret",
        )
        model = AvailableModel(
            model_provider=provider,
            external_id="qwen",
            status="AVAILABLE",
            discovered_context_window=65_536,
            discovered_max_tokens=16_384,
        )
        session.add_all([provider, model])
        await session.commit()
        provider_id, model_id = provider.id, model.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        retained = await client.patch(
            f"/api/settings/model-providers/{provider_id}",
            json={"name": "Renamed"},
        )
        assert retained.status_code == 200
        assert "default_model_id" not in retained.json()
        rejected = await client.patch(
            f"/api/settings/model-providers/{provider_id}",
            json={"api_key": None},
        )
        assert rejected.status_code == 422
        replaced = await client.patch(
            f"/api/settings/model-providers/{provider_id}",
            json={"api_key": "replacement"},
        )
        assert replaced.status_code == 200
        assert replaced.json()["credential_hint"] == "…ment"

    async with factory() as session:
        provider = await session.get(ModelProvider, provider_id)
        assert provider is not None
        assert provider.name == "Renamed"
        assert cipher.decrypt(
            provider.credential_ciphertext, provider.credential_nonce
        ) == "replacement"


@pytest.mark.asyncio
async def test_profiles_and_tasks_pin_concrete_models(
    settings_app,
) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        provider = ModelProvider(
            name="Local",
            slug="local",
            kind="openai-compatible",
            base_url="http://local.test/v1",
        )
        model = AvailableModel(
            model_provider=provider,
            external_id="qwen",
            status="AVAILABLE",
            discovered_context_window=65_536,
            discovered_max_tokens=16_384,
        )
        profile = AgentProfile(name="implementation", provider="pi")
        project = Project(name="Project", key="MOD", repository_path="/tmp/mod")
        task = Task(
            id="MOD-1",
            project=project,
            sequence=1,
            title="Model override",
            goal="Use OpenRouter",
        )
        session.add_all([provider, model, profile, task])
        await session.flush()
        await session.commit()
        model_id = model.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        profile_response = await client.patch(
            "/api/agent-profiles/implementation",
            json={"available_model_id": model_id},
        )
        assert profile_response.status_code == 200
        assert "model_provider_id" not in profile_response.json()
        assert profile_response.json()["available_model_id"] == model_id

        task_response = await client.patch(
            "/api/tasks/MOD-1/model",
            json={"available_model_id": model_id},
        )
        assert task_response.status_code == 200
        assert task_response.json()["available_model_id"] == model_id


@pytest.mark.asyncio
async def test_model_provider_refresh_action_is_explicit_and_auditable(
    settings_app, monkeypatch
) -> None:
    app, factory, key = settings_app
    cipher = CredentialCipher.from_base64(key)
    encrypted = cipher.encrypt("secret")
    async with factory() as session:
        provider = ModelProvider(
            name="Local",
            slug="local",
            kind="openai-compatible",
            base_url="http://local.test/v1",
            credential_ciphertext=encrypted.ciphertext,
            credential_nonce=encrypted.nonce,
        )
        session.add(provider)
        await session.commit()
        provider_id = provider.id

    called: list[int] = []

    async def refresh(_factory, _cipher, selected_provider_id):
        called.append(selected_provider_id)
        from carlo.model_providers import ModelRefreshResult

        return ModelRefreshResult(selected_provider_id, 2, 1)

    monkeypatch.setattr("carlo.api.refresh_model_provider", refresh, raising=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        response = await client.post(
            f"/api/settings/model-providers/{provider_id}/refresh",
            headers={"Origin": "http://test"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider_id": provider_id,
        "seen": 2,
        "unavailable": 1,
    }
    assert called == [provider_id]


@pytest.mark.asyncio
async def test_model_provider_delete_refuses_profile_references(settings_app) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        free = ModelProvider(
            name="Free",
            slug="free",
            kind="openai-compatible",
            base_url="http://free.test/v1",
        )
        used = ModelProvider(
            name="Used",
            slug="used",
            kind="openai-compatible",
            base_url="http://used.test/v1",
        )
        model = AvailableModel(
            model_provider=used,
            external_id="used-model",
            status="AVAILABLE",
            discovered_context_window=65_536,
            discovered_max_tokens=16_384,
        )
        profile = AgentProfile(name="implementation", provider="pi")
        session.add_all([free, used, model, profile])
        await session.flush()
        profile.available_model_id = model.id
        await session.commit()
        free_id, used_id = free.id, used.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        client.headers["Origin"] = "http://test"
        assert (
            await client.delete(f"/api/settings/model-providers/{used_id}")
        ).status_code == 409
        assert (
            await client.delete(f"/api/settings/model-providers/{free_id}")
        ).status_code == 204

    async with factory() as session:
        assert await session.get(ModelProvider, free_id) is None
        assert await session.get(ModelProvider, used_id) is not None


@pytest.mark.asyncio
async def test_pi_settings_reject_policy_that_does_not_fit_available_models(
    settings_app,
) -> None:
    app, factory, _ = settings_app
    async with factory() as session:
        provider = ModelProvider(
            name="Small",
            slug="small",
            kind="openai-compatible",
            base_url="http://small.test/v1",
        )
        session.add(
            AvailableModel(
                model_provider=provider,
                external_id="small",
                status="AVAILABLE",
                discovered_context_window=100,
                discovered_max_tokens=10,
            )
        )
        await session.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "admin-password"},
        )
        response = await client.patch(
            "/api/settings/pi",
            headers={"Origin": "http://test"},
            json={"reserve_percent": 90, "keep_recent_percent": 20},
        )
        assert response.status_code == 422

        current = await client.get("/api/settings/pi")
        assert current.json()["reserve_percent"] == 10

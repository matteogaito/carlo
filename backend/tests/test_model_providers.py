import importlib
import importlib.util
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from carlo import models


def test_credential_cipher_round_trip_uses_random_nonces() -> None:
    assert importlib.util.find_spec("carlo.model_providers") is not None, (
        "model provider security is missing"
    )
    module = importlib.import_module("carlo.model_providers")
    cipher = module.CredentialCipher(b"x" * 32)

    first = cipher.encrypt("sk-secret")
    second = cipher.encrypt("sk-secret")

    assert first != second
    assert cipher.decrypt(first.ciphertext, first.nonce) == "sk-secret"
    assert b"sk-secret" not in first.ciphertext


def test_model_provider_records_keep_discovered_and_overridden_limits() -> None:
    assert hasattr(models, "ModelProvider"), "model provider persistence is missing"
    assert hasattr(models, "AvailableModel"), "model catalog persistence is missing"
    provider = models.ModelProvider(
        name="Local OMLX",
        slug="omlx",
        kind="openai-compatible",
        base_url="http://127.0.0.1:11435/v1",
    )
    model = models.AvailableModel(
        model_provider=provider,
        external_id="qwen",
        discovered_context_window=65_536,
        discovered_max_tokens=16_384,
        context_window_override=131_072,
    )

    assert model.effective_context_window == 131_072
    assert model.effective_max_tokens == 16_384
    assert provider.models == [model]


def test_missing_output_limit_is_capped_to_the_discovered_context() -> None:
    model = models.AvailableModel(
        external_id="small-model",
        discovered_context_window=8_192,
    )

    assert model.effective_max_tokens == 2_048


def test_missing_context_limit_expands_for_the_discovered_output() -> None:
    model = models.AvailableModel(
        external_id="large-output-model",
        discovered_max_tokens=32_768,
    )

    assert model.effective_context_window == 131_072


def test_openai_catalog_parser_reads_all_models_and_common_context_fields() -> None:
    module = importlib.import_module("carlo.model_providers")
    assert hasattr(module, "parse_openai_models"), "catalog discovery is missing"

    entries = module.parse_openai_models(
        {
            "object": "list",
            "data": [
                {"id": "qwen", "max_model_len": 65_536},
                {"id": "router-model", "context_length": 131_072, "max_tokens": 8_192},
                {"id": "unknown-limits"},
            ],
        }
    )

    assert [entry.external_id for entry in entries] == [
        "qwen",
        "router-model",
        "unknown-limits",
    ]
    assert entries[0].context_window == 65_536
    assert entries[1].context_window == 131_072
    assert entries[1].max_tokens == 8_192
    assert entries[2].context_window is None


@pytest.fixture
async def model_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("postgresql+psycopg:///carlov3_test")
    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)
        await connection.execute(
            text(
                "TRUNCATE model_providers, available_models, pi_runtime_settings "
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
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_refresh_retains_missing_models_without_selecting_one(
    model_factory,
) -> None:
    module = importlib.import_module("carlo.model_providers")
    assert hasattr(module, "refresh_model_provider"), "catalog refresh is missing"
    cipher = module.CredentialCipher(b"x" * 32)
    encrypted = cipher.encrypt("omlx-local")
    async with model_factory() as session:
        provider = models.ModelProvider(
            name="Local OMLX",
            slug="omlx",
            kind="openai-compatible",
            base_url="http://127.0.0.1:11435/v1",
            credential_ciphertext=encrypted.ciphertext,
            credential_nonce=encrypted.nonce,
        )
        session.add(provider)
        await session.commit()
        provider_id = provider.id

    catalogs = iter(
        [
            [{"id": "old", "max_model_len": 65_536}],
            [{"id": "new", "max_model_len": 131_072}],
        ]
    )

    async def fetcher(_provider, api_key):
        assert api_key == "omlx-local"
        return module.parse_openai_models({"data": next(catalogs)})

    await module.refresh_model_provider(model_factory, cipher, provider_id, fetcher=fetcher)
    await module.refresh_model_provider(model_factory, cipher, provider_id, fetcher=fetcher)

    async with model_factory() as session:
        provider = await session.get(models.ModelProvider, provider_id)
        catalog = (
            await session.scalars(
                select(models.AvailableModel).order_by(models.AvailableModel.external_id)
            )
        ).all()
        assert [(model.external_id, model.status) for model in catalog] == [
            ("new", "AVAILABLE"),
            ("old", "UNAVAILABLE"),
        ]
        assert provider is not None
        assert provider.last_refresh_status == "SUCCESS"
        changes = (
            await session.scalars(
                select(models.Event)
                .where(models.Event.type == "model_provider.availability_changed")
                .order_by(models.Event.sequence)
            )
        ).all()
        assert changes[-1].payload == {
            "model_provider_id": provider_id,
            "available": ["new"],
            "unavailable": ["old"],
        }


@pytest.mark.asyncio
async def test_refresh_keyless_provider_does_not_require_a_cipher(model_factory) -> None:
    module = importlib.import_module("carlo.model_providers")
    async with model_factory() as session:
        provider = models.ModelProvider(
            name="Keyless Local",
            slug="keyless",
            kind="openai-compatible",
            base_url="http://127.0.0.1:11435/v1",
        )
        session.add(provider)
        await session.commit()
        provider_id = provider.id

    async def fetcher(_provider, api_key):
        assert api_key is None
        return module.parse_openai_models(
            {"data": [{"id": "local", "max_model_len": 65_536, "max_tokens": 8_192}]}
        )

    result = await module.refresh_model_provider(
        model_factory, None, provider_id, fetcher=fetcher
    )

    assert result.seen == 1


@pytest.mark.asyncio
async def test_periodic_refresh_only_runs_due_active_providers(model_factory) -> None:
    module = importlib.import_module("carlo.model_providers")
    assert hasattr(module, "refresh_due_model_providers"), (
        "periodic catalog refresh is missing"
    )
    cipher = module.CredentialCipher(b"x" * 32)
    encrypted = cipher.encrypt("key")
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    async with model_factory() as session:
        session.add_all(
            [
                models.ModelProvider(
                    name="Due",
                    slug="due",
                    kind="openai-compatible",
                    base_url="http://due.test/v1",
                    credential_ciphertext=encrypted.ciphertext,
                    credential_nonce=encrypted.nonce,
                    last_refresh_attempt_at=now - timedelta(minutes=15),
                ),
                models.ModelProvider(
                    name="Recent",
                    slug="recent",
                    kind="openai-compatible",
                    base_url="http://recent.test/v1",
                    credential_ciphertext=encrypted.ciphertext,
                    credential_nonce=encrypted.nonce,
                    last_refresh_attempt_at=now - timedelta(minutes=14),
                ),
            ]
        )
        await session.commit()

    called: list[str] = []

    async def fetcher(provider, _api_key):
        called.append(provider.slug)
        return []

    count = await module.refresh_due_model_providers(
        model_factory, cipher, now=now, fetcher=fetcher
    )

    assert count == 1
    assert called == ["due"]


@pytest.mark.asyncio
async def test_refresh_records_wrong_encryption_key_without_losing_catalog(
    model_factory,
) -> None:
    module = importlib.import_module("carlo.model_providers")
    encrypted = module.CredentialCipher(b"x" * 32).encrypt("secret")
    async with model_factory() as session:
        provider = models.ModelProvider(
            name="Protected",
            slug="protected",
            kind="openai-compatible",
            base_url="http://protected.test/v1",
            credential_ciphertext=encrypted.ciphertext,
            credential_nonce=encrypted.nonce,
        )
        session.add(provider)
        await session.flush()
        session.add(
            models.AvailableModel(
                model_provider_id=provider.id,
                external_id="existing",
                status="AVAILABLE",
            )
        )
        await session.commit()
        provider_id = provider.id

    with pytest.raises(module.ModelProviderError, match="could not be decrypted"):
        await module.refresh_model_provider(
            model_factory,
            module.CredentialCipher(b"y" * 32),
            provider_id,
        )

    async with model_factory() as session:
        provider = await session.get(models.ModelProvider, provider_id)
        catalog = (await session.scalars(select(models.AvailableModel))).all()
        assert provider is not None
        assert provider.last_refresh_status == "FAILED"
        assert provider.last_refresh_error == "model provider credential could not be decrypted"
        assert [(item.external_id, item.status) for item in catalog] == [
            ("existing", "AVAILABLE")
        ]


@pytest.mark.asyncio
async def test_agent_profile_resolution_uses_concrete_model_and_task_override(
    model_factory,
) -> None:
    module = importlib.import_module("carlo.model_providers")
    cipher = module.CredentialCipher(b"x" * 32)
    encrypted = cipher.encrypt("runtime-secret")
    async with model_factory() as session:
        provider = models.ModelProvider(
            name="Local",
            slug="omlx",
            kind="openai-compatible",
            base_url="http://local.test/v1",
            credential_ciphertext=encrypted.ciphertext,
            credential_nonce=encrypted.nonce,
            compatibility={"supportsDeveloperRole": False},
        )
        default = models.AvailableModel(
            model_provider=provider,
            external_id="qwen",
            status="AVAILABLE",
            discovered_context_window=65_536,
            discovered_max_tokens=16_384,
        )
        override = models.AvailableModel(
            model_provider=provider,
            external_id="qwen-large",
            status="AVAILABLE",
            discovered_context_window=131_072,
            discovered_max_tokens=32_768,
        )
        profile = models.AgentProfile(
            name="implementation-resolver",
            provider="pi",
            permissions={"tools": ["read", "edit"]},
            default_skills=["carlo-ui-design"],
        )
        plan_profile = models.AgentProfile(
            name="plan",
            provider="pi",
            default_packages=["ponytail"],
            default_skills=["carlo-ui-design"],
        )
        runtime = await session.get(models.PiRuntimeSettings, 1)
        if runtime is None:
            runtime = models.PiRuntimeSettings(id=1)
            session.add(runtime)
        runtime.default_packages = ["superpowers", "ponytail"]
        runtime.default_skills = ["frontend-design"]
        session.add_all([provider, default, override, profile, plan_profile])
        await session.flush()
        profile.available_model_id = default.id
        plan_profile.available_model_id = default.id
        await session.flush()

        resolved = await module.resolve_agent_profile(session, profile, cipher)
        task_resolved = await module.resolve_agent_profile(
            session, profile, cipher, task_model_id=override.id
        )
        plan_resolved = await module.resolve_agent_profile(session, plan_profile, cipher)
        historical = await module.resolve_agent_profile(
            session, plan_profile, cipher, extra_skills=("ponytail",)
        )

        assert resolved.resolved_model.external_id == "qwen"
        assert resolved.resolved_model.api_key == "runtime-secret"
        assert resolved.resolved_model.reserve_tokens == 16_384
        assert task_resolved.resolved_model.external_id == "qwen-large"
        assert task_resolved.resolved_model.keep_recent_tokens == 26_214
        assert task_resolved.tools == ("read", "edit")
        assert plan_resolved.skills == (
            "carlo-planning",
            "frontend-design",
            "carlo-ui-design",
        )
        assert plan_resolved.packages == ("superpowers", "ponytail")
        assert historical.packages == ("superpowers", "ponytail")
        assert "ponytail" not in historical.skills


@pytest.mark.asyncio
async def test_agent_profile_resolution_rejects_unconfigured_and_bad_catalog(
    model_factory,
) -> None:
    module = importlib.import_module("carlo.model_providers")
    cipher = module.CredentialCipher(b"x" * 32)
    async with model_factory() as session:
        unconfigured = models.AgentProfile(name="unconfigured-resolver", provider="pi")
        provider = models.ModelProvider(
            name="Broken",
            slug="broken",
            kind="openai-compatible",
            base_url="http://broken.test/v1",
            active=False,
        )
        model = models.AvailableModel(
            model_provider=provider,
            external_id="missing-limits",
            status="UNAVAILABLE",
        )
        broken = models.AgentProfile(
            name="broken-resolver", provider="pi", available_model_id=None
        )
        legacy = models.AgentProfile(
            name="legacy-resolver",
            provider="pi",
            available_model_id=None,
            default_skills=["/tmp/untrusted-skill"],
        )
        session.add_all([unconfigured, provider, model, broken, legacy])
        await session.flush()
        broken.available_model_id = model.id
        legacy.available_model_id = model.id
        await session.flush()

        with pytest.raises(module.ModelProviderError, match="no managed model"):
            await module.resolve_agent_profile(session, unconfigured, cipher)
        with pytest.raises(module.ModelProviderError, match="inactive"):
            await module.resolve_agent_profile(session, broken, cipher)
        provider.active = True
        model.status = "AVAILABLE"
        model.discovered_context_window = 65_536
        model.discovered_max_tokens = 16_384
        with pytest.raises(module.ModelProviderError, match="unknown skills"):
            await module.resolve_agent_profile(session, legacy, cipher)

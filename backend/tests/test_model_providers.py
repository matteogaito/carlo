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
async def test_refresh_retains_missing_models_and_updates_single_default(
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
        assert provider.default_model_id == catalog[0].id
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

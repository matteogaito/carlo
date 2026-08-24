import asyncio
import base64
import json
import os
from pathlib import Path
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    AgentProfile as AgentProfileRecord,
    AvailableModel,
    Event,
    ModelProvider,
    PiRuntimeSettings,
)

MODEL_CATALOG_LIMIT = 8 * 1024 * 1024
MANAGED_PROFILE_PACKAGES = ("superpowers", "ponytail")
DEFAULT_PROFILE_PACKAGES = MANAGED_PROFILE_PACKAGES
MANAGED_PROFILE_SKILLS = ("frontend-design",)
REQUIRED_PROFILE_SKILLS: dict[str, tuple[str, ...]] = {
    "brief": ("carlo-planning",),
    "plan": ("carlo-planning",),
    "discovery": ("carlo-discovery",),
}


def available_profile_skills() -> set[str]:
    bundled = {
        path.parent.name
        for path in (Path(__file__).resolve().parents[2] / "skills").glob("*/SKILL.md")
    }
    return bundled | set(MANAGED_PROFILE_SKILLS)


def available_profile_packages() -> set[str]:
    return set(MANAGED_PROFILE_PACKAGES)


def partition_plan_resources(
    names: tuple[str, ...],
    package_names: set[str],
    skill_names: set[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    packages = tuple(dict.fromkeys(name for name in names if name in package_names))
    skills = tuple(dict.fromkeys(name for name in names if name in skill_names))
    unknown = sorted(set(names) - package_names - skill_names)
    if unknown:
        raise ModelProviderError(f"plan has unknown resources: {', '.join(unknown)}")
    return packages, skills


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes


@dataclass(frozen=True, slots=True)
class DiscoveredModel:
    external_id: str
    display_name: str | None
    context_window: int | None
    max_tokens: int | None
    input_modalities: tuple[str, ...]
    reasoning: bool
    metadata: dict[str, Any]


class ModelProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelRefreshResult:
    provider_id: int
    seen: int
    unavailable: int


@dataclass(frozen=True, slots=True)
class CompactionTokens:
    reserve_tokens: int
    keep_recent_tokens: int


def calculate_compaction(
    context_window: int,
    max_tokens: int,
    reserve_percent: int,
    keep_recent_percent: int,
) -> CompactionTokens:
    reserve = max(max_tokens, int(context_window * reserve_percent / 100))
    keep_recent = int(context_window * keep_recent_percent / 100)
    if reserve >= context_window or keep_recent >= context_window - reserve:
        raise ModelProviderError("compaction policy does not fit the model context")
    return CompactionTokens(reserve, keep_recent)


async def resolve_agent_profile(
    session: AsyncSession,
    record: AgentProfileRecord,
    cipher: "CredentialCipher | None",
    *,
    task_model_id: int | None = None,
    extra_skills: tuple[str, ...] = (),
    default_tools: tuple[str, ...] = ("read", "grep", "find", "ls"),
) -> Any:
    from .provider import AgentProfile, ResolvedModel

    tools = tuple(record.permissions.get("tools") or default_tools)
    runtime = await session.get(PiRuntimeSettings, 1)
    plan_packages, plan_skills = partition_plan_resources(
        extra_skills, available_profile_packages(), available_profile_skills()
    )
    packages = tuple(
        dict.fromkeys(
            (
                *(runtime.default_packages if runtime else DEFAULT_PROFILE_PACKAGES),
                *record.default_packages,
                *plan_packages,
            )
        )
    )
    unknown_packages = sorted(set(packages) - available_profile_packages())
    if unknown_packages:
        raise ModelProviderError(
            f"agent profile has unknown packages: {', '.join(unknown_packages)}"
        )
    skills = tuple(
        dict.fromkeys(
            (
                *REQUIRED_PROFILE_SKILLS.get(record.name, ()),
                *(runtime.default_skills if runtime else ()),
                *record.default_skills,
                *plan_skills,
            )
        )
    )
    unknown_skills = sorted(set(skills) - available_profile_skills())
    if unknown_skills:
        raise ModelProviderError(f"agent profile has unknown skills: {', '.join(unknown_skills)}")
    model_id = task_model_id or record.available_model_id
    if model_id is None:
        raise ModelProviderError("agent profile has no managed model")
    provider: ModelProvider | None = None
    model: AvailableModel | None = None
    model = await session.get(AvailableModel, model_id)
    if model is None:
        raise ModelProviderError("selected model was not found")
    provider = await session.get(ModelProvider, model.model_provider_id)

    if provider is None or model is None:
        raise ModelProviderError("selected model configuration is incomplete")
    if not provider.active:
        raise ModelProviderError("selected model provider is inactive")
    if model.status != "AVAILABLE":
        raise ModelProviderError("selected model is unavailable")
    context_window = model.effective_context_window
    max_tokens = model.effective_max_tokens
    if context_window is None or max_tokens is None:
        raise ModelProviderError("selected model has no verified context limits")
    reserve_percent = runtime.reserve_percent if runtime else 10
    keep_recent_percent = runtime.keep_recent_percent if runtime else 20
    compaction = calculate_compaction(
        context_window, max_tokens, reserve_percent, keep_recent_percent
    )
    api_key = _provider_api_key(provider, cipher, selected=True)
    compatibility = dict(provider.compatibility)
    api = str(compatibility.pop("api", "openai-completions"))
    return AgentProfile(
        name=record.name,
        model=None,
        effort=record.effort,
        tools=tools,
        skills=skills,
        resolved_model=ResolvedModel(
            model_provider_id=provider.id,
            available_model_id=model.id,
            provider_slug=provider.slug,
            base_url=provider.base_url,
            api=api,
            external_id=model.external_id,
            display_name=model.display_name or model.external_id,
            api_key=api_key,
            compatibility=compatibility,
            input_modalities=tuple(model.input_modalities or ["text"]),
            reasoning=model.reasoning,
            context_window=context_window,
            max_tokens=max_tokens,
            compaction_enabled=runtime.compaction_enabled if runtime else True,
            reserve_tokens=compaction.reserve_tokens,
            keep_recent_tokens=compaction.keep_recent_tokens,
        ),
        packages=packages,
    )


def model_runtime_evidence(profile: Any) -> dict[str, Any] | None:
    model = profile.resolved_model
    if model is None:
        return None
    return {
        "model_provider_id": model.model_provider_id,
        "available_model_id": model.available_model_id,
        "model_provider": model.provider_slug,
        "model": model.external_id,
        "context_window": model.context_window,
        "max_tokens": model.max_tokens,
        "reserve_tokens": model.reserve_tokens,
        "keep_recent_tokens": model.keep_recent_tokens,
    }


class CredentialCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("credential encryption key must contain 32 bytes")
        self._cipher = AESGCM(key)

    @classmethod
    def from_base64(cls, value: str) -> "CredentialCipher":
        return cls(base64.b64decode(value, validate=True))

    def encrypt(self, secret: str) -> EncryptedCredential:
        nonce = os.urandom(12)
        return EncryptedCredential(
            self._cipher.encrypt(
                nonce, secret.encode(), b"carlo:model-provider:v1"
            ),
            nonce,
        )

    def decrypt(self, ciphertext: bytes, nonce: bytes) -> str:
        return self._cipher.decrypt(
            nonce, ciphertext, b"carlo:model-provider:v1"
        ).decode()


def parse_openai_models(payload: Any) -> list[DiscoveredModel]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ModelProviderError("model provider returned an invalid catalog")
    if len(payload["data"]) > 10_000:
        raise ModelProviderError("model provider returned too many models")
    result: list[DiscoveredModel] = []
    seen: set[str] = set()
    for value in payload["data"]:
        if not isinstance(value, dict):
            continue
        external_id = value.get("id")
        if (
            not isinstance(external_id, str)
            or not external_id
            or len(external_id) > 240
            or external_id in seen
        ):
            continue
        seen.add(external_id)
        display_name = value.get("name")
        if not isinstance(display_name, str) or len(display_name) > 240:
            display_name = None
        modalities = value.get("input") or value.get("input_modalities") or []
        result.append(
            DiscoveredModel(
                external_id=external_id,
                display_name=display_name,
                context_window=_first_positive_integer(
                    value, "context_window", "context_length", "max_model_len"
                ),
                max_tokens=_first_positive_integer(value, "max_tokens"),
                input_modalities=tuple(
                    item for item in modalities if isinstance(item, str)
                )
                if isinstance(modalities, list)
                else (),
                reasoning=value.get("reasoning") is True,
                metadata=value,
            )
        )
    return result


async def fetch_openai_models(
    provider: ModelProvider, api_key: str | None
) -> list[DiscoveredModel]:
    return parse_openai_models(
        await asyncio.to_thread(_fetch_json, provider.base_url, api_key)
    )


async def refresh_model_provider(
    factory: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher | None,
    provider_id: int,
    *,
    fetcher: Callable[
        [ModelProvider, str | None], Awaitable[list[DiscoveredModel]]
    ] = fetch_openai_models,
    now: datetime | None = None,
) -> ModelRefreshResult:
    now = now or datetime.now(UTC)
    try:
        async with factory() as session:
            provider = await session.get(ModelProvider, provider_id)
            if provider is None:
                raise ModelProviderError("model provider not found")
            if not provider.active:
                raise ModelProviderError("model provider is inactive")
            api_key = _provider_api_key(provider, cipher)
            provider.last_refresh_attempt_at = now
            await session.commit()
    except ModelProviderError as error:
        await _record_refresh_failure(factory, provider_id, now, error)
        raise

    try:
        discovered = await fetcher(provider, api_key)
    except Exception as error:
        await _record_refresh_failure(factory, provider_id, now, error)
        raise ModelProviderError(str(error)) from error

    async with factory() as session:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            raise ModelProviderError("model provider was deleted during refresh")
        existing = {
            model.external_id: model
            for model in (
                await session.scalars(
                    select(AvailableModel).where(
                        AvailableModel.model_provider_id == provider_id
                    )
                )
            ).all()
        }
        seen: set[str] = set()
        became_available: list[str] = []
        for entry in discovered:
            seen.add(entry.external_id)
            model = existing.get(entry.external_id)
            if model is None:
                model = AvailableModel(
                    model_provider_id=provider_id, external_id=entry.external_id
                )
                session.add(model)
                existing[entry.external_id] = model
                became_available.append(entry.external_id)
            elif model.status != "AVAILABLE":
                became_available.append(entry.external_id)
            model.display_name = entry.display_name
            model.status = "AVAILABLE"
            model.discovered_context_window = entry.context_window
            model.discovered_max_tokens = entry.max_tokens
            model.input_modalities = list(entry.input_modalities)
            model.reasoning = entry.reasoning
            model.metadata_json = entry.metadata
            model.last_seen_at = now
        unavailable = 0
        became_unavailable: list[str] = []
        for external_id, model in existing.items():
            if external_id not in seen:
                if model.status == "AVAILABLE":
                    became_unavailable.append(external_id)
                model.status = "UNAVAILABLE"
                unavailable += 1
        await session.flush()
        provider.last_refresh_success_at = now
        provider.last_refresh_status = "SUCCESS"
        provider.last_refresh_error = None
        if became_available or became_unavailable:
            session.add(
                Event(
                    type="model_provider.availability_changed",
                    payload={
                        "model_provider_id": provider_id,
                        "available": became_available,
                        "unavailable": became_unavailable,
                    },
                )
            )
        session.add(
            Event(
                type="model_provider.refresh_completed",
                payload={
                    "model_provider_id": provider_id,
                    "seen": len(discovered),
                    "unavailable": unavailable,
                },
            )
        )
        await session.commit()
        return ModelRefreshResult(provider_id, len(discovered), unavailable)


async def refresh_due_model_providers(
    factory: async_sessionmaker[AsyncSession],
    cipher: CredentialCipher | None,
    *,
    now: datetime | None = None,
    fetcher: Callable[
        [ModelProvider, str | None], Awaitable[list[DiscoveredModel]]
    ] = fetch_openai_models,
) -> int:
    now = now or datetime.now(UTC)
    async with factory() as session:
        providers = (
            await session.scalars(
                select(ModelProvider)
                .where(ModelProvider.active.is_(True))
                .order_by(ModelProvider.id)
            )
        ).all()
    due = [
        provider.id
        for provider in providers
        if provider.last_refresh_attempt_at is None
        or provider.last_refresh_attempt_at
        <= now - timedelta(minutes=provider.refresh_interval_minutes)
    ]
    for provider_id in due:
        try:
            await refresh_model_provider(
                factory, cipher, provider_id, fetcher=fetcher, now=now
            )
        except ModelProviderError:
            pass
    return len(due)


async def _record_refresh_failure(
    factory: async_sessionmaker[AsyncSession],
    provider_id: int,
    now: datetime,
    error: Exception,
) -> None:
    message = str(error).strip()[:500] or error.__class__.__name__
    async with factory() as session:
        provider = await session.get(ModelProvider, provider_id)
        if provider is None:
            return
        provider.last_refresh_attempt_at = now
        provider.last_refresh_status = "FAILED"
        provider.last_refresh_error = message
        session.add(
            Event(
                type="model_provider.refresh_failed",
                payload={"model_provider_id": provider_id, "error": message},
            )
        )
        await session.commit()


def _fetch_json(base_url: str, api_key: str | None) -> Any:
    url = f"{base_url.rstrip('/')}/models"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelProviderError("model provider URL must be HTTP(S)")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            body = response.read(MODEL_CATALOG_LIMIT + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ModelProviderError(f"model catalog request failed: {error}") from error
    if len(body) > MODEL_CATALOG_LIMIT:
        raise ModelProviderError("model provider catalog exceeds 8 MiB")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ModelProviderError("model provider returned invalid JSON") from error


def _first_positive_integer(value: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            return candidate
    return None


def _provider_api_key(
    provider: ModelProvider,
    cipher: CredentialCipher | None,
    *,
    selected: bool = False,
) -> str | None:
    prefix = "selected model provider" if selected else "model provider"
    ciphertext = provider.credential_ciphertext
    nonce = provider.credential_nonce
    if ciphertext is None and nonce is None:
        return None
    if ciphertext is None or nonce is None:
        raise ModelProviderError(f"{prefix} credential configuration is incomplete")
    if cipher is None:
        raise ModelProviderError("credential encryption is not configured")
    try:
        return cipher.decrypt(ciphertext, nonce)
    except (InvalidTag, UnicodeDecodeError) as error:
        raise ModelProviderError(f"{prefix} credential could not be decrypted") from error

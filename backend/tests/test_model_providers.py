import importlib
import importlib.util

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

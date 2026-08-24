import base64
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True, slots=True)
class EncryptedCredential:
    ciphertext: bytes
    nonce: bytes


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

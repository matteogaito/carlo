import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import LoginFailure, User, UserSession

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
LOGIN_WINDOW = timedelta(minutes=15)
FAILURE_RETENTION = timedelta(hours=24)
MAX_LOGIN_FAILURES = 5


class InvalidCredentials(Exception):
    pass


class LoginThrottled(Exception):
    pass


def normalize_username(username: str) -> str:
    return username.strip().lower()


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=int(n),
            r=int(r),
            p=int(p),
        )
        return secrets.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


_DUMMY_PASSWORD_HASH = hash_password("unavailable-password")


async def authenticate(
    factory: async_sessionmaker[AsyncSession], username: str, password: str
) -> User | None:
    normalized = normalize_username(username)
    async with factory() as session:
        user = await session.scalar(select(User).where(User.username == normalized))
        encoded = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        valid = verify_password(password, encoded)
        if user is None or not user.active or not valid:
            return None
        return user


async def login(
    factory: async_sessionmaker[AsyncSession],
    username: str,
    password: str,
    source_ip: str,
    session_hours: int,
) -> tuple[User, str]:
    now = datetime.now(UTC)
    normalized = normalize_username(username)
    async with factory() as session:
        await session.execute(
            delete(LoginFailure).where(
                LoginFailure.attempted_at < now - FAILURE_RETENTION
            )
        )
        failures = await session.scalar(
            select(func.count(LoginFailure.id)).where(
                LoginFailure.attempted_at >= now - LOGIN_WINDOW,
                or_(
                    LoginFailure.username == normalized,
                    LoginFailure.source_ip == source_ip,
                ),
            )
        )
        if (failures or 0) >= MAX_LOGIN_FAILURES:
            await session.commit()
            raise LoginThrottled("try again later")

        user = await session.scalar(select(User).where(User.username == normalized))
        encoded = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
        if user is None or not user.active or not verify_password(password, encoded):
            session.add(LoginFailure(username=normalized, source_ip=source_ip))
            await session.commit()
            raise InvalidCredentials("invalid credentials")

        token = secrets.token_urlsafe(32)
        session.add(
            UserSession(
                user_id=user.id,
                token_digest=_token_digest(token),
                expires_at=now + timedelta(hours=session_hours),
            )
        )
        await session.execute(
            delete(LoginFailure).where(
                or_(
                    LoginFailure.username == normalized,
                    LoginFailure.source_ip == source_ip,
                )
            )
        )
        await session.commit()
        return user, token


async def resolve_session(
    factory: async_sessionmaker[AsyncSession], token: str
) -> User | None:
    if not token:
        return None
    async with factory() as session:
        record = await session.scalar(
            select(UserSession).where(
                UserSession.token_digest == _token_digest(token),
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > datetime.now(UTC),
            )
        )
        if record is None or not record.user.active:
            return None
        return record.user


async def revoke_session(
    factory: async_sessionmaker[AsyncSession], token: str
) -> None:
    if not token:
        return
    async with factory() as session:
        record = await session.scalar(
            select(UserSession).where(UserSession.token_digest == _token_digest(token))
        )
        if record is not None and record.revoked_at is None:
            record.revoked_at = datetime.now(UTC)
            await session.commit()


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

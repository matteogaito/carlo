import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import hash_password, normalize_username
from .config import Settings
from .db import make_engine, make_session_factory
from .models import User


async def bootstrap_admin(
    factory: async_sessionmaker[AsyncSession], username: str, password: str
) -> User:
    normalized = normalize_username(username)
    if not normalized or normalized.startswith("change_me") or password.startswith(
        "CHANGE_ME"
    ):
        raise ValueError("bootstrap credentials still contain a placeholder")

    async with factory() as session:
        existing = await session.scalar(select(User).where(User.role == "admin"))
        if existing is not None:
            return existing
        user = User(
            username=normalized,
            password_hash=hash_password(password),
            role="admin",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def run() -> None:
    settings = Settings.from_env()
    engine = make_engine(settings)
    try:
        user = await bootstrap_admin(
            make_session_factory(engine),
            settings.bootstrap_admin_username,
            settings.bootstrap_admin_password,
        )
        print(f"Administrator ready: {user.username}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())

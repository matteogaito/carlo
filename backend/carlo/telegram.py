import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import Event, NotificationCursor, NotificationDelivery

logger = logging.getLogger("carlo.telegram")

LABELS = {
    "task.created": "Task created",
    "planning.started": "Planning started",
    "planning.completed": "Plan completed",
    "planning.failed": "Planning failed",
    "plan.approved": "Plan approved",
    "execution.started": "Execution started",
    "execution.completed": "Task completed",
    "execution.blocked": "Execution blocked",
    "execution.failed": "Execution failed",
    "execution.interrupted": "Execution interrupted",
    "escalation.completed": "Escalation completed",
    "plan.amendment_proposed": "Plan amendment needs approval",
}
BLOCKING_EVENTS = {
    "planning.failed",
    "execution.blocked",
    "execution.failed",
    "execution.interrupted",
    "plan.amendment_proposed",
}


class TelegramError(Exception):
    pass


class TelegramSender(Protocol):
    async def send(self, token: str, chat_id: str, message: str) -> None: ...


class TelegramTransport:
    async def send(self, token: str, chat_id: str, message: str) -> None:
        body = json.dumps({"chat_id": chat_id, "text": message}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            await asyncio.to_thread(_send_request, request)
        except urllib.error.HTTPError as error:
            raise TelegramError(f"Telegram returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise TelegramError("Telegram request failed") from None


def _send_request(request: urllib.request.Request) -> None:
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read())
    if not payload.get("ok"):
        raise ValueError("Telegram rejected the message")


def telegram_enabled(token: str, chat_id: str) -> bool:
    return bool(
        token
        and chat_id
        and not token.upper().startswith("CHANGE_ME")
        and not chat_id.upper().startswith("CHANGE_ME")
    )


def format_event(event: Event) -> tuple[str, str]:
    severity = (
        "blocking"
        if event.type in BLOCKING_EVENTS
        or event.type.endswith((".failed", ".blocked", ".interrupted"))
        else "info"
    )
    label = LABELS.get(event.type, event.type.replace(".", " ").capitalize())
    identity = event.task_id or "CARLO"
    lines = [f"[{severity.upper()}] {identity} — {label}"]
    for key, value in list(event.payload.items())[:6]:
        rendered = str(value).replace("\n", " ")[:240]
        lines.append(f"{key}: {rendered}")
    return "\n".join(lines)[:3900], severity


class TelegramNotifier:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        transport: TelegramSender,
        token: str,
        chat_id: str,
        level: str,
        *,
        max_attempts: int = 5,
        retry_seconds: int = 5,
    ) -> None:
        self.session_factory = session_factory
        self.transport = transport
        self.token = token
        self.chat_id = chat_id
        self.level = level
        self.max_attempts = max_attempts
        self.retry_seconds = retry_seconds
        self.destination = f"telegram:{chat_id}"

    async def initialize_cursor(self) -> None:
        async with self.session_factory() as session:
            if await session.get(NotificationCursor, self.destination) is not None:
                return
            sequence = await session.scalar(select(func.max(Event.sequence)))
            session.add(
                NotificationCursor(
                    destination=self.destination, last_sequence=int(sequence or 0)
                )
            )
            await session.commit()

    async def deliver_next(self) -> bool:
        await self.initialize_cursor()
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            cursor = await session.scalar(
                select(NotificationCursor)
                .where(NotificationCursor.destination == self.destination)
                .with_for_update()
            )
            if cursor is None:
                return False
            event = await session.scalar(
                select(Event)
                .where(Event.sequence > cursor.last_sequence)
                .order_by(Event.sequence)
                .limit(1)
            )
            if event is None:
                return False
            delivery = await session.scalar(
                select(NotificationDelivery).where(
                    NotificationDelivery.event_sequence == event.sequence,
                    NotificationDelivery.destination == self.destination,
                )
            )
            if delivery is None:
                delivery = NotificationDelivery(
                    event_sequence=event.sequence,
                    destination=self.destination,
                )
                session.add(delivery)
                await session.flush()
            if delivery.status in {"sent", "skipped", "abandoned"}:
                cursor.last_sequence = event.sequence
                await session.commit()
                return True
            if delivery.next_attempt_at is not None and delivery.next_attempt_at > now:
                return False

            message, severity = format_event(event)
            if self.level == "blocking" and severity != "blocking":
                delivery.status = "skipped"
                cursor.last_sequence = event.sequence
                await session.commit()
                return True

            try:
                await self.transport.send(self.token, self.chat_id, message)
            except Exception as error:
                delivery.attempts += 1
                delivery.last_error = str(error).replace(self.token, "<redacted>")[:500]
                if delivery.attempts >= self.max_attempts:
                    delivery.status = "abandoned"
                    cursor.last_sequence = event.sequence
                else:
                    delivery.status = "pending"
                    delivery.next_attempt_at = now + timedelta(
                        seconds=self.retry_seconds * 2 ** (delivery.attempts - 1)
                    )
                await session.commit()
                return True

            delivery.attempts += 1
            delivery.status = "sent"
            delivery.next_attempt_at = None
            delivery.last_error = None
            cursor.last_sequence = event.sequence
            await session.commit()
            return True


async def notification_loop(notifier: TelegramNotifier) -> None:
    await notifier.initialize_cursor()
    while True:
        try:
            delivered = await notifier.deliver_next()
        except Exception:
            logger.exception("Telegram notification cycle failed")
            delivered = False
        await asyncio.sleep(0.25 if delivered else 2)

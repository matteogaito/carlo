import asyncio
import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .domain import TaskStage, TaskStatus
from .models import ActionRun, Event, NotificationCursor, NotificationDelivery, Task

logger = logging.getLogger("carlo.telegram")

LABELS = {
    "task.created": "Task created",
    "planning.started": "Planning started",
    "planning.completed": "Plan completed",
    "planning.failed": "Planning failed",
    "planning.question": "Planner question needs an answer",
    "plan.approved": "Plan approved",
    "execution.started": "Execution started",
    "execution.completed": "Task completed",
    "execution.blocked": "Execution blocked",
    "execution.failed": "Execution failed",
    "execution.interrupted": "Execution interrupted",
    "escalation.completed": "Escalation completed",
    "plan.amendment_proposed": "Plan amendment needs approval",
    "system.started": "CARLO started",
    "worker.started": "CARLO worker started",
    "pi.update_completed": "Pi weekly update completed",
    "pi.update_failed": "Pi weekly update failed",
    "pi.resources_updated": "Pi skills updated",
    "pi.resources_update_failed": "Pi skills update failed",
}
NOTIFY_EVENTS = frozenset(LABELS)
DETAIL_FIELDS = {
    "planning.completed": ("revision",),
    "planning.failed": ("error",),
    "planning.question": ("text",),
    "execution.blocked": ("outcome", "reason", "error"),
    "execution.failed": ("outcome", "error"),
    "execution.interrupted": ("interruption", "limit", "error"),
    "plan.amendment_proposed": ("summary", "reason"),
    "system.started": ("hostname", "pi_version"),
    "worker.started": ("hostname", "pi_version"),
    "pi.update_completed": ("version", "previous_version"),
    "pi.update_failed": ("error", "exit_code"),
    "pi.resources_updated": ("summary",),
    "pi.resources_update_failed": ("error",),
}
ALWAYS_NOTIFY_EVENTS = {
    "system.started",
    "worker.started",
    "pi.update_completed",
    "pi.update_failed",
    "pi.resources_updated",
    "pi.resources_update_failed",
}
BLOCKING_EVENTS = {
    "planning.failed",
    "planning.question",
    "execution.blocked",
    "execution.failed",
    "execution.interrupted",
    "escalation.completed",
    "plan.amendment_proposed",
}


class TelegramError(Exception):
    pass


class TelegramSender(Protocol):
    async def send(self, token: str, chat_id: str, message: str) -> None: ...


class TelegramTransport:
    async def send(self, token: str, chat_id: str, message: str) -> None:
        await self._request(token, "sendMessage", {"chat_id": chat_id, "text": message})

    async def set_commands(
        self, token: str, commands: list[dict[str, str]]
    ) -> None:
        await self._request(token, "setMyCommands", {"commands": commands})

    async def get_updates(
        self, token: str, offset: int, timeout: int
    ) -> list[dict[str, Any]]:
        result = await self._request(
            token, "getUpdates", {"offset": offset, "timeout": timeout}, timeout + 5
        )
        return result if isinstance(result, list) else []

    async def _request(
        self,
        token: str,
        method: str,
        payload: dict[str, Any],
        timeout: int = 10,
    ) -> Any:
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            return await asyncio.to_thread(_send_request, request, timeout)
        except urllib.error.HTTPError as error:
            raise TelegramError(f"Telegram returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise TelegramError("Telegram request failed") from None


def _send_request(request: urllib.request.Request, timeout: int) -> Any:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    if not payload.get("ok"):
        raise ValueError("Telegram rejected the message")
    return payload.get("result")


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
    for key in DETAIL_FIELDS.get(event.type, ()):
        if key not in event.payload:
            continue
        value = event.payload[key]
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
            sequence = await session.scalar(select(func.max(Event.sequence)))
            await session.execute(
                insert(NotificationCursor)
                .values(
                    destination=self.destination, last_sequence=int(sequence or 0)
                )
                .on_conflict_do_nothing(index_elements=["destination"])
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

            if event.type not in NOTIFY_EVENTS:
                delivery.status = "skipped"
                cursor.last_sequence = event.sequence
                await session.commit()
                return True

            message, severity = format_event(event)
            if (
                self.level == "blocking"
                and severity != "blocking"
                and event.type not in ALWAYS_NOTIFY_EVENTS
            ):
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


class TelegramCommandTransport(TelegramSender, Protocol):
    async def set_commands(
        self, token: str, commands: list[dict[str, str]]
    ) -> None: ...

    async def get_updates(
        self, token: str, offset: int, timeout: int
    ) -> list[dict[str, Any]]: ...


class TelegramCommandBot:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        transport: TelegramCommandTransport,
        token: str,
        chat_id: str,
    ) -> None:
        self.session_factory = session_factory
        self.transport = transport
        self.token = token
        self.chat_id = chat_id
        self.destination = f"telegram-inbound:{chat_id}"
        self.initialized = False

    async def initialize(self) -> None:
        if self.initialized:
            return
        await self.transport.set_commands(
            self.token,
            [{"command": "status", "description": "Show what CARLO is doing"}],
        )
        async with self.session_factory() as session:
            await session.execute(
                insert(NotificationCursor)
                .values(destination=self.destination, last_sequence=0)
                .on_conflict_do_nothing(index_elements=["destination"])
            )
            await session.commit()
        self.initialized = True

    async def poll_once(self) -> bool:
        await self.initialize()
        async with self.session_factory() as session:
            cursor = await session.get(NotificationCursor, self.destination)
            offset = (cursor.last_sequence if cursor else 0) + 1
        updates = await self.transport.get_updates(self.token, offset, 25)
        if not updates:
            return False
        for update in sorted(updates, key=lambda item: int(item.get("update_id", 0))):
            update_id = int(update.get("update_id", 0))
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            text = message.get("text", "")
            if chat_id == self.chat_id and _command(text) == "/status":
                await self.transport.send(
                    self.token, self.chat_id, await _status_message(self.session_factory)
                )
            async with self.session_factory() as session:
                cursor = await session.get(NotificationCursor, self.destination)
                if cursor is not None and update_id > cursor.last_sequence:
                    cursor.last_sequence = update_id
                    await session.commit()
        return True


def _command(text: Any) -> str:
    if not isinstance(text, str) or not text:
        return ""
    return text.split(maxsplit=1)[0].split("@", 1)[0].lower()


async def _status_message(
    factory: async_sessionmaker[AsyncSession],
) -> str:
    async with factory() as session:
        implementation = await session.scalar(
            select(Task)
            .where(Task.status == TaskStatus.IN_PROGRESS)
            .order_by(Task.updated_at.desc())
            .limit(1)
        )
        planning = (
            await session.scalars(
                select(Task)
                .where(Task.stage.in_((TaskStage.BRIEFING, TaskStage.PLANNING)))
                .order_by(Task.updated_at.desc())
                .limit(3)
            )
        ).all()
        action = await session.scalar(
            select(ActionRun)
            .where(ActionRun.status == "running")
            .order_by(ActionRun.started_at.desc(), ActionRun.id)
            .limit(1)
        )
        ready = int(
            await session.scalar(
                select(func.count())
                .select_from(Task)
                .where(Task.status == TaskStatus.READY)
            )
            or 0
        )
        action_queue = int(
            await session.scalar(
                select(func.count())
                .select_from(ActionRun)
                .where(ActionRun.status == "queued")
            )
            or 0
        )

    lines = ["CARLO status", ""]
    lines.append(
        f"Implementation: {implementation.id} — {implementation.stage.value}"
        if implementation
        else "Implementation: idle"
    )
    if action:
        step = f", step {action.current_step}" if action.current_step is not None else ""
        lines.append(
            f"Action: {action.action_name} #{action.id} — {action.internal_stage}{step}"
        )
    else:
        lines.append("Action: idle")
    lines.append(
        "Planning: " + ", ".join(f"{task.id} — {task.stage.value}" for task in planning)
        if planning
        else "Planning: idle"
    )
    lines.extend((f"Ready queue: {ready}", f"Action queue: {action_queue}"))
    return "\n".join(lines)


async def notification_loop(notifier: TelegramNotifier) -> None:
    await notifier.initialize_cursor()
    while True:
        try:
            delivered = await notifier.deliver_next()
        except Exception:
            logger.exception("Telegram notification cycle failed")
            delivered = False
        await asyncio.sleep(0.25 if delivered else 2)


async def command_loop(bot: TelegramCommandBot) -> None:
    while True:
        try:
            await bot.poll_once()
        except Exception:
            logger.exception("Telegram command cycle failed")
            await asyncio.sleep(2)

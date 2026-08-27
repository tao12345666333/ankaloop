from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..agent import Agent, BusyError, create_agent_by_name
from ..agent_spec import get_default_agent_spec
from ..config import load_config, save_config
from ..constants import CONFIG_DIR_NAME
from ..event_bus import EventType, get_event_bus
from ..memory import CONFIG_DIR
from ..memory_dream import MemoryDreamer
from ..multi_agent import get_agent_registry
from ..runtime import CancellationResult, TurnCancelledError, TurnHandle, TurnStatus
from .auth import AuthMiddleware
from .config import TelegramConfig, TelegramStreamingConfig, normalize_dm_policy, normalize_group_policy
from .formatter import TelegramFormatter
from .handlers import (
    RateLimiter,
    SessionManager,
    TelegramDeliveryToken,
    TelegramHandlers,
    TelegramQueuedMessage,
)
from .scheduler import SCHEDULE_BLUEPRINTS, TelegramScheduledPrompt, TelegramScheduleStore

if TYPE_CHECKING:
    from telegram.ext import Application

_TELEGRAM_BAD_REQUEST_TYPES: tuple[type[BaseException], ...]
_TELEGRAM_NETWORK_ERROR_TYPES: tuple[type[BaseException], ...]

try:
    from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.error import BadRequest, NetworkError
    from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters
except ImportError:  # pragma: no cover - optional dependency
    _TELEGRAM_BAD_REQUEST_TYPES = ()
    _TELEGRAM_NETWORK_ERROR_TYPES = ()
    BotCommand = None  # type: ignore[assignment,misc]
    InlineKeyboardButton = None  # type: ignore[assignment,misc]
    InlineKeyboardMarkup = None  # type: ignore[assignment,misc]
    ApplicationBuilder = None  # type: ignore[assignment,misc]
    CallbackQueryHandler = None  # type: ignore[assignment,misc]
    CommandHandler = None  # type: ignore[assignment,misc]
    MessageHandler = None  # type: ignore[assignment,misc]
    filters = None  # type: ignore[assignment,misc]
else:
    _TELEGRAM_BAD_REQUEST_TYPES = (BadRequest,)
    _TELEGRAM_NETWORK_ERROR_TYPES = (NetworkError,)

logger = logging.getLogger(__name__)

TELEGRAM_MEMORY_DREAM_INTERVAL_SECONDS = 60 * 60


@dataclass
class PairingRequest:
    code: str
    user_id: int
    chat_id: int
    username: str | None
    created_at: datetime


class _StreamingDeliveryManager:
    """Accumulate LLM chunks and periodically edit a Telegram status message.

    The manager throttles ``edit_message_text`` calls to respect Telegram
    rate limits (roughly 30 per minute per chat).  When the accumulated text
    exceeds ``max_chars``, the current message is sealed (final edit) and a
    new message is started for overflow content.
    """

    def __init__(
        self,
        bot: Any,
        chat_id: int,
        status_message_id: int | None,
        formatter: TelegramFormatter,
        cfg: TelegramStreamingConfig,
        is_current: Callable[[], bool],
        stop_markup: Any = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = status_message_id
        self._formatter = formatter
        self._cfg = cfg
        self._is_current = is_current
        self._stop_markup = stop_markup
        self._buffer: str = ""
        self._last_edit_time: float = 0.0
        self._sealed_messages: list[str] = []
        self._overflow_message_id: int | None = None
        self._needs_flush: bool = False

    def on_chunk(self, event_type: str, data: dict[str, Any]) -> None:
        """Synchronous callback invoked by the agent event system.

        Only accumulates text; the actual Telegram edit is deferred to
        ``maybe_flush`` which runs in the async context of _process_message.
        """
        if event_type != "message.chunk":
            return
        chunk = data.get("content", "")
        if not chunk:
            return
        self._buffer += chunk
        now = time.monotonic()
        if len(self._buffer) >= self._cfg.streaming_min_chars and (
            now - self._last_edit_time >= self._cfg.streaming_interval_seconds
        ):
            self._needs_flush = True

    async def maybe_flush(self) -> None:
        """Edit the status message if enough content has accumulated.

        Called periodically from the async context in ``_process_message``.
        """
        if not self._needs_flush or not self._buffer:
            return
        self._needs_flush = False
        if not self._is_current():
            return
        self._last_edit_time = time.monotonic()
        if len(self._buffer) > self._cfg.streaming_max_chars:
            seal_text = self._buffer[: self._cfg.streaming_max_chars]
            self._buffer = self._buffer[self._cfg.streaming_max_chars :]
            await self._seal_and_send(seal_text)
        else:
            await self._edit_status(self._buffer)

    async def _seal_and_send(self, sealed_text: str) -> None:
        """Seal current message with sealed_text, start new message for buffer."""
        await self._edit_status(sealed_text)
        self._sealed_messages.append(sealed_text)
        try:
            msg = await self._bot.send_text(self._chat_id, self._buffer[: self._cfg.streaming_max_chars])
            self._overflow_message_id = self._bot._get_telegram_message_id(msg)
        except Exception:
            logger.debug("Failed to send overflow streaming message", exc_info=True)

    async def _edit_status(self, text: str) -> None:
        if self._message_id is None:
            return
        try:
            await self._bot._edit_text(self._chat_id, self._message_id, text, markdown=False)
        except Exception:
            logger.debug("Streaming edit failed for %s/%s", self._chat_id, self._message_id, exc_info=True)

    async def finalize(self, full_response: str) -> None:
        """Deliver the final formatted response with proper markdown."""
        if not self._is_current():
            return
        self._buffer = ""
        chunks = self._formatter.format_response(full_response) if full_response else []
        if not chunks:
            return
        # Edit the status/overflow message with the first chunk (markdown formatted)
        target_id = self._overflow_message_id or self._message_id
        remaining = chunks
        if target_id is not None and await self._bot._edit_text(self._chat_id, target_id, chunks[0], markdown=True):
            remaining = chunks[1:]
        # Send remaining chunks as new messages
        for chunk in remaining:
            if not self._is_current():
                break
            await self._bot.send_markdown(self._chat_id, chunk)


def _utf16_len(text: str) -> int:
    """Length of text in UTF-16 code units (Telegram's message limit unit)."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _split_plain_text(text: str, max_length: int) -> list[str]:
    """Split plain text into chunks within a UTF-16 length limit, preferring newlines."""
    if _utf16_len(text) <= max_length:
        return [text]
    parts: list[str] = []
    remaining = text
    limit = max(1, max_length)
    while remaining:
        # Longest prefix of remaining that fits in `limit` UTF-16 units.
        units = 0
        cut = 0
        for i, ch in enumerate(remaining):
            units += 2 if ord(ch) > 0xFFFF else 1
            if units > limit:
                break
            cut = i + 1
        if cut < len(remaining):
            newline = remaining.rfind("\n", 0, cut)
            if newline > 0:
                cut = newline
        parts.append(remaining[:cut].rstrip("\n"))
        remaining = remaining[cut:].lstrip("\n")
        if not parts[-1]:
            break
    return parts


class TelegramBot:
    """Telegram bot that connects chat messages to an AnkaLoop agent."""

    def __init__(
        self,
        token: str,
        allowed_users: set[int],
        admin_users: set[int] | None = None,
        *,
        agent_factory: Callable[[str], Agent] | None = None,
        work_dir: Path | None = None,
        config: TelegramConfig | None = None,
    ) -> None:
        if ApplicationBuilder is None:
            raise RuntimeError("python-telegram-bot is required. Install ankaloop[telegram].")

        self._token = token
        self._work_dir = work_dir
        self._config = config or TelegramConfig(enabled=True)
        self._config.bot_token = token
        self._auth = AuthMiddleware(
            allowed_users=set(allowed_users),
            admin_users=set(admin_users or []),
        )
        self._auth.allowed_users.update(self._auth.admin_users)
        self._config.allowed_users = sorted(self._auth.allowed_users)
        self._config.admin_users = sorted(self._auth.admin_users)
        self._formatter = TelegramFormatter(max_length=self._config.max_message_length)
        self._session_manager = SessionManager(
            agent_factory=agent_factory or self._default_agent_factory,
            session_timeout=self._config.session_timeout,
        )
        self._rate_limiter = RateLimiter(limit=self._config.rate_limit_messages)
        self._handlers = TelegramHandlers(
            bot=self,
            session_manager=self._session_manager,
            auth=self._auth,
            rate_limiter=self._rate_limiter,
            work_dir=self._work_dir,
        )
        self._application = self._build_application()
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._notification_handler_ids: list[str] = []
        self._skill_watcher: Any = None  # SkillWatcher (lazy import)
        self._typing_tasks: dict[int, asyncio.Task[None]] = {}
        self._typing_counts: dict[int, int] = {}
        self._typing_lock = asyncio.Lock()
        self._pairing_requests: dict[str, PairingRequest] = {}
        self._scheduler: Any = None  # AssistantScheduler (lazy import)
        self._prompt_scheduler: Any = None  # TelegramPromptScheduler (lazy import)
        self._schedule_store = TelegramScheduleStore()
        self._session_boundary_locks: dict[int, asyncio.Lock] = {}
        self._memory_dream_task: asyncio.Task[None] | None = None
        self._memory_dream_interval_seconds = TELEGRAM_MEMORY_DREAM_INTERVAL_SECONDS

    @property
    def config(self) -> TelegramConfig:
        return self._config

    def config_summary(self) -> str:
        token_status = "set" if self._config.bot_token else "unset"
        return "\n".join(
            [
                f"enabled: {self._config.enabled}",
                f"bot_token: {token_status}",
                f"allowed_users: {len(self._config.allowed_users)}",
                f"admin_users: {len(self._config.admin_users)}",
                f"webhook_mode: {self._config.webhook_mode}",
                f"webhook_url: {self._config.webhook_url or ''}",
                f"max_message_length: {self._config.max_message_length}",
                f"rate_limit_messages: {self._config.rate_limit_messages}",
                f"session_timeout: {self._config.session_timeout}",
                f"dm_policy: {self._config.dm_policy}",
                f"group_policy: {self._config.group_policy}",
                f"group_allow_users: {len(self._config.group_allow_users)}",
                f"typing_indicator: {self._config.typing_indicator}",
                f"typing_interval_seconds: {self._config.typing_interval_seconds}",
                f"max_queue_size: {self._config.max_queue_size}",
            ]
        )

    def users_summary(self) -> str:
        allowed = ", ".join(str(u) for u in sorted(self._auth.allowed_users)) or "none"
        admins = ", ".join(str(u) for u in sorted(self._auth.admin_users)) or "none"
        return f"allowed_users: {allowed}\nadmin_users: {admins}"

    def add_allowed_user(self, user_id: int) -> None:
        self._auth.allowed_users.add(user_id)
        self._config.allowed_users = sorted(self._auth.allowed_users)

    def remove_allowed_user(self, user_id: int) -> None:
        self._auth.allowed_users.discard(user_id)
        self._config.allowed_users = sorted(self._auth.allowed_users)
        if user_id in self._auth.admin_users:
            self._auth.admin_users.discard(user_id)
            self._config.admin_users = sorted(self._auth.admin_users)

    def add_admin_user(self, user_id: int) -> None:
        self._auth.admin_users.add(user_id)
        self._auth.allowed_users.add(user_id)
        self._config.admin_users = sorted(self._auth.admin_users)
        self._config.allowed_users = sorted(self._auth.allowed_users)

    def remove_admin_user(self, user_id: int) -> None:
        self._auth.admin_users.discard(user_id)
        self._config.admin_users = sorted(self._auth.admin_users)

    def update_config_value(self, key: str, value: str) -> tuple[bool, str]:
        key_map = {
            "enabled": bool,
            "webhook_mode": bool,
            "webhook_url": str,
            "max_message_length": int,
            "rate_limit_messages": int,
            "session_timeout": int,
            "typing_indicator": bool,
            "typing_interval_seconds": int,
            "max_queue_size": int,
            "dm_policy": str,
            "group_policy": str,
        }
        if key not in key_map:
            return False, f"Unsupported config key: {key}"
        try:
            parsed: Any = value
            if key in {"dm_policy", "group_policy"}:
                parsed = normalize_dm_policy(value) if key == "dm_policy" else normalize_group_policy(value)
                if parsed != value.strip().lower():
                    return False, f"Invalid value for {key}."
            elif key_map[key] is bool:
                parsed = value.lower() in {"1", "true", "yes", "on"}
            elif key_map[key] is int:
                parsed = int(value)
                if key in {"typing_interval_seconds", "max_queue_size"} and parsed < 1:
                    return False, f"{key} must be >= 1."
            else:
                parsed = value
            setattr(self._config, key, parsed)
            if key == "max_message_length":
                self._formatter.max_length = parsed
            elif key == "rate_limit_messages":
                self._rate_limiter.update_limit(parsed)
            elif key == "session_timeout":
                self._session_manager.update_session_timeout(parsed)
            return True, f"Updated {key}."
        except ValueError:
            return False, f"Invalid value for {key}."

    def models_summary(self) -> str:
        """Return a user-facing summary of configured LLM provider profiles."""
        cfg = load_config()
        chat = cfg.chat
        if chat is None:
            return "No chat configuration found."

        if not chat.providers:
            lines = ["Configured providers:", "* current"]
            if chat.model:
                lines[-1] += f" — model={chat.model}"
            if chat.base_url:
                lines[-1] += f" — base_url={chat.base_url}"
            lines.append("Add [chat.providers.<name>] entries to config.toml to enable switching.")
            return "\n".join(lines)

        lines = ["Configured providers:"]
        for name, provider in sorted(chat.providers.items()):
            marker = "*" if name == chat.active_provider else "-"
            details = []
            if provider.api_type:
                details.append(f"api_type={provider.api_type}")
            if provider.model:
                details.append(f"model={provider.model}")
            if provider.base_url:
                details.append(f"base_url={provider.base_url}")
            suffix = f" — {'; '.join(details)}" if details else ""
            lines.append(f"{marker} {name}{suffix}")
        lines.append("Use /model use <name> to switch provider.")
        return "\n".join(lines)

    def use_model_provider(self, name: str) -> tuple[bool, str]:
        """Switch the active LLM provider profile and persist it to config.toml."""
        provider_name = name.strip()
        if not provider_name:
            return False, "Usage: /model use <name>"

        cfg = load_config()
        if cfg.chat is None:
            return False, "No chat configuration found."
        provider = cfg.chat.providers.get(provider_name)
        if provider is None:
            available = ", ".join(sorted(cfg.chat.providers)) or "none"
            return False, f"Unknown provider: {provider_name}. Available: {available}"

        cfg.chat.active_provider = provider_name
        cfg.chat.base_url = provider.base_url
        cfg.chat.model = provider.model
        cfg.chat.api_key = provider.api_key
        cfg.chat.api_type = provider.api_type
        cfg.chat.model_config = provider.model_config
        path = save_config(cfg)
        try:
            path.chmod(0o600)
        except OSError:
            logger.warning("Failed to set config file permissions.")
        model = provider.model or "unset"
        api_type = provider.api_type or "openai"
        return True, f"Switched provider to {provider_name} (api_type={api_type}, model={model})."

    async def persist_config(self) -> None:
        cfg = load_config()
        telegram_cfg = self._config
        if os.environ.get("ANKA_TELEGRAM_BOT_TOKEN"):
            telegram_cfg = replace(telegram_cfg, bot_token=None)
        cfg.telegram = telegram_cfg
        path = save_config(cfg)
        try:
            path.chmod(0o600)
        except OSError:
            logger.warning("Failed to set config file permissions.")

    async def start_polling(self) -> None:
        self._register_telegram_send_tool()
        self._register_telegram_schedule_tool()
        self._register_notifications()
        await self._start_skill_watcher()
        if self._config.assistant_mode:
            await self._start_assistant_scheduler()
        await self._start_prompt_scheduler()
        self._start_memory_dream_loop()
        await self._run_polling_loop()

    async def start_webhook(self, url: str, listen: str = "0.0.0.0", port: int = 8443) -> None:
        self._register_telegram_send_tool()
        self._register_telegram_schedule_tool()
        self._register_notifications()
        await self._start_skill_watcher()
        if self._config.assistant_mode:
            await self._start_assistant_scheduler()
        await self._start_prompt_scheduler()
        self._start_memory_dream_loop()
        await self._run_webhook_loop(url, listen=listen, port=port)

    async def stop(self) -> None:
        await self._stop_memory_dream_loop()
        if self._prompt_scheduler:
            await self._prompt_scheduler.stop()
            self._prompt_scheduler = None
        if self._scheduler:
            await self._scheduler.stop()
            self._scheduler = None
        if self._skill_watcher:
            await self._skill_watcher.stop()
            self._skill_watcher = None
        await self._stop_all_typing()
        await self._session_manager.close_all()
        if self._stop_event:
            self._stop_event.set()
        elif self._application:
            try:
                await self._application.stop()
                await self._application.shutdown()
            except Exception:
                logger.exception("Failed to stop Telegram application cleanly.")
        self._unregister_notifications()
        self._unregister_telegram_schedule_tool()
        self._unregister_telegram_send_tool()

    async def cancel_session(self, chat_id: int) -> CancellationResult:
        """Cancel and await all current Session turns."""
        async with self._get_session_boundary_lock(chat_id):
            session = self._session_manager.get_current_session(chat_id)
            if session is None:
                return CancellationResult(False, 0)
            result = await session.agent.cancel(clear_queue=True)
            if not isinstance(result, CancellationResult):
                raise TypeError("Agent.cancel() must return CancellationResult")
            return result

    async def cancel_turn(self, chat_id: int, turn_id: str) -> bool:
        """Cancel one current Session turn by its immutable ID."""
        async with self._get_session_boundary_lock(chat_id):
            session = self._session_manager.get_current_session(chat_id)
            if session is None:
                return False
            handle = session.agent.get_turn(turn_id)
            if handle is None:
                return False
            if handle.status == TurnStatus.CANCELLED:
                return True
            if handle.done:
                return False
            return bool(await session.agent.cancel_turn(turn_id))

    def _get_session_boundary_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._session_boundary_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_boundary_locks[chat_id] = lock
        return lock

    async def create_new_session(self, chat_id: int) -> Any:
        """Invalidate and drain old work before publishing a new Session."""
        async with self._get_session_boundary_lock(chat_id):
            old_session = self._session_manager.get_current_session(chat_id)
            if old_session:
                await self._session_manager.abandon_current_session(chat_id)
                snapshot = list(old_session.agent.conversation_history)
                await self.flush_session_memory(
                    chat_id,
                    old_session,
                    conversation_snapshot=snapshot,
                )

            session = self._session_manager.create_session(chat_id)
            self._bind_session_memory(chat_id, session)
            return session

    async def switch_session(self, chat_id: int, session_id: str) -> bool:
        """Drain the current Session before switching the delivery boundary."""
        async with self._get_session_boundary_lock(chat_id):
            return await self._session_manager.switch_session(chat_id, session_id)

    async def reset_session(self, chat_id: int) -> Any:
        """Reset the current Session inside the chat lifecycle boundary."""
        async with self._get_session_boundary_lock(chat_id):
            session = await self._session_manager.get_or_create_session(chat_id)
            await session.agent.reset_session()
            return session

    def memory_project_root(self, chat_id: int) -> Path:
        """Return the isolated project-memory root for a Telegram chat."""
        # TODO: Before considering group support production-ready, propagate
        # sender identity into canonical turns and isolate forum topics by
        # (chat_id, message_thread_id) for sessions, history, and memory.
        return CONFIG_DIR / "telegram" / f"chat_{chat_id}"

    def _bind_session_memory(self, chat_id: int, session: Any) -> None:
        self._bind_agent_memory_context(chat_id, session.agent)

    def _bind_agent_memory_context(self, chat_id: int, agent: Any) -> None:
        context = getattr(agent, "execution_context", None)
        if isinstance(context, dict):
            context["memory_project_root"] = str(self.memory_project_root(chat_id))
            context["source"] = "telegram"
            context["telegram_chat_id"] = str(chat_id)

    def create_scheduled_prompt(
        self,
        chat_id: int,
        schedule: str,
        prompt: str,
        *,
        name: str | None = None,
        notify: bool = True,
        timeout: int = 900,
    ) -> tuple[bool, str]:
        """Create a persistent scheduled prompt for a Telegram chat."""
        try:
            job = self._schedule_store.create_job(
                chat_id=chat_id,
                schedule=schedule,
                prompt=prompt,
                name=name,
                notify=notify,
                timeout=timeout,
            )
        except ValueError as exc:
            return False, str(exc)
        return True, f"Created scheduled prompt {job.id}: {job.schedule}"

    def create_scheduled_blueprint(
        self,
        chat_id: int,
        blueprint: str,
        schedule: str,
        *,
        name: str | None = None,
        notify: bool = True,
        timeout: int = 900,
    ) -> tuple[bool, str]:
        """Create a persistent scheduled prompt from a built-in blueprint."""
        try:
            job = self._schedule_store.create_blueprint_job(
                chat_id=chat_id,
                blueprint=blueprint,
                schedule=schedule,
                name=name,
                notify=notify,
                timeout=timeout,
            )
        except ValueError as exc:
            return False, str(exc)
        return True, f"Created scheduled blueprint {job.blueprint} as {job.id}: {job.schedule}"

    def list_scheduled_prompts(self, chat_id: int) -> list[TelegramScheduledPrompt]:
        """List persistent scheduled prompts for a Telegram chat."""
        return self._schedule_store.list_jobs(chat_id)

    def delete_scheduled_prompt(self, chat_id: int, job_id: str) -> tuple[bool, str]:
        """Delete one persistent scheduled prompt for a Telegram chat."""
        if self._schedule_store.delete_job(chat_id, job_id):
            return True, f"Deleted scheduled prompt {job_id}."
        return False, f"Scheduled prompt not found: {job_id}"

    def list_schedule_blueprints(self) -> str:
        """Return a user-facing list of built-in schedule blueprints."""
        lines = ["Schedule blueprints:"]
        for name, blueprint in sorted(SCHEDULE_BLUEPRINTS.items()):
            lines.append(f"- {name}: {blueprint.description}")
        return "\n".join(lines)

    async def flush_session_memory(
        self,
        chat_id: int,
        session: Any,
        *,
        conversation_snapshot: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Flush durable memory for a Telegram session before it is replaced."""
        self._bind_session_memory(chat_id, session)
        flush = getattr(session.agent, "flush_memory", None)
        if not callable(flush):
            return False
        try:
            return bool(
                await asyncio.wait_for(
                    flush(
                        work_dir=self._work_dir,
                        conversation_snapshot=conversation_snapshot,
                    ),
                    timeout=60,
                )
            )
        except Exception:
            logger.exception("Telegram memory flush failed.")
            return False

    def _start_memory_dream_loop(self) -> None:
        if self._memory_dream_task and not self._memory_dream_task.done():
            return
        self._memory_dream_task = asyncio.create_task(self._memory_dream_loop())

    async def _stop_memory_dream_loop(self) -> None:
        task = self._memory_dream_task
        if not task:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._memory_dream_task = None

    async def _memory_dream_loop(self) -> None:
        while True:
            await asyncio.sleep(self._memory_dream_interval_seconds)
            await self._run_memory_dream_once()

    async def _run_memory_dream_once(self) -> None:
        for chat_id in await self._session_manager.list_chat_ids():
            project_root = self.memory_project_root(chat_id)
            try:
                result = await asyncio.to_thread(MemoryDreamer(project_root).run_once)
                logger.debug(
                    "Telegram memory dream chat=%s ran=%s updated=%s reason=%s",
                    chat_id,
                    result.ran,
                    result.updated,
                    result.reason,
                )
            except Exception:
                logger.exception("Telegram memory dream failed for chat %s.", chat_id)

    def create_pairing_request(self, user_id: int, chat_id: int, username: str | None = None) -> tuple[str, bool]:
        self._prune_pairing_requests()
        for request in self._pairing_requests.values():
            if request.user_id == user_id:
                request.chat_id = chat_id
                request.username = username
                return request.code, False

        if len(self._pairing_requests) >= self._config.pairing.max_pending:
            oldest = min(self._pairing_requests.values(), key=lambda item: item.created_at)
            self._pairing_requests.pop(oldest.code, None)

        alphabet = string.ascii_uppercase + string.digits
        code = ""
        while not code or code in self._pairing_requests:
            code = "".join(secrets.choice(alphabet) for _ in range(8))

        self._pairing_requests[code] = PairingRequest(
            code=code,
            user_id=user_id,
            chat_id=chat_id,
            username=username,
            created_at=datetime.now(),
        )
        return code, True

    def approve_pairing_code(self, code: str) -> tuple[bool, str]:
        self._prune_pairing_requests()
        normalized = code.strip().upper()
        if not normalized:
            return False, "Pairing code is required."
        request = self._pairing_requests.pop(normalized, None)
        if request is None:
            return False, "Pairing code not found or expired."
        self.add_allowed_user(request.user_id)
        return True, f"Approved Telegram user {request.user_id}."

    def list_pairing_requests(self) -> list[PairingRequest]:
        self._prune_pairing_requests()
        return sorted(self._pairing_requests.values(), key=lambda req: req.created_at)

    def _prune_pairing_requests(self) -> None:
        ttl = max(60, int(self._config.pairing.code_ttl_seconds))
        cutoff = datetime.now() - timedelta(seconds=ttl)
        for code, request in list(self._pairing_requests.items()):
            if request.created_at < cutoff:
                self._pairing_requests.pop(code, None)

    def _is_queue_full(self, session: Any) -> bool:
        max_queue_size = self._config.max_queue_size
        if max_queue_size <= 0:
            return False
        return bool(session.agent.queued_count() >= max_queue_size)

    async def handle_prompt(self, chat_id: int, user_id: int, text: str) -> None:
        async with self._get_session_boundary_lock(chat_id):
            session = await self._session_manager.get_or_create_session(chat_id)
            session.last_used = datetime.now()
            queued_message = TelegramQueuedMessage(chat_id=chat_id, user_id=user_id, text=text)
            await self._enqueue_message(session, queued_message)

    async def handle_media(
        self, chat_id: int, user_id: int, text: str, message_type: str, metadata: dict[str, Any] | None
    ) -> None:
        async with self._get_session_boundary_lock(chat_id):
            session = await self._session_manager.get_or_create_session(chat_id)
            session.last_used = datetime.now()
            queued_message = TelegramQueuedMessage(
                chat_id=chat_id, user_id=user_id, text=text, message_type=message_type, metadata=metadata
            )
            await self._enqueue_message(session, queued_message)

    async def _enqueue_message(self, session: Any, message: TelegramQueuedMessage) -> None:
        queue_lock = self._get_session_queue_lock(session)
        async with queue_lock:
            active = session.agent.is_busy()
            if active and self._is_queue_full(session):
                await self.send_text(message.chat_id, "Session queue is full. Please try again later.")
                return

            status_text = (
                f"Session busy. Queued at position {session.agent.queued_count() + 1}."
                if active
                else "Started processing your request..."
            )
            self._bind_session_memory(message.chat_id, session)
            streaming = self._config.streaming.streaming_enabled
            # Agent.submit routes through the shared ApplicationSessionService.
            handle = await session.agent.submit(
                message.text,
                work_dir=self._work_dir,
                stream=streaming,
                show_progress=False,
            )
            try:
                status_message = await self.send_text(
                    message.chat_id,
                    status_text,
                    reply_markup=self._stop_task_markup(handle.id),
                )
            except BaseException:
                await asyncio.shield(session.agent.cancel_turn(handle.id))
                raise
            message.status_message_id = self._get_telegram_message_id(status_message)
            task = asyncio.create_task(self._process_message(session, message, handle=handle))
            session.delivery_tasks.add(task)
            task.add_done_callback(session.delivery_tasks.discard)

    @staticmethod
    def _get_session_queue_lock(session: Any) -> asyncio.Lock:
        lock = getattr(session, "queue_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            session.queue_lock = lock
        return lock

    @staticmethod
    def _session_worker_active(session: Any) -> bool:
        return bool(session.agent.is_busy())

    @staticmethod
    def _get_telegram_message_id(message: Any) -> int | None:
        message_id = getattr(message, "message_id", None)
        return int(message_id) if message_id is not None else None

    # Wall-clock guard for Bot API calls. A silently-dead keep-alive
    # connection can otherwise hang an API call forever with no error surfacing.
    BOT_API_TIMEOUT_SECONDS: ClassVar[float] = 60.0
    BOT_API_RETRY_DELAY_SECONDS: ClassVar[float] = 2.0

    async def _bot_api_call(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        *,
        retry: bool = False,
    ) -> Any:
        """Run a Bot API call with a timeout and optionally retry transient failures once."""
        last_exc: Exception | None = None
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(self.BOT_API_RETRY_DELAY_SECONDS)
            try:
                return await asyncio.wait_for(coro_factory(), timeout=self.BOT_API_TIMEOUT_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Bot API call failed (attempt %d): %s: %s",
                    attempt + 1,
                    type(exc).__name__,
                    str(exc)[:120],
                )
                if attempt + 1 >= attempts or not self._is_transient_bot_api_error(exc):
                    raise
        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _is_transient_bot_api_error(exc: Exception) -> bool:
        """Return whether retrying a Bot API failure is safe and useful."""
        return isinstance(exc, TimeoutError) or (
            isinstance(exc, _TELEGRAM_NETWORK_ERROR_TYPES) and not isinstance(exc, _TELEGRAM_BAD_REQUEST_TYPES)
        )

    @staticmethod
    def _is_message_not_modified_error(exc: Exception) -> bool:
        """Return whether Telegram reports that an idempotent edit already took effect."""
        return isinstance(exc, _TELEGRAM_BAD_REQUEST_TYPES) and "message is not modified" in str(exc).lower()

    def _register_error_handler(self) -> None:
        """Register a PTB error handler so failures surface instead of
        disappearing with "No error handlers are registered"."""
        app = self._application

        async def _on_error(update: object, context: Any) -> None:
            error = getattr(context, "error", None)
            logger.error("Unhandled Telegram update error", exc_info=error)
            effective_chat = None
            effective_message = getattr(update, "effective_message", None)
            if effective_message is not None:
                effective_chat = getattr(effective_message, "chat_id", None)
            if effective_chat is None:
                callback_query = getattr(update, "callback_query", None)
                if callback_query is not None:
                    effective_chat = getattr(getattr(callback_query, "message", None), "chat_id", None)
            if effective_chat is None:
                return
            try:
                await self._bot_api_call(
                    lambda: context.bot.send_message(
                        chat_id=effective_chat,
                        text="Internal error while processing the update. Please retry.",
                    )
                )
            except Exception:
                logger.warning("Failed to deliver error notice to chat %s", effective_chat)

        try:
            app.add_error_handler(_on_error)
        except Exception:
            logger.debug("Failed to register PTB error handler", exc_info=True)

    async def send_text(self, chat_id: int, text: str, *, reply_markup: Any = None) -> Any:
        if _utf16_len(text) > self._formatter.max_length:
            last_message: Any = None
            text_parts = _split_plain_text(text, self._formatter.max_length)
            for part in text_parts:
                kwargs: dict[str, Any] = {"chat_id": chat_id, "text": part}
                if reply_markup is not None and part is text_parts[-1]:
                    kwargs["reply_markup"] = reply_markup
                last_message = await self._bot_api_call(partial(self._application.bot.send_message, **kwargs))
            return last_message
        kwargs = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            kwargs["reply_markup"] = reply_markup
        return await self._bot_api_call(lambda: self._application.bot.send_message(**kwargs))

    async def send_markdown(self, chat_id: int, text: str) -> Any:
        return await self._bot_api_call(
            lambda: self._application.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True,
            )
        )

    async def _edit_text(self, chat_id: int, message_id: int, text: str, *, markdown: bool = False) -> bool:
        try:
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": None,
            }
            if markdown:
                kwargs["parse_mode"] = "MarkdownV2"
                kwargs["disable_web_page_preview"] = True
            await self._bot_api_call(
                lambda: self._application.bot.edit_message_text(**kwargs),
                retry=True,
            )
            return True
        except Exception as exc:
            if self._is_message_not_modified_error(exc):
                return True
            logger.debug("Failed to edit Telegram status message %s/%s: %s", chat_id, message_id, exc)
            return False

    async def send_notification(self, text: str) -> None:
        if self._loop and asyncio.get_running_loop() is not self._loop:
            future = asyncio.run_coroutine_threadsafe(
                self._send_notification_inner(text),
                self._loop,
            )
            await asyncio.wrap_future(future)
            return
        await self._send_notification_inner(text)

    def tail_logs(self, lines: int) -> str:
        log_path = Path.home() / ".config" / CONFIG_DIR_NAME / "logs" / "ankaloop.log"
        if not log_path.exists():
            return "No log file found."
        content = log_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(content[-lines:]) if content else "No log entries."

    async def _send_notification_inner(self, text: str) -> None:
        for user_id in sorted(self._auth.allowed_users):
            await self.send_text(user_id, text)

    async def _process_message(
        self,
        session: Any,
        message: TelegramQueuedMessage,
        *,
        handle: Any | None = None,
    ) -> None:
        token = TelegramDeliveryToken(
            chat_id=message.chat_id,
            session_id=session.session_id,
            generation=getattr(session, "generation", 0),
            turn_id=getattr(handle, "id", f"direct-{id(message)}"),
        )
        status_message_id = message.status_message_id
        self._bind_session_memory(message.chat_id, session)
        streaming_cfg = self._config.streaming if self._config.streaming.streaming_enabled else None
        streamer: _StreamingDeliveryManager | None = None
        if streaming_cfg and handle is not None and status_message_id is not None:
            streamer = _StreamingDeliveryManager(
                bot=self,
                chat_id=message.chat_id,
                status_message_id=status_message_id,
                formatter=self._formatter,
                cfg=streaming_cfg,
                is_current=lambda: self._is_delivery_current(token, session),
                stop_markup=self._stop_task_markup(handle.id),
            )
            session.agent.add_event_callback(streamer.on_chunk)
        async with self._typing_session(message.chat_id):
            try:
                if handle is None:
                    response = await session.agent.run(
                        user_input=message.text,
                        work_dir=self._work_dir,
                        stream=False,
                        show_progress=False,
                        queue_if_busy=False,
                    )
                else:
                    if streamer is not None:
                        response = await self._wait_with_streaming(handle, streamer)
                    else:
                        response = await handle.wait()
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if streamer is not None:
                    session.agent.remove_event_callback(streamer.on_chunk)
                await self._deliver_status_or_text_if_current(
                    token,
                    session,
                    status_message_id,
                    "Request cancelled.",
                )
                return
            except BusyError:
                if streamer is not None:
                    session.agent.remove_event_callback(streamer.on_chunk)
                await self._deliver_status_or_text_if_current(
                    token,
                    session,
                    status_message_id,
                    "Session busy. Please try again.",
                )
                return
            except Exception as exc:
                if streamer is not None:
                    session.agent.remove_event_callback(streamer.on_chunk)
                logger.exception("Failed to process Telegram message.")
                await self._deliver_status_or_markdown_if_current(
                    token,
                    session,
                    status_message_id,
                    self._formatter.format_error(str(exc)),
                )
                return

            if streamer is not None:
                session.agent.remove_event_callback(streamer.on_chunk)

            response_text = response or ""
            if not self._is_delivery_current(token, session):
                return
            if response_text:
                if streamer is not None:
                    await streamer.finalize(response_text)
                else:
                    chunks = self._formatter.format_response(response_text)
                    await self._deliver_response_chunks(token, session, status_message_id, chunks)
            elif status_message_id is not None:
                await self._edit_if_current(token, session, status_message_id, "Done.")

    async def _wait_with_streaming(self, handle: TurnHandle, streamer: _StreamingDeliveryManager) -> str:
        """Wait for a turn while periodically flushing streaming chunks to Telegram."""
        flush_interval = max(0.3, self._config.streaming.streaming_interval_seconds)
        while not handle.done:
            try:
                await asyncio.wait_for(asyncio.shield(handle.wait()), timeout=flush_interval)
                break
            except TimeoutError:
                await streamer.maybe_flush()
            except TurnCancelledError:
                raise
            except asyncio.CancelledError:
                raise
        await streamer.maybe_flush()
        return await handle.wait()

    def _is_delivery_current(self, token: TelegramDeliveryToken, session: Any) -> bool:
        if session.session_id != token.session_id or getattr(session, "generation", 0) != token.generation:
            return False
        current_id = self._session_manager.get_current_session_id(token.chat_id)
        return current_id is None or current_id == token.session_id

    async def _deliver_status_or_text_if_current(
        self,
        token: TelegramDeliveryToken,
        session: Any,
        message_id: int | None,
        text: str,
    ) -> None:
        async with self._get_session_boundary_lock(token.chat_id):
            if self._is_delivery_current(token, session):
                await self._deliver_status_or_text(token.chat_id, message_id, text)

    async def _deliver_status_or_markdown_if_current(
        self,
        token: TelegramDeliveryToken,
        session: Any,
        message_id: int | None,
        text: str,
    ) -> None:
        async with self._get_session_boundary_lock(token.chat_id):
            if self._is_delivery_current(token, session):
                await self._deliver_status_or_markdown(token.chat_id, message_id, text)

    async def _edit_if_current(
        self,
        token: TelegramDeliveryToken,
        session: Any,
        message_id: int,
        text: str,
    ) -> None:
        async with self._get_session_boundary_lock(token.chat_id):
            if self._is_delivery_current(token, session):
                await self._edit_text(token.chat_id, message_id, text)

    async def _deliver_status_or_text(self, chat_id: int, message_id: int | None, text: str) -> None:
        if message_id is not None and await self._edit_text(chat_id, message_id, text):
            return
        await self.send_text(chat_id, text)

    async def _deliver_status_or_markdown(self, chat_id: int, message_id: int | None, text: str) -> None:
        if message_id is not None and await self._edit_text(chat_id, message_id, text, markdown=True):
            return
        await self.send_markdown(chat_id, text)

    async def _deliver_response_chunks(
        self,
        token: TelegramDeliveryToken,
        session: Any,
        message_id: int | None,
        chunks: list[str],
    ) -> None:
        if not chunks:
            return

        start_idx = 0
        async with self._get_session_boundary_lock(token.chat_id):
            if not self._is_delivery_current(token, session):
                return
            if message_id is not None and await self._edit_text(
                token.chat_id,
                message_id,
                chunks[0],
                markdown=True,
            ):
                start_idx = 1

        for chunk in chunks[start_idx:]:
            async with self._get_session_boundary_lock(token.chat_id):
                if not self._is_delivery_current(token, session):
                    return
                await self.send_markdown(token.chat_id, chunk)

    @staticmethod
    def _stop_task_markup(turn_id: str) -> Any:
        if InlineKeyboardButton is None or InlineKeyboardMarkup is None:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton("Stop task", callback_data=f"cancel:{turn_id}")]])

    def _build_application(self) -> Application:
        application = ApplicationBuilder().token(self._token).post_init(self._post_init).build()
        application.add_handler(CommandHandler("start", self._handlers.handle_start))
        application.add_handler(CommandHandler("help", self._handlers.handle_help))
        application.add_handler(CommandHandler("status", self._handlers.handle_status))
        application.add_handler(CommandHandler("session", self._handlers.handle_session))
        application.add_handler(CommandHandler("new", self._handlers.handle_new))
        application.add_handler(CommandHandler("cancel", self._handlers.handle_cancel))
        application.add_handler(CommandHandler("ask", self._handlers.handle_ask))
        application.add_handler(CommandHandler("skills", self._handlers.handle_skills))
        application.add_handler(CommandHandler("activate", self._handlers.handle_activate))
        application.add_handler(CommandHandler("memory", self._handlers.handle_memory))
        application.add_handler(CommandHandler("models", self._handlers.handle_models))
        application.add_handler(CommandHandler("model", self._handlers.handle_model))
        application.add_handler(CommandHandler("config", self._handlers.handle_config))
        application.add_handler(CommandHandler("users", self._handlers.handle_users))
        application.add_handler(CommandHandler("pair", self._handlers.handle_pair))
        application.add_handler(CommandHandler("logs", self._handlers.handle_logs))
        application.add_handler(CommandHandler("shutdown", self._handlers.handle_shutdown))
        application.add_handler(
            CallbackQueryHandler(
                self._handlers.handle_cancel_callback,
                pattern=r"^cancel:turn-[A-Za-z0-9]+$",
            )
        )
        # Handle /skill:<name> commands (not supported by CommandHandler due to colon)
        application.add_handler(
            MessageHandler(
                filters.TEXT & filters.Regex(r"^/skill:[a-zA-Z0-9_-]+"),
                self._handlers.handle_skill,
            )
        )
        application.add_handler(
            MessageHandler(
                (
                    filters.TEXT
                    | filters.PHOTO
                    | filters.AUDIO
                    | filters.VIDEO
                    | filters.VOICE
                    | filters.Document.ALL
                    | filters.Sticker.ALL
                    | filters.VIDEO_NOTE
                )
                & ~filters.COMMAND,
                self._handlers.handle_message,
            )
        )
        application.add_handler(MessageHandler(filters.COMMAND, self._handlers.handle_unknown))
        return application

    def _get_user_bot_commands(self) -> list[BotCommand]:
        """Return user-facing commands to register in Telegram's bot menu.

        Admin-only commands (config, users, pair, logs, shutdown) and helper
        commands better discovered via /help (cancel, ask, activate, memory) are
        intentionally omitted; /help already lists them.
        """
        return [
            BotCommand("start", "Initialize the bot"),
            BotCommand("help", "Show available commands"),
            BotCommand("status", "Show agent and session status"),
            BotCommand("new", "Start a new conversation session"),
            BotCommand("session", "Manage sessions (new|list|switch)"),
            BotCommand("skills", "List and manage skills"),
            BotCommand("models", "List configured LLM providers"),
        ]

    async def _post_init(self, application: Application) -> None:
        """Register user-facing commands with Telegram's bot menu after init."""
        if BotCommand is None:
            return
        try:
            await application.bot.set_my_commands(self._get_user_bot_commands())
            logger.info("Registered Telegram bot menu commands")
        except Exception:
            logger.warning("Failed to register Telegram bot menu commands", exc_info=True)

    async def _run_polling_loop(self) -> None:
        updater = getattr(self._application, "updater", None)
        if updater is None or not hasattr(updater, "start_polling"):
            await asyncio.to_thread(self._application.run_polling)
            return
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        try:
            await self._application.initialize()
            await self._application.start()
        except AttributeError:
            await asyncio.to_thread(self._application.run_polling)
            return
        self._register_error_handler()
        await updater.start_polling()
        await self._stop_event.wait()
        await updater.stop()
        await self._application.stop()
        await self._application.shutdown()

    async def _run_webhook_loop(self, url: str, listen: str, port: int) -> None:
        updater = getattr(self._application, "updater", None)
        if updater is None or not hasattr(updater, "start_webhook"):
            await asyncio.to_thread(
                self._application.run_webhook,
                listen=listen,
                port=port,
                webhook_url=url,
            )
            return
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        try:
            await self._application.initialize()
            await self._application.start()
        except AttributeError:
            await asyncio.to_thread(
                self._application.run_webhook,
                listen=listen,
                port=port,
                webhook_url=url,
            )
            return
        self._register_error_handler()
        await updater.start_webhook(listen=listen, port=port, webhook_url=url)
        await self._stop_event.wait()
        await updater.stop()
        await self._application.stop()
        await self._application.shutdown()

    def _register_telegram_send_tool(self) -> None:
        """Register the TelegramSendTool with the global tool registry."""
        from ..tools import get_tool_registry
        from .tools import TelegramSendTool

        registry = get_tool_registry()
        if registry.get_tool("telegram_send") is None:
            tool = TelegramSendTool(bot_token=self._token)
            registry.register(tool)
            logger.info("Registered telegram_send tool")

    def _unregister_telegram_send_tool(self) -> None:
        """Unregister the TelegramSendTool from the global tool registry."""
        from ..tools import get_tool_registry

        registry = get_tool_registry()
        if registry.get_tool("telegram_send") is not None:
            registry.unregister("telegram_send")
            logger.info("Unregistered telegram_send tool")

    def _register_telegram_schedule_tool(self) -> None:
        """Register the TelegramScheduleTool with the global tool registry."""
        from ..tools import get_tool_registry
        from .tools import TelegramScheduleTool

        registry = get_tool_registry()
        if registry.get_tool("telegram_schedule") is None:
            registry.register(TelegramScheduleTool(self._schedule_store))
            logger.info("Registered telegram_schedule tool")

    def _unregister_telegram_schedule_tool(self) -> None:
        """Unregister the TelegramScheduleTool from the global tool registry."""
        from ..tools import get_tool_registry

        registry = get_tool_registry()
        if registry.get_tool("telegram_schedule") is not None:
            registry.unregister("telegram_schedule")
            logger.info("Unregistered telegram_schedule tool")

    def _register_notifications(self) -> None:
        if not self._config.notifications:
            return
        bus = get_event_bus()
        if self._config.notifications.task_completions:
            self._notification_handler_ids.append(bus.subscribe(EventType.TASK_COMPLETED, self._on_task_completed))
        if self._config.notifications.error_alerts:
            self._notification_handler_ids.append(bus.subscribe(EventType.AGENT_ERROR, self._on_agent_error))
            self._notification_handler_ids.append(bus.subscribe(EventType.TOOL_ERROR, self._on_tool_error))

    def _unregister_notifications(self) -> None:
        bus = get_event_bus()
        for handler_id in self._notification_handler_ids:
            bus.unsubscribe(handler_id)
        self._notification_handler_ids = []

    async def _on_task_completed(self, event: Any) -> None:
        description = event.data.get("description", "")
        duration = event.data.get("duration_ms", "")
        result = event.data.get("result", "")
        message = f"Task completed.\nDescription: {description}\nDuration: {duration}\nResult: {str(result)[:500]}"
        await self.send_notification(message)

    async def _on_agent_error(self, event: Any) -> None:
        error = event.data.get("error", "")
        session_id = event.session_id or "unknown"
        message = f"Agent error.\nSession: {session_id}\nError: {str(error)[:500]}"
        await self.send_notification(message)

    async def _on_tool_error(self, event: Any) -> None:
        error = event.data.get("error", "")
        tool = event.data.get("tool_name", "")
        message = f"Tool error.\nTool: {tool}\nError: {str(error)[:500]}"
        await self.send_notification(message)

    def _default_agent_factory(self, session_id: str) -> Agent:
        cfg = load_config()
        if cfg.chat and cfg.chat.default_agent:
            registry = get_agent_registry()
            if cfg.chat.default_agent in registry.list_agents():
                return create_agent_by_name(cfg.chat.default_agent, session_id=session_id)
        spec = get_default_agent_spec()
        return Agent(spec, session_id=session_id)

    async def _start_skill_watcher(self) -> None:
        """Start skill hot-reload watcher."""
        from ..skills import SkillWatcher, get_skill_manager

        mgr = get_skill_manager()
        self._skill_watcher = SkillWatcher(mgr)
        await self._skill_watcher.start(project_root=self._work_dir)
        logger.info("Telegram: SkillWatcher started for hot reload")

    async def _start_assistant_scheduler(self) -> None:
        """Start the assistant scheduler for cron-triggered skills."""
        from ..skills import get_skill_manager
        from .scheduler import AssistantScheduler

        mgr = get_skill_manager()
        self._scheduler = AssistantScheduler(
            skill_manager=mgr,
            agent_factory=self._session_manager._agent_factory,
            send_notification=self.send_notification,
            work_dir=self._work_dir,
        )
        await self._scheduler.start()
        logger.info("Telegram: AssistantScheduler started")

    async def _start_prompt_scheduler(self) -> None:
        """Start the internal scheduler for persistent Telegram prompt jobs."""
        from .scheduler import TelegramPromptScheduler

        if self._prompt_scheduler:
            return
        self._prompt_scheduler = TelegramPromptScheduler(
            store=self._schedule_store,
            agent_factory=self._session_manager._agent_factory,
            send_chat=self.send_text,
            bind_agent_context=self._bind_agent_memory_context,
            work_dir=self._work_dir,
        )
        await self._prompt_scheduler.start()
        logger.info("Telegram: PromptScheduler started")

    async def _start_typing(self, chat_id: int) -> None:
        if not self._config.typing_indicator:
            return
        async with self._typing_lock:
            count = self._typing_counts.get(chat_id, 0)
            self._typing_counts[chat_id] = count + 1
            task = self._typing_tasks.get(chat_id)
            if task is not None and not task.done():
                return
            try:
                await self._send_typing_action(chat_id)
            except asyncio.CancelledError:
                if count:
                    self._typing_counts[chat_id] = count
                else:
                    self._typing_counts.pop(chat_id, None)
                raise
            self._typing_tasks[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    async def _stop_typing(self, chat_id: int) -> None:
        async with self._typing_lock:
            count = self._typing_counts.get(chat_id, 0)
            if count > 1:
                self._typing_counts[chat_id] = count - 1
                return
            self._typing_counts.pop(chat_id, None)
            task = self._typing_tasks.pop(chat_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _stop_all_typing(self) -> None:
        async with self._typing_lock:
            tasks = list(self._typing_tasks.values())
            self._typing_tasks.clear()
            self._typing_counts.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @contextlib.asynccontextmanager
    async def _typing_session(self, chat_id: int):
        await self._start_typing(chat_id)
        try:
            yield
        finally:
            await self._stop_typing(chat_id)

    async def _send_typing_action(self, chat_id: int) -> bool:
        try:
            await self._bot_api_call(
                lambda: self._application.bot.send_chat_action(chat_id=chat_id, action="typing"),
                retry=True,
            )
            return True
        except Exception as exc:
            logger.warning("Typing action failed for chat %s: %s", chat_id, exc)
            return False

    async def _typing_loop(self, chat_id: int) -> None:
        interval = max(1, int(self._config.typing_interval_seconds))
        try:
            while True:
                await asyncio.sleep(interval)
                await self._send_typing_action(chat_id)
        except asyncio.CancelledError:
            return

"""Telegram interface — the primary way to reach JARVIS from anywhere.

Security: locked to the numeric user IDs in TELEGRAM_ALLOWED_USER_IDS.
Messages from anyone else are ignored (no reply — don't advertise the bot
exists) and audited. Runs inside the hub process; imports of the telegram
library are deferred so an unconfigured bot costs zero RAM.
"""

from __future__ import annotations

from jarvis.cognition.agent import ChatAgent
from jarvis.core.audit import AuditLog
from jarvis.core.config import Config
from jarvis.core.killswitch import KillSwitch
from jarvis.core.logging import get_logger

log = get_logger(__name__)

TG_MAX = 4096


class TelegramInterface:
    def __init__(self, cfg: Config, agent: ChatAgent, killswitch: KillSwitch,
                 audit: AuditLog, node_status: dict):
        self.cfg = cfg
        self.agent = agent
        self.killswitch = killswitch
        self.audit = audit
        self.node_status = node_status
        self.token = cfg.secrets.telegram_bot_token
        self.allowed_ids = {
            int(x) for x in cfg.secrets.telegram_allowed_user_ids.split(",") if x.strip()
        }
        self._app = None

    @property
    def configured(self) -> bool:
        return bool(self.token and self.allowed_ids)

    def _authorized(self, update) -> bool:
        user = update.effective_user
        if user and user.id in self.allowed_ids:
            return True
        self.audit.record(
            "telegram", "auth.rejected",
            {"user_id": user.id if user else None,
             "username": getattr(user, "username", None)},
            "denied",
        )
        log.warning("telegram_unauthorized", user_id=user.id if user else None)
        return False  # silence: unauthorized senders get nothing back

    # -- handlers -------------------------------------------------------------

    async def _cmd_start(self, update, context) -> None:
        if not self._authorized(update):
            return
        await update.message.reply_text(
            "JARVIS online. Speak freely — everything stays on your hardware "
            "unless routing policy says otherwise."
        )

    async def _cmd_status(self, update, context) -> None:
        if not self._authorized(update):
            return
        ks = self.killswitch.state()["state"]
        nodes = ", ".join(
            f"{n}: {s.get('status', '?')}" for n, s in self.node_status.items()
        ) or "no nodes probed yet"
        await update.message.reply_text(f"killswitch={ks} | {nodes}")

    async def _cmd_pause(self, update, context) -> None:
        if not self._authorized(update):
            return
        self.killswitch.pause("telegram /pause")
        self.audit.record("telegram", "control.pause", {"via": "telegram"})
        await update.message.reply_text("Paused. Nothing autonomous will run until /resume.")

    async def _cmd_resume(self, update, context) -> None:
        if not self._authorized(update):
            return
        self.killswitch.resume()
        self.audit.record("telegram", "control.resume", {"via": "telegram"})
        await update.message.reply_text("Resumed.")

    async def _on_message(self, update, context) -> None:
        if not self._authorized(update) or not update.message or not update.message.text:
            return
        chat_id = update.effective_chat.id
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        try:
            result = await self.agent.handle(
                update.message.text, interface="telegram", external_id=str(chat_id)
            )
            reply = result["reply"]
        except Exception:
            log.exception("telegram_handle_failed")
            reply = "Something broke on my end handling that. It's been logged."
        for i in range(0, len(reply), TG_MAX):
            await update.message.reply_text(reply[i:i + TG_MAX])

    # -- outbound (reminders, digests) --------------------------------------------

    async def send_to_operator(self, text: str) -> bool:
        """Push a message to the operator (first allowed ID). Used by the
        reminder loop and, from Phase 4, the proactive engine."""
        if not (self._app and self.allowed_ids):
            return False
        chat_id = sorted(self.allowed_ids)[0]
        try:
            for i in range(0, len(text), TG_MAX):
                await self._app.bot.send_message(chat_id=chat_id, text=text[i:i + TG_MAX])
            return True
        except Exception:
            log.exception("telegram_push_failed")
            return False

    # -- lifecycle --------------------------------------------------------------

    async def start(self) -> None:
        if not self.configured:
            log.info("telegram_disabled",
                     reason="TELEGRAM_BOT_TOKEN / TELEGRAM_ALLOWED_USER_IDS not set")
            return
        from telegram.ext import (
            ApplicationBuilder,
            CommandHandler,
            MessageHandler,
            filters,
        )

        self._app = ApplicationBuilder().token(self.token).build()
        self._app.add_handler(CommandHandler("start", self._cmd_start))
        self._app.add_handler(CommandHandler("status", self._cmd_status))
        self._app.add_handler(CommandHandler("pause", self._cmd_pause))
        self._app.add_handler(CommandHandler("resume", self._cmd_resume))
        self._app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )
        await self._app.initialize()
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)
        log.info("telegram_started", allowed_ids=len(self.allowed_ids))

    async def stop(self) -> None:
        if self._app:
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()

"""Polling worker and explicit database management commands."""

import argparse
import asyncio
import json
import logging
import os
import signal
from pathlib import Path

from telegram.ext import Application
from telethon import TelegramClient

from .config import ConfigError, Settings
from .database.repository import Database
from .handlers import Handlers
from .welcome import validate_template


class JsonFormatter(logging.Formatter):
    def format(self, record):
        # Library messages, request URLs, exception strings and traceback locals
        # are deliberately excluded because they can contain credentials.
        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "event": getattr(record, "event", "library_event"),
                "error_type": getattr(record, "error_type", None),
            }
        )


def logging_setup():
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telethon").setLevel(logging.WARNING)


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.db = self.client = self.lock_conn = None
        self.handlers = None

    async def initialize(self, app):
        try:
            self.db = await Database.connect(self.settings)
            self.lock_conn = await self.db.pool.acquire()
            # One worker per database/session. Limits and callback state are process local.
            if not await self.lock_conn.fetchval("SELECT pg_try_advisory_lock(735193023)"):
                raise RuntimeError("Another worker is already using this database.")
            if not await self.db.pool.fetchval("SELECT to_regclass('af_files') IS NOT NULL"):
                raise RuntimeError("Run python bot.py --migrate before starting the worker.")
            validate_template(self.settings.default_welcome)
            session = Path(self.settings.session_path)
            if not await asyncio.to_thread(session.is_file):
                raise RuntimeError("Generate the Telethon session file before starting the worker.")
            if (await asyncio.to_thread(session.stat)).st_mode & 0o077:
                raise RuntimeError("Telethon session permissions must be 0600.")
            self.client = TelegramClient(
                str(session),
                self.settings.api_id,
                self.settings.api_hash,
                flood_sleep_threshold=self.settings.max_flood_wait,
                request_retries=2,
                connection_retries=5,
            )
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError("The Telethon session is not authorized. Generate it interactively.")
            from .source import SourceService

            source = SourceService(self.client, self.db, self.settings)
            await self.client.get_dialogs()
            self.handlers = Handlers(self.settings, self.db, source)
            self.handlers.search_service.cache.ttl = await self.db.setting("cache_ttl", self.settings.cache_ttl)
            self.handlers.install(app)
            app.add_error_handler(self.on_error)
            logging.getLogger(__name__).info("ready", extra={"event": "worker_ready"})
        except BaseException:
            await self.shutdown(app)
            raise

    async def on_error(self, update, context):
        name = type(context.error).__name__
        self.handlers.errors.append(name)
        logging.getLogger(__name__).error("handler_error", extra={"event": "handler_error", "error_type": name})
        try:
            if update and update.callback_query:
                await update.callback_query.answer("Unable to complete the request. Please try later.", show_alert=True)
            elif update and update.effective_message:
                await update.effective_message.reply_text("Unable to complete the request. Please try later.")
        except Exception:
            logging.getLogger(__name__).warning("error_response_failed", extra={"event": "error_response_failed"})

    async def shutdown(self, app):
        try:
            if self.client:
                try:
                    await self.client.disconnect()
                finally:
                    self.client = None
        finally:
            if self.db:
                try:
                    if self.lock_conn:
                        await self.lock_conn.execute("SELECT pg_advisory_unlock(735193023)")
                        await self.db.pool.release(self.lock_conn)
                        self.lock_conn = None
                finally:
                    try:
                        async with asyncio.timeout(15):
                            await self.db.close()
                    except TimeoutError:
                        self.db.pool.terminate()
                    self.db = None


async def database_command(settings, migrate):
    db = await Database.connect(settings)
    try:
        if migrate:
            await db.migrate()
            print("Migrations applied successfully.")
        else:
            if not await db.health():
                raise RuntimeError("Database health check failed.")
            print("Database connection is healthy.")
    finally:
        await db.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Authorized Telegram auto filter worker")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--migrate", action="store_true")
    mode.add_argument("--check-db", action="store_true")
    args = parser.parse_args()
    logging_setup()
    try:
        settings = Settings.load()
        if args.migrate or args.check_db:
            asyncio.run(database_command(settings, args.migrate))
            return
        runtime = Runtime(settings)
        app = (
            Application.builder()
            .token(settings.token)
            .concurrent_updates(32)
            .connection_pool_size(40)
            .connect_timeout(10)
            .read_timeout(35)
            .post_init(runtime.initialize)
            .post_shutdown(runtime.shutdown)
            .build()
        )
        app.run_polling(
            drop_pending_updates=False,
            stop_signals=(signal.SIGINT, signal.SIGTERM),
            allowed_updates=["message", "callback_query", "channel_post"],
        )
    except ConfigError as exc:
        parser.exit(2, f"Configuration error: {exc}\n")
    except Exception as exc:
        # No repr(exc): connection errors can include credential-bearing DSNs.
        logging.getLogger(__name__).error(
            "startup_failed", extra={"event": "startup_failed", "error_type": type(exc).__name__}
        )
        parser.exit(
            1, "Startup failed. Check configuration, database migrations, session permissions, and account access.\n"
        )

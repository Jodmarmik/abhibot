"""Create a sensitive SQLite session locally; never print a session string."""

import asyncio
import getpass
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient


async def generate():
    load_dotenv()
    os.umask(0o077)
    target = await asyncio.to_thread(
        lambda: Path(os.environ.get("TELETHON_SESSION_PATH", "secrets/userbot.session")).expanduser()
    )
    if target.suffix != ".session":
        raise ValueError("TELETHON_SESSION_PATH must end in .session.")
    if target.exists():
        raise ValueError("Session already exists. Choose a new path to avoid overwriting it.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    api_id = int(os.environ.get("API_ID") or getpass.getpass("API ID: "))
    api_hash = os.environ.get("API_HASH") or getpass.getpass("API hash: ")
    client = TelegramClient(str(target), api_id, api_hash)
    try:
        await client.start(
            phone=lambda: getpass.getpass("Telegram phone (with country code): "),
            code_callback=lambda: getpass.getpass("Telegram login code: "),
            password=lambda: getpass.getpass("Two-step verification password: "),
        )
        target.chmod(0o600)
        print("Session saved privately. Keep this file secret and mount it at the configured path.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(generate())
    except Exception as exc:
        raise SystemExit(f"Session generation failed ({type(exc).__name__}). Check input and connectivity.") from None

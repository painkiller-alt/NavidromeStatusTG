import asyncio
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path

import requests
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.types import BusinessConnection

# --- Настройки ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

NAVIDROME_URL = os.getenv("NAVIDROME_URL", "").rstrip("/")
NAVIDROME_USER = os.getenv("NAVIDROME_USER", "")
NAVIDROME_PASSWORD = os.getenv("NAVIDROME_PASSWORD", "")
NAVIDROME_CLIENT = os.getenv("NAVIDROME_CLIENT", "BioBot")
NAVIDROME_API_VERSION = os.getenv("NAVIDROME_API_VERSION", "1.16.1")

POLL_INTERVAL = 2          # секунды между запросами к Navidrome
BIO_MAX_LEN = 70           # лимит Telegram Business bio
EMOJI = "🎵"

STORAGE_FILE = Path(__file__).parent / "connections.json"

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# connection_id -> asyncio.Task
tracker_tasks: dict[str, asyncio.Task] = {}
# connection_id -> BusinessConnection
active_connections: dict[str, BusinessConnection] = {}

_storage_lock = asyncio.Lock()


# ================================================================
# Хранилище подключений (JSON)
# ================================================================
def _load_connections_sync() -> dict[str, BusinessConnection]:
    """Читает connections.json и валидирует записи через BusinessConnection."""
    if not STORAGE_FILE.exists():
        return {}
    try:
        raw = json.loads(STORAGE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.error(f"Не удалось прочитать {STORAGE_FILE}: {e}")
        return {}

    result: dict[str, BusinessConnection] = {}
    for cid, data in raw.items():
        try:
            result[cid] = BusinessConnection.model_validate(data)
        except Exception as e:
            logging.warning(f"Пропускаю битую запись {cid}: {e}")
    return result


def _save_connections_sync(connections: dict[str, BusinessConnection]) -> None:
    """Атомарно пишет connections.json."""
    data = {cid: c.model_dump(mode="json") for cid, c in connections.items()}
    tmp = STORAGE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STORAGE_FILE)


async def save_connections() -> None:
    async with _storage_lock:
        await asyncio.to_thread(_save_connections_sync, active_connections)


# ================================================================
# Navidrome: получение текущего трека
# ================================================================
def _fetch_now_playing_sync() -> str | None:
    if not (NAVIDROME_URL and NAVIDROME_USER and NAVIDROME_PASSWORD):
        return None

    salt = secrets.token_hex(16)
    token = hashlib.md5((NAVIDROME_PASSWORD + salt).encode("utf-8")).hexdigest()

    params = {
        "u": NAVIDROME_USER,
        "t": token,
        "s": salt,
        "v": NAVIDROME_API_VERSION,
        "c": NAVIDROME_CLIENT,
        "f": "json",
    }

    try:
        r = requests.get(
            f"{NAVIDROME_URL}/rest/getNowPlaying",
            params=params,
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logging.error(f"Navidrome request failed: {e}")
        return None

    sub = data.get("subsonic-response", {})
    if sub.get("status") != "ok":
        return None

    entries = sub.get("nowPlaying", {}).get("entry", [])
    if isinstance(entries, dict):
        entries = [entries]
    if not entries:
        return None

    entry = entries[0]
    artist = entry.get("artist") or entry.get("albumArtist") or "Unknown"
    title = entry.get("title") or "Unknown"
    text = f"{EMOJI} {artist} - {title}"

    if len(text) > BIO_MAX_LEN:
        text = text[: BIO_MAX_LEN - 1] + "…"
    return text


async def fetch_now_playing() -> str | None:
    return await asyncio.to_thread(_fetch_now_playing_sync)


# ================================================================
# Фоновая задача: обновление био каждые POLL_INTERVAL сек.
# ================================================================
async def track_loop(connection_id: str):
    last_bio: str | None = None
    logging.info(f"▶️ Tracking started: {connection_id}")

    while True:
        try:
            track = await fetch_now_playing()
            if track and track != last_bio:
                ok = await bot.set_business_account_bio(
                    business_connection_id=connection_id,
                    bio=track,
                )
                if ok:
                    last_bio = track
                    logging.info(f"🎵 Bio updated: {track}")
        except asyncio.CancelledError:
            logging.info(f"⏹ Tracking stopped: {connection_id}")
            raise
        except Exception as e:
            logging.error(f"track_loop error: {e}")

        await asyncio.sleep(POLL_INTERVAL)


def _start_tracking(connection_id: str) -> None:
    old = tracker_tasks.get(connection_id)
    if old and not old.done():
        old.cancel()
    tracker_tasks[connection_id] = asyncio.create_task(track_loop(connection_id))


def _stop_tracking(connection_id: str) -> None:
    task = tracker_tasks.pop(connection_id, None)
    if task and not task.done():
        task.cancel()


# ================================================================
# Бизнес-подключение
# ================================================================
@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    # Отключение
    if not connection.is_enabled:
        _stop_tracking(connection.id)
        active_connections.pop(connection.id, None)
        await save_connections()
        logging.info(f"🔌 Disconnected: {connection.id}")
        return

    # Подключение / обновление прав
    active_connections[connection.id] = connection
    await save_connections()
    logging.info(f"🔗 Connected: {connection.id} (user {connection.user.id})")

    if connection.rights and connection.rights.can_edit_bio:
        _start_tracking(connection.id)
    else:
        logging.warning(f"⚠️ No can_edit_bio right for {connection.id}")


# ================================================================
# Запуск
# ================================================================
async def restore_connections() -> None:
    """Загружает сохранённые подключения и запускает трекинг."""
    restored = await asyncio.to_thread(_load_connections_sync)
    if not restored:
        logging.info("💾 Нет сохранённых подключений.")
        return

    for cid, conn in restored.items():
        active_connections[cid] = conn
        if conn.rights and conn.rights.can_edit_bio:
            _start_tracking(cid)
            logging.info(f"♻️ Восстановлен трекинг: {cid}")
        else:
            logging.info(f"♻️ Подключение без can_edit_bio: {cid}")


async def main():
    logging.info("🤖 Bot started.")
    await restore_connections()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
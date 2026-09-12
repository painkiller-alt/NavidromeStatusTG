#!/usr/bin/env python3
"""Выводит текущий прослушиваемый трек с сервера Navidrome."""

import hashlib
import os
import secrets
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def get_config() -> dict:
    """Читает и валидирует конфигурацию из .env."""
    required = ["NAVIDROME_URL", "NAVIDROME_USER", "NAVIDROME_PASSWORD"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        sys.exit(f"❌ Не заданы обязательные переменные в .env: {', '.join(missing)}")

    return {
        "url": os.getenv("NAVIDROME_URL").rstrip("/"),
        "user": os.getenv("NAVIDROME_USER"),
        "password": os.getenv("NAVIDROME_PASSWORD"),
        "client": os.getenv("NAVIDROME_CLIENT", "NowPlayingScript"),
        "api_version": os.getenv("NAVIDROME_API_VERSION", "1.16.1"),
        "filter_user": os.getenv("NAVIDROME_FILTER_USER", ""),
        "output_format": os.getenv("OUTPUT_FORMAT", "text").lower(),
    }


def build_auth_params(user: str, password: str) -> dict:
    """Генерирует соль и токен для Subsonic API."""
    salt = secrets.token_hex(16)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {"u": user, "t": token, "s": salt}


def fetch_now_playing(cfg: dict) -> list:
    """Запрашивает у Navidrome список активных сессий."""
    params = {
        **build_auth_params(cfg["user"], cfg["password"]),
        "v": cfg["api_version"],
        "c": cfg["client"],
        "f": "json",
    }

    endpoint = f"{cfg['url']}/rest/getNowPlaying"

    try:
        response = requests.get(endpoint, params=params, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        sys.exit(f"❌ Ошибка запроса к Navidrome: {exc}")

    data = response.json()

    # Subsonic API всегда возвращает статус внутри тела ответа
    sub_response = data.get("subsonic-response", {})
    if sub_response.get("status") != "ok":
        error = sub_response.get("error", {})
        sys.exit(f"❌ Navidrome вернул ошибку: {error.get('message', 'неизвестная ошибка')}")

    entries = sub_response.get("nowPlaying", {}).get("entry", [])
    # Если записей одна — API возвращает объект, а не список
    if isinstance(entries, dict):
        entries = [entries]
    return entries


def format_track(entry: dict) -> str:
    """Формирует строку 'Автор - Трек'."""
    artist = entry.get("artist") or entry.get("albumArtist") or "Неизвестный исполнитель"
    title = entry.get("title") or "Без названия"
    return f"{artist} - {title}"


def main() -> None:
    cfg = get_config()
    entries = fetch_now_playing(cfg)

    if not entries:
        print("🔇 Сейчас ничего не воспроизводится.")
        return

    # Фильтр по конкретному пользователю, если задан
    if cfg["filter_user"]:
        entries = [e for e in entries if e.get("username") == cfg["filter_user"]]
        if not entries:
            print(f"🔇 Пользователь '{cfg['filter_user']}' сейчас ничего не слушает.")
            return

    if cfg["output_format"] == "json":
        import json
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return

    # Текстовый вывод
    for entry in entries:
        track = format_track(entry)
        user = entry.get("username", "?")
        player = entry.get("playerName") or entry.get("playerId") or "?"
        minutes = entry.get("minutesAgo", 0)
        suffix = f"  [{user} @ {player}, {minutes} мин. назад]"
        print(f"🎵 {track}{suffix}")


if __name__ == "__main__":
    main()
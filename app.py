import asyncio
import logging
import os
import uuid
import subprocess
import hashlib
import time
from datetime import date

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile
)
from aiogram.filters import Command

from config import *

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

queue = asyncio.Semaphore(3)

CACHE_DB = "cache.db"
LOG_DB = "logs.db"

user_last_requests = {}
user_temp_ban = {}

MAX_SIZE_MB = 60
RATE_LIMIT_COUNT = 3
RATE_LIMIT_WINDOW = 30

# ================= INIT =================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            joined_at TEXT
        )
        """)
        await db.commit()

    async with aiosqlite.connect(LOG_DB) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS downloads_log (
            user_id INTEGER,
            platform TEXT,
            file_size REAL,
            created_at TEXT
        )
        """)
        await db.commit()

# ================= USERS =================

async def add_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users VALUES (?, datetime('now'))", (user_id,))
        await db.commit()

async def get_user_count():
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0]

# ================= LOGS =================

async def log_download(user_id, platform, size):
    async with aiosqlite.connect(LOG_DB) as db:
        await db.execute(
            "INSERT INTO downloads_log VALUES (?, ?, ?, datetime('now'))",
            (user_id, platform, size)
        )
        await db.commit()

async def get_stats():
    async with aiosqlite.connect(LOG_DB) as db:
        cur = await db.execute("SELECT COUNT(*) FROM downloads_log")
        total = (await cur.fetchone())[0]

        cur = await db.execute(
            "SELECT platform, COUNT(*) FROM downloads_log GROUP BY platform"
        )
        platforms = await cur.fetchall()

        return total, platforms

# ================= PLATFORM =================

def detect_platform(url):
    url = url.lower()
    if "youtube.com" in url or "youtu.be" in url:
        return "YouTube"
    if "tiktok.com" in url:
        return "TikTok"
    if "instagram.com" in url:
        return "Instagram"
    if "vk.com" in url:
        return "VK"
    if "twitter.com" in url or "x.com" in url:
        return "Twitter"
    if "pinterest" in url:
        return "Pinterest"
    return "Other"

# ================= RATE LIMIT =================

def check_rate_limit(user_id):
    now = time.time()

    if user_id in user_temp_ban:
        if now < user_temp_ban[user_id]:
            return False
        else:
            del user_temp_ban[user_id]

    timestamps = user_last_requests.get(user_id, [])
    timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    timestamps.append(now)

    user_last_requests[user_id] = timestamps

    if len(timestamps) > RATE_LIMIT_COUNT:
        user_temp_ban[user_id] = now + 60
        return False

    return True

# ================= DOWNLOAD =================

def run_yt_dlp(url):
    filename = hashlib.md5(url.encode()).hexdigest()
    output = os.path.join(DOWNLOAD_PATH, filename)

    command = [
        "yt-dlp",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--concurrent-fragments", "5",
        "-o", f"{output}.%(ext)s",
        url
    ]

    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for file in os.listdir(DOWNLOAD_PATH):
        if file.startswith(filename):
            return os.path.join(DOWNLOAD_PATH, file)

    return None

# ================= START =================

@dp.message(Command("start"))
async def start(message: Message):
    await add_user(message.from_user.id)
    await message.answer("🎬 Hoard Video Bot\n\nОтправь ссылку 🔥")

# ================= ADMIN =================

@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    users = await get_user_count()
    total, platforms = await get_stats()

    text = f"👥 Пользователей: {users}\n📥 Всего скачиваний: {total}\n\n"

    for p in platforms:
        text += f"{p[0]} — {p[1]}\n"

    await message.answer(text)

# ================= MAIN HANDLER =================

@dp.message(F.text)
async def handle(message: Message):

    url = message.text.strip()

    if not url.startswith("http"):
        return

    if not check_rate_limit(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов. Подожди 1 минуту.")
        return

    await message.answer("⏳ Загружаю...")

    async with queue:
        file_path = run_yt_dlp(url)

    if not file_path:
        await message.answer("❌ Ссылка не поддерживается или нерабочая")
        return

    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    if size_mb > MAX_SIZE_MB:
        os.remove(file_path)
        await message.answer("❌ Видео больше 60 МБ. Слишком большое для отправки.")
        return

    platform = detect_platform(url)

    await log_download(message.from_user.id, platform, size_mb)

    file = FSInputFile(file_path)

    await message.answer_video(
        file,
        caption="🎉 Скачано с помощью\n@HoardVideoBot"
    )

    os.remove(file_path)

# ================= MAIN =================

async def main():
    await init_db()
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    print("Bot 4.0 started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

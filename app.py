import asyncio
import logging
import os
import uuid
import subprocess
from datetime import date

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    FSInputFile
)
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import *

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

queue = asyncio.Semaphore(2)

# ---------------- DATABASE ----------------

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            joined_at TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            telegram_id INTEGER,
            date TEXT,
            count INTEGER DEFAULT 0,
            PRIMARY KEY (telegram_id, date)
        )
        """)
        await db.commit()

async def add_user(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        INSERT OR IGNORE INTO users (telegram_id, joined_at)
        VALUES (?, datetime('now'))
        """, (user_id,))
        await db.commit()

async def get_downloads_today(user_id):
    today = str(date.today())
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "SELECT count FROM downloads WHERE telegram_id=? AND date=?",
            (user_id, today)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

async def increment_download(user_id):
    today = str(date.today())
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        INSERT INTO downloads (telegram_id, date, count)
        VALUES (?, ?, 1)
        ON CONFLICT(telegram_id, date)
        DO UPDATE SET count = count + 1
        """, (user_id, today))
        await db.commit()

# ---------------- DOWNLOAD ----------------

def run_yt_dlp(url, audio_only=False):
    filename = str(uuid.uuid4())
    output = os.path.join(DOWNLOAD_PATH, filename)

    command = [
        "yt-dlp",
        "--max-filesize", f"{MAX_FILESIZE_MB}M",
        "-o", f"{output}.%(ext)s",
        url
    ]

    if audio_only:
        command.insert(1, "-x")
        command.insert(2, "--audio-format")
        command.insert(3, "mp3")

    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for file in os.listdir(DOWNLOAD_PATH):
        if file.startswith(filename):
            return os.path.join(DOWNLOAD_PATH, file)

    return None

# ---------------- START ----------------

@dp.message(Command("start"))
async def start(message: Message):
    await add_user(message.from_user.id)

    text = f"""
🎬 <b>Hoard Video Bot</b>

━━━━━━━━━━━━━━━━━━

📥 Скачиваю видео из:

▸ YouTube  
▸ TikTok  
▸ Instagram  
▸ Pinterest  
▸ Facebook  
▸ Twitter  

━━━━━━━━━━━━━━━━━━

📎 Просто отправь ссылку  
И я пришлю файл в один миг ⚡
"""

    await message.answer(text, parse_mode="HTML")

# ---------------- LINK HANDLER ----------------

@dp.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()

    if not url.startswith("http"):
        return

    downloads = await get_downloads_today(message.from_user.id)

    if downloads >= DAILY_LIMIT:
        await message.answer("⚠️ Лимит на сегодня исчерпан")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎥 Видео", callback_data=f"video|{url}"),
            InlineKeyboardButton(text="🎵 MP3", callback_data=f"audio|{url}")
        ]
    ])

    await message.answer("Выбери формат:", reply_markup=keyboard)

# ---------------- CALLBACK ----------------

@dp.callback_query(F.data.contains("|"))
async def process_download(callback: CallbackQuery):
    mode, url = callback.data.split("|")

    await callback.answer("⏳ Обработка...")

    waiting = await callback.message.answer("⏳ Загружаю файл...")

    async with queue:
        file_path = run_yt_dlp(url, audio_only=(mode == "audio"))

    if not file_path:
        await waiting.edit_text("❌ Ошибка загрузки")
        return

    try:
        file = FSInputFile(file_path)

        if mode == "video":
            await callback.message.answer_video(
                file,
                caption="🎉 Скачано с помощью\n@HoardVideoBot"
            )
        else:
            await callback.message.answer_audio(
                file,
                caption="🎉 Скачано с помощью\n@HoardVideoBot"
            )

        await increment_download(callback.from_user.id)
        await waiting.delete()

    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

# ---------------- WEBHOOK ----------------

async def on_startup(app):
    await init_db()
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    await bot.set_webhook(WEBHOOK_URL)

async def on_shutdown(app):
    await bot.delete_webhook()

def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path="/")
    setup_application(app, dp, bot=bot)

    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()

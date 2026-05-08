import asyncio
import logging
import os
import uuid
import subprocess
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

queue = asyncio.Semaphore(2)
broadcast_mode = {}

# ================= DATABASE =================

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


async def get_total_users():
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0]


async def get_downloads_today(user_id):
    today = str(date.today())
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute(
            "SELECT count FROM downloads WHERE telegram_id=? AND date=?",
            (user_id, today)
        )
        row = await cur.fetchone()
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

# ================= DOWNLOAD =================

def run_yt_dlp(url, audio_only=False):
    filename = str(uuid.uuid4())
    output = os.path.join(DOWNLOAD_PATH, filename)

    command = [
        "yt-dlp",
        "-f", "mp4",
        "--no-playlist",
        "-o", f"{output}.%(ext)s",
        url
    ]

    if audio_only:
        command = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "-o", f"{output}.%(ext)s",
            url
        ]

    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for file in os.listdir(DOWNLOAD_PATH):
        if file.startswith(filename):
            return os.path.join(DOWNLOAD_PATH, file)

    return None


def compress_video(input_path):
    output_path = input_path.replace(".mp4", "_compressed.mp4")

    command = [
        "ffmpeg",
        "-i", input_path,
        "-vcodec", "libx264",
        "-crf", "28",
        output_path
    ]

    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return output_path


# ================= START =================

@dp.message(Command("start"))
async def start(message: Message):
    await add_user(message.from_user.id)

    text = """
🎬 <b>Hoard Video Bot</b>

━━━━━━━━━━━━━━━━━━

📥 Поддержка:

▸ YouTube Shorts  
▸ TikTok (без watermark)  
▸ Instagram  
▸ VK  
▸ Twitter/X  
▸ Pinterest  

━━━━━━━━━━━━━━━━━━

📎 Просто отправь ссылку
"""

    await message.answer(text, parse_mode="HTML")


# ================= ADMIN =================

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    users = await get_total_users()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="broadcast")]
    ])

    await message.answer(
        f"👑 Админ панель\n\n👥 Пользователей: {users}",
        reply_markup=keyboard
    )


@dp.callback_query(F.data == "broadcast")
async def start_broadcast(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    broadcast_mode[ADMIN_ID] = True
    await callback.message.answer("Отправь сообщение для рассылки")


@dp.message()
async def handle_broadcast(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    if not broadcast_mode.get(ADMIN_ID):
        return

    broadcast_mode[ADMIN_ID] = False

    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("SELECT telegram_id FROM users")
        users = await cur.fetchall()

    sent = 0
    for user in users:
        try:
            await bot.send_message(user[0], message.text)
            sent += 1
        except:
            pass

    await message.answer(f"✅ Отправлено: {sent}")


# ================= DOWNLOAD =================

@dp.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()

    if not url.startswith("http"):
        return

    downloads = await get_downloads_today(message.from_user.id)

    if downloads >= DAILY_LIMIT and message.from_user.id != ADMIN_ID:
        await message.answer("⚠️ Лимит на сегодня исчерпан")
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎥 Видео", callback_data=f"video|{url}"),
            InlineKeyboardButton(text="🎵 MP3", callback_data=f"audio|{url}")
        ]
    ])

    await message.answer("Выбери формат:", reply_markup=keyboard)


@dp.callback_query(F.data.contains("|"))
async def process_download(callback: CallbackQuery):
    mode, url = callback.data.split("|")

    await callback.answer("⏳ Обработка...")

    waiting = await callback.message.answer("⏳ Загружаю...")

    if callback.from_user.id == ADMIN_ID:
        file_path = run_yt_dlp(url, audio_only=(mode == "audio"))
    else:
        async with queue:
            file_path = run_yt_dlp(url, audio_only=(mode == "audio"))

    if not file_path:
        await waiting.edit_text("❌ Ошибка загрузки")
        return

    size_mb = os.path.getsize(file_path) / (1024 * 1024)

    if size_mb > MAX_FILESIZE_MB and mode == "video":
        compressed = compress_video(file_path)
        file_path = compressed

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


# ================= MAIN =================

async def main():
    await init_db()
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    print("Bot started (2.0)")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

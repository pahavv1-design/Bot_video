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

queue = asyncio.Semaphore(3)
broadcast_mode = {}
user_links = {}

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

# ================= SUBSCRIPTION =================

async def check_subscription(user_id):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

# ================= DOWNLOAD =================

def run_yt_dlp(url, audio_only=False):
    filename = str(uuid.uuid4())
    output = os.path.join(DOWNLOAD_PATH, filename)

    if audio_only:
        command = [
            "yt-dlp",
            "-x",
            "--audio-format", "mp3",
            "--no-playlist",
            "--concurrent-fragments", "5",
            "-o", f"{output}.%(ext)s",
            url
        ]
    else:
        command = [
            "yt-dlp",
            "-f", "bestvideo+bestaudio/best",
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

    if not await check_subscription(message.from_user.id):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@','')}")],
            [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")]
        ])
        await message.answer("Подпишитесь на канал 👇", reply_markup=keyboard)
        return

    await message.answer(
        "🎬 <b>Hoard Video Bot</b>\n\n"
        "Поддержка:\n"
        "YouTube Shorts\nTikTok\nInstagram\nVK\nTwitter/X\nPinterest\n\n"
        "Отправь ссылку 🔥",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.edit_text("✅ Подписка подтверждена. Отправь ссылку.")
    else:
        await callback.answer("❌ Вы не подписаны.", show_alert=True)

# ================= ADMIN =================

@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("👑 Админ панель работает ✅")

# ================= MESSAGE =================

@dp.message(F.text)
async def handle_message(message: Message):

    if not await check_subscription(message.from_user.id):
        return

    url = message.text.strip()

    if not url.startswith("http"):
        return

    downloads = await get_downloads_today(message.from_user.id)

    if downloads >= DAILY_LIMIT and message.from_user.id != ADMIN_ID:
        await message.answer("⚠️ Лимит исчерпан")
        return

    # сохраняем ссылку в память
    user_links[message.from_user.id] = url

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎥 Видео", callback_data="video"),
            InlineKeyboardButton(text="🎵 MP3", callback_data="audio")
        ]
    ])

    await message.answer("Выбери формат:", reply_markup=keyboard)

# ================= CALLBACK =================

@dp.callback_query(F.data.in_(["video", "audio"]))
async def process_download(callback: CallbackQuery):

    url = user_links.get(callback.from_user.id)

    if not url:
        await callback.answer("Ошибка ссылки")
        return

    mode = callback.data

    await callback.answer("⏳ Обработка...")
    waiting = await callback.message.answer("⏳ Загружаю...")

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

# ================= MAIN =================

async def main():
    await init_db()
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    print("Bot fixed version started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

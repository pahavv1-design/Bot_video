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
    CallbackQuery
)
from aiogram.filters import Command
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from config import *

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

queue = asyncio.Semaphore(3)

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


async def get_total_users():
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        row = await cursor.fetchone()
        return row[0]


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

# ---------------- SUBSCRIPTION CHECK ----------------

async def check_subscription(user_id):
    if not REQUIRED_CHANNEL:
        return True

    member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
    return member.status in ["member", "administrator", "creator"]

# ---------------- DOWNLOADER ----------------

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

# ---------------- HANDLERS ----------------

@dp.message(Command("start"))
async def start(message: Message):
    await add_user(message.from_user.id)

    text = """
🎬 Downloader Bot

Поддержка:
YouTube | TikTok | Instagram | Pinterest

Отправь ссылку 👇
"""
    await message.answer(text)


@dp.message(Command("admin"))
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    total = await get_total_users()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="broadcast")]
    ])

    await message.answer(f"👑 Админ панель\n\n👥 Пользователей: {total}", reply_markup=keyboard)


@dp.callback_query()
async def admin_callbacks(callback: CallbackQuery):

    if callback.data == "stats":
        total = await get_total_users()
        await callback.message.answer(f"📊 Всего пользователей: {total}")

    if callback.data == "broadcast":
        await callback.message.answer("Отправь сообщение для рассылки")
        dp.message.register(broadcast_message)


async def broadcast_message(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT telegram_id FROM users")
        users = await cursor.fetchall()

    sent = 0
    for user in users:
        try:
            await bot.send_message(user[0], message.text)
            sent += 1
        except:
            pass

    await message.answer(f"✅ Отправлено: {sent}")

# ---------------- DOWNLOAD HANDLER ----------------

@dp.message(F.text)
async def handle_link(message: Message):
    url = message.text.strip()

    if not url.startswith("http"):
        return

    if not await check_subscription(message.from_user.id):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подписаться", url=f"https://t.me/{REQUIRED_CHANNEL.replace('@','')}")]
        ])
        await message.answer("Подпишись на канал для использования бота", reply_markup=keyboard)
        return

    downloads = await get_downloads_today(message.from_user.id)

    if downloads >= DAILY_LIMIT:
        await message.answer("⚠ Лимит на сегодня исчерпан")
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

    await callback.message.edit_text("⏳ Скачиваю...")

    async with queue:
        file_path = run_yt_dlp(url, audio_only=(mode == "audio"))

    if not file_path:
        await callback.message.answer("❌ Ошибка загрузки")
        return

    try:
        await callback.message.answer_document(open(file_path, "rb"))
        await increment_download(callback.from_user.id)
    finally:
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

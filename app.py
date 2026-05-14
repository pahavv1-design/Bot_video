import asyncio
import logging
import os
import subprocess
import hashlib
import time
import re
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, FSInputFile
from aiogram.filters import Command
from config import *

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

queue = asyncio.Semaphore(5)

DB_MAIN = "main.db"
DB_LOG = "logs.db"
DB_CACHE = "cache.db"

MAX_SIZE_MB = 60
CACHE_TTL = 86400  # 24 часа

user_links = {}

# ================= INIT =================

async def init_db():
    async with aiosqlite.connect(DB_MAIN) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
        await db.commit()

    async with aiosqlite.connect(DB_LOG) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            platform TEXT,
            created_at INTEGER
        )
        """)
        await db.commit()

    async with aiosqlite.connect(DB_CACHE) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS cache (
            url TEXT PRIMARY KEY,
            file_path TEXT,
            created_at INTEGER
        )
        """)
        await db.commit()

# ================= CACHE =================

async def get_cached(url):
    async with aiosqlite.connect(DB_CACHE) as db:
        cur = await db.execute("SELECT file_path, created_at FROM cache WHERE url=?", (url,))
        row = await cur.fetchone()

        if not row:
            return None

        file_path, created_at = row

        # Проверка срока жизни
        if time.time() - created_at > CACHE_TTL:
            if os.path.exists(file_path):
                os.remove(file_path)
            await db.execute("DELETE FROM cache WHERE url=?", (url,))
            await db.commit()
            return None

        if os.path.exists(file_path):
            return file_path

        return None


async def save_cache(url, file_path):
    async with aiosqlite.connect(DB_CACHE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
            (url, file_path, int(time.time()))
        )
        await db.commit()


async def cleanup_cache():
    async with aiosqlite.connect(DB_CACHE) as db:
        cur = await db.execute("SELECT url, file_path, created_at FROM cache")
        rows = await cur.fetchall()

        for url, path, created in rows:
            if time.time() - created > CACHE_TTL:
                if os.path.exists(path):
                    os.remove(path)
                await db.execute("DELETE FROM cache WHERE url=?", (url,))
        await db.commit()

# ================= USERS =================

async def add_user(user_id):
    async with aiosqlite.connect(DB_MAIN) as db:
        await db.execute("INSERT OR IGNORE INTO users VALUES (?)", (user_id,))
        await db.commit()

# ================= PLATFORM =================

def detect_platform(url):
    u = url.lower()
    if "youtube" in u or "youtu.be" in u:
        return "YouTube"
    if "tiktok" in u:
        return "TikTok"
    if "instagram" in u:
        return "Instagram"
    if "vk.com" in u:
        return "VK"
    if "twitter" in u or "x.com" in u:
        return "Twitter"
    if "pinterest" in u or "pin.it" in u:
        return "Pinterest"
    return "Other"

# ================= NORMALIZE YOUTUBE =================

def normalize_youtube(url):
    url = url.split("?")[0]
    match = re.search(r"/shorts/([a-zA-Z0-9_-]+)", url)
    if match:
        video_id = match.group(1)
        return f"https://www.youtube.com/watch?v={video_id}"
    return url

# ================= DOWNLOAD =================

def run_yt_dlp(url):
    filename = hashlib.md5(url.encode()).hexdigest()
    output = os.path.join(DOWNLOAD_PATH, filename)

    command = [
        "yt-dlp",
        "-f", "best",
        "--no-playlist",
        "-o", f"{output}.%(ext)s",
        url
    ]

    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for f in os.listdir(DOWNLOAD_PATH):
        if f.startswith(filename):
            return os.path.join(DOWNLOAD_PATH, f)

    return None

# ================= START =================

@dp.message(Command("start"))
async def start(message: Message):
    await add_user(message.from_user.id)

    text = """
🎬 <b>HoardVideoBot</b>

━━━━━━━━━━━━━━━━━━

📥 Поддержка:

▸ YouTube & Shorts  
▸ TikTok  
▸ Instagram (фото + видео)  
▸ VK (фото + видео)  
▸ Twitter/X (фото + видео)  
▸ Pinterest (фото + видео)  

━━━━━━━━━━━━━━━━━━

📎 Отправьте ссылку —
я быстро подготовлю файл ⚡
"""

    await message.answer(text, parse_mode="HTML")

# ================= MESSAGE =================

@dp.message(F.text)
async def handle(message: Message):

    url = message.text.strip()
    if not url.startswith("http"):
        return

    url = normalize_youtube(url)
    user_links[message.from_user.id] = url

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎥 Видео", callback_data="video"),
            InlineKeyboardButton(text="🎵 Музыка", callback_data="audio"),
            InlineKeyboardButton(text="🖼 Фото", callback_data="photo")
        ]
    ])

    await message.answer("Выберите формат:", reply_markup=keyboard)

# ================= CALLBACK =================

@dp.callback_query(F.data.in_(["video", "audio", "photo"]))
async def process(callback: CallbackQuery):

    url = user_links.get(callback.from_user.id)
    if not url:
        return

    loading = await callback.message.answer("⏳ Обрабатываю...")

    cached = await get_cached(url)

    if cached:
        file_path = cached
    else:
        async with queue:
            file_path = run_yt_dlp(url)

        if not file_path:
            await loading.edit_text("❌ Ссылка не поддерживается")
            return

        await save_cache(url, file_path)

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > MAX_SIZE_MB:
        await loading.edit_text("❌ Файл больше 60 МБ")
        return

    file = FSInputFile(file_path)

    if callback.data == "video":
        await callback.message.answer_video(file, caption="🎉 @HoardVideoBot")
    elif callback.data == "audio":
        await callback.message.answer_audio(file, caption="🎉 @HoardVideoBot")
    else:
        await callback.message.answer_photo(file, caption="🎉 @HoardVideoBot")

    await loading.delete()

# ================= MAIN =================

async def main():
    await init_db()
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    await cleanup_cache()  # автоочистка при старте
    print("Bot с продакшен-кэшем запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

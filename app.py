import asyncio
import logging
import os
import subprocess
import hashlib
import time
import re
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

DB_MAIN = "main.db"
DB_LOG = "logs.db"

MAX_SIZE_MB = 60
RATE_LIMIT = 3
RATE_WINDOW = 30

user_requests = {}
temp_ban = {}
user_links = {}
broadcast_mode = {}
set_channel_mode = {}

# ================= INIT =================

async def init_db():
    async with aiosqlite.connect(DB_MAIN) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY)")
        await db.commit()

    async with aiosqlite.connect(DB_LOG) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            platform TEXT,
            created_at TEXT
        )
        """)
        await db.commit()

# ================= RATE LIMIT =================

def check_rate(user_id):
    now = time.time()

    if user_id in temp_ban:
        if now < temp_ban[user_id]:
            return False
        else:
            del temp_ban[user_id]

    times = user_requests.get(user_id, [])
    times = [t for t in times if now - t < RATE_WINDOW]
    times.append(now)
    user_requests[user_id] = times

    if len(times) > RATE_LIMIT:
        temp_ban[user_id] = now + 60
        return False

    return True

# ================= USERS =================

async def add_user(user_id):
    async with aiosqlite.connect(DB_MAIN) as db:
        await db.execute("INSERT OR IGNORE INTO users VALUES (?)", (user_id,))
        await db.commit()

async def get_users_count():
    async with aiosqlite.connect(DB_MAIN) as db:
        cur = await db.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
        return row[0]

# ================= LOGS =================

async def log_download(platform):
    async with aiosqlite.connect(DB_LOG) as db:
        await db.execute("INSERT INTO downloads VALUES (?, datetime('now'))", (platform,))
        await db.commit()

async def get_download_stats():
    async with aiosqlite.connect(DB_LOG) as db:
        cur = await db.execute("SELECT COUNT(*) FROM downloads")
        total = (await cur.fetchone())[0]

        cur = await db.execute("SELECT platform, COUNT(*) FROM downloads GROUP BY platform")
        rows = await cur.fetchall()

        return total, rows

# ================= PLATFORM =================

def detect_platform(url):
    u = url.lower()
    if "youtube" in u:
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

# ================= DOWNLOAD =================

def run_yt_dlp(url, audio=False):
    filename = hashlib.md5(url.encode()).hexdigest()
    output = os.path.join(DOWNLOAD_PATH, filename)

    # Instagram через mobile extractor
    if "instagram.com" in url:
        base = [
            "yt-dlp",
            "--user-agent", "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X)",
            "-o", f"{output}.%(ext)s",
            url
        ]
    else:
        base = [
            "yt-dlp",
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", f"{output}.%(ext)s",
            url
        ]

    if audio:
        base.insert(1, "-x")
        base.insert(2, "--audio-format")
        base.insert(3, "mp3")

    subprocess.run(base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for f in os.listdir(DOWNLOAD_PATH):
        if f.startswith(filename):
            return os.path.join(DOWNLOAD_PATH, f)

    return None

# ================= START =================

@dp.message(Command("start"))
async def start(message: Message):
    await add_user(message.from_user.id)

    await message.answer("""
🎬 <b>HoardVideoBot</b>

━━━━━━━━━━━━━━━━━━
📥 YouTube & Shorts
📥 TikTok
📥 Instagram ( фото и видео) 
📥 VK
📥 Twitter/X
📥 Pinterest ( видео) 
━━━━━━━━━━━━━━━━━━

📎 Отправь ссылку 🔥
""", parse_mode="HTML")

# ================= ADMIN =================

@dp.message(Command("admin"))
async def admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        return

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="broadcast")]
    ])

    await message.answer("👑 Админ панель", reply_markup=keyboard)

@dp.callback_query(F.data == "stats")
async def stats(callback: CallbackQuery):
    users = await get_users_count()
    total, rows = await get_download_stats()

    text = f"👥 Пользователи: {users}\n📥 Всего скачиваний: {total}\n\n"
    for r in rows:
        text += f"{r[0]} — {r[1]}\n"

    await callback.message.answer(text)

@dp.callback_query(F.data == "broadcast")
async def start_broadcast(callback: CallbackQuery):
    broadcast_mode[callback.from_user.id] = True
    await callback.message.answer("Отправь текст для рассылки")

# ================= MESSAGE =================

@dp.message(F.text)
async def handle(message: Message):

    if message.from_user.id in broadcast_mode:
        broadcast_mode.pop(message.from_user.id)

        async with aiosqlite.connect(DB_MAIN) as db:
            cur = await db.execute("SELECT id FROM users")
            users = await cur.fetchall()

        for u in users:
            try:
                await bot.send_message(u[0], message.text)
            except:
                pass

        await message.answer("✅ Рассылка завершена")
        return

    url = message.text.strip()

    if not url.startswith("http"):
        return

    if not check_rate(message.from_user.id):
        await message.answer("⚠️ Слишком много запросов.")
        return

    user_links[message.from_user.id] = url

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎥 Видео", callback_data="video"),
         InlineKeyboardButton(text="🎵 Музыка", callback_data="audio")]
    ])

    await message.answer("Выбери формат:", reply_markup=keyboard)

# ================= CALLBACK =================

@dp.callback_query(F.data.in_(["video", "audio"]))
async def process(callback: CallbackQuery):

    url = user_links.get(callback.from_user.id)
    if not url:
        return

    platform = detect_platform(url)

    loading = await callback.message.answer("⏳ Загружаю...")

    async with queue:
        file_path = run_yt_dlp(url, audio=(callback.data == "audio"))

    if not file_path:
        await loading.edit_text("❌ Ссылка не поддерживается или нерабочая")
        return

    size_mb = os.path.getsize(file_path) / (1024 * 1024)
    if size_mb > MAX_SIZE_MB:
        os.remove(file_path)
        await loading.edit_text("❌ Файл больше 60 МБ")
        return

    await log_download(platform)

    file = FSInputFile(file_path)

    if callback.data == "video":
        await callback.message.answer_video(file, caption="🎉 @HoardVideoBot")
    else:
        await callback.message.answer_audio(file, caption="🎉 @HoardVideoBot")

    os.remove(file_path)
    await loading.delete()

# ================= MAIN =================

async def main():
    await init_db()
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)
    print("Bot Instagram fixed version")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

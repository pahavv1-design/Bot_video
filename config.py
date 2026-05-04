import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", 3))
MAX_FILESIZE_MB = int(os.getenv("MAX_FILESIZE_MB", 50))

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8000))

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL")  # @channel_username

DOWNLOAD_PATH = "downloads"
DB_NAME = "users.db"

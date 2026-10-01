import asyncio
import csv
import io
import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Message, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter


# ============================================================
# CONFIG — EDIT THESE VALUES BEFORE FIRST LAUNCH
# ============================================================

# Bothost automatically provides BOT_TOKEN as an environment variable.
# Local values below are only fallbacks for running the bot outside Bothost.
BOT_TOKEN = os.getenv("BOT_TOKEN", "8884323949:AAHInqNiW_-bvUUMEhikK_3_Ueb73MyZJ_4")

# Set ADMIN_IDS on Bothost as a comma-separated list, e.g. "123456789,987654321".
def _parse_admin_ids(value: str):
    result = []
    for item in value.split(","):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))
    return result or [123456789]

ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", "8754872846"))

# Used only for the manual Telegram Stars flow.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "@chlenMixi")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1004371944515")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+ZU25gLDwviYzZmY1")

# Bothost preserves /app/data between container restarts/deployments.
# Keep the SQLite DB there instead of next to the source code.
DB_PATH = os.getenv("DB_PATH", os.getenv("DATABASE_PATH", "/app/data/schoolvideos.db"))
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

# Initial settings are used only when the database is empty.
BOT_NAME = os.getenv("BOT_NAME", "SchoolVideos")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("schoolvideos")


# ============================================================
# DATABASE
# ============================================================

class Database:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = asyncio.Lock()

    async def init(self):
        async with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                blocked INTEGER NOT NULL DEFAULT 0,
                subscribed INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                rub_price INTEGER NOT NULL DEFAULT 0,
                stars_price INTEGER NOT NULL DEFAULT 0,
                is_free INTEGER NOT NULL DEFAULT 0,
                cooldown_seconds INTEGER NOT NULL DEFAULT 86400,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                added_at TEXT NOT NULL,
                deliveries INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                price INTEGER NOT NULL DEFAULT 0,
                payment_method TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                confirmed_at TEXT,
                confirmed_by INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                payment_method TEXT NOT NULL DEFAULT 'RUB',
                receipt_type TEXT,
                receipt_file_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                handled_at TEXT,
                handled_by INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS free_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                claimed_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(category_id) REFERENCES categories(id)
            );

            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category_id INTEGER NOT NULL,
                video_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(category_id) REFERENCES categories(id),
                FOREIGN KEY(video_id) REFERENCES videos(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_users_last_seen ON users(last_seen);
            CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(blocked);
            CREATE INDEX IF NOT EXISTS idx_videos_category ON videos(category_id);
            CREATE INDEX IF NOT EXISTS idx_deliveries_user ON deliveries(user_id);
            CREATE INDEX IF NOT EXISTS idx_deliveries_category ON deliveries(category_id);
            CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id);
            CREATE INDEX IF NOT EXISTS idx_purchases_category ON purchases(category_id);
            CREATE INDEX IF NOT EXISTS idx_requests_status ON payment_requests(status);
            """)
            self.conn.commit()

            defaults = {
                "bot_name": BOT_NAME,
                "main_text": "Добро пожаловать в магазин видео.",
                "subscription_text": "Для того чтобы не потерять нашего бота, подпишитесь на канал!",
                "help_text": "Выберите нужный раздел в меню. Если нужна помощь с покупкой, обратитесь к администратору.",
                "payment_text": "Переведите {price} ₽ по указанным реквизитам.\n\n{details}\n\nПосле оплаты отправьте чек.",
                "recipient_name": "",
                "payment_details": "Реквизиты пока не установлены.",
                "main_photo": "",
                "category_photo": "",
                "payment_photo": "",
                "success_photo": "",
                "admin_photo": "",
                "purchases_enabled": "1",
                "stars_enabled": "1",
                "free_enabled": "1",
                "registration_enabled": "1",
                "maintenance": "0",
                "channel_id": CHANNEL_ID,
                "channel_url": CHANNEL_URL,
                "admin_username": ADMIN_USERNAME,
                "btn_categories": "Категории",
                "btn_purchases": "Мои покупки",
                "btn_free": "Бесплатное видео",
                "btn_help": "Помощь",
            }
            for k, v in defaults.items():
                self.conn.execute(
                    "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v)
                )

            count = self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            if count == 0:
                now = utcnow()
                self.conn.executemany(
                    """INSERT INTO categories
                    (name,rub_price,stars_price,is_free,cooldown_seconds,created_at)
                    VALUES(?,?,?,?,?,?)""",
                    [
                        ("Красивые", 90, 50, 0, 86400, now),
                        ("Элегантные", 125, 100, 0, 86400, now),
                        ("Hades", 0, 0, 1, 86400, now),
                    ],
                )
            self.conn.commit()

    async def execute(self, sql, params=()):
        async with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    async def fetchone(self, sql, params=()):
        async with self.lock:
            return self.conn.execute(sql, params).fetchone()

    async def fetchall(self, sql, params=()):
        async with self.lock:
            return self.conn.execute(sql, params).fetchall()

    async def setting(self, key, default=""):
        row = await self.fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key, value):
        await self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


db = Database(DB_PATH)


# ============================================================
# HELPERS
# ============================================================

def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dt_from_iso(value):
    return datetime.fromisoformat(value)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def log_action(admin_id: int, action: str, details: str = ""):
    await db.execute(
        "INSERT INTO action_log(admin_id,action,details,created_at) VALUES(?,?,?,?)",
        (admin_id, action, details, utcnow()),
    )


async def ensure_user(user):
    now = utcnow()
    row = await db.fetchone("SELECT id FROM users WHERE id=?", (user.id,))
    if row:
        if await db.setting("registration_enabled", "1") == "1":
            await db.execute(
                "UPDATE users SET username=?, first_name=?, last_seen=? WHERE id=?",
                (user.username or "", user.first_name or "", now, user.id),
            )
    else:
        if await db.setting("registration_enabled", "1") == "1" or is_admin(user.id):
            await db.execute(
                "INSERT INTO users(id,username,first_name,first_seen,last_seen) VALUES(?,?,?,?,?)",
                (user.id, user.username or "", user.first_name or "", now, now),
            )


async def is_blocked(user_id: int) -> bool:
    row = await db.fetchone("SELECT blocked FROM users WHERE id=?", (user_id,))
    return bool(row and row["blocked"])


async def guarded(message_or_callback):
    user = message_or_callback.from_user
    await ensure_user(user)
    if is_admin(user.id):
        return False
    if await is_blocked(user.id):
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer("Доступ к боту ограничен.", show_alert=True)
        else:
            await message_or_callback.answer("Доступ к боту ограничен.")
        return True
    maintenance = await db.setting("maintenance", "0")
    if maintenance == "1":
        if isinstance(message_or_callback, CallbackQuery):
            await message_or_callback.answer("Бот временно находится на техническом обслуживании.", show_alert=True)
        else:
            await message_or_callback.answer("Бот временно находится на техническом обслуживании.")
        return True
    return False


async def subscription_required(user_id: int) -> bool:
    channel_id = await db.setting("channel_id", CHANNEL_ID)
    if not channel_id:
        return False
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except TelegramBadRequest as e:
        error_text = str(e)
        if "member list is inaccessible" in error_text.lower():
            logger.warning(
                "Subscription check unavailable: the bot must be an administrator "
                "of the private channel %s. Add the bot as an admin and restart the bot.",
                channel_id,
            )
        else:
            logger.warning("Subscription check failed: %s", e)
        return False
    except Exception as e:
        logger.warning("Subscription check failed: %s", e)
        return False


async def sub_keyboard():
    url = await db.setting("channel_url", CHANNEL_URL)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться", url=url or CHANNEL_URL)],
        [InlineKeyboardButton(text="Проверить подписку", callback_data="sub_check")],
    ])


async def require_subscription_message(message):
    text = await db.setting("subscription_text")
    await message.answer(text, reply_markup=await sub_keyboard())


async def main_menu():
    # Main menu is a persistent Telegram reply keyboard below the input field.
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=await db.setting("btn_categories")),
                KeyboardButton(text=await db.setting("btn_purchases")),
            ],
            [
                KeyboardButton(text=await db.setting("btn_free")),
                KeyboardButton(text=await db.setting("btn_help")),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


async def send_main_menu(message):
    text = await db.setting("main_text")
    photo = await db.setting("main_photo")
    kb = await main_menu()
    if photo:
        try:
            await message.answer_photo(photo, caption=text, reply_markup=kb)
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb)


async def categories_keyboard():
    cats = await db.fetchall(
        "SELECT c.*, COUNT(v.id) AS video_count "
        "FROM categories c LEFT JOIN videos v ON v.category_id=c.id "
        "WHERE c.enabled=1 GROUP BY c.id ORDER BY c.id"
    )
    rows = []
    for c in cats:
        rows.append([InlineKeyboardButton(
            text=f"{c['name']} · {c['video_count']} видео",
            callback_data=f"cat:{c['id']}"
        )])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def category_text(category_id: int):
    c = await db.fetchone(
        "SELECT c.*, COUNT(v.id) AS video_count FROM categories c "
        "LEFT JOIN videos v ON v.category_id=c.id WHERE c.id=? GROUP BY c.id",
        (category_id,),
    )
    if not c:
        return None
    if c["is_free"]:
        price = "Бесплатно"
    else:
        price = f"{c['rub_price']} ₽ / {c['stars_price']} Stars"
    return f"<b>{escape(c['name'])}</b>\n\nВидео: {c['video_count']}\nЦена: {price}"


async def category_keyboard(category_id: int):
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (category_id,))
    rows = []
    if not c:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="categories")]
        ])
    if c["is_free"]:
        rows.append([InlineKeyboardButton(text="Получить бесплатно", callback_data=f"freecat:{category_id}")])
    else:
        if await db.setting("purchases_enabled", "1") == "1":
            rows.append([InlineKeyboardButton(text=f"Купить за {c['rub_price']} ₽", callback_data=f"rub:{category_id}")])
        if await db.setting("stars_enabled", "1") == "1":
            rows.append([InlineKeyboardButton(text=f"Купить за {c['stars_price']} Stars", callback_data=f"stars:{category_id}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="categories")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def deliver_category(user_id: int, category_id: int, source: str, status_text=True):
    cat = await db.fetchone("SELECT * FROM categories WHERE id=?", (category_id,))
    if not cat:
        return 0
    videos = await db.fetchall(
        "SELECT id,file_id FROM videos WHERE category_id=? ORDER BY id", (category_id,)
    )
    if not videos:
        return 0

    if status_text:
        try:
            await bot.send_message(user_id, "Начинаем выдачу видео.")
        except Exception:
            pass

    delivered = 0
    for video in videos:
        try:
            await bot.send_video(user_id, video["file_id"])
            await db.execute("UPDATE videos SET deliveries=deliveries+1 WHERE id=?", (video["id"],))
            await db.execute(
                "INSERT INTO deliveries(user_id,category_id,video_id,source,created_at) VALUES(?,?,?,?,?)",
                (user_id, category_id, video["id"], source, utcnow()),
            )
            delivered += 1
        except Exception as e:
            logger.warning("Delivery failed for user %s: %s", user_id, e)
        await asyncio.sleep(1)

    if status_text:
        try:
            await bot.send_message(user_id, "Выдача завершена.")
        except Exception:
            pass
    return delivered


# ============================================================
# FSM
# ============================================================

class BuyStates(StatesGroup):
    waiting_receipt = State()


class AdminStates(StatesGroup):
    broadcast_text = State()
    broadcast_photo = State()
    search_user = State()
    grant_user = State()
    grant_category = State()
    add_category_name = State()
    add_category_rub = State()
    add_category_stars = State()
    add_category_free = State()
    add_category_cooldown = State()
    add_video = State()
    delete_video = State()
    edit_category_name = State()
    edit_category_rub = State()
    edit_category_stars = State()
    edit_category_free = State()
    edit_category_cooldown = State()
    setting_value = State()


# ============================================================
# USER HANDLERS
# ============================================================

router = Router()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message.from_user)

    if await is_blocked(message.from_user.id) and not is_admin(message.from_user.id):
        await message.answer("Доступ к боту ограничен.")
        return

    if await db.setting("maintenance", "0") == "1" and not is_admin(message.from_user.id):
        await message.answer("Бот временно находится на техническом обслуживании.")
        return

    if not is_admin(message.from_user.id):
        if not await subscription_required(message.from_user.id):
            await require_subscription_message(message)
            return

    await send_main_menu(message)


@router.message(F.text.in_({"Категории", "Мои покупки", "Бесплатное видео", "Помощь"}))
async def main_menu_button(message: Message, state: FSMContext):
    # These are the four default labels stored in settings.
    # Do not intercept admin FSM input while an admin is entering data.
    if await state.get_state() is not None:
        return
    await ensure_user(message.from_user)

    if await is_blocked(message.from_user.id) and not is_admin(message.from_user.id):
        await message.answer("Доступ к боту ограничен.")
        return

    if not is_admin(message.from_user.id) and not await subscription_required(message.from_user.id):
        await require_subscription_message(message)
        return

    button = message.text
    if button == await db.setting("btn_categories", "Категории"):
        text = "Выберите категорию:"
        photo = await db.setting("category_photo") or await db.setting("main_photo")
        kb = await categories_keyboard()
        if photo:
            try:
                await message.answer_photo(photo, caption=text, reply_markup=kb)
                return
            except Exception:
                pass
        await message.answer(text, reply_markup=kb)
        return

    if button == await db.setting("btn_purchases", "Мои покупки"):
        rows = await db.fetchall(
            "SELECT p.*, c.name FROM purchases p JOIN categories c ON c.id=p.category_id "
            "WHERE p.user_id=? ORDER BY p.id DESC LIMIT 20",
            (message.from_user.id,),
        )
        if not rows:
            text = "У вас пока нет покупок."
        else:
            parts = ["<b>Мои покупки</b>\n"]
            for purchase in rows:
                parts.append(
                    f"#{purchase['id']} · {escape(purchase['name'])} · "
                    f"{purchase['payment_method']} · {purchase['status']}"
                )
            text = "\n".join(parts)
        await message.answer(text)
        return

    if button == await db.setting("btn_free", "Бесплатное видео"):
        cats = await db.fetchall("SELECT * FROM categories WHERE is_free=1 AND enabled=1")
        if not cats:
            await message.answer("Сейчас бесплатных видео нет.")
            return
        rows = [[InlineKeyboardButton(text=c["name"], callback_data=f"freecat:{c['id']}")] for c in cats]
        await message.answer("Бесплатные видео:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        return

    if button == await db.setting("btn_help", "Помощь"):
        await message.answer(await db.setting("help_text"))


@router.callback_query(F.data == "sub_check")
async def sub_check(callback: CallbackQuery):
    await ensure_user(callback.from_user)
    if await subscription_required(callback.from_user.id) or is_admin(callback.from_user.id):
        await db.execute("UPDATE users SET subscribed=1 WHERE id=?", (callback.from_user.id,))
        await callback.answer("Подписка подтверждена.")
        try:
            await callback.message.edit_text("Подписка подтверждена.")
        except Exception:
            pass
        await send_main_menu(callback.message)
    else:
        await callback.answer("Подписка не найдена. Сначала подпишитесь на канал.", show_alert=True)


@router.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery):
    if await guarded(callback):
        return
    if not is_admin(callback.from_user.id) and not await subscription_required(callback.from_user.id):
        await require_subscription_message(callback.message)
        return
    await callback.answer()
    await send_main_menu(callback.message)


@router.callback_query(F.data == "categories")
async def categories(callback: CallbackQuery):
    if await guarded(callback):
        return
    if not is_admin(callback.from_user.id) and not await subscription_required(callback.from_user.id):
        await require_subscription_message(callback.message)
        return
    text = "Выберите категорию:"
    photo = await db.setting("category_photo") or await db.setting("main_photo")
    kb = await categories_keyboard()
    await callback.answer()
    try:
        if photo:
            await callback.message.edit_media(
                InputMediaPhoto(media=photo, caption=text),
                reply_markup=kb,
            )
        else:
            await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        await callback.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cat:"))
async def category_open(callback: CallbackQuery):
    if await guarded(callback):
        return
    category_id = int(callback.data.split(":")[1])
    text = await category_text(category_id)
    if not text:
        await callback.answer("Категория не найдена.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=await category_keyboard(category_id))


@router.callback_query(F.data.startswith("freecat:"))
async def free_category(callback: CallbackQuery):
    if await guarded(callback):
        return
    category_id = int(callback.data.split(":")[1])
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (category_id,))
    if not c or not c["is_free"]:
        await callback.answer("Категория недоступна.", show_alert=True)
        return
    if await db.setting("free_enabled", "1") != "1":
        await callback.answer("Бесплатные выдачи временно отключены.", show_alert=True)
        return

    last = await db.fetchone(
        "SELECT claimed_at FROM free_claims WHERE user_id=? AND category_id=? "
        "ORDER BY claimed_at DESC LIMIT 1",
        (callback.from_user.id, category_id),
    )
    if last:
        next_time = dt_from_iso(last["claimed_at"]) + timedelta(seconds=c["cooldown_seconds"])
        if datetime.now(timezone.utc) < next_time:
            left = next_time - datetime.now(timezone.utc)
            hours = int(left.total_seconds() // 3600)
            minutes = int((left.total_seconds() % 3600) // 60)
            await callback.answer(f"Следующее получение доступно через {hours} ч. {minutes:02d} мин.", show_alert=True)
            return

    videos = await db.fetchone("SELECT COUNT(*) AS n FROM videos WHERE category_id=?", (category_id,))
    if videos["n"] == 0:
        await callback.answer("В этой категории пока нет видео.", show_alert=True)
        return

    await db.execute(
        "INSERT INTO free_claims(user_id,category_id,claimed_at) VALUES(?,?,?)",
        (callback.from_user.id, category_id, utcnow()),
    )
    await db.execute(
        "INSERT INTO purchases(user_id,category_id,price,payment_method,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (callback.from_user.id, category_id, 0, "FREE", "confirmed", utcnow()),
    )
    await callback.answer("Выдача началась.")
    await deliver_category(callback.from_user.id, category_id, "FREE")


@router.callback_query(F.data == "free")
async def free_menu(callback: CallbackQuery):
    if await guarded(callback):
        return
    cats = await db.fetchall("SELECT * FROM categories WHERE is_free=1 AND enabled=1")
    if not cats:
        await callback.answer("Бесплатных категорий сейчас нет.", show_alert=True)
        return
    rows = []
    for c in cats:
        rows.append([InlineKeyboardButton(text=c["name"], callback_data=f"freecat:{c['id']}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
    await callback.answer()
    await callback.message.edit_text("Бесплатные видео:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("rub:"))
async def rub_buy(callback: CallbackQuery, state: FSMContext):
    if await guarded(callback):
        return
    category_id = int(callback.data.split(":")[1])
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (category_id,))
    if not c or c["is_free"]:
        await callback.answer("Категория недоступна.", show_alert=True)
        return
    if await db.setting("purchases_enabled", "1") != "1":
        await callback.answer("Покупки временно отключены.", show_alert=True)
        return

    details = await db.setting("payment_details")
    recipient = await db.setting("recipient_name")
    if recipient:
        details += f"\nПолучатель: {recipient}"
    text = (await db.setting("payment_text")).format(
        price=c["rub_price"], details=details
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Я оплатил", callback_data=f"paid:{category_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"cat:{category_id}")],
    ])
    photo = await db.setting("payment_photo") or await db.setting("main_photo")
    await callback.answer()
    if photo:
        try:
            await callback.message.edit_media(
                InputMediaPhoto(media=photo, caption=text),
                reply_markup=kb,
            )
            return
        except Exception:
            pass
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("paid:"))
async def paid(callback: CallbackQuery, state: FSMContext):
    if await guarded(callback):
        return
    category_id = int(callback.data.split(":")[1])
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (category_id,))
    if not c:
        await callback.answer("Категория не найдена.", show_alert=True)
        return
    await state.set_state(BuyStates.waiting_receipt)
    await state.update_data(category_id=category_id)
    await callback.answer()
    await callback.message.edit_text(
        "Отправьте чек одним сообщением — фотографией или документом.\n\n"
        "Для отмены используйте /start."
    )


@router.message(BuyStates.waiting_receipt, F.photo)
async def receipt_photo(message: Message, state: FSMContext):
    if await guarded(message):
        return
    data = await state.get_data()
    category_id = data.get("category_id")
    if not category_id:
        await state.clear()
        await message.answer("Сессия покупки завершена. Начните заново.")
        return
    file_id = message.photo[-1].file_id
    await create_rub_request(message, category_id, "photo", file_id, state)


@router.message(BuyStates.waiting_receipt, F.document)
async def receipt_document(message: Message, state: FSMContext):
    if await guarded(message):
        return
    data = await state.get_data()
    category_id = data.get("category_id")
    if not category_id:
        await state.clear()
        await message.answer("Сессия покупки завершена. Начните заново.")
        return
    await create_rub_request(message, category_id, "document", message.document.file_id, state)


async def create_rub_request(message, category_id, receipt_type, file_id, state):
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (category_id,))
    request = await db.execute(
        "INSERT INTO payment_requests(user_id,category_id,amount,payment_method,receipt_type,receipt_file_id,status,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (message.from_user.id, category_id, c["rub_price"], "RUB", receipt_type, file_id, "pending", utcnow()),
    )
    request_id = request.lastrowid
    await state.clear()

    username = f"@{message.from_user.username}" if message.from_user.username else "нет"
    admin_text = (
        "<b>Новая заявка на покупку</b>\n\n"
        f"ID заявки: <code>{request_id}</code>\n"
        f"Категория: {escape(c['name'])}\n"
        f"Цена: {c['rub_price']} ₽\n"
        f"Пользователь: {escape(username)}\n"
        f"ID: <code>{message.from_user.id}</code>\n"
        "Чек: прикреплён"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Выдать", callback_data=f"req_ok:{request_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"req_no:{request_id}")],
    ])
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, admin_text, reply_markup=kb)
            if receipt_type == "photo":
                await bot.send_photo(admin_id, file_id)
            else:
                await bot.send_document(admin_id, file_id)
        except Exception as e:
            logger.warning("Cannot notify admin %s: %s", admin_id, e)
    await message.answer("Заявка отправлена администратору. Ожидайте подтверждения.")


@router.callback_query(F.data.startswith("req_ok:"))
async def request_approve(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    request_id = int(callback.data.split(":")[1])
    req = await db.fetchone("SELECT * FROM payment_requests WHERE id=?", (request_id,))
    if not req or req["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (req["category_id"],))
    await db.execute(
        "UPDATE payment_requests SET status='approved',handled_at=?,handled_by=? WHERE id=?",
        (utcnow(), callback.from_user.id, request_id),
    )
    await db.execute(
        "INSERT INTO purchases(user_id,category_id,price,payment_method,status,created_at,confirmed_at,confirmed_by) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (req["user_id"], req["category_id"], req["amount"], "RUB", "confirmed", utcnow(), utcnow(), callback.from_user.id),
    )
    await log_action(callback.from_user.id, "approve_payment", f"request={request_id}")
    await callback.answer("Оплата подтверждена.")
    try:
        await bot.send_message(req["user_id"], "Оплата успешно подтверждена.\nНачинаем выдачу видео.")
        await deliver_category(req["user_id"], req["category_id"], "RUB", status_text=True)
    except Exception as e:
        logger.warning("Could not deliver approved request: %s", e)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("req_no:"))
async def request_reject(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    request_id = int(callback.data.split(":")[1])
    req = await db.fetchone("SELECT * FROM payment_requests WHERE id=?", (request_id,))
    if not req or req["status"] != "pending":
        await callback.answer("Заявка уже обработана.", show_alert=True)
        return
    await db.execute(
        "UPDATE payment_requests SET status='rejected',handled_at=?,handled_by=? WHERE id=?",
        (utcnow(), callback.from_user.id, request_id),
    )
    await log_action(callback.from_user.id, "reject_payment", f"request={request_id}")
    await callback.answer("Заявка отменена.")
    try:
        await bot.send_message(req["user_id"], "Оплата не подтверждена.")
    except Exception:
        pass
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("stars:"))
async def stars_buy(callback: CallbackQuery):
    if await guarded(callback):
        return
    category_id = int(callback.data.split(":")[1])
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (category_id,))
    if not c or c["is_free"]:
        await callback.answer("Категория недоступна.", show_alert=True)
        return
    if await db.setting("stars_enabled", "1") != "1":
        await callback.answer("Покупки за Stars временно отключены.", show_alert=True)
        return

    admin_username = (await db.setting("admin_username", ADMIN_USERNAME)).lstrip("@")
    text = (
        "<b>Покупка за Stars</b>\n\n"
        f"Категория: {escape(c['name'])}\n"
        f"Цена: {c['stars_price']} Stars\n\n"
        "Нажмите кнопку ниже. Оплата не проводится внутри бота: "
        "администратор самостоятельно проверяет получение Stars."
    )
    prepared = (
        f"Здравствуйте! Хотел бы купить категорию «{c['name']}» за {c['stars_price']} Stars.\n\n"
        f"Мой Telegram ID: {callback.from_user.id}\n"
        f"Категория: {c['name']}\n"
        f"Цена: {c['stars_price']} Stars"
    )
    url = f"https://t.me/{admin_username}?text={quote(prepared)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить за Stars", url=url)],
        [InlineKeyboardButton(text="Отмена", callback_data=f"cat:{category_id}")],
    ])
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "my_purchases")
async def my_purchases(callback: CallbackQuery):
    if await guarded(callback):
        return
    rows = await db.fetchall(
        "SELECT p.*, c.name FROM purchases p JOIN categories c ON c.id=p.category_id "
        "WHERE p.user_id=? ORDER BY p.id DESC LIMIT 20",
        (callback.from_user.id,),
    )
    if not rows:
        text = "У вас пока нет покупок."
    else:
        parts = ["<b>Мои покупки</b>\n"]
        for p in rows:
            parts.append(
                f"#{p['id']} · {escape(p['name'])} · {p['payment_method']} · {p['status']}"
            )
        text = "\n".join(parts)
    await callback.answer()
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="back_main")]
        ])
    )


@router.callback_query(F.data == "help")
async def help_menu(callback: CallbackQuery):
    if await guarded(callback):
        return
    await callback.answer()
    await callback.message.edit_text(
        await db.setting("help_text"),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="back_main")]
        ])
    )


# ============================================================
# ADMIN MENU
# ============================================================

def admin_only(callback_or_message):
    return is_admin(callback_or_message.from_user.id)


async def admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика", callback_data="admin:stats"),
         InlineKeyboardButton(text="Пользователи", callback_data="admin:users")],
        [InlineKeyboardButton(text="Поиск пользователя", callback_data="admin:search"),
         InlineKeyboardButton(text="Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="Категории", callback_data="admin:categories"),
         InlineKeyboardButton(text="Видео", callback_data="admin:videos")],
        [InlineKeyboardButton(text="Заявки", callback_data="admin:requests"),
         InlineKeyboardButton(text="Выдать категорию", callback_data="admin:grant")],
        [InlineKeyboardButton(text="Заблокированные", callback_data="admin:banned"),
         InlineKeyboardButton(text="Настройки", callback_data="admin:settings")],
        [InlineKeyboardButton(text="Журнал действий", callback_data="admin:logs")],
        [InlineKeyboardButton(text="Резервная копия", callback_data="admin:backup"),
         InlineKeyboardButton(text="Экспорт CSV", callback_data="admin:csv")],
        [InlineKeyboardButton(text="Тестовая выдача", callback_data="admin:test")],
    ])


@router.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Недоступно.")
        return
    await message.answer("<b>Админ-панель</b>", reply_markup=await admin_keyboard())


@router.callback_query(F.data == "admin:menu")
async def admin_menu(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text("<b>Админ-панель</b>", reply_markup=await admin_keyboard())


@router.callback_query(F.data.startswith("admin:"))
async def admin_sections(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    section = callback.data.split(":")[1]
    await callback.answer()

    if section == "stats":
        await show_stats(callback.message)
    elif section == "users":
        await show_users(callback.message)
    elif section == "search":
        await state.set_state(AdminStates.search_user)
        await callback.message.edit_text("Введите Telegram ID пользователя.", reply_markup=back_admin_kb())
    elif section == "broadcast":
        await state.set_state(AdminStates.broadcast_text)
        await callback.message.edit_text("Отправьте текст рассылки.\nДля фото с подписью сначала отправьте фото с подписью.", reply_markup=back_admin_kb())
    elif section == "categories":
        await show_admin_categories(callback.message)
    elif section == "videos":
        await show_admin_videos(callback.message)
    elif section == "requests":
        await show_requests(callback.message)
    elif section == "grant":
        await state.set_state(AdminStates.grant_user)
        await callback.message.edit_text("Введите Telegram ID пользователя для выдачи.", reply_markup=back_admin_kb())
    elif section == "banned":
        await show_banned(callback.message)
    elif section == "settings":
        await show_settings(callback.message)
    elif section == "logs":
        await show_logs(callback.message)
    elif section == "backup":
        await send_backup(callback.message, callback.from_user.id)
    elif section == "csv":
        await send_stats_csv(callback.message)
    elif section == "test":
        await callback.message.edit_text(
            "Тестовая выдача: выберите категорию.",
            reply_markup=await admin_category_keyboard("testcat")
        )


def back_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Назад в админ-панель", callback_data="admin:menu")]
    ])


async def show_users(message):
    total = (await db.fetchone("SELECT COUNT(*) n FROM users"))["n"]
    blocked = (await db.fetchone("SELECT COUNT(*) n FROM users WHERE blocked=1"))["n"]
    today = (await db.fetchone(
        "SELECT COUNT(*) n FROM users WHERE first_seen >= ?",
        ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),)
    ))["n"]
    week = (await db.fetchone(
        "SELECT COUNT(*) n FROM users WHERE first_seen >= ?",
        ((datetime.now(timezone.utc)-timedelta(days=7)).isoformat(),)
    ))["n"]
    text = f"<b>Пользователи</b>\n\nВсего: {total}\nАктивные: {total-blocked}\nЗаблокированные: {blocked}\nНовые за 24 часа: {today}\nНовые за 7 дней: {week}"
    await message.edit_text(text, reply_markup=back_admin_kb())


async def show_banned(message):
    rows = await db.fetchall("SELECT id,username,first_name FROM users WHERE blocked=1 ORDER BY id DESC LIMIT 50")
    if not rows:
        text = "Заблокированных пользователей нет."
    else:
        text = "<b>Заблокированные</b>\n\n" + "\n".join(
            f"<code>{r['id']}</code> · @{escape(r['username']) if r['username'] else 'нет'} · {escape(r['first_name'])}"
            for r in rows
        )
    await message.edit_text(text, reply_markup=back_admin_kb())


async def show_requests(message):
    rows = await db.fetchall(
        "SELECT r.*, c.name FROM payment_requests r JOIN categories c ON c.id=r.category_id "
        "WHERE r.status='pending' ORDER BY r.id DESC LIMIT 30"
    )
    if not rows:
        text = "Ожидающих заявок нет."
        await message.edit_text(text, reply_markup=back_admin_kb())
        return
    for r in rows:
        user = await db.fetchone("SELECT username,first_name FROM users WHERE id=?", (r["user_id"],))
        uname = f"@{user['username']}" if user and user["username"] else "нет"
        text = (
            f"<b>Заявка #{r['id']}</b>\n"
            f"Пользователь: {escape(uname)}\n"
            f"ID: <code>{r['user_id']}</code>\n"
            f"Категория: {escape(r['name'])}\n"
            f"Сумма: {r['amount']} ₽\n"
            f"Дата: {r['created_at']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Выдать", callback_data=f"req_ok:{r['id']}"),
             InlineKeyboardButton(text="Отмена", callback_data=f"req_no:{r['id']}")]
        ])
        await message.answer(text, reply_markup=kb)
    await message.answer("Конец списка.", reply_markup=back_admin_kb())


async def show_admin_categories(message):
    rows = await db.fetchall(
        "SELECT c.*, COUNT(v.id) video_count FROM categories c "
        "LEFT JOIN videos v ON v.category_id=c.id GROUP BY c.id ORDER BY c.id"
    )
    kb_rows = []
    for c in rows:
        kb_rows.append([InlineKeyboardButton(
            text=f"{c['name']} · {c['video_count']} видео",
            callback_data=f"acat:{c['id']}"
        )])
    kb_rows.append([InlineKeyboardButton(text="Добавить категорию", callback_data="addcat")])
    kb_rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:menu")])
    await message.edit_text("<b>Управление категориями</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


async def admin_category_keyboard(prefix="cat"):
    rows = await db.fetchall("SELECT * FROM categories ORDER BY id")
    buttons = [[InlineKeyboardButton(text=c["name"], callback_data=f"{prefix}:{c['id']}")] for c in rows]
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def show_admin_videos(message):
    rows = await db.fetchall(
        "SELECT c.id,c.name,COUNT(v.id) count FROM categories c "
        "LEFT JOIN videos v ON v.category_id=c.id GROUP BY c.id ORDER BY c.id"
    )
    text = "<b>Видео</b>\n\n" + "\n".join(f"{r['name']}: {r['count']}" for r in rows)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить видео", callback_data="video:add")],
        [InlineKeyboardButton(text="Список видео", callback_data="video:list")],
        [InlineKeyboardButton(text="Удалить видео", callback_data="video:delete")],
        [InlineKeyboardButton(text="Назад", callback_data="admin:menu")]
    ])
    await message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data.startswith("acat:"))
async def admin_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    cid = int(callback.data.split(":")[1])
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (cid,))
    if not c:
        await callback.answer("Не найдена.", show_alert=True)
        return
    text = (
        f"<b>{escape(c['name'])}</b>\n\n"
        f"Цена RUB: {c['rub_price']} ₽\n"
        f"Цена Stars: {c['stars_price']}\n"
        f"Бесплатная: {'да' if c['is_free'] else 'нет'}\n"
        f"Cooldown: {c['cooldown_seconds']} сек."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить название", callback_data=f"editname:{cid}")],
        [InlineKeyboardButton(text="Цена RUB", callback_data=f"editrub:{cid}"),
         InlineKeyboardButton(text="Цена Stars", callback_data=f"editstars:{cid}")],
        [InlineKeyboardButton(text="Бесплатность", callback_data=f"editfree:{cid}")],
        [InlineKeyboardButton(text="Cooldown", callback_data=f"editcool:{cid}")],
        [InlineKeyboardButton(text="Добавить видео", callback_data=f"addvidcat:{cid}")],
        [InlineKeyboardButton(text="Удалить категорию", callback_data=f"delcat:{cid}")],
        [InlineKeyboardButton(text="Назад", callback_data="admin:categories")]
    ])
    await callback.answer()
    await callback.message.edit_text(text, reply_markup=kb)


@router.callback_query(F.data == "addcat")
async def addcat_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.add_category_name)
    await callback.answer()
    await callback.message.edit_text("Введите название новой категории.", reply_markup=back_admin_kb())


@router.message(AdminStates.add_category_name)
async def addcat_name(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.update_data(name=message.text.strip())
    await state.set_state(AdminStates.add_category_rub)
    await message.answer("Введите цену в рублях. Для 0 введите 0.")


@router.message(AdminStates.add_category_rub)
async def addcat_rub(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: value = int(message.text)
    except ValueError:
        await message.answer("Введите целое число."); return
    await state.update_data(rub=value)
    await state.set_state(AdminStates.add_category_stars)
    await message.answer("Введите цену в Stars. Для 0 введите 0.")


@router.message(AdminStates.add_category_stars)
async def addcat_stars(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: value = int(message.text)
    except ValueError:
        await message.answer("Введите целое число."); return
    await state.update_data(stars=value)
    await state.set_state(AdminStates.add_category_free)
    await message.answer("Бесплатная категория? Введите да или нет.")


@router.message(AdminStates.add_category_free)
async def addcat_free(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    val = message.text.strip().lower() in {"да","yes","1"}
    await state.update_data(free=int(val))
    await state.set_state(AdminStates.add_category_cooldown)
    await message.answer("Введите cooldown в секундах. Например, 86400 для 24 часов.")


@router.message(AdminStates.add_category_cooldown)
async def addcat_cooldown(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: cooldown = int(message.text)
    except ValueError:
        await message.answer("Введите целое число."); return
    data = await state.get_data()
    try:
        await db.execute(
            "INSERT INTO categories(name,rub_price,stars_price,is_free,cooldown_seconds,created_at) VALUES(?,?,?,?,?,?)",
            (data["name"], data["rub"], data["stars"], data["free"], cooldown, utcnow()),
        )
    except sqlite3.IntegrityError:
        await message.answer("Категория с таким названием уже существует.")
        return
    await log_action(message.from_user.id, "add_category", data["name"])
    await state.clear()
    await message.answer("Категория создана.", reply_markup=back_admin_kb())


async def edit_category_numeric(message, state, field):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    cid = data["category_id"]
    try: value = int(message.text)
    except ValueError:
        await message.answer("Введите целое число."); return
    await db.execute(f"UPDATE categories SET {field}=? WHERE id=?", (value, cid))
    await log_action(message.from_user.id, f"edit_{field}", f"category={cid},value={value}")
    await state.clear()
    await message.answer("Изменение сохранено.", reply_markup=back_admin_kb())


@router.callback_query(F.data.startswith("editname:"))
async def editname_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.update_data(category_id=int(callback.data.split(":")[1]))
    await state.set_state(AdminStates.edit_category_name)
    await callback.answer()
    await callback.message.edit_text("Введите новое название.")


@router.message(AdminStates.edit_category_name)
async def editname(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    try:
        await db.execute("UPDATE categories SET name=? WHERE id=?", (message.text.strip(), data["category_id"]))
    except sqlite3.IntegrityError:
        await message.answer("Такое название уже используется."); return
    await log_action(message.from_user.id, "edit_category_name", f"category={data['category_id']}")
    await state.clear()
    await message.answer("Название изменено.", reply_markup=back_admin_kb())


@router.callback_query(F.data.startswith("editrub:"))
async def editrub_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.update_data(category_id=int(callback.data.split(":")[1]))
    await state.set_state(AdminStates.edit_category_rub)
    await callback.answer()
    await callback.message.edit_text("Введите новую цену RUB.")


@router.message(AdminStates.edit_category_rub)
async def editrub(message: Message, state: FSMContext):
    await edit_category_numeric(message, state, "rub_price")


@router.callback_query(F.data.startswith("editstars:"))
async def editstars_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.update_data(category_id=int(callback.data.split(":")[1]))
    await state.set_state(AdminStates.edit_category_stars)
    await callback.answer()
    await callback.message.edit_text("Введите новую цену Stars.")


@router.message(AdminStates.edit_category_stars)
async def editstars(message: Message, state: FSMContext):
    await edit_category_numeric(message, state, "stars_price")


@router.callback_query(F.data.startswith("editfree:"))
async def editfree_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.update_data(category_id=int(callback.data.split(":")[1]))
    await state.set_state(AdminStates.edit_category_free)
    await callback.answer()
    await callback.message.edit_text("Введите да или нет.")


@router.message(AdminStates.edit_category_free)
async def editfree(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    value = int(message.text.strip().lower() in {"да","yes","1"})
    await db.execute("UPDATE categories SET is_free=? WHERE id=?", (value, data["category_id"]))
    await state.clear()
    await message.answer("Сохранено.", reply_markup=back_admin_kb())


@router.callback_query(F.data.startswith("editcool:"))
async def editcool_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.update_data(category_id=int(callback.data.split(":")[1]))
    await state.set_state(AdminStates.edit_category_cooldown)
    await callback.answer()
    await callback.message.edit_text("Введите cooldown в секундах.")


@router.message(AdminStates.edit_category_cooldown)
async def editcool(message: Message, state: FSMContext):
    await edit_category_numeric(message, state, "cooldown_seconds")


@router.callback_query(F.data.startswith("delcat:"))
async def delete_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    cid = int(callback.data.split(":")[1])
    await db.execute("DELETE FROM categories WHERE id=?", (cid,))
    await log_action(callback.from_user.id, "delete_category", f"category={cid}")
    await callback.answer("Категория удалена.")
    await show_admin_categories(callback.message)


@router.callback_query(F.data == "video:add")
async def video_add_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.add_video)
    await callback.answer()
    await callback.message.edit_text(
        "Выберите категорию, затем отправьте видео.",
        reply_markup=await admin_category_keyboard("addvid")
    )


@router.callback_query(F.data.startswith("addvid:"))
async def video_add_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    cid = int(callback.data.split(":")[1])
    await state.update_data(category_id=cid)
    await state.set_state(AdminStates.add_video)
    await callback.answer()
    await callback.message.edit_text("Теперь отправьте Telegram-видео.")


@router.callback_query(F.data.startswith("addvidcat:"))
async def video_add_from_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    cid = int(callback.data.split(":")[1])
    await state.update_data(category_id=cid)
    await state.set_state(AdminStates.add_video)
    await callback.answer()
    await callback.message.edit_text("Отправьте Telegram-видео.")


@router.message(AdminStates.add_video, F.video)
async def video_add_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    cid = data.get("category_id")
    if not cid:
        await message.answer("Категория не выбрана."); return
    await db.execute(
        "INSERT INTO videos(category_id,file_id,added_at) VALUES(?,?,?)",
        (cid, message.video.file_id, utcnow()),
    )
    await log_action(message.from_user.id, "add_video", f"category={cid}")
    await message.answer("Видео добавлено. Можно отправить следующее видео.")
    # State intentionally remains active for batch upload.


@router.callback_query(F.data.startswith("testcat:"))
async def test_category(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    cid = int(callback.data.split(":")[1])
    videos = await db.fetchall("SELECT id,file_id FROM videos WHERE category_id=? ORDER BY id", (cid,))
    if not videos:
        await callback.answer("В категории нет видео.", show_alert=True)
        return
    await callback.answer("Отправляю тестовую выдачу.")
    for video in videos:
        try:
            await bot.send_video(callback.from_user.id, video["file_id"])
        except Exception as e:
            logger.warning("Test delivery failed: %s", e)
        await asyncio.sleep(1)


@router.callback_query(F.data.startswith("listvid:"))
async def list_category_videos(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    cid = int(callback.data.split(":")[1])
    c = await db.fetchone("SELECT name FROM categories WHERE id=?", (cid,))
    rows = await db.fetchall(
        "SELECT id,added_at,deliveries FROM videos WHERE category_id=? ORDER BY id DESC LIMIT 100",
        (cid,),
    )
    if not c:
        await callback.answer("Категория не найдена.", show_alert=True)
        return
    if not rows:
        text = f"<b>{escape(c['name'])}</b>\n\nВидео нет."
    else:
        text = f"<b>{escape(c['name'])}</b>\n\n" + "\n".join(
            f"ID {r['id']} · добавлено {r['added_at']} · выдач {r['deliveries']}"
            for r in rows
        )
    await callback.answer()
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data="admin:videos")]
        ])
    )


@router.callback_query(F.data == "video:list")
async def video_list_start(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Недоступно.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "Выберите категорию:",
        reply_markup=await admin_category_keyboard("listvid")
    )


@router.callback_query(F.data == "video:delete")
async def video_delete_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.delete_video)
    await callback.answer()
    await callback.message.edit_text("Введите ID видео для удаления.", reply_markup=back_admin_kb())


@router.message(AdminStates.delete_video)
async def video_delete_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: vid = int(message.text)
    except ValueError:
        await message.answer("Введите числовой ID видео."); return
    row = await db.fetchone("SELECT id FROM videos WHERE id=?", (vid,))
    if not row:
        await message.answer("Видео не найдено."); return
    await db.execute("DELETE FROM videos WHERE id=?", (vid,))
    await log_action(message.from_user.id, "delete_video", f"video={vid}")
    await state.clear()
    await message.answer("Видео удалено.", reply_markup=back_admin_kb())


# ============================================================
# ADMIN SEARCH / BAN / GRANT
# ============================================================

@router.message(AdminStates.search_user)
async def search_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: uid = int(message.text)
    except ValueError:
        await message.answer("Введите числовой Telegram ID."); return
    u = await db.fetchone("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        await message.answer("Пользователь не найден.", reply_markup=back_admin_kb())
        await state.clear()
        return
    bought = await db.fetchone("SELECT COUNT(*) n FROM purchases WHERE user_id=? AND status='confirmed'", (uid,))
    rub = await db.fetchone("SELECT COALESCE(SUM(price),0) n FROM purchases WHERE user_id=? AND payment_method='RUB' AND status='confirmed'", (uid,))
    stars = await db.fetchone("SELECT COUNT(*) n FROM purchases WHERE user_id=? AND payment_method='STARS' AND status='confirmed'", (uid,))
    vids = await db.fetchone("SELECT COUNT(*) n FROM deliveries WHERE user_id=?", (uid,))
    free = await db.fetchone("SELECT COUNT(*) n FROM free_claims WHERE user_id=?", (uid,))
    text = (
        "<b>Информация о пользователе</b>\n\n"
        f"Telegram ID: <code>{u['id']}</code>\n"
        f"Username: @{escape(u['username']) if u['username'] else 'нет'}\n"
        f"Имя: {escape(u['first_name'])}\n"
        f"Первый запуск: {u['first_seen']}\n"
        f"Последняя активность: {u['last_seen']}\n"
        f"Заблокирован: {'да' if u['blocked'] else 'нет'}\n"
        f"Купленных категорий: {bought['n']}\n"
        f"Потрачено RUB: {rub['n']} ₽\n"
        f"Покупок Stars: {stars['n']}\n"
        f"Видео получено: {vids['n']}\n"
        f"Бесплатных выдач: {free['n']}"
    )
    buttons = [
        [InlineKeyboardButton(text="Выдать категорию", callback_data=f"grantuser:{uid}")],
    ]
    if u["blocked"]:
        buttons.append([InlineKeyboardButton(text="Разбанить", callback_data=f"unban:{uid}")])
    else:
        buttons.append([InlineKeyboardButton(text="Забанить", callback_data=f"ban:{uid}")])
    buttons.append([InlineKeyboardButton(text="Назад", callback_data="admin:menu")])
    await state.clear()
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("ban:"))
async def ban_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    uid = int(callback.data.split(":")[1])
    if uid in ADMIN_IDS:
        await callback.answer("Администратора блокировать нельзя.", show_alert=True); return
    await db.execute("UPDATE users SET blocked=1 WHERE id=?", (uid,))
    await log_action(callback.from_user.id, "ban_user", str(uid))
    await callback.answer("Пользователь заблокирован.")
    await callback.message.edit_reply_markup(reply_markup=back_admin_kb())


@router.callback_query(F.data.startswith("unban:"))
async def unban_user(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    uid = int(callback.data.split(":")[1])
    await db.execute("UPDATE users SET blocked=0 WHERE id=?", (uid,))
    await log_action(callback.from_user.id, "unban_user", str(uid))
    await callback.answer("Пользователь разблокирован.")
    await callback.message.edit_reply_markup(reply_markup=back_admin_kb())


@router.callback_query(F.data.startswith("grantuser:"))
async def grant_user_from_search(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    uid = int(callback.data.split(":")[1])
    await state.update_data(grant_user=uid)
    await state.set_state(AdminStates.grant_category)
    await callback.answer()
    await callback.message.edit_text("Выберите категорию:", reply_markup=await admin_category_keyboard("grantcat"))


@router.message(AdminStates.grant_user)
async def grant_user_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try: uid = int(message.text)
    except ValueError:
        await message.answer("Введите числовой Telegram ID."); return
    u = await db.fetchone("SELECT id FROM users WHERE id=?", (uid,))
    if not u:
        await message.answer("Пользователь не найден."); return
    await state.update_data(grant_user=uid)
    await state.set_state(AdminStates.grant_category)
    await message.answer("Выберите категорию:", reply_markup=await admin_category_keyboard("grantcat"))


@router.callback_query(F.data.startswith("grantcat:"))
async def grant_category(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    cid = int(callback.data.split(":")[1])
    data = await state.get_data()
    uid = data.get("grant_user")
    if not uid:
        await callback.answer("Пользователь не указан.", show_alert=True); return
    c = await db.fetchone("SELECT * FROM categories WHERE id=?", (cid,))
    if not c:
        await callback.answer("Категория не найдена.", show_alert=True); return

    await db.execute(
        "INSERT INTO purchases(user_id,category_id,price,payment_method,status,created_at,confirmed_at,confirmed_by) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (uid, cid, c["stars_price"], "STARS", "confirmed", utcnow(), utcnow(), callback.from_user.id),
    )
    await log_action(callback.from_user.id, "grant_category", f"user={uid},category={cid}")
    await state.clear()
    await callback.answer("Категория выдана.")
    try:
        await bot.send_message(uid, f"Администратор выдал вам категорию «{c['name']}».\nНачинаем выдачу видео.")
        await deliver_category(uid, cid, "STARS")
    except Exception as e:
        logger.warning("Grant delivery failed: %s", e)
    await callback.message.edit_text("Выдача выполнена.", reply_markup=back_admin_kb())


# ============================================================
# SETTINGS
# ============================================================

SETTING_LABELS = {
    "bot_name": "Название бота",
    "main_text": "Текст главного меню",
    "subscription_text": "Текст проверки подписки",
    "help_text": "Текст помощи",
    "payment_details": "Реквизиты",
    "recipient_name": "Имя получателя",
    "channel_id": "ID/username канала",
    "channel_url": "Ссылка на канал",
    "admin_username": "Username администратора для Stars",
    "main_photo": "Главное фото",
    "category_photo": "Фото категорий",
    "payment_photo": "Фото оплаты",
    "success_photo": "Фото успешной покупки",
    "admin_photo": "Фото админ-панели",
}


async def show_settings(message):
    rows = []
    for key, label in SETTING_LABELS.items():
        rows.append([InlineKeyboardButton(text=label, callback_data=f"set:{key}")])
    rows += [
        [InlineKeyboardButton(text="Переключатели", callback_data="toggles")],
        [InlineKeyboardButton(text="Назад", callback_data="admin:menu")]
    ]
    await message.edit_text("<b>Настройки</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("set:"))
async def setting_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    key = callback.data.split(":", 1)[1]
    await state.update_data(setting_key=key)
    await state.set_state(AdminStates.setting_value)
    current = await db.setting(key)
    instruction = "Отправьте новое значение."
    if key.endswith("_photo"):
        instruction = "Отправьте новую фотографию. Для удаления фото отправьте /clear."
    await callback.answer()
    await callback.message.edit_text(f"<b>{SETTING_LABELS.get(key,key)}</b>\n\nТекущее значение:\n{escape(current)}\n\n{instruction}")


@router.message(AdminStates.setting_value)
async def setting_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data()
    key = data["setting_key"]
    if key.endswith("_photo"):
        if message.text == "/clear":
            await db.set_setting(key, "")
        elif message.photo:
            await db.set_setting(key, message.photo[-1].file_id)
        else:
            await message.answer("Нужна фотография или /clear."); return
    else:
        value = message.text or ""
        await db.set_setting(key, value)
    await log_action(message.from_user.id, "setting_changed", key)
    await state.clear()
    await message.answer("Настройка сохранена.", reply_markup=back_admin_kb())


@router.callback_query(F.data == "toggles")
async def toggles(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    values = {}
    for k in ("purchases_enabled","stars_enabled","free_enabled","registration_enabled","maintenance"):
        values[k] = await db.setting(k, "1")
    labels = {
        "purchases_enabled": "Покупки",
        "stars_enabled": "Stars",
        "free_enabled": "Бесплатные выдачи",
        "registration_enabled": "Регистрация",
        "maintenance": "Технический режим",
    }
    rows = []
    for k,v in values.items():
        rows.append([InlineKeyboardButton(
            text=f"{labels[k]}: {'ВКЛ' if v=='1' else 'ВЫКЛ'}",
            callback_data=f"toggle:{k}"
        )])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="admin:settings")])
    await callback.answer()
    await callback.message.edit_text("Переключатели:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("toggle:"))
async def toggle(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    key = callback.data.split(":")[1]
    old = await db.setting(key, "0")
    new = "0" if old == "1" else "1"
    await db.set_setting(key, new)
    await log_action(callback.from_user.id, "toggle", f"{key}={new}")
    await callback.answer("Изменено.")
    await toggles(callback)


# ============================================================
# BROADCAST
# ============================================================

@router.message(AdminStates.broadcast_text)
async def broadcast_receive(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if message.photo:
        caption = message.caption or ""
        await state.update_data(kind="photo", file_id=message.photo[-1].file_id, caption=caption)
    else:
        await state.update_data(kind="text", text=message.text or "")
    await state.clear()
    await run_broadcast(message, message.from_user.id)


async def run_broadcast(source_message, admin_id):
    users = await db.fetchall("SELECT id FROM users WHERE blocked=0")
    # The source message itself is used as the content. Copying it preserves
    # photo/text formatting without downloading files.
    delivered = failed = 0
    for u in users:
        try:
            await bot.copy_message(
                chat_id=u["id"],
                from_chat_id=source_message.chat.id,
                message_id=source_message.message_id,
            )
            delivered += 1
            await asyncio.sleep(0.05)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            try:
                await bot.copy_message(
                    chat_id=u["id"],
                    from_chat_id=source_message.chat.id,
                    message_id=source_message.message_id,
                )
                delivered += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1
            await db.execute("UPDATE users SET blocked=1 WHERE id=?", (u["id"],))
        except Exception:
            failed += 1
    await log_action(admin_id, "broadcast", f"total={len(users)},delivered={delivered},failed={failed}")
    await source_message.answer(
        f"<b>Рассылка завершена.</b>\n\n"
        f"Всего пользователей: {len(users)}\n"
        f"Доставлено: {delivered}\n"
        f"Не доставлено: {failed}\n"
        f"Ошибок: {failed}",
        reply_markup=back_admin_kb(),
    )


# ============================================================
# STATISTICS / BACKUP / EXPORT
# ============================================================

async def show_stats(message):
    now = datetime.now(timezone.utc)
    day = (now - timedelta(days=1)).isoformat()
    week = (now - timedelta(days=7)).isoformat()
    total_users = (await db.fetchone("SELECT COUNT(*) n FROM users"))["n"]
    new24 = (await db.fetchone("SELECT COUNT(*) n FROM users WHERE first_seen>=?", (day,)))["n"]
    new7 = (await db.fetchone("SELECT COUNT(*) n FROM users WHERE first_seen>=?", (week,)))["n"]
    blocked = (await db.fetchone("SELECT COUNT(*) n FROM users WHERE blocked=1"))["n"]
    active = (await db.fetchone("SELECT COUNT(*) n FROM users WHERE last_seen>=?", (week,)))["n"]
    unique_video_users = (await db.fetchone("SELECT COUNT(DISTINCT user_id) n FROM deliveries"))["n"]
    total_deliveries = (await db.fetchone("SELECT COUNT(*) n FROM deliveries"))["n"]
    deliveries24 = (await db.fetchone("SELECT COUNT(*) n FROM deliveries WHERE created_at>=?", (day,)))["n"]
    deliveries7 = (await db.fetchone("SELECT COUNT(*) n FROM deliveries WHERE created_at>=?", (week,)))["n"]
    avg = total_deliveries / unique_video_users if unique_video_users else 0
    buyers = (await db.fetchone("SELECT COUNT(DISTINCT user_id) n FROM purchases WHERE status='confirmed' AND payment_method!='FREE'"))["n"]
    purchases = (await db.fetchone("SELECT COUNT(*) n FROM purchases WHERE status='confirmed' AND payment_method!='FREE'"))["n"]
    rub_sum = (await db.fetchone("SELECT COALESCE(SUM(price),0) n FROM purchases WHERE status='confirmed' AND payment_method='RUB'"))["n"]
    stars_count = (await db.fetchone("SELECT COUNT(*) n FROM purchases WHERE status='confirmed' AND payment_method='STARS'"))["n"]
    free_count = (await db.fetchone("SELECT COUNT(*) n FROM purchases WHERE status='confirmed' AND payment_method='FREE'"))["n"]

    top = await db.fetchall(
        "SELECT v.id, c.name, v.deliveries FROM videos v JOIN categories c ON c.id=v.category_id "
        "ORDER BY v.deliveries DESC, v.id ASC LIMIT 5"
    )
    cats = await db.fetchall(
        "SELECT c.name, COUNT(DISTINCT v.id) video_count, "
        "COALESCE(SUM(v.deliveries),0) deliveries, "
        "COUNT(DISTINCT CASE WHEN p.status='confirmed' THEN p.id END) purchases, "
        "COALESCE(SUM(CASE WHEN p.status='confirmed' AND p.payment_method='RUB' THEN p.price ELSE 0 END),0) income "
        "FROM categories c "
        "LEFT JOIN videos v ON v.category_id=c.id "
        "LEFT JOIN purchases p ON p.category_id=c.id "
        "GROUP BY c.id ORDER BY c.id"
    )
    entered = total_users
    passed = (await db.fetchone("SELECT COUNT(*) n FROM users WHERE subscribed=1"))["n"]
    opened = (await db.fetchone("SELECT COUNT(DISTINCT user_id) n FROM deliveries"))["n"]
    text = (
        f"<b>ПОДРОБНЫЙ ОТЧЁТ</b>\n\n"
        f"На {now.strftime('%d.%m.%Y %H:%M UTC')}\n\n"
        f"<b>Аудитория</b>\n"
        f"Всего пользователей: {total_users}\n"
        f"Новые за 24 часа: {new24}\n"
        f"Новые за 7 дней: {new7}\n"
        f"Активные за 7 дней: {active}\n"
        f"Заблокированные: {blocked}\n\n"
        f"<b>Видео</b>\n"
        f"Получили хотя бы одно видео: {unique_video_users}\n"
        f"Всего выдач: {total_deliveries}\n"
        f"Выдач за 24 часа: {deliveries24}\n"
        f"Выдач за 7 дней: {deliveries7}\n"
        f"Среднее видео на пользователя: {avg:.2f}\n\n"
        f"<b>Продажи</b>\n"
        f"Покупателей: {buyers}\n"
        f"Покупок: {purchases}\n"
        f"Сумма RUB: {rub_sum} ₽\n"
        f"Покупок Stars: {stars_count}\n"
        f"Бесплатных выдач: {free_count}\n\n"
        f"<b>Воронка</b>\n"
        f"Зашли: {entered}\n"
        f"Прошли подписку: {passed}\n"
        f"Получили видео: {opened}\n"
    )
    text += "\n<b>Топ-5 видео</b>\n"
    text += "\n".join(f"#{r['id']} · {escape(r['name'])} · {r['deliveries']} выдач" for r in top) or "Нет данных."
    text += "\n\n<b>Категории</b>\n"
    text += "\n".join(
        f"{escape(c['name'])}: {c['video_count']} видео · {c['deliveries']} выдач · "
        f"{c['purchases']} покупок · {c['income']} ₽"
        for c in cats
    )
    await message.edit_text(text, reply_markup=back_admin_kb())


async def show_logs(message):
    rows = await db.fetchall("SELECT * FROM action_log ORDER BY id DESC LIMIT 30")
    if not rows:
        text = "Журнал пуст."
    else:
        text = "<b>Журнал действий</b>\n\n" + "\n".join(
            f"{r['created_at']} · {r['admin_id']} · {escape(r['action'])} · {escape(r['details'] or '')}"
            for r in rows
        )
    await message.edit_text(text, reply_markup=back_admin_kb())


async def send_backup(message, admin_id):
    path = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    async with db.lock:
        db.conn.commit()
        shutil.copy2(DB_PATH, path)
    await bot.send_document(admin_id, FSInputFile(path), caption="Резервная копия базы данных.")
    os.remove(path)
    await log_action(admin_id, "backup")


async def send_stats_csv(message):
    path = f"stats_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    cats = await db.fetchall(
        "SELECT c.name, COUNT(DISTINCT v.id) videos, COALESCE(SUM(v.deliveries),0) deliveries, "
        "COUNT(DISTINCT CASE WHEN p.status='confirmed' THEN p.id END) purchases, "
        "COALESCE(SUM(CASE WHEN p.status='confirmed' AND p.payment_method='RUB' THEN p.price ELSE 0 END),0) income "
        "FROM categories c LEFT JOIN videos v ON v.category_id=c.id LEFT JOIN purchases p ON p.category_id=c.id "
        "GROUP BY c.id"
    )
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Категория","Видео","Выдачи","Покупки","Доход RUB"])
        for c in cats:
            writer.writerow([c["name"],c["videos"],c["deliveries"],c["purchases"],c["income"]])
    await bot.send_document(message.chat.id, FSInputFile(path), caption="Экспорт статистики.")
    os.remove(path)


# ============================================================
# STARTUP / ERRORS
# ============================================================

async def main():
    global bot
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        raise RuntimeError("Укажите BOT_TOKEN в начале main.py.")

    await db.init()

    bot = Bot(
        BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot started: %s", await db.setting("bot_name", BOT_NAME))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")

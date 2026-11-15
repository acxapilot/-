"""
Telegram-бот студии — один файл. Aiogram 3 · SQLite (встроенный sqlite3) · асинхронно.
Настройки и BOT_TOKEN / ADMIN_IDS — в config.py.
Интерфейс — инлайн-кнопки; навигация правит одно и то же сообщение.
"""
import asyncio
import html
import logging
import secrets
import sqlite3
import sys
from contextlib import closing
from datetime import date
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, CommandStart, Filter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
    TelegramObject,
)

import config

log = logging.getLogger("bot")


# =============================================================================
# 1. БАЗА ДАННЫХ
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    is_blocked INTEGER NOT NULL DEFAULT 0,
    bot_blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'site' CHECK (category IN ('site', 'bot')),
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    features TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL DEFAULT 0,
    demo_url TEXT, media_ids TEXT NOT NULL DEFAULT '',
    is_published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'project' CHECK (kind IN ('project', 'product')),
    category TEXT NOT NULL DEFAULT 'other' CHECK (category IN ('bot', 'site', 'miniapp', 'other')),
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
    features TEXT NOT NULL DEFAULT '', price INTEGER NOT NULL DEFAULT 0,
    license TEXT NOT NULL DEFAULT 'Стандартная', sale_terms TEXT NOT NULL DEFAULT '',
    demo_url TEXT, media_ids TEXT NOT NULL DEFAULT '',
    in_stock INTEGER NOT NULL DEFAULT 1, is_published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS hosting_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, price INTEGER NOT NULL DEFAULT 0,
    cpu TEXT NOT NULL DEFAULT '', ram TEXT NOT NULL DEFAULT '', disk TEXT NOT NULL DEFAULT '',
    traffic TEXT NOT NULL DEFAULT '', period TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '', is_published INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, kind TEXT NOT NULL, item_id INTEGER,
    name TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
    references_ TEXT NOT NULL DEFAULT '', budget TEXT NOT NULL DEFAULT '',
    deadline TEXT NOT NULL DEFAULT '', contact TEXT NOT NULL DEFAULT '',
    price INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new', 'in_progress', 'awaiting_payment', 'done', 'cancelled')),
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE TABLE IF NOT EXISTS payment_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL, user_id INTEGER NOT NULL, method TEXT NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0, proof_type TEXT NOT NULL DEFAULT '',
    proof_file_id TEXT NOT NULL DEFAULT '', proof_text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'rejected')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')), checked_at TEXT
);
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, chat_ref TEXT NOT NULL, invite_url TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS promo_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    discount_type TEXT NOT NULL CHECK (discount_type IN ('percent', 'fixed')),
    amount INTEGER NOT NULL, max_uses INTEGER NOT NULL DEFAULT 0,
    uses INTEGER NOT NULL DEFAULT 0, expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS promo_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_id INTEGER NOT NULL, user_id INTEGER NOT NULL, order_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS support_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL, text TEXT NOT NULL DEFAULT '',
    file_type TEXT NOT NULL DEFAULT '', file_id TEXT NOT NULL DEFAULT '',
    admin_reply TEXT NOT NULL DEFAULT '', is_open INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL, media_type TEXT NOT NULL DEFAULT '', media_id TEXT NOT NULL DEFAULT '',
    button_text TEXT NOT NULL DEFAULT '', button_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'running', 'stopped', 'finished')),
    sent_count INTEGER NOT NULL DEFAULT 0, failed_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS broadcast_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    broadcast_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    ok INTEGER NOT NULL, error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS texts (key TEXT PRIMARY KEY, html TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS text_media (key TEXT PRIMARY KEY, media_ids TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS buttons (
    key TEXT PRIMARY KEY, label TEXT NOT NULL, style TEXT NOT NULL DEFAULT ''
);
"""

# Тексты экранов хранятся как HTML: жирный, ссылки и премиум-эмодзи сохраняются.
DEFAULT_TEXTS = {
    "welcome": "👋 Добро пожаловать в <b>{studio}</b>!\n\n{description}\n\nВыберите раздел ниже.",
    "order_pick": "🛒 <b>Заказать</b>\nЧто нужно разработать?",
    "order_done": "✅ <b>Заявка №{id} принята.</b>\nМы изучим её и свяжемся с вами. Статус — в «👤 Профиль».",
    "templates_pick": "🧩 <b>Шаблоны</b>\nВыберите категорию:",
    "projects_pick": "📁 <b>Готовые проекты</b>\nВыберите категорию:",
    "hosting_pick": "🖥 <b>Хостинг</b>\nВыберите тариф:",
    "support_intro": "💬 <b>Поддержка</b>\nНапишите вопрос. Можно приложить фото, видео или документ.",
    "support_done": "✅ Обращение №{id} отправлено. Ответим здесь же, в чате с ботом.",
    "pay_open": "💳 <b>Оплата заказа №{id}</b>\nК оплате: <b>{price}</b>\n\nВыберите удобный способ оплаты:",
    "pay_method": ("{title}\nСумма: <b>{amount}</b>\n\n<b>Реквизиты / инструкция:</b>\n{details}\n\n"
                   "После оплаты отправьте скриншот чека. Администратор проверит поступление средств и подтвердит оплату."),
    "pay_done": "📨 Подтверждение получено. Уведомим, когда проверим поступление средств.",
    "sub_text": "Чтобы пользоваться ботом, подпишитесь на каналы ниже и нажмите «Проверить подписку».",
}

TEXT_LABELS = {
    "welcome": "Приветствие (/start)",
    "order_pick": "Экран «Заказать»",
    "order_done": "Текст после отправки заявки",
    "templates_pick": "Экран «Шаблоны»",
    "projects_pick": "Экран «Готовые проекты»",
    "hosting_pick": "Экран «Хостинг»",
    "support_intro": "Экран «Уточнить»",
    "support_done": "Текст после обращения",
    "pay_open": "Экран выбора способа оплаты",
    "pay_method": "Экран реквизитов оплаты",
    "pay_done": "Текст после чека",
    "sub_text": "Текст обязательной подписки",
}

# Кнопки, которые можно переименовать и раскрасить: key -> (текст по умолчанию, цвет по умолчанию)
BUTTON_DEFAULTS = {
    "menu_order": ("🛒 Заказать", "success"),
    "menu_templates": ("🧩 Шаблоны", "primary"),
    "menu_projects": ("📁 Готовые проекты", "primary"),
    "menu_profile": ("👤 Профиль", ""),
    "menu_support": ("💬 Уточнить", ""),
    "menu_hosting": ("🖥 Хостинг", "primary"),
    "menu_admin": ("⚙️ Админ-панель", "danger"),
    "order_site": ("🌐 Разработка сайта", "primary"),
    "order_bot": ("🤖 Разработка Telegram-бота", "primary"),
    "buy": ("🛒 Заказать", "success"),
    "discuss": ("💬 Обсудить", ""),
    "pay": ("💳 Оплатить", "success"),
    "submit": ("☑ Отправить заявку", "success"),
    "demo": ("🔗 Демо", ""),
    "cancel": ("❌ Отмена", "danger"),
    "sub_check": ("✅ Проверить подписку", "success"),
}
BUTTON_STYLE_NAMES = {"": "обычная", "primary": "синяя", "success": "зелёная", "danger": "красная"}

ORDER_STATUSES = {
    "new": "🆕 Новая",
    "in_progress": "🛠 В работе",
    "awaiting_payment": "💳 Ожидает оплаты",
    "done": "✅ Завершена",
    "cancelled": "❌ Отменена",
}
ACTIVE_STATUSES = ("new", "in_progress", "awaiting_payment")

# Поля, которые админка может менять (защита от подстановки имён колонок)
ALLOWED = {
    "templates": {"title", "category", "description", "features", "price", "demo_url", "media_ids", "is_published"},
    "products": {"title", "category", "description", "features", "price", "license", "sale_terms",
                 "demo_url", "media_ids", "in_stock", "is_published"},
    "hosting_plans": {"title", "price", "cpu", "ram", "disk", "traffic", "period", "description", "is_published"},
    "subscriptions": {"title", "chat_ref", "invite_url", "is_active"},
    "payment_methods": {"title", "details", "url", "is_active"},
    "promo_codes": {"is_active"},
    "orders": {"status", "price", "comment"},
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _fetch_sync(sql: str, params: tuple) -> list[dict]:
    with closing(_connect()) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _scalar_sync(sql: str, params: tuple) -> Any:
    with closing(_connect()) as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else 0


def _run_sync(sql: str, params: tuple) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid or 0


async def db_fetch(sql: str, params: tuple = ()) -> list[dict]:
    return await asyncio.to_thread(_fetch_sync, sql, tuple(params))


async def db_one(sql: str, params: tuple = ()) -> dict | None:
    rows = await db_fetch(sql, params)
    return rows[0] if rows else None


async def db_scalar(sql: str, params: tuple = ()) -> Any:
    return await asyncio.to_thread(_scalar_sync, sql, tuple(params))


async def db_run(sql: str, params: tuple = ()) -> int:
    return await asyncio.to_thread(_run_sync, sql, tuple(params))


def _init_schema_sync() -> None:
    with closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        conn.commit()


async def init_db() -> None:
    await asyncio.to_thread(_init_schema_sync)
    if await db_scalar("SELECT COUNT(*) FROM payment_methods") == 0:
        for m in config.PAYMENT_METHODS_DEFAULTS:
            await insert_row("payment_methods", code=secrets.token_hex(4), title=m["title"],
                             details=m["details"], url=m["url"])
    defaults = {
        "studio_name": config.STUDIO_NAME,
        "studio_description": config.STUDIO_DESCRIPTION,
        "support_contact": config.SUPPORT_CONTACT,
        "notify_admins": "1",
        "sub_enabled": "1",
    }
    for key, value in defaults.items():
        await db_run("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (key, value))
    for key, value in DEFAULT_TEXTS.items():
        await db_run("INSERT OR IGNORE INTO texts(key, html) VALUES(?, ?)", (key, value))
    for key, (label, style) in BUTTON_DEFAULTS.items():
        await db_run("INSERT OR IGNORE INTO buttons(key, label, style) VALUES(?, ?, ?)", (key, label, style))
    for admin_id in config.ADMIN_IDS:
        await db_run("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (admin_id,))
    log.info("База данных готова: %s", config.DB_PATH)


# --- универсальные операции ---

async def get_row(table: str, rid: int) -> dict | None:
    return await db_one(f"SELECT * FROM {table} WHERE id = ?", (rid,))


async def list_rows(table: str, where: str = "", params: tuple = (), order: str = "id DESC",
                    limit: int = 50) -> list[dict]:
    sql = f"SELECT * FROM {table}" + (f" WHERE {where}" if where else "")
    return await db_fetch(sql + f" ORDER BY {order} LIMIT {int(limit)}", params)


async def insert_row(table: str, **values: Any) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    return await db_run(f"INSERT INTO {table}({cols}) VALUES({marks})", tuple(values.values()))


async def update_row(table: str, rid: int, **changes: Any) -> None:
    bad = set(changes) - ALLOWED.get(table, set())
    if bad:
        raise ValueError(f"Поле нельзя менять: {bad}")
    if changes:
        sets = ", ".join(f"{k} = ?" for k in changes)
        await db_run(f"UPDATE {table} SET {sets} WHERE id = ?", (*changes.values(), rid))


async def delete_row(table: str, rid: int) -> None:
    await db_run(f"DELETE FROM {table} WHERE id = ?", (rid,))


async def get_setting(key: str, default: str = "") -> str:
    row = await db_one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    await db_run("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


async def T(key: str, **fmt: Any) -> str:
    """Текст экрана из базы (HTML с премиум-эмодзи), с подстановкой {переменных}."""
    row = await db_one("SELECT html FROM texts WHERE key = ?", (key,))
    text = row["html"] if row else DEFAULT_TEXTS.get(key, "")
    for k, v in fmt.items():
        text = text.replace("{" + k + "}", str(v))
    return text


async def text_media(key: str) -> str:
    row = await db_one("SELECT media_ids FROM text_media WHERE key = ?", (key,))
    return row["media_ids"] if row else ""


async def button_def(key: str) -> tuple[str, str]:
    row = await db_one("SELECT label, style FROM buttons WHERE key = ?", (key,))
    if row:
        return row["label"], row["style"]
    return BUTTON_DEFAULTS[key]


# --- пользователи ---

async def upsert_user(uid: int, username: str | None, first_name: str | None) -> None:
    await db_run(
        """INSERT INTO users(id, username, first_name) VALUES(?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET username=excluded.username,
               first_name=excluded.first_name, last_seen=datetime('now'), bot_blocked=0""",
        (uid, username, first_name))


async def get_user(uid: int) -> dict | None:
    return await db_one("SELECT * FROM users WHERE id = ?", (uid,))


async def set_user_flag(uid: int, column: str, value: bool) -> None:
    if column not in ("is_blocked", "bot_blocked"):
        raise ValueError(column)
    await db_run(f"UPDATE users SET {column} = ? WHERE id = ?", (int(value), uid))


async def list_admin_ids() -> list[int]:
    rows = await db_fetch("SELECT user_id FROM admins")
    return sorted({r["user_id"] for r in rows} | set(config.ADMIN_IDS))


async def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS or await db_scalar(
        "SELECT COUNT(*) FROM admins WHERE user_id = ?", (uid,)) > 0


async def search_users(query: str) -> list[dict]:
    q = query.strip().lstrip("@")
    if q.isdigit():
        return await db_fetch("SELECT * FROM users WHERE id = ?", (int(q),))
    return await db_fetch("SELECT * FROM users WHERE username LIKE ? OR first_name LIKE ? LIMIT 20",
                          (f"%{q}%", f"%{q}%"))


async def user_stats() -> dict:
    return {
        "total": await db_scalar("SELECT COUNT(*) FROM users"),
        "new_day": await db_scalar("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now','-1 day')"),
        "new_week": await db_scalar("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now','-7 day')"),
        "new_month": await db_scalar("SELECT COUNT(*) FROM users WHERE created_at >= datetime('now','-30 day')"),
        "active_week": await db_scalar("SELECT COUNT(*) FROM users WHERE last_seen >= datetime('now','-7 day')"),
        "blocked_bot": await db_scalar("SELECT COUNT(*) FROM users WHERE bot_blocked = 1"),
        "blocked_admin": await db_scalar("SELECT COUNT(*) FROM users WHERE is_blocked = 1"),
    }


# --- заказы, оплаты, аналитика ---

async def order_stats() -> dict:
    return {
        "total": await db_scalar("SELECT COUNT(*) FROM orders"),
        "unfinished": await db_scalar(
            "SELECT COUNT(*) FROM orders WHERE status IN ('new','in_progress','awaiting_payment')"),
        "day": await db_scalar("SELECT COUNT(*) FROM orders WHERE created_at >= datetime('now','-1 day')"),
        "week": await db_scalar("SELECT COUNT(*) FROM orders WHERE created_at >= datetime('now','-7 day')"),
        "month": await db_scalar("SELECT COUNT(*) FROM orders WHERE created_at >= datetime('now','-30 day')"),
    }


async def revenue_stats() -> dict:
    base = "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='confirmed'"
    return {
        "total": await db_scalar(base),
        "day": await db_scalar(base + " AND checked_at >= datetime('now','-1 day')"),
        "week": await db_scalar(base + " AND checked_at >= datetime('now','-7 day')"),
        "month": await db_scalar(base + " AND checked_at >= datetime('now','-30 day')"),
        "purchases": await db_scalar("SELECT COUNT(*) FROM payments WHERE status='confirmed'"),
    }


async def popular_items(limit: int = 5) -> list[dict]:
    return await db_fetch(
        """SELECT kind, item_id, COUNT(*) AS cnt FROM orders
           WHERE item_id IS NOT NULL GROUP BY kind, item_id ORDER BY cnt DESC LIMIT ?""", (limit,))


async def item_title(kind: str, item_id: int) -> str:
    table = {"template": "templates", "hosting": "hosting_plans"}.get(kind, "products")
    row = await get_row(table, item_id)
    return row["title"] if row else f"#{item_id}"


async def create_order(uid: int, kind: str, item_id: int | None, price: int, **f: str) -> int:
    return await insert_row("orders", user_id=uid, kind=kind, item_id=item_id, price=price,
                            name=f.get("name", ""), description=f.get("description", ""),
                            references_=f.get("references", ""), budget=f.get("budget", ""),
                            deadline=f.get("deadline", ""), contact=f.get("contact", ""))


async def user_orders(uid: int) -> list[dict]:
    return await list_rows("orders", "user_id = ?", (uid,), limit=100)


async def validate_promo(code: str) -> tuple[dict | None, str]:
    row = await db_one("SELECT * FROM promo_codes WHERE code = ?", (code.upper(),))
    if not row:
        return None, "Промокод не найден."
    if not row["is_active"]:
        return None, "Промокод деактивирован."
    if row["max_uses"] and row["uses"] >= row["max_uses"]:
        return None, "Лимит использований исчерпан."
    if row["expires_at"] and await db_scalar("SELECT date('now') > date(?)", (row["expires_at"],)):
        return None, "Срок действия промокода истёк."
    return row, ""


# --- подписка, уведомления, рассылка ---

async def check_subscription(bot: Bot, uid: int) -> list[dict]:
    missing = []
    for ch in await list_rows("subscriptions", "is_active = 1", order="id"):
        try:
            member = await bot.get_chat_member(chat_id=ch["chat_ref"], user_id=uid)
            status = member.status
        except TelegramAPIError as exc:
            log.warning("Проверка подписки на %s не удалась: %s", ch["chat_ref"], exc)
            status = "unknown"
        if status not in ("member", "administrator", "creator"):
            missing.append(ch)
    return missing


async def relay_to_admins(bot: Bot, text: str, markup=None, forward: Message | None = None) -> None:
    """Текст админам с кнопками. Если передан forward — сначала пересылается исходное сообщение
    клиента (чек, скриншот, документ), чтобы админ видел файл."""
    for aid in await list_admin_ids():
        try:
            if forward is not None:
                await forward.forward(aid)
            await bot.send_message(aid, text, reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить админу %s: %s", aid, exc)


async def notify_client(bot: Bot, uid: int, text: str, markup=None) -> bool:
    try:
        await bot.send_message(uid, text, reply_markup=markup)
        return True
    except TelegramForbiddenError:
        await set_user_flag(uid, "bot_blocked", True)
    except TelegramAPIError as exc:
        log.warning("Не удалось отправить клиенту %s: %s", uid, exc)
    return False


async def order_title_line(order: dict) -> str:
    """Заголовок админского сообщения: сразу видно, что заказали по шаблону, проекту или тарифу."""
    kind, item_id = order["kind"], order["item_id"]
    if kind == "template":
        t = await get_row("templates", item_id) if item_id else None
        if t and t["category"] == "bot":
            return "🤖 <b>ЗАКАЗ ПО ШАБЛОНУ TELEGRAM-БОТА</b>"
        return "🌐 <b>ЗАКАЗ ПО ШАБЛОНУ САЙТА</b>"
    if kind == "product":
        p = await get_row("products", item_id) if item_id else None
        if p and p["kind"] == "project":
            return "📁 <b>ЗАКАЗ ГОТОВОГО ПРОЕКТА</b>"
        return "🛍 <b>ЗАКАЗ ТОВАРА</b>"
    if kind == "hosting":
        return "🖥 <b>ЗАКАЗ ТАРИФА ХОСТИНГА</b>"
    return "🛒 <b>ЗАКАЗ</b>"


def admin_order_caption(title_line: str, oid: int, user: dict, item_name: str, price: int, extra: str = "") -> str:
    return (f"{title_line}\n━━━━━━━━━━━━━━\n"
            f"🧾 Заказ №{oid}\n📦 {esc(item_name)}\n💰 {money(price)}\n"
            f"👤 {esc(user['first_name'])} · <code>{user['id']}</code>"
            + (f"\n\n{extra}" if extra else ""))


async def send_to_admins_with_file(bot: Bot, text: str, markup, file_type: str, file_id: str) -> None:
    """Файл (чек, скриншот) и текст заказа — одним сообщением: подпись к файлу с кнопками."""
    senders = {"photo": bot.send_photo, "video": bot.send_video, "document": bot.send_document}
    for aid in await list_admin_ids():
        try:
            if file_id and file_type in senders and len(text) <= 1024:
                await senders[file_type](aid, file_id, caption=text, reply_markup=markup)
                continue
            if file_id and file_type in senders:
                await senders[file_type](aid, file_id)
            await bot.send_message(aid, text, reply_markup=markup)
        except TelegramAPIError as exc:
            log.warning("Не удалось отправить админу %s: %s", aid, exc)


RUNNING: dict[int, asyncio.Event] = {}


async def broadcast_send(bot: Bot, bc: dict, markup, stop: asyncio.Event) -> tuple[int, int]:
    recipients = [r["id"] for r in await db_fetch("SELECT id FROM users WHERE is_blocked = 0 AND bot_blocked = 0")]
    sent = failed = 0
    for i, uid in enumerate(recipients, start=1):
        if stop.is_set():
            break
        ok, error = False, ""
        for _ in range(3):
            try:
                caption = bc["text"] or None
                if bc["media_type"] == "photo":
                    await bot.send_photo(uid, bc["media_id"], caption=caption, reply_markup=markup)
                elif bc["media_type"] == "video":
                    await bot.send_video(uid, bc["media_id"], caption=caption, reply_markup=markup)
                elif bc["media_type"] == "document":
                    await bot.send_document(uid, bc["media_id"], caption=caption, reply_markup=markup)
                else:
                    await bot.send_message(uid, bc["text"], reply_markup=markup)
                ok = True
                break
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after + 1)
            except TelegramForbiddenError:
                error = "бот заблокирован"
                await set_user_flag(uid, "bot_blocked", True)
                break
            except TelegramBadRequest as exc:
                error = f"bad request: {exc.message}"
                break
            except TelegramAPIError as exc:
                error = str(exc)
                await asyncio.sleep(1)
        await db_run("INSERT INTO broadcast_logs(broadcast_id, user_id, ok, error) VALUES(?, ?, ?, ?)",
                     (bc["id"], uid, int(ok), error[:300]))
        col = "sent_count" if ok else "failed_count"
        await db_run(f"UPDATE broadcasts SET {col} = {col} + 1 WHERE id = ?", (bc["id"],))
        sent += ok
        failed += not ok
        await asyncio.sleep(config.BROADCAST_DELAY_SEC)
        if i % config.BROADCAST_CHUNK == 0:
            await asyncio.sleep(config.BROADCAST_CHUNK_PAUSE_SEC)
    return sent, failed


# =============================================================================
# 2. ИНТЕРФЕЙС: КНОПКИ, ЭКРАНЫ, МЕДИА
# =============================================================================

def B(text: str, data: str | None = None, url: str | None = None,
      style: str | None = None) -> InlineKeyboardButton:
    """Инлайн-кнопка. style: primary (синяя), success (зелёная), danger (красная) или None."""
    kw: dict[str, Any] = {"text": text}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = data
    if style:
        kw["style"] = style
    return InlineKeyboardButton(**kw)


async def bt(key: str, data: str | None = None, url: str | None = None) -> InlineKeyboardButton:
    """Кнопка с текстом и цветом из базы (их меняют в админке)."""
    label, style = await button_def(key)
    return B(label, data, url, style or None)


def KB(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])


def BACK(data: str) -> list[InlineKeyboardButton]:
    return [B("⬅️ Назад", data)]


def parse_media(raw: str) -> list[tuple[str, str]]:
    out = []
    for part in (raw or "").split(","):
        if ":" in part:
            kind, fid = part.strip().split(":", 1)
            if kind in ("photo", "video", "document") and fid:
                out.append((kind, fid))
    return out


def join_media(items: list[tuple[str, str]]) -> str:
    return ",".join(f"{k}:{v}" for k, v in items)


def esc(v: Any) -> str:
    return html.escape(str(v if v not in (None, "") else "—"))


def lines_list(raw: str) -> str:
    parts = [p.strip() for p in raw.replace(";", "\n").replace(",", "\n").split("\n") if p.strip()]
    return "\n".join(f"• {html.escape(p)}" for p in parts) if parts else "—"


def money(v: int) -> str:
    return f"{v:,}".replace(",", " ") + " ₽"


async def _delete_quiet(bot: Bot, chat_id: int, mid: int) -> None:
    try:
        await bot.delete_message(chat_id, mid)
    except TelegramAPIError:
        pass


async def send_card(bot: Bot, chat_id: int, text: str, media_raw: str, markup) -> int:
    """Текст с медиа. Одно медиа с подписью, альбом фото/видео, либо медиа и текст отдельно.
    Возвращает id сообщения с текстом и кнопками."""
    media = parse_media(media_raw)
    if not media:
        return (await bot.send_message(chat_id, text, reply_markup=markup)).message_id
    senders = {"photo": bot.send_photo, "video": bot.send_video, "document": bot.send_document}
    if len(media) == 1 and len(text) <= 1024:
        kind, fid = media[0]
        return (await senders[kind](chat_id, fid, caption=text, reply_markup=markup)).message_id
    visual = [m for m in media if m[0] in ("photo", "video")]
    if len(visual) >= 2:
        album = [InputMediaPhoto(media=f) if k == "photo" else InputMediaVideo(media=f)
                 for k, f in visual[:10]]
        await bot.send_media_group(chat_id, album)
        rest = [m for m in media if m[0] == "document"] + visual[10:]
    else:
        rest = media
    for kind, fid in rest:
        await senders[kind](chat_id, fid)
    return (await bot.send_message(chat_id, text, reply_markup=markup)).message_id


async def show(bot: Bot, chat_id: int, text: str, markup=None, mid: int | None = None,
               media: list[tuple[str, str]] | None = None) -> int:
    """Одно окно: правит текущее сообщение. Экран с медиа заменяет его новым сообщением."""
    if media:
        if mid:
            await _delete_quiet(bot, chat_id, mid)
        return await send_card(bot, chat_id, text, join_media(media), markup)
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=markup)
            return mid
        except TelegramBadRequest as exc:
            if "not modified" in str(exc):
                return mid
            await _delete_quiet(bot, chat_id, mid)   # например, раньше тут было сообщение с медиа
    sent = await bot.send_message(chat_id, text, reply_markup=markup)
    return sent.message_id


async def screen(bot: Bot, chat_id: int, key: str, markup=None, mid: int | None = None, **fmt: Any) -> int:
    """Экран из базы: текст и медиа, которые админ задал в редакторе текстов."""
    text = await T(key, **fmt)
    media = parse_media(await text_media(key))
    return await show(bot, chat_id, text, markup, mid, media or None)


async def studio_fmt() -> dict:
    return {
        "studio": html.escape(await get_setting("studio_name", config.STUDIO_NAME)),
        "description": html.escape(await get_setting("studio_description", config.STUDIO_DESCRIPTION)),
    }


async def main_kb(admin: bool) -> InlineKeyboardMarkup:
    rows = [
        [await bt("menu_order", "nav:order")],
        [await bt("menu_templates", "nav:templates"), await bt("menu_projects", "nav:projects")],
        [await bt("menu_profile", "nav:profile"), await bt("menu_support", "nav:support")],
        [await bt("menu_hosting", "nav:hosting")],
    ]
    if admin:
        rows.append([await bt("menu_admin", "adm:home")])
    return KB(*rows)


async def safe_answer(call: CallbackQuery, text: str | None = None, show_alert: bool = False) -> None:
    """Ответ на callback. Повторный ответ в цепочке обработчиков игнорируется."""
    try:
        await call.answer(text, show_alert=show_alert)
    except TelegramBadRequest:
        pass


# =============================================================================
# 3. СОСТОЯНИЯ, ФИЛЬТРЫ, MIDDLEWARE
# =============================================================================

class OrderFSM(StatesGroup):
    step = State()
    confirm = State()
    edit = State()


class SupportFSM(StatesGroup):
    message = State()


class PaymentFSM(StatesGroup):
    proof = State()


class AdminFSM(StatesGroup):
    input = State()


class AdminOnly(Filter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return bool(event.from_user) and await is_admin(event.from_user.id)


class ActivityMiddleware(BaseMiddleware):
    """Регистрирует пользователя, не пускает заблокированных и требует подписку на каналы."""

    async def __call__(self, handler, event: TelegramObject, data: dict) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or user.is_bot:
            return await handler(event, data)
        await upsert_user(user.id, user.username, user.first_name)
        row = await get_user(user.id)
        admin = await is_admin(user.id)
        is_cb = isinstance(event, CallbackQuery)
        if row and row["is_blocked"] and not admin:
            if is_cb:
                await safe_answer(event, "Доступ ограничен.", show_alert=True)
            else:
                await event.answer("🚫 Доступ к боту ограничен.")
            return None
        exempt = is_cb and event.data == "sub:check"
        if not admin and not exempt and await get_setting("sub_enabled", "1") == "1":
            missing = await check_subscription(data["bot"], user.id)
            if missing:
                if is_cb:
                    await safe_answer(event, "Сначала подпишитесь на каналы.", show_alert=True)
                else:
                    rows = [[B(c["title"], url=c["invite_url"])] for c in missing]
                    rows.append([await bt("sub_check", "sub:check")])
                    await event.answer(await T("sub_text"), reply_markup=KB(*rows))
                return None
        return await handler(event, data)


# =============================================================================
# 4. КЛИЕНТСКИЕ РАЗДЕЛЫ
# =============================================================================

router = Router(name="main")

ORDER_FIELDS = [
    {"key": "name", "label": "Название", "optional": False,
     "q": "1️⃣ <b>Название или идея проекта</b>\nКоротко опишите, что нужно создать."},
    {"key": "description", "label": "Функционал", "optional": False,
     "q": "2️⃣ <b>Подробное описание функционала</b>\nЧто должно уметь, для кого, какие страницы или команды."},
    {"key": "references", "label": "Референсы", "optional": True,
     "q": "3️⃣ <b>Примеры или референсы</b> (необязательно)\nПришлите ссылки или нажмите «Пропустить»."},
    {"key": "budget", "label": "Бюджет", "optional": False,
     "q": "4️⃣ <b>Бюджет</b>\nНапример: 50 000 – 80 000 ₽."},
    {"key": "deadline", "label": "Сроки", "optional": False,
     "q": "5️⃣ <b>Желаемые сроки</b>\nНапример: «до 1 декабря»."},
    {"key": "contact", "label": "Контакт", "optional": False,
     "q": "6️⃣ <b>Контакт для связи</b>\nTelegram @username, телефон или email."},
]
KIND_LABELS = {"site": "🌐 Сайт", "bot": "🤖 Telegram-бот"}
PROJECT_CATS = {"bot": "🤖 Telegram-боты", "site": "🌐 Сайты", "miniapp": "📱 Mini Apps", "other": "📦 Другие проекты"}


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await screen(message.bot, message.chat.id, "welcome", await main_kb(await is_admin(message.from_user.id)),
                 None, **await studio_fmt())


@router.callback_query(F.data == "nav:main")
async def nav_main(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(call)
    await screen(call.bot, call.message.chat.id, "welcome", await main_kb(await is_admin(call.from_user.id)),
                 call.message.message_id, **await studio_fmt())


@router.callback_query(F.data == "nav:profile")
async def nav_profile(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(call)
    user = await get_user(call.from_user.id)
    orders = await user_orders(call.from_user.id)
    active = [o for o in orders if o["status"] in ACTIVE_STATUSES]
    lines = [
        "👤 <b>Ваш профиль</b>",
        f"🆔 Telegram ID: <code>{user['id']}</code>",
        f"🙍 Имя: {esc(user['first_name'])}",
        f"🔖 Username: {('@' + esc(user['username'])) if user['username'] else '—'}",
        f"📅 Регистрация: {esc(user['created_at'])}",
        f"🧾 Всего заказов: {len(orders)} · текущих: {len(active)}",
    ]
    if orders:
        lines.append("\n<b>Статусы заказов:</b>")
        lines += [f"• №{o['id']} — {ORDER_STATUSES[o['status']]}" for o in orders[:10]]
    pay_rows = [[await bt("pay", f"pay:open:{o['id']}")] for o in orders if o["status"] == "awaiting_payment"]
    await show(call.bot, call.message.chat.id, "\n".join(lines),
               KB(*pay_rows, BACK("nav:main")), call.message.message_id)


# --- шаблоны, проекты, хостинг ---

@router.callback_query(F.data == "nav:templates")
async def nav_templates(call: CallbackQuery) -> None:
    await safe_answer(call)
    await screen(call.bot, call.message.chat.id, "templates_pick", KB(
        [B("🌐 Сайты", "tplcat:site", style="primary"), B("🤖 Боты", "tplcat:bot", style="primary")],
        BACK("nav:main")), call.message.message_id)


@router.callback_query(F.data.startswith("tplcat:"))
async def tpl_list(call: CallbackQuery) -> None:
    cat = call.data.split(":")[1]
    items = await list_rows("templates", "category = ? AND is_published = 1", (cat,), order="id DESC")
    await safe_answer(call)
    rows = [[B(f"{t['title']} — {money(t['price'])}", f"tpl:view:{t['id']}")] for t in items]
    await show(call.bot, call.message.chat.id, "🧩 Выберите шаблон:" if items else "Пока шаблонов нет.",
               KB(*rows, BACK("nav:templates")), call.message.message_id)


@router.callback_query(F.data.startswith("tpl:view:"))
async def tpl_view(call: CallbackQuery) -> None:
    t = await get_row("templates", int(call.data.split(":")[2]))
    if not t or not t["is_published"]:
        await safe_answer(call, "Шаблон недоступен.", show_alert=True)
        return
    await safe_answer(call)
    text = (f"🧩 <b>{esc(t['title'])}</b>\n\n{t['description']}\n\n"
            f"<b>Возможности:</b>\n{lines_list(t['features'])}\n\n💰 <b>Стоимость:</b> {money(t['price'])}")
    rows = [[await bt("buy", f"buy:tpl:{t['id']}")]]
    if t["demo_url"]:
        rows.append([await bt("demo", url=t["demo_url"])])
    rows.append(BACK(f"tplcat:{t['category']}"))
    await send_card(call.bot, call.message.chat.id, text, t["media_ids"], KB(*rows))


@router.callback_query(F.data == "nav:projects")
async def nav_projects(call: CallbackQuery) -> None:
    await safe_answer(call)
    rows = [[B(v, f"prodcat:{k}", style="primary")] for k, v in PROJECT_CATS.items()]
    await screen(call.bot, call.message.chat.id, "projects_pick", KB(*rows, BACK("nav:main")),
                 call.message.message_id)


@router.callback_query(F.data.startswith("prodcat:"))
async def prod_list(call: CallbackQuery) -> None:
    cat = call.data.split(":")[1]
    items = await list_rows("products", "kind = 'project' AND category = ? AND is_published = 1", (cat,))
    await safe_answer(call)
    rows = [[B(p["title"], f"prod:view:{p['id']}")] for p in items]
    await show(call.bot, call.message.chat.id, PROJECT_CATS[cat] if items else "В этой категории пока пусто.",
               KB(*rows, BACK("nav:projects")), call.message.message_id)


@router.callback_query(F.data.startswith("prod:view:"))
async def prod_view(call: CallbackQuery) -> None:
    p = await get_row("products", int(call.data.split(":")[2]))
    if not p or not p["is_published"]:
        await safe_answer(call, "Проект недоступен.", show_alert=True)
        return
    await safe_answer(call)
    text = (f"📁 <b>{esc(p['title'])}</b>\nКатегория: {PROJECT_CATS.get(p['category'], '—')}\n\n"
            f"{p['description']}\n\n<b>Функционал:</b>\n{lines_list(p['features'])}\n\n"
            f"💰 <b>Стоимость:</b> {money(p['price'])}\n📜 <b>Лицензия:</b> {esc(p['license'])}\n"
            f"📦 <b>Наличие:</b> {'✅ В наличии' if p['in_stock'] else '⏳ Нет в наличии'}")
    if p["sale_terms"]:
        text += f"\n\n<b>Условия продажи:</b>\n{p['sale_terms']}"
    rows = [[await bt("buy", f"buy:prod:{p['id']}"), B("💬 Обсудить", f"disc:prod:{p['id']}")]]
    if p["demo_url"]:
        rows.append([await bt("demo", url=p["demo_url"])])
    rows.append(BACK(f"prodcat:{p['category']}"))
    await send_card(call.bot, call.message.chat.id, text, p["media_ids"], KB(*rows))


@router.callback_query(F.data == "nav:hosting")
async def nav_hosting(call: CallbackQuery) -> None:
    await safe_answer(call)
    plans = await list_rows("hosting_plans", "is_published = 1", order="price")
    rows = [[B(f"{p['title']} — {money(p['price'])}", f"host:view:{p['id']}")] for p in plans]
    await screen(call.bot, call.message.chat.id, "hosting_pick",
                 KB(*rows, BACK("nav:main")), call.message.message_id)


@router.callback_query(F.data.startswith("host:view:"))
async def host_view(call: CallbackQuery) -> None:
    p = await get_row("hosting_plans", int(call.data.split(":")[2]))
    if not p or not p["is_published"]:
        await safe_answer(call, "Тариф недоступен.", show_alert=True)
        return
    await safe_answer(call)
    text = (f"🖥 <b>{esc(p['title'])}</b>\n\n💰 {money(p['price'])}\n⚙️ CPU: {esc(p['cpu'])}\n"
            f"🧠 RAM: {esc(p['ram'])}\n💽 Диск: {esc(p['disk'])}\n🌍 Трафик: {esc(p['traffic'])}\n"
            f"📆 Срок: {esc(p['period'])}\n\n{p['description']}")
    await send_card(call.bot, call.message.chat.id, text, "", KB(
        [await bt("buy", f"buy:host:{p['id']}"), B("💬 Уточнить", f"disc:host:{p['id']}")],
        BACK("nav:hosting")))
    await _delete_quiet(call.bot, call.message.chat.id, call.message.message_id)


# --- покупка из карточек ---

@router.callback_query(F.data.startswith("buy:"))
async def buy_item(call: CallbackQuery) -> None:
    _, short, raw = call.data.split(":")
    table = {"tpl": "templates", "prod": "products", "host": "hosting_plans"}[short]
    kind = {"tpl": "template", "prod": "product", "host": "hosting"}[short]
    item = await get_row(table, int(raw))
    if not item or (short == "prod" and not item["in_stock"]):
        await safe_answer(call, "Этот товар сейчас недоступен.", show_alert=True)
        return
    oid = await create_order(call.from_user.id, kind, int(raw), item["price"],
                             name=item["title"], description=f"Заказ из раздела: {kind}")
    user = await get_user(call.from_user.id)
    if item["price"] > 0:
        # Платная позиция: сразу переходим к оплате
        await update_row("orders", oid, status="awaiting_payment")
        await safe_answer(call)
        methods = await list_rows("payment_methods", "is_active = 1", order="id")
        rows = [[B(m["title"], f"pay:m:{m['code']}:{oid}", style="primary")] for m in methods]
        await screen(call.bot, call.message.chat.id, "pay_open", KB(*rows, BACK("nav:main")),
                     call.message.message_id, id=oid, price=money(item["price"]))
        return
    await safe_answer(call)
    await show(call.bot, call.message.chat.id,
               f"✅ <b>Заявка №{oid} отправлена.</b>\nПозиция: {esc(item['title'])}\n"
               "Менеджер свяжется с вами в этом чате.",
               KB(BACK("nav:main")), call.message.message_id)
    await relay_to_admins(call.bot,
                          f"🛍 <b>Новый заказ №{oid}</b>\nКлиент: {esc(user['first_name'])} "
                          f"(ID <code>{user['id']}</code>)\nПозиция: {esc(item['title'])} — {money(item['price'])}",
                          KB([B("📋 Открыть заказ", f"adm:order:{oid}")]))


# --- заявка на разработку: все шаги в одном окне ---

def order_summary(data: dict) -> str:
    lines = [f"📝 <b>Ваша заявка — {KIND_LABELS[data['kind']]}</b>", ""]
    lines += [f"<b>{f['label']}:</b> {esc(data['values'].get(f['key']) or '—')}" for f in ORDER_FIELDS]
    lines.append("\nПроверьте данные и отправьте заявку.")
    return "\n".join(lines)


async def order_prompt(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    field = ORDER_FIELDS[data["step"]]
    rows = []
    if field["optional"]:
        rows.append(B("⏭ Пропустить", "order:skip"))
    await show(bot, chat_id, field["q"], KB(rows, [await bt("cancel", "order:cancel")]), data["ui"])


async def order_summary_view(bot: Bot, chat_id: int, state: FSMContext) -> None:
    await state.set_state(OrderFSM.confirm)
    data = await state.get_data()
    await show(bot, chat_id, order_summary(data), KB(
        [await bt("submit", "order:submit")],
        [B("✏️ Изменить", "order:edit", style="primary"), await bt("cancel", "order:cancel")],
    ), data["ui"])


@router.callback_query(F.data == "nav:order")
async def nav_order(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(call)
    await screen(call.bot, call.message.chat.id, "order_pick", KB(
        [await bt("order_site", "order:kind:site")],
        [await bt("order_bot", "order:kind:bot")],
        BACK("nav:main")), call.message.message_id)


@router.callback_query(F.data.startswith("order:kind:"))
async def order_start(call: CallbackQuery, state: FSMContext) -> None:
    kind = call.data.split(":")[2]
    await state.set_state(OrderFSM.step)
    await state.set_data({"kind": kind, "step": 0, "values": {}, "editing": False, "ui": call.message.message_id})
    await safe_answer(call)
    await order_prompt(call.bot, call.message.chat.id, state)


async def order_advance(bot: Bot, chat_id: int, state: FSMContext, text: str) -> None:
    data = await state.get_data()
    field = ORDER_FIELDS[data["step"]]
    data["values"][field["key"]] = text
    if data["editing"]:
        data["editing"] = False
        await state.set_data(data)
        await order_summary_view(bot, chat_id, state)
        return
    data["step"] += 1
    await state.set_data(data)
    if data["step"] >= len(ORDER_FIELDS):
        await order_summary_view(bot, chat_id, state)
    else:
        await order_prompt(bot, chat_id, state)


@router.message(OrderFSM.step, F.text)
async def order_text(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    field = ORDER_FIELDS[data["step"]]
    text = message.text.strip()
    await _delete_quiet(message.bot, message.chat.id, message.message_id)
    if len(text) < 2 and not field["optional"]:
        await show(message.bot, message.chat.id, field["q"] + "\n\n<i>Ответ слишком короткий.</i>",
                   KB([await bt("cancel", "order:cancel")]), data["ui"])
        return
    if len(text) > 2000:
        await message.answer("Слишком длинно (максимум 2000 символов).")
        return
    await order_advance(message.bot, message.chat.id, state, text)


@router.callback_query(OrderFSM.step, F.data == "order:skip")
async def order_skip(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    await order_advance(call.bot, call.message.chat.id, state, "")


@router.callback_query(OrderFSM.confirm, F.data == "order:submit")
async def order_submit(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    v = data["values"]
    oid = await create_order(call.from_user.id, data["kind"], None, 0, name=v.get("name", ""),
                             description=v.get("description", ""), references=v.get("references", ""),
                             budget=v.get("budget", ""), deadline=v.get("deadline", ""),
                             contact=v.get("contact", ""))
    await state.clear()
    await safe_answer(call, "Заявка отправлена!")
    await screen(call.bot, call.message.chat.id, "order_done", KB(BACK("nav:main")),
                 call.message.message_id, id=oid)
    user = await get_user(call.from_user.id)
    await relay_to_admins(call.bot,
                          f"🆕 <b>Новая заявка №{oid}</b> ({KIND_LABELS[data['kind']]})\n"
                          f"Клиент: {esc(user['first_name'])} (ID <code>{user['id']}</code>)\n"
                          f"Название: {esc(v.get('name'))}\nБюджет: {esc(v.get('budget'))}, "
                          f"сроки: {esc(v.get('deadline'))}",
                          KB([B("📋 Открыть заказ", f"adm:order:{oid}")]))


@router.callback_query(OrderFSM.confirm, F.data == "order:edit")
async def order_edit(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    rows = [[B(f["label"], f"order:field:{i}")] for i, f in enumerate(ORDER_FIELDS)]
    await show(call.bot, call.message.chat.id, "Выберите поле для изменения:",
               KB(*rows, [B("⬅️ К заявке", "order:back")]), call.message.message_id)
    await state.set_state(OrderFSM.edit)


@router.callback_query(OrderFSM.edit, F.data.startswith("order:field:"))
async def order_pick(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    data.update(step=int(call.data.split(":")[2]), editing=True, ui=call.message.message_id)
    await state.set_data(data)
    await state.set_state(OrderFSM.step)
    await safe_answer(call)
    await order_prompt(call.bot, call.message.chat.id, state)


@router.callback_query(OrderFSM.edit, F.data == "order:back")
async def order_back(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    data = await state.get_data()
    data["ui"] = call.message.message_id
    await state.set_data(data)
    await order_summary_view(call.bot, call.message.chat.id, state)


@router.callback_query(F.data == "order:cancel")
async def order_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(call, "Заявка отменена.")
    await show(call.bot, call.message.chat.id, "❌ Заявка отменена.", KB(BACK("nav:main")), call.message.message_id)


# --- поддержка ---

async def start_support(bot: Bot, chat_id: int, state: FSMContext, mid: int | None, ref: str = "") -> None:
    await state.set_state(SupportFSM.message)
    await state.set_data({"ref": ref, "ui": mid})
    intro = await T("support_intro") + (f"\n\nПо позиции: {esc(ref)}" if ref else "")
    media = parse_media(await text_media("support_intro"))
    await show(bot, chat_id, intro, KB([await bt("cancel", "nav:main")]), mid, media or None)


@router.callback_query(F.data == "nav:support")
async def nav_support(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    await start_support(call.bot, call.message.chat.id, state, call.message.message_id)


@router.callback_query(F.data.startswith("disc:"))
async def discuss(call: CallbackQuery, state: FSMContext) -> None:
    _, short, raw = call.data.split(":")
    item = await get_row("products" if short == "prod" else "hosting_plans", int(raw))
    await safe_answer(call)
    await start_support(call.bot, call.message.chat.id, state, None,
                        ref=item["title"] if item else "позиция")


@router.message(SupportFSM.message)
async def support_receive(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = (message.text or message.caption or "").strip()
    file_type, file_id = "", ""
    if message.photo:
        file_type, file_id = "photo", message.photo[-1].file_id
    elif message.video:
        file_type, file_id = "video", message.video.file_id
    elif message.document:
        file_type, file_id = "document", message.document.file_id
    if not text and not file_id:
        return
    mid = await insert_row("support_messages", user_id=message.from_user.id, text=text,
                           file_type=file_type, file_id=file_id)
    await state.clear()
    await _delete_quiet(message.bot, message.chat.id, message.message_id)
    user = await get_user(message.from_user.id)
    ref = data.get("ref", "")
    header = (f"💬 <b>Обращение №{mid}</b>\nОт: {esc(user['first_name'])} (@{esc(user.get('username'))}, "
              f"ID <code>{user['id']}</code>)\n" + (f"По позиции: {esc(ref)}\n" if ref else "")
              + f"\n{esc(text) if text else '<i>без текста</i>'}")
    await relay_to_admins(message.bot, header, KB([B("✉️ Ответить", f"adm:sup:{mid}")]),
                          forward=message if file_id else None)
    await screen(message.bot, message.chat.id, "support_done", KB(BACK("nav:main")), data["ui"], id=mid)


# --- оплата ---

@router.callback_query(F.data.startswith("pay:open:"))
async def pay_open(call: CallbackQuery) -> None:
    oid = int(call.data.split(":")[2])
    order = await get_row("orders", oid)
    if not order or order["user_id"] != call.from_user.id:
        await safe_answer(call, "Заказ не найден.", show_alert=True)
        return
    if order["status"] != "awaiting_payment":
        await safe_answer(call, "Оплата сейчас не требуется.", show_alert=True)
        return
    methods = await list_rows("payment_methods", "is_active = 1", order="id")
    if not methods:
        await safe_answer(call, "Способы оплаты временно недоступны.", show_alert=True)
        return
    await safe_answer(call)
    rows = [[B(m["title"], f"pay:m:{m['code']}:{oid}", style="primary")] for m in methods]
    await screen(call.bot, call.message.chat.id, "pay_open", KB(*rows, BACK("nav:profile")),
                 call.message.message_id, id=oid, price=money(order["price"]))


@router.callback_query(F.data.startswith("pay:m:"))
async def pay_method(call: CallbackQuery, state: FSMContext) -> None:
    _, _, code, raw = call.data.split(":")
    order = await get_row("orders", int(raw))
    method = await db_one("SELECT * FROM payment_methods WHERE code = ?", (code,))
    if (not order or order["user_id"] != call.from_user.id or order["status"] != "awaiting_payment"
            or not method or not method["is_active"]):
        await safe_answer(call, "Способ оплаты недоступен.", show_alert=True)
        return
    await state.set_state(PaymentFSM.proof)
    await state.set_data({"order_id": order["id"], "method": code, "method_title": method["title"],
                          "amount": order["price"], "ui": call.message.message_id})
    await safe_answer(call)
    rows = []
    if method["url"]:
        rows.append([B("🔗 Перейти к оплате", url=method["url"], style="success")])
    rows.append([await bt("cancel", "nav:main")])
    await screen(call.bot, call.message.chat.id, "pay_method", KB(*rows), call.message.message_id,
                 title=esc(method["title"]), amount=money(order["price"]), details=method["details"])


@router.message(PaymentFSM.proof)
async def pay_proof(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = (message.text or message.caption or "").strip()
    proof_type, file_id = "text", ""
    if message.photo:
        proof_type, file_id = "photo", message.photo[-1].file_id
    elif message.document:
        proof_type, file_id = "document", message.document.file_id
    elif message.video:
        proof_type, file_id = "video", message.video.file_id
    if not file_id:
        await message.answer("Отправьте скриншот чека (фото или файл). Текст можно добавить в подписи.")
        return
    pid = await insert_row("payments", order_id=data["order_id"], user_id=message.from_user.id,
                           method=data["method"], amount=data["amount"], proof_type=proof_type,
                           proof_file_id=file_id, proof_text=text)
    await state.clear()
    await _delete_quiet(message.bot, message.chat.id, message.message_id)
    user = await get_user(message.from_user.id)
    order = await get_row("orders", data["order_id"])
    caption = admin_order_caption(await order_title_line(order), order["id"], user, order["name"],
                                  data["amount"],
                                  f"💳 <b>Чек по оплате #{pid}</b> · {esc(data['method_title'])}\n"
                                  f"💬 {esc(text) if text else '—'}\n"
                                  "⚠️ Проверьте поступление средств.")
    await send_to_admins_with_file(message.bot, caption, KB(
        [B("📋 Открыть заказ", f"adm:order:{order['id']}")],
        [B("✅ Подтвердить", f"adm:pok:{pid}", style="success"),
         B("❌ Отклонить", f"adm:pno:{pid}", style="danger")]), proof_type, file_id)
    await screen(message.bot, message.chat.id, "pay_done", KB(BACK("nav:main")), data["ui"])


@router.message(Command("promo"))
async def cmd_promo(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: <code>/promo КОД</code>")
        return
    promo, error = await validate_promo(parts[1].strip())
    if not promo:
        await message.answer(f"❌ {error}")
        return
    orders = [o for o in await user_orders(message.from_user.id) if o["status"] == "awaiting_payment"]
    if not orders:
        await message.answer("Промокод применяется к заказу, ожидающему оплаты.")
        return
    order = orders[0]
    if await db_scalar("SELECT COUNT(*) FROM promo_uses WHERE order_id = ?", (order["id"],)):
        await message.answer("К этому заказу промокод уже применён.")
        return
    if promo["discount_type"] == "percent":
        new_price = max(0, round(order["price"] * (100 - promo["amount"]) / 100))
    else:
        new_price = max(0, order["price"] - promo["amount"])
    await update_row("orders", order["id"], price=new_price)
    await db_run("UPDATE promo_codes SET uses = uses + 1 WHERE id = ?", (promo["id"],))
    await insert_row("promo_uses", promo_id=promo["id"], user_id=message.from_user.id, order_id=order["id"])
    await message.answer(f"🎟 Промокод применён. Новая сумма заказа №{order['id']}: <b>{money(new_price)}</b>.")


@router.callback_query(F.data == "sub:check")
async def sub_check(call: CallbackQuery) -> None:
    if await check_subscription(call.bot, call.from_user.id):
        await safe_answer(call, "Вы ещё не подписались на все каналы.", show_alert=True)
        return
    await safe_answer(call, "Спасибо! Подписка подтверждена.")
    await show(call.bot, call.message.chat.id, "✅ Подписка подтверждена. Добро пожаловать!",
               await main_kb(await is_admin(call.from_user.id)), call.message.message_id)


# =============================================================================
# 5. АДМИН-ПАНЕЛЬ: ОБЩИЕ МЕХАНИЗМЫ
# =============================================================================

ACTIONS: dict[str, Callable[[Message, FSMContext, dict], Awaitable[str | None]]] = {}
WIZARDS: dict[str, dict] = {}
HTML_KEYS = {"description", "sale_terms", "details"}     # поля, где админ может вставлять премиум-эмодзи и форматирование


def action(name: str):
    def deco(fn):
        ACTIONS[name] = fn
        return fn
    return deco


def msg_html(message: Message) -> str:
    """Текст сообщения в HTML: сохраняет жирный, ссылки и премиум-эмодзи."""
    return (message.html_text if message.text else (message.html_caption or "")).strip()


async def check_html(bot: Bot, chat_id: int, text: str) -> None:
    """Проверяет разметку пробной отправкой и сразу удаляет тестовое сообщение."""
    if not text:
        raise ValueError("Текст пустой.")
    try:
        test = await bot.send_message(chat_id, text)
    except TelegramBadRequest as exc:
        raise ValueError(f"Ошибка разметки: {exc.message}") from exc
    await _delete_quiet(bot, chat_id, test.message_id)


def p_text(lo: int, hi: int) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        if len(raw) < lo:
            raise ValueError(f"Нужно минимум {lo} симв.")
        if len(raw) > hi:
            raise ValueError(f"Максимум {hi} симв.")
        return raw
    return parse


def p_int(lo: int) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        cleaned = raw.replace(" ", "").replace(",", "")
        if not cleaned.lstrip("-").isdigit() or int(cleaned) < lo:
            raise ValueError(f"Введите целое число не меньше {lo}.")
        return int(cleaned)
    return parse


def p_url(raw: str) -> str:
    if not raw.startswith(("https://", "http://", "t.me/")):
        raise ValueError("Ссылка должна начинаться с https:// или t.me/")
    return raw


def p_choice(options: dict[str, str]) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        if raw.strip().lower() in options:
            return options[raw.strip().lower()]
        raise ValueError("Выберите вариант: " + ", ".join(sorted(options)))
    return parse


def p_date(raw: str) -> str:
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError as exc:
        raise ValueError("Дата в формате ГГГГ-ММ-ДД.") from exc


CAT_CHOICE = {"telegram-бот": "bot", "бот": "bot", "сайт": "site", "site": "site", "mini app": "miniapp",
              "miniapp": "miniapp", "другое": "other", "other": "other"}
TPL_CAT_CHOICE = {"сайт": "site", "site": "site", "бот": "bot", "bot": "bot"}


async def ask(call: CallbackQuery, state: FSMContext, act: str, prompt: str, back: str, **params) -> None:
    """Переводит админа в режим ввода: следующее его сообщение обработает action `act`."""
    await state.set_state(AdminFSM.input)
    await state.set_data({"action": act, "ui": call.message.message_id, "back": back, **params})
    await show(call.bot, call.message.chat.id, prompt, KB([B("❌ Отмена", back, style="danger")]),
               call.message.message_id)


async def prompt_step(bot: Bot, chat_id: int, data: dict) -> None:
    _, question, _ = WIZARDS[data["wiz"]]["fields"][data["step"]]
    hint = "\n\n<i>Чтобы пропустить, отправьте «-».</i>" if "(необяз" in question else ""
    await show(bot, chat_id, question + hint, KB([B("❌ Отмена", data["back"], style="danger")]), data["ui"])


def wizard(name: str, fields: list[tuple[str, str, Callable[[str], Any]]], finish: Callable) -> None:
    WIZARDS[name] = {"fields": fields, "finish": finish}


@action("wiz")
async def act_wiz(message: Message, state: FSMContext, data: dict) -> str | None:
    w = WIZARDS[data["wiz"]]
    key, question, parser = w["fields"][data["step"]]
    optional = "(необяз" in question
    raw = msg_html(message) if key in HTML_KEYS else (message.text or "").strip()
    if not raw:
        raise ValueError("Нужен ответ.")
    if raw == "-" and optional:
        value = None
    else:
        if key in HTML_KEYS:
            await check_html(message.bot, message.chat.id, raw)
        value = parser(raw)
    vals = dict(data["vals"])
    vals[key] = value
    if data["step"] + 1 < len(w["fields"]):
        data.update(step=data["step"] + 1, vals=vals)
        await state.set_data(data)
        await prompt_step(message.bot, message.chat.id, data)
        return None
    return await w["finish"](vals)


def parse_kind(kind: str, raw: str) -> Any:
    """Разбор значения по виду поля: text | int | url | url_opt | chat | cat | tcat."""
    if kind == "text":
        return p_text(2, 100)(raw)
    if kind == "int":
        return p_int(0)(raw)
    if kind == "url":
        return p_url(raw)
    if kind == "url_opt":
        return None if raw == "-" else p_url(raw)
    if kind == "chat":
        return p_text(3, 100)(raw)
    if kind == "cat":
        return p_choice(CAT_CHOICE)(raw)
    if kind == "tcat":
        return p_choice(TPL_CAT_CHOICE)(raw)
    raise ValueError("Неизвестный тип поля")


@action("field")
async def act_field(message: Message, state: FSMContext, data: dict) -> str:
    if data["kind"] == "desc":                      # описание: сохраняем HTML с премиум-эмодзи
        value = msg_html(message)
        await check_html(message.bot, message.chat.id, value)
    else:
        value = parse_kind(data["kind"], (message.text or "").strip())
    await update_row(data["table"], data["rid"], **{data["field"]: value})
    return "✅ Сохранено."


@action("setting")
async def act_setting(message: Message, state: FSMContext, data: dict) -> str:
    value = (message.text or "").strip()
    if not value:
        raise ValueError("Нужен текст.")
    await set_setting(data["key"], value)
    return "✅ Сохранено."


@action("text")
async def act_text(message: Message, state: FSMContext, data: dict) -> str:
    value = msg_html(message)
    await check_html(message.bot, message.chat.id, value)
    await db_run("INSERT INTO texts(key, html) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET html = excluded.html",
                 (data["key"], value))
    return "✅ Текст сохранён. Премиум-эмодзи и форматирование сохранены."


async def take_media(message: Message, items: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if message.photo:
        items.append(("photo", message.photo[-1].file_id))
    elif message.video:
        items.append(("video", message.video.file_id))
    elif message.document:
        items.append(("document", message.document.file_id))
    else:
        raise ValueError("Нужно фото, видео или документ, либо «-».")
    return items


@action("media")
async def act_media(message: Message, state: FSMContext, data: dict) -> str | None:
    if (message.text or "").strip() == "-":
        return "✅ Готово."
    item = await get_row(data["table"], data["rid"])
    items = await take_media(message, parse_media(item.get("media_ids", "")))
    await update_row(data["table"], data["rid"], media_ids=join_media(items))
    await message.bot.send_message(message.chat.id, f"📎 Добавлено. Всего: {len(items)}. Отправьте ещё или «-».")
    return None


@action("tmedia")
async def act_text_media(message: Message, state: FSMContext, data: dict) -> str | None:
    if (message.text or "").strip() == "-":
        return "✅ Готово."
    items = await take_media(message, parse_media(await text_media(data["key"])))
    await db_run("INSERT INTO text_media(key, media_ids) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET media_ids = excluded.media_ids", (data["key"], join_media(items)))
    await message.bot.send_message(message.chat.id, f"📎 Добавлено. Всего: {len(items)}. Отправьте ещё или «-».")
    return None


@action("btnlabel")
async def act_button_label(message: Message, state: FSMContext, data: dict) -> str:
    label = (message.text or "").strip()
    if not 1 <= len(label) <= 64:
        raise ValueError("Текст кнопки от 1 до 64 символов.")
    await db_run("UPDATE buttons SET label = ? WHERE key = ?", (label, data["key"]))
    return "✅ Текст кнопки сохранён."


@action("omsg")
async def act_order_msg(message: Message, state: FSMContext, data: dict) -> str:
    text = (message.text or "").strip()
    if not text:
        raise ValueError("Нужен текст сообщения.")
    order = await get_row("orders", data["rid"])
    ok = await notify_client(message.bot, order["user_id"],
                             f"💬 <b>Сообщение по заказу №{order['id']}:</b>\n{esc(text)}")
    return "✅ Сообщение отправлено." if ok else "Клиент недоступен."


@action("sup_reply")
async def act_sup_reply(message: Message, state: FSMContext, data: dict) -> str:
    text = (message.text or "").strip()
    if not text:
        raise ValueError("Нужен текстовый ответ.")
    msg = await get_row("support_messages", data["rid"])
    await db_run("UPDATE support_messages SET admin_reply = ?, is_open = 0 WHERE id = ?", (text, msg["id"]))
    ok = await notify_client(message.bot, msg["user_id"],
                             f"💬 <b>Ответ поддержки</b> (обращение №{msg['id']}):\n{esc(text)}")
    return "✅ Ответ отправлен клиенту." if ok else "Клиент недоступен."


@action("admin_add")
async def act_admin_add(message: Message, state: FSMContext, data: dict) -> str:
    uid = p_int(1)((message.text or "").strip())
    await db_run("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (uid,))
    return f"✅ Пользователь {uid} назначен администратором."


@action("user_search")
async def act_user_search(message: Message, state: FSMContext, data: dict) -> str | None:
    query = (message.text or "").strip()
    if not query:
        raise ValueError("Введите ID или имя.")
    users = await search_users(query)
    if not users:
        return "Никого не найдено."
    rows = []
    for u in users[:10]:
        name = f"{u['first_name'] or ''} {('@' + u['username']) if u['username'] else ''}".strip()
        rows.append([B(f"{'🚫' if u['is_blocked'] else '✅'} {name} · {u['id']}",
                       f"adm:user:{'unblock' if u['is_blocked'] else 'block'}:{u['id']}")])
    await show(message.bot, message.chat.id, f"Найдено: {len(users)}. Нажмите, чтобы заблокировать или разблокировать:",
               KB(*rows, BACK("adm:home")), data["ui"])
    return None


@action("bc_compose")
async def act_bc_compose(message: Message, state: FSMContext, data: dict) -> str | None:
    media_type, media_id = "", ""
    if message.photo:
        media_type, media_id = "photo", message.photo[-1].file_id
    elif message.video:
        media_type, media_id = "video", message.video.file_id
    elif message.document:
        media_type, media_id = "document", message.document.file_id
    text = msg_html(message)
    if not text and not media_id:
        raise ValueError("Отправьте текст или медиа.")
    if len(text) > 4000:
        raise ValueError("Текст слишком длинный (максимум 4000 символов).")
    if text:
        await check_html(message.bot, message.chat.id, text)
    data.update(text=text, media_type=media_type, media_id=media_id, button_text="", button_url="",
                action="bc_done")
    await state.set_data(data)
    await show(message.bot, message.chat.id, "Добавить кнопку со ссылкой под сообщением?",
               KB([B("➕ Добавить кнопку", "adm:bcbtn:add", style="primary"), B("Без кнопки", "adm:bcbtn:none")],
                  BACK("adm:home")), data["ui"])
    return None


@action("bc_btn")
async def act_bc_btn(message: Message, state: FSMContext, data: dict) -> None:
    raw = (message.text or "").strip()
    if "|" not in raw:
        raise ValueError("Формат: Текст | https://ссылка")
    label, url = (p.strip() for p in raw.split("|", 1))
    if not label or len(label) > 60:
        raise ValueError("Текст кнопки от 1 до 60 символов.")
    p_url(url)
    data.update(button_text=label, button_url=url)
    await state.set_data(data)
    await bc_preview(message.bot, message.chat.id, state, data["ui"])
    return None


async def bc_preview(bot: Bot, chat_id: int, state: FSMContext, ui: int) -> None:
    data = await state.get_data()
    bc_id = await insert_row("broadcasts", text=data["text"], media_type=data["media_type"],
                             media_id=data["media_id"], button_text=data["button_text"],
                             button_url=data["button_url"])
    bc = await get_row("broadcasts", bc_id)
    markup = KB([B(bc["button_text"], url=bc["button_url"])]) if bc["button_text"] and bc["button_url"] else None
    await bot.send_message(chat_id, "👁 <b>Предпросмотр</b>:")
    senders = {"photo": bot.send_photo, "video": bot.send_video, "document": bot.send_document}
    if bc["media_type"] in senders:
        await senders[bc["media_type"]](chat_id, bc["media_id"], caption=bc["text"] or None, reply_markup=markup)
    else:
        await bot.send_message(chat_id, bc["text"], reply_markup=markup)
    total = await db_scalar("SELECT COUNT(*) FROM users WHERE is_blocked = 0 AND bot_blocked = 0")
    await state.clear()
    await show(bot, chat_id, f"Получателей: <b>{total}</b>. Запустить рассылку?", KB(
        [B("🚀 Запустить", f"adm:bcgo:{bc_id}", style="success")],
        [B("❌ Отменить", f"adm:bccancel:{bc_id}", style="danger")]), ui)


# =============================================================================
# 6. АДМИН-ПАНЕЛЬ: РАЗДЕЛЫ
# =============================================================================

@router.callback_query(F.data == "adm:home", AdminOnly())
async def adm_home(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(call)
    await show(call.bot, call.message.chat.id, "⚙️ <b>Админ-панель</b>", KB(
        [B("📊 Аналитика", "adm:analytics", style="primary"), B("📈 Статистика", "adm:stats", style="primary")],
        [B("📋 Заказы", "adm:orders"), B("💳 Оплаты", "adm:payments")],
        [B("📁 Проекты", "adm:cat:prod"), B("🧩 Шаблоны", "adm:cat:tpl")],
        [B("🛍 Товары", "adm:cat:good"), B("🖥 Хостинг", "adm:cat:host")],
        [B("📢 Рассылка", "adm:bc", style="success")],
        [B("👥 Пользователи", "adm:users"), B("📣 Подписка", "adm:subs")],
        [B("🎟 Промокоды", "adm:promos"), B("⚙️ Настройки", "adm:settings")],
        [B("⬅️ В главное меню", "nav:main")],
    ), call.message.message_id)


@router.callback_query(F.data == "adm:analytics", AdminOnly())
async def adm_analytics(call: CallbackQuery) -> None:
    await safe_answer(call)
    rev, orders, users = await revenue_stats(), await order_stats(), await user_stats()
    lines = [
        "📊 <b>Аналитика</b>",
        f"💰 Выручка: <b>{money(rev['total'])}</b>",
        f"   день {money(rev['day'])} · неделя {money(rev['week'])} · месяц {money(rev['month'])}",
        f"🧾 Заказов: {orders['total']} (день {orders['day']}, неделя {orders['week']}, месяц {orders['month']})",
        f"👥 Пользователей: {users['total']} · покупок: {rev['purchases']}",
        "", "<b>Популярные позиции:</b>",
    ]
    pop = await popular_items()
    lines += [f"• {esc(await item_title(p['kind'], p['item_id']))} — {p['cnt']}" for p in pop] or ["— нет данных"]
    await show(call.bot, call.message.chat.id, "\n".join(lines), KB(BACK("adm:home")), call.message.message_id)


@router.callback_query(F.data == "adm:stats", AdminOnly())
async def adm_stats(call: CallbackQuery) -> None:
    await safe_answer(call)
    u, o = await user_stats(), await order_stats()
    text = (f"📈 <b>Статистика</b>\n👥 Всего: {u['total']}\n🆕 Новых: день {u['new_day']}, неделя {u['new_week']}, "
            f"месяц {u['new_month']}\n🔥 Активных за неделю: {u['active_week']}\n"
            f"🚫 Заблокировали бота: {u['blocked_bot']}\n🔒 Заблокированы админом: {u['blocked_admin']}\n"
            f"🧾 Заказов: {o['total']} · незавершённых: {o['unfinished']}")
    await show(call.bot, call.message.chat.id, text, KB(BACK("adm:home")), call.message.message_id)


# --- заказы ---

@router.callback_query(F.data == "adm:orders", AdminOnly())
async def adm_orders(call: CallbackQuery) -> None:
    await safe_answer(call)
    rows = []
    for key, label in ORDER_STATUSES.items():
        cnt = await db_scalar("SELECT COUNT(*) FROM orders WHERE status = ?", (key,))
        rows.append([B(f"{label} ({cnt})", f"adm:olist:{key}")])
    rows.append([B("🗂 Все", "adm:olist:all")])
    await show(call.bot, call.message.chat.id, "📋 <b>Заказы по статусам</b>",
               KB(*rows, BACK("adm:home")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:olist:"), AdminOnly())
async def adm_olist(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    await safe_answer(call)
    where, params = ("", ()) if key == "all" else ("status = ?", (key,))
    orders = await list_rows("orders", where, params, limit=30)
    rows = [[B(f"№{o['id']} · {(o['name'] or o['kind'])[:25]} · {ORDER_STATUSES[o['status']]}", f"adm:order:{o['id']}")]
            for o in orders]
    await show(call.bot, call.message.chat.id, "Выберите заказ:" if orders else "Заказов нет.",
               KB(*rows, BACK("adm:orders")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:order:"), AdminOnly())
async def adm_order(call: CallbackQuery) -> None:
    oid = int(call.data.split(":")[2])
    o = await get_row("orders", oid)
    if not o:
        await safe_answer(call, "Заказ не найден.", show_alert=True)
        return
    await safe_answer(call)
    user = await get_user(o["user_id"])
    uname = f"@{user['username']}" if user and user.get("username") else "без username"
    text = (f"📋 <b>Заказ №{o['id']}</b>\n👤 {esc(user['first_name'] if user else '')} ({uname}), "
            f"ID <code>{o['user_id']}</code>\n📌 Тип: {esc(o['kind'])}\n"
            f"🔖 Статус: {ORDER_STATUSES[o['status']]}\n💰 Стоимость: {money(o['price'])}\n\n"
            f"<b>Название:</b> {esc(o['name'])}\n<b>Функционал:</b> {esc(o['description'])}\n"
            f"<b>Референсы:</b> {esc(o['references_'])}\n<b>Бюджет:</b> {esc(o['budget'])}\n"
            f"<b>Сроки:</b> {esc(o['deadline'])}\n<b>Контакт:</b> {esc(o['contact'])}\n"
            f"<b>Комментарий:</b> {o['comment'] or '—'}")
    await show(call.bot, call.message.chat.id, text, KB(
        [B("🔄 Статус", f"adm:ost:{oid}", style="primary"),
         B("💰 Стоимость", f"adm:fld:orders:price:int:{oid}:adm:order:{oid}")],
        [B("💬 Комментарий", f"adm:fld:orders:comment:desc:{oid}:adm:order:{oid}"),
         B("✉️ Написать", f"adm:omsg:{oid}")],
        [B("💳 Запросить оплату", f"adm:opay:{oid}", style="success"),
         B("❌ Отменить", f"adm:ocancel:{oid}", style="danger")],
        BACK("adm:orders")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:ost:"), AdminOnly())
async def adm_status_pick(call: CallbackQuery) -> None:
    oid = int(call.data.split(":")[2])
    await safe_answer(call)
    rows = [[B(label, f"adm:oset:{oid}:{key}")] for key, label in ORDER_STATUSES.items()]
    await show(call.bot, call.message.chat.id, "Выберите новый статус:", KB(*rows, BACK(f"adm:order:{oid}")),
               call.message.message_id)


@router.callback_query(F.data.startswith("adm:oset:"), AdminOnly())
async def adm_status_set(call: CallbackQuery) -> None:
    _, _, raw, key = call.data.split(":")
    o = await get_row("orders", int(raw))
    if not o or key not in ORDER_STATUSES:
        await safe_answer(call, "Ошибка.", show_alert=True)
        return
    await update_row("orders", o["id"], status=key)
    await notify_client(call.bot, o["user_id"], f"ℹ️ Статус заказа №{o['id']}: <b>{ORDER_STATUSES[key]}</b>")
    await safe_answer(call, "Статус изменён.")
    await show(call.bot, call.message.chat.id, f"✅ Заказ №{o['id']}: {ORDER_STATUSES[key]}",
               KB(BACK(f"adm:order:{o['id']}")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:opay:"), AdminOnly())
async def adm_request_pay(call: CallbackQuery) -> None:
    o = await get_row("orders", int(call.data.split(":")[2]))
    if not o or o["price"] <= 0:
        await safe_answer(call, "Сначала укажите стоимость заказа.", show_alert=True)
        return
    await update_row("orders", o["id"], status="awaiting_payment")
    sent = await notify_client(call.bot, o["user_id"],
                               f"💳 По заказу №{o['id']} ожидается оплата: <b>{money(o['price'])}</b>.",
                               KB([await bt("pay", f"pay:open:{o['id']}")]))
    await safe_answer(call, "Запрос отправлен." if sent else "Клиент недоступен.", show_alert=not sent)


@router.callback_query(F.data.startswith("adm:ocancel:"), AdminOnly())
async def adm_cancel_order(call: CallbackQuery) -> None:
    o = await get_row("orders", int(call.data.split(":")[2]))
    if not o:
        await safe_answer(call, "Заказ не найден.", show_alert=True)
        return
    await update_row("orders", o["id"], status="cancelled")
    await notify_client(call.bot, o["user_id"], f"❌ Заказ №{o['id']} отменён. Вопросы — в «💬 Уточнить».")
    await safe_answer(call, "Заказ отменён.")


@router.callback_query(F.data.startswith("adm:omsg:"), AdminOnly())
async def adm_order_msg(call: CallbackQuery, state: FSMContext) -> None:
    oid = int(call.data.split(":")[2])
    await safe_answer(call)
    await ask(call, state, "omsg", "Введите сообщение клиенту:", f"adm:order:{oid}", rid=oid)


# --- оплаты ---

@router.callback_query(F.data == "adm:payments", AdminOnly())
async def adm_payments(call: CallbackQuery) -> None:
    await safe_answer(call)
    pending = await list_rows("payments", "status = 'pending'", order="id", limit=30)
    rows = [[B(f"Оплата #{p['id']} · заказ №{p['order_id']} · {money(p['amount'])}", f"adm:pay:{p['id']}")]
            for p in pending]
    await show(call.bot, call.message.chat.id,
               "💳 <b>Оплаты на проверке</b>" if pending else "Ожидающих оплат нет.",
               KB(*rows, BACK("adm:home")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:pay:"), AdminOnly())
async def adm_payment_view(call: CallbackQuery) -> None:
    p = await get_row("payments", int(call.data.split(":")[2]))
    if not p:
        await safe_answer(call, "Не найдено.", show_alert=True)
        return
    await safe_answer(call)
    user = await get_user(p["user_id"])
    method = await db_one("SELECT title FROM payment_methods WHERE code = ?", (p["method"],))
    text = (f"💳 <b>Оплата #{p['id']}</b> · заказ №{p['order_id']}\n"
            f"Клиент: {esc(user['first_name'] if user else '')} (ID <code>{p['user_id']}</code>)\n"
            f"Способ: {esc(method['title'] if method else p['method'])} · {money(p['amount'])}\n"
            f"Комментарий: {esc(p['proof_text']) if p['proof_text'] else '—'}\n\n"
            "⚠️ Проверьте поступление средств вручную.")
    if p["proof_file_id"]:
        senders = {"photo": call.bot.send_photo, "video": call.bot.send_video, "document": call.bot.send_document}
        if p["proof_type"] in senders:
            await senders[p["proof_type"]](call.message.chat.id, p["proof_file_id"])
    await show(call.bot, call.message.chat.id, text, KB(
        [B("✅ Подтвердить", f"adm:pok:{p['id']}", style="success"),
         B("❌ Отклонить", f"adm:pno:{p['id']}", style="danger")],
        BACK("adm:payments")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:pok:"), AdminOnly())
async def adm_pay_ok(call: CallbackQuery) -> None:
    p = await get_row("payments", int(call.data.split(":")[2]))
    if not p or p["status"] != "pending":
        await safe_answer(call, "Уже обработано.", show_alert=True)
        return
    await db_run("UPDATE payments SET status = 'confirmed', checked_at = datetime('now') WHERE id = ?", (p["id"],))
    await update_row("orders", p["order_id"], status="in_progress")
    await notify_client(call.bot, p["user_id"], f"✅ Оплата по заказу №{p['order_id']} подтверждена. Спасибо!")
    await safe_answer(call, "Оплата подтверждена.")
    await show(call.bot, call.message.chat.id, f"✅ Оплата #{p['id']} подтверждена, заказ в работе.",
               KB(BACK("adm:payments")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:pno:"), AdminOnly())
async def adm_pay_no(call: CallbackQuery) -> None:
    p = await get_row("payments", int(call.data.split(":")[2]))
    if not p or p["status"] != "pending":
        await safe_answer(call, "Уже обработано.", show_alert=True)
        return
    await db_run("UPDATE payments SET status = 'rejected', checked_at = datetime('now') WHERE id = ?", (p["id"],))
    await notify_client(call.bot, p["user_id"],
                        f"❌ Оплата по заказу №{p['order_id']} не подтверждена. Проверьте реквизиты и отправьте снова.")
    await safe_answer(call, "Оплата отклонена.")
    await show(call.bot, call.message.chat.id, f"❌ Оплата #{p['id']} отклонена.", KB(BACK("adm:payments")),
               call.message.message_id)


@router.callback_query(F.data.startswith("adm:sup:"), AdminOnly())
async def adm_sup(call: CallbackQuery, state: FSMContext) -> None:
    mid = int(call.data.split(":")[2])
    await safe_answer(call)
    await ask(call, state, "sup_reply", "Введите ответ клиенту:", "adm:home", rid=mid)


# --- каталог: шаблоны, проекты, товары, тарифы ---

CATALOG = {
    "tpl": {"table": "templates", "title": "Шаблон", "where": "", "add": "tpl_add",
            "fields": [("title", "Название", "text"), ("price", "Цена", "int"), ("category", "Категория", "tcat"),
                       ("description", "Описание", "desc"), ("features", "Возможности", "text"),
                       ("demo_url", "Демо", "url_opt")]},
    "prod": {"table": "products", "title": "Проект", "where": "kind = 'project'", "add": "prod_add_project",
             "fields": [("title", "Название", "text"), ("price", "Цена", "int"), ("category", "Категория", "cat"),
                        ("description", "Описание", "desc"), ("features", "Функционал", "text"),
                        ("license", "Лицензия", "text"), ("sale_terms", "Условия продажи", "desc"),
                        ("demo_url", "Демо", "url_opt")]},
    "good": {"table": "products", "title": "Товар", "where": "kind = 'product'", "add": "prod_add_good",
             "fields": [("title", "Название", "text"), ("price", "Цена", "int"), ("category", "Категория", "cat"),
                        ("description", "Описание", "desc"), ("features", "Характеристики", "text"),
                        ("license", "Лицензия", "text"), ("sale_terms", "Условия продажи", "desc"),
                        ("demo_url", "Ссылка", "url_opt")]},
    "host": {"table": "hosting_plans", "title": "Тариф", "where": "", "add": "host_add",
             "fields": [("title", "Название", "text"), ("price", "Цена", "int"), ("cpu", "CPU", "text"),
                        ("ram", "RAM", "text"), ("disk", "Диск", "text"), ("traffic", "Трафик", "text"),
                        ("period", "Срок", "text"), ("description", "Описание", "desc")]},
}


@router.callback_query(F.data.startswith("adm:cat:"), AdminOnly())
async def adm_cat(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    spec = CATALOG[key]
    await safe_answer(call)
    items = await list_rows(spec["table"], spec["where"], order="id DESC", limit=50)
    rows = [[B(f"{'✅' if i.get('is_published', 1) else '🚫'} {i['title']}", f"adm:item:{key}:{i['id']}")]
            for i in items]
    await show(call.bot, call.message.chat.id, f"<b>{spec['title']}</b>: список",
               KB([B("➕ Добавить", f"adm:add:{spec['add']}:adm:cat:{key}", style="success")], *rows,
                  BACK("adm:home")), call.message.message_id)


async def render_item(bot: Bot, chat_id: int, mid: int, key: str, rid: int) -> None:
    spec = CATALOG[key]
    item = await get_row(spec["table"], rid)
    if not item:
        await show(bot, chat_id, "Не найдено.", KB(BACK(f"adm:cat:{key}")), mid)
        return
    back = f"adm:item:{key}:{rid}"
    lines = [f"<b>{spec['title']}:</b> {esc(item['title'])}"]
    if "price" in item:
        lines.append(f"💰 Цена: {money(item['price'])}")
    lines.append(f"Статус: {'опубликован' if item.get('is_published', 1) else 'скрыт'}")
    lines.append(f"Медиа: {len(parse_media(item.get('media_ids', '')))} шт.")
    rows = [[B(f"✏️ {label}", f"adm:fld:{spec['table']}:{field}:{kind}:{rid}:{back}")]
            for field, label, kind in spec["fields"]]
    rows.append([B("📎 Добавить медиа", f"adm:media:{key}:{rid}"), B("🗑 Очистить медиа", f"adm:mclear:{key}:{rid}")])
    if spec["table"] == "products":
        rows.append([B(f"📦 Наличие: {'да' if item['in_stock'] else 'нет'}", f"adm:stock:{rid}")])
    rows.append([B("👁 Опубликовать / скрыть", f"adm:tog:{key}:{rid}", style="primary"),
                 B("🗑 Удалить", f"adm:del:{key}:{rid}", style="danger")])
    rows.append(BACK(f"adm:cat:{key}"))
    await show(bot, chat_id, "\n".join(lines), KB(*rows), mid)


@router.callback_query(F.data.startswith("adm:item:"), AdminOnly())
async def adm_item(call: CallbackQuery) -> None:
    _, _, key, raw = call.data.split(":")
    await safe_answer(call)
    await render_item(call.bot, call.message.chat.id, call.message.message_id, key, int(raw))


@router.callback_query(F.data.startswith("adm:fld:"), AdminOnly())
async def adm_field(call: CallbackQuery, state: FSMContext) -> None:
    parts = call.data.split(":", 6)          # adm:fld:TABLE:FIELD:KIND:ID:BACK
    table, field, kind, rid, back = parts[2], parts[3], parts[4], int(parts[5]), parts[6]
    await safe_answer(call)
    await ask(call, state, "field", f"Введите новое значение поля «{field}»:"
              + ("\n\n<i>Можно с премиум-эмодзи и форматированием.</i>" if kind == "desc" else ""),
              back, table=table, field=field, kind=kind, rid=rid)


@router.callback_query(F.data.startswith("adm:tog:"), AdminOnly())
async def adm_toggle(call: CallbackQuery) -> None:
    _, _, key, raw = call.data.split(":")
    table = CATALOG[key]["table"]
    item = await get_row(table, int(raw))
    await update_row(table, item["id"], is_published=0 if item["is_published"] else 1)
    await safe_answer(call, "Готово.")
    await render_item(call.bot, call.message.chat.id, call.message.message_id, key, item["id"])


@router.callback_query(F.data.startswith("adm:stock:"), AdminOnly())
async def adm_stock(call: CallbackQuery) -> None:
    rid = int(call.data.split(":")[2])
    item = await get_row("products", rid)
    await update_row("products", rid, in_stock=0 if item["in_stock"] else 1)
    await safe_answer(call, "Готово.")
    await render_item(call.bot, call.message.chat.id, call.message.message_id,
                      "good" if item["kind"] == "product" else "prod", rid)


@router.callback_query(F.data.startswith("adm:mclear:"), AdminOnly())
async def adm_media_clear(call: CallbackQuery) -> None:
    _, _, key, raw = call.data.split(":")
    await update_row(CATALOG[key]["table"], int(raw), media_ids="")
    await safe_answer(call, "Медиа удалено.")
    await render_item(call.bot, call.message.chat.id, call.message.message_id, key, int(raw))


@router.callback_query(F.data.startswith("adm:media:"), AdminOnly())
async def adm_media(call: CallbackQuery, state: FSMContext) -> None:
    _, _, key, raw = call.data.split(":")
    await safe_answer(call)
    await ask(call, state, "media", "Отправьте фото, видео или документ. Можно несколько по очереди. "
                                    "Чтобы закончить — «-».", f"adm:item:{key}:{raw}",
              table=CATALOG[key]["table"], rid=int(raw))


@router.callback_query(F.data.startswith("adm:del:"), AdminOnly())
async def adm_del(call: CallbackQuery) -> None:
    _, _, key, raw = call.data.split(":")
    await safe_answer(call)
    await show(call.bot, call.message.chat.id, "Удалить позицию безвозвратно?", KB(
        [B("🗑 Да, удалить", f"adm:delok:{key}:{raw}", style="danger")], BACK(f"adm:item:{key}:{raw}")),
        call.message.message_id)


@router.callback_query(F.data.startswith("adm:delok:"), AdminOnly())
async def adm_delok(call: CallbackQuery) -> None:
    _, _, key, raw = call.data.split(":")
    await delete_row(CATALOG[key]["table"], int(raw))
    await safe_answer(call, "Удалено.")
    await show(call.bot, call.message.chat.id, "🗑 Удалено.", KB(BACK(f"adm:cat:{key}")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:add:"), AdminOnly())
async def adm_add(call: CallbackQuery, state: FSMContext) -> None:
    _, _, name, back = call.data.split(":", 3)
    await safe_answer(call)
    data = {"action": "wiz", "wiz": name, "step": 0, "vals": {}, "ui": call.message.message_id, "back": back}
    await state.set_state(AdminFSM.input)
    await state.set_data(data)
    await prompt_step(call.bot, call.message.chat.id, data)


CAT_TPL = TPL_CAT_CHOICE


def fin_item(table: str, build: Callable[[dict], dict], ok: str):
    async def fin(v: dict) -> str:
        await insert_row(table, **build(v))
        return ok
    return fin


wizard("tpl_add", [
    ("category", "Категория: «сайт» или «бот»", p_choice(TPL_CAT_CHOICE)),
    ("title", "Название шаблона", p_text(2, 100)),
    ("description", "Описание (можно с премиум-эмодзи)", p_text(2, 1500)),
    ("features", "Возможности (через запятую)", p_text(2, 1500)),
    ("price", "Стоимость, ₽", p_int(0)),
    ("demo_url", "(необязательно) Демо-ссылка, https://… или «-»", p_url),
], fin_item("templates", lambda v: dict(category=v["category"], title=v["title"], description=v["description"],
                                       features=v["features"], price=v["price"], demo_url=v.get("demo_url")),
            "✅ Шаблон добавлен. Медиа — в карточке шаблона."))

_PROD_FIELDS = lambda kind: [  # noqa: E731
    ("category", "Категория: telegram-бот, сайт, mini app или другое", p_choice(CAT_CHOICE)),
    ("title", "Название", p_text(2, 100)),
    ("description", "Описание", p_text(2, 1500)),
    ("features", "Функционал / характеристики", p_text(2, 1500)),
    ("price", "Стоимость, ₽", p_int(0)),
    ("license", "Тип лицензии", p_text(2, 100)),
    ("sale_terms", "(необязательно) Условия продажи или «-»", p_text(2, 1000)),
    ("demo_url", "(необязательно) Ссылка или «-»", p_url),
]
_PROD_BUILD = lambda kind: (lambda v: dict(kind=kind, category=v["category"], title=v["title"],  # noqa: E731
                                           description=v["description"], features=v["features"], price=v["price"],
                                           license=v["license"], sale_terms=v["sale_terms"] or "",
                                           demo_url=v.get("demo_url")))
wizard("prod_add_project", _PROD_FIELDS("project"),
       fin_item("products", _PROD_BUILD("project"), "✅ Проект опубликован. Медиа — в карточке."))
wizard("prod_add_good", _PROD_FIELDS("product"),
       fin_item("products", _PROD_BUILD("product"), "✅ Товар опубликован. Медиа — в карточке."))
wizard("host_add", [
    ("title", "Название тарифа", p_text(2, 100)),
    ("price", "Стоимость, ₽ в месяц", p_int(0)),
    ("cpu", "CPU, например: 1 vCPU", p_text(1, 100)),
    ("ram", "RAM, например: 1 ГБ", p_text(1, 100)),
    ("disk", "Диск, например: 10 ГБ SSD", p_text(1, 100)),
    ("traffic", "Трафик, например: 100 ГБ", p_text(1, 100)),
    ("period", "Срок, например: 30 дней", p_text(1, 100)),
    ("description", "(необязательно) Описание или «-»", p_text(2, 1000)),
], fin_item("hosting_plans", lambda v: dict(title=v["title"], price=v["price"], cpu=v["cpu"], ram=v["ram"],
                                           disk=v["disk"], traffic=v["traffic"], period=v["period"],
                                           description=v["description"] or ""), "✅ Тариф добавлен."))


# --- способы оплаты: реквизиты, ссылки, включение ---

async def _fin_pm(v: dict) -> str:
    await insert_row("payment_methods", code=secrets.token_hex(4), title=v["title"],
                     details=v["details"], url=v["url"] or "")
    return "✅ Способ оплаты добавлен."


wizard("pm_add", [
    ("title", "Название, например: 💎 Tonkeeper", p_text(2, 60)),
    ("details", "Реквизиты или инструкция (можно с премиум-эмодзи)", p_text(3, 1500)),
    ("url", "(необязательно) Ссылка на оплату, https://… или «-»", p_url),
], _fin_pm)


@router.callback_query(F.data == "adm:pms", AdminOnly())
async def adm_pms(call: CallbackQuery) -> None:
    await safe_answer(call)
    rows = [[B("➕ Добавить способ оплаты", "adm:add:pm_add:adm:pms", style="success")]]
    for m in await list_rows("payment_methods", order="id", limit=30):
        rows.append([B(f"{'✅' if m['is_active'] else '⏸'} {m['title']}", f"adm:pm:{m['id']}")])
    await show(call.bot, call.message.chat.id, "💳 <b>Способы оплаты и реквизиты</b>\nНажмите способ, чтобы изменить.",
               KB(*rows, BACK("adm:settings")), call.message.message_id)


async def render_pm(bot: Bot, chat_id: int, mid: int, pid: int) -> None:
    m = await get_row("payment_methods", pid)
    if not m:
        await show(bot, chat_id, "Не найдено.", KB(BACK("adm:pms")), mid)
        return
    back = f"adm:pm:{pid}"
    await show(bot, chat_id,
               f"<b>{esc(m['title'])}</b>\nСтатус: {'включён' if m['is_active'] else 'выключен'}\n"
               f"Ссылка: {esc(m['url']) if m['url'] else '—'}\n\n<b>Реквизиты:</b>\n{m['details']}",
               KB([B("✏️ Название", f"adm:fld:payment_methods:title:text:{pid}:{back}"),
                   B("📝 Реквизиты", f"adm:fld:payment_methods:details:desc:{pid}:{back}")],
                  [B("🔗 Ссылка", f"adm:fld:payment_methods:url:url_opt:{pid}:{back}")],
                  [B("⏯ Вкл/выкл", f"adm:pmflip:{pid}", style="primary"),
                   B("🗑 Удалить", f"adm:pmdel:{pid}", style="danger")],
                  BACK("adm:pms")), mid)


@router.callback_query(F.data.startswith("adm:pm:"), AdminOnly())
async def adm_pm_view(call: CallbackQuery) -> None:
    await safe_answer(call)
    await render_pm(call.bot, call.message.chat.id, call.message.message_id, int(call.data.split(":")[2]))


@router.callback_query(F.data.startswith("adm:pmflip:"), AdminOnly())
async def adm_pm_flip(call: CallbackQuery) -> None:
    pid = int(call.data.split(":")[2])
    m = await get_row("payment_methods", pid)
    await update_row("payment_methods", pid, is_active=0 if m["is_active"] else 1)
    await safe_answer(call, "Сохранено.")
    await render_pm(call.bot, call.message.chat.id, call.message.message_id, pid)


@router.callback_query(F.data.startswith("adm:pmdel:"), AdminOnly())
async def adm_pm_del(call: CallbackQuery) -> None:
    await delete_row("payment_methods", int(call.data.split(":")[2]))
    await safe_answer(call, "Удалено.")
    await show(call.bot, call.message.chat.id, "🗑 Удалено.", KB(BACK("adm:pms")), call.message.message_id)


# --- тексты, кнопки, настройки ---

@router.callback_query(F.data == "adm:texts", AdminOnly())
async def adm_texts(call: CallbackQuery) -> None:
    await safe_answer(call)
    rows = [[B(label, f"adm:text:{key}")] for key, label in TEXT_LABELS.items()]
    await show(call.bot, call.message.chat.id, "📝 <b>Тексты экранов</b>\nВыберите экран:",
               KB(*rows, BACK("adm:settings")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:text:"), AdminOnly())
async def adm_text_view(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    await safe_answer(call)
    media = len(parse_media(await text_media(key)))
    await show(call.bot, call.message.chat.id,
               f"<b>{TEXT_LABELS.get(key, key)}</b>\n\nТекущий вид:\n\n{await T(key)}\n\n<i>Медиа: {media} шт.</i>",
               KB([B("✏️ Изменить текст", f"adm:textedit:{key}", style="primary")],
                  [B("📎 Медиа", f"adm:tmedia:{key}"), B("🗑 Убрать медиа", f"adm:tmclear:{key}")],
                  [B("↩️ По умолчанию", f"adm:textreset:{key}", style="danger")],
                  BACK("adm:texts")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:textedit:"), AdminOnly())
async def adm_text_edit(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":")[2]
    await safe_answer(call)
    await ask(call, state, "text", "Отправьте новый текст. Жирный, ссылки и премиум-эмодзи сохранятся.",
              f"adm:text:{key}", key=key)


@router.callback_query(F.data.startswith("adm:tmedia:"), AdminOnly())
async def adm_text_media(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":")[2]
    await safe_answer(call)
    await ask(call, state, "tmedia", "Отправьте фото, видео или документ для этого экрана. "
                                     "Можно несколько. Чтобы закончить — «-».", f"adm:text:{key}", key=key)


@router.callback_query(F.data.startswith("adm:tmclear:"), AdminOnly())
async def adm_text_media_clear(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    await db_run("UPDATE text_media SET media_ids = '' WHERE key = ?", (key,))
    await safe_answer(call, "Медиа убрано.")
    call.data = f"adm:text:{key}"
    await adm_text_view(call)


@router.callback_query(F.data.startswith("adm:textreset:"), AdminOnly())
async def adm_text_reset(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    await db_run("UPDATE texts SET html = ? WHERE key = ?", (DEFAULT_TEXTS.get(key, ""), key))
    await safe_answer(call, "Сброшено.")
    call.data = f"adm:text:{key}"
    await adm_text_view(call)


@router.callback_query(F.data == "adm:btns", AdminOnly())
async def adm_btns(call: CallbackQuery) -> None:
    await safe_answer(call)
    rows = []
    for key in BUTTON_DEFAULTS:
        label, style = await button_def(key)
        rows.append([B(f"{label} · {BUTTON_STYLE_NAMES.get(style, '')}", f"adm:btn:{key}")])
    await show(call.bot, call.message.chat.id, "🔘 <b>Кнопки</b>\nНажмите кнопку, чтобы изменить текст или цвет.",
               KB(*rows, BACK("adm:settings")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:btn:"), AdminOnly())
async def adm_btn_view(call: CallbackQuery) -> None:
    key = call.data.split(":")[2]
    await safe_answer(call)
    label, style = await button_def(key)
    await show(call.bot, call.message.chat.id,
               f"Кнопка: <b>{esc(label)}</b>\nЦвет: {BUTTON_STYLE_NAMES.get(style, '')}",
               KB([B("✏️ Текст", f"adm:btnlabel:{key}")],
                  [B("⚪ Обычная", f"adm:bst:{key}:none"), B("🔵 Синяя", f"adm:bst:{key}:primary", style="primary")],
                  [B("🟢 Зелёная", f"adm:bst:{key}:success", style="success"),
                   B("🔴 Красная", f"adm:bst:{key}:danger", style="danger")],
                  BACK("adm:btns")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:btnlabel:"), AdminOnly())
async def adm_btn_label(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":")[2]
    await safe_answer(call)
    await ask(call, state, "btnlabel", "Введите новый текст кнопки (до 64 символов).", f"adm:btn:{key}", key=key)


@router.callback_query(F.data.startswith("adm:bst:"), AdminOnly())
async def adm_btn_style(call: CallbackQuery) -> None:
    _, _, key, style = call.data.split(":")
    await db_run("UPDATE buttons SET style = ? WHERE key = ?", ("" if style == "none" else style, key))
    await safe_answer(call, "Цвет сохранён.")
    call.data = f"adm:btn:{key}"
    await adm_btn_view(call)


@router.callback_query(F.data == "adm:settings", AdminOnly())
async def adm_settings(call: CallbackQuery) -> None:
    await safe_answer(call)
    notify = await get_setting("notify_admins", "1") == "1"
    rows = [
        [B("💳 Способы оплаты и реквизиты", "adm:pms", style="primary")],
        [B("📝 Тексты экранов и медиа", "adm:texts")],
        [B("🖼 Медиа на все экраны сразу", "adm:allmedia", style="primary")],
        [B("🧹 Убрать медиа со всех экранов", "adm:allclear", style="danger")],
        [B("🔘 Кнопки: текст и цвет", "adm:btns")],
        [B("🏷 Название студии", "adm:setf:studio_name")],
        [B("🖊 Описание студии", "adm:setf:studio_description")],
        [B("📞 Контакт поддержки", "adm:setf:support_contact")],
        [B(f"🔔 Уведомления админам: {'вкл' if notify else 'выкл'}", "adm:notify")],
        [B("👮 Добавить администратора", "adm:addadmin", style="success")],
    ]
    await show(call.bot, call.message.chat.id, "⚙️ <b>Настройки</b>", KB(*rows, BACK("adm:home")),
               call.message.message_id)


@router.callback_query(F.data.startswith("adm:setf:"), AdminOnly())
async def adm_setting_ask(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.split(":")[2]
    await safe_answer(call)
    await ask(call, state, "setting", f"Текущее значение:\n{esc(await get_setting(key))}\n\nВведите новое:",
              "adm:settings", key=key)


@router.callback_query(F.data == "adm:notify", AdminOnly())
async def adm_notify(call: CallbackQuery) -> None:
    now = "0" if await get_setting("notify_admins", "1") == "1" else "1"
    await set_setting("notify_admins", now)
    await safe_answer(call, "Сохранено.")
    call.data = "adm:settings"
    await adm_settings(call)


@router.callback_query(F.data == "adm:addadmin", AdminOnly())
async def adm_addadmin(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    await ask(call, state, "admin_add", "Введите Telegram ID нового администратора (число).", "adm:settings")


# --- подписка, промокоды, пользователи, рассылка ---

@router.callback_query(F.data == "adm:subs", AdminOnly())
async def adm_subs(call: CallbackQuery) -> None:
    await safe_answer(call)
    enabled = await get_setting("sub_enabled", "1") == "1"
    rows = [[B("🔕 Выключить проверку" if enabled else "🔔 Включить проверку", "adm:subchk", style="primary")],
            [B("➕ Добавить канал", "adm:add:sub_add:adm:subs", style="success")]]
    for c in await list_rows("subscriptions", order="id"):
        rows.append([B(f"{'✅' if c['is_active'] else '⏸'} {c['title']}", f"adm:subitem:{c['id']}")])
    await show(call.bot, call.message.chat.id, "📣 <b>Обязательная подписка</b>", KB(*rows, BACK("adm:home")),
               call.message.message_id)


@router.callback_query(F.data == "adm:subchk", AdminOnly())
async def adm_subchk(call: CallbackQuery) -> None:
    now = "0" if await get_setting("sub_enabled", "1") == "1" else "1"
    await set_setting("sub_enabled", now)
    await safe_answer(call, "Сохранено.")
    call.data = "adm:subs"
    await adm_subs(call)


@router.callback_query(F.data.startswith("adm:subitem:"), AdminOnly())
async def adm_subitem(call: CallbackQuery) -> None:
    sid = int(call.data.split(":")[2])
    await safe_answer(call)
    c = await get_row("subscriptions", sid)
    await show(call.bot, call.message.chat.id,
               f"<b>{esc(c['title'])}</b>\nID/@: <code>{esc(c['chat_ref'])}</code>\nСсылка: {esc(c['invite_url'])}\n"
               f"Статус: {'активен' if c['is_active'] else 'выключен'}",
               KB([B("⏯ Вкл/выкл", f"adm:subflip:{sid}", style="primary"),
                   B("🗑 Удалить", f"adm:subdel:{sid}", style="danger")],
                  BACK("adm:subs")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:subflip:"), AdminOnly())
async def adm_subflip(call: CallbackQuery) -> None:
    sid = int(call.data.split(":")[2])
    c = await get_row("subscriptions", sid)
    await update_row("subscriptions", sid, is_active=0 if c["is_active"] else 1)
    call.data = f"adm:subitem:{sid}"
    await adm_subitem(call)


@router.callback_query(F.data.startswith("adm:subdel:"), AdminOnly())
async def adm_subdel(call: CallbackQuery) -> None:
    await delete_row("subscriptions", int(call.data.split(":")[2]))
    call.data = "adm:subs"
    await adm_subs(call)


async def _fin_sub(v: dict) -> str:
    await insert_row("subscriptions", title=v["title"], chat_ref=v["chat_ref"], invite_url=v["invite_url"])
    return "✅ Канал добавлен."


wizard("sub_add", [
    ("title", "Название канала", p_text(2, 60)),
    ("chat_ref", "ID или @username канала (бот должен быть админом)", p_text(3, 100)),
    ("invite_url", "Ссылка на канал, https://t.me/…", p_url),
], _fin_sub)


@router.callback_query(F.data == "adm:promos", AdminOnly())
async def adm_promos(call: CallbackQuery) -> None:
    await safe_answer(call)
    rows = [[B("➕ Создать промокод", "adm:add:promo_add:adm:promos", style="success")]]
    for p in await list_rows("promo_codes", order="id DESC", limit=30):
        value = f"{p['amount']}%" if p["discount_type"] == "percent" else money(p["amount"])
        rows.append([B(f"{'✅' if p['is_active'] else '⏸'} {p['code']} · -{value} · {p['uses']}/{p['max_uses'] or '∞'}",
                       f"adm:promotog:{p['id']}")])
    await show(call.bot, call.message.chat.id, "🎟 <b>Промокоды</b> (нажмите, чтобы включить или выключить)",
               KB(*rows, BACK("adm:home")), call.message.message_id)


@router.callback_query(F.data.startswith("adm:promotog:"), AdminOnly())
async def adm_promo_tog(call: CallbackQuery) -> None:
    pid = int(call.data.split(":")[2])
    p = await get_row("promo_codes", pid)
    await update_row("promo_codes", pid, is_active=0 if p["is_active"] else 1)
    call.data = "adm:promos"
    await adm_promos(call)


async def _fin_promo(v: dict) -> str:
    try:
        await insert_row("promo_codes", code=v["code"].upper(), discount_type=v["discount_type"],
                         amount=v["amount"], max_uses=v["max_uses"] or 0, expires_at=v["expires"])
    except sqlite3.IntegrityError:
        return "⚠️ Промокод с таким кодом уже есть."
    return f"✅ Промокод {v['code'].upper()} создан."


wizard("promo_add", [
    ("code", "Код промокода, например SPRING26", p_text(3, 32)),
    ("discount_type", "Тип скидки: «процент» или «фикс»",
     p_choice({"процент": "percent", "фикс": "fixed", "percent": "percent", "fixed": "fixed"})),
    ("amount", "Размер: процент (1–100) или сумма в ₽", p_int(1)),
    ("max_uses", "Лимит использований (0 — без ограничения)", p_int(0)),
    ("expires", "(необязательно) Дата окончания ГГГГ-ММ-ДД или «-»", p_date),
], _fin_promo)


@router.callback_query(F.data == "adm:users", AdminOnly())
async def adm_users(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    await ask(call, state, "user_search", "Введите Telegram ID или имя пользователя:", "adm:home")


@router.callback_query(F.data.startswith("adm:user:"), AdminOnly())
async def adm_user_flag(call: CallbackQuery) -> None:
    _, _, act, raw = call.data.split(":")
    await set_user_flag(int(raw), "is_blocked", act == "block")
    await safe_answer(call, "Готово.")
    await show(call.bot, call.message.chat.id,
               "🚫 Пользователь заблокирован." if act == "block" else "🔓 Пользователь разблокирован.",
               KB(BACK("adm:home")), call.message.message_id)


@router.callback_query(F.data == "adm:bc", AdminOnly())
async def adm_bc(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    await ask(call, state, "bc_compose",
              "📢 <b>Новая рассылка</b>\nОтправьте текст (с премиум-эмодзи и форматированием) или медиа с подписью.",
              "adm:home")


@router.callback_query(F.data == "adm:bcbtn:none", AdminOnly())
async def adm_bc_nobtn(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    await bc_preview(call.bot, call.message.chat.id, state, call.message.message_id)


@router.callback_query(F.data == "adm:bcbtn:add", AdminOnly())
async def adm_bc_addbtn(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    data = await state.get_data()
    data.update(action="bc_btn", ui=call.message.message_id)
    await state.set_state(AdminFSM.input)
    await state.set_data(data)
    await show(call.bot, call.message.chat.id, "Введите кнопку: <code>Текст | https://ссылка</code>",
               KB([B("❌ Отмена", "adm:home", style="danger")]), call.message.message_id)


@router.callback_query(F.data.startswith("adm:bcgo:"), AdminOnly())
async def adm_bc_go(call: CallbackQuery) -> None:
    bc_id = int(call.data.split(":")[2])
    bc = await get_row("broadcasts", bc_id)
    if not bc or bc["status"] != "draft":
        await safe_answer(call, "Уже запущена или отменена.", show_alert=True)
        return
    await db_run("UPDATE broadcasts SET status = 'running' WHERE id = ?", (bc_id,))
    RUNNING[bc_id] = asyncio.Event()
    await safe_answer(call, "Запущено.")
    await show(call.bot, call.message.chat.id, f"🚀 Рассылка #{bc_id} запущена.",
               KB([B("⏹ Остановить", f"adm:bcstop:{bc_id}", style="danger")]), call.message.message_id)
    asyncio.create_task(run_broadcast(call.bot, call.message.chat.id, bc_id))


async def run_broadcast(bot: Bot, chat_id: int, bc_id: int) -> None:
    try:
        bc = await get_row("broadcasts", bc_id)
        markup = KB([B(bc["button_text"], url=bc["button_url"])]) if bc["button_text"] and bc["button_url"] else None
        sent, failed = await broadcast_send(bot, bc, markup, RUNNING[bc_id])
        stopped = RUNNING[bc_id].is_set()
        await db_run("UPDATE broadcasts SET status = ? WHERE id = ?", ("stopped" if stopped else "finished", bc_id))
        await bot.send_message(chat_id, f"{'⏹ Остановлена' if stopped else '✅ Завершена'} рассылка #{bc_id}\n"
                                        f"Отправлено: {sent} · ошибок: {failed}")
    except Exception:
        log.exception("Сбой рассылки #%s", bc_id)
        await db_run("UPDATE broadcasts SET status = 'stopped' WHERE id = ?", (bc_id,))
    finally:
        RUNNING.pop(bc_id, None)


@router.callback_query(F.data.startswith("adm:bcstop:"), AdminOnly())
async def adm_bc_stop(call: CallbackQuery) -> None:
    ev = RUNNING.get(int(call.data.split(":")[2]))
    if not ev:
        await safe_answer(call, "Рассылка уже не выполняется.", show_alert=True)
        return
    ev.set()
    await safe_answer(call, "Останавливаю…")


@router.callback_query(F.data.startswith("adm:bccancel:"), AdminOnly())
async def adm_bc_cancel(call: CallbackQuery) -> None:
    await db_run("UPDATE broadcasts SET status = 'stopped' WHERE id = ?", (int(call.data.split(":")[2]),))
    await safe_answer(call, "Отменено.")
    await show(call.bot, call.message.chat.id, "Рассылка отменена.", KB(BACK("adm:home")), call.message.message_id)


# =============================================================================
# 7. ВВОД АДМИНИСТРАТОРА И ЗАПУСК
# =============================================================================

@action("allmedia")
async def act_all_media(message: Message, state: FSMContext, data: dict) -> str | None:
    """Копит медиа в состоянии и при «-» ставит его на все экраны сразу."""
    if (message.text or "").strip() == "-":
        items = data.get("items", [])
        if not items:
            return "Медиа не добавлено."
        for key in TEXT_LABELS:
            await db_run("INSERT INTO text_media(key, media_ids) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET media_ids = excluded.media_ids", (key, join_media(items)))
        return f"✅ Медиа ({len(items)} шт.) установлено на все экраны: {len(TEXT_LABELS)}."
    items = await take_media(message, list(data.get("items", [])))
    data["items"] = items
    await state.set_data(data)
    await message.bot.send_message(message.chat.id, f"📎 Добавлено. Всего: {len(items)}. Отправьте ещё или «-».")
    return None


@router.callback_query(F.data == "adm:allmedia", AdminOnly())
async def adm_all_media(call: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(call)
    await ask(call, state, "allmedia",
              "🖼 <b>Медиа на все экраны сразу</b>\nОтправьте фото, видео или документ. Можно несколько. "
              "Чтобы применить ко всем экранам — «-».\n\n<i>Старое медиа экранов будет заменено.</i>",
              "adm:settings", items=[])


@router.callback_query(F.data == "adm:allclear", AdminOnly())
async def adm_all_clear(call: CallbackQuery) -> None:
    await db_run("UPDATE text_media SET media_ids = ''")
    await safe_answer(call, "Медиа убрано со всех экранов.")
    await adm_settings(call)


@router.message(AdminFSM.input, AdminOnly())
async def admin_input(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await _delete_quiet(message.bot, message.chat.id, message.message_id)
    handler = ACTIONS.get(data.get("action", ""))
    if handler is None:
        await state.clear()
        return
    back = data.get("back", "adm:home")
    try:
        result = await handler(message, state, data)
    except ValueError as exc:
        await show(message.bot, message.chat.id, f"⚠️ {exc}\n\nПопробуйте ещё раз.",
                   KB([B("❌ Отмена", back, style="danger")]), data["ui"])
        return
    except TelegramAPIError as exc:
        await show(message.bot, message.chat.id, f"⚠️ Ошибка Telegram: {esc(exc)}", KB(BACK(back)), data["ui"])
        await state.clear()
        return
    if result is None:
        return                                  # шаг мастера или рассылка — ждём следующее сообщение
    await state.clear()
    await show(message.bot, message.chat.id, result, KB(BACK(back)), data["ui"])


def setup_logging() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(config.LOG_FILE, encoding="utf-8")],
    )


async def on_startup(bot: Bot) -> None:
    await init_db()
    me = await bot.get_me()
    log.info("Запущен бот @%s", me.username)


if __name__ == "__main__":
    setup_logging()
    if not config.BOT_TOKEN or config.BOT_TOKEN.startswith("ВСТАВЬТЕ"):
        raise SystemExit("Вставьте токен бота в BOT_TOKEN в config.py.")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: добавьте свой Telegram ID в config.py.")
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    activity = ActivityMiddleware()
    dp.message.outer_middleware(activity)
    dp.callback_query.outer_middleware(activity)
    dp.include_router(router)
    dp.startup.register(on_startup)
    try:
        asyncio.run(dp.start_polling(bot, allowed_updates=["message", "callback_query"]))
    except (KeyboardInterrupt, SystemExit):
        log.info("Бот остановлен.")

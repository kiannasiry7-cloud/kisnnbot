import asyncio
import logging
import os
import secrets
import string
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ContentType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.deep_linking import create_start_link

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "bot.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("nashenas")

router = Router()

SUPPORTED_TYPES = {
    ContentType.TEXT,
    ContentType.PHOTO,
    ContentType.VIDEO,
    ContentType.VOICE,
    ContentType.AUDIO,
    ContentType.DOCUMENT,
    ContentType.ANIMATION,
    ContentType.STICKER,
    ContentType.VIDEO_NOTE,
}


class Compose(StatesGroup):
    waiting_message = State()


class ReplyAnon(StatesGroup):
    waiting_reply = State()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_slug(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 لینک من"), KeyboardButton(text="⚙️ تنظیمات")],
            [KeyboardButton(text="ℹ️ راهنما")],
        ],
        resize_keyboard=True,
    )


def action_keyboard(message_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="↩️ پاسخ ناشناس", callback_data=f"reply:{message_id}")],
            [
                InlineKeyboardButton(text="🚫 بلاک", callback_data=f"block:{message_id}"),
                InlineKeyboardButton(text="⚠️ گزارش", callback_data=f"report:{message_id}"),
            ],
        ]
    )


def settings_keyboard(paused: bool) -> InlineKeyboardMarkup:
    label = "✅ فعال کردن دریافت پیام" if paused else "⏸ توقف دریافت پیام"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=label, callback_data="toggle_pause")]]
    )


@asynccontextmanager
async def db():
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


async def init_db() -> None:
    async with db() as conn:
        await conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocks (
                blocker_id INTEGER NOT NULL,
                blocked_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (blocker_id, blocked_id)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                recipient_message_id INTEGER,
                content_type TEXT NOT NULL,
                content_summary TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, id DESC);
            """
        )
        await conn.commit()


async def ensure_user(user_id: int) -> aiosqlite.Row:
    async with db() as conn:
        row = await (await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))).fetchone()
        if row:
            return row
        for _ in range(8):
            slug = new_slug()
            try:
                await conn.execute(
                    "INSERT INTO users(user_id, slug, created_at) VALUES(?,?,?)",
                    (user_id, slug, now_iso()),
                )
                await conn.commit()
                return await (await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))).fetchone()
            except aiosqlite.IntegrityError:
                continue
        raise RuntimeError("Could not allocate unique public slug")


async def user_by_slug(slug: str) -> Optional[aiosqlite.Row]:
    async with db() as conn:
        return await (await conn.execute("SELECT * FROM users WHERE slug = ?", (slug,))).fetchone()


async def is_blocked(recipient_id: int, sender_id: int) -> bool:
    async with db() as conn:
        row = await (
            await conn.execute(
                "SELECT 1 FROM blocks WHERE blocker_id = ? AND blocked_id = ?",
                (recipient_id, sender_id),
            )
        ).fetchone()
        return bool(row)


def summarize(message: Message) -> str:
    if message.text:
        return message.text[:500]
    caption = (message.caption or "").strip()
    label = str(message.content_type)
    return f"[{label}] {caption}"[:500]


async def create_message_record(sender_id: int, recipient_id: int, message: Message) -> int:
    async with db() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO messages(sender_id, recipient_id, content_type, content_summary, created_at)
            VALUES(?,?,?,?,?)
            """,
            (sender_id, recipient_id, str(message.content_type), summarize(message), now_iso()),
        )
        await conn.commit()
        return int(cursor.lastrowid)


async def finish_message_record(message_id: int, recipient_message_id: int) -> None:
    async with db() as conn:
        await conn.execute(
            "UPDATE messages SET recipient_message_id = ? WHERE id = ?",
            (recipient_message_id, message_id),
        )
        await conn.commit()


async def get_message_record(message_id: int) -> Optional[aiosqlite.Row]:
    async with db() as conn:
        return await (await conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,))).fetchone()


async def deliver_anonymous(bot: Bot, source: Message, sender_id: int, recipient_id: int) -> int:
    recipient = await ensure_user(recipient_id)
    if int(recipient["paused"]):
        raise ValueError("paused")
    if await is_blocked(recipient_id, sender_id):
        raise ValueError("blocked")

    record_id = await create_message_record(sender_id, recipient_id, source)
    header = "📩 پیام ناشناس جدید"
    if source.content_type == ContentType.TEXT:
        sent = await bot.send_message(
            recipient_id,
            f"{header}\n\n{source.text}",
            reply_markup=action_keyboard(record_id),
        )
    else:
        sent = await bot.copy_message(
            chat_id=recipient_id,
            from_chat_id=source.chat.id,
            message_id=source.message_id,
            reply_markup=action_keyboard(record_id),
        )
        await bot.send_message(recipient_id, header)
    await finish_message_record(record_id, sent.message_id)
    return record_id


async def show_home(message: Message) -> None:
    await message.answer(
        "👻 <b>ربات پیام ناشناس</b>\n\n"
        "لینک اختصاصی‌ات را برای دیگران بفرست. هر کسی از طریق لینک وارد شود می‌تواند بدون نمایش هویتش برایت پیام بفرستد.",
        reply_markup=main_keyboard(),
        parse_mode=ParseMode.HTML,
    )


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    await ensure_user(message.from_user.id)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        target = await user_by_slug(parts[1].strip())
        if not target:
            await message.answer("این لینک معتبر نیست یا منقضی شده است.", reply_markup=main_keyboard())
            return
        if int(target["user_id"]) == message.from_user.id:
            await message.answer("این لینک شخصی خودت است 🙂", reply_markup=main_keyboard())
            return
        if int(target["paused"]):
            await message.answer("این کاربر فعلاً دریافت پیام ناشناس را متوقف کرده است.")
            return
        if await is_blocked(int(target["user_id"]), message.from_user.id):
            await message.answer("امکان ارسال پیام به این کاربر وجود ندارد.")
            return
        await state.set_state(Compose.waiting_message)
        await state.update_data(target_id=int(target["user_id"]))
        await message.answer(
            "✍️ پیام ناشناست را بفرست. متن، عکس، ویدیو، ویس، فایل و استیکر پشتیبانی می‌شود.\n\n/cancel برای لغو"
        )
        return

    await show_home(message)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("لغو شد.", reply_markup=main_keyboard())


@router.message(F.text == "🔗 لینک من")
async def my_link(message: Message, bot: Bot) -> None:
    user = await ensure_user(message.from_user.id)
    link = await create_start_link(bot, str(user["slug"]), encode=False)
    await message.answer(
        "🔗 <b>لینک اختصاصی تو:</b>\n\n"
        f"<code>{link}</code>\n\n"
        "این لینک را هر جا خواستی بفرست؛ فرستنده برای گیرنده ناشناس می‌ماند.",
        parse_mode=ParseMode.HTML,
    )


@router.message(F.text == "⚙️ تنظیمات")
async def settings(message: Message) -> None:
    user = await ensure_user(message.from_user.id)
    paused = bool(user["paused"])
    status = "متوقف" if paused else "فعال"
    await message.answer(
        f"⚙️ دریافت پیام ناشناس الان <b>{status}</b> است.",
        reply_markup=settings_keyboard(paused),
        parse_mode=ParseMode.HTML,
    )


@router.callback_query(F.data == "toggle_pause")
async def toggle_pause(callback: CallbackQuery) -> None:
    user = await ensure_user(callback.from_user.id)
    new_value = 0 if int(user["paused"]) else 1
    async with db() as conn:
        await conn.execute("UPDATE users SET paused = ? WHERE user_id = ?", (new_value, callback.from_user.id))
        await conn.commit()
    await callback.message.edit_text(
        "⚙️ دریافت پیام ناشناس الان <b>{}</b> است.".format("متوقف" if new_value else "فعال"),
        reply_markup=settings_keyboard(bool(new_value)),
        parse_mode=ParseMode.HTML,
    )
    await callback.answer("ذخیره شد")


@router.message(F.text == "ℹ️ راهنما")
async def help_message(message: Message) -> None:
    await message.answer(
        "ℹ️ <b>راهنما</b>\n\n"
        "• «لینک من» را بزن و لینک را منتشر کن.\n"
        "• دیگران از طریق لینک برایت پیام ناشناس می‌فرستند.\n"
        "• زیر هر پیام می‌توانی ناشناس پاسخ بدهی، فرستنده را بلاک کنی یا پیام را گزارش کنی.\n"
        "• از تنظیمات می‌توانی دریافت پیام را موقتاً متوقف کنی.",
        parse_mode=ParseMode.HTML,
    )


@router.message(Compose.waiting_message)
async def compose_message(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.content_type not in SUPPORTED_TYPES:
        await message.answer("این نوع پیام پشتیبانی نمی‌شود. متن، عکس، ویدیو، ویس یا فایل بفرست.")
        return
    data = await state.get_data()
    target_id = int(data["target_id"])
    try:
        await deliver_anonymous(bot, message, message.from_user.id, target_id)
    except ValueError as exc:
        if str(exc) == "paused":
            await message.answer("این کاربر فعلاً دریافت پیام را متوقف کرده است.")
        else:
            await message.answer("امکان ارسال پیام به این کاربر وجود ندارد.")
        await state.clear()
        return
    except Exception:
        logger.exception("delivery failed")
        await message.answer("ارسال انجام نشد. دوباره امتحان کن.")
        return
    await state.clear()
    await message.answer("✅ پیام ناشناس ارسال شد.", reply_markup=main_keyboard())


@router.callback_query(F.data.startswith("reply:"))
async def reply_button(callback: CallbackQuery, state: FSMContext) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    record = await get_message_record(message_id)
    if not record or int(record["recipient_id"]) != callback.from_user.id:
        await callback.answer("این پیام برای شما نیست.", show_alert=True)
        return
    await state.set_state(ReplyAnon.waiting_reply)
    await state.update_data(reply_to=message_id)
    await callback.message.answer("↩️ پاسخ ناشناست را بفرست.\n/cancel برای لغو")
    await callback.answer()


@router.message(ReplyAnon.waiting_reply)
async def reply_content(message: Message, state: FSMContext, bot: Bot) -> None:
    if message.content_type not in SUPPORTED_TYPES:
        await message.answer("این نوع پیام پشتیبانی نمی‌شود.")
        return
    data = await state.get_data()
    original = await get_message_record(int(data["reply_to"]))
    if not original or int(original["recipient_id"]) != message.from_user.id:
        await state.clear()
        await message.answer("پیام اصلی پیدا نشد.")
        return
    target_id = int(original["sender_id"])
    try:
        await deliver_anonymous(bot, message, message.from_user.id, target_id)
    except ValueError:
        await message.answer("امکان ارسال پاسخ وجود ندارد.")
        await state.clear()
        return
    except Exception:
        logger.exception("anonymous reply failed")
        await message.answer("ارسال پاسخ انجام نشد. دوباره امتحان کن.")
        return
    await state.clear()
    await message.answer("✅ پاسخ ناشناس ارسال شد.", reply_markup=main_keyboard())


@router.callback_query(F.data.startswith("block:"))
async def block_sender(callback: CallbackQuery) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    record = await get_message_record(message_id)
    if not record or int(record["recipient_id"]) != callback.from_user.id:
        await callback.answer("این عملیات مجاز نیست.", show_alert=True)
        return
    async with db() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO blocks(blocker_id, blocked_id, created_at) VALUES(?,?,?)",
            (callback.from_user.id, int(record["sender_id"]), now_iso()),
        )
        await conn.commit()
    await callback.answer("فرستنده بلاک شد.", show_alert=True)


@router.callback_query(F.data.startswith("report:"))
async def report_message(callback: CallbackQuery, bot: Bot) -> None:
    message_id = int(callback.data.split(":", 1)[1])
    record = await get_message_record(message_id)
    if not record or int(record["recipient_id"]) != callback.from_user.id:
        await callback.answer("این عملیات مجاز نیست.", show_alert=True)
        return
    async with db() as conn:
        await conn.execute(
            "INSERT INTO reports(message_id, reporter_id, created_at) VALUES(?,?,?)",
            (message_id, callback.from_user.id, now_iso()),
        )
        await conn.commit()
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                "⚠️ <b>گزارش جدید</b>\n"
                f"Message ID: <code>{message_id}</code>\n"
                f"Sender ID: <code>{record['sender_id']}</code>\n"
                f"Recipient ID: <code>{record['recipient_id']}</code>\n"
                f"Content: <code>{(record['content_summary'] or '')[:350]}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("could not notify admin")
    await callback.answer("گزارش ثبت شد.", show_alert=True)


@router.message(Command("admin"))
async def admin_panel(message: Message) -> None:
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    async with db() as conn:
        users = (await (await conn.execute("SELECT COUNT(*) c FROM users")).fetchone())["c"]
        messages_count = (await (await conn.execute("SELECT COUNT(*) c FROM messages")).fetchone())["c"]
        reports = (await (await conn.execute("SELECT COUNT(*) c FROM reports")).fetchone())["c"]
    await message.answer(
        "🛠 <b>پنل ادمین</b>\n\n"
        f"کاربران: <b>{users}</b>\n"
        f"پیام‌ها: <b>{messages_count}</b>\n"
        f"گزارش‌ها: <b>{reports}</b>\n\n"
        "/admin_last — 10 پیام آخر",
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("admin_last"))
async def admin_last(message: Message) -> None:
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return
    async with db() as conn:
        rows = await (
            await conn.execute(
                "SELECT id, sender_id, recipient_id, content_type, content_summary, created_at "
                "FROM messages ORDER BY id DESC LIMIT 10"
            )
        ).fetchall()
    if not rows:
        await message.answer("هنوز پیامی ثبت نشده است.")
        return
    lines = ["🧾 <b>۱۰ پیام آخر</b>"]
    for row in rows:
        summary = (row["content_summary"] or "").replace("<", "&lt;").replace(">", "&gt;")[:140]
        lines.append(
            f"\n#{row['id']} | <code>{row['sender_id']}</code> → <code>{row['recipient_id']}</code>\n{summary}"
        )
    await message.answer("".join(lines), parse_mode=ParseMode.HTML)


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("از منوی پایین یکی از گزینه‌ها را انتخاب کن.", reply_markup=main_keyboard())


async def main() -> None:
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    me = await bot.get_me()
    logger.info("Starting @%s", me.username)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

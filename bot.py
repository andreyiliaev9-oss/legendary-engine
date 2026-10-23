import os
import sqlite3
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("8836985962:AAEEjRSTMV0BGVkcQIi1J2Afri1Vz8dfckc", "PASTE_YOUR_BOT_TOKEN_HERE")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "8141670312").split(",") if x.strip()]
TZ = ZoneInfo(os.getenv("TZ", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", "bot.db")

# ============================================================
# ВНИМАНИЕ
# Telegram не поддерживает реально красить фон кнопок.
# Есть только визуальный эффект: пометка текста эмодзи-псевдоцвета,
# например: "⚪ Белая" / "🔴 Красная".
# Это лучший рабочий способ в Bot API.
# ============================================================

# ============================================================
# DB
# ============================================================
def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = db_connect()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        first_name TEXT,
        username TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_contact_at TEXT,
        notes TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        closed_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ticket_id INTEGER,
        direction TEXT NOT NULL CHECK(direction IN ('incoming', 'outgoing')),
        message_type TEXT NOT NULL DEFAULT 'text',
        text TEXT,
        file_id TEXT,
        sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        status TEXT NOT NULL DEFAULT 'sent',
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (ticket_id) REFERENCES tickets(id)
    );

    CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        ticket_id INTEGER,
        template_id INTEGER,
        text TEXT NOT NULL,
        due_at TEXT NOT NULL,
        recurrence TEXT DEFAULT 'once',
        status TEXT NOT NULL DEFAULT 'scheduled',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        last_sent_at TEXT,
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (ticket_id) REFERENCES tickets(id)
    );

    CREATE TABLE IF NOT EXISTS templates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        day_of_week TEXT,
        start_time TEXT,
        end_time TEXT,
        is_holiday INTEGER DEFAULT 0,
        timezone TEXT DEFAULT 'Europe/Moscow'
    );

    CREATE TABLE IF NOT EXISTS admin_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_id INTEGER,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        payload TEXT
    );
    """)
    conn.commit()
    conn.close()

    # example schedule
    conn = db_connect()
    if conn.execute("SELECT COUNT(*) FROM schedule").fetchone()[0] == 0:
        rows = [
            ("Monday", "09:00", "18:00", 0),
            ("Tuesday", "09:00", "18:00", 0),
            ("Wednesday", "09:00", "18:00", 0),
            ("Thursday", "09:00", "18:00", 0),
            ("Friday", "09:00", "18:00", 0),
            ("Saturday", "00:00", "00:00", 1),
            ("Sunday", "00:00", "00:00", 1),
        ]
        conn.executemany("INSERT INTO schedule (day_of_week, start_time, end_time, is_holiday) VALUES (?, ?, ?, ?)", rows)
        conn.commit()
    conn.close()

    # example templates
    conn = db_connect()
    if conn.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
        templates = [
            ("Welcome", "greeting", "Здравствуйте, {имя}! Добро пожаловать в сервис «Призыва Нет»."),
            ("Info", "notification", "Внимание! Важно соблюдать график работы и своевременно реагировать на уведомления."),
            ("Payment reminder", "reminder", "Напоминаем вам о сумме {сумма} к оплате. Предполагаемая дата: {дата}."),
            ("Ticket reply", "reply", "Ваше обращение принято. Оператор рассмотрит его в ближайшее время."),
            ("Close ticket", "completion", "Ваше обращение закрыто. Если возникнут вопросы, напишите нам снова."),
        ]
        conn.executemany("INSERT INTO templates (name, category, text) VALUES (?, ?, ?)", templates)
        conn.commit()
    conn.close()

def ensure_user(telegram_id: int, first_name: str | None = None, username: str | None = None):
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    now = datetime.utcnow().isoformat()
    if row is None:
        cur = conn.execute(
            "INSERT INTO users (telegram_id, first_name, username, created_at, last_contact_at) VALUES (?, ?, ?, ?, ?)",
            (telegram_id, first_name, username, now, now)
        )
        conn.commit()
        user_id = cur.lastrowid
    else:
        conn.execute(
            """
            UPDATE users
            SET first_name = COALESCE(?, first_name),
                username = COALESCE(?, username),
                last_contact_at = ?
            WHERE telegram_id = ?
            """,
            (first_name, username, now, telegram_id)
        )
        conn.commit()
        user_id = row["id"]
    conn.close()
    return user_id

def get_user_by_telegram_id(telegram_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return row

def get_user_by_id(user_id: int):
    conn = db_connect()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return row

def create_ticket(user_id: int, status: str = "new"):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO tickets (user_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (user_id, status, datetime.utcnow().isoformat(), datetime.utcnow().isoformat())
    )
    conn.commit()
    ticket_id = cur.lastrowid
    conn.close()
    return ticket_id

def get_open_ticket_for_user(user_id: int):
    conn = db_connect()
    row = conn.execute(
        """
        SELECT * FROM tickets
        WHERE user_id = ? AND status NOT IN ('closed')
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (user_id,)
    ).fetchone()
    conn.close()
    return row

def save_message(user_id: int, direction: str, text: str = "", ticket_id: int | None = None, message_type: str = "text", file_id: str | None = None):
    conn = db_connect()
    conn.execute(
        """
        INSERT INTO messages (user_id, ticket_id, direction, message_type, text, file_id, sent_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (user_id, ticket_id, direction, message_type, text, file_id, datetime.utcnow().isoformat(), "sent")
    )
    conn.commit()
    conn.close()

def list_users(limit: int = 20):
    conn = db_connect()
    rows = conn.execute("SELECT * FROM users ORDER BY last_contact_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows

def list_open_tickets():
    conn = db_connect()
    rows = conn.execute(
        """
        SELECT t.*, u.telegram_id, u.username
        FROM tickets t
        JOIN users u ON u.id = t.user_id
        WHERE t.status NOT IN ('closed')
        ORDER BY t.created_at DESC
        """
    ).fetchall()
    conn.close()
    return rows

def update_ticket_status(ticket_id: int, status: str):
    conn = db_connect()
    conn.execute(
        "UPDATE tickets SET status = ?, updated_at = ?, closed_at = ? WHERE id = ?",
        (status, datetime.utcnow().isoformat(), (datetime.utcnow().isoformat() if status == "closed" else None), ticket_id)
    )
    conn.commit()
    conn.close()

def get_ticket_history(user_id: int):
    conn = db_connect()
    rows = conn.execute(
        """
        SELECT m.*, t.status
        FROM messages m
        LEFT JOIN tickets t ON t.id = m.ticket_id
        WHERE m.user_id = ?
        ORDER BY m.sent_at ASC
        """,
        (user_id,)
    ).fetchall()
    conn.close()
    return rows

def create_reminder(user_id: int, text: str, due_at: str, recurrence: str = "once", ticket_id: int | None = None):
    conn = db_connect()
    cur = conn.execute(
        """
        INSERT INTO reminders (user_id, ticket_id, text, due_at, recurrence, status, active, created_at)
        VALUES (?, ?, ?, ?, ?, 'scheduled', 1, ?)
        """,
        (user_id, ticket_id, text, due_at, recurrence, datetime.utcnow().isoformat())
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid

def get_due_reminders():
    conn = db_connect()
    rows = conn.execute(
        """
        SELECT * FROM reminders
        WHERE active = 1 AND status IN ('scheduled', 'queued')
        AND due_at <= ?
        ORDER BY due_at ASC
        """,
        (datetime.utcnow().isoformat(),)
    ).fetchall()
    conn.close()
    return rows

def mark_reminder_sent(reminder_id: int):
    conn = db_connect()
    conn.execute(
        "UPDATE reminders SET status = 'sent', last_sent_at = ?, active = 0 WHERE id = ?",
        (datetime.utcnow().isoformat(), reminder_id)
    )
    conn.commit()
    conn.close()

def add_template(name: str, category: str, text: str):
    conn = db_connect()
    cur = conn.execute(
        "INSERT INTO templates (name, category, text, created_at) VALUES (?, ?, ?, ?)",
        (name, category, text, datetime.utcnow().isoformat())
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid

def get_templates():
    conn = db_connect()
    rows = conn.execute("SELECT * FROM templates ORDER BY category, id DESC").fetchall()
    conn.close()
    return rows

def ensure_admin(telegram_id: int):
    conn = db_connect()
    row = conn.execute("SELECT id FROM admin_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO admin_users (telegram_id, role, is_active, created_at) VALUES (?, 'admin', 1, ?)",
            (telegram_id, datetime.utcnow().isoformat())
        )
        conn.commit()
    conn.close()

def is_admin(telegram_id: int):
    conn = db_connect()
    row = conn.execute("SELECT id FROM admin_users WHERE telegram_id = ? AND is_active = 1", (telegram_id,)).fetchone()
    conn.close()
    return row is not None

def audit(admin_id: int | None, action: str, entity_type: str | None = None, entity_id: int | None = None, payload: str | None = None):
    conn = db_connect()
    conn.execute(
        "INSERT INTO audit_log (admin_id, action, entity_type, entity_id, created_at, payload) VALUES (?, ?, ?, ?, ?, ?)",
        (admin_id, action, entity_type, entity_id, datetime.utcnow().isoformat(), payload)
    )
    conn.commit()
    conn.close()

# ============================================================
# FSM
# ============================================================
class ContactState(StatesGroup):
    waiting_for_message = State()

class AdminReplyState(StatesGroup):
    waiting_for_reply_text = State()

# ============================================================
# KEYBOARDS
# ============================================================
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚪ Связаться с оператором", callback_data="contact_operator")],
        [InlineKeyboardButton(text="⚪ График работы", callback_data="schedule")],
        [InlineKeyboardButton(text="⚪ Уведомления", callback_data="notifications")],
        [InlineKeyboardButton(text="⚪ Информация", callback_data="info")],
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Открытые обращения", callback_data="admin_open_tickets")],
        [InlineKeyboardButton(text="⚪ Пользователи", callback_data="admin_users")],
        [InlineKeyboardButton(text="⚪ Напоминания", callback_data="admin_reminders")],
        [InlineKeyboardButton(text="⚪ Шаблоны", callback_data="admin_templates")],
        [InlineKeyboardButton(text="⚪ История", callback_data="admin_history")],
    ])

def admin_ticket_actions_kb(ticket_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔵 Ответить", callback_data=f"reply_ticket:{ticket_id}")],
        [InlineKeyboardButton(text="🔴 Закрыть", callback_data=f"close_ticket:{ticket_id}")],
        [InlineKeyboardButton(text="⚪ История", callback_data=f"ticket_history:{ticket_id}")],
    ])

# ============================================================
# BOT LOGIC
# ============================================================
router = Router()

async def send_main_menu(bot: Bot, chat_id: int):
    await bot.send_message(
        chat_id,
        "Здравствуйте!\n"
        "Вы обратились в компанию «Призыва Нет».\n"
        "В данном чате вы можете получать уведомления, направлять обращения оператору "
        "и знакомиться с информацией о режиме работы.\n"
        "Выберите необходимый раздел в меню ниже.",
        reply_markup=main_menu_kb()
    )

async def notify_admins(bot: Bot, user_id: int, message_text: str, user_name: str):
    for admin_id in ADMIN_IDS:
        await bot.send_message(
            admin_id,
            "Новое обращение\n"
            f"Пользователь: ID {user_id}\n"
            f"Имя: {user_name}\n"
            f"Время: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"Сообщение:\n{message_text[:2000]}"
        )

@router.message(CommandStart())
async def cmd_start(message: Message):
    ensure_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    await send_main_menu(message.bot, message.chat.id)

@router.callback_query(F.data == "contact_operator")
async def contact_operator(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ContactState.waiting_for_message)
    await cb.message.answer(
        "Вы можете направить сообщение оператору компании «Призыва Нет».\n"
        "Пожалуйста, изложите суть вашего вопроса одним сообщением. "
        "Ваше обращение будет зарегистрировано и передано на рассмотрение.\n"
        "Ответ поступит в этот чат."
    )

@router.callback_query(F.data == "schedule")
async def schedule_info(cb: CallbackQuery):
    await cb.message.answer(
        "График работы\n"
        "Понедельник — пятница: 09:00–18:00\n"
        "Суббота и воскресенье: выходные дни\n"
        "Часы, выходные, праздничные даты и часовой пояс настраиваются в административной панели."
    )

@router.callback_query(F.data == "notifications")
async def notifications_info(cb: CallbackQuery):
    await cb.message.answer(
        "Уведомления и напоминания формируются автоматически по расписанию и событиям системы.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚪ Создать напоминание", callback_data="create_reminder_ui")],
            [InlineKeyboardButton(text="⚪ Список напоминаний", callback_data="list_reminders_ui")],
        ])
    )

@router.callback_query(F.data == "info")
async def info_screen(cb: CallbackQuery):
    await cb.message.answer(
        "Информация\n"
        "Бот используется для связи с компанией «Призыва Нет».\n"
        "Вы можете задавать вопросы, получать уведомления и узнавать статус обращений.\n"
        "Все исходящие сообщения отправляются от имени бота."
    )

@router.callback_query(F.data == "create_reminder_ui")
async def reminder_ui(cb: CallbackQuery):
    await cb.message.answer(
        "Создание напоминания: используйте команду /create_reminder <telegram_id> <YYYY-MM-DDTHH:MM> <текст>."
    )

@router.callback_query(F.data == "list_reminders_ui")
async def reminder_list_ui(cb: CallbackQuery):
    conn = db_connect()
    rows = conn.execute("SELECT * FROM reminders ORDER BY due_at DESC LIMIT 10").fetchall()
    conn.close()
    if not rows:
        await cb.message.answer("Напоминания отсутствуют.")
        return
    lines = [f"id={r['id']} | due={r['due_at']} | status={r['status']} | text={r['text'][:80]}" for r in rows]
    await cb.message.answer("\n".join(lines))

@router.message(state=ContactState.waiting_for_message, (F.text | F.photo | F.document))
async def handle_contact_message(message: Message, state: FSMContext):
    user = get_user_by_telegram_id(message.from_user.id)
    if user is None:
        user_id = ensure_user(message.from_user.id, message.from_user.first_name, message.from_user.username)
    else:
        user_id = user["id"]

    ticket = get_open_ticket_for_user(user_id)
    if ticket is None:
        ticket_id = create_ticket(user_id, status="new")
    else:
        ticket_id = ticket["id"]

    text = message.text or message.caption or ""
    if not text and (message.photo or message.document):
        text = "Файл/изображение"

    save_message(user_id, "incoming", text, ticket_id, "text" if not message.photo and not message.document else "file")

    await notify_admins(
        message.bot,
        user_id=user_id,
        user_name=message.from_user.username or message.from_user.first_name or "unknown",
        message_text=text
    )

    await message.answer(
        "Ваше сообщение принято и передано оператору компании «Призыва Нет».\n"
        "Специалист рассмотрит обращение в соответствии с графиком работы.\n"
        "Ответ будет направлен в этот чат."
    )
    await state.clear()

@router.message(F.text)
async def default_text(message: Message):
    if message.text.startswith("/"):
        return
    await message.answer(
        "Ваше сообщение принято. Если вы хотите написать оператору — откройте главное меню и выберите «Связаться с оператором»."
    )

# ============================================================
# ADMIN
# ============================================================
def admin_only(func):
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id not in ADMIN_IDS:
            await message.answer("Доступ запрещён.")
            return
        return await func(message, *args, **kwargs)
    return wrapper

@router.message(Command("admin"))
@admin_only
async def admin_panel(message: Message):
    ensure_admin(message.from_user.id)
    await message.answer("Административная панель", reply_markup=admin_menu_kb())

@router.callback_query(F.data == "admin_open_tickets")
@admin_only
async def admin_open_tickets(cb: CallbackQuery):
    rows = list_open_tickets()
    if not rows:
        await cb.message.answer("Нет активных обращений")
        return
    for r in rows:
        await cb.message.answer(
            f"💬 ticket_id={r['id']} | user_id={r['user_id']} | status={r['status']} | telegram_id={r['telegram_id']}",
            reply_markup=admin_ticket_actions_kb(r["id"])
        )

@router.callback_query(F.data.startswith("reply_ticket:"))
@admin_only
async def admin_reply_ticket(cb: CallbackQuery, state: FSMContext):
    ticket_id = int(cb.data.split(":")[1])
    conn = db_connect()
    row = conn.execute("SELECT user_id FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    if not row:
        await cb.message.answer("Тикет не найден")
        return
    user = get_user_by_id(row["user_id"])
    if not user:
        await cb.message.answer("Пользователь не найден")
        return
    await state.update_data(reply_ticket_id=ticket_id, reply_user_id=user["id"], reply_telegram_id=user["telegram_id"])
    await state.set_state(AdminReplyState.waiting_for_reply_text)
    await cb.message.answer(f"Напишите текст ответа пользователю Telegram ID {user['telegram_id']}. Для отмены: /cancel")

@router.callback_query(F.data.startswith("close_ticket:"))
@admin_only
async def admin_close_ticket(cb: CallbackQuery):
    ticket_id = int(cb.data.split(":")[1])
    update_ticket_status(ticket_id, "closed")
    await cb.message.answer(f"Тикет {ticket_id} закрыт.")
    audit(cb.from_user.id, "ticket_closed", "ticket", ticket_id)

@router.callback_query(F.data.startswith("ticket_history:"))
@admin_only
async def admin_ticket_history(cb: CallbackQuery):
    ticket_id = int(cb.data.split(":")[1])
    conn = db_connect()
    ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not ticket:
        await cb.message.answer("Тикет не найден.")
        return
    messages = conn.execute("SELECT * FROM messages WHERE ticket_id = ? ORDER BY sent_at ASC", (ticket_id,)).fetchall()
    conn.close()
    if not messages:
        await cb.message.answer("История отсутствует.")
        return
    lines = [f"{m['direction']} | {m['sent_at']} | {m['text'][:200]}" for m in messages]
    await cb.message.answer("\n".join(lines))

@router.message(Command("users"))
@admin_only
async def admin_users(message: Message):
    rows = list_users(20)
    if not rows:
        await message.answer("Пользователи не найдены.")
        return
    lines = []
    for r in rows:
        lines.append(f"ID: {r['id']} | TG: {r['telegram_id']} | @{r['username'] or '—'} | {r['first_name'] or '—'} | last: {r['last_contact_at'] or '—'}")
    await message.answer("\n".join(lines))

@router.message(Command("tickets"))
@admin_only
async def admin_tickets(message: Message):
    rows = list_open_tickets()
    if not rows:
        await message.answer("Нет активных обращений.")
        return
    out = []
    for r in rows:
        out.append(f"ticket_id={r['id']} | user_id={r['user_id']} | status={r['status']} | telegram_id={r['telegram_id']}")
    await message.answer("\n".join(out))

@router.message(Command("history"))
@admin_only
async def admin_history(message: Message):
    args = message.get_args().strip()
    if not args:
        await message.answer("Использование: /history <telegram_id>")
        return
    try:
        target = int(args)
    except:
        await message.answer("Telegram ID должен быть числом.")
        return
    user = get_user_by_telegram_id(target)
    if not user:
        await message.answer("Пользователь не найден.")
        return
    history = get_ticket_history(user["id"])
    if not history:
        await message.answer("История сообщений отсутствует.")
        return
    lines = [f"{h['direction']} | {h['sent_at']} | {h['text'][:200]}" for h in history]
    await message.answer("\n".join(lines))

@router.message(Command("reply"))
@admin_only
async def admin_reply_start(message: Message, state: FSMContext):
    args = message.get_args().strip()
    if not args:
        await message.answer("Использование: /reply <telegram_id>")
        return
    try:
        target_tg = int(args)
    except:
        await message.answer("Telegram ID должен быть числом.")
        return
    user = get_user_by_telegram_id(target_tg)
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    await state.update_data(reply_target_tg=target_tg, reply_user_id=user["id"])
    await state.set_state(AdminReplyState.waiting_for_reply_text)
    await message.answer(f"Напишите текст ответа пользователю Telegram ID {target_tg}. Для отмены: /cancel")

@router.message(state=AdminReplyState.waiting_for_reply_text, F.text)
async def admin_reply_send(message: Message, state: FSMContext):
    data = await state.get_data()
    target_tg = data.get("reply_target_tg")
    if target_tg is not None:
        user = get_user_by_telegram_id(target_tg)
    else:
        user = None

    if not user:
        await message.answer("Получатель не найден.")
        await state.clear()
        return

    text = message.text
    await message.bot.send_message(target_tg, text)
    save_message(user["id"], "outgoing", text, None, "text")
    audit(message.from_user.id, "reply_sent", "user", user["id"], json.dumps({"to_telegram_id": target_tg, "text": text[:200]}))
    await message.answer("Ответ отправлен пользователю через бота.")
    await state.clear()

@router.message(Command("create_reminder"))
@admin_only
async def create_reminder_cmd(message: Message):
    args = message.get_args().strip()
    if not args:
        await message.answer("Использование: /create_reminder <telegram_id> <YYYY-MM-DDTHH:MM> <текст>")
        return
    try:
        parts = args.split(" ", 2)
        tg_id = int(parts[0])
        due = parts[1]
        text = parts[2] if len(parts) > 2 else "Напоминание"
    except Exception:
        await message.answer("Неверный формат. Пример: /create_reminder 123456789 2026-10-25T15:00 Напомним о платеже")
        return

    user = get_user_by_telegram_id(tg_id)
    if user is None:
        await message.answer("Пользователь не найден.")
        return

    rid = create_reminder(user["id"], text, due, "once")
    await message.answer(f"Напоминание создано: id={rid}")
    audit(message.from_user.id, "reminder_created", "reminder", rid, json.dumps({"to_telegram_id": tg_id, "due_at": due, "text": text}))

@router.message(Command("templates"))
@admin_only
async def templates_list(message: Message):
    rows = get_templates()
    if not rows:
        await message.answer("Шаблоны отсутствуют.")
        return
    out = [f"{r['id']} | {r['category']} | {r['name']} | {r['text'][:80]}" for r in rows]
    await message.answer("\n".join(out))

@router.message(Command("add_template"))
@admin_only
async def add_template_cmd(message: Message):
    args = message.get_args().strip()
    if not args:
        await message.answer("Использование: /add_template <name> <category> <text>")
        return
    parts = args.split(" ", 2)
    if len(parts) < 3:
        await message.answer("Ошибка: нужно 3 параметра: name, category, text")
        return
    name, category, text = parts[0], parts[1], parts[2]
    tid = add_template(name, category, text)
    await message.answer(f"Шаблон добавлен: id={tid}")
    audit(message.from_user.id, "template_created", "template", tid, json.dumps({"name": name, "category": category}))

@router.message(Command("cancel"))
async def cancel_state(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Операция отменена.")

# ============================================================
# SCHEDULER
# ============================================================
async def check_due_reminders(bot: Bot):
    reminders = get_due_reminders()
    for row in reminders:
        user = get_user_by_id(row["user_id"])
        if user is None:
            continue
        await bot.send_message(user["telegram_id"], row["text"])
        save_message(user["id"], "outgoing", row["text"], row["ticket_id"], "text")
        mark_reminder_sent(row["id"])

def start_scheduler(bot: Bot):
    scheduler = AsyncIOScheduler(timezone=str(TZ))
    scheduler.add_job(check_due_reminders, "interval", seconds=30, args=[bot], id="reminders_check")
    scheduler.start()
    return scheduler

# ============================================================
# MAIN
# ============================================================
async def main():
    init_db()

    for admin_id in ADMIN_IDS:
        ensure_admin(admin_id)

    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = start_scheduler(bot)
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown()
        await bot.session.close()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

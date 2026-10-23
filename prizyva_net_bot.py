"""
Telegram-бот «Призыва Нет» — единый файл.

Установка:   pip install aiogram==3.* aiosqlite
Запуск:      TELEGRAM_BOT_TOKEN=xxx python prizyva_net_bot.py
(токен хранится в переменной окружения, не в коде)

Команды администратора (только для ADMIN_IDS):
  /admin                         — панель
  /users                         — список пользователей
  /user <tg_id>                  — карточка пользователя
  /send <tg_id> <текст>          — сообщение от имени бота
  /history <tg_id>               — история переписки
  /note <tg_id> <текст>          — внутренняя заметка
  /tickets                       — открытые обращения
  /status <ticket_id> <new|work|wait_user|check|closed>
  /remind <tg_id> <ГГГГ-ММ-ДД ЧЧ:ММ> <повтор_дней|0> <макс_раз> <текст>
  /reminders                     — список напоминаний
  /cancel_reminder <id>
  /templates  /addtemplate <код> <текст>  /tpl <код> <tg_id>
  /setschedule <день 0-6> <ЧЧ:ММ-ЧЧ:ММ|off>   (0 = понедельник)
  /holiday <ГГГГ-ММ-ДД> <ЧЧ:ММ-ЧЧ:ММ|off>
  /setinfo <текст>               — текст раздела «Информация»
  /log                           — журнал действий
Ответ на уведомление: кнопка «Ответить» или Reply на сообщение-уведомление.
"""
import asyncio, os, logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as B

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_IDS = {8141670312}
TZ = ZoneInfo(os.environ.get("BOT_TZ", "Europe/Moscow"))
DB = os.environ.get("BOT_DB", "prizyva_net.db")
COMPANY = "«Призыва Нет»"
PAYMENT_WORDS = ("оплатил", "оплатила", "оплачено", "перевел", "перевёл", "внес платеж", "внёс платёж")
DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
STATUSES = {"new": "Новое", "work": "В работе", "wait_user": "Ожидает ответа пользователя",
            "check": "Ожидает проверки", "closed": "Закрыто"}

logging.basicConfig(level=logging.INFO)
router = Router()


class Reply(StatesGroup):
    text = State()


# ---------------- База данных ----------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE, name TEXT,
  username TEXT, first_seen TEXT, last_contact TEXT, notes TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, direction TEXT,
  text TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, status TEXT,
  created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS reminders(id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, text TEXT,
  next_at TEXT, repeat_days INTEGER, max_sends INTEGER, sent_count INTEGER DEFAULT 0,
  status TEXT DEFAULT 'scheduled', last_error TEXT);
CREATE TABLE IF NOT EXISTS templates(code TEXT PRIMARY KEY, text TEXT);
CREATE TABLE IF NOT EXISTS schedule(day INTEGER PRIMARY KEY, hours TEXT);
CREATE TABLE IF NOT EXISTS holidays(date TEXT PRIMARY KEY, hours TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admin_users(tg_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, action TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS notify_map(admin_msg_id INTEGER PRIMARY KEY, tg_id INTEGER);
"""
DEFAULT_INFO = (f"Бот компании {COMPANY} предназначен для направления уведомлений и приёма обращений.\n\n"
                "• Чтобы связаться с оператором, нажмите «Связаться с оператором» и опишите вопрос.\n"
                "• Обращения рассматриваются в порядке поступления в рабочее время.\n"
                "• Сообщения в этом чате отправляются от имени компании через бота.\n"
                "• Персональные данные обрабатываются только в объёме, необходимом для ответа.")


def now():
    return datetime.now(TZ)


def ts():
    return now().strftime("%Y-%m-%d %H:%M")


async def q(sql, *a, one=False, all=False):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, a)
        r = await (cur.fetchone() if one else cur.fetchall()) if (one or all) else None
        await db.commit()
        return r


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript(SCHEMA)
        for d in range(7):
            await db.execute("INSERT OR IGNORE INTO schedule VALUES(?,?)", (d, "09:00-18:00" if d < 5 else "off"))
        await db.execute("INSERT OR IGNORE INTO settings VALUES('info',?)", (DEFAULT_INFO,))
        for a in ADMIN_IDS:
            await db.execute("INSERT OR IGNORE INTO admin_users VALUES(?)", (a,))
        await db.commit()


async def is_admin(uid):
    return bool(await q("SELECT 1 FROM admin_users WHERE tg_id=?", uid, one=True))


async def audit(admin_id, action):
    await q("INSERT INTO audit_log(admin_id,action,created) VALUES(?,?,?)", admin_id, action, ts())


async def log_msg(tg_id, direction, text):
    await q("INSERT INTO messages(tg_id,direction,text,created) VALUES(?,?,?,?)", tg_id, direction, text, ts())
    await q("UPDATE users SET last_contact=? WHERE tg_id=?", ts(), tg_id)


# ---------------- График ----------------
async def hours_for(dt):
    h = await q("SELECT hours FROM holidays WHERE date=?", dt.strftime("%Y-%m-%d"), one=True)
    if not h:
        h = await q("SELECT hours FROM schedule WHERE day=?", dt.weekday(), one=True)
    return h[0] if h else "off"


async def is_working():
    h = await hours_for(now())
    if h == "off":
        return False
    a, b = h.split("-")
    return a <= now().strftime("%H:%M") < b


async def schedule_text():
    rows = await q("SELECT day,hours FROM schedule ORDER BY day", all=True)
    lines = [f"{DAYS[d]} — {'выходной' if h == 'off' else h.replace('-', '–')}" for d, h in rows]
    return "🕘 График работы\n\n" + "\n".join(lines) + f"\n\nЧасовой пояс: {TZ.key}"


# ---------------- Клавиатуры ----------------
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="Связаться с оператором", callback_data="u:op")],
        [B(text="График работы", callback_data="u:sch")],
        [B(text="Уведомления", callback_data="u:ntf")],
        [B(text="Информация", callback_data="u:info")]])


def back_kb(op=False):
    rows = [[B(text="Написать оператору", callback_data="u:op")]] if op else []
    return InlineKeyboardMarkup(inline_keyboard=rows + [[B(text="В главное меню", callback_data="u:menu")]])


def ticket_kb(tg_id, tid):
    return InlineKeyboardMarkup(inline_keyboard=[[
        B(text="Ответить", callback_data=f"a:rep:{tg_id}"),
        B(text="История", callback_data=f"a:his:{tg_id}"),
        B(text="Закрыть", callback_data=f"a:cls:{tid}")]])


WELCOME = (f"Здравствуйте!\nВы обратились в компанию {COMPANY}.\n\n"
           "В данном чате вы можете получать уведомления, направлять обращения оператору "
           "и знакомиться с информацией о режиме работы.\n\nВыберите необходимый раздел в меню ниже.")


# ---------------- Пользовательская часть ----------------
@router.message(CommandStart())
async def start(m: Message):
    u = m.from_user
    await q("INSERT OR IGNORE INTO users(tg_id,name,username,first_seen,last_contact) VALUES(?,?,?,?,?)",
            u.id, u.full_name, u.username, ts(), ts())
    await m.answer(WELCOME, reply_markup=main_kb())


@router.callback_query(F.data.startswith("u:"))
async def user_menu(c: CallbackQuery):
    act = c.data[2:]
    if act == "menu":
        await c.message.answer(WELCOME, reply_markup=main_kb())
    elif act == "op":
        await c.message.answer(f"Вы можете направить сообщение оператору компании {COMPANY}.\n\n"
                               "Пожалуйста, изложите суть вашего вопроса одним сообщением. "
                               "Ваше обращение будет зарегистрировано и передано на рассмотрение.\n"
                               "Ответ поступит в этот чат.", reply_markup=back_kb())
    elif act == "sch":
        await c.message.answer(await schedule_text(), reply_markup=back_kb(op=True))
    elif act == "ntf":
        rows = await q("SELECT text,next_at FROM reminders WHERE tg_id=? AND status='scheduled' ORDER BY next_at",
                       c.from_user.id, all=True)
        txt = "🔔 Активных уведомлений нет." if not rows else \
            "🔔 Запланированные уведомления:\n\n" + "\n".join(f"• {n}" for _, n in rows)
        await c.message.answer(txt, reply_markup=back_kb())
    elif act == "info":
        info = (await q("SELECT value FROM settings WHERE key='info'", one=True))[0]
        await c.message.answer("ℹ️ Информация\n\n" + info, reply_markup=back_kb(op=True))
    await c.answer()


def content_of(m: Message):
    if m.text:
        return m.text
    kind = "фото" if m.photo else "документ" if m.document else "вложение"
    return f"[{kind}] {m.caption or ''}".strip()


async def handle_user_message(m: Message, bot: Bot):
    u = m.from_user
    await q("INSERT OR IGNORE INTO users(tg_id,name,username,first_seen,last_contact) VALUES(?,?,?,?,?)",
            u.id, u.full_name, u.username, ts(), ts())
    text = content_of(m)
    await log_msg(u.id, "in", text)

    t = await q("SELECT id,status FROM tickets WHERE tg_id=? AND status!='closed' ORDER BY id DESC LIMIT 1",
                u.id, one=True)
    new_ticket = t is None
    if new_ticket:
        await q("INSERT INTO tickets(tg_id,status,created,updated) VALUES(?,?,?,?)", u.id, "new", ts(), ts())
        t = await q("SELECT id,status FROM tickets WHERE tg_id=? ORDER BY id DESC LIMIT 1", u.id, one=True)
    else:
        st = "work" if t[1] == "wait_user" else t[1]
        await q("UPDATE tickets SET status=?,updated=? WHERE id=?", st, ts(), t[0])
    tid = t[0]

    paid = any(w in text.lower() for w in PAYMENT_WORDS)
    if paid:
        await q("UPDATE reminders SET status='paused' WHERE tg_id=? AND status='scheduled'", u.id)
        await q("UPDATE tickets SET status='check',updated=? WHERE id=?", ts(), tid)

    # Подтверждение — только для нового обращения (защита от повторов)
    if new_ticket:
        reply = (f"Ваше сообщение принято и передано оператору компании {COMPANY}.\n"
                 "Специалист рассмотрит обращение в соответствии с графиком работы.\n"
                 "Ответ будет направлен в этот чат.")
        if not await is_working():
            reply += "\n\nСейчас нерабочее время.\n\n" + await schedule_text()
        await m.answer(reply)
        await log_msg(u.id, "out", reply)

    who = f"{u.full_name}" + (f" (@{u.username})" if u.username else "")
    head = (f"📩 {'Новое обращение' if new_ticket else 'Сообщение по обращению'} #{tid}"
            f"{' · 💳 сообщает об оплате, напоминания приостановлены' if paid else ''}\n"
            f"Пользователь: {who}\nID: {u.id}\nВремя: {now():%H:%M}\n\nСообщение:\n{text}")
    for a in ADMIN_IDS:
        sent = await bot.send_message(a, head, reply_markup=ticket_kb(u.id, tid))
        await q("INSERT OR REPLACE INTO notify_map VALUES(?,?)", sent.message_id, u.id)
        if not m.text:
            await bot.copy_message(a, m.chat.id, m.message_id)


# ---------------- Операторская часть ----------------
async def send_to_user(bot: Bot, admin_id: int, tg_id: int, text: str):
    await bot.send_message(tg_id, text)
    await log_msg(tg_id, "out", text)
    await q("UPDATE tickets SET status='work',updated=? WHERE tg_id=? AND status='new'", ts(), tg_id)
    await audit(admin_id, f"Сообщение пользователю {tg_id}: {text[:80]}")


@router.callback_query(F.data.startswith("a:"))
async def admin_buttons(c: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    _, act, arg = c.data.split(":")
    if act == "rep":
        await state.set_state(Reply.text)
        await state.update_data(tg_id=int(arg))
        await c.message.answer(f"Введите ответ пользователю {arg} (или /cancel):")
    elif act == "his":
        await c.message.answer(await history_text(int(arg)))
    elif act == "cls":
        await q("UPDATE tickets SET status='closed',updated=? WHERE id=?", ts(), int(arg))
        await audit(c.from_user.id, f"Закрыто обращение #{arg}")
        await c.message.answer(f"Обращение #{arg} закрыто.")
    await c.answer()


@router.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.")


@router.message(Reply.text)
async def reply_text(m: Message, state: FSMContext, bot: Bot):
    tg_id = (await state.get_data())["tg_id"]
    await state.clear()
    try:
        await send_to_user(bot, m.from_user.id, tg_id, m.text or "")
        await m.answer(f"✅ Доставлено пользователю {tg_id}.")
    except Exception as e:
        await m.answer(f"❌ Ошибка доставки: {e}")


async def history_text(tg_id, limit=20):
    rows = await q("SELECT direction,text,created FROM messages WHERE tg_id=? ORDER BY id DESC LIMIT ?",
                   tg_id, limit, all=True)
    if not rows:
        return "История пуста."
    return f"🗂 История {tg_id}:\n\n" + "\n\n".join(
        f"{'👤' if d == 'in' else '🤖'} {c}\n{t}" for d, t, c in reversed(rows))


def admin_only(handler):
    async def wrapper(m: Message, *a, **kw):
        if not await is_admin(m.from_user.id):
            return await handle_user_message(m, kw["bot"])
        return await handler(m, *a, **kw)
    wrapper.__name__ = handler.__name__
    return wrapper


def args(m: Message, n):
    parts = (m.text or "").split(maxsplit=n)
    return parts[1:] if len(parts) > n else None


@router.message(Command("admin"))
@admin_only
async def admin_panel(m: Message, bot: Bot):
    new = (await q("SELECT COUNT(*) FROM tickets WHERE status='new'", one=True))[0]
    open_ = (await q("SELECT COUNT(*) FROM tickets WHERE status!='closed'", one=True))[0]
    users = (await q("SELECT COUNT(*) FROM users", one=True))[0]
    rem = (await q("SELECT COUNT(*) FROM reminders WHERE status='scheduled'", one=True))[0]
    await m.answer(f"🛠 Панель оператора\n\nПользователей: {users}\nНовых обращений: {new}\n"
                   f"Открытых обращений: {open_}\nЗапланировано напоминаний: {rem}\n\n" + __doc__.split("Команды")[1])


@router.message(Command("users"))
@admin_only
async def users_cmd(m: Message, bot: Bot):
    rows = await q("SELECT id,tg_id,name,username,last_contact FROM users ORDER BY last_contact DESC LIMIT 50", all=True)
    await m.answer("👥 Пользователи:\n\n" + "\n".join(
        f"№{i} · {t} · {n or ''} {'@' + u if u else ''} · {lc}" for i, t, n, u, lc in rows) if rows else "Нет пользователей.")


@router.message(Command("user"))
@admin_only
async def user_card(m: Message, bot: Bot):
    a = args(m, 1)
    if not a:
        return await m.answer("Формат: /user <tg_id>")
    r = await q("SELECT id,tg_id,name,username,first_seen,last_contact,notes FROM users WHERE tg_id=?", int(a[0]), one=True)
    if not r:
        return await m.answer("Не найден.")
    t = await q("SELECT id,status FROM tickets WHERE tg_id=? ORDER BY id DESC LIMIT 1", r[1], one=True)
    rem = await q("SELECT id,next_at,status FROM reminders WHERE tg_id=? AND status IN('scheduled','paused')", r[1], all=True)
    await m.answer(f"🪪 Пользователь №{r[0]}\nTelegram ID: {r[1]}\nИмя: {r[2]}\nUsername: {r[3] or '—'}\n"
                   f"Первый запуск: {r[4]}\nПоследний контакт: {r[5]}\n"
                   f"Обращение: {f'#{t[0]} — {STATUSES[t[1]]}' if t else '—'}\n"
                   f"Напоминания: {', '.join(f'#{i} {n} ({s})' for i, n, s in rem) or '—'}\nЗаметки: {r[6] or '—'}")


@router.message(Command("send"))
@admin_only
async def send_cmd(m: Message, bot: Bot):
    a = args(m, 2)
    if not a:
        return await m.answer("Формат: /send <tg_id> <текст>")
    try:
        await send_to_user(bot, m.from_user.id, int(a[0]), a[1])
        await m.answer("✅ Отправлено.")
    except Exception as e:
        await m.answer(f"❌ Ошибка: {e}")


@router.message(Command("history"))
@admin_only
async def history_cmd(m: Message, bot: Bot):
    a = args(m, 1)
    await m.answer(await history_text(int(a[0])) if a else "Формат: /history <tg_id>")


@router.message(Command("note"))
@admin_only
async def note_cmd(m: Message, bot: Bot):
    a = args(m, 2)
    if not a:
        return await m.answer("Формат: /note <tg_id> <текст>")
    await q("UPDATE users SET notes=COALESCE(notes,'')||? WHERE tg_id=?", f"\n[{ts()}] {a[1]}", int(a[0]))
    await audit(m.from_user.id, f"Заметка {a[0]}")
    await m.answer("Заметка сохранена.")


@router.message(Command("tickets"))
@admin_only
async def tickets_cmd(m: Message, bot: Bot):
    rows = await q("SELECT id,tg_id,status,updated FROM tickets WHERE status!='closed' ORDER BY id", all=True)
    await m.answer("📂 Открытые обращения:\n\n" + "\n".join(
        f"#{i} · {t} · {STATUSES[s]} · {u}" for i, t, s, u in rows) if rows else "Открытых обращений нет.")


@router.message(Command("status"))
@admin_only
async def status_cmd(m: Message, bot: Bot):
    a = args(m, 2)
    if not a or a[1] not in STATUSES:
        return await m.answer("Формат: /status <id> <" + "|".join(STATUSES) + ">")
    await q("UPDATE tickets SET status=?,updated=? WHERE id=?", a[1], ts(), int(a[0]))
    await audit(m.from_user.id, f"Обращение #{a[0]} → {a[1]}")
    await m.answer(f"Обращение #{a[0]}: {STATUSES[a[1]]}")


@router.message(Command("remind"))
@admin_only
async def remind_cmd(m: Message, bot: Bot):
    a = args(m, 6)
    try:
        tg_id, d, t, rep, mx, text = a
        at = datetime.strptime(f"{d} {t}", "%Y-%m-%d %H:%M")
        rep, mx = int(rep), max(1, min(int(mx), 10))
    except Exception:
        return await m.answer("Формат: /remind <tg_id> <ГГГГ-ММ-ДД> <ЧЧ:ММ> <повтор_дней|0> <макс_раз 1-10> <текст>")
    await q("INSERT INTO reminders(tg_id,text,next_at,repeat_days,max_sends) VALUES(?,?,?,?,?)",
            int(tg_id), text, at.strftime("%Y-%m-%d %H:%M"), rep, mx)
    await audit(m.from_user.id, f"Напоминание для {tg_id} на {at}")
    await m.answer(f"⏰ Запланировано: {tg_id}, {at:%d.%m.%Y %H:%M}, повтор {rep} дн., максимум {mx} раз.")


@router.message(Command("reminders"))
@admin_only
async def reminders_cmd(m: Message, bot: Bot):
    rows = await q("SELECT id,tg_id,next_at,sent_count,max_sends,status,text FROM reminders "
                   "WHERE status IN('scheduled','paused','error') ORDER BY next_at", all=True)
    await m.answer("⏰ Напоминания:\n\n" + "\n\n".join(
        f"#{i} · {t} · {n} · {s}/{mx} · {st}\n{tx[:100]}" for i, t, n, s, mx, st, tx in rows) if rows else "Нет напоминаний.")


@router.message(Command("cancel_reminder"))
@admin_only
async def cancel_rem(m: Message, bot: Bot):
    a = args(m, 1)
    if not a:
        return await m.answer("Формат: /cancel_reminder <id>")
    await q("UPDATE reminders SET status='cancelled' WHERE id=?", int(a[0]))
    await audit(m.from_user.id, f"Отменено напоминание #{a[0]}")
    await m.answer("Отменено.")


@router.message(Command("templates"))
@admin_only
async def templates_cmd(m: Message, bot: Bot):
    rows = await q("SELECT code,text FROM templates", all=True)
    await m.answer("📝 Шаблоны:\n\n" + "\n\n".join(f"{c}: {t}" for c, t in rows) if rows else "Шаблонов нет. /addtemplate <код> <текст>")


@router.message(Command("addtemplate"))
@admin_only
async def addtpl(m: Message, bot: Bot):
    a = args(m, 2)
    if not a:
        return await m.answer("Формат: /addtemplate <код> <текст>")
    await q("INSERT OR REPLACE INTO templates VALUES(?,?)", a[0], a[1])
    await audit(m.from_user.id, f"Шаблон {a[0]}")
    await m.answer("Шаблон сохранён.")


@router.message(Command("tpl"))
@admin_only
async def tpl_send(m: Message, bot: Bot):
    a = args(m, 2)
    r = a and await q("SELECT text FROM templates WHERE code=?", a[0], one=True)
    if not r:
        return await m.answer("Формат: /tpl <код> <tg_id>")
    await send_to_user(bot, m.from_user.id, int(a[1]), r[0])
    await m.answer("✅ Отправлено.")


def valid_hours(h):
    if h == "off":
        return True
    try:
        a, b = h.split("-")
        return datetime.strptime(a, "%H:%M") < datetime.strptime(b, "%H:%M")
    except Exception:
        return False


@router.message(Command("setschedule"))
@admin_only
async def setsch(m: Message, bot: Bot):
    a = args(m, 2)
    if not a or not a[0].isdigit() or int(a[0]) > 6 or not valid_hours(a[1]):
        return await m.answer("Формат: /setschedule <0-6> <09:00-18:00|off>")
    await q("INSERT OR REPLACE INTO schedule VALUES(?,?)", int(a[0]), a[1])
    await audit(m.from_user.id, f"График {DAYS[int(a[0])]} = {a[1]}")
    await m.answer(await schedule_text())


@router.message(Command("holiday"))
@admin_only
async def holiday(m: Message, bot: Bot):
    a = args(m, 2)
    if not a or not valid_hours(a[1]):
        return await m.answer("Формат: /holiday <ГГГГ-ММ-ДД> <10:00-15:00|off>")
    await q("INSERT OR REPLACE INTO holidays VALUES(?,?)", a[0], a[1])
    await audit(m.from_user.id, f"Праздник {a[0]} = {a[1]}")
    await m.answer("Сохранено.")


@router.message(Command("setinfo"))
@admin_only
async def setinfo(m: Message, bot: Bot):
    a = args(m, 1)
    if not a:
        return await m.answer("Формат: /setinfo <текст>")
    await q("INSERT OR REPLACE INTO settings VALUES('info',?)", a[0])
    await audit(m.from_user.id, "Изменён раздел «Информация»")
    await m.answer("Сохранено.")


@router.message(Command("log"))
@admin_only
async def log_cmd(m: Message, bot: Bot):
    rows = await q("SELECT created,admin_id,action FROM audit_log ORDER BY id DESC LIMIT 30", all=True)
    await m.answer("📜 Журнал:\n\n" + "\n".join(f"{c} · {a} · {t}" for c, a, t in rows) if rows else "Журнал пуст.")


# Все прочие сообщения
@router.message()
async def any_message(m: Message, bot: Bot):
    if await is_admin(m.from_user.id):
        # Reply на уведомление = ответ пользователю
        if m.reply_to_message:
            r = await q("SELECT tg_id FROM notify_map WHERE admin_msg_id=?", m.reply_to_message.message_id, one=True)
            if r:
                await send_to_user(bot, m.from_user.id, r[0], m.text or "")
                return await m.answer(f"✅ Доставлено пользователю {r[0]}.")
        return await m.answer("Используйте /admin. Чтобы ответить — кнопка «Ответить» или Reply на уведомление.")
    await handle_user_message(m, bot)


# ---------------- Планировщик ----------------
async def scheduler(bot: Bot):
    while True:
        try:
            rows = await q("SELECT id,tg_id,text,repeat_days,max_sends,sent_count,next_at FROM reminders "
                           "WHERE status='scheduled' AND next_at<=?", ts(), all=True)
            for rid, tg_id, text, rep, mx, cnt, nxt in rows:
                # блокируем запись до отправки — защита от дублей при перезапуске
                await q("UPDATE reminders SET status='sending' WHERE id=? AND status='scheduled'", rid)
                try:
                    await bot.send_message(tg_id, text)
                    await log_msg(tg_id, "out", text)
                    cnt += 1
                    if rep > 0 and cnt < mx:
                        n = datetime.strptime(nxt, "%Y-%m-%d %H:%M") + timedelta(days=rep)
                        await q("UPDATE reminders SET status='scheduled',sent_count=?,next_at=? WHERE id=?",
                                cnt, n.strftime("%Y-%m-%d %H:%M"), rid)
                    else:
                        await q("UPDATE reminders SET status='sent',sent_count=? WHERE id=?", cnt, rid)
                except Exception as e:
                    await q("UPDATE reminders SET status='error',last_error=? WHERE id=?", str(e), rid)
                    for a in ADMIN_IDS:
                        await bot.send_message(a, f"⚠️ Напоминание #{rid} не доставлено ({tg_id}): {e}")
        except Exception:
            logging.exception("scheduler")
        await asyncio.sleep(30)


async def main():
    await init_db()
    # восстановление после сбоя: зависшие 'sending' считаем ошибкой, без повторной отправки
    await q("UPDATE reminders SET status='error',last_error='interrupted' WHERE status='sending'")
    bot = Bot(TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

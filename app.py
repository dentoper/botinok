import asyncio
import logging
import logging.handlers
import os
import sqlite3
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

from config import BOT_TOKEN, ADMIN_IDS

# ---------------------------------------------------------------------------
# ЛОГИРОВАНИЕ
# ---------------------------------------------------------------------------

def setup_logging():
    """
    Настраивает два обработчика:
      - logs/bot.log       — INFO и выше, ротация каждый день, хранить 30 файлов
      - logs/errors.log    — WARNING и выше, ротация каждый день, хранить 30 файлов
    Формат: дата/время | уровень | модуль | сообщение
    """
    os.makedirs("logs", exist_ok=True)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Общий лог (INFO+)
    info_handler = logging.handlers.TimedRotatingFileHandler(
        filename="logs/bot.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(fmt)
    info_handler.suffix = "%Y-%m-%d"

    # Лог ошибок (WARNING+)
    error_handler = logging.handlers.TimedRotatingFileHandler(
        filename="logs/errors.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.WARNING)
    error_handler.setFormatter(fmt)
    error_handler.suffix = "%Y-%m-%d"

    # Консоль (INFO+)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(info_handler)
    root_logger.addHandler(error_handler)
    root_logger.addHandler(console_handler)


setup_logging()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# АНТИСПАМ — кулдаун для /ab и /отмена (раздельно для каждой команды)
# ---------------------------------------------------------------------------
# Два независимых словаря: кулдаун /ab не влияет на /отмена и наоборот.
# Это позволяет сразу отменить случайную отметку, но не спамить повторно.
import time as _time

_last_absent_time: dict[int, float] = {}
_last_cancel_time: dict[int, float] = {}
SPAM_COOLDOWN_SECONDS = 60  # минимальный интервал между одинаковыми командами


def _check(store: dict, user_id: int) -> float:
    """Возвращает 0 если кулдаун прошёл, иначе — сколько секунд осталось."""
    remaining = SPAM_COOLDOWN_SECONDS - (_time.time() - store.get(user_id, 0))
    return max(0.0, remaining)


def _reset(store: dict, user_id: int) -> None:
    store[user_id] = _time.time()


def check_absent_cooldown(user_id: int) -> float:
    return _check(_last_absent_time, user_id)


def reset_absent_cooldown(user_id: int) -> None:
    _reset(_last_absent_time, user_id)


def check_cancel_cooldown(user_id: int) -> float:
    return _check(_last_cancel_time, user_id)


def reset_cancel_cooldown(user_id: int) -> None:
    _reset(_last_cancel_time, user_id)


# ---------------------------------------------------------------------------
# FSM-СОСТОЯНИЯ
# ---------------------------------------------------------------------------

class AdminStates(StatesGroup):
    waiting_for_ban_id            = State()
    waiting_for_unban_id          = State()
    waiting_for_delete_student_id = State()
    waiting_for_remove_absence_id = State()

# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ
# ---------------------------------------------------------------------------

DB_PATH = "students.db"


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS students (
                user_id   INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS absences (
                user_id     INTEGER,
                absent_date TEXT,
                comment     TEXT,
                PRIMARY KEY (user_id, absent_date)
            );
            CREATE TABLE IF NOT EXISTS blacklist (
                user_id INTEGER PRIMARY KEY
            );
        """)
    logger.info("База данных инициализирована.")


def db_query(query: str, params: tuple = (), *, fetch: bool = False):
    """Универсальная обёртка над sqlite3."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(query, params)
        return cursor.fetchall() if fetch else None


# ---------------------------------------------------------------------------
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ---------------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_banned(user_id: int) -> bool:
    return bool(db_query("SELECT 1 FROM blacklist WHERE user_id = ?", (user_id,), fetch=True))


def is_registered(user_id: int) -> bool:
    return bool(db_query("SELECT 1 FROM students WHERE user_id = ?", (user_id,), fetch=True))


def get_absent_list_text() -> str:
    """Формирует текст со списком отсутствующих за вчера и сегодня."""
    now = datetime.now()
    result = ""
    for label, d_obj in [("Вчера", now - timedelta(days=1)), ("Сегодня", now)]:
        db_date      = d_obj.strftime("%Y-%m-%d")
        display_date = d_obj.strftime("%d.%m.%Y")
        rows = db_query(
            """SELECT s.full_name, a.comment
               FROM students s JOIN absences a ON s.user_id = a.user_id
               WHERE a.absent_date = ?""",
            (db_date,),
            fetch=True,
        )
        result += f"📅 <b>{label} ({display_date}):</b>\n"
        if rows:
            for i, (name, comment) in enumerate(rows, 1):
                comment_str = f" (<i>{comment}</i>)" if comment else ""
                result += f"{i}. {name}{comment_str}\n"
        else:
            result += "— Список пуст\n"
        result += "\n"
    return result


async def notify_admins(bot: Bot):
    """Рассылает актуальный список отсутствующих всем администраторам."""
    text = "🔔 <b>Обновление данных!</b>\n\n" + get_absent_list_text()
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception as exc:
            logger.error("Ошибка уведомления администратора %d: %s", admin_id, exc)

# ---------------------------------------------------------------------------
# КЛАВИАТУРА УЧИТЕЛЯ
# ---------------------------------------------------------------------------

ADMIN_BTN_LIST   = "📋 Список (Вчера/Сегодня)"
ADMIN_BTN_ALL    = "👥 Все ученики"
ADMIN_BTN_BAN    = "🚫 Забанить по ID"
ADMIN_BTN_UNBAN  = "✅ Разбанить по ID"
ADMIN_BTN_DEL_ST = "🗑 Удалить из учеников"
ADMIN_BTN_REM_AB = "➖ Убрать из отсутствующих"
ADMIN_BTN_CLEAR  = "🧹 Очистить сегодня"
ADMIN_BTN_CLOSE  = "❌ Закрыть панель"


def get_admin_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=ADMIN_BTN_LIST),   KeyboardButton(text=ADMIN_BTN_ALL)],
            [KeyboardButton(text=ADMIN_BTN_BAN),    KeyboardButton(text=ADMIN_BTN_UNBAN)],
            [KeyboardButton(text=ADMIN_BTN_DEL_ST), KeyboardButton(text=ADMIN_BTN_REM_AB)],
            [KeyboardButton(text=ADMIN_BTN_CLEAR),  KeyboardButton(text=ADMIN_BTN_CLOSE)],
        ],
        resize_keyboard=True,
    )

# ---------------------------------------------------------------------------
# ИНИЦИАЛИЗАЦИЯ
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher(storage=MemoryStorage())

# ---------------------------------------------------------------------------
# КОМАНДЫ ПОЛЬЗОВАТЕЛЯ
# ---------------------------------------------------------------------------

@dp.message(Command("помощь", "help"))
async def cmd_help(message: types.Message):
    text = (
        "📖 <b>Справка:</b>\n\n"
        "/список — Кто отсутствовал вчера и сегодня\n"
        "/ab [причина] — Отметиться как отсутствующий\n"
        "/отмена — Удалить свою запись на сегодня\n"
        "/переименовать [Фамилия Имя] — Сменить имя\n"
    )
    if is_admin(message.from_user.id):
        text += "\n👑 <b>Для учителя:</b>\n/админ — Панель управления"
    await message.answer(text)


@dp.message(Command("старт", "start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    row = db_query("SELECT full_name FROM students WHERE user_id = ?", (uid,), fetch=True)
    if row:
        logger.info("Пользователь %d (%s) запустил /start — уже в базе.", uid, row[0][0])
        await message.answer(f"Привет, {row[0][0]}! Ты в базе.\n/список — посмотреть отсутствие.")
    else:
        logger.info("Пользователь %d запустил /start — не зарегистрирован.", uid)
        await message.answer("Привет! Напиши свои <b>Фамилию и Имя</b> для регистрации.")


@dp.message(Command("список", "list"))
async def cmd_list(message: types.Message):
    await message.answer(get_absent_list_text())


@dp.message(Command("ab", "отсутствую"))
async def cmd_absent(message: types.Message, command: CommandObject):
    uid = message.from_user.id

    if is_banned(uid):
        return

    if not is_registered(uid):
        return await message.answer("Сначала зарегистрируйся (напиши ФИО).")

    # Антиспам — кулдаун только на повторный /ab
    remaining = check_absent_cooldown(uid)
    if remaining > 0:
        return await message.answer(
            f"⏳ Не так быстро! Подожди ещё <b>{int(remaining)} сек.</b> перед повторной отметкой."
        )

    today = datetime.now().strftime("%Y-%m-%d")

    # Проверка: уже отмечен сегодня?
    already = db_query(
        "SELECT 1 FROM absences WHERE user_id = ? AND absent_date = ?",
        (uid, today),
        fetch=True,
    )
    if already:
        return await message.answer(
            "⚠️ Ты уже отметился/лась.\n"
            "Если ты отметился/лась по ошибке — напиши /отмена."
        )

    db_query(
        "INSERT INTO absences (user_id, absent_date, comment) VALUES (?, ?, ?)",
        (uid, today, command.args),
    )
    reset_absent_cooldown(uid)
    logger.info("Пользователь %d отметился как отсутствующий (%s). Причина: %s", uid, today, command.args)
    await message.answer("✅ Ты добавлен в список на сегодня.")
    await notify_admins(message.bot)


@dp.message(Command("отмена", "cancel"))
async def cmd_cancel(message: types.Message):
    uid = message.from_user.id

    if is_banned(uid):
        return

    # Антиспам — кулдаун только на повторный /отмена
    remaining = check_cancel_cooldown(uid)
    if remaining > 0:
        return await message.answer(
            f"⏳ Не так быстро! Подожди ещё <b>{int(remaining)} сек.</b> перед повторной отменой."
        )

    today = datetime.now().strftime("%Y-%m-%d")

    # Проверяем, была ли отметка вообще
    was_absent = db_query(
        "SELECT 1 FROM absences WHERE user_id = ? AND absent_date = ?",
        (uid, today),
        fetch=True,
    )
    if not was_absent:
        return await message.answer("У тебя нет отметки на сегодня.")

    # Получаем имя для уведомления учителей
    row = db_query("SELECT full_name FROM students WHERE user_id = ?", (uid,), fetch=True)
    full_name = row[0][0] if row else f"ID {uid}"

    db_query("DELETE FROM absences WHERE user_id = ? AND absent_date = ?", (uid, today))
    reset_cancel_cooldown(uid)
    logger.info("Пользователь %d (%s) отменил отметку (%s).", uid, full_name, today)
    await message.answer("✅ Твоя запись на сегодня удалена.")

    # Уведомление учителям с именем ученика
    cancel_text = f"❌ <b>{full_name}</b> отменил(а) свою отметку об отсутствии.\n\n" + get_absent_list_text()
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, cancel_text)
        except Exception as exc:
            logger.error("Ошибка уведомления администратора %d: %s", admin_id, exc)


@dp.message(Command("переименовать", "rename"))
async def cmd_rename(message: types.Message, command: CommandObject):
    """FIX: команда была в /помощь, но не реализована."""
    uid = message.from_user.id
    if is_banned(uid):
        return
    if not is_registered(uid):
        return await message.answer("Сначала зарегистрируйся (напиши ФИО).")
    if not command.args:
        return await message.answer("Укажи новое имя: /переименовать Фамилия Имя")

    new_name = command.args.strip()
    db_query("UPDATE students SET full_name = ? WHERE user_id = ?", (new_name, uid))
    logger.info("Пользователь %d сменил имя на «%s».", uid, new_name)
    await message.answer(f"✅ Имя изменено на: <b>{new_name}</b>.")


@dp.message(Command("админ", "admin"))
async def cmd_admin(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer("Панель учителя открыта.", reply_markup=get_admin_kb())

# ---------------------------------------------------------------------------
# КНОПКИ ПАНЕЛИ УЧИТЕЛЯ
# ---------------------------------------------------------------------------

@dp.message(F.text == ADMIN_BTN_CLOSE)
async def btn_close_admin(message: types.Message, state: FSMContext):
    # FIX: проверка прав (раньше отсутствовала)
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Панель закрыта.", reply_markup=ReplyKeyboardRemove())


@dp.message(F.text == ADMIN_BTN_LIST)
async def btn_list(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer(get_absent_list_text())


@dp.message(F.text == ADMIN_BTN_ALL)
async def btn_all_students(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    rows = db_query("SELECT user_id, full_name FROM students ORDER BY full_name", fetch=True)
    text = "👥 <b>Все ученики:</b>\n\n"
    if rows:
        for i, (uid, name) in enumerate(rows, 1):
            # tg://user?id= открывает профиль пользователя по клику
            text += f"{i}. <a href=\"tg://user?id={uid}\">{name}</a> [<code>{uid}</code>]\n"
    else:
        text += "Список пуст."
    await message.answer(text, disable_web_page_preview=True)


@dp.message(F.text == ADMIN_BTN_CLEAR)
async def btn_clear_today(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    today = datetime.now().strftime("%Y-%m-%d")
    db_query("DELETE FROM absences WHERE absent_date = ?", (today,))
    logger.info("Администратор %d очистил список на %s.", message.from_user.id, today)
    await message.answer("🧹 Список на сегодня очищен.")
    await notify_admins(message.bot)


# --- Бан ---

@dp.message(F.text == ADMIN_BTN_BAN)
async def btn_ban_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите ID для бана:")
    await state.set_state(AdminStates.waiting_for_ban_id)


@dp.message(AdminStates.waiting_for_ban_id)
async def btn_ban_finish(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        uid = int(message.text.strip())
        db_query("INSERT OR IGNORE INTO blacklist VALUES (?)", (uid,))
        logger.warning("Администратор %d забанил пользователя %d.", message.from_user.id, uid)
        await message.answer(f"🚫 Пользователь {uid} забанен.")
        await state.clear()
    except ValueError:
        await message.answer("Ошибка: введите числовой ID.")


# --- Разбан ---

@dp.message(F.text == ADMIN_BTN_UNBAN)
async def btn_unban_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите ID для разбана:")
    await state.set_state(AdminStates.waiting_for_unban_id)


@dp.message(AdminStates.waiting_for_unban_id)
async def btn_unban_finish(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        uid = int(message.text.strip())
        was_banned = db_query("SELECT 1 FROM blacklist WHERE user_id = ?", (uid,), fetch=True)
        if not was_banned:
            await message.answer(f"ℹ️ Пользователь {uid} не находится в бане.")
        else:
            db_query("DELETE FROM blacklist WHERE user_id = ?", (uid,))
            logger.warning("Администратор %d разбанил пользователя %d.", message.from_user.id, uid)
            await message.answer(f"✅ Пользователь {uid} разбанен.")
        await state.clear()
    except ValueError:
        await message.answer("Ошибка: введите числовой ID.")


# --- Удаление ученика ---

@dp.message(F.text == ADMIN_BTN_DEL_ST)
async def btn_del_student_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите ID для удаления ученика:")
    await state.set_state(AdminStates.waiting_for_delete_student_id)


@dp.message(AdminStates.waiting_for_delete_student_id)
async def btn_del_student_finish(message: types.Message, state: FSMContext):
    # FIX: проверка прав внутри FSM-обработчика
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        uid = int(message.text.strip())
        db_query("DELETE FROM students WHERE user_id = ?", (uid,))
        db_query("DELETE FROM absences WHERE user_id = ?", (uid,))
        logger.warning("Администратор %d удалил ученика %d из базы.", message.from_user.id, uid)
        await message.answer(f"🗑 Пользователь {uid} удалён из базы.")
        await state.clear()
    except ValueError:
        await message.answer("Ошибка: введите числовой ID.")


# --- Убрать из отсутствующих ---

@dp.message(F.text == ADMIN_BTN_REM_AB)
async def btn_rem_absence_start(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите ID, чтобы убрать из списка на сегодня:")
    await state.set_state(AdminStates.waiting_for_remove_absence_id)


@dp.message(AdminStates.waiting_for_remove_absence_id)
async def btn_rem_absence_finish(message: types.Message, state: FSMContext):
    # FIX: проверка прав внутри FSM-обработчика
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    try:
        uid = int(message.text.strip())
        today = datetime.now().strftime("%Y-%m-%d")
        db_query("DELETE FROM absences WHERE user_id = ? AND absent_date = ?", (uid, today))
        logger.info("Администратор %d убрал пользователя %d из списка (%s).", message.from_user.id, uid, today)
        await message.answer(f"✅ Пользователь {uid} убран из списка.")
        await notify_admins(message.bot)
        await state.clear()
    except ValueError:
        await message.answer("Ошибка: введите числовой ID.")


# ---------------------------------------------------------------------------
# РЕГИСТРАЦИЯ (свободный текст)
# ---------------------------------------------------------------------------

@dp.message(F.text)
async def handle_text(message: types.Message):
    uid = message.from_user.id

    # Игнорируем команды, администраторов и забаненных
    if message.text.startswith("/") or is_admin(uid) or is_banned(uid):
        return

    if not is_registered(uid):
        name = message.text.strip()
        db_query("INSERT INTO students (user_id, full_name) VALUES (?, ?)", (uid, name))
        logger.info("Зарегистрирован новый ученик %d: «%s».", uid, name)
        await message.answer(
            f"✅ Записал тебя как: <b>{name}</b>.\n"
            "Теперь можешь использовать /отсутствую."
        )

# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------

async def main():
    init_db()
    await bot.set_my_commands([
        BotCommand(command="start",        description="Запуск"),
        BotCommand(command="list",         description="Список вчера/сегодня"),
        BotCommand(command="ab",           description="Отметиться как отсутствующий"),
        BotCommand(command="cancel",       description="Отмена отметки"),
        BotCommand(command="rename",       description="Сменить имя"),
        BotCommand(command="help",         description="Помощь"),
    ])
    logger.info("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

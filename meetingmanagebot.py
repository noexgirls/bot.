import logging
import asyncio
import re
from dotenv import load_dotenv
from pathlib import Path
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

from tinydb import TinyDB, Query
from tinydb.operations import delete

db = TinyDB("meetings_db.json")

meetings_table = db.table("meetings")
agendas_table = db.table("agendas")
proposals_table = db.table("proposals")
users_table = db.table("users")

BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"

load_dotenv(env_path)

API_TOKEN = os.getenv("BOT_TOKEN")
print("ENV PATH:", env_path)
print("BOT_TOKEN:", API_TOKEN)

if not API_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN не найден. "
        "Создай .env на основе .env.example"
    )

ADMIN_IDS = {642167821}
MEETINGS_PER_PAGE = 5

logging.basicConfig(level=logging.INFO)
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

meetings: dict[int, dict[str,str]]       = {}
agendas:  dict[int, list[dict[str,str]]]  = {}
proposals: dict[int, list[tuple[str,str]]] = {}
all_users: set[int]                      = set()

def save_meetings():
    meetings_table.truncate()
    for mid, data in meetings.items():
        meetings_table.insert({"id": mid, **data})

def save_agendas():
    agendas_table.truncate()
    for mid, items in agendas.items():
        agendas_table.insert({"meeting_id": mid, "items": items})

def save_proposals():
    proposals_table.truncate()
    for mid, items in proposals.items():
        proposals_table.insert({"meeting_id": mid, "items": items})

def save_users():
    users_table.truncate()
    for u in all_users:
        users_table.insert({"user_id": u})

def load_data():
    global meetings, agendas, proposals, all_users
    meetings = {r["id"]: {k: v for k, v in r.items() if k != "id"} for r in meetings_table.all()}
    agendas = {r["meeting_id"]: r["items"] for r in agendas_table.all()}
    proposals = {r["meeting_id"]: r["items"] for r in proposals_table.all()}
    all_users = {r["user_id"] for r in users_table.all()}

class States(StatesGroup):
    editing_select  = State()
    editing_date    = State()
    editing_title   = State()
    editing_desc    = State()
    cancelling      = State()
    agenda_add      = State()
    agenda_notify   = State()
    assign_action = State()
    assign_id     = State()
    propose_select = State()
    propose_text   = State()
    propose_confirm = State()
    creating_date  = State()
    creating_title = State()
    creating_desc  = State()
    agenda_mid      = State()
    agenda_title2   = State()
    agenda_desc2    = State()
    agenda_type2    = State()
    agenda_edit_field = State()
    agenda_edit_title = State()
    agenda_edit_desc  = State()
    agenda_edit_type  = State()
    agenda_assign_user = State()
    agenda_view_select = State()


USER_MAIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📋 Список совещаний", callback_data="menu_list")],
    [InlineKeyboardButton(text="➕ Предложить тему", callback_data="menu_propose")],
])

ADMIN_MAIN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📋 Список совещаний", callback_data="menu_list")],
    [InlineKeyboardButton(text="➕ Создать совещание", callback_data="menu_create")],
    [InlineKeyboardButton(text="🗒 Завести повестку",   callback_data="menu_agenda")],
    [InlineKeyboardButton(text="🔍 Предложения",       callback_data="menu_view_props")],
    [InlineKeyboardButton(text="👥 Админы",            callback_data="menu_assign")],
])

LIST_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="✏️ Изменить", callback_data="list_edit"),
        InlineKeyboardButton(text="❌ Удалить", callback_data="list_delete"),
    ],
    [InlineKeyboardButton(text="📝 Предложить тему", callback_data="list_propose"),],
    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")],
])

@dp.callback_query(F.data == "list_propose")
async def cb_list_propose(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    if not meetings:
        kb = ADMIN_MAIN_KB if c.from_user.id in ADMIN_IDS else USER_MAIN_KB
        return await c.message.answer("Сначала создайте совещание.", reply_markup=kb)

    text = "Для какого совещания хотите предложить тему? Введите номер:\n\n" + "\n".join(
        f"{i}. {v['datetime']} — {v['title']}" for i,v in meetings.items()
    )
    await c.message.answer(text)
    await state.set_state(States.propose_select)

AGENDA_KB = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Добавить пункт",     callback_data="agenda_add")],
    [InlineKeyboardButton(text="🔔 Рассылка повестки", callback_data="agenda_notify")],
    [InlineKeyboardButton(text="🏠 Меню",               callback_data="menu_home")],
])

ASSIGN_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="➕ Назначить",   callback_data="assign_add"),
        InlineKeyboardButton(text="➖ Снять права", callback_data="assign_remove"),
    ],
    [InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")],
])

def meeting_kb(mid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✏️ Изменить", callback_data=f"meet_edit:{mid}"),
            InlineKeyboardButton(text="❌ Удалить",  callback_data=f"meet_del:{mid}")
        ],
        [InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")],
    ])
# вспомогательная функция для валидации
def validate_datetime(dt_str: str) -> tuple[bool, str, datetime]:
    """
    Парсит строку dt_str в формате 'дд.мм.гг чч:мм'.
    Возвращает (ok, error_message, dt_obj).
    """
    try:
        dt_obj = datetime.strptime(dt_str, "%d.%m.%y %H:%M")
    except ValueError:
        return False, "⚠️ Некорректный формат даты. Используйте dd.mm.yy hh:MM.", None

    now = datetime.now()
    one_year = now + timedelta(days=365)
    if dt_obj < now:
        return False, "❌ Дата меньше текущей.", None
    if dt_obj > one_year:
        return False, "❌ Дата больше, чем через год — введите более раннюю дату.", None

    return True, "", dt_obj

async def send_reminder(users: list[int], text: str, delay: float):
    await asyncio.sleep(delay)
    for u in users:
        try:
            await bot.send_message(u, text)
        except Exception as e:
            logging.warning(f"Не удалось отправить напоминание {u}: {e}")
            
def get_sorted_meetings():
    """Возвращает список встреч, отсортированный по дате."""
    return sorted(meetings.items(), key=lambda x: datetime.strptime(x[1]['datetime'], "%d.%m.%y %H:%M"))

def get_meetings_page(page: int):
    """Возвращает срез встреч для страницы."""
    sorted_meets = get_sorted_meetings()
    start = page * MEETINGS_PER_PAGE
    end = start + MEETINGS_PER_PAGE
    return sorted_meets[start:end], len(sorted_meets)

def list_keyboard(page: int, total: int, is_admin: bool):
    """Клавиатура для списка с пагинацией."""
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅ Назад", callback_data=f"list_page:{page-1}"))
    if (page+1) * MEETINGS_PER_PAGE < total:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡", callback_data=f"list_page:{page+1}"))

    kb_buttons = []
    if is_admin:
        kb_buttons.append([InlineKeyboardButton(text="✏️ Изменить", callback_data="list_edit"),
                           InlineKeyboardButton(text="❌ Удалить", callback_data="list_delete")])
        kb_buttons.append([InlineKeyboardButton(text="📝 Предложить тему", callback_data="list_propose")])
    else:
        kb_buttons.append([InlineKeyboardButton(text="📝 Предложить тему", callback_data="list_propose")])

    kb_buttons.append([InlineKeyboardButton(text="ℹ️ Подробнее", callback_data="list_details")])

    kb_buttons.append(nav_buttons) if nav_buttons else None
    kb_buttons.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")])

    return InlineKeyboardMarkup(inline_keyboard=kb_buttons)

def meeting_edit_kb(mid: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Дата/время", callback_data=f"edit_field:date:{mid}")],
        [InlineKeyboardButton(text="📝 Тема",       callback_data=f"edit_field:title:{mid}")],
        [InlineKeyboardButton(text="📄 Описание",  callback_data=f"edit_field:desc:{mid}")],
        [InlineKeyboardButton(text="🏠 Меню",      callback_data="menu_home")],
    ])

def back_home_kb(back_callback: str = "menu_home") -> InlineKeyboardMarkup:
    """Универсальная клавиатура Назад/Главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="↩ Назад", callback_data=back_callback),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="menu_home"),
        ]
    ])

def back_home_kb(back_callback: str = "menu_home") -> InlineKeyboardMarkup:
    """Универсальная клавиатура Назад/Главное меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="↩ Назад", callback_data=back_callback),
        ]
    ])

def next_agenda_item_id(mid: int) -> int:
    items = agendas.get(mid, [])
    return max((it.get("id", 0) for it in items), default=0) + 1

def find_agenda_item(mid: int, item_id: int):
    return next((it for it in agendas.get(mid, []) if it.get("id") == item_id), None)

def normalize_orders(mid: int):
    """Перенумеровывает поля order по порядку в списке."""
    items = agendas.get(mid, [])
    items.sort(key=lambda x: x.get("order", 0))
    for idx, it in enumerate(items, start=1):
        it["order"] = idx

def build_agenda_text_and_kb(mid: int, page: int, is_admin: bool):
    items = agendas.get(mid, [])
    if not items:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")]
        ])
        return "📭 Повестка пуста.", kb

    per_page = 6
    start = page * per_page
    end = start + per_page
    items_sorted = sorted(items, key=lambda x: x.get("order", 0))
    page_items = items_sorted[start:end]

    text = f"📖 Повестка к совещанию {mid}\n\n"

    for it in page_items:
        status = "✅" if it.get("done") else "▫️"
        assigned = f" (ответственный: {it['assigned']})" if it.get("assigned") else ""

        raw_typ = (it.get("type") or "").strip()
        typ_label = ""
        if raw_typ:
            rt = raw_typ.lower()
            if rt in ("required", "обязательный", "обязательно"):
                typ_label = " (обязательный)"
            elif rt in ("optional", "доп", "дополнительный"):
                typ_label = " (дополнительный)"

        text += f"{it['order']}. {status} {it['title']}{typ_label}{assigned}\n"
        if it.get("desc"):
            text += f"    {it['desc']}\n"

    kb_rows = []

    nav = []
    if start > 0:
        nav.append(InlineKeyboardButton(text="⬅", callback_data=f"meet_agenda:{mid}:{page-1}"))
    if end < len(items_sorted):
        nav.append(InlineKeyboardButton(text="➡", callback_data=f"meet_agenda:{mid}:{page+1}"))
    if nav:
        kb_rows.append(nav)

    if is_admin:
        kb_rows.append([InlineKeyboardButton(text="➕ Добавить пункт", callback_data=f"agenda_add_for:{mid}")])
        kb_rows.append([InlineKeyboardButton(text="🗑 Удалить пункт", callback_data=f"agenda_del_for:{mid}")])
        kb_rows.append([InlineKeyboardButton(text="🔔 Рассылка повестки", callback_data=f"agenda_notify:{mid}")])


    kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    return text, kb


@dp.message(Command("start"))
async def cmd_start(m: types.Message, state: FSMContext):
    all_users.add(m.from_user.id)
    save_users()
    kb = ADMIN_MAIN_KB if m.from_user.id in ADMIN_IDS else USER_MAIN_KB
    await m.answer("Добро пожаловать!", reply_markup=kb)

@dp.callback_query(F.data=="menu_home")
async def cb_home(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    kb = ADMIN_MAIN_KB if c.from_user.id in ADMIN_IDS else USER_MAIN_KB
    await c.message.answer("Главное меню:", reply_markup=kb)
    await c.message.delete()

@dp.callback_query(F.data == "menu_list")
async def cb_list(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(page=0)
    await show_meetings_page(c.message, c.from_user.id, 0)

@dp.callback_query(F.data.startswith("list_page:"))
async def cb_list_page(c: types.CallbackQuery, state: FSMContext):
    page = int(c.data.split(":")[1])
    await state.update_data(page=page)
    await show_meetings_page(c.message, c.from_user.id, page)

async def show_meetings_page(message: types.Message, user_id: int, page: int):
    if not meetings:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать", callback_data="menu_create")] if user_id in ADMIN_IDS else [],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")],
        ])
        return await message.answer("Нет запланированных совещаний.", reply_markup=kb)

    meets, total_count = get_meetings_page(page)

    start_index = page * MEETINGS_PER_PAGE + 1
    text_lines = []
    for offset, (mid, v) in enumerate(meets, start=start_index):
        ag_count = len(agendas.get(mid, []))
        ag_s = f" 📌 {ag_count} п." if ag_count else ""
        text_lines.append(f"{offset}. {v['datetime']} — {v['title']}{ag_s}")

    text = "📋 Список совещаний:\n\n" + "\n".join(text_lines)
    kb = list_keyboard(page, total_count, user_id in ADMIN_IDS)
    await message.answer(text, reply_markup=kb)
    try:
        await message.delete()
    except Exception:
        pass

@dp.callback_query(F.data=="list_details")
async def cb_list_details(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    if not meetings:
        kb = ADMIN_MAIN_KB if c.from_user.id in ADMIN_IDS else USER_MAIN_KB
        return await c.message.answer("Нет созданных совещаний.", reply_markup=kb)

    text = "Введите номер совещания, чтобы посмотреть детали:\n\n" + "\n".join(
        f"{i}. {v['datetime']} — {v['title']}" for i, v in meetings.items()
    )
    await c.message.answer(text)
    await state.set_state(States.agenda_view_select)

@dp.message(States.agenda_view_select, F.text.regexp(r"^\d+$"))
async def agenda_view_by_number(m: types.Message, state: FSMContext):
    mid = int(m.text)
    if mid not in meetings:
        return await m.answer("❌ Не найдено.")

    meet = meetings[mid]
    desc = meet.get("description", "—")
    header = (
        f"ℹ️ Совещание #{mid}\n"
        f"📅 {meet.get('datetime','')}\n"
        f"📝 {meet.get('title','')}\n"
        f"📄 {desc}\n\n"
    )

    text, kb = build_agenda_text_and_kb(mid, page=0, is_admin=(m.from_user.id in ADMIN_IDS))
    await m.answer(header + text, reply_markup=kb)

    await state.clear()

# — СОЗДАТЬ совещание —
@dp.callback_query(F.data=="menu_create")
async def cb_create(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Нет прав", show_alert=True)

    await c.message.answer("Введите дату и время в формате: дд.мм.гг чч:мм", reply_markup=back_home_kb("menu_home"))
    await state.set_state(States.creating_date)

@dp.message(States.creating_date)
async def create_get_date(m: types.Message, state: FSMContext):
    ok, err, dt_obj = validate_datetime(m.text.strip())
    if not ok:
        return await m.answer(err)

    await state.update_data(datetime=m.text.strip())
    await m.answer("Введите тему совещания:")
    await state.set_state(States.creating_title)


@dp.message(States.creating_title)
async def create_get_title(m: types.Message, state: FSMContext):
    await state.update_data(title=m.text.strip())
    await m.answer("Введите описание совещания (можно пропустить, отправив -):")
    await state.set_state(States.creating_desc)


@dp.message(States.creating_desc)
async def create_get_desc(m: types.Message, state: FSMContext):
    desc = "" if m.text.strip() == "-" else m.text.strip()
    data = await state.get_data()

    idx = max(meetings.keys(), default=0) + 1
    meetings[idx] = {
        "datetime": data["datetime"],
        "title": data["title"],
        "description": desc
    }
    save_meetings()

    note = f"🆕 Создано совещание #{idx}\n📅 {data['datetime']}\n📝 {data['title']}"
    for u in all_users:
        await bot.send_message(u, note)

    await m.answer(
        f"✅ Совещание #{idx} создано.",
        reply_markup=meeting_kb(idx)
    )

    try:
        dt = datetime.strptime(data["datetime"], "%d.%m.%y %H:%M")
        now = datetime.now()
        for h in (24, 1):
            delay = (dt - timedelta(hours=h) - now).total_seconds()
            if delay > 0:
                reminder_text = (
                    f"⌛ Напоминание: через {h}ч совещание #{idx}\n"
                    f"📅 {data['datetime']}\n"
                    f"📝 {data['title']}"
                )
                asyncio.create_task(send_reminder(list(all_users), reminder_text, delay))
    except Exception as e:
        logging.warning(f"Не удалось запланировать напоминания: {e}")

    await state.clear()

@dp.callback_query(F.data.startswith("meet_edit:"))
async def cb_meet_edit(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    mid = int(c.data.split(":",1)[1])
    if mid not in meetings:
        return await c.answer("❌ Не найдено", show_alert=True)
    await c.message.answer(f"Что хотите изменить у совещания #{mid}?", reply_markup=meeting_edit_kb(mid))
    await c.message.answer("Навигация:", reply_markup=back_home_kb("menu_list"))


@dp.callback_query(F.data.startswith("edit_field:"))
async def cb_edit_field(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    _, field, mid = c.data.split(":")
    mid = int(mid)
    if mid not in meetings:
        return await c.answer("❌ Не найдено", show_alert=True)

    await state.update_data(edit_id=mid, edit_field=field)

    if field == "date":
        await c.message.answer("Введите новую дату и время (дд.мм.гг чч:мм):")
        await state.set_state(States.editing_date)
    elif field == "title":
        await c.message.answer("Введите новую тему:")
        await state.set_state(States.editing_title)
    elif field == "desc":
        await c.message.answer("Введите новое описание (или - чтобы оставить пустым):")
        await state.set_state(States.editing_desc)
        
@dp.callback_query(F.data.startswith("meet_del:"))
async def cb_meet_del(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    mid = int(c.data.split(":", 1)[1])
    if mid not in meetings:
        return await c.answer("❌ Не найдено", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить удаление", callback_data=f"meet_del_confirm:{mid}")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu_home")]
    ])
    await c.message.answer(f"Вы уверены, что хотите удалить совещание #{mid}?", reply_markup=kb)

@dp.callback_query(F.data.startswith("meet_del_confirm:"))
async def cb_meet_del_confirm(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    mid = int(c.data.split(":", 1)[1])
    if mid not in meetings:
        return await c.message.answer("❌ Не найдено.")
    meetings.pop(mid)
    reindex_meetings()
    kb = ADMIN_MAIN_KB if c.from_user.id in ADMIN_IDS else USER_MAIN_KB
    await c.message.answer(f"❌ Совещание #{mid} удалено.", reply_markup=kb)
    await state.clear()
    save_meetings()


@dp.callback_query(F.data=="list_edit")
async def cb_list_edit(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    await c.message.answer("Введите номер совещания для изменения:")
    await state.set_state(States.editing_select)

@dp.callback_query(F.data=="list_delete")
async def cb_list_delete(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(States.cancelling)
    await c.message.answer("Введите номер совещания, которое хотите удалить:")

@dp.message(States.editing_select, F.text.regexp(r"^\d+$"))
async def pick_edit(m: types.Message, state: FSMContext):
    mid = int(m.text)
    if mid not in meetings:
        return await m.answer("❌ Не найдено.")

    await state.update_data(edit_id=mid)
    await m.answer(f"Что хотите изменить у совещания #{mid}?", reply_markup=meeting_edit_kb(mid))


@dp.message(States.editing_date)
async def edit_get_date(m: types.Message, state: FSMContext):
    ok, err, dt_obj = validate_datetime(m.text.strip())
    if not ok:
        return await m.answer(err)
    data = await state.get_data()
    mid = data["edit_id"]
    old = meetings[mid].get("datetime")
    meetings[mid]["datetime"] = m.text.strip()
    note = f"🔄 Обновлено время совещания #{mid}\nБыло: {old}\nСтало: {m.text.strip()}\n📝 {meetings[mid].get('title','')}"
    for u in all_users:
        try:
            await bot.send_message(u, note)
        except Exception as e:
            logging.warning(f"Не удалось отправить уведомление {u}: {e}")
    try:
        dt = datetime.strptime(meetings[mid]["datetime"], "%d.%m.%y %H:%M")
        now = datetime.now()
        for h in (24, 1):
            delay = (dt - timedelta(hours=h) - now).total_seconds()
            if delay > 0:
                reminder_text = (
                    f"⌛ Напоминание: через {h}ч совещание #{mid}\n"
                    f"📅 {meetings[mid]['datetime']}\n"
                    f"📝 {meetings[mid]['title']}"
                )
                asyncio.create_task(send_reminder(list(all_users), reminder_text, delay))
    except Exception as e:
        logging.warning(f"Не удалось перепланировать напоминания: {e}")

    await m.answer(f"✅ Дата/время для совещания #{mid} обновлены.", reply_markup=meeting_kb(mid))
    await state.clear()
    save_meetings()

@dp.message(States.editing_title)
async def edit_get_title(m: types.Message, state: FSMContext):
    data = await state.get_data()
    mid = data["edit_id"]
    meetings[mid]["title"] = m.text.strip()
    await m.answer(f"✅ Тема для совещания #{mid} обновлена.", reply_markup=meeting_kb(mid))
    await state.clear()
    save_meetings()

@dp.message(States.editing_desc)
async def edit_get_desc(m: types.Message, state: FSMContext):
    data = await state.get_data()
    mid = data["edit_id"]
    desc = "" if m.text.strip() == "-" else m.text.strip()
    meetings[mid]["description"] = desc
    await m.answer(f"✅ Описание для совещания #{mid} обновлено.", reply_markup=meeting_kb(mid))
    await state.clear()
    save_meetings()

def reindex_meetings():
    global meetings
    meetings = {new_id: meetings[old_id]
                for new_id, old_id in enumerate(sorted(meetings.keys()), start=1)}

@dp.message(States.cancelling, F.text.regexp(r"^\d+$"))
async def apply_cancel(m: types.Message, state: FSMContext):
    mid = int(m.text)
    if mid not in meetings:
        return await m.answer("❌ Совещание с таким номером не найдено.")

    await state.update_data(del_meeting_id=mid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить удаление", callback_data=f"meeting_delete_confirm:{mid}")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu_home")]
    ])
    await m.answer(f"Вы уверены, что хотите удалить совещание #{mid}?", reply_markup=kb)

@dp.callback_query(F.data.startswith("meeting_delete_confirm:"))
async def cb_meeting_delete_confirm(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    mid = int(c.data.split(":")[1])
    if mid not in meetings:
        return await c.message.answer("❌ Совещание не найдено.")

    meetings.pop(mid)
    reindex_meetings()
    save_meetings()

    for u in all_users - {c.from_user.id}:
        try:
            await bot.send_message(u, f"❌ Совещание #{mid} отменено.")
        except Exception as e:
            logging.warning(f"Не удалось уведомить {u}: {e}")

    kb = ADMIN_MAIN_KB if c.from_user.id in ADMIN_IDS else USER_MAIN_KB
    await c.message.answer(f"✅ Совещание #{mid} удалено.", reply_markup=kb)
    await state.clear()

@dp.callback_query(F.data == "menu_agenda")
async def cb_menu_agenda(c: types.CallbackQuery, state: FSMContext):
    """Админ: выбрать совещание, в которое нужно добавить пункт повестки."""
    await c.answer()
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Нет прав", show_alert=True)

    if not meetings:
        return await c.message.answer("Нет созданных совещаний. Сначала создайте совещание.", reply_markup=ADMIN_MAIN_KB)

    kb_rows = []
    for mid, m in meetings.items():
        label = f"{mid}. {m.get('datetime','')} — {m.get('title','')}"
        kb_rows.append([InlineKeyboardButton(text=f"Добавить пункт → {label}", callback_data=f"agenda_add_for:{mid}")])
    kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")])
    await c.message.answer("Выберите совещание, в которое добавить пункт повестки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))


@dp.callback_query(F.data.startswith("meet_agenda:"))
async def cb_meet_agenda(c: types.CallbackQuery, state: FSMContext):
    """Показать повестку совещания (поддерживает meet_agenda:<mid>[:<page>])."""
    await c.answer()
    parts = c.data.split(":")
    try:
        mid = int(parts[1])
    except Exception:
        return await c.message.answer("Неверный ID совещания.")
    page = int(parts[2]) if len(parts) > 2 else 0

    text, kb = build_agenda_text_and_kb(mid, page=page, is_admin=(c.from_user.id in ADMIN_IDS))
    await c.message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("agenda_notify:"))
async def cb_agenda_notify(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    try:
        mid = int(c.data.split(":")[1])
    except Exception:
        return await c.message.answer("Неверный ID совещания.")

    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Нет прав", show_alert=True)

    if mid not in meetings:
        return await c.message.answer("Совещание не найдено.")
    text, _ = build_agenda_text_and_kb(mid, page=0, is_admin=False)

    for u in all_users:
        try:
            await bot.send_message(u, text)
        except Exception as e:
            logging.warning(f"Не удалось отправить повестку {u}: {e}")

    await c.message.answer(f"✅ Повестка совещания #{mid} разослана.", 
                           reply_markup=ADMIN_MAIN_KB)


@dp.callback_query(F.data.startswith("agenda_add_for:"))
async def cb_agenda_add_for(c: types.CallbackQuery, state: FSMContext):
    """Начало: админ нажал 'Добавить пункт' для конкретного совещания."""
    await c.answer()
    try:
        mid = int(c.data.split(":", 1)[1])
    except Exception:
        return await c.message.answer("Неверный ID совещания.")
    logging.info("cb_agenda_add_for: mid=%s by=%s", mid, c.from_user.id)

    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Нет прав", show_alert=True)
    if mid not in meetings:
        return await c.message.answer("Совещание не найдено.")

    await state.update_data(agenda_mid=mid, quick_add=True)

    if agendas.get(mid):
        await c.message.answer(f"Повестка для совещания #{mid} уже существует. Введите заголовок пункта, чтобы добавить его:")
    else:
        agendas.setdefault(mid, [])
        await c.message.answer(f"✅ Повестка для совещания #{mid} создана. Введите заголовок пункта, чтобы добавить его:")

    await state.set_state(States.agenda_title2)

@dp.callback_query(F.data.startswith("agenda_del_for:"))
async def cb_agenda_del_for(c: types.CallbackQuery, state: FSMContext):
    """Удаление пункта повестки"""
    await c.answer()
    try:
        mid = int(c.data.split(":")[1])
    except Exception:
        return await c.message.answer("Неверный ID совещания.")

    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Нет прав", show_alert=True)

    if not agendas.get(mid):
        return await c.message.answer("Повестка пуста.")

    text = "Введите номер пункта для удаления:\n\n" + "\n".join(
        f"{it['order']}. {it['title']}" for it in sorted(agendas[mid], key=lambda x: x['order'])
    )

    await c.message.answer(text)
    await state.update_data(del_agenda_mid=mid)
    await state.set_state(States.agenda_edit_field)    

@dp.message(States.agenda_edit_field, F.text.regexp(r"^\d+$"))
async def agenda_delete_item(m: types.Message, state: FSMContext):
    data = await state.get_data()
    mid = data.get("del_agenda_mid")
    if mid not in agendas:
        await m.answer("Ошибка: повестка не найдена.")
        await state.clear()
        return

    num = int(m.text)
    items = sorted(agendas[mid], key=lambda x: x['order'])
    if num < 1 or num > len(items):
        return await m.answer("❌ Неверный номер.")

    deleted = items[num - 1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Удалить", callback_data=f"agenda_item_del_confirm:{mid}:{deleted['id']}")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="menu_home")]
    ])
    await m.answer(f"Подтвердите удаление пункта №{num}: {deleted['title']}", reply_markup=kb)
    await state.clear()

@dp.callback_query(F.data.startswith("agenda_item_del_confirm:"))
async def cb_agenda_item_del_confirm(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    _, mid_s, item_id_s = c.data.split(":")
    mid, item_id = int(mid_s), int(item_id_s)
    item = find_agenda_item(mid, item_id)
    if not item:
        return await c.message.answer("Пункт не найден.")
    agendas[mid].remove(item)
    normalize_orders(mid)
    text, kb = build_agenda_text_and_kb(mid, page=0, is_admin=True)
    await c.message.answer(f"🗑 Пункт '{item['title']}' удалён.\n\n{text}", reply_markup=kb)
    save_agendas()
    

@dp.message(States.agenda_title2)
async def agenda_title2_handler(m: types.Message, state: FSMContext):
    logging.info("agenda_title2_handler: from=%s text=%r", m.from_user.id, m.text)
    title_text = m.text.strip()
    await state.update_data(agenda_title=title_text)

    data = await state.get_data()
    if data.get("quick_add"):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Обязательный", callback_data="agenda_type:required")],
            [InlineKeyboardButton(text="Дополнительный", callback_data="agenda_type:optional")],
        ])
        await m.answer("Выберите тип пункта (кнопкой):", reply_markup=kb)
        await state.set_state(States.agenda_type2)
        return

    await m.answer("Введите описание пункта повестки (или отправьте '-' чтобы оставить пустым):")
    await state.set_state(States.agenda_desc2)

@dp.message(States.agenda_desc2)
async def agenda_desc2_handler(m: types.Message, state: FSMContext):
    logging.info("agenda_desc2_handler: from=%s text=%r", m.from_user.id, m.text)
    desc = "" if m.text.strip() == "-" else m.text.strip()
    await state.update_data(agenda_desc=desc)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обязательный", callback_data="agenda_type:required")],
        [InlineKeyboardButton(text="Дополнительный", callback_data="agenda_type:optional")],
    ])
    await m.answer("Выберите тип пункта (кнопкой) или введите текстом 'обязательный'/'доп':", reply_markup=kb)
    await state.set_state(States.agenda_type2)


@dp.callback_query(F.data.startswith("agenda_type:"))
async def agenda_set_type(c: types.CallbackQuery, state: FSMContext):
    """
    Обработчик выбора типа через кнопку.
    Поддерживает оба варианта callback_data:
      - "agenda_type:required" (использует mid из state)
      - "agenda_type:<mid>:required" (передаёт mid прямо)
    """
    await c.answer()
    logging.info("agenda_set_type callback from=%s data=%s", c.from_user.id, c.data)
    parts = c.data.split(":")
    if len(parts) == 2:
        typ_code = parts[1]
        data = await state.get_data()
        mid = data.get("agenda_mid")
    elif len(parts) == 3:
        mid = int(parts[1])
        typ_code = parts[2]
    else:
        return await c.message.answer("Неверные данные.")

    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Нет прав", show_alert=True)

    if mid is None or mid not in meetings:
        await c.message.answer("Ошибка: совещание не выбрано или не найдено.")
        await state.clear()
        return

    typ_human = "обязательный" if typ_code == "required" else "доп"
    data = await state.get_data()
    title = data.get("agenda_title", "(без заголовка)")
    desc  = data.get("agenda_desc", "")

    item_id = next_agenda_item_id(mid)
    order = len(agendas.get(mid, [])) + 1
    item = {
        "id": item_id,
        "order": order,
        "title": title,
        "desc": desc,
        "type": typ_human,
        "assigned": None,
        "done": False,
        "created_by": c.from_user.id,
        "created_at": datetime.now().strftime("%d.%m.%y %H:%M")
    }
    agendas.setdefault(mid, []).append(item)
    logging.info("agenda_set_type: added item id=%s to mid=%s", item_id, mid)
    text, kb = build_agenda_text_and_kb(mid, page=0, is_admin=(c.from_user.id in ADMIN_IDS))
    await c.message.answer(text, reply_markup=kb)

    await state.clear()
    save_agendas()


@dp.message(States.agenda_type2)
async def agenda_type2_text_handler(m: types.Message, state: FSMContext):
    """Если пользователь ввёл тип текстом вместо нажатия кнопки."""
    logging.info("agenda_type2_text_handler: from=%s text=%r", m.from_user.id, m.text)
    txt = m.text.strip().lower()
    if txt in ("обязательный", "required", "1"):
        typ_code = "required"
    elif txt in ("доп", "дополнительный", "optional", "2"):
        typ_code = "optional"
    else:
        return await m.answer("Не понял тип. Введите 'обязательный' или 'доп', либо нажмите соответствующую кнопку.")

    data = await state.get_data()
    mid = data.get("agenda_mid")
    if mid is None or mid not in meetings:
        await m.answer("Ошибка: не выбрано совещание.")
        await state.clear()
        return

    typ_human = "обязательный" if typ_code == "required" else "доп"
    title = data.get("agenda_title", "(без заголовка)")
    desc  = data.get("agenda_desc", "")

    item_id = next_agenda_item_id(mid)
    order = len(agendas.get(mid, [])) + 1
    item = {
        "id": item_id,
        "order": order,
        "title": title,
        "desc": desc,
        "type": typ_human,
        "assigned": None,
        "done": False,
        "created_by": m.from_user.id,
        "created_at": datetime.now().strftime("%d.%m.%y %H:%M")
    }
    agendas.setdefault(mid, []).append(item)
    logging.info("agenda_type2_text_handler: added item id=%s to mid=%s", item_id, mid)

    meet = meetings.get(mid, {})
    preview = (
        f"✅ Добавлено в повестку #{mid}\n"
        f"📅 {meet.get('datetime','')}\n📝 {meet.get('title')}\n\n"
        f"• [{typ_human}] {title}" + (f": {desc}" if desc else "")
    )
    await m.answer(preview, reply_markup=ADMIN_MAIN_KB if m.from_user.id in ADMIN_IDS else USER_MAIN_KB)

    text, kb = build_agenda_text_and_kb(mid, page=0, is_admin=(m.from_user.id in ADMIN_IDS))
    await m.answer(text, reply_markup=kb)

    await state.clear()

@dp.callback_query(F.data.startswith("agenda_manage:"))
async def cb_agenda_manage(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    try:
        mid = int(c.data.split(":",1)[1])
    except:
        return await c.message.answer("Неверный ID совещания.")

    items = sorted(agendas.get(mid, []), key=lambda x: x.get("order", 0))
    if not items:
        return await c.message.answer("Повестка пуста.")

    kb_rows = []
    for it in items:
        kb_rows.append([InlineKeyboardButton(text=f"🗑 {it['order']}. {it['title']}", 
                                            callback_data=f"agenda_delete:{mid}:{it['id']}")])
    kb_rows.append([InlineKeyboardButton(text="🏠 Меню", callback_data="menu_home")])
    await c.message.answer("Выберите пункт для удаления/редактирования:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))

@dp.callback_query(F.data=="menu_propose")
async def cb_propose(c: types.CallbackQuery, state: FSMContext):
    await c.answer(); await state.clear()
    if not meetings:
        kb = ADMIN_MAIN_KB if c.from_user.id in ADMIN_IDS else USER_MAIN_KB
        return await c.message.answer("Нет запланированных совещаний.", reply_markup=kb)
    text = "Для какого совещания хотите предложить тему? Введите номер:\n\n" + "\n".join(
        f"{i}. {v['datetime']} — {v['title']}" for i,v in meetings.items()
    )
    await c.message.answer(text)
    await state.set_state(States.propose_select)

# Получили ID совещания
@dp.message(States.propose_select, F.text.regexp(r"^\d+$"))
async def pick_propose(m: types.Message, state: FSMContext):
    mid = int(m.text)
    if mid not in meetings:
        return await m.answer("❌ Не найдено.")
    await state.update_data(propose_mid=mid)
    await m.answer("Напишите текст вашего предложения:")
    await state.set_state(States.propose_text)

@dp.message(States.propose_text)
async def got_propose_text(m: types.Message, state: FSMContext):
    await state.update_data(propose_text=m.text)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Анонимно", callback_data="prop_anon")],
        [InlineKeyboardButton(text="С именем", callback_data="prop_named")],
    ])
    await m.answer("Как вы хотите подписать своё предложение?", reply_markup=keyboard)
    await state.set_state(States.propose_confirm)
  
@dp.callback_query(F.data.in_(["prop_anon","prop_named"]))
async def confirm_propose(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    data = await state.get_data()
    mid = data["propose_mid"]
    text = data["propose_text"]
    anon = (c.data == "prop_anon")
    user = "Аноним" if anon else c.from_user.full_name
    proposals.setdefault(mid, []).append((user, text))
    kb = ADMIN_MAIN_KB if c.from_user.id in ADMIN_IDS else USER_MAIN_KB
    await c.message.answer(f"✅ Предложение для #{mid} принято ({'анонимно' if anon else 'с именем'}).", reply_markup=kb)
    await state.clear()
    save_proposals()


# — ПРОСМОТР предложений —
@dp.callback_query(F.data=="menu_view_props")
async def cb_view_props(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("⛔ Нет прав", show_alert=True)
    text = ""
    for mid,lst in proposals.items():
        text += f"\nСовещание {mid}:\n" + "\n".join(f"{u}: {t}" for u,t in lst)+"\n"
    await c.message.answer(text or "Нет предложений.", reply_markup=ADMIN_MAIN_KB)

@dp.callback_query(F.data=="menu_assign")
async def cb_assign(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    admins_list = "\n".join(f"- {uid}" for uid in sorted(ADMIN_IDS))
    text = f"👥 Текущие администраторы:\n{admins_list or '— нет админов —'}\n\nЧто вы хотите сделать?"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="assign_do:add")],
        [InlineKeyboardButton(text="➖ Удалить", callback_data="assign_do:remove")],
        [InlineKeyboardButton(text="🏠 Меню",    callback_data="menu_home")],
    ])
    await c.message.answer(text, reply_markup=kb)
    await state.set_state(States.assign_action)


@dp.callback_query(F.data.startswith("assign_do:"))
async def cb_assign_do(c: types.CallbackQuery, state: FSMContext):
    await c.answer()
    action = c.data.split(":",1)[1]
    await state.update_data(assign_action=action)
    await c.message.answer("Введите, пожалуйста, Telegram-ID пользователя:")
    await state.set_state(States.assign_id)

@dp.message(States.assign_id, F.text.regexp(r"^\d+$"))
async def cb_assign_apply(m: types.Message, state: FSMContext):
    data = await state.get_data()
    action = data["assign_action"]
    uid = int(m.text)
    if action == "add":
        ADMIN_IDS.add(uid)
        text = f"✅ Пользователь {uid} назначен админом."
        try: await bot.send_message(uid, "🎉 Вам выданы права администратора бота!")
        except: pass
    else:
        ADMIN_IDS.discard(uid)
        text = f"🗑 Админские права пользователя {uid} сняты."
        try: await bot.send_message(uid, "ℹ️ Ваши права администратора бота отозваны.")
        except: pass

    kb = ADMIN_MAIN_KB if m.from_user.id in ADMIN_IDS else USER_MAIN_KB
    await m.answer(text, reply_markup=kb)
    await state.clear()

load_data()
async def main():
    await dp.start_polling(bot)

if __name__=="__main__":
    asyncio.run(main())

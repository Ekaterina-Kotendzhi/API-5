# -*- coding: utf-8 -*-
"""
Telegram-бот «Мини-кошелёк для путешественника».
API: api.exchangerate.host (только он). Данные в SQLite.
"""

import re
import logging
from typing import Optional

import telebot
from telebot import types

from api_client import get_config, convert
from current_api import country_to_currency
import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация загружается из config.env в корне проекта
try:
    config = get_config()
    BOT_TOKEN = config["TELEGRAM_BOT_TOKEN"]
    ACCESS_KEY = config["EXCHANGERATE_ACCESS_KEY"]
except Exception as e:
    logger.error("Конфигурация: %s", e)
    BOT_TOKEN = ""
    ACCESS_KEY = ""

bot = telebot.TeleBot(BOT_TOKEN)

# --- Inline keyboard: главное меню ---
def main_menu_markup():
    return types.InlineKeyboardMarkup(row_width=1).add(
        types.InlineKeyboardButton("Создать новое путешествие", callback_data="menu_newtrip"),
        types.InlineKeyboardButton("Мои путешествия", callback_data="menu_trips"),
        types.InlineKeyboardButton("Баланс", callback_data="menu_balance"),
        types.InlineKeyboardButton("История расходов", callback_data="menu_history"),
        types.InlineKeyboardButton("Изменить курс", callback_data="menu_setrate"),
        types.InlineKeyboardButton("Удалить путешествие", callback_data="menu_deletetrip"),
    )


def trips_list_markup(trips: list, prefix: str = "switch_"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for t in trips:
        kb.add(types.InlineKeyboardButton(
            f"{t['name']} ({t['dest_currency']})",
            callback_data=f"{prefix}{t['id']}",
        ))
    kb.add(types.InlineKeyboardButton("← Назад", callback_data="menu_main"))
    return kb


def trip_confirm_delete_markup(trip_id: int):
    """Кнопки подтверждения удаления поездки."""
    return types.InlineKeyboardMarkup(row_width=2).add(
        types.InlineKeyboardButton("Удалить", callback_data=f"del_confirm_{trip_id}"),
        types.InlineKeyboardButton("Отмена", callback_data="del_cancel"),
    )


def back_to_main_markup():
    return types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("← В главное меню", callback_data="menu_main"),
    )


# --- Кнопки быстрого доступа (постоянное меню под полем ввода) ---
MENU_BTN_NEWTRIP = "Создать путешествие"
MENU_BTN_TRIPS = "Мои путешествия"
MENU_BTN_BALANCE = "Баланс"
MENU_BTN_HISTORY = "История расходов"
MENU_BTN_SETRATE = "Изменить курс"
MENU_BTN_DELETETRIP = "Удалить путешествие"


def reply_keyboard_menu():
    """Клавиатура меню для быстрого доступа (всегда видна под полем ввода)."""
    return types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=False).add(
        types.KeyboardButton(MENU_BTN_NEWTRIP),
        types.KeyboardButton(MENU_BTN_TRIPS),
    ).add(
        types.KeyboardButton(MENU_BTN_BALANCE),
        types.KeyboardButton(MENU_BTN_HISTORY),
    ).add(
        types.KeyboardButton(MENU_BTN_SETRATE),
        types.KeyboardButton(MENU_BTN_DELETETRIP),
    )


def yes_no_markup(amount_dest: float, amount_home: float, dest_cur: str, home_cur: str):
    # callback: expense_yes_<trip_id>_<amount_dest>_<amount_home>
    return types.InlineKeyboardMarkup(row_width=2).add(
        types.InlineKeyboardButton("✅ Да", callback_data=f"expense_yes_{amount_dest}_{amount_home}"),
        types.InlineKeyboardButton("❌ Нет", callback_data="expense_no"),
    )


# --- Текст главного меню ---
def main_menu_text():
    return (
        "Главное меню. Выберите действие:\n\n"
        "• Создать новое путешествие — добавить поездку с валютной парой и курсом.\n"
        "• Мои путешествия — переключиться между поездками.\n"
        "• Баланс — посмотреть остаток по активному путешествию.\n"
        "• История расходов — список трат.\n"
        "• Изменить курс — задать курс вручную для выбранной поездки.\n"
        "• Удалить путешествие — удалить поездку и все её расходы."
    )


def send_main_menu(chat_id: int, text: Optional[str] = None):
    bot.send_message(
        chat_id,
        text or main_menu_text(),
        reply_markup=main_menu_markup(),
    )


# --- Обработка числа как расхода ---
def is_number_message(text: str) -> bool:
    if not text or not text.strip():
        return False
    # Допускаем число с точкой/запятой и опционально пробелами
    s = text.strip().replace(",", ".")
    return bool(re.match(r"^-?\d+\.?\d*$", s))


def parse_amount(text: str) -> float:
    s = text.strip().replace(",", ".")
    return float(s)


# --- Создание путешествия (FSM) ---
def start_new_trip_flow(chat_id: int, user_id: int):
    db.set_user_state(user_id, "newtrip_country_from", None)
    bot.send_message(
        chat_id,
        "Введите страну отправления (домашнюю валюту), например: Россия, США, Китай.",
    )


def handle_newtrip_country_from(message, user_id: int):
    country = message.text.strip()
    cur = country_to_currency(country)
    if not cur:
        bot.send_message(
            message.chat.id,
            "Не удалось определить валюту по этой стране. Введите страну ещё раз или код валюты (например RUB, USD).",
        )
        return
    db.set_user_state(user_id, "newtrip_country_to", cur)
    bot.send_message(
        message.chat.id,
        f"Валюта отправления: {cur}. Теперь введите страну назначения (валюту поездки), например: Китай, Таиланд.",
    )


def handle_newtrip_country_to(message, user_id: int, state_data: str):
    home_currency = state_data
    country = message.text.strip()
    cur = country_to_currency(country)
    if not cur:
        # Попробуем как код валюты (3 буквы)
        if len(country) == 3 and country.isalpha():
            cur = country.upper()
        else:
            bot.send_message(
                message.chat.id,
                "Не удалось определить валюту. Введите страну или код валюты (3 буквы).",
            )
            return
    if cur == home_currency:
        bot.send_message(message.chat.id, "Валюта назначения должна отличаться от домашней. Введите другую страну.")
        return
    # API не вызываем здесь — только по кнопке «Получить из API». Так не превышаем лимит и не показываем ошибку.
    db.set_user_state(user_id, "newtrip_choose_rate_source", f"{home_currency}|{cur}|{country}")
    bot.send_message(
        message.chat.id,
        f"Пара валют: {home_currency} → {cur}. Как задать курс?\n\n"
        "Рекомендуем «Ввести вручную» — так не будет ошибок лимита API.",
        reply_markup=types.InlineKeyboardMarkup(row_width=1).add(
            types.InlineKeyboardButton("Ввести курс вручную (по обменнику)", callback_data="newtrip_manual_rate_now"),
            types.InlineKeyboardButton("Получить текущий курс из API", callback_data="newtrip_fetch_rate"),
        ),
    )


def handle_newtrip_initial_sum(message, user_id: int, state_data: str):
    parts = state_data.split("|", 3)
    if len(parts) < 4:
        db.clear_state(user_id)
        send_main_menu(message.chat.id, "Что-то пошло не так. Начните создание поездки заново.")
        return
    home_cur, dest_cur, rate_str = parts[0], parts[1], float(parts[2])
    name = parts[3] if len(parts) > 3 else dest_cur
    if not is_number_message(message.text) or parse_amount(message.text) <= 0:
        bot.send_message(message.chat.id, "Введите положительное число — сумму в валюте отправления (домашней).")
        return
    amount_home = parse_amount(message.text)
    # Курс: домашняя за 1 валюту поездки. Конвертируем: сумма в поездке = домашняя / курс
    amount_dest = amount_home / float(rate_str)
    trip_id = db.create_trip(user_id, name, home_cur, dest_cur, float(rate_str), amount_home, amount_dest)
    db.clear_state(user_id)
    trip = db.get_trip(trip_id, user_id)
    bot.send_message(
        message.chat.id,
        f"Путешествие «{name}» создано.\n{db.format_balance(trip)}\n\nТеперь можно вводить суммы расходов в {dest_cur} — бот будет пересчитывать в {home_cur} и предлагать учесть трату.",
        reply_markup=main_menu_markup(),
    )


# --- Callback: меню и действия ---
@bot.callback_query_handler(func=lambda c: c.data == "menu_main")
def cb_menu_main(c):
    bot.answer_callback_query(c.id)
    send_main_menu(c.message.chat.id)

    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except Exception:
        pass


@bot.callback_query_handler(func=lambda c: c.data == "menu_newtrip")
def cb_menu_newtrip(c):
    bot.answer_callback_query(c.id)
    start_new_trip_flow(c.message.chat.id, c.from_user.id)


@bot.callback_query_handler(func=lambda c: c.data == "menu_trips")
def cb_menu_trips(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    trips = db.get_user_trips(user_id)
    if not trips:
        bot.send_message(
            c.message.chat.id,
            "У вас пока нет путешествий. Создайте первое — кнопка «Создать новое путешествие».",
            reply_markup=back_to_main_markup(),
        )
        return
    lines = ["Выберите путешествие для переключения:"]
    active_id = db.get_active_trip_id(user_id)
    for t in trips:
        mark = " ✓" if t["id"] == active_id else ""
        lines.append(f"• {t['name']} ({t['dest_currency']}){mark}")
    bot.send_message(
        c.message.chat.id,
        "\n".join(lines),
        reply_markup=trips_list_markup(trips, "switch_"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("switch_"))
def cb_switch_trip(c):
    bot.answer_callback_query(c.id)
    try:
        trip_id = int(c.data.replace("switch_", ""))
    except ValueError:
        return
    user_id = c.from_user.id
    trip = db.get_trip(trip_id, user_id)
    if not trip:
        bot.send_message(c.message.chat.id, "Путешествие не найдено.", reply_markup=back_to_main_markup())
        return
    db.set_active_trip(user_id, trip_id)
    bot.send_message(
        c.message.chat.id,
        f"Активное путешествие: «{trip['name']}» ({trip['dest_currency']}).\n{db.format_balance(trip)}",
        reply_markup=back_to_main_markup(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "menu_balance")
def cb_menu_balance(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    trip_id = db.get_active_trip_id(user_id)
    if not trip_id:
        bot.send_message(
            c.message.chat.id,
            "Нет активного путешествия. Выберите или создайте поездку в разделе «Мои путешествия».",
            reply_markup=back_to_main_markup(),
        )
        return
    trip = db.get_trip(trip_id, user_id)
    if not trip:
        bot.send_message(c.message.chat.id, "Путешествие не найдено.", reply_markup=back_to_main_markup())
        return
    bot.send_message(
        c.message.chat.id,
        f"«{trip['name']}»\n{db.format_balance(trip)}",
        reply_markup=back_to_main_markup(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "menu_history")
def cb_menu_history(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    trip_id = db.get_active_trip_id(user_id)
    if not trip_id:
        bot.send_message(
            c.message.chat.id,
            "Нет активного путешествия. Выберите поездку в «Мои путешествия».",
            reply_markup=back_to_main_markup(),
        )
        return
    trip = db.get_trip(trip_id, user_id)
    if not trip:
        bot.send_message(c.message.chat.id, "Путешествие не найдено.", reply_markup=back_to_main_markup())
        return
    expenses = db.get_expenses(trip_id, user_id)
    if not expenses:
        bot.send_message(
            c.message.chat.id,
            f"По путешествию «{trip['name']}» расходов пока нет.",
            reply_markup=back_to_main_markup(),
        )
        return
    lines = [f"История расходов: «{trip['name']}»", ""]
    for e in expenses:
        lines.append(db.format_expense_line(e, trip["dest_currency"], trip["home_currency"]))
    bot.send_message(
        c.message.chat.id,
        "\n".join(lines),
        reply_markup=back_to_main_markup(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "menu_setrate")
def cb_menu_setrate(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    trips = db.get_user_trips(user_id)
    if not trips:
        bot.send_message(
            c.message.chat.id,
            "Нет путешествий. Сначала создайте поездку.",
            reply_markup=back_to_main_markup(),
        )
        return
    bot.send_message(
        c.message.chat.id,
        "Выберите путешествие, для которого изменить курс:",
        reply_markup=trips_list_markup(trips, "setrate_"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("setrate_"))
def cb_setrate_choose(c):
    bot.answer_callback_query(c.id)
    try:
        trip_id = int(c.data.replace("setrate_", ""))
    except ValueError:
        return
    user_id = c.from_user.id
    trip = db.get_trip(trip_id, user_id)
    if not trip:
        bot.send_message(c.message.chat.id, "Путешествие не найдено.", reply_markup=back_to_main_markup())
        return
    db.set_user_state(user_id, "setrate_trip", str(trip_id))
    bot.send_message(
        c.message.chat.id,
        f"Текущий курс для «{trip['name']}»: 1 {trip['dest_currency']} = {trip['rate']} {trip['home_currency']}. Введите новый курс: сколько {trip['home_currency']} за 1 {trip['dest_currency']} (например 12.5):",
        reply_markup=back_to_main_markup(),
    )


@bot.callback_query_handler(func=lambda c: c.data == "menu_deletetrip")
def cb_menu_deletetrip(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    trips = db.get_user_trips(user_id)
    if not trips:
        bot.send_message(
            c.message.chat.id,
            "Нет путешествий для удаления.",
            reply_markup=back_to_main_markup(),
        )
        return
    bot.send_message(
        c.message.chat.id,
        "Выберите путешествие для удаления (вместе с ним удалятся все расходы):",
        reply_markup=trips_list_markup(trips, "del_"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("del_confirm_"))
def cb_del_confirm(c):
    bot.answer_callback_query(c.id)
    try:
        trip_id = int(c.data.replace("del_confirm_", ""))
    except ValueError:
        return
    user_id = c.from_user.id
    if not db.delete_trip(trip_id, user_id):
        bot.send_message(c.message.chat.id, "Путешествие не найдено или уже удалено.", reply_markup=back_to_main_markup())
        return
    bot.send_message(c.message.chat.id, "Путешествие удалено.", reply_markup=back_to_main_markup())


@bot.callback_query_handler(func=lambda c: c.data == "del_cancel")
def cb_del_cancel(c):
    bot.answer_callback_query(c.id)
    send_main_menu(c.message.chat.id, "Удаление отменено.")


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("del_") and c.data != "del_cancel" and not c.data.startswith("del_confirm_"))
def cb_del_choose(c):
    """Показать подтверждение удаления выбранной поездки."""
    bot.answer_callback_query(c.id)
    try:
        trip_id = int(c.data.replace("del_", ""))
    except ValueError:
        return
    user_id = c.from_user.id
    trip = db.get_trip(trip_id, user_id)
    if not trip:
        bot.send_message(c.message.chat.id, "Путешествие не найдено.", reply_markup=back_to_main_markup())
        return
    bot.send_message(
        c.message.chat.id,
        f"Удалить путешествие «{trip['name']}» ({trip['dest_currency']})? Все расходы по этой поездке будут удалены.",
        reply_markup=trip_confirm_delete_markup(trip_id),
    )


@bot.callback_query_handler(func=lambda c: c.data == "newtrip_fetch_rate")
def cb_newtrip_fetch_rate(c):
    """Получить курс из API по нажатию кнопки (не при вводе страны — экономия лимита)."""
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    st = db.get_user_state(user_id)
    if not st or st["state"] != "newtrip_choose_rate_source" or not st["state_data"]:
        bot.send_message(c.message.chat.id, "Сессия устарела. Начните создание поездки заново.", reply_markup=main_menu_markup())
        return
    parts = st["state_data"].split("|", 2)
    if len(parts) < 2:
        db.clear_state(user_id)
        send_main_menu(c.message.chat.id)
        return
    home_currency, cur, country = parts[0], parts[1], (parts[2].strip() if len(parts) > 2 else cur)
    result = convert(ACCESS_KEY, home_currency, cur, 1.0)
    if not result.get("success"):
        db.set_user_state(user_id, "newtrip_manual_rate", f"{home_currency}|{cur}|{country}")
        # Никогда не показываем сырой текст ошибки API пользователю — только короткое сообщение.
        info = (result.get("info") or "").lower()
        is_limit = any(w in info for w in ("limit", "rate", "exceeded", "limitation", "maximum"))
        msg = "Превышен лимит запросов к сервису курсов. Введите курс вручную (например по обменнику)." if is_limit else "Сервис курсов временно недоступен. Введите курс вручную."
        bot.send_message(
            c.message.chat.id,
            f"{msg}\n\nСколько {cur} за 1 {home_currency}? (одно число)",
        )
        return
    api_dest_per_home = result["result"]
    rate = 1.0 / api_dest_per_home
    db.set_user_state(user_id, "newtrip_confirm_rate", f"{home_currency}|{cur}|{rate}|{country}")
    bot.send_message(
        c.message.chat.id,
        f"Текущий курс: 1 {cur} = {rate:.4f} {home_currency} (1 {home_currency} = {api_dest_per_home:.4f} {cur}).\n\nВас устраивает?",
        reply_markup=types.InlineKeyboardMarkup(row_width=2).add(
            types.InlineKeyboardButton("Да", callback_data="newtrip_rate_ok"),
            types.InlineKeyboardButton("Нет, ввести вручную", callback_data="newtrip_rate_manual"),
        ),
    )


@bot.callback_query_handler(func=lambda c: c.data == "newtrip_manual_rate_now")
def cb_newtrip_manual_rate_now(c):
    """Переход к ручному вводу курса без вызова API."""
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    st = db.get_user_state(user_id)
    if not st or st["state"] != "newtrip_choose_rate_source" or not st["state_data"]:
        bot.send_message(c.message.chat.id, "Сессия устарела. Начните заново.", reply_markup=main_menu_markup())
        return
    parts = st["state_data"].split("|", 2)
    if len(parts) < 2:
        db.clear_state(user_id)
        send_main_menu(c.message.chat.id)
        return
    home_cur, dest_cur = parts[0], parts[1]
    country = parts[2].strip() if len(parts) > 2 else dest_cur
    db.set_user_state(user_id, "newtrip_manual_rate", f"{home_cur}|{dest_cur}|{country}")
    bot.send_message(
        c.message.chat.id,
        f"Введите курс: сколько {dest_cur} за 1 {home_cur}? (одно число, например 12.8)",
    )


@bot.callback_query_handler(func=lambda c: c.data == "newtrip_rate_ok")
def cb_newtrip_rate_ok(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    st = db.get_user_state(user_id)
    if not st or st["state"] != "newtrip_confirm_rate" or not st["state_data"]:
        bot.send_message(c.message.chat.id, "Сессия устарела. Начните создание поездки заново.", reply_markup=main_menu_markup())
        return
    parts = st["state_data"].split("|", 3)
    if len(parts) < 3:
        db.clear_state(user_id)
        send_main_menu(c.message.chat.id, "Ошибка данных. Создайте поездку заново.")
        return
    home_cur, dest_cur, rate = parts[0], parts[1], parts[2]
    name = parts[3].strip() if len(parts) > 3 and parts[3] else dest_cur
    db.set_user_state(user_id, "newtrip_initial_sum", f"{home_cur}|{dest_cur}|{rate}|{name}")
    bot.send_message(
        c.message.chat.id,
        f"Курс принят. Введите начальную сумму в домашней валюте ({home_cur}) — она будет конвертирована в {dest_cur} и станет стартовым балансом.",
    )


@bot.callback_query_handler(func=lambda c: c.data == "newtrip_rate_manual")
def cb_newtrip_rate_manual(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    st = db.get_user_state(user_id)
    if not st or st["state"] != "newtrip_confirm_rate" or not st["state_data"]:
        bot.send_message(c.message.chat.id, "Сессия устарела. Начните заново.", reply_markup=main_menu_markup())
        return
    parts = st["state_data"].split("|", 3)
    if len(parts) < 2:
        db.clear_state(user_id)
        send_main_menu(c.message.chat.id)
        return
    home_cur, dest_cur = parts[0], parts[1]
    name = parts[3].strip() if len(parts) > 3 and parts[3] else dest_cur
    db.set_user_state(user_id, "newtrip_manual_rate", f"{home_cur}|{dest_cur}|{name}")
    bot.send_message(
        c.message.chat.id,
        f"Введите курс вручную: сколько {dest_cur} за 1 {home_cur}? (одно число, например 12.8)",
    )


@bot.callback_query_handler(func=lambda c: c.data == "expense_no")
def cb_expense_no(c):
    bot.answer_callback_query(c.id)
    bot.send_message(c.message.chat.id, "Расход не учтён.", reply_markup=back_to_main_markup())


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("expense_yes_"))
def cb_expense_yes(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    # expense_yes_<amount_dest>_<amount_home> — trip_id нужен из активного
    trip_id = db.get_active_trip_id(user_id)
    if not trip_id:
        bot.send_message(c.message.chat.id, "Нет активного путешествия.")
        return
    parts = c.data.replace("expense_yes_", "").split("_")
    if len(parts) < 2:
        bot.send_message(c.message.chat.id, "Ошибка данных.")
        return
    try:
        amount_dest = float(parts[0])
        amount_home = float(parts[1])
    except ValueError:
        bot.send_message(c.message.chat.id, "Неверный формат суммы.")
        return
    ok = db.add_expense(trip_id, user_id, amount_dest, amount_home)
    if not ok:
        bot.send_message(
            c.message.chat.id,
            "Не удалось учесть расход (возможно, недостаточно средств на балансе).",
            reply_markup=back_to_main_markup(),
        )
        return
    trip = db.get_trip(trip_id, user_id)
    bot.send_message(
        c.message.chat.id,
        f"Расход учтён. {db.format_balance(trip)}",
        reply_markup=back_to_main_markup(),
    )


# --- Yes/No для расхода: нужно передать trip_id в callback (т.к. пользователь может переключить поездку)
# Переделаем: в callback храним trip_id, amount_dest, amount_home (через разделитель, без подчёркивания в числах)
def expense_confirm_markup(trip_id: int, amount_dest: float, amount_home: float):
    # Используем букву e и целые числа * 100 чтобы избежать точки
    a_d = int(round(amount_dest * 100))
    a_h = int(round(amount_home * 100))
    return types.InlineKeyboardMarkup(row_width=2).add(
        types.InlineKeyboardButton("✅ Да", callback_data=f"ex_{trip_id}_{a_d}_{a_h}"),
        types.InlineKeyboardButton("❌ Нет", callback_data="expense_no"),
    )


@bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("ex_"))
def cb_expense_confirm(c):
    bot.answer_callback_query(c.id)
    user_id = c.from_user.id
    parts = c.data.split("_")
    if len(parts) != 4:
        bot.send_message(c.message.chat.id, "Ошибка данных.")
        return
    try:
        trip_id = int(parts[1])
        amount_dest = int(parts[2]) / 100.0
        amount_home = int(parts[3]) / 100.0
    except (ValueError, IndexError):
        bot.send_message(c.message.chat.id, "Неверный формат.")
        return
    ok = db.add_expense(trip_id, user_id, amount_dest, amount_home)
    if not ok:
        bot.send_message(
            c.message.chat.id,
            "Не удалось учесть расход (недостаточно средств или неверная поездка).",
            reply_markup=back_to_main_markup(),
        )
        return
    trip = db.get_trip(trip_id, user_id)
    bot.send_message(
        c.message.chat.id,
        f"Расход учтён. {db.format_balance(trip)}",
        reply_markup=back_to_main_markup(),
    )


# Исправление: при показе "Учесть как расход?" использовать ex_ с trip_id
# (уже добавлен expense_confirm_markup и cb_expense_confirm)


# --- Сообщения и команды ---
@bot.message_handler(commands=["start"])
def cmd_start(message):
    db.ensure_user(message.from_user.id)
    db.clear_state(message.from_user.id)
    bot.send_message(
        message.chat.id,
        "Привет! Это мини-кошелёк для путешественника. Можно создать путешествие (страна отправления → страна назначения), "
        "задать курс и начальный баланс, а затем записывать расходы в валюте поездки.\n\n"
        "Используйте кнопки меню ниже для быстрого доступа или выберите действие:",
        reply_markup=reply_keyboard_menu(),
    )
    bot.send_message(
        message.chat.id,
        main_menu_text(),
        reply_markup=main_menu_markup(),
    )


@bot.message_handler(commands=["newtrip"])
def cmd_newtrip(message):
    start_new_trip_flow(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["switch"])
def cmd_switch(message):
    user_id = message.from_user.id
    trips = db.get_user_trips(user_id)
    if not trips:
        bot.send_message(message.chat.id, "У вас пока нет путешествий. Создайте: /newtrip или кнопка «Создать новое путешествие».", reply_markup=main_menu_markup())
        return
    bot.send_message(
        message.chat.id,
        "Выберите путешествие:",
        reply_markup=trips_list_markup(trips, "switch_"),
    )


@bot.message_handler(commands=["balance"])
def cmd_balance(message):
    user_id = message.from_user.id
    trip_id = db.get_active_trip_id(user_id)
    if not trip_id:
        bot.send_message(message.chat.id, "Нет активного путешествия. /switch — выбрать поездку.", reply_markup=main_menu_markup())
        return
    trip = db.get_trip(trip_id, user_id)
    if not trip:
        bot.send_message(message.chat.id, "Путешествие не найдено.")
        return
    bot.send_message(message.chat.id, f"«{trip['name']}»\n{db.format_balance(trip)}", reply_markup=back_to_main_markup())


@bot.message_handler(commands=["history"])
def cmd_history(message):
    user_id = message.from_user.id
    trip_id = db.get_active_trip_id(user_id)
    if not trip_id:
        bot.send_message(message.chat.id, "Нет активного путешествия. /switch — выбрать поездку.", reply_markup=main_menu_markup())
        return
    trip = db.get_trip(trip_id, user_id)
    if not trip:
        bot.send_message(message.chat.id, "Путешествие не найдено.")
        return
    expenses = db.get_expenses(trip_id, user_id)
    if not expenses:
        bot.send_message(message.chat.id, f"По «{trip['name']}» расходов пока нет.", reply_markup=back_to_main_markup())
        return
    lines = [f"«{trip['name']}»", ""] + [db.format_expense_line(e, trip["dest_currency"], trip["home_currency"]) for e in expenses]
    bot.send_message(message.chat.id, "\n".join(lines), reply_markup=back_to_main_markup())


@bot.message_handler(commands=["setrate"])
def cmd_setrate(message):
    user_id = message.from_user.id
    trips = db.get_user_trips(user_id)
    if not trips:
        bot.send_message(message.chat.id, "Нет путешествий. /newtrip — создать.", reply_markup=main_menu_markup())
        return
    bot.send_message(
        message.chat.id,
        "Выберите путешествие для смены курса:",
        reply_markup=trips_list_markup(trips, "setrate_"),
    )


@bot.message_handler(commands=["deletetrip"])
def cmd_deletetrip(message):
    user_id = message.from_user.id
    trips = db.get_user_trips(user_id)
    if not trips:
        bot.send_message(message.chat.id, "Нет путешествий для удаления.", reply_markup=main_menu_markup())
        return
    bot.send_message(
        message.chat.id,
        "Выберите путешествие для удаления (вместе с ним удалятся все расходы):",
        reply_markup=trips_list_markup(trips, "del_"),
    )


@bot.message_handler(func=lambda m: True)
def handle_message(message):
    user_id = message.from_user.id
    chat_id = message.chat.id
    st = db.get_user_state(user_id)
    state = st["state"] if st else ""
    state_data = (st["state_data"] or "") if st else ""

    # FSM: создание поездки
    if state == "newtrip_country_from":
        handle_newtrip_country_from(message, user_id)
        return
    if state == "newtrip_country_to":
        handle_newtrip_country_to(message, user_id, state_data)
        return
    if state == "newtrip_initial_sum":
        handle_newtrip_initial_sum(message, user_id, state_data)
        return
    if state == "newtrip_manual_rate":
        # Ввод курса вручную: сколько валюты назначения за 1 домашнюю -> храним (домашняя за 1 назначения) = 1/ввод
        if not is_number_message(message.text) or parse_amount(message.text) <= 0:
            bot.send_message(chat_id, "Введите положительное число — курс (сколько валюты назначения за 1 единицу домашней).")
            return
        manual_dest_per_home = parse_amount(message.text)
        rate = 1.0 / manual_dest_per_home
        parts = state_data.split("|", 2)
        if len(parts) < 2:
            db.clear_state(user_id)
            send_main_menu(chat_id, "Ошибка. Начните создание заново.")
            return
        home_cur, dest_cur = parts[0], parts[1]
        name = parts[2].strip() if len(parts) > 2 and parts[2] else dest_cur
        db.set_user_state(user_id, "newtrip_initial_sum", f"{home_cur}|{dest_cur}|{rate}|{name}")
        bot.send_message(chat_id, f"Курс принят: 1 {home_cur} = {rate} {dest_cur}. Введите начальную сумму в {home_cur}:")
        return
    if state == "setrate_trip":
        if not is_number_message(message.text) or parse_amount(message.text) <= 0:
            bot.send_message(chat_id, "Введите положительное число — новый курс.")
            return
        try:
            trip_id = int(state_data)
        except ValueError:
            db.clear_state(user_id)
            send_main_menu(chat_id)
            return
        trip = db.get_trip(trip_id, user_id)
        if not trip:
            db.clear_state(user_id)
            bot.send_message(chat_id, "Путешествие не найдено.", reply_markup=main_menu_markup())
            return
        new_rate = parse_amount(message.text)
        db.update_trip_rate(trip_id, user_id, new_rate)
        db.clear_state(user_id)
        trip = db.get_trip(trip_id, user_id)
        bot.send_message(chat_id, f"Курс обновлён: 1 {trip['home_currency']} = {trip['rate']} {trip['dest_currency']}.", reply_markup=main_menu_markup())
        return

    # Нажатие кнопок быстрого меню (reply keyboard)
    text = (message.text or "").strip()
    if text == MENU_BTN_NEWTRIP:
        start_new_trip_flow(chat_id, user_id)
        return
    if text == MENU_BTN_TRIPS:
        trips = db.get_user_trips(user_id)
        if not trips:
            bot.send_message(chat_id, "У вас пока нет путешествий. Создайте первое — кнопка «Создать путешествие».", reply_markup=back_to_main_markup())
        else:
            lines = ["Выберите путешествие:"]
            active_id = db.get_active_trip_id(user_id)
            for t in trips:
                mark = " ✓" if t["id"] == active_id else ""
                lines.append(f"• {t['name']} ({t['dest_currency']}){mark}")
            bot.send_message(chat_id, "\n".join(lines), reply_markup=trips_list_markup(trips, "switch_"))
        return
    if text == MENU_BTN_BALANCE:
        trip_id = db.get_active_trip_id(user_id)
        if not trip_id:
            bot.send_message(chat_id, "Нет активного путешествия. Выберите или создайте в «Мои путешествия».", reply_markup=back_to_main_markup())
        else:
            trip = db.get_trip(trip_id, user_id)
            if trip:
                bot.send_message(chat_id, f"«{trip['name']}»\n{db.format_balance(trip)}", reply_markup=back_to_main_markup())
            else:
                bot.send_message(chat_id, "Путешествие не найдено.", reply_markup=back_to_main_markup())
        return
    if text == MENU_BTN_HISTORY:
        trip_id = db.get_active_trip_id(user_id)
        if not trip_id:
            bot.send_message(chat_id, "Нет активного путешествия. Выберите поездку в «Мои путешествия».", reply_markup=back_to_main_markup())
        else:
            trip = db.get_trip(trip_id, user_id)
            if not trip:
                bot.send_message(chat_id, "Путешествие не найдено.", reply_markup=back_to_main_markup())
            else:
                expenses = db.get_expenses(trip_id, user_id)
                if not expenses:
                    bot.send_message(chat_id, f"По «{trip['name']}» расходов пока нет.", reply_markup=back_to_main_markup())
                else:
                    lines = [f"История: «{trip['name']}»", ""] + [db.format_expense_line(e, trip["dest_currency"], trip["home_currency"]) for e in expenses]
                    bot.send_message(chat_id, "\n".join(lines), reply_markup=back_to_main_markup())
        return
    if text == MENU_BTN_SETRATE:
        trips = db.get_user_trips(user_id)
        if not trips:
            bot.send_message(chat_id, "Нет путешествий. Создайте поездку в «Создать путешествие».", reply_markup=back_to_main_markup())
        else:
            bot.send_message(chat_id, "Выберите путешествие для смены курса:", reply_markup=trips_list_markup(trips, "setrate_"))
        return
    if text == MENU_BTN_DELETETRIP:
        trips = db.get_user_trips(user_id)
        if not trips:
            bot.send_message(chat_id, "Нет путешествий для удаления.", reply_markup=back_to_main_markup())
        else:
            bot.send_message(
                chat_id,
                "Выберите путешествие для удаления (вместе с ним удалятся все расходы):",
                reply_markup=trips_list_markup(trips, "del_"),
            )
        return

    # Число без состояния = расход в валюте активного путешествия
    if is_number_message(message.text):
        trip_id = db.get_active_trip_id(user_id)
        if not trip_id:
            bot.send_message(
                chat_id,
                "Сначала выберите или создайте путешествие (меню «Мои путешествия» или /switch).",
                reply_markup=main_menu_markup(),
            )
            return
        trip = db.get_trip(trip_id, user_id)
        if not trip:
            bot.send_message(chat_id, "Путешествие не найдено.", reply_markup=main_menu_markup())
            return
        amount_dest = parse_amount(message.text)
        if amount_dest <= 0:
            bot.send_message(chat_id, "Введите положительную сумму расхода.")
            return
        # Курс хранится как "домашняя за 1 валюту поездки". amount_home = amount_dest * rate
        amount_home = amount_dest * trip["rate"]
        bot.send_message(
            chat_id,
            f"{amount_dest:.2f} {trip['dest_currency']} = {amount_home:.2f} {trip['home_currency']}. Учесть как расход?",
            reply_markup=expense_confirm_markup(trip_id, amount_dest, amount_home),
        )
        return

    # Любое другое сообщение — подсказка
    send_main_menu(chat_id, "Не понял. Введите число для записи расхода по активному путешествию или выберите действие в меню.")


def main():
    db.init_db()
    logger.info("Бот запущен")
    bot.infinity_polling()


if __name__ == "__main__":
    main()

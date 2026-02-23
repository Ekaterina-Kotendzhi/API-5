# -*- coding: utf-8 -*-
"""
Клиент для api.exchangerate.host.
Все запросы идут на http://api.exchangerate.host (convert и list).
Курсы кэшируются на 5 минут, чтобы не превышать лимит запросов API.
"""

import time
import requests

BASE_URL = "http://api.exchangerate.host"

# Кэш курсов: ключ (from_cur, to_cur) -> (результат, время). TTL = 300 сек.
_convert_cache: dict = {}
_CONVERT_CACHE_TTL = 300


def get_config():
    """Читает переменные из config.env в корне проекта (EXCHANGERATE_ACCESS_KEY, TELEGRAM_BOT_TOKEN)."""
    import os
    # Ищем config.env: рядом с этим файлом (корень проекта) или в текущей рабочей папке
    project_dir = os.path.dirname(os.path.abspath(__file__))
    cwd = os.getcwd()
    for base in (project_dir, cwd):
        config_path = os.path.join(base, "config.env")
        if os.path.isfile(config_path):
            break
    else:
        config_path = os.path.join(project_dir, "config.env")
        raise FileNotFoundError(
            "Файл config.env не найден. Создайте его в корне проекта (рядом с bot.py) по образцу config.example.env "
            "и укажите EXCHANGERATE_ACCESS_KEY и TELEGRAM_BOT_TOKEN."
        )
    env = {}
    with open(config_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    access_key = env.get("EXCHANGERATE_ACCESS_KEY")
    if not access_key:
        raise ValueError("В config.env не указан EXCHANGERATE_ACCESS_KEY.")
    return env


def convert(access_key: str, from_currency: str, to_currency: str, amount: float):
    """
    Конвертация суммы через endpoint /convert.
    Результат для пары (from, to) кэшируется на 5 минут, чтобы не превышать лимит API.
    Возвращает dict с ключами: success, result, from, to, amount, info (при ошибке).
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    cache_key = (from_currency, to_currency)
    now = time.time()
    if cache_key in _convert_cache:
        cached_result, cached_at = _convert_cache[cache_key]
        if now - cached_at < _CONVERT_CACHE_TTL and cached_result.get("success"):
            # Возвращаем закэшированный курс, пересчитанный на amount
            rate = cached_result["result"] / max(cached_result.get("amount", 1), 1e-9)
            return {
                "success": True,
                "from": from_currency,
                "to": to_currency,
                "amount": amount,
                "result": amount * rate,
            }

    url = f"{BASE_URL}/convert"
    params = {
        "access_key": access_key,
        "from": from_currency,
        "to": to_currency,
        "amount": amount,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
    except requests.RequestException as e:
        return {
            "success": False,
            "error": "request_failed",
            "info": f"Не удалось связаться с API: {e!s}",
        }
    except ValueError as e:
        return {"success": False, "error": "invalid_response", "info": str(e)}

    if not data.get("success", False):
        err = data.get("error", {})
        if isinstance(err, dict):
            code = err.get("code", "?")
            info = err.get("info", "Неизвестная ошибка API.")
        else:
            code = err
            info = data.get("info", "Ошибка API.")
        return {
            "success": False,
            "error": "api_error",
            "code": code,
            "info": info,
        }

    result = {
        "success": True,
        "from": data.get("query", {}).get("from", from_currency),
        "to": data.get("query", {}).get("to", to_currency),
        "amount": float(data.get("query", {}).get("amount", amount)),
        "result": float(data.get("result", 0)),
    }
    _convert_cache[cache_key] = (result, now)
    return result


def get_currencies_list(access_key: str):
    """
    Получить список поддерживаемых валют через endpoint /list.
    Возвращает dict: success, currencies (dict код -> название) или info при ошибке.
    """
    url = f"{BASE_URL}/list"
    params = {"access_key": access_key}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
    except requests.RequestException as e:
        return {
            "success": False,
            "error": "request_failed",
            "info": f"Не удалось связаться с API: {e!s}",
        }
    except ValueError as e:
        return {"success": False, "error": "invalid_response", "info": str(e)}

    if not data.get("success", False):
        err = data.get("error", {})
        if isinstance(err, dict):
            info = err.get("info", "Неизвестная ошибка API.")
        else:
            info = data.get("info", "Ошибка API.")
        return {"success": False, "error": "api_error", "info": info}

    return {
        "success": True,
        "currencies": data.get("currencies", {}),
    }


def check_currencies_available(access_key: str, from_currency: str, to_currency: str):
    """
    Проверить, что обе валюты поддерживаются API (через convert с amount=1).
    Возвращает (ok: bool, message: str).
    """
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return False, "Валюта отправления и назначения не должны совпадать."
    result = convert(access_key, from_currency, to_currency, 1.0)
    if not result.get("success"):
        info = result.get("info", "Валюта недоступна в API.")
        return False, info
    return True, "OK"

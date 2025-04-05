# tools/user_data_tools.py

import logging
import asyncio
import json
from typing import Dict, Optional, Any, Union # Добавили Union

# --- Импортируем CRUD операции напрямую ---
try:
    import database
except ImportError:
    logging.critical("CRITICAL: Failed to import 'database' module in user_data_tools.")
    database = None # type: ignore

# --- Импортируем зависимости для аватара ---
try:
    from bot_loader import bot
    from config import settings # Нужен токен бота и API ключ Gemini
    import aiohttp
    from io import BytesIO
    # Импортируем функцию генерации описания
    from ai_interface.gemini_api import generate_image_description
except ImportError:
    logging.error("Failed to import dependencies (bot_loader, config, aiohttp, gemini_api) for avatar functionality.", exc_info=True)
    bot = None # type: ignore
    settings = None # type: ignore
    aiohttp = None # type: ignore
    BytesIO = None # type: ignore
    generate_image_description = None # type: ignore

logger = logging.getLogger(__name__)

# --- Инструменты ---

async def find_user_id(query: str) -> Dict[str, Any]:
    """
    Ищет user_id пользователя в базе данных по его имени (first_name)
    или username (без @). Использует database.find_user_id_by_profile.

    Args:
        query (str): Имя или username пользователя для поиска.

    Returns:
        dict: Словарь со статусом ('success' или 'not_found'/'error') и user_id (если найден).
    """
    tool_name = "find_user_id"
    logger.info(f"--- Tool Call: {tool_name}(query='{query}') ---")
    if not query or not isinstance(query, str):
        return {"status": "error", "message": "Query must be a non-empty string."}
    if database is None:
        return {"status": "error", "message": "Database module is unavailable."}

    try:
        found_id = await database.find_user_id_by_profile(query)
        if found_id:
            msg = f"User ID {found_id} found for query '{query}'."
            logger.info(msg)
            return {"status": "success", "user_id": found_id, "message": msg}
        else:
            msg = f"User with name or username similar to '{query}' not found in known profiles."
            logger.info(msg)
            return {"status": "not_found", "message": msg}
    except Exception as e:
        msg = f"Error searching for user ID with query '{query}': {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


# --- ИСПРАВЛЕНО: Обработка user_id ---
async def reading_user_info(user_id: Union[int, float, str]) -> Dict[str, Any]:
    """
    Получает всю известную информацию (профиль + заметки) о пользователе по его ID.
    Использует database.get_user_data_combined.

    Args:
        user_id (Union[int, float, str]): ID пользователя Telegram (может прийти как float от LLM).

    Returns:
        dict: Словарь со статусом операции и данными пользователя ('user_data_json').
              Данные возвращаются как JSON-строка. При ошибке или отсутствии данных возвращает соответствующий статус.
    """
    tool_name = "reading_user_info"

    # --- Валидация и конвертация user_id ---
    try:
        user_id_int = int(float(user_id)) # Сначала в float для универсальности, потом в int
        if user_id_int <= 0:
             raise ValueError("User ID must be positive.")
    except (ValueError, TypeError):
         logger.error(f"{tool_name}: Invalid user_id type or value: type={type(user_id)}, value={user_id}")
         return {"status": "error", "message": f"Invalid user_id provided: {user_id}"}
    # --- Конец валидации ---

    logger.info(f"--- Tool Call: {tool_name}(user_id={user_id_int}) ---") # Логируем int ID

    # Старая, некорректная проверка удалена
    # if not isinstance(user_id, int) or user_id <= 0:
    #     return {"status": "error", "message": "Invalid user_id provided."}

    if database is None:
        return {"status": "error", "message": "Database module is unavailable."}

    try:
        # Используем user_id_int для запроса к БД
        user_data = await database.get_user_data_combined(user_id_int)

        if not user_data:
            msg = f"No information found for user {user_id_int}."
            logger.info(msg)
            return {"status": "not_found", "message": msg}
        else:
            try:
                data_json_str = json.dumps(user_data, ensure_ascii=False, indent=2, default=str)
                msg = f"User data for {user_id_int} retrieved successfully."
                logger.info(msg)
                return {"status": "success", "message": msg, "user_data_json": data_json_str}
            except Exception as json_err:
                 msg = f"Failed to serialize user data to JSON for user {user_id_int}: {json_err}"
                 logger.error(msg, exc_info=True)
                 return {"status": "warning", "message": f"{msg}. Returning raw data.", "data": user_data}

    except Exception as e:
        msg = f"Error retrieving combined user data for user_id={user_id_int}: {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


# --- ИСПРАВЛЕНО: Обработка user_id ---
async def remember_user_info(
    user_id: Union[int, float, str], # Принимаем разные типы
    info_category: str,
    info_value: str,
    merge_lists: bool = True
) -> Dict[str, str]:
    """
    Сохраняет или обновляет заметку о пользователе. Использует database.upsert_user_note.
    Поддерживает объединение JSON списков/словарей.

    Args:
        user_id (Union[int, float, str]): ID пользователя.
        info_category (str): Категория заметки.
        info_value (str): Значение заметки (текст или JSON-строка).
        merge_lists (bool): Объединять ли списки/словари (по умолчанию True).

    Returns:
        dict: Словарь со статусом операции ('success' или 'error') и сообщением.
    """
    tool_name = "remember_user_info"

    # --- Валидация и конвертация user_id ---
    try:
        user_id_int = int(float(user_id))
        if user_id_int <= 0:
             raise ValueError("User ID must be positive.")
    except (ValueError, TypeError):
         logger.error(f"{tool_name}: Invalid user_id type or value: type={type(user_id)}, value={user_id}")
         return {"status": "error", "message": f"Invalid user_id provided: {user_id}"}
    # --- Конец валидации ---

    logger.info(f"--- Tool Call: {tool_name}(user_id={user_id_int}, category='{info_category}', value='{info_value[:50]}...', merge={merge_lists}) ---")

    # Старая, некорректная проверка удалена
    # if not isinstance(user_id, int) or user_id <= 0: return {"status": "error", "message": "Invalid user_id."}

    # Остальная валидация аргументов
    if not info_category or not isinstance(info_category, str): return {"status": "error", "message": "Invalid info_category."}
    if info_value is None or not isinstance(info_value, str): return {"status": "error", "message": "Invalid info_value (must be string)."}
    if not isinstance(merge_lists, bool):
        logger.warning(f"{tool_name}: Invalid merge_lists type ({type(merge_lists)}). Defaulting to True.")
        merge_lists = True

    if database is None: return {"status": "error", "message": "Database module is unavailable."}

    try:
        # Вызываем CRUD функцию с user_id_int
        success = await database.upsert_user_note(
            user_id=user_id_int,
            category=info_category,
            value=info_value,
            merge_lists=merge_lists
        )
        if success:
            msg = f"Note '{info_category}' for user {user_id_int} upserted successfully (merge={merge_lists})."
            logger.info(msg)
            return {"status": "success", "message": msg}
        else:
            # Функция upsert_user_note должна логировать ошибку БД
            return {"status": "error", "message": f"Failed to upsert note '{info_category}' for user {user_id_int}."}
    except Exception as e:
        msg = f"Unexpected error in {tool_name} handler: {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


# --- ИСПРАВЛЕНО: Обработка user_id ---
async def forget_user_info(
    user_id: Union[int, float, str], # Принимаем разные типы
    info_category: str,
    key: Optional[str] = None,
    list_item: Optional[str] = None
) -> Dict[str, str]:
    """
    Удаляет заметку (или ее часть) о пользователе. Использует database.delete_user_note_nested или database.delete_user_note.

    Args:
        user_id (Union[int, float, str]): ID пользователя.
        info_category (str): Категория заметки.
        key (Optional[str]): Ключ для удаления из словаря.
        list_item (Optional[str]): Элемент для удаления из списка (передается как строка).

    Returns:
        dict: Словарь со статусом операции ('success', 'not_found' или 'error') и сообщением.
    """
    tool_name = "forget_user_info"

    # --- Валидация и конвертация user_id ---
    try:
        user_id_int = int(float(user_id))
        if user_id_int <= 0:
             raise ValueError("User ID must be positive.")
    except (ValueError, TypeError):
         logger.error(f"{tool_name}: Invalid user_id type or value: type={type(user_id)}, value={user_id}")
         return {"status": "error", "message": f"Invalid user_id provided: {user_id}"}
    # --- Конец валидации ---

    logger.info(f"--- Tool Call: {tool_name}(user_id={user_id_int}, category='{info_category}', key={key}, list_item={list_item}) ---")

    # Старая, некорректная проверка удалена
    # if not isinstance(user_id, int) or user_id <= 0: return {"status": "error", "message": "Invalid user_id."}

    if not info_category or not isinstance(info_category, str): return {"status": "error", "message": "Invalid info_category."}

    if database is None: return {"status": "error", "message": "Database module is unavailable."}

    parsed_list_item: Any = list_item

    try:
        if key is not None or list_item is not None:
            # Используем вложенное удаление с user_id_int
            success = await database.delete_user_note_nested(
                user_id=user_id_int,
                category=info_category,
                key=key,
                list_item=parsed_list_item
            )
            if success:
                op_desc = f"key '{key}'" if key is not None else f"item matching '{list_item}'"
                msg = f"{op_desc.capitalize()} deleted from note '{info_category}' for user {user_id_int}."
                logger.info(msg)
                return {"status": "success", "message": msg}
            else:
                op_desc = f"Key '{key}'" if key is not None else f"Item matching '{list_item}'"
                msg = f"{op_desc} or note category '{info_category}' not found for user {user_id_int}."
                logger.info(msg)
                return {"status": "not_found", "message": msg}
        else:
            # Удаляем всю категорию с user_id_int
            success = await database.delete_user_note(user_id_int, info_category)
            if success:
                msg = f"Note category '{info_category}' deleted for user {user_id_int}."
                logger.info(msg)
                return {"status": "success", "message": msg}
            else:
                msg = f"Note category '{info_category}' not found for user {user_id_int}."
                logger.info(msg)
                return {"status": "not_found", "message": msg}

    except Exception as e:
        msg = f"Unexpected error in {tool_name} handler: {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}


# --- ИСПРАВЛЕНО: Обработка user_id ---
async def get_avatar_description(user_id: Union[int, float, str], force_update: bool = False) -> Dict[str, Any]:
    """
    Получает или генерирует описание аватара пользователя, используя AI Vision.
    Использует database.get_user_profile и database.update_avatar_description.

    Args:
        user_id (Union[int, float, str]): ID пользователя.
        force_update (bool): Принудительно обновить описание.

    Returns:
        dict: Словарь со статусом и описанием (или сообщением об ошибке).
    """
    tool_name = "get_avatar_description"

    # --- Валидация и конвертация user_id ---
    try:
        user_id_int = int(float(user_id))
        if user_id_int <= 0:
             raise ValueError("User ID must be positive.")
    except (ValueError, TypeError):
         logger.error(f"{tool_name}: Invalid user_id type or value: type={type(user_id)}, value={user_id}")
         return {"status": "error", "message": f"Invalid user_id provided: {user_id}"}
    # --- Конец валидации ---

    logger.info(f"--- Tool Call: {tool_name}(user_id={user_id_int}, force_update={force_update}) ---")

    # Проверка зависимостей
    if database is None: return {"status": "error", "message": "Database module unavailable."}
    if bot is None: return {"status": "error", "message": "Bot instance unavailable."}
    if settings is None: return {"status": "error", "message": "Settings unavailable."}
    if aiohttp is None or BytesIO is None: return {"status": "error", "message": "Required libraries (aiohttp/io) missing."}
    if generate_image_description is None: return {"status": "error", "message": "Image description function unavailable."}

    # Старая, некорректная проверка удалена
    # if not isinstance(user_id, int) or user_id <= 0: return {"status": "error", "message": "Invalid user_id."}

    if not isinstance(force_update, bool):
         logger.warning(f"{tool_name}: Invalid force_update type ({type(force_update)}). Defaulting to False.")
         force_update = False

    try:
        # Используем user_id_int
        profile_data = await database.get_user_profile(user_id_int)

        # Проверяем кэш в БД, если не нужно принудительное обновление
        if not force_update and profile_data and profile_data.get("avatar_description"):
            desc = profile_data["avatar_description"]
            msg = f"Using cached avatar description for user {user_id_int}."
            logger.info(msg)
            return {"status": "success", "description": desc, "message": msg}

        # --- Получение File ID аватара ---
        avatar_file_id: Optional[str] = None
        # Используем user_id_int
        if profile_data and profile_data.get("avatar_file_id"):
             avatar_file_id = profile_data["avatar_file_id"]
             logger.debug(f"Found avatar file_id in profile cache for user {user_id_int}.")
        else:
             logger.info(f"Avatar file_id not in profile for user {user_id_int}, trying get_user_profile_photos API call.")
             try:
                 # Используем user_id_int
                 profile_photos = await bot.get_user_profile_photos(user_id_int, limit=1)
                 if profile_photos and profile_photos.photos:
                     avatar_file_id = profile_photos.photos[0][-1].file_id
                     logger.info(f"Retrieved avatar file_id '{avatar_file_id}' via API for user {user_id_int}.")
                     # Используем user_id_int
                     await database.update_avatar_description(user_id_int, avatar_file_id=avatar_file_id)
                 else:
                     msg = f"User {user_id_int} has no accessible profile photos via API."
                     logger.info(msg)
                     return {"status": "not_found", "message": msg}
             except Exception as get_photo_err:
                 msg = f"Failed to get user profile photos via API for user {user_id_int}: {get_photo_err}"
                 logger.error(msg, exc_info=False)
                 return {"status": "error", "message": f"Failed to get profile photos: {get_photo_err}"}

        if not avatar_file_id:
             return {"status": "error", "message": "Could not retrieve avatar file_id (not in DB or via API)."}

        # --- Загрузка и Описание Аватара ---
        logger.info(f"Generating new avatar description for user {user_id_int} (file_id: {avatar_file_id})...")
        image_bytes: Optional[bytes] = None
        try:
            file_info = await bot.get_file(avatar_file_id)
            if not file_info.file_path:
                 raise ValueError("Telegram API returned file info without file_path.")
            if not settings.bot_token: raise ValueError("Bot token is not configured in settings.")

            async with aiohttp.ClientSession() as session:
                file_url = bot.session.api.file_url(settings.bot_token, file_info.file_path)
                logger.debug(f"Downloading avatar from: {file_url}")
                async with session.get(file_url) as resp:
                    if resp.status != 200:
                        raise ConnectionError(f"Failed download avatar: HTTP {resp.status} from {file_url}")
                    image_bytes = await resp.read()
                    logger.debug(f"Avatar downloaded ({len(image_bytes)} bytes).")

        except Exception as download_err:
            msg = f"Error downloading avatar (file_id: {avatar_file_id}) user {user_id_int}: {download_err}"
            logger.error(msg, exc_info=True)
            return {"status": "error", "message": msg}

        if image_bytes:
            try:
                if not settings.google_api_key:
                     raise ValueError("Google API Key is not configured for Vision model.")

                prompt = "Опиши подробно, что изображено на этой аватарке пользователя Telegram. Сфокусируйся на визуальных деталях: объекты, люди (если есть), стиль, цвета, настроение. Будь объективен. Ответ дай в 1-3 предложениях."
                new_description = await generate_image_description(
                    api_key=settings.google_api_key,
                    image_bytes=image_bytes,
                    prompt=prompt
                )

                if new_description is None or "[Описание заблокировано:" in new_description:
                    msg = f"Image description generation failed or was blocked for user {user_id_int}."
                    logger.error(msg)
                    return {"status": "error", "message": new_description or msg }

                # Используем user_id_int
                update_success = await database.update_avatar_description(
                    user_id_int,
                    avatar_file_id=avatar_file_id,
                    avatar_description=new_description
                )
                if not update_success:
                     logger.error(f"Failed to save generated avatar description to DB for user {user_id_int}.")

                msg = f"New avatar description generated and {'saved' if update_success else 'failed to save'} for user {user_id_int}."
                logger.info(msg)
                return {"status": "success" if update_success else "warning", "description": new_description, "message": msg}

            except Exception as vision_err:
                 msg = f"Error generating image description for user {user_id_int}: {vision_err}"
                 logger.error(msg, exc_info=True)
                 return {"status": "error", "message": msg}
        else:
             return {"status": "error", "message": "Avatar download failed, cannot generate description."}

    except Exception as e:
        # Используем user_id_int в логе, если он был определен
        user_id_for_log = locals().get('user_id_int', user_id)
        msg = f"Unexpected error in {tool_name} handler for user_id={user_id_for_log}: {e}"
        logger.error(msg, exc_info=True)
        return {"status": "error", "message": msg}
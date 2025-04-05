# database/crud_ops/settings.py
import aiosqlite
import logging
from typing import Dict, Any, Optional

# Используем относительный импорт для связи с connection.py
try:
    from ..connection import get_connection
except ImportError:
    # Заглушка на случай проблем с импортом
    async def get_connection(): raise ImportError("Connection module not loaded")
    logging.getLogger(__name__).critical("Failed to import get_connection from ..connection")

logger = logging.getLogger(__name__)

# Константы для ai_mode, чтобы избежать опечаток и обеспечить согласованность
# Эти же константы можно будет импортировать в другие модули
AI_MODE_PRO = "pro"
AI_MODE_DEFAULT = "default" # Используем 'default' как имя для g4f или другого стандартного режима

async def upsert_chat_settings(
    chat_id: int,
    custom_prompt: Optional[str] = None,
    ai_mode: Optional[str] = None, # Принимает строки, соответствующие константам
    gemini_model: Optional[str] = None
) -> bool:
    """
    Обновляет или вставляет настройки для указанного чата.
    Обновляет только переданные не-None значения.
    Возвращает True при успехе, False при ошибке.
    """
    if not isinstance(chat_id, int):
        logger.error(f"upsert_chat_settings: Invalid chat_id: {chat_id}")
        return False

    conn: aiosqlite.Connection
    update_parts = []
    params: List[Any] = [] # Список для параметров SQL запроса

    # Собираем части для обновления
    if custom_prompt is not None:
        update_parts.append("custom_prompt = ?")
        params.append(custom_prompt)
    if ai_mode is not None:
        # Валидируем значение ai_mode
        if ai_mode not in [AI_MODE_PRO, AI_MODE_DEFAULT]:
            logger.warning(f"upsert_chat_settings: Invalid ai_mode '{ai_mode}' provided for chat {chat_id}. Ignoring update for this field.")
            # Не добавляем некорректное значение в запрос
        else:
            update_parts.append("ai_mode = ?")
            params.append(ai_mode)
    if gemini_model is not None:
        # TODO: Возможно, добавить валидацию имени модели по списку из config?
        update_parts.append("gemini_model = ?")
        params.append(gemini_model)

    # Если нечего обновлять/вставлять, считаем операцию успешной
    if not update_parts:
        logger.debug(f"No valid settings provided to upsert for chat_id {chat_id}.")
        return True # Успех, т.к. не было ошибки и нечего делать

    update_parts.append("last_update_ts = CURRENT_TIMESTAMP") # Всегда обновляем время

    try:
        conn = await get_connection()
        # Сначала пытаемся обновить существующую запись
        sql_update = f"UPDATE chat_settings SET {', '.join(update_parts)} WHERE chat_id = ?"
        params.append(chat_id) # Добавляем chat_id для WHERE
        cursor = await conn.execute(sql_update, tuple(params))
        rows_affected = cursor.rowcount
        await conn.commit() # Коммитим после UPDATE

        if rows_affected > 0:
            logger.info(f"Updated chat settings for chat_id={chat_id}")
            await cursor.close() # Закрываем курсор
            return True
        else:
            # Если не обновилось, значит записи нет - вставляем
            await cursor.close() # Закрываем курсор от UPDATE
            logger.info(f"Settings not found for update chat_id={chat_id}, attempting insert...")

            # Собираем параметры для вставки (только те, что были переданы и валидны)
            insert_fields = ["chat_id", "last_update_ts"]
            insert_placeholders = ["?", "CURRENT_TIMESTAMP"]
            insert_params = [chat_id]

            # Используем исходные значения из аргументов функции
            if custom_prompt is not None:
                insert_fields.append("custom_prompt")
                insert_placeholders.append("?")
                insert_params.append(custom_prompt)
            if ai_mode in [AI_MODE_PRO, AI_MODE_DEFAULT]: # Вставляем только валидный режим
                insert_fields.append("ai_mode")
                insert_placeholders.append("?")
                insert_params.append(ai_mode)
            elif ai_mode is not None: # Если передан невалидный, вставляем дефолтный
                 logger.warning(f"Inserting default AI mode for chat {chat_id} as provided mode '{ai_mode}' was invalid.")
                 insert_fields.append("ai_mode")
                 insert_placeholders.append("?")
                 insert_params.append(AI_MODE_DEFAULT) # Вставляем дефолтный
            # Если ai_mode не передан, будет использовано значение DEFAULT из схемы таблицы

            if gemini_model is not None:
                insert_fields.append("gemini_model")
                insert_placeholders.append("?")
                insert_params.append(gemini_model)

            # Используем INSERT OR IGNORE на случай гонки потоков
            sql_insert = f"INSERT OR IGNORE INTO chat_settings ({', '.join(insert_fields)}) VALUES ({', '.join(insert_placeholders)})"
            cursor = await conn.execute(sql_insert, tuple(insert_params))
            rows_inserted = cursor.rowcount
            await conn.commit() # Коммитим после INSERT
            await cursor.close() # Закрываем курсор

            if rows_inserted > 0:
                logger.info(f"Inserted new chat settings for chat_id={chat_id}")
                return True
            else:
                # Может произойти при одновременной попытке вставки (IGNORE) или если запись уже появилась между UPDATE и INSERT
                logger.warning(f"Failed to insert chat settings for chat_id={chat_id} (maybe race condition or other IGNORE issue). Settings might have been updated by another process.")
                # Попробуем еще раз прочитать настройки, чтобы убедиться
                current_settings = await get_chat_settings(chat_id)
                if current_settings:
                     logger.info(f"Settings for chat {chat_id} seem to exist now. Considering upsert successful despite failed INSERT.")
                     return True # Считаем успехом, если запись теперь есть
                return False # Считаем неудачей, если не обновили и не вставили

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error upserting chat settings for chat_id={chat_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after settings upsert error: {rb_e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error upserting chat settings for chat_id={chat_id}: {e}", exc_info=True)
        return False


async def get_chat_settings(chat_id: int) -> Optional[Dict[str, Any]]:
    """
    Получает настройки чата по его ID.
    Возвращает словарь настроек или None, если не найдено или ошибка.
    В словаре будут ключи: chat_id, custom_prompt, ai_mode, gemini_model, last_update_ts.
    """
    if not isinstance(chat_id, int):
        logger.error(f"get_chat_settings: Invalid chat_id: {chat_id}")
        return None

    conn: aiosqlite.Connection
    settings_data: Optional[Dict[str, Any]] = None
    try:
        conn = await get_connection()
        async with conn.execute(
            """
            SELECT chat_id, custom_prompt, ai_mode, gemini_model, last_update_ts
            FROM chat_settings
            WHERE chat_id = ?
            """,
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                settings_data = dict(row)
                logger.debug(f"Retrieved settings for chat_id={chat_id}")
            else:
                logger.debug(f"No settings found for chat_id={chat_id}")

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting chat settings for chat_id {chat_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error getting settings for chat_id {chat_id}: {e}", exc_info=True)

    return settings_data


async def delete_chat_settings(chat_id: int) -> bool:
    """
    Удаляет настройки для указанного chat_id.
    Возвращает True при успехе, False если запись не найдена или произошла ошибка.
    """
    if not isinstance(chat_id, int):
        logger.error(f"delete_chat_settings: Invalid chat_id: {chat_id}")
        return False

    conn: aiosqlite.Connection
    success = False
    try:
        conn = await get_connection()
        cursor = await conn.execute("DELETE FROM chat_settings WHERE chat_id = ?", (chat_id,))
        deleted_count = cursor.rowcount
        await conn.commit()
        await cursor.close()

        if deleted_count > 0:
            logger.info(f"Deleted chat settings for chat_id={chat_id}")
            success = True
        else:
            logger.info(f"Settings not found for deletion: chat_id={chat_id}")
            success = False # Запись не найдена, но ошибки не было

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error deleting chat settings for chat_id={chat_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after settings delete error: {rb_e}")
        success = False
    except Exception as e:
        logger.error(f"Unexpected error deleting settings for chat_id={chat_id}: {e}", exc_info=True)
        success = False

    return success
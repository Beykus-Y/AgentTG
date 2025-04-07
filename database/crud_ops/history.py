# database/crud_ops/history.py
import aiosqlite
import json
import logging
from typing import List, Dict, Any, Optional

# Используем относительный импорт для связи с connection.py и converters.py
try:
    from ..connection import get_connection
    # Используем абсолютный импорт от корня проекта для utils
    from utils.converters import _serialize_parts, _deserialize_parts
except ImportError as e:
    # Логгируем более конкретную ошибку
    logging.getLogger(__name__).critical(f"Failed to import dependencies in history.py: {e}", exc_info=True)
    # Заглушки
    async def get_connection(): raise ImportError("Connection module not loaded")
    def _serialize_parts(parts: List[Any]) -> str: return "[]"
    def _deserialize_parts(parts_json: str) -> List[Dict[str, Any]]: return []

logger = logging.getLogger(__name__)

# --- Основные CRUD операции ---

async def add_message_to_history(
    chat_id: int,
    role: str,
    parts: str, # <<< ИЗМЕНЕНО: Теперь принимаем JSON строку
    user_id: Optional[int] = None
):
    """
    Добавляет сообщение в историю чата.
    `parts` должен быть валидной JSON строкой.
    """
    if role not in ('user', 'model', 'system', 'function'):
        logger.error(f"Invalid role '{role}' provided for chat history (chat_id: {chat_id}).")
        return

    # Проверяем, что parts - это строка
    if not isinstance(parts, str):
         logger.error(f"add_message_to_history expected 'parts' argument to be a JSON string, got {type(parts)}. History not saved.")
         return

    parts_json = parts # Переименовываем для ясности

    # --- УБИРАЕМ ВЫЗОВ СЕРИАЛИЗАЦИИ ---
    # parts_json = _serialize_parts(parts) # <<< УДАЛЕН ВЫЗОВ
    # -------------------------------

    # <<< Переносим проверку на пустой JSON сюда (для роли model) >>>
    if parts_json == "[]" and role != 'model':
         # Оставляем ошибку для не-model ролей, если исходные parts НЕ были пустыми
         logger.error(f"Serialization resulted in empty JSON '[]' for non-empty parts chat={chat_id}, role={role}. History not saved.")
         return
    elif parts_json == "[]" and role == 'model':
         logger.info(f"Saving history entry for model with empty parts (parts_json='[]') for chat={chat_id}.")
    # elif parts_json != "[]":
         # logger.debug(f"Serialized parts to JSON (size: {len(parts_json)}) for chat={chat_id}, role={role}")

    conn: aiosqlite.Connection
    try:
        conn = await get_connection()
        await conn.execute(
            """
            INSERT INTO chat_history (chat_id, role, user_id, parts_json)
            VALUES (?, ?, ?, ?)
            """,
            (chat_id, role, user_id, parts_json) # <<< Передаем parts_json
        )
        logger.debug(f"Executed INSERT for chat={chat_id}, role={role}. Preparing to commit...") # Лог перед commit
        await conn.commit()
        logger.info(f"Successfully committed history entry: chat={chat_id}, role={role}, user={user_id}, json_size={len(parts_json)}") # Лог после commit

        # <<< ПРОВЕРКА СРАЗУ ПОСЛЕ КОММИТА >>>
        try:
            # Ищем последнюю запись для этого чата и роли (немного неточно, но для теста сойдет)
            sql = "SELECT id, timestamp FROM chat_history WHERE chat_id = ? AND role = ? ORDER BY id DESC LIMIT 1"
            params = (chat_id, role)
            async with conn.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                if row:
                    logger.info(f"VERIFY AFTER COMMIT: Found history for {chat_id}/{role}, id: {row['id']}, ts: {row['timestamp']}")
                else:
                    logger.error(f"VERIFY AFTER COMMIT: FAILED TO FIND history for {chat_id}/{role} immediately after commit!")
        except Exception as verify_err:
            logger.error(f"VERIFY AFTER COMMIT: Error during verification select for {chat_id}/{role}: {verify_err}", exc_info=True)
        # <<< КОНЕЦ ПРОВЕРКИ >>>

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Failed insert history chat={chat_id}, role={role}, user={user_id}: {e}", exc_info=True)
        # Попытка отката
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
            try:
                logger.warning(f"Attempting rollback after failed history insert for chat={chat_id}, role={role}...") # Лог перед rollback
                await conn.rollback()
                logger.info(f"Rollback successful after failed history insert for chat={chat_id}, role={role}.") # Лог после rollback
            except Exception as rb_e:
                logger.error(f"Rollback failed after history insert error: {rb_e}")
    except Exception as e:
        logger.error(f"Unexpected error adding history chat={chat_id}, role={role}, user={user_id}: {e}", exc_info=True)


async def get_chat_history(chat_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Получает историю чата из БД в виде списка словарей.
    Возвращает [{role: ..., user_id: ... (opt), parts: [{text: ...}, ...]}, ...].
    """
    if not isinstance(limit, int) or limit <= 0:
        limit = 50

    conn: aiosqlite.Connection
    history_list: List[Dict[str, Any]] = []
    try:
        conn = await get_connection()
        async with conn.execute(
            """
            SELECT role, user_id, parts_json, timestamp
            FROM chat_history
            WHERE chat_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()

        for row in reversed(rows):
            # --- ВЫЗОВ ДЕСЕРИАЛИЗАЦИИ ---
            parts_list = _deserialize_parts(row['parts_json']) # <-- Сначала десериализуем
            # ---------------------------

            # --->>> НАЧАЛО ИСПРАВЛЕННОГО БЛОКА <<<---
            deserialization_error = False
            # Проверяем на маркер ошибки ПОСЛЕ десериализации
            # Убираем лишний отступ у этого if
            if parts_list and isinstance(parts_list, list) and len(parts_list) > 0 and isinstance(parts_list[0], dict) and parts_list[0].get("error") == "deserialization_failed":
                # Логгируем ошибку и устанавливаем флаг
                logger.error(f"History entry skipped due to deserialization error: chat={chat_id}, role={row['role']}, ts={row['timestamp']}")
                deserialization_error = True
                # Пропускаем обработку этой записи, так как она повреждена
                continue # <-- ВАЖНО: переходим к следующей строке истории
            # --->>> КОНЕЦ ИСПРАВЛЕННОГО БЛОКА <<<---

            # Добавляем запись в историю, только если не было ошибки десериализации
            # и список частей не пустой (если мы все же решили не сохранять пустые)
            if not deserialization_error and parts_list:
                entry = {"role": row["role"], "parts": parts_list}
                if row["role"] == 'user' and row['user_id'] is not None:
                    entry['user_id'] = row['user_id']
                history_list.append(entry)
            elif not deserialization_error: # Если список пуст, но ошибки не было
                 logger.warning(f"Deserialized parts resulted in empty list (and no error marker), skipping history entry. chat={chat_id}, role={row['role']}, ts={row['timestamp']}")

        logger.debug(f"Retrieved {len(history_list)} history entries for chat_id={chat_id} (limit={limit})")
        return history_list

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Failed fetch chat history chat_id={chat_id}: {e}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching chat history chat_id={chat_id}: {e}", exc_info=True)
        return []
        
async def clear_chat_history(chat_id: int) -> int:
    """
    Удаляет всю историю сообщений для указанного chat_id.
    Возвращает количество удаленных записей.
    """
    conn: aiosqlite.Connection
    deleted_count = 0
    try:
        conn = await get_connection()
        cursor = await conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
        deleted_count = cursor.rowcount
        logger.debug(f"Executed DELETE and committed for clear_history chat={chat_id}.") # Лог после commit
        await cursor.close()
        if deleted_count > 0:
             logger.info(f"Cleared {deleted_count} history entries for chat_id={chat_id}")
        else:
             logger.info(f"No history entries found to clear for chat_id={chat_id}")
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Failed to clear history for chat_id={chat_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try:
                  logger.warning(f"Attempting rollback after failed history clear for chat={chat_id}...") # Лог перед rollback
                  await conn.rollback()
                  logger.info(f"Rollback successful after failed history clear for chat={chat_id}.") # Лог после rollback
              except Exception as rb_e: logger.error(f"Rollback failed after history clear error: {rb_e}")
    except Exception as e:
        logger.error(f"Unexpected error clearing history for chat_id={chat_id}: {e}", exc_info=True)
    return deleted_count
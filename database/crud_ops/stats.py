# database/crud_ops/stats.py
import aiosqlite
import logging
from typing import List, Tuple, Dict, Optional
from datetime import datetime, timezone

# Используем относительный импорт
try:
    from ..connection import get_connection
except ImportError:
    async def get_connection(): raise ImportError("Connection module not loaded")
    logging.getLogger(__name__).critical("Failed to import get_connection from ..connection")

logger = logging.getLogger(__name__)

# --- Статистика сообщений ---

async def increment_message_count(chat_id: int, user_id: int) -> bool:
    """
    Увеличивает счетчик сообщений для пользователя в чате.
    Создает запись, если ее нет. Использует INSERT ON CONFLICT DO UPDATE.
    Возвращает True при успехе, False при ошибке.
    """
    if not isinstance(chat_id, int) or not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"increment_message_count: Invalid chat_id or user_id: c={chat_id}, u={user_id}")
        return False

    conn: aiosqlite.Connection
    try:
        conn = await get_connection()
        # INSERT OR ... ON CONFLICT ... DO UPDATE - атомарная операция
        await conn.execute('''
            INSERT INTO message_stats (chat_id, user_id, message_count, last_message_ts)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                message_count = message_count + 1,
                last_message_ts = CURRENT_TIMESTAMP
            WHERE chat_id = excluded.chat_id AND user_id = excluded.user_id;
        ''', (chat_id, user_id))
        await conn.commit()
        # Не логируем успех, т.к. слишком часто вызывается
        # logger.debug(f"Incremented message count user={user_id}, chat={chat_id}")
        return True
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error incrementing msg count user={user_id}, chat={chat_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after msg count increment error: {rb_e}")
        return False
    except Exception as e:
         logger.error(f"Unexpected error incrementing msg count user={user_id}, chat={chat_id}: {e}", exc_info=True)
         return False


async def get_chat_stats_top_users(chat_id: int, limit: int = 10) -> List[Tuple[int, int]]:
    """
    Получает топ N пользователей по количеству сообщений в чате.
    Возвращает список кортежей (user_id, message_count) или пустой список.
    """
    if not isinstance(chat_id, int) or not isinstance(limit, int) or limit <= 0:
        logger.error(f"get_chat_stats_top_users: Invalid chat_id or limit: c={chat_id}, l={limit}")
        return []

    conn: aiosqlite.Connection
    top_users: List[Tuple[int, int]] = []
    try:
        conn = await get_connection()
        async with conn.execute(
            """
            SELECT user_id, message_count
            FROM message_stats
            WHERE chat_id = ? AND message_count > 0 -- Исключаем пользователей с 0 сообщений
            ORDER BY message_count DESC
            LIMIT ?
            """,
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            top_users = [(row['user_id'], row['message_count']) for row in rows]
        logger.debug(f"Retrieved top {len(top_users)} users for chat_id={chat_id}")
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting chat stats chat_id={chat_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error getting chat stats chat_id={chat_id}: {e}", exc_info=True)

    return top_users

# --- Предупреждения пользователей ---

async def get_user_warn_count(chat_id: int, user_id: int) -> int:
    """
    Получает текущее количество предупреждений пользователя в чате.
    Возвращает 0, если запись не найдена или произошла ошибка.
    """
    if not isinstance(chat_id, int) or not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"get_user_warn_count: Invalid chat_id or user_id: c={chat_id}, u={user_id}")
        return 0

    conn: aiosqlite.Connection
    count = 0
    try:
        conn = await get_connection()
        async with conn.execute(
            "SELECT warn_count FROM user_warnings WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                count = row['warn_count']
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting warn count user={user_id}, chat={chat_id}: {e}", exc_info=True)
    except Exception as e:
         logger.error(f"Unexpected error getting warn count user={user_id}, chat={chat_id}: {e}", exc_info=True)
    return count


async def add_user_warning(chat_id: int, user_id: int) -> Optional[int]:
    """
    Добавляет одно предупреждение пользователю в чате.
    Создает запись, если ее нет. Использует INSERT ON CONFLICT DO UPDATE.
    Возвращает НОВОЕ количество предупреждений или None при ошибке.
    """
    if not isinstance(chat_id, int) or not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"add_user_warning: Invalid chat_id or user_id: c={chat_id}, u={user_id}")
        return None

    conn: aiosqlite.Connection
    new_count: Optional[int] = None
    try:
        conn = await get_connection()
        # Атомарное добавление/обновление
        await conn.execute('''
            INSERT INTO user_warnings (chat_id, user_id, warn_count, last_warn_ts)
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                warn_count = warn_count + 1,
                last_warn_ts = CURRENT_TIMESTAMP
            WHERE chat_id = excluded.chat_id AND user_id = excluded.user_id;
        ''', (chat_id, user_id))
        await conn.commit()

        # Получаем новое значение (надежнее, чем предполагать +1)
        new_count = await get_user_warn_count(chat_id, user_id)
        if new_count is not None: # get_user_warn_count вернет 0 если ошибка, None тут быть не должно
            logger.info(f"Added warning user={user_id}, chat={chat_id}. New count: {new_count}")
        else:
             logger.error(f"Failed to retrieve new warn count after adding user={user_id}, chat={chat_id}")

        return new_count

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error adding warning user={user_id}, chat={chat_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after adding warning error: {rb_e}")
        return None
    except Exception as e:
         logger.error(f"Unexpected error adding warning user={user_id}, chat={chat_id}: {e}", exc_info=True)
         return None


async def remove_user_warning(chat_id: int, user_id: int, count: int = 1) -> Optional[int]:
    """
    Уменьшает количество предупреждений пользователя на 'count'.
    Не позволяет счетчику уйти ниже нуля.
    Возвращает НОВОЕ количество предупреждений или None при ошибке.
    """
    if not isinstance(chat_id, int) or not isinstance(user_id, int) or user_id <= 0 or not isinstance(count, int) or count <= 0:
        logger.error(f"remove_user_warning: Invalid params: c={chat_id}, u={user_id}, count={count}")
        return None

    conn: aiosqlite.Connection
    new_count: Optional[int] = None
    try:
        conn = await get_connection()
        # Обновляем, используя MAX(0, ...)
        # Обновляем только если есть что уменьшать (warn_count > 0)
        cursor = await conn.execute(
            """
            UPDATE user_warnings
            SET warn_count = MAX(0, warn_count - ?),
                last_warn_ts = CURRENT_TIMESTAMP
            WHERE chat_id = ? AND user_id = ? AND warn_count > 0;
            """,
            (count, chat_id, user_id)
        )
        rows_affected = cursor.rowcount
        await conn.commit()
        await cursor.close()

        # Получаем новое значение
        new_count = await get_user_warn_count(chat_id, user_id)
        if new_count is not None:
            if rows_affected > 0:
                logger.info(f"Removed {count} warning(s) user={user_id}, chat={chat_id}. New count: {new_count}")
            else:
                 logger.info(f"No warnings to remove or user not found user={user_id}, chat={chat_id}. Current count: {new_count}")
        else:
             logger.error(f"Failed to retrieve new warn count after removing user={user_id}, chat={chat_id}")

        return new_count

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error removing warning user={user_id}, chat={chat_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after removing warning error: {rb_e}")
        return None
    except Exception as e:
         logger.error(f"Unexpected error removing warning user={user_id}, chat={chat_id}: {e}", exc_info=True)
         return None


async def get_chat_warnings(chat_id: int) -> Dict[int, int]:
    """
    Получает словарь {user_id: warn_count} для всех пользователей с варнами > 0 в чате.
    Возвращает пустой словарь при ошибке или отсутствии варнов.
    """
    if not isinstance(chat_id, int):
        logger.error(f"get_chat_warnings: Invalid chat_id: {chat_id}")
        return {}

    conn: aiosqlite.Connection
    warnings_dict: Dict[int, int] = {}
    try:
        conn = await get_connection()
        async with conn.execute(
            "SELECT user_id, warn_count FROM user_warnings WHERE chat_id = ? AND warn_count > 0 ORDER BY user_id",
            (chat_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            warnings_dict = {row['user_id']: row['warn_count'] for row in rows}
        logger.debug(f"Retrieved {len(warnings_dict)} users with warnings for chat_id={chat_id}")
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting chat warnings chat_id={chat_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error getting chat warnings chat_id={chat_id}: {e}", exc_info=True)

    return warnings_dict


async def reset_user_warnings(chat_id: int, user_id: int) -> bool:
    """
    Сбрасывает счетчик предупреждений пользователя в чате до 0 (удаляет запись).
    Возвращает True при успехе (даже если записи не было), False при ошибке БД.
    """
    if not isinstance(chat_id, int) or not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"reset_user_warnings: Invalid chat_id or user_id: c={chat_id}, u={user_id}")
        return False

    conn: aiosqlite.Connection
    success = False
    try:
        conn = await get_connection()
        cursor = await conn.execute("DELETE FROM user_warnings WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        deleted_count = cursor.rowcount
        await conn.commit()
        await cursor.close()
        if deleted_count > 0:
            logger.info(f"Reset warnings for user={user_id} in chat={chat_id}")
            success = True
        else:
            # Если записи не было, это тоже успех (счетчик уже 0)
            logger.info(f"No warnings found to reset for user={user_id} in chat={chat_id}")
            success = True
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error resetting warnings user={user_id}, chat={chat_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after resetting warnings error: {rb_e}")
        success = False
    except Exception as e:
         logger.error(f"Unexpected error resetting warnings user={user_id}, chat={chat_id}: {e}", exc_info=True)
         success = False
    return success
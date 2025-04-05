# database/crud_ops/profiles.py
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

async def upsert_user_profile(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str]
) -> bool:
    """
    Добавляет или обновляет базовую информацию о пользователе
    (username, first_name, last_name, last_seen).
    Возвращает True при успехе, False при ошибке.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"Attempted to upsert profile with invalid user_id: {user_id}")
        return False

    conn: aiosqlite.Connection
    try:
        conn = await get_connection()
        await conn.execute('''
            INSERT INTO user_profiles (user_id, username, first_name, last_name, last_seen)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_seen = CURRENT_TIMESTAMP
            WHERE user_id = excluded.user_id;
        ''', (user_id, username, first_name, last_name))
        logger.debug(f"Executed UPSERT for user_id {user_id}. Preparing to commit...")
        await conn.commit()
        logger.info(f"Successfully committed upsert for user profile for user_id {user_id}")

        # <<< ПРОВЕРКА СРАЗУ ПОСЛЕ КОММИТА >>>
        try:
            async with conn.execute("SELECT last_seen FROM user_profiles WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    logger.info(f"VERIFY AFTER COMMIT: Found profile for {user_id}, last_seen: {row['last_seen']}")
                else:
                    logger.error(f"VERIFY AFTER COMMIT: FAILED TO FIND profile for {user_id} immediately after commit!")
        except Exception as verify_err:
            logger.error(f"VERIFY AFTER COMMIT: Error during verification select for {user_id}: {verify_err}", exc_info=True)
        # <<< КОНЕЦ ПРОВЕРКИ >>>

        return True
    except (aiosqlite.Error, ImportError) as e: # Ловим и ImportError от get_connection
         logger.error(f"Failed to upsert user profile for user_id {user_id}: {e}", exc_info=True)
         # Попытка отката не нужна при SELECT/INSERT OR REPLACE, но не помешает
         if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try:
                  logger.warning(f"Attempting rollback after failed profile upsert for user_id={user_id}...")
                  await conn.rollback()
                  logger.info(f"Rollback successful after failed profile upsert for user_id={user_id}.")
              except Exception as rb_e: logger.error(f"Rollback failed after profile upsert error: {rb_e}")
         return False
    except Exception as e:
        logger.error(f"Unexpected error upserting profile for user_id {user_id}: {e}", exc_info=True)
        return False


async def get_user_profile(user_id: int) -> Optional[Dict[str, Any]]:
    """
    Получает данные профиля пользователя из таблицы user_profiles.
    Возвращает словарь с данными или None, если пользователь не найден или произошла ошибка.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"Attempted to get profile with invalid user_id: {user_id}")
        return None

    conn: aiosqlite.Connection
    profile_data: Optional[Dict[str, Any]] = None
    try:
        conn = await get_connection()
        async with conn.execute(
            """
            SELECT user_id, username, first_name, last_name, last_seen, avatar_file_id, avatar_description
            FROM user_profiles
            WHERE user_id = ?
            """,
            (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                # Преобразуем aiosqlite.Row в обычный словарь
                profile_data = dict(row)
                logger.debug(f"Retrieved profile data for user_id={user_id}")
            else:
                logger.debug(f"No profile found for user_id={user_id}")
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting user profile for user_id {user_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error getting profile for user_id {user_id}: {e}", exc_info=True)

    return profile_data # Возвращаем словарь или None


async def update_avatar_description(
    user_id: int,
    avatar_file_id: Optional[str] = None,
    avatar_description: Optional[str] = None
) -> bool:
    """
    Обновляет информацию об аватаре пользователя (file_id и/или description) в его профиле.
    Если профиль не существует, пытается создать его с этой информацией.
    Возвращает True при успехе, False при ошибке.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"Attempted avatar update with invalid user_id: {user_id}")
        return False
    if avatar_file_id is None and avatar_description is None:
        logger.warning(f"Attempted avatar update with no data provided for user_id {user_id}")
        # Считаем это успехом, т.к. не было ошибки, просто нечего делать
        return True

    conn: aiosqlite.Connection
    updated = False
    try:
        conn = await get_connection()
        update_fields = []
        params = []
        if avatar_file_id is not None:
            update_fields.append("avatar_file_id = ?")
            params.append(avatar_file_id)
        if avatar_description is not None:
            update_fields.append("avatar_description = ?")
            params.append(avatar_description)

        params.append(user_id) # Добавляем user_id для WHERE

        sql_update = f"UPDATE user_profiles SET {', '.join(update_fields)}, last_seen = CURRENT_TIMESTAMP WHERE user_id = ?"

        logger.debug(f"Executing UPDATE avatar for user_id={user_id}. Preparing to commit...")
        cursor = await conn.execute(sql_update, tuple(params))
        rows_affected = cursor.rowcount
        await conn.commit()
        logger.info(f"Successfully committed avatar UPDATE for user_id={user_id}. Rows affected: {rows_affected}")
        await cursor.close()

        if rows_affected > 0:
            logger.info(f"Successfully updated avatar info for user_id={user_id}")
            updated = True
        else:
            # Профиль мог не существовать, пытаемся вставить
            logger.info(f"Profile not found for update user_id={user_id}, attempting insert...")
            insert_fields = ["user_id", "last_seen"]
            insert_placeholders = ["?", "CURRENT_TIMESTAMP"]
            insert_params = [user_id]

            if avatar_file_id is not None:
                insert_fields.append("avatar_file_id")
                insert_placeholders.append("?")
                insert_params.append(avatar_file_id)
            if avatar_description is not None:
                insert_fields.append("avatar_description")
                insert_placeholders.append("?")
                insert_params.append(avatar_description)

            # Используем INSERT OR IGNORE на случай гонки потоков
            sql_insert = f"INSERT OR IGNORE INTO user_profiles ({', '.join(insert_fields)}) VALUES ({', '.join(insert_placeholders)})"

            logger.debug(f"Executing INSERT avatar for user_id={user_id}. Preparing to commit...")
            cursor = await conn.execute(sql_insert, tuple(insert_params))
            rows_inserted = cursor.rowcount
            await conn.commit()
            logger.info(f"Successfully committed avatar INSERT for user_id={user_id}. Rows inserted: {rows_inserted}")
            await cursor.close()

            if rows_inserted > 0:
                logger.info(f"Successfully inserted profile with avatar info for user_id={user_id}")
                updated = True
            else:
                # Возможно, конфликт ключа при одновременной вставке, или другая ошибка IGNORE
                logger.warning(f"Failed to insert profile with avatar info for user_id={user_id} (maybe already exists or other IGNORE issue).")
                updated = False # Считаем неудачей, если не обновили и не вставили

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error updating/inserting avatar info for user_id={user_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try:
                  logger.warning(f"Attempting rollback after failed avatar update/insert for user_id={user_id}...")
                  await conn.rollback()
                  logger.info(f"Rollback successful after failed avatar update/insert for user_id={user_id}.")
              except Exception as rb_e: logger.error(f"Rollback failed after avatar update error: {rb_e}")
        updated = False
    except Exception as e:
        logger.error(f"Unexpected error updating avatar info for user_id={user_id}: {e}", exc_info=True)
        updated = False

    return updated


async def find_user_id_by_profile(query: str) -> Optional[int]:
    """
    Ищет user_id в таблице user_profiles по имени (first_name) или username (без @).
    Поиск без учета регистра.

    Args:
        query (str): Имя или username пользователя для поиска.

    Returns:
        Optional[int]: Найденный user_id или None.
    """
    if not query or not isinstance(query, str):
        logger.warning("find_user_id_by_profile called with empty or invalid query.")
        return None

    conn: aiosqlite.Connection
    query_lower = query.lower().strip()
    username_query = query_lower.lstrip('@') # Убираем @ для поиска по username

    found_id: Optional[int] = None
    try:
        conn = await get_connection()
        # Сначала ищем по точному совпадению username (без учета регистра)
        sql_user = 'SELECT user_id FROM user_profiles WHERE LOWER(username) = ? LIMIT 1'
        async with conn.execute(sql_user, (username_query,)) as cursor:
            row = await cursor.fetchone()
            if row:
                found_id = row['user_id']
                logger.info(f"Found user_id {found_id} by username '{query}'")

        # Если не нашли по username, ищем по точному совпадению first_name (без учета регистра)
        if found_id is None:
            sql_name = 'SELECT user_id FROM user_profiles WHERE LOWER(first_name) = ? LIMIT 1'
            async with conn.execute(sql_name, (query_lower,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    found_id = row['user_id']
                    logger.info(f"Found user_id {found_id} by first_name '{query}'")

        # Если ничего не нашли
        if found_id is None:
            logger.info(f"User ID not found for query: '{query}'")

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error searching for user_id with query '{query}': {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error searching for user_id with query '{query}': {e}", exc_info=True)

    return found_id
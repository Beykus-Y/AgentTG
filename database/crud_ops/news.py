# database/crud_ops/news.py
import aiosqlite
import json
import logging
from typing import List, Dict, Any, Optional, Set
from datetime import datetime, timedelta, timezone # Добавляем timezone

# Используем относительный импорт
try:
    from ..connection import get_connection
except ImportError:
    async def get_connection(): raise ImportError("Connection module not loaded")
    logging.getLogger(__name__).critical("Failed to import get_connection from ..connection")

logger = logging.getLogger(__name__)

# --- Функции для news_subscriptions ---

async def add_or_update_subscription(
    channel_id: int,
    topics: List[str],
    schedule: List[str]
) -> bool:
    """
    Добавляет или обновляет подписку на новости для канала.
    Возвращает True при успехе, False при ошибке.
    """
    if not isinstance(channel_id, int):
        logger.error(f"add_or_update_subscription: Invalid channel_id: {channel_id}")
        return False
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        logger.error(f"add_or_update_subscription: Invalid topics format for channel {channel_id}")
        return False
    if not isinstance(schedule, list) or not all(isinstance(t, str) for t in schedule):
         logger.error(f"add_or_update_subscription: Invalid schedule format for channel {channel_id}")
         return False

    conn: aiosqlite.Connection
    try:
        conn = await get_connection()
        topics_json = json.dumps(topics, ensure_ascii=False)
        schedule_json = json.dumps(schedule, ensure_ascii=False)

        # Используем INSERT ... ON CONFLICT для атомарного добавления/обновления
        await conn.execute('''
            INSERT INTO news_subscriptions (channel_id, topics_json, schedule_json)
            VALUES (?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                topics_json = excluded.topics_json,
                schedule_json = excluded.schedule_json,
                -- last_post_ts не сбрасываем при обновлении тем/расписания, сохраняем старое значение
                last_post_ts = last_post_ts
            WHERE channel_id = excluded.channel_id;
        ''', (channel_id, topics_json, schedule_json))
        await conn.commit()
        logger.info(f"Upserted news subscription for channel_id={channel_id}")
        return True

    except (aiosqlite.Error, ImportError, json.JSONDecodeError) as e:
        logger.error(f"Error upserting news subscription channel={channel_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after news subscription upsert error: {rb_e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error upserting news subscription channel={channel_id}: {e}", exc_info=True)
        return False


async def get_subscription(channel_id: int) -> Optional[Dict[str, Any]]:
    """
    Получает данные подписки для канала.
    Возвращает словарь с 'channel_id', 'topics' (list), 'schedule' (list), 'last_post_ts' (datetime или None) или None.
    """
    if not isinstance(channel_id, int):
        logger.error(f"get_subscription: Invalid channel_id: {channel_id}")
        return None

    conn: aiosqlite.Connection
    subscription_data: Optional[Dict[str, Any]] = None
    try:
        conn = await get_connection()
        async with conn.execute(
            "SELECT channel_id, topics_json, schedule_json, last_post_ts FROM news_subscriptions WHERE channel_id = ?",
            (channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                try:
                    topics = json.loads(row['topics_json'])
                    schedule = json.loads(row['schedule_json'])
                    # Конвертируем строку времени из БД в datetime объект, если она есть
                    last_post_ts_str = row['last_post_ts']
                    last_post_ts = datetime.fromisoformat(last_post_ts_str) if last_post_ts_str else None

                    subscription_data = {
                        "channel_id": row['channel_id'],
                        "topics": topics,
                        "schedule": schedule,
                        "last_post_ts": last_post_ts # Объект datetime или None
                    }
                    logger.debug(f"Retrieved subscription for channel_id={channel_id}")
                except (json.JSONDecodeError, TypeError, ValueError) as parse_error:
                     logger.error(f"Error parsing subscription data channel={channel_id}: {parse_error}")
                     # subscription_data останется None
            else:
                logger.debug(f"No subscription found for channel_id={channel_id}")

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting subscription channel={channel_id}: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error getting subscription channel={channel_id}: {e}", exc_info=True)

    return subscription_data


async def get_all_subscriptions() -> List[Dict[str, Any]]:
    """
    Получает список всех активных подписок.
    Возвращает пустой список при ошибке или отсутствии подписок.
    """
    conn: aiosqlite.Connection
    subscriptions: List[Dict[str, Any]] = []
    try:
        conn = await get_connection()
        async with conn.execute(
            "SELECT channel_id, topics_json, schedule_json, last_post_ts FROM news_subscriptions ORDER BY channel_id"
        ) as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                 try:
                    topics = json.loads(row['topics_json'])
                    schedule = json.loads(row['schedule_json'])
                    last_post_ts_str = row['last_post_ts']
                    last_post_ts = datetime.fromisoformat(last_post_ts_str) if last_post_ts_str else None
                    subscriptions.append({
                        "channel_id": row['channel_id'],
                        "topics": topics,
                        "schedule": schedule,
                        "last_post_ts": last_post_ts
                    })
                 except (json.JSONDecodeError, TypeError, ValueError) as parse_error:
                      logger.error(f"Error parsing subscription JSON during get_all channel={row['channel_id']}: {parse_error}")
                      continue # Пропускаем поврежденную запись

        logger.info(f"Retrieved {len(subscriptions)} news subscriptions.")
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting all subscriptions: {e}", exc_info=True)
    except Exception as e:
        logger.error(f"Unexpected error getting all subscriptions: {e}", exc_info=True)

    return subscriptions


async def update_subscription_last_post(channel_id: int, timestamp: datetime) -> bool:
    """
    Обновляет время последнего поста для подписки.
    Принимает объект datetime.
    """
    if not isinstance(channel_id, int):
        logger.error(f"update_subscription_last_post: Invalid channel_id: {channel_id}")
        return False
    if not isinstance(timestamp, datetime):
         logger.error(f"update_subscription_last_post: Invalid timestamp type for channel {channel_id}: {type(timestamp)}")
         return False

    conn: aiosqlite.Connection
    try:
        conn = await get_connection()
        # Преобразуем datetime в строку ISO формата для записи в БД
        ts_iso_string = timestamp.isoformat()
        cursor = await conn.execute(
            "UPDATE news_subscriptions SET last_post_ts = ? WHERE channel_id = ?",
            (ts_iso_string, channel_id)
        )
        rows_affected = cursor.rowcount
        await conn.commit()
        await cursor.close()
        if rows_affected > 0:
             logger.debug(f"Updated last_post_ts for channel={channel_id} to {ts_iso_string}")
             return True
        else:
             logger.warning(f"Subscription not found to update last_post_ts for channel={channel_id}")
             return False # Подписка не найдена, но ошибки не было
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error updating last_post_ts channel={channel_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after last_post_ts update error: {rb_e}")
        return False
    except Exception as e:
         logger.error(f"Unexpected error updating last_post_ts channel={channel_id}: {e}", exc_info=True)
         return False


async def delete_subscription(channel_id: int) -> bool:
    """Удаляет подписку канала."""
    if not isinstance(channel_id, int):
        logger.error(f"delete_subscription: Invalid channel_id: {channel_id}")
        return False

    conn: aiosqlite.Connection
    success = False
    try:
        conn = await get_connection()
        cursor = await conn.execute("DELETE FROM news_subscriptions WHERE channel_id = ?", (channel_id,))
        deleted_count = cursor.rowcount
        await conn.commit()
        await cursor.close()
        if deleted_count > 0:
            logger.info(f"Deleted news subscription for channel_id={channel_id}")
            success = True
        else:
            logger.info(f"Subscription not found for deletion: channel_id={channel_id}")
            success = False # Запись не найдена, но ошибки не было
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error deleting subscription channel={channel_id}: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after subscription delete error: {rb_e}")
        success = False
    except Exception as e:
         logger.error(f"Unexpected error deleting subscription channel={channel_id}: {e}", exc_info=True)
         success = False
    return success


# --- Функции для работы с sent_news_guids ---

async def add_sent_guid(guid: str) -> bool:
    """Добавляет GUID новости в базу данных. Использует INSERT OR IGNORE."""
    if not guid or not isinstance(guid, str):
        logger.warning("add_sent_guid: Attempted to add empty or invalid GUID.")
        return False

    conn: aiosqlite.Connection
    try:
        conn = await get_connection()
        # Используем CURRENT_TIMESTAMP для времени отправки
        await conn.execute(
            "INSERT OR IGNORE INTO sent_news_guids (guid, sent_ts) VALUES (?, CURRENT_TIMESTAMP)",
            (guid,)
        )
        await conn.commit()
        # Не логируем успех для каждой новости, чтобы не засорять логи
        # logger.debug(f"Added or ignored sent GUID: {guid}")
        return True
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error adding sent GUID '{guid}': {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after adding sent GUID error: {rb_e}")
        return False
    except Exception as e:
         logger.error(f"Unexpected error adding sent GUID '{guid}': {e}", exc_info=True)
         return False


async def is_guid_sent(guid: str) -> bool:
    """Проверяет, был ли уже отправлен GUID."""
    if not guid or not isinstance(guid, str):
        return False # Считаем невалидный GUID "не отправленным"

    conn: aiosqlite.Connection
    is_sent = False
    try:
        conn = await get_connection()
        async with conn.execute("SELECT 1 FROM sent_news_guids WHERE guid = ? LIMIT 1", (guid,)) as cursor:
            row = await cursor.fetchone()
            is_sent = row is not None
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error checking sent GUID '{guid}': {e}", exc_info=True)
    except Exception as e:
         logger.error(f"Unexpected error checking sent GUID '{guid}': {e}", exc_info=True)
    return is_sent


async def load_recent_sent_guids(days: int = 7) -> Set[str]:
    """Загружает GUIDы, отправленные за последние N дней."""
    conn: aiosqlite.Connection
    guids: Set[str] = set()
    try:
        conn = await get_connection()
        # Вычисляем дату N дней назад (с учетом часового пояса UTC)
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
        # Сравниваем с меткой времени в БД (которая должна сохраняться в UTC по умолчанию)
        async with conn.execute(
            "SELECT guid FROM sent_news_guids WHERE sent_ts >= ?",
            (cutoff_date.isoformat(),) # Передаем дату в ISO формате
        ) as cursor:
            rows = await cursor.fetchall()
            guids = {row['guid'] for row in rows}
        logger.info(f"Loaded {len(guids)} recent sent GUIDs (last {days} days).")
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error loading recent sent GUIDs: {e}", exc_info=True)
    except Exception as e:
         logger.error(f"Unexpected error loading recent sent GUIDs: {e}", exc_info=True)
    return guids


async def cleanup_old_guids(days: int = 30) -> int:
    """Удаляет GUIDы старше N дней. Возвращает количество удаленных."""
    conn: aiosqlite.Connection
    deleted_count = 0
    try:
        conn = await get_connection()
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
        cursor = await conn.execute("DELETE FROM sent_news_guids WHERE sent_ts < ?", (cutoff_date.isoformat(),))
        deleted_count = cursor.rowcount
        await conn.commit()
        await cursor.close()
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} old sent GUIDs (older than {days} days).")
        else:
             logger.info(f"No old GUIDs found to cleanup (older than {days} days).")
    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error cleaning up old GUIDs: {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after GUID cleanup error: {rb_e}")
    except Exception as e:
        logger.error(f"Unexpected error cleaning up old GUIDs: {e}", exc_info=True)
    return deleted_count
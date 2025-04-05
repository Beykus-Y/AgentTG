# database/crud_ops/notes.py
import aiosqlite
import json
import logging
from typing import Dict, Any, Optional, List

# Используем относительный импорт
try:
    from ..connection import get_connection
    # Импортируем функции профиля для get_user_data_combined
    from .profiles import get_user_profile
except ImportError:
    async def get_connection(): raise ImportError("Connection module not loaded")
    async def get_user_profile(uid): return None # Заглушка
    logging.getLogger(__name__).critical("Failed to import dependencies from ..connection or .profiles")

logger = logging.getLogger(__name__)

async def upsert_user_note(
    user_id: int,
    category: str,
    value: str,
    merge_lists: bool = True
) -> bool:
    """
    Добавляет или обновляет заметку о пользователе.
    При merge_lists=True обрабатывает JSON-списки/словари для объединения.
    Возвращает True при успехе, False при ошибке.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"upsert_user_note: Invalid user_id: {user_id}")
        return False
    if not category or not isinstance(category, str):
        logger.warning(f"upsert_user_note: Attempted save with empty/invalid category for user {user_id}")
        return False
    if value is None: # Пустая строка допустима, None - нет
        logger.warning(f"upsert_user_note: Attempted save None value for user {user_id}, category '{category}'")
        return False
    if not isinstance(value, str):
         logger.warning(f"upsert_user_note: value not string ({type(value)}), converting for user {user_id}, cat '{category}'")
         value = str(value)

    conn: aiosqlite.Connection
    category_cleaned = category.strip().lower() # Приводим категорию к нижнему регистру
    value_cleaned = value.strip()
    final_value = value_cleaned # Значение для записи по умолчанию

    try:
        conn = await get_connection()
        # Получаем текущее значение, если нужно объединять
        current_data: Optional[Any] = None
        if merge_lists:
            async with conn.execute(
                "SELECT value FROM user_notes WHERE user_id = ? AND category = ?",
                (user_id, category_cleaned)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    try:
                        # Пытаемся распарсить существующее значение
                        current_data = json.loads(row['value'])
                        logger.debug(f"Found existing JSON data for merging: user={user_id}, cat={category_cleaned}")
                    except (json.JSONDecodeError, TypeError):
                        # Если не JSON, объединять не с чем
                        current_data = None
                        logger.debug(f"Existing value is not JSON, cannot merge. user={user_id}, cat={category_cleaned}")

        # Пытаемся объединить, если нужно и возможно
        if merge_lists and current_data is not None:
            try:
                # Пытаемся распарсить новое значение
                new_data = json.loads(value_cleaned)

                if isinstance(current_data, list) and isinstance(new_data, list):
                    # Объединяем списки, добавляя уникальные элементы из нового
                    existing_set = set(json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item for item in current_data)
                    for item in new_data:
                        item_key = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item
                        if item_key not in existing_set:
                            current_data.append(item)
                            existing_set.add(item_key) # Добавляем в set для проверки дубликатов внутри new_data
                    final_value = json.dumps(current_data, ensure_ascii=False)
                    logger.info(f"Merged lists for user={user_id}, cat='{category_cleaned}', new size={len(current_data)}")
                elif isinstance(current_data, dict) and isinstance(new_data, dict):
                    # Обновляем словарь (новые ключи заменят старые)
                    current_data.update(new_data)
                    final_value = json.dumps(current_data, ensure_ascii=False)
                    logger.info(f"Updated dictionary for user={user_id}, cat='{category_cleaned}'")
                else:
                    # Типы не совпадают или не список/словарь - не объединяем
                    logger.debug(f"Cannot merge: type mismatch ({type(current_data)} vs {type(new_data)}) or not list/dict. user={user_id}, cat={category_cleaned}")
                    # final_value остается value_cleaned

            except (json.JSONDecodeError, TypeError):
                # Новое значение не JSON - не объединяем
                logger.debug(f"Cannot merge: new value is not JSON. user={user_id}, cat={category_cleaned}")
                # final_value остается value_cleaned

        # Выполняем INSERT OR REPLACE (или ON CONFLICT DO UPDATE)
        await conn.execute('''
            INSERT INTO user_notes (user_id, category, value, timestamp)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id, category) DO UPDATE SET
                value = excluded.value,
                timestamp = CURRENT_TIMESTAMP
            WHERE user_id = excluded.user_id AND category = excluded.category;
        ''', (user_id, category_cleaned, final_value))
        await conn.commit()
        logger.info(f"Upserted note for user={user_id}: category='{category_cleaned}'")
        return True

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error upserting note user={user_id}, cat='{category_cleaned}': {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after note upsert error: {rb_e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error upserting note user={user_id}, cat='{category_cleaned}': {e}", exc_info=True)
        return False


async def get_user_notes(user_id: int, parse_json: bool = True) -> Dict[str, Any]:
    """
    Получает все заметки о пользователе в виде словаря {категория: значение}.
    Если parse_json=True, пытается автоматически распарсить значения как JSON.
    Возвращает пустой словарь при ошибке или отсутствии заметок.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"get_user_notes: Invalid user_id: {user_id}")
        return {}

    conn: aiosqlite.Connection
    notes: Dict[str, Any] = {}
    try:
        conn = await get_connection()
        async with conn.execute(
            "SELECT category, value FROM user_notes WHERE user_id = ? ORDER BY category",
            (user_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            if not rows:
                logger.debug(f"No notes found for user_id={user_id}")
                return {}

            for row in rows:
                category = row['category'] # Категория уже должна быть lowercase из БД
                value_str = row['value']
                if parse_json:
                    try:
                        # Пробуем распарсить, только если похоже на JSON
                        if value_str and (value_str.startswith('[') or value_str.startswith('{')):
                            notes[category] = json.loads(value_str)
                        else:
                            notes[category] = value_str # Оставляем как строку
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(f"Failed to parse note as JSON user={user_id}, cat='{category}'. Returning as string.")
                        notes[category] = value_str # Возвращаем как строку при ошибке парсинга
                else:
                    notes[category] = value_str

            logger.debug(f"Retrieved {len(notes)} notes for user_id={user_id}")

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error getting notes for user_id {user_id}: {e}", exc_info=True)
        return {} # Возвращаем пустой словарь при ошибке
    except Exception as e:
        logger.error(f"Unexpected error getting notes for user_id {user_id}: {e}", exc_info=True)
        return {}

    return notes


async def delete_user_note(user_id: int, category: str) -> bool:
    """
    Удаляет заметку пользователя по категории (без учета регистра).
    Возвращает True при успехе, False если заметка не найдена или произошла ошибка.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"delete_user_note: Invalid user_id: {user_id}")
        return False
    if not category or not isinstance(category, str):
        logger.warning(f"delete_user_note: Attempted delete with empty/invalid category user={user_id}")
        return False

    conn: aiosqlite.Connection
    success = False
    category_cleaned = category.strip().lower()
    try:
        conn = await get_connection()
        cursor = await conn.execute(
            # Используем LOWER() для сравнения без учета регистра, если в таблице не COLLATE NOCASE
            "DELETE FROM user_notes WHERE user_id = ? AND category = ?",
            (user_id, category_cleaned)
        )
        deleted_count = cursor.rowcount
        await conn.commit()
        await cursor.close()

        if deleted_count > 0:
            logger.info(f"Deleted note user={user_id}, category='{category_cleaned}'")
            success = True
        else:
            logger.info(f"Note not found for deletion: user={user_id}, category='{category_cleaned}'")
            success = False # Явно указываем, что не найдена

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"Error deleting note user={user_id}, cat='{category_cleaned}': {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after note delete error: {rb_e}")
        success = False
    except Exception as e:
        logger.error(f"Unexpected error deleting note user={user_id}, cat='{category_cleaned}': {e}", exc_info=True)
        success = False

    return success


async def delete_user_note_nested(
    user_id: int,
    category: str,
    key: Optional[str] = None,
    list_item: Optional[Any] = None # Принимаем любой тип для list_item
) -> bool:
    """
    Удаляет часть заметки пользователя (ключ из словаря или элемент из списка).
    Если key и list_item не указаны, вызывает delete_user_note.
    Возвращает True при успехе, False при ошибке или если элемент/ключ не найден.
    """
    if not isinstance(user_id, int) or user_id <= 0: return False
    if not category or not isinstance(category, str): return False

    # Если не указано, что удалять внутри, удаляем всю заметку
    if key is None and list_item is None:
        logger.debug(f"Redirecting nested delete to full delete for user={user_id}, cat='{category}'")
        return await delete_user_note(user_id, category)

    conn: aiosqlite.Connection
    success = False
    category_cleaned = category.strip().lower()

    try:
        conn = await get_connection()
        # 1. Получаем текущее значение
        async with conn.execute(
            "SELECT value FROM user_notes WHERE user_id = ? AND category = ?",
            (user_id, category_cleaned)
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            logger.info(f"Note not found for nested delete: user={user_id}, cat='{category_cleaned}'")
            return False

        current_value_str = row['value']

        # 2. Пытаемся распарсить как JSON
        try:
            current_data = json.loads(current_value_str)
            original_data_repr = str(current_data)[:100] # Для логов до изменения
            data_modified = False

            # 3. Удаляем ключ из словаря
            if key is not None and isinstance(current_data, dict):
                if key in current_data:
                    del current_data[key]
                    logger.info(f"Deleted key '{key}' from note dict user={user_id}, cat='{category_cleaned}'")
                    data_modified = True
                else:
                    logger.info(f"Key '{key}' not found in note dict user={user_id}, cat='{category_cleaned}'")
                    return False # Ключ не найден, считаем неудачей

            # 4. Удаляем элемент из списка
            elif list_item is not None and isinstance(current_data, list):
                 initial_len = len(current_data)
                 # Удаляем все вхождения элемента, сравнивая напрямую
                 current_data = [item for item in current_data if item != list_item]
                 if len(current_data) < initial_len:
                      logger.info(f"Removed item '{str(list_item)[:50]}...' from list note user={user_id}, cat='{category_cleaned}'")
                      data_modified = True
                 else:
                      logger.info(f"Item '{str(list_item)[:50]}...' not found in list note user={user_id}, cat='{category_cleaned}'")
                      return False # Элемент не найден

            # 5. Если ни ключ, ни элемент не подходят
            elif key is not None or list_item is not None:
                logger.warning(f"Cannot perform nested deletion: data type ({type(current_data)}) is not dict/list or mismatch for user={user_id}, cat='{category_cleaned}'")
                return False

            # 6. Обновляем или удаляем запись в БД
            if data_modified:
                # Если словарь/список стал пустым после удаления, удаляем всю заметку
                if not current_data:
                    logger.info(f"Data became empty after nested delete user={user_id}, cat='{category_cleaned}'. Deleting full note.")
                    # Закрываем предыдущий курсор перед вызовом delete_user_note
                    # (хотя async with должен был это сделать)
                    await cursor.close()
                    return await delete_user_note(user_id, category_cleaned)
                else:
                    # Обновляем JSON в БД
                    new_value_str = json.dumps(current_data, ensure_ascii=False)
                    # Используем новый курсор для UPDATE
                    await conn.execute(
                        "UPDATE user_notes SET value = ?, timestamp = CURRENT_TIMESTAMP WHERE user_id = ? AND category = ?",
                        (new_value_str, user_id, category_cleaned)
                    )
                    await conn.commit()
                    success = True
            # else: Не было модификаций (ошибка выше или элемент не найден)

        except json.JSONDecodeError:
            logger.warning(f"Cannot perform nested deletion: note value is not valid JSON user={user_id}, cat='{category_cleaned}'")
            return False

    except (aiosqlite.Error, ImportError) as e:
        logger.error(f"DB Error nested delete user={user_id}, cat='{category_cleaned}': {e}", exc_info=True)
        if 'conn' in locals() and conn and not isinstance(e, ImportError):
              try: await conn.rollback()
              except Exception as rb_e: logger.error(f"Rollback failed after nested note delete error: {rb_e}")
        success = False
    except Exception as e:
        logger.error(f"Unexpected error nested delete user={user_id}, cat='{category_cleaned}': {e}", exc_info=True)
        success = False

    return success


async def get_user_data_combined(user_id: int) -> Dict[str, Any]:
    """
    Получает все данные о пользователе из таблиц user_profiles и user_notes.
    Возвращает словарь с ключами 'profile' и 'notes', или пустой словарь при ошибке.
    """
    if not isinstance(user_id, int) or user_id <= 0:
        logger.error(f"get_user_data_combined: Invalid user_id: {user_id}")
        return {}

    result: Dict[str, Any] = {}
    try:
        # Получаем данные профиля
        profile_data = await get_user_profile(user_id)
        if profile_data:
            result["profile"] = profile_data

        # Получаем заметки с автоматическим парсингом JSON
        notes_data = await get_user_notes(user_id, parse_json=True)
        if notes_data:
            result["notes"] = notes_data

        if not result:
             logger.debug(f"No profile or notes found for user_id={user_id}")

    except Exception as e:
        # Логируем ошибку, если она произошла в get_user_profile или get_user_notes
        logger.error(f"Error combining user data for user_id={user_id}: {e}", exc_info=True)
        return {} # Возвращаем пустой словарь при ошибке

    return result
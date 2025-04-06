# database/crud_ops/execution_logs.py

import logging
import json
from typing import Optional, Dict, List, Any

import aiosqlite

# Локальный импорт для получения соединения
try:
    from ..connection import get_connection
except ImportError:
    # Заглушка для тестов или изолированного запуска
    async def get_connection(): raise ImportError("Could not import get_connection from parent package")

# Импорт настроек для лимитов
try:
    from config import settings
except ImportError:
    class MockSettings:
        # Добавляем заглушку для max_command_output_len, если settings не импортируется
        max_command_output_len: int = 4000
    settings = MockSettings()
    logging.warning("Could not import settings from config.py, using default command output len for logs.")

# Определяем максимальную длину для сохранения в лог
# Используем существующую настройку как разумное ограничение
MAX_LOG_LEN = settings.max_command_output_len if hasattr(settings, 'max_command_output_len') else 4000 # Fallback

logger = logging.getLogger(__name__)

async def add_tool_execution_log(
    chat_id: int,
    user_id: Optional[int],
    tool_name: str,
    tool_args: Optional[Dict] = None,
    status: str = 'error',
    return_code: Optional[int] = None,
    result_message: Optional[str] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    full_result: Optional[Dict] = None,
    trigger_message_id: Optional[int] = None
) -> Optional[int]:
    """
    Добавляет запись о выполнении инструмента в таблицу tool_executions.

    Args:
        chat_id (int): ID чата.
        user_id (Optional[int]): ID пользователя, инициировавшего вызов.
        tool_name (str): Название выполненного инструмента.
        tool_args (Optional[Dict]): Аргументы вызова инструмента (сериализуются в JSON).
        status (str): Статус выполнения ('success', 'error', 'not_found', 'warning', 'timeout').
        return_code (Optional[int]): Код возврата (для команд/скриптов).
        result_message (Optional[str]): Сообщение из результата выполнения.
        stdout (Optional[str]): Стандартный вывод (будет обрезан).
        stderr (Optional[str]): Стандартный вывод ошибок (будет обрезан).
        trigger_message_id (Optional[int]): ID сообщения, вызвавшего инструмент.

    Returns:
        Optional[int]: ID созданной записи лога или None при ошибке.
    """
    # Инициализация переменных
    conn: Optional[aiosqlite.Connection] = None
    cursor: Optional[aiosqlite.Cursor] = None
    inserted_id: Optional[int] = None

    try:
        # Сериализация и обрезка данных
        tool_args_json = json.dumps(tool_args, ensure_ascii=False) if tool_args else None
        truncated_stdout = (stdout[:MAX_LOG_LEN] + '...[truncated]') if stdout and len(stdout) > MAX_LOG_LEN else stdout
        truncated_stderr = (stderr[:MAX_LOG_LEN] + '...[truncated]') if stderr and len(stderr) > MAX_LOG_LEN else stderr

        # <<< НОВОЕ: Сериализация полного результата >>>
        full_result_json_str = None
        if full_result is not None:
            try:
                full_result_json_str = json.dumps(full_result, ensure_ascii=False, default=str) # Добавим default=str на всякий случай
            except Exception as json_err:
                logger.error(f"Failed to serialize full_result for tool log '{tool_name}': {json_err}. Storing error message.", exc_info=True)
                full_result_json_str = json.dumps({"error": f"Serialization failed: {json_err}"})

        # Добавляем проверку статуса на допустимые значения
        valid_statuses = {'success', 'error', 'not_found', 'warning', 'timeout'}
        if status not in valid_statuses:
            logger.warning(f"Invalid status '{status}' provided for tool log. Using 'error'.")
            status = 'error'

        # Получение соединения и выполнение запроса
        conn = await get_connection()
        cursor = await conn.execute(
            """
            INSERT INTO tool_executions (
                chat_id, user_id, tool_name, tool_args_json, status,
                return_code, result_message, stdout, stderr, full_result_json,
                trigger_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id, user_id, tool_name, tool_args_json, status,
                return_code, result_message, truncated_stdout, truncated_stderr,
                full_result_json_str, # <-- Теперь это значение соответствует 10-му полю
                trigger_message_id  # <-- Теперь это значение соответствует 11-му полю
            )
        )
        inserted_id = cursor.lastrowid # Получаем ID СРАЗУ после execute
        await conn.commit() # Коммитим
        await cursor.close() # ЗАКРЫВАЕМ КУРСОР ЯВНО
        cursor = None # Сбрасываем ссылку

        if inserted_id is not None:
            # Используем полученный ID в логе
            logger.info(f"Added tool execution log ID: {inserted_id} for tool '{tool_name}' in chat {chat_id}")
        else:
             # Эта ситуация маловероятна с lastrowid после INSERT, но логируем на всякий случай
             logger.warning(f"Could not retrieve lastrowid after inserting tool log for tool '{tool_name}' in chat {chat_id}. INSERT seemed successful.")

        return inserted_id # Возвращаем полученный ID

    except (aiosqlite.Error, json.JSONDecodeError, Exception) as e:
        # Используем f-string для основного сообщения об ошибке
        logger.error(f"Failed to log tool execution for chat={chat_id}, tool={tool_name}: {e}", exc_info=True)
        # Попытка отката, если соединение было установлено
        if conn:
            try: await conn.rollback()
            except Exception as rb_err:
                 logger.error(f"Rollback failed after error logging tool execution: {rb_err}")
        # Попытка закрыть курсор, если он остался открытым
        if cursor:
            try: await cursor.close()
            except Exception as c_err:
                 logger.error(f"Failed to close cursor after error logging tool execution: {c_err}")
        return None # Возвращаем None при любой ошибке

async def get_recent_tool_executions(chat_id: int, limit: int = 3) -> List[Dict[str, Any]]:
    """
    Получает последние N записей логов выполнения инструментов для указанного чата.

    Args:
        chat_id (int): ID чата.
        limit (int): Максимальное количество записей для возврата.

    Returns:
        List[Dict[str, Any]]: Список словарей, представляющих записи логов.
                              Каждый словарь содержит все поля таблицы tool_executions.
                              Возвращает пустой список при ошибке или отсутствии логов.
    """
    conn = await get_connection()
    try:
        async with conn.cursor() as cursor:
            await cursor.execute(
                '''
                SELECT execution_id, chat_id, user_id, timestamp, tool_name, tool_args_json,
                       status, return_code, result_message, stdout, stderr, full_result_json,
                       trigger_message_id
                FROM tool_executions
                WHERE chat_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
                ''',
                (chat_id, limit)
            )
            rows = await cursor.fetchall()
            # Преобразуем строки в словари
            results = [dict(row) for row in rows]
            logger.debug(f"Retrieved {len(results)} recent tool execution logs for chat {chat_id}.")
            return results
    except aiosqlite.Error as e:
        logger.error(f"Error fetching recent tool execution logs for chat {chat_id}: {e}", exc_info=True)
        return []
    except Exception as e:
        logger.error(f"Unexpected error fetching recent tool execution logs for chat {chat_id}: {e}", exc_info=True)
        return [] 
# core_agent/history_manager.py

import logging
import json
import re
from typing import List, Dict, Any, Optional, Tuple, Union

# --- Google Types ---
# Импортируем типы Google. Ошибка импорта здесь критична для работы с моделью.
try:
    from google.ai import generativelanguage as glm
    # RepeatedComposite нужен для корректной обработки типов, возвращаемых Google API
    from google.protobuf.internal.containers import RepeatedComposite
    Content = glm.Content
    Part = glm.Part
    FunctionResponse = glm.FunctionResponse
    FunctionCall = glm.FunctionCall
    logger_types = logging.getLogger(__name__)
    logger_types.debug("Successfully imported Google types and RepeatedComposite.")
except ImportError as e:
    logger_types = logging.getLogger(__name__)
    logger_types.critical(f"CRITICAL: Failed to import Google AI types or protobuf dependencies in history_manager: {e}", exc_info=True)
    # Определяем заглушки, но функционал, связанный с Content/Part, будет нарушен
    RepeatedComposite = Any # Fallback
    Content = Any # Fallback
    Part = Any # Fallback
    FunctionResponse = Any # Fallback
    FunctionCall = Any # Fallback
    # При такой ошибке модуль database, который тоже используется, может сбоить.
    # Возможно, стоит пересмотреть зависимости или сделать импорт database здесь.
    # Но пока оставим import database в отдельном блоке ниже, как было.
    # Это позволит history_manager импортироваться, даже если типы Google не загрузились,
    # но функции prepare_history/save_history должны будут проверить наличие database.

# --- Database Module ---
# Импортируем модуль базы данных. Ошибка здесь критична для хранения истории.
try:
    import database
    logger_db = logging.getLogger(__name__)
    logger_db.debug("Successfully imported database module.")
except ImportError as e:
    logger_db = logging.getLogger(__name__)
    logger_db.critical(f"CRITICAL: Failed to import database module in history_manager: {e}", exc_info=True)
    database = None # type: ignore
    # Этот импорт не должен сбоить из-за ошибки RepeatedComposite, если зависимость прописана верно.
    # Если он сбоит, это другая проблема.

# --- Utility Functions ---
# Импортируем вспомогательные функции. Ошибка здесь также может быть критичной.
try:
    # escape_markdown_v2 и remove_markdown нужны для форматирования логов и ответов
    from utils.helpers import escape_markdown_v2, remove_markdown
    # Функции конвертации для работы с JSON представлением истории в БД
    from utils.converters import _deserialize_parts, reconstruct_content_object, _convert_part_to_dict, _convert_value_for_json
    logger_utils = logging.getLogger(__name__)
    logger_utils.debug("Successfully imported utility functions.")
except ImportError as e:
    logger_utils = logging.getLogger(__name__)
    logger_utils.critical(f"CRITICAL: Failed to import utility functions or converters in history_manager: {e}", exc_info=True)
    # Определяем заглушки для базовой работоспособности
    def escape_markdown_v2(text: Optional[str]) -> str: return text or "" # type: ignore
    def remove_markdown(text: Optional[str]) -> str: return text or "" # type: ignore
    def _deserialize_parts(parts_json: Optional[str]) -> List[Dict[str, Any]]: return [] # type: ignore
    def reconstruct_content_object(role: str, parts_list: List[Dict[str, Any]]) -> Optional[Any]: return None # type: ignore
    def _convert_part_to_dict(part: Any) -> Optional[Dict[str, Any]]: return None # type: ignore
    def _convert_value_for_json(value: Any) -> Any: return str(value) # type: ignore # Грубая заглушка
    logging.warning("Using mock utility functions in history_manager due to import errors.")


logger = logging.getLogger(__name__) # Получаем основной логгер для этого модуля

# Константы для обрезки вывода логов в истории
MAX_LOG_CONTEXT_LEN = 200
MAX_FULL_RESULT_PREVIEW = 500

# --- Функция подготовки истории ---
async def prepare_history(
    chat_id: int,
    user_id: int, # ID ТЕКУЩЕГО пользователя (для заметок и профиля)
    chat_type: Any, # Тип чата для определения, нужны ли префиксы (Any для совместимости с заглушкой)
    add_notes: bool = True, # Флаг, добавлять ли контекст пользователя
    add_recent_logs: bool = True, # Флаг, добавлять ли недавние логи выполнения
    recent_logs_limit: int = 8 # Количество недавних логов для добавления
) -> Tuple[List[Any], int]: # List[Any] т.к. Content может быть заглушкой
    """
    Получает историю из БД, заметки/профиль пользователя, недавние логи выполнения,
    форматирует историю для модели (префиксы).
    *** УДАЛЕНА некорректная логика фильтрации последнего текстового сообщения модели. ***

    Возвращает:
        - final_history_for_api: Список объектов Content (или их заглушек) для model.start_chat().
        - original_db_len: Исходная длина истории из БД (для расчета новых сообщений).
    """
    logger.debug(f"Preparing history for chat={chat_id}, current_user={user_id}, chat_type={chat_type}, add_notes={add_notes}, add_logs={add_recent_logs}, log_limit={recent_logs_limit}")

    # Проверка доступности БД и необходимых утилит
    if database is None or _deserialize_parts is None or reconstruct_content_object is None or escape_markdown_v2 is None:
         logger.critical("Database module or essential utility functions unavailable. Cannot prepare history.")
         return [], 0 # Возвращаем пустую историю и 0 записей

    # 1. Получение данных из БД
    history_from_db: List[Dict[str, Any]] = []
    user_profile: Optional[Dict[str, Any]] = None
    user_notes: Dict[str, Any] = {}
    recent_logs: List[Dict[str, Any]] = []
    original_db_len: int = 0 # Инициализируем
    try:
        # Получаем историю в виде словарей (как она хранится в БД)
        history_from_db = await database.get_chat_history(chat_id) # Ожидаем List[Dict]
        original_db_len = len(history_from_db) # <<< ЗАПОМИНАЕМ ОРИГИНАЛЬНУЮ ДЛИНУ ЗДЕСЬ
        if add_notes:
            # Проверяем наличие функций перед вызовом
            if hasattr(database, 'get_user_profile'): user_profile = await database.get_user_profile(user_id)
            else: logger.warning("Database.get_user_profile unavailable.")
            if hasattr(database, 'get_user_notes'): user_notes = await database.get_user_notes(user_id, parse_json=True)
            else: logger.warning("Database.get_user_notes unavailable.")
        if add_recent_logs and recent_logs_limit > 0:
            if hasattr(database, 'get_recent_tool_executions'): recent_logs = await database.get_recent_tool_executions(chat_id, limit=recent_logs_limit)
            else: logger.warning("Database.get_recent_tool_executions unavailable.")

    except Exception as db_err:
        logger.error(f"DB error during history/profile/notes/logs fetch for chat={chat_id}, user={user_id}: {db_err}", exc_info=True)
        # original_db_len остается 0, если была ошибка
        # Возвращаем пустую историю, чтобы бот не упал
        return [], 0


    # --- УДАЛЕНА логика фильтрации последнего текстового сообщения ---
    # Это была попытка обойти проблему, теперь она не нужна.


    # 2. Формирование истории для модели (список объектов Content)
    # Используем List[Any], так как Content может быть заглушкой
    prepared_history_objects: List[Any] = []

    # --- Добавляем блок RAG (недавние логи) ---
    if add_recent_logs and recent_logs:
        # Используем escape_markdown_v2 для всего, что может содержать спецсимволы из логов
        logs_str_parts = [escape_markdown_v2("~~~Недавние Выполненные Действия~~~")]
        added_log_count = 0
        # Используем reversed, чтобы последние логи были ближе к концу контекста
        for log_entry in reversed(recent_logs):
            tool_name = log_entry.get('tool_name', 'unknown_tool')
            # Не добавляем логи инструментов коммуникации или инструментов,
            # которые могут зацикливать контекст или создают слишком много шума.
            # send_telegram_message, Developer_Feedback уже обрабатываются отдельно.
            # Пропускаем логи поиска, если они уже включены в Deep Search output (например, в future версиях)
            if tool_name in {'send_telegram_message', 'Developer_Feedback', '_perform_web_search_async'}:
                logger.debug(f"History Prep: Skipping log entry for tool '{tool_name}' (filtered).")
                continue

            log_line_parts = []
            # Безопасно получаем время и обрезаем миллисекунды
            ts = escape_markdown_v2(str(log_entry.get('timestamp', 'N/A')).split('.')[0])
            status = escape_markdown_v2(str(log_entry.get('status', 'unknown')))
            msg = log_entry.get('result_message')
            stdout = log_entry.get('stdout')
            stderr = log_entry.get('stderr')
            full_result_json_str = log_entry.get('full_result_json') # Уже строка JSON или None

            log_line_parts.append(f"- [{ts}] **{escape_markdown_v2(tool_name)}** (Статус: **{status}**)")

            # Добавляем сообщение результата, если оно есть и не дублируется в full_result_json
            if msg:
                try:
                    # Проверяем, содержится ли msg в full_result_json_str
                    if full_result_json_str and isinstance(full_result_json_str, str) and msg in full_result_json_str:
                         # logger.debug(f"History Prep: Skipping result_message as it appears in full_result_json for '{tool_name}'.")
                         pass # Пропускаем msg, если он часть полного результата
                    else:
                        truncated_msg = (msg[:MAX_LOG_CONTEXT_LEN] + '...') if len(msg) > MAX_LOG_CONTEXT_LEN else msg
                        log_line_parts.append(f"  - Результат: `{escape_markdown_v2(truncated_msg)}`")
                except Exception as check_msg_err:
                    logger.warning(f"History Prep: Error checking if msg is in full_result_json for '{tool_name}': {check_msg_err}")
                    # В случае ошибки проверки, добавим msg
                    truncated_msg = (msg[:MAX_LOG_CONTEXT_LEN] + '...') if len(msg) > MAX_LOG_CONTEXT_LEN else msg
                    log_line_parts.append(f"  - Результат: `{escape_markdown_v2(truncated_msg)}`")


            if full_result_json_str:
                try:
                    # Пытаемся распарсить и красиво отформатировать JSON
                    # Важно: даже если парсинг тут упадет, full_result_json_str все еще строка и может быть отправлена как raw
                    parsed_result = json.loads(full_result_json_str)
                    # Форматируем с отступами для лучшей читаемости (но больше токенов)
                    formatted_result = json.dumps(parsed_result, indent=2, ensure_ascii=False, default=str) # default=str на всякий случай
                    # Обрезаем, если слишком длинный
                    if len(formatted_result) > MAX_FULL_RESULT_PREVIEW:
                        result_preview = formatted_result[:MAX_FULL_RESULT_PREVIEW] + "\n... [Full Result Truncated]"
                    else:
                        result_preview = formatted_result

                    log_line_parts.append(f"  - Полный Результат (JSON):\n```json\n{escape_markdown_v2(result_preview)}\n```")
                except json.JSONDecodeError:
                    # Если это не JSON, добавляем как текст (обрезанный)
                    preview = (full_result_json_str[:MAX_FULL_RESULT_PREVIEW] + '...[Truncated]') if len(full_result_json_str) > MAX_FULL_RESULT_PREVIEW else full_result_json_str
                    log_line_parts.append(f"  - Полный Результат (Raw):\n```\n{escape_markdown_v2(preview)}\n```")
                except Exception as format_err:
                     logger.warning(f"History Prep: Error formatting full_result_json: {format_err}")
                     # В случае ошибки форматирования, добавим raw строку
                     preview = (str(full_result_json_str)[:MAX_FULL_RESULT_PREVIEW] + '...[Truncated]') if len(str(full_result_json_str)) > MAX_FULL_RESULT_PREVIEW else str(full_result_json_str)
                     log_line_parts.append(f"  - Полный Результат (Error):\n```\n{escape_markdown_v2(preview)}\n```")


            if stdout and (not full_result_json_str or (isinstance(full_result_json_str, str) and stdout not in full_result_json_str)):
                truncated_stdout = (stdout[:MAX_LOG_CONTEXT_LEN] + '... (обрезано)') if len(stdout) > MAX_LOG_CONTEXT_LEN else stdout
                log_line_parts.append(f"  - Вывод (stdout):\n```\n{escape_markdown_v2(truncated_stdout)}\n```")
            if stderr and (not full_result_json_str or (isinstance(full_result_json_str, str) and stderr not in full_result_json_str)):
                truncated_stderr = (stderr[:MAX_LOG_CONTEXT_LEN] + '... (обрезано)') if len(stderr) > MAX_LOG_CONTEXT_LEN else stderr
                log_line_parts.append(f"  - Ошибки (stderr):\n```\n{escape_markdown_v2(truncated_stderr)}\n```")

            # Добавляем пустую строку для разделения логов
            logs_str_parts.append("\n".join(log_line_parts))
            added_log_count += 1

        # Добавляем блок логов, только если есть что добавить
        if added_log_count > 0:
            full_logs_str = "\n\n".join(logs_str_parts)
            try:
                # Создаем Content объект для блока логов
                # Используем glm.Part и glm.Content для надежности, если импортированы
                logs_content = (
                    glm.Content(role="model", parts=[glm.Part(text=full_logs_str)])
                    if all([glm, glm.Part, glm.Content]) else (
                         Content(role="model", parts=[Part(text=full_logs_str)]) if all([Content, Part]) else None
                    )
                )
                if logs_content: prepared_history_objects.append(logs_content)
                else: logger.warning("Failed to create log content object due to missing types.")

                logger.info(f"Added {added_log_count} recent non-communication tool execution logs to history context for chat {chat_id}.")
            except Exception as logs_content_err:
                 logger.error(f"Failed to create Content object for recent logs: {logs_content_err}", exc_info=True)


    # --- Добавляем контекст пользователя (профиль + заметки) ---
    if add_notes and database and hasattr(database, 'get_user_profile') and hasattr(database, 'get_user_notes'):
        user_data_str_parts = []
        context_added = False
        try:
            user_profile = await database.get_user_profile(user_id)
            user_notes = await database.get_user_notes(user_id, parse_json=True)

            if user_profile:
                profile_parts = [f"*Ваш Профиль (User ID: {user_id}):*"] # user_id здесь - ID текущего пользователя
                if user_profile.get('first_name'): profile_parts.append(f"- Имя: {escape_markdown_v2(str(user_profile['first_name']))}")
                if user_profile.get('username'): profile_parts.append(f"- Username: @{escape_markdown_v2(str(user_profile['username']))}")
                if user_profile.get("avatar_description"): profile_parts.append(f"- Аватар: {escape_markdown_v2(str(user_profile['avatar_description']))}")
                user_data_str_parts.append("\n".join(profile_parts))
                context_added = True
            if user_notes:
                notes_str_list = []
                # Используем сортировку по ключам для консистентности
                for cat in sorted(user_notes.keys()):
                     val = user_notes[cat]
                     try:
                          # Если значение является dict или list, сериализуем его красиво
                          if isinstance(val, (dict, list)):
                               val_str = json.dumps(val, ensure_ascii=False, indent=2, default=str) # default=str на всякий случай
                               notes_str_list.append(f"- **{escape_markdown_v2(cat)}** (JSON):\n```json\n{escape_markdown_v2(val_str)}\n```")
                          else:
                               # Иначе, просто конвертируем в строку и экранируем
                               notes_str_list.append(f"- **{escape_markdown_v2(cat)}**: {escape_markdown_v2(str(val))}")
                     except Exception as format_err:
                          logger.warning(f"Error formatting note value for cat '{cat}': {format_err}. Using str().")
                          # В случае ошибки форматирования, просто используем str() и экранируем
                          notes_str_list.append(f"- **{escape_markdown_v2(cat)}**: {escape_markdown_v2(str(val))}")
                if notes_str_list:
                     notes_section = "*Ваши Заметки:*\n" + "\n".join(notes_str_list)
                     user_data_str_parts.append(notes_section)
                     context_added = True

        except Exception as db_notes_err:
             logger.error(f"Error fetching user notes/profile from DB for user {user_id}: {db_notes_err}", exc_info=True)
             # Не прерываем, просто не добавляем контекст

        if context_added:
             full_context_str = escape_markdown_v2("~~~Контекст Текущего Пользователя~~~") + "\n" + "\n\n".join(user_data_str_parts)
             try:
                 # Создаем Content объект для блока контекста
                 context_content = (
                     glm.Content(role="model", parts=[glm.Part(text=full_context_str)])
                     if all([glm, glm.Part, glm.Content]) else (
                         Content(role="model", parts=[Part(text=full_context_str)]) if all([Content, Part]) else None
                     )
                 )
                 if logs_content: prepared_history_objects.append(context_content)
                 else: logger.warning("Failed to create user context content object due to missing types.")

                 logger.info(f"Added combined profile/notes context for current user {user_id} in chat {chat_id}.")
             except Exception as context_err:
                 logger.error(f"Failed to create Content object for user context: {context_err}", exc_info=True)
    elif add_notes:
        logger.warning("Skipping adding user notes/profile context: Database module or necessary functions unavailable.")


    # --- Добавляем историю сообщений из БД (ИЗ ПОЛНОГО СПИСКА) ---
    processed_db_entries_count = 0
    # ИСПОЛЬЗУЕМ history_from_db БЕЗ ФИЛЬТРАЦИИ
    # Проверяем, что reconstruct_content_object и _deserialize_parts доступны
    if reconstruct_content_object and _deserialize_parts:
        for entry in history_from_db:
            role = entry.get("role")
            parts_json_str = entry.get("parts_json") # Получаем строку JSON
            db_user_id = entry.get("user_id") # ID пользователя из БД

            if not role or parts_json_str is None or not isinstance(parts_json_str, str):
                logger.warning(f"History Prep: Skipping DB entry with missing/invalid role or parts_json: {entry}")
                continue

            # НЕ пропускаем 'function' роли здесь. Они нужны модели для FC цикла.

            # Десериализуем JSON строку в список словарей
            parts_list_of_dicts = _deserialize_parts(parts_json_str)

            # Пытаемся реконструировать объект Content из словарей
            reconstructed_content = reconstruct_content_object(role, parts_list_of_dicts)

            # Добавляем реконструированный объект
            # Проверяем, что объект Content успешно создан и имеет правильную роль
            if reconstructed_content and getattr(reconstructed_content, 'role', None) == role:
                 # Добавляем префикс пользователя (если нужно)
                 # Добавляем префикс ТОЛЬКО К СООБЩЕНИЯМ ПОЛЬЗОВАТЕЛЕЙ, если чат не личный
                 if role == 'user' and db_user_id is not None and chat_type != ChatType.PRIVATE:
                     try:
                         # Ищем текстовую часть для добавления префикса и модифицируем ее
                         # Убедимся, что glm и glm.Part доступны, если используем их.
                         if all([glm, glm.Part]):
                             new_parts_for_user_entry = []
                             prefix_added = False
                             for original_part in reconstructed_content.parts:
                                  # Проверяем тип Part
                                  if isinstance(original_part, glm.Part) and hasattr(original_part, 'text') and isinstance(original_part.text, str):
                                      # Создаем новую Part с префиксом
                                      prefixed_text = f"User {db_user_id}: {original_part.text}"
                                      # Создаем новую Part, так как Part из glm могут быть immutable
                                      new_parts_for_user_entry.append(glm.Part(text=prefixed_text))
                                      prefix_added = True
                                  elif isinstance(original_part, glm.Part):
                                      # Сохраняем другие типы частей (FC, FR) без изменений
                                      new_parts_for_user_entry.append(original_part)
                                  # Игнорируем другие типы в parts (если они там оказались)

                             # Если удалось добавить префикс хотя бы к одной текстовой части, заменяем parts у reconstructed_content
                             if prefix_added:
                                  # Создаем новый Content объект с модифицированными частями
                                  reconstructed_content = glm.Content(role='user', parts=new_parts_for_user_entry)
                                  logger.debug(f"Added user prefix for chat {chat_id}, user {db_user_id}.")
                             # else: logger.debug(...) # Нет текстовых частей для префикса

                         elif Content and Part: # Fallback с заглушками Part/Content
                              # Логика с заглушками будет проще, но менее надежной
                              # Можно пропустить добавление префикса в этом случае
                              logger.warning("History Prep: Skipping user prefix due to missing Google types.")
                         else:
                             logger.warning("History Prep: Skipping user prefix due to missing Google or fallback types.")


                     except Exception as prefix_err:
                         logger.error(f"Error adding user prefix to reconstructed content: {prefix_err}", exc_info=True)

                 # Добавляем обработанный (возможно, с префиксом) Content объект
                 prepared_history_objects.append(reconstructed_content)
                 processed_db_entries_count += 1
            else:
                logger.warning(f"History Prep: Skipped DB entry for role '{role}' because reconstruction failed or returned None/wrong role.")
        logger.debug(f"History Prep: Finished processing {processed_db_entries_count} entries from DB history. Prepared {len(prepared_history_objects)} Content objects.")
    else:
        logger.critical("History Prep: Skipping DB history processing: _deserialize_parts or reconstruct_content_object unavailable.")


    final_history_for_api = prepared_history_objects
    # Добавим лог, чтобы видеть, что передается
    logger.debug(f"Final history prepared for API call. Length: {len(final_history_for_api)}")
    # Возвращаем подготовленную историю и исходную длину из БД
    return final_history_for_api, original_db_len

# --- Функция сохранения истории ---
async def save_history(
    chat_id: int,
    final_history_obj_list: Optional[List[Any]], # List[Any] т.к. Content может быть заглушкой
    original_db_history_len: int,
    current_user_id: int,
    last_sent_message_text: Optional[str] = None # Принято для совместимости, сейчас не используется
):
    """
    Сохраняет НОВЫЕ сообщения из final_history в БД chat_history.
    НЕ сохраняет 'user' и 'function' сообщения.
    *** Исправлено: СОХРАНЯЕТ текстовые части 'model' сообщений, даже если они содержат FunctionCall/Response. ***
    Использует original_db_history_len для определения новых сообщений.

    Args:
        chat_id (int): ID чата.
        final_history_obj_list (Optional[List[Content]]): Полный список истории Content объектов
            после взаимодействия с моделью, или None при ошибке.
        original_db_history_len (int): Количество сообщений, которые были *загружены из БД*
            перед этим взаимодействием.
        current_user_id (int): ID пользователя, отправившего последнее сообщение в этом цикле.
        last_sent_message_text (Optional[str]): Текст ПОСЛЕДНЕГО сообщения пользователя,
            которое было отправлено модели в ЭТОМ цикле (сейчас не используется, но принято для совместимости).
    """
    # Проверка доступности БД и необходимых утилит/типов
    if database is None or _convert_part_to_dict is None or _convert_value_for_json is None or Content is Any: # Проверяем, что Content не заглушка
         logger.critical(f"Database module or essential utility functions/types unavailable. Cannot save history for chat {chat_id}.")
         return

    if not final_history_obj_list:
        logger.debug(f"Save History: Received empty or None final_history_obj_list for chat {chat_id}. Nothing to save.")
        return

    # Рассчитываем количество новых элементов
    num_new_items = len(final_history_obj_list) - original_db_history_len

    if num_new_items <= 0:
        logger.debug(f"Save History: No new entries detected in final_history compared to initial history for chat {chat_id} (original_db_len={original_db_history_len}, final_len={len(final_history_obj_list)}). Nothing to save.")
        return

    # Берем только новые элементы в конце списка
    new_history_entries_content = final_history_obj_list[-num_new_items:]

    logger.info(f"Save History: Preparing to save {len(new_history_entries_content)} new entries (detected delta) for chat {chat_id}.")

    save_count = 0
    # Проверяем наличие функции добавления в историю перед циклом
    if not hasattr(database, 'add_message_to_history'):
         logger.critical("Database.add_message_to_history function unavailable. Cannot save history.")
         return

    for entry_content in new_history_entries_content:
        # Проверка, что это Content объект (на всякий случай)
        if not isinstance(entry_content, (glm.Content if glm else Content)):
             logger.warning(f"Save History: Skipping non-Content item in new entries list: {type(entry_content)}")
             continue

        role = getattr(entry_content, 'role', None)
        parts_obj_list = getattr(entry_content, 'parts', None) # Получаем список частей

        if not role or parts_obj_list is None or not isinstance(parts_obj_list, (list, tuple, RepeatedComposite)):
            logger.warning(f"Save History: Skipping invalid Content entry (missing role or parts): {entry_content}")
            continue

        # Пропускаем 'function' и 'user' роли - они сохраняются в других местах или не должны быть в истории чата для модели.
        if role == 'function':
            logger.debug(f"Save History: Skipping role 'function' entry, not saving to chat_history.")
            continue
        if role == 'user':
            logger.debug(f"Save History: Skipping role 'user' entry, should be saved on send.")
            continue


        # --- Обрабатываем только 'model' роль для сохранения ---
        if role == 'model':
            parts_list_of_dicts: List[Dict[str, Any]] = []
            # Переменная has_function_call больше не используется для логики фильтрации

            try:
                # Шаг 1: Конвертируем объекты Part в словари Python
                # Используем _convert_part_to_dict, которая должна обрабатывать все типы Part (text, function_call, function_response)
                # Она возвращает None для пустых или некорректных частей.
                for part_obj in parts_obj_list:
                    converted_part_dict = _convert_part_to_dict(part_obj)
                    if converted_part_dict is not None:
                        parts_list_of_dicts.append(converted_part_dict)
                    else:
                         logger.debug(f"Save History: Skipping part conversion result (None) for role '{role}'. Part object type: {type(part_obj)}")


                # <<< ИСПРАВЛЕНО: Удалена логика условной фильтрации >>>
                # Теперь parts_list_of_dicts содержит все части (текст, FC, FR), которые удалось сконвертировать.
                filtered_parts_for_db = parts_list_of_dicts

                logger.debug(f"Save History: Converted {len(parts_obj_list)} original parts to {len(filtered_parts_for_db)} serializable parts for role '{role}'.")


                # Шаг 2: Сериализуем список словарей в JSON строку
                parts_json_str = "[]" # Значение по умолчанию
                # Сериализуем, только если список не пустой (после конвертации)
                if filtered_parts_for_db:
                    try:
                        parts_json_str = json.dumps(filtered_parts_for_db, ensure_ascii=False, default=str) # default=str на всякий случай
                        logger.debug(f"Save History (model): Serialized parts for DB (size: {len(parts_json_str)}): {parts_json_str[:200]}...")
                    except Exception as serialize_err:
                        logger.error(f"Save History (model): Failed to serialize parts list to JSON: {serialize_err}. Saving empty list.", exc_info=True)
                        parts_json_str = "[]" # Fallback

                # Если после сериализации получился пустой список JSON '[]', возможно, сообщение не содержало ничего полезного
                # Пропускаем сохранение, если JSON пустой, чтобы не засорять историю
                if parts_json_str == "[]":
                    logger.debug(f"Save History (model): Skipping save for chat {chat_id}, role '{role}' because serialized parts resulted in empty JSON '[]'.")
                    continue

                # Шаг 3: Сохраняем в БД
                try:
                    # Вызываем add_message_to_history с уже готовой JSON строкой
                    await database.add_message_to_history(
                        chat_id=chat_id,
                        user_id=current_user_id, # Сохраняем ID пользователя, который инициировал диалог
                        role=role,
                        parts=parts_json_str # Передаем СТРОКУ JSON
                    )
                    logger.info(f"Save History: Successfully saved 'model' entry (incl. text, FC, FR) to chat_history for chat {chat_id}. JSON size: {len(parts_json_str)}.")
                    save_count += 1
                # Обработка ошибок базы данных (остается прежней)
                except TypeError as te:
                     logger.critical(f"Save History: TYPE ERROR calling add_message_to_history for 'model' entry (chat {chat_id}): {te}. Check arguments! Passed parts type: {type(parts_json_str)}, value: {parts_json_str[:100]}...", exc_info=True)
                except AttributeError as ae:
                     logger.critical(f"Save History: ATTRIBUTE ERROR calling DB function for 'model' entry (chat {chat_id}): {ae}. CHECK FUNCTION NAME! Expected 'add_message_to_history'. Parts JSON: {parts_json_str}", exc_info=True)
                except Exception as db_save_err:
                    logger.error(f"Save History: DB Error calling add_message_to_history for 'model' entry (chat {chat_id}): {db_save_err}", exc_info=True)

            except Exception as e: # Ловим любые ошибки на этапе конвертации/сериализации для этой записи
                logger.error(f"Save History: Error processing parts for role '{role}': {e}. Skipping entry.", exc_info=True)
                continue
        else:
            # Если роль не 'user', 'function' и не 'model' (неожиданно)
            logger.warning(f"Save History: Encountered unexpected role '{role}' during save loop. Skipping.")

    logger.info(f"Save History: Finished saving new entries for chat {chat_id}. Saved {save_count} new messages.")

# --- НЕ ОЧИЩАЕМ FC/FR из ИСТОРИИ после сохранения ---
# Это больше не нужно, т.к. мы сохраняем их в БД, и prepare_history
# реконструирует их обратно в Content объекты для API.
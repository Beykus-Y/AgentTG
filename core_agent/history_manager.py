import logging
import json
import re
from typing import List, Dict, Any, Optional, Tuple

# --- Зависимости ---
try:
    # Импортируем весь модуль database для доступа к CRUD функциям
    import database
    # Импортируем утилиты
    from utils.helpers import escape_markdown_v2
    # Используем абсолютный импорт от корня проекта для utils
    from utils.converters import _deserialize_parts, reconstruct_content_object, _convert_part_to_dict
except ImportError as e:
    logging.critical(f"CRITICAL: Failed to import core dependencies in history_manager: {e}", exc_info=True)
    # В реальном приложении здесь может быть выход или обработка ошибки
    raise

# Типы данных
try:
    from aiogram.enums import ChatType
    # <<< ВОЗВРАЩАЕМ glm >>>
    from google.ai import generativelanguage as glm
    Content = glm.Content
    Part = glm.Part # Добавляем Part
except ImportError:
    ChatType = Any
    Content = Any
    Part = Any # Добавляем Part в fallback
    logging.getLogger(__name__).warning("Could not import specific types (ChatType, Content, Part) in history_manager.")

logger = logging.getLogger(__name__)

# Константа для обрезки вывода логов в истории
MAX_LOG_CONTEXT_LEN = 200
MAX_FULL_RESULT_PREVIEW = 500
# --- Функция подготовки истории ---
async def prepare_history(
    chat_id: int,
    user_id: int, # ID ТЕКУЩЕГО пользователя (для заметок и профиля)
    chat_type: ChatType, # Тип чата для определения, нужны ли префиксы
    add_notes: bool = True, # Флаг, добавлять ли контекст пользователя
    add_recent_logs: bool = True, # Флаг, добавлять ли недавние логи выполнения
    recent_logs_limit: int = 4 # Количество недавних логов для добавления
) -> Tuple[List[Content], int]:
    """
    Получает историю из БД, заметки/профиль пользователя, недавние логи выполнения,
    форматирует историю для модели (префиксы, очистка FC/FR).
    *** Добавлена логика для пропуска последнего текстового сообщения модели,
        если оно следует сразу за сообщением модели с FC/FR, чтобы предотвратить зацикливание API. ***

    Возвращает:
        - final_history_for_api: Список объектов google.ai.generativelanguage.Content для model.start_chat().
        - original_db_len: Исходная длина истории из БД (для расчета новых сообщений).
    """
    logger.debug(f"Preparing history for chat={chat_id}, current_user={user_id}, chat_type={chat_type}, add_notes={add_notes}, add_logs={add_recent_logs}, log_limit={recent_logs_limit}")

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
            user_profile = await database.get_user_profile(user_id)
            user_notes = await database.get_user_notes(user_id, parse_json=True)
        if add_recent_logs and recent_logs_limit > 0:
            recent_logs = await database.get_recent_tool_executions(chat_id, limit=recent_logs_limit)
    except Exception as db_err:
        logger.error(f"DB error during history/profile/notes/logs fetch for chat={chat_id}, user={user_id}: {db_err}", exc_info=True)
        # original_db_len остается 0, если была ошибка

    # <<< НАЧАЛО НОВОГО БЛОКА ФИЛЬТРАЦИИ >>>
    history_to_process = history_from_db # По умолчанию обрабатываем всю историю
    skip_last_model_text = False

    if len(history_from_db) >= 2:
        last_entry = history_from_db[-1]
        second_last_entry = history_from_db[-2]

        # Проверяем, что последние два сообщения - от модели
        if last_entry.get("role") == "model" and second_last_entry.get("role") == "model":
            # Проверяем, содержал ли предпоследний ответ FC или FR
            second_last_parts = second_last_entry.get("parts", [])
            second_last_had_fc_fr = any(
                isinstance(part, dict) and ('function_call' in part or 'function_response' in part)
                for part in second_last_parts
            )

            # Проверяем, содержит ли последний ответ ТОЛЬКО текст (и текст не пустой)
            last_parts = last_entry.get("parts", [])
            last_has_only_text = False
            if last_parts and all(isinstance(part, dict) for part in last_parts):
                 last_has_only_text = (
                     all('text' in part and not ('function_call' in part or 'function_response' in part)
                         for part in last_parts)
                     and any(part.get('text') for part in last_parts) # Убедимся, что текст НЕ пустой
                 )

            if second_last_had_fc_fr and last_has_only_text:
                skip_last_model_text = True
                logger.info("History Prep Filter: Skipping last model's text-only message after FC/FR to prevent potential API loop.")
                history_to_process = history_from_db[:-1] # Используем историю БЕЗ последнего сообщения для API
            else:
                 logger.debug(f"History Prep Filter: Pattern not matched. second_last_had_fc_fr={second_last_had_fc_fr}, last_has_only_text={last_has_only_text}")
        else:
             logger.debug("History Prep Filter: Last two entries are not both 'model'.")
    # <<< КОНЕЦ НОВОГО БЛОКА ФИЛЬТРАЦИИ >>>

    # 2. Формирование истории для модели (список объектов Content)
    prepared_history_objects: List[Content] = []

    # --- Добавляем блок RAG (недавние логи) ---
    if add_recent_logs and recent_logs:
        logs_str_parts = ["\\~\\~\\~Недавние Выполненные Действия\\~\\~\\~"]
        added_log_count = 0
        for log_entry in reversed(recent_logs):
            # ... (остальной код форматирования логов) ...
            tool_name = log_entry.get('tool_name', 'unknown_tool')
            if tool_name == 'send_telegram_message':
                logger.debug(f"History Prep: Skipping log entry for tool '{tool_name}' (communication tool).")
                continue
            # ... (остальной код форматирования) ...
            log_line_parts = []
            ts = log_entry.get('timestamp', 'N/A').split('.')[0]
            status = log_entry.get('status', 'unknown')
            msg = log_entry.get('result_message')
            stdout = log_entry.get('stdout')
            stderr = log_entry.get('stderr')
            full_result_json = log_entry.get('full_result_json')
            log_line_parts.append(f"- [{ts}] **{escape_markdown_v2(tool_name)}** (Статус: **{escape_markdown_v2(status)}**)")
            if msg and (not full_result_json or msg not in full_result_json):
                truncated_msg = (msg[:MAX_LOG_CONTEXT_LEN] + '...') if len(msg) > MAX_LOG_CONTEXT_LEN else msg
                log_line_parts.append(f"  - Результат: `{escape_markdown_v2(truncated_msg)}`")

            if full_result_json:
                try:
                    # Пытаемся распарсить и красиво отформатировать JSON
                    parsed_result = json.loads(full_result_json)
                    # Форматируем с отступами для лучшей читаемости (но больше токенов)
                    formatted_result = json.dumps(parsed_result, indent=2, ensure_ascii=False, default=str)
                    # Обрезаем, если слишком длинный
                    if len(formatted_result) > MAX_FULL_RESULT_PREVIEW:
                        result_preview = formatted_result[:MAX_FULL_RESULT_PREVIEW] + "\n... [Full Result Truncated]"
                    else:
                        result_preview = formatted_result

                    log_line_parts.append(f"  - Полный Результат (JSON):\n```json\n{escape_markdown_v2(result_preview)}\n```")
                except json.JSONDecodeError:
                    # Если это не JSON, добавляем как текст (обрезанный)
                    preview = (full_result_json[:MAX_FULL_RESULT_PREVIEW] + '...[Truncated]') if len(full_result_json) > MAX_FULL_RESULT_PREVIEW else full_result_json
                    log_line_parts.append(f"  - Полный Результат (Raw):\n```\n{escape_markdown_v2(preview)}\n```")
                except Exception as format_err:
                     logger.warning(f"History Prep: Error formatting full_result_json: {format_err}")
                     preview = (str(full_result_json)[:MAX_FULL_RESULT_PREVIEW] + '...[Truncated]') if len(str(full_result_json)) > MAX_FULL_RESULT_PREVIEW else str(full_result_json)
                     log_line_parts.append(f"  - Полный Результат (Error):\n```\n{escape_markdown_v2(preview)}\n```")



            if stdout and (not full_result_json or stdout not in full_result_json):
                truncated_stdout = (stdout[:MAX_LOG_CONTEXT_LEN] + '... (обрезано)') if len(stdout) > MAX_LOG_CONTEXT_LEN else stdout
                log_line_parts.append(f"  - Вывод (stdout):\n```\n{escape_markdown_v2(truncated_stdout)}\n```")
            if stderr and (not full_result_json or stderr not in full_result_json):
                truncated_stderr = (stderr[:MAX_LOG_CONTEXT_LEN] + '... (обрезано)') if len(stderr) > MAX_LOG_CONTEXT_LEN else stderr
                log_line_parts.append(f"  - Ошибки (stderr):\n```\n{escape_markdown_v2(truncated_stderr)}\n```")
            logs_str_parts.append("\n".join(log_line_parts))
            added_log_count += 1

            
        if added_log_count > 0:
            full_logs_str = "\n\n".join(logs_str_parts)
            try:
                logs_content = Content(role="model", parts=[glm.Part(text=full_logs_str)])
                prepared_history_objects.append(logs_content)
                logger.info(f"Added {added_log_count} recent non-communication tool execution logs to history context for chat {chat_id}.")
            except Exception as logs_content_err:
                 logger.error(f"Failed to create Content object for recent logs: {logs_content_err}", exc_info=True)
        else:
             logger.info(f"No relevant (non-communication) tool execution logs found to add to history context for chat {chat_id}.")

    # --- Добавляем контекст пользователя (профиль + заметки) ---
    if add_notes:
        user_data_str_parts = []
        context_added = False
        if user_profile:
            profile_parts = [f"*Ваш Профиль (User ID: {user_id}):*"]
            if user_profile.get('first_name'): profile_parts.append(f"- Имя: {escape_markdown_v2(str(user_profile['first_name']))}")
            if user_profile.get('username'): profile_parts.append(f"- Username: @{escape_markdown_v2(str(user_profile['username']))}")
            if user_profile.get("avatar_description"): profile_parts.append(f"- Аватар: {escape_markdown_v2(str(user_profile['avatar_description']))}")
            user_data_str_parts.append("\n".join(profile_parts))
            context_added = True
        if user_notes:
            notes_str_list = []
            for cat, val in user_notes.items():
                 try:
                      if isinstance(val, (dict, list)):
                           val_str = json.dumps(val, ensure_ascii=False, indent=2)
                           notes_str_list.append(f"- **{escape_markdown_v2(cat)}** (JSON):\n```json\n{val_str}\n```")
                      else:
                           notes_str_list.append(f"- **{escape_markdown_v2(cat)}**: {escape_markdown_v2(str(val))}")
                 except Exception as format_err:
                      logger.warning(f"Error formatting note value for cat '{cat}': {format_err}. Using str().")
                      notes_str_list.append(f"- **{escape_markdown_v2(cat)}**: {escape_markdown_v2(str(val))}")
            if notes_str_list:
                 notes_section = "*Ваши Заметки:*\n" + "\n".join(notes_str_list)
                 user_data_str_parts.append(notes_section)
                 context_added = True
        if context_added:
             full_context_str = "\\~\\~\\~Контекст Текущего Пользователя\\~\\~\\~\n" + "\n\n".join(user_data_str_parts)
             try:
                 context_content = Content(role="model", parts=[glm.Part(text=full_context_str)])
                 prepared_history_objects.append(context_content)
                 logger.info(f"Added combined profile/notes context for current user {user_id} in chat {chat_id}.")
             except Exception as context_err:
                 logger.error(f"Failed to create Content object for user context: {context_err}", exc_info=True)

    # --- Добавляем историю сообщений из БД (ИЗ ОТФИЛЬТРОВАННОГО СПИСКА) ---
    processed_db_entries_count = 0
    for entry in history_to_process: # <<< ИЗМЕНЕНИЕ: Используем history_to_process
        role = entry.get("role")
        # parts теперь уже список словарей Python
        parts_list_of_dicts = entry.get("parts")
        db_user_id = entry.get("user_id")

        if not role or parts_list_of_dicts is None or not isinstance(parts_list_of_dicts, list):
            logger.warning(f"Skipping history entry from DB with missing/invalid role or parts: {entry}")
            continue

        # Пропускаем 'function' роли сразу
        if role == 'function':
            logger.debug(f"History Prep: Skipping 'function' role entry from DB.")
            continue

        # Пытаемся реконструировать объект Content
        reconstructed_content = reconstruct_content_object(role, parts_list_of_dicts)

        if reconstructed_content is None:
            logger.warning(f"Skipping history entry for role '{role}' because reconstruction failed (entry data: {entry}).")
            continue
        if not reconstructed_content.parts:
             # Обрабатываем случай, когда реконструкция удалась, но частей нет (например, пустой model)
             if role == 'model':
                  logger.debug(f"History Prep: Reconstructed empty 'model' entry for chat {chat_id}.")
                  # Добавляем пустой объект Content(role='model') для сохранения структуры
                  try:
                      prepared_history_objects.append(Content(role='model', parts=[]))
                      processed_db_entries_count += 1
                  except Exception as empty_create_err:
                       logger.error(f"History Prep: Error creating empty model content: {empty_create_err}")
             else:
                  logger.warning(f"Skipping history entry for role '{role}' because reconstructed content has empty parts.")
             continue # Пропускаем запись без частей (кроме model)

        

        # Добавляем в итоговый список, если объект все еще валиден
        if reconstructed_content:
            # Добавляем префикс пользователя (если нужно)
            if role == 'user' and db_user_id is not None and chat_type != ChatType.PRIVATE:
                try:
                    prefix_added = False
                    for part in reconstructed_content.parts:
                        if hasattr(part, 'text') and isinstance(part.text, str):
                            part.text = f"User {db_user_id}: {part.text}"
                            prefix_added = True
                            break
                    if not prefix_added:
                         logger.warning(f"Could not add user prefix for chat {chat_id}, user {db_user_id}: No text part found.")
                except Exception as prefix_err:
                    logger.error(f"Error adding user prefix to reconstructed content: {prefix_err}", exc_info=True)

            prepared_history_objects.append(reconstructed_content)
            processed_db_entries_count += 1

    logger.debug(f"History Prep: Finished processing {processed_db_entries_count} entries from filtered DB history.")

    # --- Очищаем FunctionCall/FunctionResponse ИЗ ПОДГОТОВЛЕННОЙ ИСТОРИИ для API ---
    final_history_for_api: List[Content] = []
    for entry in prepared_history_objects:
        if entry.role == 'model' and entry.parts:
            cleaned_parts = [
                part for part in entry.parts
                if not (hasattr(part, 'function_call') and part.function_call is not None)
                and not (hasattr(part, 'function_response') and part.function_response is not None) # <<< ДОБАВЛЕНО: Удаление FR >>>
            ]
            # Добавляем сообщение модели только если оно не стало пустым после очистки FC/FR
            if cleaned_parts:
                try:
                    cleaned_entry = Content(role='model', parts=cleaned_parts)
                    final_history_for_api.append(cleaned_entry)
                    logger.debug(f"History Prep API: Kept model entry (FC/FR cleaned).")
                except Exception as clean_err:
                    logger.error(f"History Prep API: Error creating cleaned model entry: {clean_err}. Skipping.")
            else:
                logger.debug(f"History Prep API: Skipping model entry that only contained FC/FR.")
        else:
            # Добавляем все остальные сообщения (user, system, пустые model) как есть
            final_history_for_api.append(entry)

    logger.debug(f"Final history length prepared for API call: {len(final_history_for_api)}")
    # Возвращаем подготовленную историю и исходную длину из БД
    return final_history_for_api, original_db_len

# --- Функция сохранения истории ---
async def save_history(
    chat_id: int,
    final_history_obj_list: Optional[List[Content]],
    original_db_history_len: int,
    current_user_id: int,
    last_sent_message_text: Optional[str] = None
):
    """
    Сохраняет НОВЫЕ сообщения из final_history в БД chat_history.
    НЕ сохраняет 'user' и 'function' сообщения.
    НЕ удаляет 'FunctionCall' или 'FunctionResponse' части из 'model' сообщений перед сохранением.
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
    if not final_history_obj_list:
        logger.warning(f"Save History: Received empty or None final_history_obj_list for chat {chat_id}. Nothing to save.")
        return

    # Рассчитываем количество новых элементов, как и раньше
    num_new_items = len(final_history_obj_list) - original_db_history_len

    if num_new_items <= 0:
        logger.debug(f"Save History: No new entries detected in final_history compared to initial history for chat {chat_id} (original_db_len={original_db_history_len}, final_len={len(final_history_obj_list)}). Nothing to save.")
        return

    new_history_entries_content = final_history_obj_list[-num_new_items:]

    logger.info(f"Save History: Preparing to save {len(new_history_entries_content)} new entries (detected delta) for chat {chat_id}.")

    save_count = 0
    for entry_content in new_history_entries_content:
        role = entry_content.role
        parts_obj_list = entry_content.parts

        if not role:
            logger.warning(f"Save History: Skipping invalid Content entry (no role): {entry_content}")
            continue

        # Пропускаем 'function' и 'user' роли
        if role == 'function':
            logger.debug(f"Save History: Skipping role 'function' entry, not saving to chat_history.")
            continue
        if role == 'user':
            logger.debug(f"Save History: Skipping role 'user' entry, should be saved on send.")
            continue

        # Обрабатываем только 'model' роль для сохранения
        if role == 'model':
            parts_list_of_dicts: List[Dict[str, Any]] = []
            try:
                # Шаг 1: Конвертируем объекты Part в словари Python
                for part_obj in parts_obj_list:
                    part_dict = _convert_part_to_dict(part_obj)
                    if part_dict:
                        parts_list_of_dicts.append(part_dict)
                    else:
                         logger.warning(f"Save History: Skipping part conversion result (None) for role '{role}'. Part object: {part_obj}")

                # Шаг 2: Сериализуем ПОЛНЫЙ список словарей (включая FC/FR) в JSON строку
                parts_json_str = "[]" # Значение по умолчанию
                if parts_list_of_dicts: # Сериализуем, только если список не пустой
                    try:
                        # Сериализуем исходный список, включая function_call/function_response
                        parts_json_str = json.dumps(parts_list_of_dicts, ensure_ascii=False)
                        logger.debug(f"Save History (model): Serialized parts (incl. FC/FR) for DB: {parts_json_str[:200]}...") # Обновлен лог
                    except Exception as serialize_err:
                        logger.error(f"Save History (model): Failed to serialize parts list (incl. FC/FR) to JSON: {serialize_err}. Saving empty list.", exc_info=True) # Обновлен лог
                        parts_json_str = "[]" # Fallback
                else:
                     logger.debug(f"Save History (model): No valid parts to serialize for chat {chat_id} (original list was empty or conversion failed). Saving empty list JSON '[]'.")

                # Шаг 3: Сохраняем в БД
                try:
                    await database.add_message_to_history(
                        chat_id=chat_id,
                        user_id=current_user_id, # Используем ID пользователя текущего цикла
                        role=role,
                        parts=parts_json_str # Передаем (потенциально содержащую FC/FR) СТРОКУ JSON
                    )
                    logger.info(f"Save History: Successfully saved 'model' entry (incl. FC/FR) to chat_history for chat {chat_id}.") # <-- Обновлен лог
                    save_count += 1
                # ... (обработка ошибок DB остается такой же) ...
                except TypeError as te:
                     logger.critical(f"Save History: TYPE ERROR calling add_message_to_history for 'model' entry (chat {chat_id}): {te}. Check arguments! Passed parts type: {type(parts_json_str)}, value: {parts_json_str[:100]}...", exc_info=True)
                except AttributeError as ae:
                     logger.critical(f"Save History: ATTRIBUTE ERROR calling DB function for 'model' entry (chat {chat_id}): {ae}. CHECK FUNCTION NAME! Expected 'add_message_to_history'. Parts JSON: {parts_json_str}", exc_info=True)
                except Exception as db_save_err:
                    logger.error(f"Save History: DB Error calling add_message_to_history for 'model' entry (chat {chat_id}): {db_save_err}", exc_info=True)

            except Exception as e: # Ловим ошибки на этапе конвертации
                logger.error(f"Save History: Error processing parts for role '{role}': {e}. Skipping entry.", exc_info=True)
                continue
        else:
            logger.warning(f"Save History: Encountered unexpected role '{role}' during save loop. Skipping.")

    logger.info(f"Save History: Finished saving new entries for chat {chat_id}. Saved {save_count} new messages.")
# core_agent/history_manager.py

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
    from utils.converters import gemini_history_to_dict_list, _convert_part_to_dict, _serialize_parts, _deserialize_parts, reconstruct_content_object
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
except ImportError:
    ChatType = Any
    Content = Any
    logging.getLogger(__name__).warning("Could not import specific types (ChatType, Content) in history_manager.")

logger = logging.getLogger(__name__)

# Константа для обрезки вывода логов в истории
MAX_LOG_CONTEXT_LEN = 200

# --- Функция подготовки истории ---
async def prepare_history(
    chat_id: int,
    user_id: int, # ID ТЕКУЩЕГО пользователя (для заметок и профиля)
    chat_type: ChatType, # Тип чата для определения, нужны ли префиксы
    add_notes: bool = True, # Флаг, добавлять ли контекст пользователя
    add_recent_logs: bool = True, # Флаг, добавлять ли недавние логи выполнения
    recent_logs_limit: int = 2 # Количество недавних логов для добавления
) -> Tuple[List[Content], int]:
    """
    Получает историю из БД, заметки/профиль пользователя, недавние логи выполнения,
    форматирует историю для модели (префиксы, очистка FC/FR), проверяет последний элемент.

    Возвращает:
        - prepared_history_objects: Список объектов google.ai.generativelanguage.Content для model.start_chat().
        - original_history_len_for_save: Исходная длина истории из БД (для расчета новых сообщений).
    """
    logger.debug(f"Preparing history for chat={chat_id}, current_user={user_id}, chat_type={chat_type}, add_notes={add_notes}, add_logs={add_recent_logs}, log_limit={recent_logs_limit}")

    # 1. Получение данных из БД
    history_from_db: List[Dict[str, Any]] = []
    user_profile: Optional[Dict[str, Any]] = None
    user_notes: Dict[str, Any] = {}
    recent_logs: List[Dict[str, Any]] = []
    try:
        history_from_db = await database.get_chat_history(chat_id)
        # Загружаем профиль и заметки ТЕКУЩЕГО пользователя, если нужно
        if add_notes:
            user_profile = await database.get_user_profile(user_id)
            user_notes = await database.get_user_notes(user_id, parse_json=True)
        # Загружаем недавние логи, если нужно
        if add_recent_logs and recent_logs_limit > 0:
            recent_logs = await database.get_recent_tool_executions(chat_id, limit=recent_logs_limit)
    except Exception as db_err:
        logger.error(f"DB error during history/profile/notes/logs fetch for chat={chat_id}, user={user_id}: {db_err}", exc_info=True)
        # Продолжаем с тем, что есть (пустыми данными, если ничего не загрузилось)

    # Логирование сырой истории из БД (опционально, для отладки)
    # try: ... (код логирования history_from_db) ...

    # 2. Формирование истории для модели (теперь как список объектов Content)
    prepared_history_objects: List[Content] = []

        # <<< УЛУЧШЕННЫЙ БЛОК ЛОГОВ >>>
    if add_recent_logs and recent_logs:
        logs_str_parts = ["\\~\\~\\~Недавние Выполненные Действия\\~\\~\\~"]
        added_log_count = 0 # Счетчик добавленных (не отфильтрованных) логов

        # Разворачиваем логи, чтобы самые новые были в конце блока
        for log_entry in reversed(recent_logs):
            tool_name = log_entry.get('tool_name', 'unknown_tool') # <-- Сначала извлекаем имя

            # --- УСЛОВИЕ: Пропускаем логи send_telegram_message ---
            if tool_name == 'send_telegram_message':
                logger.debug(f"History Prep: Skipping log entry for tool '{tool_name}' (communication tool).")
                continue # Переходим к следующему логу
            # --- КОНЕЦ УСЛОВИЯ ---

            # --- Остальная часть форматирования лога ---
            log_line_parts = []
            ts = log_entry.get('timestamp', 'N/A').split('.')[0]
            status = log_entry.get('status', 'unknown')
            msg = log_entry.get('result_message')
            stdout = log_entry.get('stdout')
            stderr = log_entry.get('stderr')

            # Форматируем базовую информацию
            log_line_parts.append(f"- [{ts}] **{escape_markdown_v2(tool_name)}** (Статус: **{escape_markdown_v2(status)}**)")

            # Добавляем сообщение результата (обрезанное), если оно есть
            if msg:
                truncated_msg = (msg[:MAX_LOG_CONTEXT_LEN] + '...') if len(msg) > MAX_LOG_CONTEXT_LEN else msg
                log_line_parts.append(f"  - Результат: `{escape_markdown_v2(truncated_msg)}`")

            # Добавляем stdout (обрезанный), если есть
            if stdout:
                truncated_stdout = (stdout[:MAX_LOG_CONTEXT_LEN] + '... (обрезано)') if len(stdout) > MAX_LOG_CONTEXT_LEN else stdout
                log_line_parts.append(f"  - Вывод:\n```\n{escape_markdown_v2(truncated_stdout)}\n```")

            # Добавляем stderr (обрезанный), если есть
            if stderr:
                truncated_stderr = (stderr[:MAX_LOG_CONTEXT_LEN] + '... (обрезано)') if len(stderr) > MAX_LOG_CONTEXT_LEN else stderr
                log_line_parts.append(f"  - Ошибки:\n```\n{escape_markdown_v2(truncated_stderr)}\n```")

            logs_str_parts.append("\n".join(log_line_parts))
            added_log_count += 1 # Увеличиваем счетчик добавленных логов

        # --- ИСПРАВЛЕНО: Добавляем блок, только если есть что добавить (кроме заголовка) ---
        if added_log_count > 0:
            full_logs_str = "\n\n".join(logs_str_parts)
            try:
                # Добавляем как сообщение 'model'
                logs_content = Content(role="model", parts=[glm.Part(text=full_logs_str)])
                prepared_history_objects.append(logs_content)
                # --- ИСПРАВЛЕНО: Логируем правильное количество ---
                logger.info(f"Added {added_log_count} recent non-communication tool execution logs to history context for chat {chat_id}.")
            except Exception as logs_content_err:
                 logger.error(f"Failed to create Content object for recent logs: {logs_content_err}", exc_info=True)
        else:
             # Логируем, если все логи были отфильтрованы
             logger.info(f"No relevant (non-communication) tool execution logs found to add to history context for chat {chat_id}.")
    # <<< КОНЕЦ БЛОКА ЛОГОВ >>>

    # Добавляем контекст пользователя (профиль + заметки), если нужно
    if add_notes:
        user_data_str_parts = []
        context_added = False
        if user_profile:
            profile_parts = [f"*Ваш Профиль (User ID: {user_id}):*"] # Уточняем, что это профиль ТЕКУЩЕГО юзера
            if user_profile.get('first_name'): profile_parts.append(f"- Имя: {escape_markdown_v2(str(user_profile['first_name']))}")
            if user_profile.get('username'): profile_parts.append(f"- Username: @{escape_markdown_v2(str(user_profile['username']))}")
            if user_profile.get("avatar_description"): profile_parts.append(f"- Аватар: {escape_markdown_v2(str(user_profile['avatar_description']))}")
            # Добавить другие поля профиля при необходимости
            user_data_str_parts.append("\n".join(profile_parts))
            context_added = True

        if user_notes:
            notes_str_list = []
            for cat, val in user_notes.items():
                 try:
                      if isinstance(val, (dict, list)): # Форматируем JSON
                           # Не экранируем сам JSON, т.к. он внутри ```
                           val_str = json.dumps(val, ensure_ascii=False, indent=2)
                           notes_str_list.append(f"- **{escape_markdown_v2(cat)}** (JSON):\n```json\n{val_str}\n```")
                      else: # Обычный текст
                           notes_str_list.append(f"- **{escape_markdown_v2(cat)}**: {escape_markdown_v2(str(val))}")
                 except Exception as format_err:
                      logger.warning(f"Error formatting note value for cat '{cat}': {format_err}. Using str().")
                      notes_str_list.append(f"- **{escape_markdown_v2(cat)}**: {escape_markdown_v2(str(val))}")

            if notes_str_list:
                 notes_section = "*Ваши Заметки:*\n" + "\n".join(notes_str_list) # Уточняем, что это заметки ТЕКУЩЕГО юзера
                 user_data_str_parts.append(notes_section)
                 context_added = True

        if context_added:
             full_context_str = "\\~\\~\\~Контекст Текущего Пользователя\\~\\~\\~\n" + "\n\n".join(user_data_str_parts)
             # Важно: Контекст обычно добавляется как 'model', чтобы он не считался вводом пользователя
             try:
                 context_content = Content(role="model", parts=[glm.Part(text=full_context_str)])
                 prepared_history_objects.append(context_content)
                 logger.info(f"Added combined profile/notes context for current user {user_id} in chat {chat_id}.")
             except Exception as context_err:
                 logger.error(f"Failed to create Content object for user context: {context_err}", exc_info=True)

    # <<< ИЗМЕНЕНИЕ: Запоминаем длину ИСТОРИИ ИЗ БД до добавления реальных сообщений >>>
    original_db_len = len(history_from_db)

    # Добавляем историю сообщений из БД, форматируя для модели
    processed_db_entries_count = 0 # Счетчик успешно обработанных записей из БД
    for entry in history_from_db:
        role = entry.get("role")
        # <<< ВОЗВРАЩАЕМ: Чтение parts_json как СТРОКИ из БД >>>
        # parts_json_str = entry.get("parts_json") # Ожидаем строку JSON
        # <<< УДАЛЕНО: Не получаем parts как список напрямую >>>
        # <<< ИЗМЕНЕНИЕ: Снова читаем ключ 'parts', ожидая список >>>
        parts_list_of_dicts = entry.get("parts") # Ожидаем список словарей
        db_user_id = entry.get("user_id") # ID пользователя из сохраненной записи

        # <<< ВОЗВРАЩАЕМ: Проверяем наличие role и parts_json_str >>>
        # if not role or not parts_json_str: # Проверяем строку JSON
        #     logger.warning(f"Skipping history entry from DB with missing role or parts_json: {entry}")
        #     continue
        # <<< УДАЛЕНО: Старая проверка на parts_list_of_dicts >>>
        # <<< ИЗМЕНЕНИЕ: Проверяем наличие role и ключа 'parts' (значение может быть пустым списком) >>>
        if not role or parts_list_of_dicts is None: # Проверяем, что ключ 'parts' существует
            logger.warning(f"Skipping history entry from DB with missing role or None value for 'parts': {entry}")
            continue

        # <<< ВОЗВРАЩАЕМ: Десериализуем parts_json_str >>>
        # parts_list_of_dicts: Optional[List[Dict[str, Any]]] = None
        # try:
        #     parts_list_of_dicts = _deserialize_parts(parts_json_str)
        #     # Проверяем, что результат - это список
        #     if not isinstance(parts_list_of_dicts, list):
        #          logger.warning(f"Deserialized parts_json is not a list for entry: {entry}. Skipping.")
        #          continue
        # except Exception as deserialize_err:
        #     logger.warning(f"Failed to deserialize parts_json for entry: {entry}. Error: {deserialize_err}. Skipping.")
        #     continue
        # --- КОНЕЦ ДЕСЕРИАЛИЗАЦИИ ---
        # <<< УДАЛЕНО: Десериализация больше не нужна >>>

        # <<< УДАЛЕНО: Проверка типа была перенесена внутрь try >>>
        # <<< ИЗМЕНЕНИЕ: Проверяем, что значение 'parts' - это список >>>
        if not isinstance(parts_list_of_dicts, list):
             logger.warning(f"Retrieved 'parts' is not a list for entry: {entry}. Skipping.")
             continue
        # --- КОНЕЦ ПРОВЕРКИ ТИПА ---

        # <<< ЛОГИКА ОСТАЕТСЯ: Обработка пустых записей 'model' ПОСЛЕ десериализации >>>
        # <<< ИЗМЕНЕНИЕ: Обработка пустых записей 'model' БЕЗ десериализации >>>
        if role == 'model' and not parts_list_of_dicts:
            logger.debug(f"Found model entry with empty parts list from DB. Creating empty Content object for chat {chat_id}.") # Убрали 'AFTER deserialization'
            try:
                # Создаем пустой Content объект
                reconstructed_content = Content(role='model', parts=[])
                prepared_history_objects.append(reconstructed_content)
                processed_db_entries_count += 1
                continue # Переходим к следующей записи
            except Exception as empty_content_err:
                 logger.error(f"Failed to create empty Content object for model entry: {empty_content_err}. Skipping entry.", exc_info=True)
                 continue
        # --- КОНЕЦ НОВОЙ ЛОГИКИ ---

        # <<< СТАРАЯ ЛОГИКА для непустых записей >>>
        # Пытаемся реконструировать объект Content
        reconstructed_content = reconstruct_content_object(role, parts_list_of_dicts)

        # <<< ИЗМЕНЕНИЕ: Проверяем результат реконструкции И наличие частей >>>
        if reconstructed_content is None:
            logger.warning(f"Skipping history entry for role '{role}' because reconstruction failed (entry data: {entry}).")
            continue

        # --- ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА ---
        # Убедимся, что у реконструированного объекта есть непустой список parts
        if not reconstructed_content.parts:
             logger.warning(f"Skipping history entry for role '{role}' because reconstructed content has empty parts. Original dicts: {parts_list_of_dicts}")
             continue
        # <<< КОНЕЦ ИЗМЕНЕНИЯ >>>

        # <<< НАЧАЛО ИСПРАВЛЕНИЯ: Пропускаем роль 'function' ПОСЛЕ реконструкции >>>
        if role == 'function':
            logger.debug(f"History Prep: Skipping reconstructed 'function' role message to avoid API mismatch (entry data: {entry}).")
            continue # Не добавляем в prepared_history_objects
        # <<< КОНЕЦ ИСПРАВЛЕНИЯ >>>

        # Обрабатываем префикс пользователя ПОСЛЕ реконструкции И ПРОВЕРКИ
        if role == 'user' and db_user_id is not None and chat_type != ChatType.PRIVATE:
            try:
                # Находим первую текстовую часть и добавляем префикс
                prefix_added = False
                for part in reconstructed_content.parts:
                    if hasattr(part, 'text') and isinstance(part.text, str):
                        part.text = f"User {db_user_id}: {part.text}"
                        prefix_added = True
                        break # Добавляем префикс только к первой текстовой части
                if not prefix_added:
                     logger.warning(f"Could not add user prefix for chat {chat_id}, user {db_user_id}: No text part found in reconstructed content.")
            except Exception as prefix_err:
                logger.error(f"Error adding user prefix to reconstructed content: {prefix_err}", exc_info=True)
        # <<< КОНЕЦ ОБРАБОТКИ ПРЕФИКСА >>>

        # Добавляем реконструированный объект Content
        prepared_history_objects.append(reconstructed_content)
        processed_db_entries_count += 1 # Увеличиваем счетчик обработанных

    # <<< ДОБАВЛЯЕМ: Разворачиваем список ИЗ БД в хронологический порядок >>>
    # prepared_history_objects.reverse() 
    # <<< КОНЕЦ РАЗВОРОТА >>>

    # 3. Запоминаем длину ИСТОРИИ ИЗ БД для расчета новых записей при сохранении
    # Используем длину истории, ФАКТИЧЕСКИ загруженной из БД, для расчета дельты при сохранении
    original_db_len = len(history_from_db)

    # 4. <<< ВОЗВРАЩАЕМ: Очистка ПОСЛЕДНЕГО сообщения МОДЕЛИ от незавершенных FC >>>
    # Этот блок важен для соблюдения последовательности FC -> FR при отправке истории
    history_to_pass_to_gemini = prepared_history_objects.copy() # Работаем с копией
    if history_to_pass_to_gemini:
        last_entry = history_to_pass_to_gemini[-1]
        # Проверяем, что последнее сообщение от модели и содержит части
        if last_entry.role == 'model' and last_entry.parts:
            # Проверяем, есть ли среди частей FunctionCall
            has_function_call = any(hasattr(part, 'function_call') and part.function_call is not None for part in last_entry.parts)
            
            if has_function_call:
                logger.warning(f"Last model message for chat {chat_id} contains FunctionCall before sending to API. Cleaning it up.")
                # Создаем новый список частей, исключая FunctionCall
                cleaned_parts = [
                    part for part in last_entry.parts 
                    if not (hasattr(part, 'function_call') and part.function_call is not None)
                ]
                
                # Если после очистки остались какие-то части, обновляем последнюю запись
                if cleaned_parts:
                    # Создаем новый Content объект с очищенными частями
                    # Используем try-except на случай проблем с созданием Content
                    try:
                       cleaned_last_entry = Content(role='model', parts=cleaned_parts)
                       history_to_pass_to_gemini[-1] = cleaned_last_entry
                       logger.info(f"Successfully cleaned FunctionCall from the last model message for chat {chat_id}.")
                    except Exception as clean_err:
                        logger.error(f"Failed to create cleaned Content object for chat {chat_id}: {clean_err}. Removing the problematic last entry.", exc_info=True)
                        # Если не удалось создать объект, безопаснее удалить последнее сообщение
                        history_to_pass_to_gemini.pop()
                else:
                    # Если остались только FunctionCall, удаляем всё последнее сообщение модели
                    logger.warning(f"Last model message for chat {chat_id} contained ONLY FunctionCall(s). Removing it entirely before sending to API.")
                    history_to_pass_to_gemini.pop()
    # <<< КОНЕЦ ВОЗВРАЩЕННОЙ ОЧИСТКИ >>>

    # Логирование финальной истории для отправки (опционально)
    # try: ... (код логирования history_to_pass_to_gemini) ...

    # Add special system instructions if any
    #prepared_history_objects.extend(system_instructions)

    # # Reverse the history to have the most recent messages last. Gemini expects chronological order.
    # # RAG context should be first.
    # prepared_history_objects.reverse()
    # # logger.debug(f"Prepared history after reverse: {prepared_history_objects}")

    return prepared_history_objects, original_db_len


# --- Функция сохранения истории ---
async def save_history(
    chat_id: int,
    final_history_obj_list: Optional[List[Content]],
    original_db_history_len: int, # <<< ИЗМЕНЕН ПАРАМЕТР
    current_user_id: int, # ID пользователя, который инициировал ЭТОТ цикл взаимодействия
    last_sent_message_text: Optional[str] = None
):
    """
    Сохраняет НОВЫЕ сообщения из final_history в БД chat_history.
    НЕ сохраняет 'user' сообщения, если они уже есть в БД.
    НЕ сохраняет 'function' сообщения.
    Использует original_db_history_len для определения новых сообщений.
    При сохранении 'model' сообщения с FunctionCall, также сохраняет last_sent_message_text,
    если он предоставлен (для связи в RAG).

    Args:
        chat_id (int): ID чата.
        final_history_obj_list (Optional[List[Content]]): Полный список истории Content объектов
            после взаимодействия с моделью, или None при ошибке.
        original_db_history_len (int): Количество сообщений, которые были *загружены из БД* перед этим взаимодействием.
        current_user_id (int): ID пользователя, отправившего последнее сообщение в этом цикле.
        last_sent_message_text (Optional[str]): Текст ПОСЛЕДНЕГО сообщения пользователя,
            которое было отправлено модели в ЭТОМ цикле.
    """
    if not final_history_obj_list:
        logger.warning(f"Save History: Received empty or None final_history_obj_list for chat {chat_id}. Nothing to save.")
        return

    # <<< ИЗМЕНЕНО: Расчет количества новых элементов >>>
    num_new_items = len(final_history_obj_list) - original_db_history_len # Используем длину ИЗ БД

    if num_new_items <= 0:
        logger.debug(f"Save History: No new entries detected in final_history compared to initial history for chat {chat_id} (original_db_len={original_db_history_len}, final_len={len(final_history_obj_list)}). Nothing to save.")
        return

    # <<< ИЗМЕНЕНО: Получаем срез с конца списка >>>
    new_history_entries_content = final_history_obj_list[-num_new_items:]

    logger.info(f"Save History: Preparing to save {len(new_history_entries_content)} new entries (detected delta) for chat {chat_id}.")

    # --- Итерация по НОВЫМ записям (объекты Content) ---
    save_count = 0
    for entry_content in new_history_entries_content:
        role = entry_content.role
        parts_obj_list = entry_content.parts

        if not role:
            logger.warning(f"Save History: Skipping invalid Content entry (no role): {entry_content}")
            continue

        # <<< ДОБАВИТЬ ЭТУ ПРОВЕРКУ >>>
        if role == 'function':
            logger.debug(f"Save History: Skipping role 'function' entry, not saving to chat_history.")
            continue
        # <<< КОНЕЦ ДОБАВЛЕНИЯ >>>

        logger.debug(f"Save History Loop: Checking entry with role='{role}' (type: {type(role)}). Entry Content (summary): {str(entry_content)[:200]}...")

        # --- Преобразование parts в список словарей для БД --- 
        parts_list_of_dicts: List[Dict[str, Any]] = []
        is_function_call = False
        try:
            for part_obj in parts_obj_list:
                part_dict = _convert_part_to_dict(part_obj)
                if part_dict:
                    parts_list_of_dicts.append(part_dict)
                    # Проверяем, содержит ли часть вызов функции
                    if part_dict.get("type") == "function_call":
                        is_function_call = True
                else:
                     logger.warning(f"Save History: Could not convert part {part_obj} to dict. Skipping part.")
            if not parts_list_of_dicts and role != 'model': # Для model можно пустые parts (см. prepare)
                 logger.warning(f"Save History: No valid parts converted for role '{role}'. Skipping entry: {entry_content}")
                 continue
        except Exception as e:
             logger.error(f"Save History: Error converting parts for role '{role}': {e}. Skipping entry.", exc_info=True)
             continue

        # --- Логика сохранения в БД --- 
        # НЕ сохраняем 'user' сообщения (они уже должны быть сохранены при отправке)
        # <<< ИСПРАВЛЕНО: Теперь пропускаем и 'function' роль до этого момента >>>
        if role == 'user':
            logger.debug(f"Save History: Skipping role 'user' entry, should be saved on send.")
            continue

        # Сохраняем 'model' сообщение (включая пустые, если они есть)
        if role == 'model':
            parts_json_str = "[]" # Инициализируем на случай ошибки сериализации
            try:
                 # Сериализуем список словарей в СТРОКУ JSON
                 parts_json_str = json.dumps(parts_list_of_dicts, ensure_ascii=False)
                 logger.debug(f"Save History (model): Serialized parts for DB: {parts_json_str[:200]}...")
            except Exception as serialize_err:
                 logger.error(f"Save History (model): Failed to serialize parts list to JSON: {serialize_err}. Saving empty list.", exc_info=True)
                 parts_json_str = "[]" # Fallback

            try:
                # <<< ИСПРАВЛЕНИЕ: Вызываем add_message_to_history с СТРОКОЙ JSON >>>
                await database.add_message_to_history(
                    chat_id=chat_id,
                    user_id=current_user_id, # Используем ID пользователя текущего цикла
                    role=role,
                    parts=parts_json_str # Передаем СТРОКУ JSON
                )
                # Переносим лог успеха внутрь try после успешного await
                logger.info(f"Save History: Successfully saved 'model' entry to chat_history for chat {chat_id}. Parts: {str(parts_list_of_dicts)[:100]}...")
                save_count += 1
            except TypeError as te: # Ловим TypeError отдельно
                 logger.critical(f"Save History: TYPE ERROR calling add_message_to_history for 'model' entry (chat {chat_id}): {te}. Check arguments! Passed parts type: {type(parts_json_str)}, value: {parts_json_str[:100]}...", exc_info=True)
            except AttributeError as ae: # Оставляем на всякий случай
                 logger.critical(f"Save History: ATTRIBUTE ERROR calling DB function for 'model' entry (chat {chat_id}): {ae}. CHECK FUNCTION NAME! Expected 'add_message_to_history'. Parts JSON: {parts_json_str}", exc_info=True)
            except Exception as db_save_err:
                # Оставляем общий обработчик ошибок
                logger.error(f"Save History: DB Error calling add_message_to_history for 'model' entry (chat {chat_id}): {db_save_err}", exc_info=True)
        else:
            # Эта ветка по идее не должна достигаться, так как user/function пропускаются
            logger.warning(f"Save History: Encountered unexpected role '{role}' during save loop. Skipping.")

    logger.info(f"Save History: Finished saving new entries for chat {chat_id}. Saved {save_count} new messages.")

# <<< НОВАЯ ФУНКЦИЯ ДЛЯ АПДЕЙТА >>>
# ... existing code ...

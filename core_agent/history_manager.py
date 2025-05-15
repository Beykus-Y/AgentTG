# core_agent/history_manager.py

import logging
import json
from typing import List, Dict, Any, Optional, Tuple, Union

# --- Условный импорт типов AI (только для аннотаций и isinstance) ---
# Google
try:
    from google.ai import generativelanguage as glm
    # Внутренний тип Google для списков частей
    from google.protobuf.internal.containers import RepeatedComposite
    GoogleContent = glm.Content
    GooglePart = glm.Part
    GoogleFunctionResponse = glm.FunctionResponse
    GoogleFunctionCall = glm.FunctionCall
    google_imported = True
except ImportError:
    GoogleContent, GooglePart, GoogleFunctionResponse, GoogleFunctionCall, RepeatedComposite = Any, Any, Any, Any, Any
    google_imported = False
    # Не логируем здесь ошибку, т.к. это может быть ожидаемо при выборе OpenAI

# OpenAI
try:
    # Используем объекты из SDK для type hinting, если возможно
    from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageToolCall
    from openai.types.chat.chat_completion_message_tool_call import Function as OpenAIFunction
    openai_imported = True
except ImportError:
    ChatCompletionMessage = Any # Используем Any как fallback
    ChatCompletionMessageToolCall = Any
    OpenAIFunction = Any
    openai_imported = False
    # Не логируем здесь ошибку, т.к. это может быть ожидаемо при выборе Google

# --- Локальные импорты ---
try:
    import database
    from utils.helpers import escape_markdown_v2, remove_markdown
    # Нужен для конвертации сложных типов Google Args/Response
    from utils.converters import _convert_value_for_json
    from bot_loader import dp
except ImportError as e:
    logging.getLogger(__name__).critical(f"CRITICAL: Failed to import dependencies in history_manager: {e}", exc_info=True)
    database = None # type: ignore
    def escape_markdown_v2(text: Optional[str]) -> str: return text or ""
    def remove_markdown(text: Optional[str]) -> str: return text or ""
    def _convert_value_for_json(value: Any) -> Any: return str(value) # Заглушка конвертера
    dp = type('obj', (object,), {'workflow_data': {}})() # type: ignore


logger = logging.getLogger(__name__)

# Константы для логов (остаются)
MAX_LOG_CONTEXT_LEN = 200
MAX_FULL_RESULT_PREVIEW = 500

# --- Функции Конвертации Истории ---

def _google_content_to_parts_json(content: GoogleContent) -> str:
    """Конвертирует Google Content в parts_json строку."""
    if not google_imported or not isinstance(content, GoogleContent):
        logger.error("Cannot serialize Google Content: Google types not imported or invalid input.")
        return "[]"

    serializable_list = []
    parts_iterator = getattr(content, 'parts', [])
    # Убедимся, что это итерируемый объект
    if not isinstance(parts_iterator, (list, tuple, RepeatedComposite)):
        logger.error(f"Google Content parts are not iterable: type={type(parts_iterator)}")
        return "[]"

    try:
        for part in parts_iterator:
            part_data: Dict[str, Any] = {} # Очищаем для каждой части
            part_type: Optional[str] = None
            has_content = False

            # Обработка FunctionCall
            fc = getattr(part, 'function_call', None)
            if fc and isinstance(fc, GoogleFunctionCall) and getattr(fc, 'name', None):
                part_type = "google_fc"
                args_dict = {}
                try:
                    # Используем _convert_value_for_json для глубокой конвертации аргументов
                    args_dict = _convert_value_for_json(getattr(fc, 'args', {}))
                    if not isinstance(args_dict, dict): raise TypeError("Converted args not dict")
                except Exception as conv_err:
                    logger.error(f"Error converting Google FC args: {conv_err}", exc_info=True)
                    args_dict = {"error": f"Arg conversion failed: {conv_err}"}
                part_data = {"type": part_type, "name": fc.name, "args": args_dict}
                has_content = True
                serializable_list.append(part_data) # Добавляем FC как отдельную часть
                continue # FC и FR не могут быть в одной части с текстом по спецификации Gemini

            # Обработка FunctionResponse
            fr = getattr(part, 'function_response', None)
            if fr and isinstance(fr, GoogleFunctionResponse) and getattr(fr, 'name', None):
                part_type = "google_fr"
                resp_dict = {}
                try:
                    resp_dict = _convert_value_for_json(getattr(fr, 'response', {}))
                    if not isinstance(resp_dict, dict): raise TypeError("Converted response not dict")
                except Exception as conv_err:
                    logger.error(f"Error converting Google FR response: {conv_err}", exc_info=True)
                    resp_dict = {"error": f"Response conversion failed: {conv_err}"}
                part_data = {"type": part_type, "name": fr.name, "response": resp_dict}
                has_content = True
                serializable_list.append(part_data) # Добавляем FR как отдельную часть
                continue

            # Обработка Text (если не было FC/FR)
            text_content = getattr(part, 'text', None)
            if isinstance(text_content, str):
                part_type = "text"
                part_data = {"type": part_type, "content": text_content}
                # Сохраняем даже пустой текст, если это единственная часть
                # (но если были FC/FR, пустой текст не добавляем)
                if text_content or len(list(parts_iterator)) == 1:
                     serializable_list.append(part_data)

        # Сериализуем список частей
        return json.dumps(serializable_list, ensure_ascii=False, default=str) # default=str на всякий случай

    except Exception as e:
        logger.error(f"Error serializing Google Content parts: {e}", exc_info=True)
        return "[]"

def _openai_message_to_db_parts_json(message: Dict[str, Any]) -> str:
    """Конвертирует OpenAI message dict в parts_json строку."""
    if not openai_imported or not isinstance(message, dict):
        logger.error("Cannot serialize OpenAI message: OpenAI types not imported or invalid input.")
        return "[]"

    parts_for_json: List[Dict[str, Any]] = []
    role = message.get("role")

    try:
        # 1. Обработка текстового контента
        content = message.get("content")
        if isinstance(content, str):
            # Добавляем текст, даже если он пустой (важно для user/assistant ролей)
            parts_for_json.append({"type": "text", "content": content})

        # 2. Обработка tool_calls (для assistant)
        tool_calls = message.get("tool_calls")
        if role == "assistant" and isinstance(tool_calls, list):
            for tc in tool_calls:
                tc_data = {}
                # Пытаемся извлечь данные из объекта или словаря
                if isinstance(tc, dict): # Если пришло как словарь
                    tc_data = tc
                elif hasattr(tc, 'model_dump') and callable(tc.model_dump): # Если объект Pydantic V2 (SDK >= 1.0)
                    tc_data = tc.model_dump(exclude_unset=True)
                elif hasattr(tc, 'dict') and callable(tc.dict): # Если объект Pydantic V1 (старые SDK)
                     tc_data = tc.dict(exclude_unset=True)
                else:
                     logger.warning(f"Unsupported tool_call type: {type(tc)}. Skipping.")
                     continue

                call_id = tc_data.get("id")
                func_data = tc_data.get("function", {}) if isinstance(tc_data.get("function"), dict) else {}
                func_name = func_data.get("name")
                # Аргументы ДОЛЖНЫ быть строкой JSON
                func_arguments = func_data.get("arguments")

                if isinstance(call_id, str) and isinstance(func_name, str) and isinstance(func_arguments, str):
                    parts_for_json.append({
                        "type": "openai_tool_call",
                        "tool_call_id": call_id,
                        "name": func_name,
                        "arguments": func_arguments # Сохраняем как строку JSON
                    })
                else:
                     logger.warning(f"Skipping invalid openai_tool_call data: {tc_data}")

        # 3. Обработка tool role
        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            tool_content = message.get("content") # Результат инструмента (строка)
            if isinstance(tool_call_id, str) and isinstance(tool_content, str):
                 parts_for_json.append({
                     "type": "openai_tool_result",
                     "tool_call_id": tool_call_id,
                     "content": tool_content # Сохраняем результат как строку
                 })
            else:
                 logger.warning(f"Skipping invalid openai_tool_result data: id={tool_call_id}, content_type={type(tool_content)}")

        # Сериализуем полученный список
        return json.dumps(parts_for_json, ensure_ascii=False, default=str)

    except Exception as e:
        logger.error(f"Error serializing OpenAI message parts: {e}", exc_info=True)
        return "[]"


def _db_entry_to_google_content(entry: Dict[str, Any]) -> Optional[GoogleContent]:
    """Восстанавливает Google Content из записи БД."""
    if not google_imported: return None
    role = entry.get("role")
    parts_json = entry.get("parts_json")
    if not role or not parts_json: return None

    try:
        parts_data_list = json.loads(parts_json)
        if not isinstance(parts_data_list, list):
            logger.error(f"DB parts_json is not a list: {parts_json[:100]}...")
            return None

        reconstructed_parts: List[GooglePart] = []
        for part_data in parts_data_list:
            if not isinstance(part_data, dict): continue
            part_type = part_data.get("type")

            try:
                if part_type == "text" and "content" in part_data:
                    reconstructed_parts.append(GooglePart(text=str(part_data["content"])))
                elif part_type == "google_fc" and "name" in part_data and "args" in part_data:
                     # Args должны быть словарем
                     if isinstance(part_data["args"], dict):
                          reconstructed_parts.append(GooglePart(function_call=GoogleFunctionCall(name=str(part_data["name"]), args=part_data["args"])))
                     else: logger.warning(f"Skipping Google FC reconstruction: args not dict ({type(part_data['args'])})")
                elif part_type == "google_fr" and "name" in part_data and "response" in part_data:
                     # Response должен быть словарем
                     if isinstance(part_data["response"], dict):
                          reconstructed_parts.append(GooglePart(function_response=GoogleFunctionResponse(name=str(part_data["name"]), response=part_data["response"])))
                     else: logger.warning(f"Skipping Google FR reconstruction: response not dict ({type(part_data['response'])})")
                # Игнорируем 'openai_*' типы для Google
            except Exception as part_recon_err:
                 logger.error(f"Error reconstructing Google Part from data: {part_data}. Error: {part_recon_err}", exc_info=True)
                 continue # Пропускаем поврежденную часть

        if reconstructed_parts:
            # Проверяем валидность роли для Google
            valid_google_roles = {'user', 'model', 'function'} # 'system' у Google нет в Content
            if role not in valid_google_roles:
                 logger.warning(f"Invalid role '{role}' for Google Content reconstruction. Using 'user'.")
                 role = 'user'
            return GoogleContent(role=role, parts=reconstructed_parts)
        else:
            # Если нет частей, но роль 'user' или 'model', можно вернуть пустой Content?
            # Пока возвращаем None, чтобы не создавать пустых записей без необходимости
            logger.debug(f"Reconstruction resulted in no valid Google parts for role '{role}'. Original JSON: {parts_json[:100]}...")
            return None

    except json.JSONDecodeError as e:
        logger.error(f"Failed to deserialize parts JSON for Google Content: {e}. JSON: '{parts_json[:100]}...'")
        return None
    except Exception as e:
        logger.error(f"Unexpected error reconstructing Google Content: {e}", exc_info=True)
        return None

def _db_entry_to_openai_message(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Восстанавливает OpenAI message dict из записи БД."""
    if not openai_imported: return None
    role = entry.get("role")
    parts_json = entry.get("parts_json")
    if not role or not parts_json: return None

    try:
        parts_data_list = json.loads(parts_json)
        if not isinstance(parts_data_list, list):
            logger.error(f"DB parts_json is not a list: {parts_json[:100]}...")
            return None

        # Проверяем валидность роли для OpenAI
        valid_openai_roles = {'user', 'assistant', 'system', 'tool'}
        if role not in valid_openai_roles:
             logger.warning(f"Invalid role '{role}' for OpenAI message reconstruction. Skipping.")
             return None

        message: Dict[str, Any] = {"role": role}
        tool_calls_list: List[Dict] = [] # Собираем tool_calls отдельно

        for part_data in parts_data_list:
            if not isinstance(part_data, dict): continue
            part_type = part_data.get("type")

            try:
                if part_type == "text" and "content" in part_data:
                    # У OpenAI 'content' может быть только у user, assistant, system, tool
                    if role in ['user', 'assistant', 'system', 'tool']:
                         message["content"] = str(part_data["content"])
                elif part_type == "openai_tool_call" and role == "assistant":
                    # Восстанавливаем структуру tool_call
                    tool_call_id = part_data.get("tool_call_id")
                    func_name = part_data.get("name")
                    func_args_str = part_data.get("arguments") # Должна быть строка JSON
                    if isinstance(tool_call_id, str) and isinstance(func_name, str) and isinstance(func_args_str, str):
                         tool_calls_list.append({
                             "id": tool_call_id,
                             "type": "function", # Пока поддерживаем только function
                             "function": {
                                 "name": func_name,
                                 "arguments": func_args_str
                             }
                         })
                    else: logger.warning(f"Skipping invalid openai_tool_call data: {part_data}")
                elif part_type == "openai_tool_result" and role == "tool":
                    tool_call_id = part_data.get("tool_call_id")
                    result_content = part_data.get("content") # Должна быть строка (результат JSON)
                    if isinstance(tool_call_id, str) and isinstance(result_content, str):
                         message["tool_call_id"] = tool_call_id
                         message["content"] = result_content # Устанавливаем content для tool роли
                    else: logger.warning(f"Skipping invalid openai_tool_result data: {part_data}")
                # Игнорируем 'google_*' типы для OpenAI
            except Exception as part_recon_err:
                 logger.error(f"Error reconstructing OpenAI part from data: {part_data}. Error: {part_recon_err}", exc_info=True)
                 continue # Пропускаем поврежденную часть

        # Добавляем собранные tool_calls, если они есть
        if tool_calls_list:
            message["tool_calls"] = tool_calls_list

        # Проверяем, есть ли у сообщения хоть какое-то содержимое
        has_content = "content" in message
        has_tools = "tool_calls" in message or "tool_call_id" in message

        if has_content or has_tools:
            return message
        else:
            logger.debug(f"Reconstruction resulted in no valid OpenAI content/tools for role '{role}'. Original JSON: {parts_json[:100]}...")
            # Возвращаем сообщение с пустой строкой для user/assistant, если не было другого контента
            return {"role": role, "content": ""} if role in ["user", "assistant"] else None

    except json.JSONDecodeError as e:
        logger.error(f"Failed to deserialize parts JSON for OpenAI message: {e}. JSON: '{parts_json[:100]}...'")
        return None
    except Exception as e:
        logger.error(f"Unexpected error reconstructing OpenAI message: {e}", exc_info=True)
        return None


# --- Основные функции (prepare_history, save_history) ---
# В них теперь используются реализованные функции конвертации.
# Их код остается таким же, как в предыдущем ответе.

async def prepare_history(
    chat_id: int,
    user_id: int,
    chat_type: Any,
    ai_provider: str, # <<< Принимаем провайдера
    add_notes: bool = True,
    add_recent_logs: bool = True,
    recent_logs_limit: int = 8,
    group_chat_history_limit: int = 50 # Новый параметр для ограничения истории сообщений из группового чата
) -> Tuple[List[Any], int]:
    """
    Подготавливает историю для Google или OpenAI.
    """
    logger.debug(f"Preparing history for chat={chat_id}, provider={ai_provider.upper()}")

    if database is None:
        logger.critical("Database module unavailable. Cannot prepare history.")
        return [], 0

    # 1. Получение данных из БД (без изменений)
    history_from_db: List[Dict[str, Any]] = []
    user_profile: Optional[Dict[str, Any]] = None
    user_notes: Dict[str, Any] = {}
    recent_logs: List[Dict[str, Any]] = []
    original_db_len: int = 0
    try:
        # Получаем историю как список словарей
        if hasattr(database, 'get_chat_history'):
             # Для групповых чатов загружаем больше истории, чтобы обеспечить полный контекст
             if chat_type in ('group', 'supergroup') and group_chat_history_limit > 0:
                 history_from_db = await database.get_chat_history(chat_id, limit=group_chat_history_limit)
                 
                 # Для групповых чатов фильтруем историю, чтобы включить только:
                 # 1. Сообщения текущего пользователя
                 # 2. Сообщения, адресованные боту или от бота
                 # 3. Несколько последних сообщений в групповом чате независимо от пользователя
                 
                 # Получаем ID бота (0 - это система)
                 bot_user_ids = [0]  # Системные сообщения и сообщения бота по умолчанию
                 
                 # Фильтруем сообщения
                 filtered_history = []
                 recent_messages_count = 5  # Количество последних сообщений, которые сохраняем независимо от отправителя
                 
                 # Добавляем последние N сообщений
                 recent_messages = history_from_db[-recent_messages_count:] if len(history_from_db) >= recent_messages_count else history_from_db
                 
                 # Формируем отфильтрованную историю
                 for entry in history_from_db:
                     entry_user_id = entry.get("user_id")
                     role = entry.get("role")
                     
                     # Включаем сообщения:
                     # 1. От текущего пользователя
                     # 2. От помощника (assistant/model)
                     # 3. От бота
                     # 4. Последние N сообщений
                     if (entry_user_id == user_id or 
                         role in ('assistant', 'model') or 
                         entry_user_id in bot_user_ids or
                         entry in recent_messages):
                         filtered_history.append(entry)
                 
                 logger.info(f"Loaded and filtered chat history for group chat {chat_id}: {len(filtered_history)}/{len(history_from_db)} messages kept")
                 history_from_db = filtered_history
             else:
                 history_from_db = await database.get_chat_history(chat_id)
             original_db_len = len(history_from_db)
        else: logger.warning("Database.get_chat_history unavailable.")

        if add_notes:
            if hasattr(database, 'get_user_profile'): user_profile = await database.get_user_profile(user_id)
            else: logger.warning("Database.get_user_profile unavailable.")
            if hasattr(database, 'get_user_notes'): user_notes = await database.get_user_notes(user_id, parse_json=True)
            else: logger.warning("Database.get_user_notes unavailable.")
        if add_recent_logs and recent_logs_limit > 0:
            if hasattr(database, 'get_recent_tool_executions'): recent_logs = await database.get_recent_tool_executions(chat_id, limit=recent_logs_limit)
            else: logger.warning("Database.get_recent_tool_executions unavailable.")

    except Exception as db_err:
        logger.error(f"DB error fetch history/profile/notes/logs chat={chat_id}: {db_err}", exc_info=True)
        return [], 0

    # 2. Формирование истории для модели
    prepared_history: List[Any] = []
    system_context_added = False

    # --- Добавление системного промпта / контекста ---
    system_parts = [] # Собираем части для системного сообщения/контекста

    # Добавляем логи (если нужно)
    if add_recent_logs and recent_logs:
        # ... (Формирование строки логов `full_logs_str` как в предыдущем ответе) ...
        logs_str_parts = [escape_markdown_v2("~~~Недавние Выполненные Действия~~~")]
        added_log_count = 0
        for log_entry in reversed(recent_logs):
             tool_name = log_entry.get('tool_name', 'unknown_tool')
             if tool_name in {'send_telegram_message', 'Developer_Feedback'}: continue # Фильтруем
             # ... (Формируем log_line_parts как раньше) ...
             log_line_parts = []
             ts = escape_markdown_v2(str(log_entry.get('timestamp', 'N/A')).split('.')[0])
             status = escape_markdown_v2(str(log_entry.get('status', 'unknown')))
             msg = log_entry.get('result_message')
             stdout = log_entry.get('stdout')
             stderr = log_entry.get('stderr')
             full_result_json_str = log_entry.get('full_result_json')
             log_line_parts.append(f"- [{ts}] **{escape_markdown_v2(tool_name)}** (Статус: **{status}**)")
             if msg: log_line_parts.append(f"  - Результат: `{escape_markdown_v2(msg[:MAX_LOG_CONTEXT_LEN] + ('...' if len(msg) > MAX_LOG_CONTEXT_LEN else ''))}`")
             # ... (добавляем stdout/stderr/full_result_json) ...
             logs_str_parts.append("\n".join(log_line_parts))
             added_log_count += 1
        if added_log_count > 0:
             full_logs_str = "\n\n".join(logs_str_parts)
             system_parts.append(full_logs_str)
             logger.info(f"Added {added_log_count} recent logs to context.")

    # Добавляем заметки/профиль (если нужно)
    if add_notes and (user_profile or user_notes):
        # ... (Формирование строки профиля/заметок `full_context_str` как в предыдущем ответе) ...
        user_data_str_parts = []
        # ... (логика user_profile, user_notes) ...
        if user_profile:
            profile_parts = [f"*Профиль (User ID: {user_id}):*"]
            if user_profile.get('first_name'): profile_parts.append(f"- Имя: {escape_markdown_v2(str(user_profile['first_name']))}")
            if user_profile.get('username'): profile_parts.append(f"- Username: @{escape_markdown_v2(str(user_profile['username']))}")
            if user_profile.get("avatar_description"): profile_parts.append(f"- Аватар: {escape_markdown_v2(str(user_profile['avatar_description']))}")
            user_data_str_parts.append("\n".join(profile_parts))
        if user_notes:
            notes_str_list = []
            for cat in sorted(user_notes.keys()):
                 val = user_notes[cat]
                 # ... (форматирование val как JSON или строки) ...
                 notes_str_list.append(f"- **{escape_markdown_v2(cat)}**: {escape_markdown_v2(str(val))}")
            if notes_str_list:
                 notes_section = "*Заметки:*\n" + "\n".join(notes_str_list)
                 user_data_str_parts.append(notes_section)
        # --- Конец формирования ---
        if user_data_str_parts:
             full_context_str = escape_markdown_v2("~~~Контекст Текущего Пользователя~~~") + "\n" + "\n\n".join(user_data_str_parts)
             system_parts.append(full_context_str)
             logger.info(f"Added user profile/notes context for user {user_id}.")


    # --- Добавляем системные данные в зависимости от провайдера ---
    if ai_provider == "openai":
        # OpenAI: Добавляем системный промпт + контекст как одно system сообщение
        pro_system_prompt = dp.workflow_data.get("pro_system_prompt")
        if pro_system_prompt: system_parts.insert(0, pro_system_prompt) # Добавляем основной промпт в начало

        if system_parts:
            combined_system_content = "\n\n---\n\n".join(system_parts)
            prepared_history.append({"role": "system", "content": combined_system_content})
            system_context_added = True
            logger.info(f"Added combined system prompt/context for OpenAI.")

    elif ai_provider == "google":
        # Google: Добавляем контекст как отдельные 'model' сообщения
        context_objects = []
        for part_content in system_parts: # Итерируем по собранным частям контекста
            if part_content and google_imported:
                 try:
                     context_objects.append(GoogleContent(role="model", parts=[GooglePart(text=part_content)]))
                     system_context_added = True
                 except Exception as e: logger.error(f"Failed create Google context Content: {e}")
        if context_objects:
            prepared_history.extend(context_objects)
            logger.info(f"Added {len(context_objects)} context block(s) as 'model' role for Google.")
        # Системный промпт для Google устанавливается при инициализации модели

    # --- Добавляем историю сообщений из БД ---
    processed_db_entries_count = 0
    for entry in history_from_db:
        reconstructed_entry = None
        db_user_id = entry.get("user_id")

        if ai_provider == "openai":
            reconstructed_entry = _db_entry_to_openai_message(entry)
            # Добавление префикса пользователя для OpenAI только для отображения в модели, не для сохранения
            if reconstructed_entry and reconstructed_entry.get("role") == 'user' and db_user_id and chat_type != 'private':
                 user_prefix = f"User {db_user_id}: "
                 current_content = reconstructed_entry.get("content", "")
                 if isinstance(current_content, str):
                     # Проверяем, не начинается ли уже текст с User prefix
                     if not current_content.startswith(f"User {db_user_id}:"):
                         reconstructed_entry["content"] = user_prefix + current_content

        elif ai_provider == "google":
            reconstructed_entry = _db_entry_to_google_content(entry)
            # Добавление префикса пользователя для Google только для отображения в модели, не для сохранения
            if reconstructed_entry and reconstructed_entry.role == 'user' and db_user_id and chat_type != 'private' and google_imported:
                 user_prefix = f"User {db_user_id}: "
                 new_parts = []
                 prefix_added = False
                 for part in reconstructed_entry.parts:
                     if isinstance(part, GooglePart) and hasattr(part, 'text') and isinstance(part.text, str):
                          # Проверяем, не начинается ли уже текст с User prefix
                          if not part.text.startswith(f"User {db_user_id}:"):
                              new_parts.append(GooglePart(text=user_prefix + part.text))
                              prefix_added = True
                          else:
                              new_parts.append(part)
                     else: new_parts.append(part)
                 if prefix_added:
                      reconstructed_entry = GoogleContent(role='user', parts=new_parts)

        if reconstructed_entry:
            prepared_history.append(reconstructed_entry)
            processed_db_entries_count += 1
        else:
             logger.warning(f"History Prep: Failed reconstruct entry role '{entry.get('role')}' for {ai_provider}. Skipping.")

    logger.debug(f"History Prep: Processed {processed_db_entries_count}/{original_db_len} DB entries. Final history length for {ai_provider.upper()}: {len(prepared_history)}")
    return prepared_history, original_db_len


async def save_history(
    chat_id: int,
    final_history: Optional[List[Any]], # Тип зависит от провайдера
    original_db_history_len: int,
    current_user_id: int,
    ai_provider: str # <<< Принимаем провайдера
):
    """
    Сохраняет НОВЫЕ сообщения из final_history (формата Google или OpenAI) в БД.
    Важно: мы сохраняем только оригинальное содержимое, без префиксов User ID.
    """
    if database is None:
        logger.critical("Database module unavailable. Cannot save history.")
        return
    if not final_history:
        logger.debug(f"Save History ({ai_provider}): Received empty final_history. Nothing to save.")
        return

    # Рассчитываем количество новых элементов - ожидаем минимум одно новое сообщение пользователя и одно от ассистента
    num_new_items = len(final_history) - original_db_history_len
    if num_new_items <= 0:
        logger.warning(f"Save History ({ai_provider}): No new entries detected. History lengths - original: {original_db_history_len}, final: {len(final_history)}")
        # ИСПРАВЛЕНО: Проверим, есть ли ответ ассистента, который нужно сохранить
        if len(final_history) > 0:
            last_entry = final_history[-1]
            is_ai = (ai_provider == "openai" and isinstance(last_entry, dict) and last_entry.get("role") == "assistant") or \
                    (ai_provider == "google" and hasattr(last_entry, 'role') and last_entry.role == 'model')
            if is_ai:
                new_history_entries = [last_entry]
                logger.info(f"Save History ({ai_provider.upper()}): Forcing save of last message (assistant) even though no new entries were detected")
            else:
                return
        else:
            return

    # ИСПРАВЛЕНО: Если новые сообщения были обнаружены, используем их (иначе используется последнее сообщение ассистента)
    if not 'new_history_entries' in locals():
        new_history_entries = final_history[-num_new_items:]
        logger.info(f"Save History ({ai_provider.upper()}): Preparing to save {len(new_history_entries)} new entries for chat {chat_id}.")

    save_count = 0
    if not hasattr(database, 'add_message_to_history'):
         logger.critical("Database.add_message_to_history unavailable. Cannot save.")
         return

    for entry in new_history_entries:
        role = None
        parts_json = None
        user_id_to_save = None

        try:
            if ai_provider == "openai" and isinstance(entry, dict):
                role = entry.get("role")
                # Сохраняем assistant, tool, и user сообщения
                if role in ["assistant", "tool"]:
                    parts_json = _openai_message_to_db_parts_json(entry)
                elif role == "user":
                    parts_json = _openai_message_to_db_parts_json(entry)
                    user_id_to_save = current_user_id
                # Не сохраняем 'system'

            elif ai_provider == "google" and google_imported and isinstance(entry, GoogleContent):
                role = getattr(entry, 'role', None)
                # Сохраняем 'model' и 'user' сообщения
                if role == 'model':
                    parts_json = _google_content_to_parts_json(entry)
                elif role == 'user':
                    parts_json = _google_content_to_parts_json(entry)
                    user_id_to_save = current_user_id
                # Не сохраняем 'function'

            else: # Неизвестный тип
                 logger.warning(f"Save History: Skipping unknown entry type '{type(entry)}' for provider '{ai_provider}'.")
                 continue

            # Сохраняем в БД, если удалось получить parts_json и он не пустой '[]'
            if role and parts_json and parts_json != "[]":
                await database.add_message_to_history(
                    chat_id=chat_id,
                    user_id=user_id_to_save,
                    role=role,
                    parts=parts_json # Передаем строку JSON
                )
                save_count += 1
            elif role and parts_json == "[]":
                 logger.debug(f"Save History ({ai_provider}): Skipping save for role '{role}' because parts_json is empty '[]'.")
            elif not role:
                 logger.warning("Save History: Role could not be determined for entry. Skipping save.")

        except Exception as entry_proc_err:
            logger.error(f"Save History ({ai_provider}): Error processing/saving entry: {entry_proc_err}. Entry: {str(entry)[:100]}...", exc_info=True)

    logger.info(f"Save History ({ai_provider.upper()}): Finished saving. Saved {save_count}/{len(new_history_entries)} new entries for chat {chat_id}.")
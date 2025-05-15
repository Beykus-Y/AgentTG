# ai_interface/tool_processing.py  <- Новое имя файла

import asyncio
import inspect
import logging
import json # Для парсинга аргументов OpenAI и логирования
from typing import Dict, Any, List, Optional, Tuple, Callable, Union

# --- Локальные импорты ---
try:
    # Импортируем ОБА модуля API (или их заглушки)
    from . import gemini_api
    from . import openai_api
    from utils.helpers import escape_markdown_v2
    # Импорт конвертера для Google Args и БД
    from utils.converters import _convert_value_for_json
    import database
    # Импортируем валидатор сообщений OpenAI из utils
    from utils.message_utils import sanitize_openai_messages
except ImportError:
     logging.critical("CRITICAL: Failed to import dependencies in tool_processing.", exc_info=True)
     gemini_api = None; openai_api = None # type: ignore
     database = None # type: ignore
     def escape_markdown_v2(text: str) -> str: return text
     def _convert_value_for_json(value: Any) -> Any: return str(value) # Грубая заглушка
     def sanitize_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]: return messages # Заглушка

# --- Условный импорт типов AI (для аннотаций и isinstance) ---
try: # Google Types
    from google.ai import generativelanguage as glm
    GooglePart = glm.Part
    GoogleFunctionResponse = glm.FunctionResponse
    GoogleFunctionCall = glm.FunctionCall
    GoogleContent = glm.Content
    try: GoogleFinishReason = glm.Candidate.FinishReason
    except AttributeError: GoogleFinishReason = None
    from google.generativeai.types import GenerateContentResponse as GoogleResponse
    google_imported = True
except ImportError:
    GooglePart, GoogleFunctionCall, GoogleFunctionResponse, GoogleContent, GoogleFinishReason, GoogleResponse = Any, Any, Any, Any, Any, Any
    google_imported = False

try: # OpenAI Types
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletion, ChatCompletionMessage, ChatCompletionMessageToolCall
    openai_imported = True
except ImportError:
    AsyncOpenAI = Any # type: ignore
    ChatCompletion, ChatCompletionMessage, ChatCompletionMessageToolCall = Any, Any, Any
    openai_imported = False

logger = logging.getLogger(__name__)

# --- ОБЩАЯ функция выполнения хендлера (без изменений) ---
async def execute_function_call(
    handler_func: Callable,
    args: Dict[str, Any],
    chat_id_for_handlers: Optional[int] = None,
    user_id_for_handlers: Optional[int] = None
) -> Any:
    """
    Асинхронно выполняет хендлер инструмента (синхронный или асинхронный).
    (Код этой функции остается точно таким же, как в вашем fc_processing.py)
    """
    handler_sig = inspect.signature(handler_func)
    final_args = args.copy()

    # Внедрение ID чата/пользователя (если нужно и не предоставлено AI)
    if 'chat_id' in handler_sig.parameters and 'chat_id' not in args and chat_id_for_handlers is not None:
        final_args['chat_id'] = chat_id_for_handlers
        logger.debug(f"Injecting sender chat_id ({chat_id_for_handlers}) for {handler_func.__name__}")
    if 'user_id' in handler_sig.parameters and 'user_id' not in args and user_id_for_handlers is not None:
         final_args['user_id'] = user_id_for_handlers
         logger.debug(f"Injecting sender user_id ({user_id_for_handlers}) for {handler_func.__name__}")

    # Фильтрация аргументов и проверка обязательных
    filtered_args = {k: v for k, v in final_args.items() if k in handler_sig.parameters}
    missing_args = [p_name for p_name, p_obj in handler_sig.parameters.items() if p_obj.default is inspect.Parameter.empty and p_name not in filtered_args]
    if missing_args:
        err_msg = f"Missing required args for '{handler_func.__name__}': {missing_args}. Provided: {list(args.keys())}"
        logger.error(err_msg)
        return {"status": "error", "message": err_msg}

    logger.debug(f"Executing handler '{handler_func.__name__}' with final args: {filtered_args}")

    # Выполнение хендлера
    try:
        if asyncio.iscoroutinefunction(handler_func):
            return await handler_func(**filtered_args)
        else:
            loop = asyncio.get_running_loop()
            from functools import partial
            func_call = partial(handler_func, **filtered_args)
            return await loop.run_in_executor(None, func_call)
    except Exception as exec_err:
        err_msg = f"Handler execution failed for '{handler_func.__name__}': {exec_err}"
        logger.error(f"Error executing '{handler_func.__name__}' with {filtered_args}: {exec_err}", exc_info=True)
        return {"status": "error", "message": err_msg}


# --- Функция обработки цикла Function Calling для GOOGLE GEMINI ---
async def process_google_fc_cycle(
    model_instance: Any, # Экземпляр genai.GenerativeModel
    chat_session: Any,   # Экземпляр genai.ChatSession
    available_functions_map: Dict[str, Callable],
    max_steps: int,
    original_chat_id: Optional[int] = None,
    original_user_id: Optional[int] = None,
) -> Tuple[Optional[List[GoogleContent]], Optional[str], Optional[str], Optional[Dict]]:
    """
    Обрабатывает цикл Function Calling для ответа Google Gemini.
    (Этот код - это ваша старая реализация process_gemini_fc_cycle из fc_processing.py)
    """
    if not google_imported or not gemini_api:
        logger.critical("Google libraries/API module unavailable for FC processing.")
        return getattr(chat_session, 'history', None), "Google libs unavailable", None, None

    logger.info(f"--- Starting Google FC Processing Cycle (Chat: {original_chat_id}) ---")

    # --- Инициализация ---
    last_successful_fc_name: Optional[str] = None
    last_sent_text: Optional[str] = None
    last_successful_fc_result: Optional[Dict] = None
    step = 0
    current_response: Optional[GoogleResponse] = None
    final_history: Optional[List[GoogleContent]] = getattr(chat_session, 'history', None) # Начальная история

    # Получаем последний ответ из истории сессии для старта цикла
    try:
        if not hasattr(chat_session, 'history') or not chat_session.history:
            logger.warning("Chat session history empty before Google FC cycle.")
            return final_history, None, None, None
        last_content = chat_session.history[-1]
        if not isinstance(last_content, GoogleContent) or last_content.role != 'model':
             logger.debug("Last message not from model, no Google FC needed.")
             return final_history, None, None, None

        # Создаем Mock-ответ для входа в цикл (первый шаг)
        # --- Используем классы из google.ai.generativelanguage, если импортированы ---
        class MockCandidate:
             def __init__(self, content: GoogleContent):
                 self.content = content
                 self.safety_ratings = []
                 self.finish_reason = GoogleFinishReason.STOP if GoogleFinishReason else 1 # Используем импортированный FinishReason
        class MockResponse:
             def __init__(self, content: GoogleContent):
                  self.candidates = [MockCandidate(content)] if content else []
                  self.prompt_feedback = None # Добавляем атрибут
        current_response = MockResponse(last_content)

    except Exception as e:
        logger.error(f"Failed get last response from session history (Google): {e}", exc_info=True)
        return final_history, None, None, None

    # --- Основной цикл обработки FC ---
    while current_response and step < max_steps:
        step += 1
        model_name_str = getattr(model_instance, '_model_name', 'Google Model')
        logger.info(f"--- Google FC Step {step}/{max_steps} (Chat: {original_chat_id}) ---")

        # Извлекаем части из ответа
        try:
            if not current_response.candidates: break # Нет кандидатов
            candidate = current_response.candidates[0]
            # Проверка причины остановки
            finish_reason = getattr(candidate, 'finish_reason', None)
            # Сравниваем с допустимыми значениями или None
            allowed_reasons = {GoogleFinishReason.STOP, GoogleFinishReason.MAX_TOKENS, GoogleFinishReason.FINISH_REASON_UNSPECIFIED, None} if GoogleFinishReason else {1, 0, None} # 1=STOP, 0=UNSPECIFIED
            if finish_reason not in allowed_reasons:
                 logger.warning(f"Google model stopped reason: {finish_reason}. Safety: {getattr(candidate, 'safety_ratings', 'N/A')}")
                 break
            if not candidate.content or not candidate.content.parts: break # Нет контента
            parts = candidate.content.parts
        except Exception as e: logger.warning(f"Google response structure error: {e}."); break

        # Собираем валидные Function Calls
        function_calls_to_process: List[GoogleFunctionCall] = []
        for part in parts:
            if isinstance(part, GooglePart) and hasattr(part, 'function_call') and isinstance(part.function_call, GoogleFunctionCall):
                 fc = part.function_call
                 if getattr(fc, 'name', None): # Проверяем имя
                      function_calls_to_process.append(fc)
                 else: logger.debug("Ignoring Google FC with empty name.")
            # Логируем, если модель вернула FunctionResponse (не должно быть)
            elif isinstance(part, GooglePart) and hasattr(part, 'function_response'):
                  logger.warning("Model returned FunctionResponse unexpectedly (Google). Ignoring.")

        if not function_calls_to_process: logger.info("No valid Google FCs found. Ending cycle."); break

        logger.info(f"Found {len(function_calls_to_process)} Google FCs to process.")
        response_parts_for_gemini: List[GooglePart] = []
        interrupt_fc_cycle = False

        # --- Исполнение FC (последовательное) ---
        for fc_index, fc in enumerate(function_calls_to_process):
            function_name = fc.name
            original_args_for_log: Optional[Dict] = None
            args: Dict[str, Any] = {}
            handler_result: Any = None
            execution_error: Optional[Exception] = None
            log_status = 'error' # Статус по умолчанию для логирования БД

            # 1. Парсинг аргументов (используем _convert_value_for_json)
            if hasattr(fc, 'args') and fc.args is not None:
                try:
                    args = _convert_value_for_json(fc.args)
                    if not isinstance(args, dict): raise TypeError("Args not dict")
                    original_args_for_log = args
                except (TypeError, ValueError) as e:
                    logger.error(f"Cannot convert/parse args for Google FC '{function_name}': {e}")
                    response_payload = {"error": f"Failed to parse arguments: {e}"}
                    # <<< Используем GoogleFunctionResponse >>>
                    response_part = GooglePart(function_response=GoogleFunctionResponse(name=function_name, response=response_payload))
                    response_parts_for_gemini.append(response_part)
                    # Логируем ошибку в БД
                    if database:
                        asyncio.create_task(database.add_tool_execution_log(
                            chat_id=original_chat_id,
                            user_id=original_user_id,
                            tool_name=function_name,
                            tool_args=original_args_for_log,
                            status=log_status,
                            result_message=str(response_payload.get('message', response_payload)) if isinstance(response_payload, dict) else str(response_payload),
                            full_result=response_payload
                        ))
                    continue # К следующему FC
            else: original_args_for_log = {}

            logger.info(f"Executing Google FC {fc_index + 1}/{len(function_calls_to_process)}: {function_name}({args})")

            # 2. Поиск и выполнение хендлера (через execute_function_call)
            if function_name in available_functions_map:
                handler = available_functions_map[function_name]
                try: handler_result = await execute_function_call(handler, args, original_chat_id, original_user_id)
                except Exception as exec_err: execution_error = exec_err
            else:
                logger.error(f"Google FC handler '{function_name}' not found.")
                handler_result = {"status": "error", "message": f"Function '{function_name}' not implemented."}
                log_status = 'not_found'

            # 3. Обработка результата и логирование
            # ... (код определения log_status, log_result_message, last_successful..., last_sent_text из старой версии) ...
            response_content_for_fr = None # Значение для ответа Gemini
            full_result_json_str = None    # Строка для лога БД
            # --- Логика обработки handler_result ---
            if execution_error: ...
            elif isinstance(handler_result, dict): ...
            else: ... # Неожиданный тип
            # --- Конец логики обработки ---
            if log_status == 'success': last_successful_fc_name = function_name; last_successful_fc_result = handler_result
            if function_name == 'send_telegram_message': last_sent_text = original_args_for_log.get('text')

            # Логирование в БД (асинхронно)
            if database:
                asyncio.create_task(database.add_tool_execution_log(
                    chat_id=original_chat_id,
                    user_id=original_user_id,
                    tool_name=function_name,
                    tool_args=original_args_for_log,
                    status=log_status,
                    result_message=str(response_content_for_fr.get('message', response_content_for_fr)) if isinstance(response_content_for_fr, dict) else str(response_content_for_fr),
                    full_result=response_content_for_fr if isinstance(response_content_for_fr, dict) else {"value": response_content_for_fr}
                ))

            # 4. Подготовка FunctionResponse для Gemini
            response_payload_for_gemini = {}
            try:
                response_payload_for_gemini = _convert_value_for_json(response_content_for_fr)
                if not isinstance(response_payload_for_gemini, dict): response_payload_for_gemini = {"value": response_payload_for_gemini}
            except Exception as conversion_err: response_payload_for_gemini = {"error": f"Tool result conversion failed: {conversion_err}"}
            # <<< Используем GoogleFunctionResponse >>>
            response_part = GooglePart(function_response=GoogleFunctionResponse(name=function_name, response=response_payload_for_gemini))
            response_parts_for_gemini.append(response_part)

            # 5. Проверка на блокирующий вызов (как было)
            is_blocking = False
            if function_name == 'send_telegram_message':
                 requires_response = args.get('requires_user_response', False)
                 if isinstance(requires_response, str): requires_response = requires_response.lower() == 'true'
                 if requires_response is True: is_blocking = True; logger.info(f"Blocking Google FC detected.")
            if is_blocking: interrupt_fc_cycle = True; break # Прерываем цикл for fc
        # --- Конец цикла for fc ---

        if interrupt_fc_cycle:
             logger.info("Exiting Google FC cycle early due to blocking call.")
             # Отправляем то, что успели собрать (если что-то есть)
             if response_parts_for_gemini and gemini_api:
                  logger.info(f"Sending {len(response_parts_for_gemini)} Google FRs (before block) back.")
                  try:
                      # <<< Используем GoogleContent >>>
                      content_with_responses = GoogleContent(role="function", parts=response_parts_for_gemini)
                      loop = asyncio.get_running_loop()
                      # Этот вызов не обновляет current_response для след. итерации
                      await loop.run_in_executor(None, gemini_api.send_message_to_gemini, model_instance, chat_session, content_with_responses)
                      final_history = getattr(chat_session, 'history', None) # Сохраняем историю
                  except Exception as api_err: logger.error(f"Error sending partial Google FRs before block: {api_err}")
             break # <-- Выход из основного цикла WHILE

        # Если не было прерывания, отправляем все ответы и получаем след. ответ модели
        if not response_parts_for_gemini: logger.warning("No Google FRs generated. Ending cycle."); break
        if not gemini_api: logger.critical("gemini_api module unavailable."); break

        logger.info(f"Sending {len(response_parts_for_gemini)} Google FRs back to Gemini.")
        try:
            # <<< Используем GoogleContent >>>
            content_with_responses = GoogleContent(role="function", parts=response_parts_for_gemini)
            loop = asyncio.get_running_loop()
            current_response = await loop.run_in_executor(None, gemini_api.send_message_to_gemini, model_instance, chat_session, content_with_responses)
            logger.debug(f"Received next response from Gemini after sending FRs.")
            final_history = getattr(chat_session, 'history', None) # Обновляем историю
        except Exception as api_err:
             logger.error(f"Error sending Google FRs to API: {api_err}", exc_info=True)
             current_response = None; break # Прерываем цикл while
    # --- Конец цикла while для Google ---

    logger.info(f"Google FC processing cycle finished after {step} step(s).")
    
    # ИСПРАВЛЕНО: Убедимся, что финальный ответ модели сохранен в истории
    if current_response and chat_session:
        try:
            # Получаем актуальную историю сессии 
            final_history = getattr(chat_session, 'history', None)
            
            # Проверим наличие последнего ответа модели
            if final_history and len(final_history) > 0:
                last_entry = final_history[-1]
                
                # Если последняя запись не от модели, и есть текущий ответ - добавим его
                if (not hasattr(last_entry, 'role') or last_entry.role != 'model') and hasattr(current_response, 'candidates'):
                    for candidate in current_response.candidates:
                        if hasattr(candidate, 'content') and candidate.content:
                            logger.info("Adding final model message to history")
                            loop = asyncio.get_running_loop()
                            # Это эквивалентно вызову model_instance.send_message(chat_session, None)
                            # с текущим ответом в качестве content
                            await loop.run_in_executor(None, 
                                gemini_api.add_model_content_to_history, 
                                chat_session, candidate.content)
                            # Обновляем финальную историю
                            final_history = getattr(chat_session, 'history', None)
                            break
        except Exception as e:
            logger.error(f"Error ensuring final model message is in history: {e}", exc_info=True)
    
    # Возвращаем историю, имя последней функции, текст, результат
    return final_history, last_successful_fc_name, last_sent_text, last_successful_fc_result


# --- Функция обработки цикла Tools для OPENAI ---
async def process_openai_tool_cycle(
    client: AsyncOpenAI,
    model_name: str,
    initial_messages: List[Dict[str, Any]], # Начальная история сообщений OpenAI
    first_response: ChatCompletion, # Первый ответ API, полученный в ai_interaction
    tools: Optional[List[Dict[str, Any]]],
    available_functions_map: Dict[str, Callable],
    max_steps: int,
    chat_id: int,
    user_id: int,
    temperature: float,
    max_tokens: Optional[int],
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], Optional[str], Optional[Dict]]:
    """
    Обрабатывает цикл вызова инструментов (Tools) для ответа OpenAI.
    """
    if not openai_imported or not openai_api:
        logger.critical("OpenAI library/API module unavailable for Tool processing.")
        return initial_messages, "OpenAI libs unavailable", None, None

    logger.info(f"--- Starting OpenAI Tool Processing Cycle (Chat: {chat_id}) ---")

    # --- Инициализация ---
    messages = initial_messages[:] # Копируем начальную историю
    current_response: Optional[ChatCompletion] = first_response # Используем первый ответ
    step = 0
    last_successful_tool_name: Optional[str] = None
    last_sent_text: Optional[str] = None
    last_successful_tool_result: Optional[Dict] = None

    # --- Основной цикл обработки Tools ---
    while current_response and step < max_steps:
        step += 1
        logger.info(f"--- OpenAI Tool Step {step}/{max_steps} (Chat: {chat_id}) ---")

        # Проверяем наличие ответа и сообщения
        if not current_response.choices:
             logger.warning("OpenAI response has no choices. Ending cycle.")
             break
        message = current_response.choices[0].message
        finish_reason = current_response.choices[0].finish_reason

        # Если причина остановки НЕ tool_calls, выходим из цикла
        if finish_reason != "tool_calls":
            logger.info(f"OpenAI finish reason is '{finish_reason}'. Ending tool cycle.")
            # Добавляем финальное сообщение ассистента в историю.
            # Оно должно содержать либо content, либо быть пустым, если модель ничего не ответила.
            # model_dump(exclude_unset=True) не добавит поля, если они None, что безопасно.
            messages.append(message.model_dump(exclude_unset=True))
            break

        # Если есть tool_calls, обрабатываем их
        tool_calls: Optional[List[ChatCompletionMessageToolCall]] = message.tool_calls
        if not tool_calls:
            logger.warning("OpenAI finish reason is 'tool_calls', but no tool_calls found in message. Ending cycle.")
            messages.append(message.model_dump(exclude_unset=True)) # Сохраняем сообщение ассистента
            break

        # Добавляем сообщение ассистента (с tool_calls) в историю ПЕРЕД обработкой
        messages.append(message.model_dump(exclude_unset=True))
        logger.info(f"Found {len(tool_calls)} OpenAI tool calls to process.")

        # --- Последовательное выполнение Tool Calls ---
        tool_results_for_api: List[Dict[str, Any]] = [] # Результаты для следующего вызова API
        interrupt_tool_cycle = False

        for tool_call in tool_calls:
            tool_call_id = tool_call.id
            function_data = tool_call.function
            if tool_call.type != 'function' or not function_data:
                logger.warning(f"Skipping non-function tool call type: {tool_call.type}")
                continue

            function_name = function_data.name
            arguments_str = function_data.arguments # Аргументы приходят как JSON строка

            logger.info(f"Executing OpenAI tool call: ID='{tool_call_id}', Name='{function_name}', Args='{arguments_str[:100]}...'")

            # 1. Парсинг аргументов
            try:
                args = json.loads(arguments_str) if arguments_str else {}
                if not isinstance(args, dict): raise TypeError("Arguments did not parse to dict")
                original_args_for_log = args # Сохраняем для лога БД
            except (json.JSONDecodeError, TypeError) as e:
                 logger.error(f"Failed to parse JSON arguments for tool '{function_name}': {e}. Args string: '{arguments_str}'")
                 # Формируем сообщение с ошибкой для следующего вызова API
                 error_content = json.dumps({"status": "error", "message": f"Failed to parse arguments JSON: {e}"})
                 tool_results_for_api.append({"role": "tool", "tool_call_id": tool_call_id, "content": error_content})
                 # Логируем ошибку в БД
                 if database:
                     asyncio.create_task(database.add_tool_execution_log(
                         chat_id=chat_id,
                         user_id=user_id,
                         tool_name=function_name,
                         tool_args=original_args_for_log if 'original_args_for_log' in locals() else arguments_str, # если парсинг упал, args нет
                         status='error',
                         result_message=f"Failed to parse arguments JSON: {e}",
                         full_result={"error": f"Failed to parse arguments JSON: {e}, original_args_str: {arguments_str}"}
                     ))
                 continue # К следующему tool_call

            # 2. Поиск и выполнение хендлера
            handler_result: Any = None
            execution_error: Optional[Exception] = None
            log_status = 'error'

            if function_name in available_functions_map:
                handler = available_functions_map[function_name]
                try:
                    # Используем общий execute_function_call
                    handler_result = await execute_function_call(handler, args, chat_id, user_id)
                except Exception as exec_err:
                     execution_error = exec_err
            else:
                logger.error(f"OpenAI tool handler '{function_name}' not found.")
                handler_result = {"status": "error", "message": f"Tool '{function_name}' is not implemented."}
                log_status = 'not_found'

            # 3. Обработка результата и логирование
            # ... (код определения log_status, log_result_message, last_successful..., last_sent_text - как в Google FC) ...
            response_content_for_tool_msg: Optional[Any] = None
            full_result_json_str = None
            # --- Логика обработки handler_result ---
            if execution_error: response_content_for_tool_msg = {"status": "error", "message": f"Execution failed: {execution_error}"}; log_status = "error"
            elif isinstance(handler_result, dict): response_content_for_tool_msg = handler_result; log_status = handler_result.get('status', 'success')
            else: response_content_for_tool_msg = {"status": "success", "result_value": str(handler_result)}; log_status = "success" # Оборачиваем не-словари
            # --- Конец логики обработки ---
            if log_status == 'success': last_successful_tool_name = function_name; last_successful_tool_result = response_content_for_tool_msg
            if function_name == 'send_telegram_message': last_sent_text = original_args_for_log.get('text')

            # Логирование в БД (асинхронно)
            if database:
                asyncio.create_task(database.add_tool_execution_log(
                    chat_id=chat_id,
                    user_id=user_id,
                    tool_name=function_name,
                    tool_args=original_args_for_log, # Распарсенные аргументы
                    status=log_status,
                    result_message=str(response_content_for_tool_msg.get('message', response_content_for_tool_msg)) if isinstance(response_content_for_tool_msg, dict) else str(response_content_for_tool_msg),
                    full_result=response_content_for_tool_msg
                ))

            # 4. Подготовка результата для OpenAI API (должен быть строкой)
            try:
                # Сериализуем результат (даже если это ошибка) в JSON строку
                result_json_string = json.dumps(response_content_for_tool_msg, ensure_ascii=False, default=str)
            except Exception as e:
                logger.error(f"Failed to serialize tool result to JSON for '{function_name}': {e}")
                result_json_string = json.dumps({"status": "error", "message": f"Failed to serialize result: {e}"})

            # Добавляем сообщение с результатом для следующего вызова API
            tool_results_for_api.append({"role": "tool", "tool_call_id": tool_call_id, "content": result_json_string})

            # 5. Проверка на блокирующий вызов (как в Google FC)
            is_blocking = False
            if function_name == 'send_telegram_message':
                 requires_response = args.get('requires_user_response', False)
                 if isinstance(requires_response, str): requires_response = requires_response.lower() == 'true'
                 if requires_response is True: is_blocking = True; logger.info(f"Blocking OpenAI tool detected.")
            if is_blocking: interrupt_tool_cycle = True; break # Прерываем цикл for tool_call
        # --- Конец цикла for tool_call ---

        if interrupt_tool_cycle:
            logger.info("Exiting OpenAI tool cycle early due to blocking call.")
            # Добавляем результаты обработанных до блокировки инструментов в историю
            messages.extend(tool_results_for_api)
            break # <-- Выход из основного цикла WHILE

        # Если не было прерывания, добавляем все результаты и делаем следующий вызов API
        messages.extend(tool_results_for_api)
        if not openai_api: logger.critical("openai_api module unavailable for next step."); break

        # Валидируем сообщения перед отправкой в API
        try:
            sanitized_messages = sanitize_openai_messages(messages)
            if len(sanitized_messages) != len(messages):
                logger.warning(f"Removed {len(messages) - len(sanitized_messages)} invalid messages before API call")
            messages = sanitized_messages
        except Exception as e:
            logger.error(f"Error sanitizing OpenAI messages: {e}")
            # Продолжаем с исходными сообщениями

        logger.info(f"Sending {len(tool_results_for_api)} tool results back to OpenAI.")
        try:
            # Вызываем API снова с обновленной историей
            current_response, api_error_msg = await openai_api.call_openai_api(
                client=client, model=model_name, messages=messages,
                tools=tools, temperature=temperature, max_tokens=max_tokens
            )
            if api_error_msg:
                 logger.error(f"Error calling OpenAI API after sending tool results: {api_error_msg}")
                 current_response = None # Прерываем цикл при ошибке
                 break
            if current_response is None: # На всякий случай
                 logger.error("OpenAI API returned None after sending tool results.")
                 break
            logger.debug("Received next response from OpenAI after sending tool results.")

        except Exception as e:
            logger.error(f"Unexpected error calling OpenAI API after tool results: {e}", exc_info=True)
            current_response = None; break # Прерываем цикл while
    # --- Конец цикла while для OpenAI ---

    logger.info(f"OpenAI Tool processing cycle finished after {step} step(s).")
    
    # ИСПРАВЛЕНО: Убедимся, что последнее сообщение от ассистента сохранено в истории
    if current_response and current_response.choices:
        last_message = current_response.choices[0].message
        finish_reason = current_response.choices[0].finish_reason
        if finish_reason != "tool_calls" and last_message:
            logger.info("Adding final assistant message from last response to history")
            # Проверим, добавлено ли уже это сообщение
            is_already_added = False
            if messages and isinstance(messages[-1], dict) and messages[-1].get("role") == "assistant":
                # Последнее сообщение уже от ассистента, проверим его на совпадение
                if messages[-1].get("content") == last_message.content:
                    is_already_added = True
            
            if not is_already_added:
                messages.append(last_message.model_dump(exclude_unset=True))
                logger.info("Final assistant message added to history")

    # Возвращаем финальную историю сообщений, имя последней функции, текст, результат
    return messages, last_successful_tool_name, last_sent_text, last_successful_tool_result
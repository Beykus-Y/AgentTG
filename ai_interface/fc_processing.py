import asyncio
import inspect
import logging
import json # Для логирования
from typing import Dict, Any, List, Optional, Tuple, Callable

# --- Локальные импорты из новой структуры ---
try:
    # Импортируем модуль gemini_api целиком для доступа к send_message_to_gemini
    from . import gemini_api
    from utils.helpers import escape_markdown_v2 # Утилита экранирования
    # <<< ДОБАВЛЕНО: Импорт database >>>
    from utils.converters import _convert_value_for_json
    import database
except ImportError:
     logging.critical("CRITICAL: Failed to import dependencies (gemini_api, utils.helpers, database) in fc_processing.", exc_info=True)
     gemini_api = None # type: ignore
     database = None # type: ignore
     def escape_markdown_v2(text: str) -> str: return text # Заглушка

# --- Типы Google ---
try:
    # <<< ВОЗВРАЩАЕМ glm >>>
    from google.ai import generativelanguage as glm
    Part = glm.Part
    FunctionResponse = glm.FunctionResponse
    FunctionCall = glm.FunctionCall
    Content = glm.Content # Определяем Content через glm
    try:
        FinishReason = glm.Candidate.FinishReason
    except AttributeError: FinishReason = None

    # <<< GenerateContentResponse из types >>>
    from google.generativeai.types import GenerateContentResponse

    logger = logging.getLogger(__name__)
    logger.debug("Successfully imported Google types in fc_processing")
except ImportError as e:
    logger = logging.getLogger(__name__)
    logger.critical(f"CRITICAL: Failed to import Google types in fc_processing: {e}", exc_info=True)
    # <<< Обновляем заглушки >>>
    Part, Content, FunctionResponse, FunctionCall, FinishReason, GenerateContentResponse = Any, Any, Any, Any, Any, Any

logger = logging.getLogger(__name__)

async def execute_function_call(
        handler_func: Callable,
        args: Dict[str, Any], # Аргументы, предложенные моделью Gemini
        chat_id_for_handlers: Optional[int] = None, # ID чата, откуда пришел запрос
        user_id_for_handlers: Optional[int] = None  # ID пользователя, отправившего запрос
) -> Any:
    """
    Асинхронно выполняет хендлер инструмента (синхронный или асинхронный),
    передавая ему аргументы. Приоритет отдается аргументам, предложенным
    моделью Gemini (в словаре `args`). Если модель не предложила аргумент,
    а функция его ожидает (например, 'chat_id' или 'user_id'), то используются
    значения `chat_id_for_handlers` или `user_id_for_handlers`.

    Args:
        handler_func: Асинхронная или синхронная функция-обработчик инструмента.
        args: Словарь аргументов, полученный от модели Gemini.
        chat_id_for_handlers: ID чата для передачи в хендлер (если модель не передала).
        user_id_for_handlers: ID пользователя (отправителя) для передачи в хендлер (если модель не передала).

    Returns:
        Результат выполнения хендлера или словарь с ошибкой.
    """
    handler_sig = inspect.signature(handler_func)
    # Начинаем с аргументов, предоставленных моделью AI
    final_args = args.copy()

    # --- ИСПРАВЛЕННАЯ ЛОГИКА ВНЕДРЕНИЯ ID ---

    # 1. Обработка chat_id:
    #    Внедряем ID чата отправителя, ТОЛЬКО если функция его ожидает
    #    И если он НЕ был предоставлен самой моделью в args.
    if 'chat_id' in handler_sig.parameters and 'chat_id' not in args:
        if chat_id_for_handlers is not None:
            final_args['chat_id'] = chat_id_for_handlers
            logger.debug(f"Injecting sender chat_id ({chat_id_for_handlers}) into args for {handler_func.__name__}")
        else:
            # Логируем, только если chat_id был обязательным параметром без значения по умолчанию
            param_obj = handler_sig.parameters['chat_id']
            if param_obj.default is inspect.Parameter.empty:
                logger.warning(f"Handler '{handler_func.__name__}' expects mandatory 'chat_id', but it was not provided by AI or sender context.")

    # 2. Обработка user_id:
    #    Аналогично, внедряем ID пользователя-отправителя, ТОЛЬКО если
    #    функция ожидает 'user_id', и модель НЕ предоставила его в args.
    #    ВАЖНО: Если инструмент должен работать с ID ДРУГОГО пользователя,
    #    модель ДОЛЖНА передать этот ID в аргументе 'user_id' (или 'target_user_id').
    #    Эта логика гарантирует, что ID от модели имеет приоритет.
    if 'user_id' in handler_sig.parameters and 'user_id' not in args:
         if user_id_for_handlers is not None:
            final_args['user_id'] = user_id_for_handlers
            logger.debug(f"Injecting sender user_id ({user_id_for_handlers}) as 'user_id' into args for {handler_func.__name__}")
         else:
            param_obj = handler_sig.parameters['user_id']
            if param_obj.default is inspect.Parameter.empty:
                 logger.warning(f"Handler '{handler_func.__name__}' expects mandatory 'user_id', but it was not provided by AI or sender context.")

    # --- КОНЕЦ ИСПРАВЛЕННОЙ ЛОГИКИ ---

    # 3. Фильтрация аргументов:
    #    Оставляем только те аргументы из final_args (уже с возможными
    #    добавлениями chat_id/user_id отправителя), которые действительно
    #    принимает функция-хендлер.
    filtered_args = {k: v for k, v in final_args.items() if k in handler_sig.parameters}

    # 4. Проверка обязательных аргументов:
    #    Убеждаемся, что все параметры функции, у которых нет значения
    #    по умолчанию, присутствуют в `filtered_args`.
    missing_args = [
        p_name for p_name, p_obj in handler_sig.parameters.items()
        if p_obj.default is inspect.Parameter.empty and p_name not in filtered_args
    ]
    if missing_args:
        err_msg = f"Missing required arguments for '{handler_func.__name__}': {', '.join(missing_args)}. Provided args from AI: {list(args.keys())}, Final filtered args: {list(filtered_args.keys())}"
        logger.error(err_msg)
        return {"status": "error", "message": err_msg} # Возвращаем ошибку

    # Логируем финальные аргументы перед вызовом
    logger.debug(f"Executing handler '{handler_func.__name__}' with final args: {filtered_args}")

    # 5. Выполнение хендлера:
    try:
        if asyncio.iscoroutinefunction(handler_func):
            # Если хендлер асинхронный, просто await его
            return await handler_func(**filtered_args)
        else:
            # Если хендлер синхронный, запускаем его в executor'е
            loop = asyncio.get_running_loop()
            from functools import partial
            # functools.partial нужен, чтобы передать аргументы в функцию,
            # которая будет вызвана в другом потоке/процессе executor'а.
            func_call = partial(handler_func, **filtered_args)
            return await loop.run_in_executor(None, func_call)
    except Exception as exec_err:
        # Ловим любые ошибки во время выполнения самого хендлера
        err_msg = f"Handler execution failed for function '{handler_func.__name__}': {exec_err}"
        logger.error(f"Error executing handler '{handler_func.__name__}' with args {filtered_args}: {exec_err}", exc_info=True)
        # Возвращаем словарь с ошибкой, чтобы Gemini знал о проблеме
        return {"status": "error", "message": err_msg}


async def process_gemini_fc_cycle(
    model_instance: Any, # Экземпляр genai.GenerativeModel
    chat_session: Any,   # Экземпляр genai.ChatSession
    available_functions_map: Dict[str, Callable],
    max_steps: int,
    original_chat_id: Optional[int] = None,
    original_user_id: Optional[int] = None,
) -> Tuple[Optional[List[Content]], Optional[str], Optional[str], Optional[Dict]]:
    """
    Обрабатывает цикл Function Calling для ответа Gemini.
    Отправляет ответы на все Function Calls одним запросом к API.
    Возвращает: (final_history, last_successful_fc_name, last_sent_text, last_successful_fc_result)
    """
    # Проверка импорта типов Google
    if not all([Part, Content, FunctionResponse, FunctionCall, GenerateContentResponse]):
         logger.critical("Missing Google types! Cannot process Function Calling.")
         # <<< ИСПРАВЛЕНИЕ ValueError: Возвращаем 4 значения >>>
         return getattr(chat_session, 'history', None), None, None, None

    # <<< ИНИЦИАЛИЗАЦИЯ: Гарантируем инициализацию всех возвращаемых значений >>>
    last_successful_fc_name: Optional[str] = None
    last_sent_text: Optional[str] = None
    last_successful_fc_result: Optional[Dict] = None
    step = 0
    current_response: Optional[GenerateContentResponse] = None
    final_history: Optional[List[Content]] = getattr(chat_session, 'history', None)

    # Получаем последний ответ из истории сессии (инициализация current_response)
    try:
        if not hasattr(chat_session, 'history') or not chat_session.history:
             logger.warning("Chat session history is empty or missing before FC cycle.")
             # <<< ИСПРАВЛЕНИЕ ValueError: Возвращаем 4 значения >>>
             return final_history, None, None, None # Используем инициализированный final_history

        last_content = chat_session.history[-1]
        if not isinstance(last_content, Content):
             logger.error(f"Last history item is not Content object: {type(last_content)}")
             # <<< ИСПРАВЛЕНИЕ ValueError: Возвращаем 4 значения >>>
             return final_history, None, None, None
        if last_content.role != 'model':
             logger.debug("Last message in history is not from model, no FC cycle needed.")
             # <<< ИСПРАВЛЕНИЕ ValueError: Возвращаем 4 значения >>>
             return final_history, None, None, None

        # Создаем Mock-ответ для входа в цикл (первый шаг)
        class MockResponse:
            def __init__(self, content: Content):
                class MockCandidate:
                    def __init__(self, content: Content):
                        self.content = content
                        self.safety_ratings = []
                        self.finish_reason = FinishReason.STOP if hasattr(FinishReason, 'STOP') else 1
                self.candidates = [MockCandidate(content)] if content else []
        current_response = MockResponse(last_content)

    except Exception as e:
         logger.error(f"Failed to get last response from session history: {e}", exc_info=True)
         # <<< ИСПРАВЛЕНИЕ ValueError: Возвращаем 4 значения >>>
         return final_history, None, None, None

    # Основной цикл обработки FC
    while current_response and step < max_steps:
        step += 1
        model_name_str = getattr(model_instance, '_model_name', 'Unknown Model') # Попробуем достать имя модели
        logger.info(f"--- FC Analysis ({model_name_str} Step {step}/{max_steps}) Chat: {original_chat_id} ---")

        # Извлекаем части из ответа
        try:
            if not current_response.candidates:
                logger.info(f"No candidates in response for step {step}. Ending FC cycle.")
                break # Выход из цикла while
            candidate = current_response.candidates[0]
            # Проверка на блокировку контента
            if hasattr(candidate, 'finish_reason') and candidate.finish_reason not in (FinishReason.STOP, FinishReason.MAX_TOKENS, FinishReason.FINISH_REASON_UNSPECIFIED, 1, 0): # 1=STOP, 0=UNSPECIFIED (примерные значения)
                 logger.warning(f"Model response stopped with reason: {candidate.finish_reason}. Ending FC cycle. Safety: {getattr(candidate, 'safety_ratings', 'N/A')}")
                 break # Выход из цикла while
            if not candidate.content or not candidate.content.parts:
                finish_reason = getattr(candidate, 'finish_reason', 'N/A')
                logger.info(f"No content/parts in candidate (Finish reason: {finish_reason}). Ending FC cycle.")
                break # Выход из цикла while
            parts = candidate.content.parts
        except Exception as e:
            logger.warning(f"Response structure error accessing parts: {e}.")
            break # Выход из цикла while

        # Собираем валидные Function Calls для обработки
        function_calls_to_process: List[FunctionCall] = []
        for part in parts:
            # --- Обработка FunctionCall ---
            if isinstance(part, Part) and hasattr(part, 'function_call') and part.function_call is not None:
                fc = part.function_call
                fc_name = getattr(fc, 'name', None) # Безопасно получаем имя
                # Проверяем, что имя функции существует и НЕ ПУСТОЕ
                if isinstance(fc, FunctionCall) and fc_name: # Проверяем, что имя не пустое
                    function_calls_to_process.append(fc)
                    logger.debug(f"Found valid FC to process: {fc_name}")
                elif isinstance(fc, FunctionCall): # Если это FunctionCall, но имя пустое
                    logger.debug(f"Ignoring FunctionCall with empty name found in model response part: {fc}")
                else: # Если это не FunctionCall или имя None (маловероятно)
                    logger.error(f"MODEL ERROR: Found invalid/malformed FunctionCall object in model response part. IGNORING this FC. Object: {fc}")

            # --- Обработка FunctionResponse (Логирование аномалии) ---
            # Логируем, если вдруг модель вернула FunctionResponse
            if isinstance(part, Part) and hasattr(part, 'function_response') and part.function_response is not None:
                 fr = part.function_response
                 fr_name = getattr(fr, 'name', None)
                 if isinstance(fr, FunctionResponse) and fr_name: # Если имя есть и не пустое
                     logger.warning(f"MODEL WARNING: Found function_response with name '{fr_name}' in MODEL response parts (should not happen). IGNORING this FR.")
                 else: # Если имя пустое или отсутствует
                     logger.debug(f"Ignoring FunctionResponse with empty/missing name found in model response part: {fr}")

        # Если нет FC для обработки, выходим из цикла
        if not function_calls_to_process:
            logger.info("No valid Function Calls found in this step. Ending FC cycle.")
            break # Выход из цикла while

        logger.info(f"Found {len(function_calls_to_process)} valid FCs by {model_name_str} to process.")

        # Готовим и запускаем задачи выполнения хендлеров
        response_parts_for_gemini: List[Part] = [] # Список ответов для Gemini
        # <<< ИЗМЕНЕНО: Храним (task, original_args) >>>
        interrupt_fc_cycle = False

        for fc_index, fc in enumerate(function_calls_to_process):
            function_name = fc.name
            original_args_for_log: Optional[Dict] = None
            args: Dict[str, Any] = {}
            handler_result: Any = None
            execution_error: Optional[Exception] = None
            log_status = 'error' # Статус по умолчанию для логирования

            # 1. Парсинг аргументов (как и раньше)
            if hasattr(fc, 'args') and fc.args is not None:
                try:
                    args = _convert_value_for_json(fc.args) # Используем импортированную функцию
                    if not isinstance(args, dict): raise TypeError("Args not dict")
                    original_args_for_log = args
                except (TypeError, ValueError) as e:
                    logger.error(f"Cannot convert/parse args for FC '{function_name}': {e}")
                    # Формируем ответ об ошибке для Gemini
                    response_payload = {"error": f"Failed to parse arguments: {e}"}
                    response_part = Part(function_response=FunctionResponse(name=function_name, response=response_payload))
                    response_parts_for_gemini.append(response_part)
                    # Логируем ошибку в БД
                    if database:
                         try:
                              error_args_log = {"raw_args": str(getattr(fc, 'args', 'MISSING')), "parsing_error": str(e)}
                              asyncio.create_task(database.add_tool_execution_log(
                                   chat_id=original_chat_id, user_id=original_user_id, tool_name=function_name,
                                   tool_args=error_args_log, status='error', result_message=f"Argument parsing failed: {e}"
                              )) # Логируем асинхронно
                         except Exception as log_err: logger.error(f"DB log error (arg parse fail): {log_err}")
                    continue # Переходим к следующему FC в пачке
            else:
                original_args_for_log = {}

            logger.info(f"Executing FC {fc_index + 1}/{len(function_calls_to_process)} sequentially: {function_name}({args}) for chat {original_chat_id}")

            # 2. Поиск и выполнение хендлера
            if function_name in available_functions_map:
                handler = available_functions_map[function_name]
                try:
                    handler_result = await execute_function_call(
                        handler_func=handler,
                        args=args,
                        chat_id_for_handlers=original_chat_id,
                        user_id_for_handlers=original_user_id
                    )
                except Exception as exec_err:
                     execution_error = exec_err # Сохраняем ошибку выполнения
                     # handler_result остается None
            else:
                logger.error(f"Function handler '{function_name}' not found.")
                handler_result = {"status": "error", "message": f"Function '{function_name}' is not implemented or available."}
                log_status = 'not_found' # Уточняем статус для лога

            # 3. Обработка результата и логирование
            log_return_code = None
            log_result_message = None
            log_stdout = None
            log_stderr = None
            response_content_for_fr = None
            full_result_json_str = None

            if execution_error:
                 log_status = 'error'
                 log_result_message = f"Execution failed: {execution_error}"
                 response_content_for_fr = {"error": log_result_message}
            elif isinstance(handler_result, dict):
                 log_stdout = handler_result.get('stdout')
                 log_stderr = handler_result.get('stderr')
                 log_return_code = handler_result.get('returncode')
                 if 'status' in handler_result and handler_result['status'] in {'success', 'error', 'not_found', 'warning', 'timeout'}:
                     log_status = handler_result['status']
                     log_result_message = handler_result.get('message', handler_result.get('error')) if log_status == 'error' else handler_result.get('message')
                 elif 'error' in handler_result:
                     log_status = 'error'; log_result_message = handler_result['error']
                 else:
                     log_status = 'success'; log_result_message = handler_result.get('message')

                 response_content_for_fr = handler_result

                 if log_status == 'success':
                    last_successful_fc_name = function_name
                    last_successful_fc_result = handler_result
                    if function_name == 'send_telegram_message':
                         last_sent_text = original_args_for_log.get('text')
                         logger.info(f"Recorded sent text via send_telegram_message: '{last_sent_text[:50]}...'")
                         response_content_for_fr = {"status": "success", "message": "Message queued for sending."} # Упрощаем ответ для Gemini
            else: # Неожиданный тип результата
                 log_status = 'success' # Предполагаем успех
                 log_result_message = f"Handler returned non-dict/non-exception: {type(handler_result)} - {str(handler_result)[:100]}..."
                 logger.warning(f"Handler '{function_name}' returned unexpected result type: {type(handler_result)}")
                 response_content_for_fr = {"result_value": str(handler_result)} # Оборачиваем в словарь

            # Логирование в БД (асинхронно)
            if database:
                try:
                     full_result_json_str = json.dumps(handler_result, ensure_ascii=False, default=str)
                except Exception as json_full_err:
                     logger.error(f"Failed serialize full_result tool log '{function_name}': {json_full_err}")
                     full_result_json_str = json.dumps({"error": f"Full result serialization failed: {json_full_err}"})
                asyncio.create_task(database.add_tool_execution_log(
                    chat_id=original_chat_id, user_id=original_user_id, tool_name=function_name, tool_args=original_args_for_log,
                    status=log_status, return_code=log_return_code, result_message=log_result_message,
                    stdout=log_stdout, stderr=log_stderr, full_result=full_result_json_str, trigger_message_id=None
                )) # Логируем асинхронно

            # 4. Подготовка FunctionResponse для Gemini
            response_payload_for_gemini = {}
            try:
                response_payload_for_gemini = _convert_value_for_json(response_content_for_fr)
                if not isinstance(response_payload_for_gemini, dict):
                    response_payload_for_gemini = {"value": response_payload_for_gemini}
            except Exception as conversion_err:
                 logger.error(f"Explicit conversion tool result failed '{function_name}': {conversion_err}")
                 response_payload_for_gemini = {"error": f"Tool result conversion failed: {conversion_err}"}

            # Добавляем результат в список для отправки в API
            response_part = Part(function_response=FunctionResponse(name=function_name, response=response_payload_for_gemini))
            response_parts_for_gemini.append(response_part)

            # 5. Проверка на блокирующий вызов
            is_blocking = False
            # Пример: считаем блокирующим вызов send_telegram_message, если текст заканчивается на "?"
            if function_name == 'send_telegram_message':
                # Получаем значение аргумента, по умолчанию False
                requires_response = args.get('requires_user_response', False)
                # Убедимся, что значение булево (модель может вернуть строку 'true'/'false')
                if isinstance(requires_response, str):
                    requires_response = requires_response.lower() == 'true'

                if requires_response is True: # Явная проверка на True
                    logger.info(f"Blocking FC detected ({function_name} with requires_user_response=True). Interrupting batch after this call.")
                    is_blocking = True

            # Если вызов блокирующий, прерываем обработку ОСТАЛЬНЫХ FC из этой пачки
            if is_blocking:
                 interrupt_fc_cycle = True # Устанавливаем флаг для внешнего цикла
                 break # Прерываем цикл for fc in function_calls_to_process

        # --- КОНЕЦ внутреннего цикла for fc in function_calls_to_process ---

        # Если цикл был прерван блокирующим вызовом, выходим из основного цикла while
        if interrupt_fc_cycle:
             logger.info("Exiting FC cycle early due to blocking call. Sending executed responses back.")
             # Отправляем те ответы, что успели собрать ДО блокирующего вызова
             if response_parts_for_gemini:
                  logger.info(f"Sending {len(response_parts_for_gemini)} function responses (before block) back to Gemini for chat {original_chat_id}.")
                  try:
                      content_with_responses = Content(role="function", parts=response_parts_for_gemini)
                      loop = asyncio.get_running_loop()
                      # Важно: Этот вызов НЕ обновляет current_response для СЛЕДУЮЩЕЙ итерации,
                      # так как мы прерываем цикл while. Его результат нам не нужен.
                      await loop.run_in_executor(
                           None, gemini_api.send_message_to_gemini,
                           model_instance, chat_session, content_with_responses
                      )
                      final_history = getattr(chat_session, 'history', None) # Сохраняем историю до прерывания
                  except Exception as api_err:
                       logger.error(f"Error sending partial function responses to Gemini API before blocking exit: {api_err}", exc_info=True)
                       # Не прерываем здесь, просто логируем ошибку отправки
             else:
                  logger.warning("Blocking call detected, but no responses to send back (e.g., error occurred before block).")
             break # <-- ВЫХОДИМ ИЗ ОСНОВНОГО ЦИКЛА WHILE

        # Если все FC в пачке обработаны БЕЗ прерывания
        # Отправляем собранные ответы Gemini и получаем следующий ответ модели
        if not response_parts_for_gemini:
             logger.warning("No response parts generated for Gemini in this step, though FCs were present. Ending cycle.")
             break

        logger.info(f"Sending {len(response_parts_for_gemini)} function responses back to Gemini for chat {original_chat_id}.")
        if gemini_api is None:
            logger.critical("gemini_api module unavailable.")
            break

        try:
            content_with_responses = Content(role="function", parts=response_parts_for_gemini)
            loop = asyncio.get_running_loop()
            current_response = await loop.run_in_executor( # Этот вызов важен для следующей итерации
                 None, gemini_api.send_message_to_gemini,
                 model_instance, chat_session, content_with_responses
            )
            logger.debug(f"Received next response from Gemini after sending FRs for chat {original_chat_id}")
            final_history = getattr(chat_session, 'history', None) # Обновляем историю
            # response_parts_for_gemini очистится в начале следующей итерации while (перенесли инициализацию)

        except Exception as api_err:
             logger.error(f"Error sending function responses to Gemini API: {api_err}", exc_info=True)
             current_response = None # Прерываем цикл while при ошибке API
             break

    # Цикл завершен (по шагам, отсутствию FC, ошибке или прерыванию)
    # --->>> КОНЕЦ ИЗМЕНЕНИЙ <<<---
    logger.info(f"FC processing cycle finished after {step} step(s) for chat {original_chat_id}.")
    return final_history, last_successful_fc_name, last_sent_text, last_successful_fc_result

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
        fc_exec_tasks: List[Tuple[asyncio.Task, Optional[Dict]]] = [] # Задачи и их аргументы для asyncio.gather и логирования
        fc_names_in_batch = [] # Имена функций в текущем батче для сопоставления с результатами

        for fc in function_calls_to_process:
            function_name = fc.name
            fc_names_in_batch.append(function_name) # Сохраняем имя
            args: Dict[str, Any] = {}
            original_args_for_log: Optional[Dict] = None # Аргументы для лога

            if hasattr(fc, 'args') and fc.args is not None:
                try:
                     # Используем _convert_value_for_json для правильной конвертации MapComposite
                     from utils.converters import _convert_value_for_json
                     args = _convert_value_for_json(fc.args)
                     if not isinstance(args, dict):
                         raise TypeError(f"Converted args is not a dict: {type(args)}")
                     original_args_for_log = args # Сохраняем успешно распарсенные аргументы
                except (TypeError, ValueError) as e:
                    logger.error(f"Cannot convert/parse args to dict for FC '{function_name}': {e}. Raw Args: {getattr(fc, 'args', 'MISSING')}")
                    error_response = Part(function_response=FunctionResponse(name=function_name, response={"error": f"Failed to parse arguments: {e}"}))
                    response_parts_for_gemini.append(error_response)
                    # <<< ИЗМЕНЕНО: Добавляем "пустую" задачу и информацию об ошибке парсинга аргументов >>>
                    error_task = asyncio.create_task(asyncio.sleep(0, result={"error": "Argument parsing failed"}))
                    error_args_log = {"raw_args": str(getattr(fc, 'args', 'MISSING')), "parsing_error": str(e)}
                    fc_exec_tasks.append((error_task, error_args_log)) # Добавляем ошибку
                    continue # Переходим к следующему FC
            else:
                # Если аргументов нет, логируем пустой словарь
                original_args_for_log = {}

            logger.info(f"Preparing FC execution: {function_name}({args}) for chat {original_chat_id}")

            # Ищем хендлер
            if function_name in available_functions_map:
                handler = available_functions_map[function_name]
                # Создаем задачу выполнения хендлера
                # <<< ИЗМЕНЕНО: Создаем Task явно и сохраняем его вместе с аргументами >>>
                exec_task = asyncio.create_task(
                    execute_function_call(
                        handler_func=handler,
                        args=args,
                        chat_id_for_handlers=original_chat_id,
                        user_id_for_handlers=original_user_id
                    ),
                    name=f"FC_{function_name}_{original_chat_id}"
                )
                fc_exec_tasks.append((exec_task, original_args_for_log))
            else:
                # Если хендлер не найден
                logger.error(f"Function handler '{function_name}' not found in available_functions_map.")
                error_response = Part(function_response=FunctionResponse(name=function_name, response={"error": f"Function '{function_name}' is not implemented or available."}))
                response_parts_for_gemini.append(error_response)
                # <<< ИЗМЕНЕНО: Добавляем "пустую" задачу и None для аргументов >>>
                error_task = asyncio.create_task(asyncio.sleep(0, result={"error": "Function not found"}), name=f"FC_NotFound_{function_name}")
                fc_exec_tasks.append((error_task, original_args_for_log or {}))

        # Если нет задач для выполнения (например, все были с ошибками парсинга/поиска)
        if not fc_exec_tasks:
            logger.warning("No valid FC tasks generated in this step. Ending FC cycle prematurely.")
            break # Прерываем цикл, если нет задач для gather

        # Выполняем все подготовленные задачи параллельно
        logger.info(f"Executing {len(fc_exec_tasks)} function call handlers concurrently for chat {original_chat_id}...")
        # <<< ИЗМЕНЕНО: Распаковываем только задачи для gather >>>
        results = await asyncio.gather(*[task for task, _ in fc_exec_tasks], return_exceptions=True)
        logger.info(f"Function call handlers finished for chat {original_chat_id}. Got {len(results)} results.")

        # Обрабатываем результаты и готовим ответ для Gemini
        # <<< ИЗМЕНЕНО: Цикл по результатам для логирования и создания ответа >>>
        for i, result in enumerate(results):
            fc_name = fc_names_in_batch[i]
            original_args = fc_exec_tasks[i][1] # Получаем сохраненные аргументы
            response_content = None

            # --- Логирование выполнения инструмента --- >>>
            log_status = 'error'
            log_return_code = None
            log_result_message = None
            log_stdout = None
            log_stderr = None
            log_trigger_message_id = None # Пока не передается

            if isinstance(result, Exception):
                # Ошибка возникла при выполнении execute_function_call или asyncio.gather
                log_result_message = f"Execution failed: {result}"
                logger.error(f"Error during execution of {fc_name}: {result}", exc_info=result)
                response_content = {"error": log_result_message} # Ответ для Gemini
            elif isinstance(result, dict):
                # Ожидаемый результат - словарь
                log_stdout = result.get('stdout')
                log_stderr = result.get('stderr')
                log_return_code = result.get('returncode')

                # Определяем статус
                if 'status' in result and result['status'] in {'success', 'error', 'not_found', 'warning', 'timeout'}:
                    log_status = result['status']
                    # Если статус 'error', ищем сообщение в 'message' или 'error'
                    if log_status == 'error':
                         log_result_message = result.get('message', result.get('error', 'Unknown error'))
                    else:
                         log_result_message = result.get('message') # Для success, warning и т.д.
                elif 'error' in result:
                    log_status = 'error'
                    log_result_message = result['error']
                else:
                    log_status = 'success' # По умолчанию успех, если нет status или error
                    log_result_message = result.get('message')

                response_content = result # Используем весь словарь как ответ для Gemini

                # --- Особая обработка send_telegram_message ---
                if fc_name == 'send_telegram_message' and log_status == 'success':
                    last_sent_text = original_args.get('text') # Сохраняем текст отправленного сообщения
                    logger.info(f"Recorded sent text via send_telegram_message: '{last_sent_text[:50]}...'")
                    # Не нужно включать результат send_telegram_message в ответ для Gemini, это может запутать модель
                    response_content = {"status": "success", "message": "Message queued for sending."}
                # --- /Особая обработка send_telegram_message ---

                # <<< ИЗМЕНЕНО: last_successful_fc_name больше не сохраняем >>>
                if log_status == 'success':
                    last_successful_fc_name = fc_name
                    last_successful_fc_result = result # Сохраняем результат

            else:
                # Неожиданный тип результата
                log_status = 'success' # Предполагаем успех, но логируем как warning
                log_result_message = f"Handler returned non-dict/non-exception result: {type(result)} - {str(result)[:100]}..."
                logger.warning(f"Handler '{fc_name}' returned unexpected result type: {type(result)}. String repr: {str(result)[:200]}...")
                # Пытаемся сконвертировать в JSON для Gemini, если не получается - возвращаем как строку
                try:
                    response_content = json.loads(json.dumps(result, ensure_ascii=False))
                except (TypeError, json.JSONDecodeError):
                     response_content = {"result_string": str(result)}

            # Вызываем логирование, если database импортирован успешно
            if database:
                try:
                    await database.add_tool_execution_log(
                        chat_id=original_chat_id,
                        user_id=original_user_id,
                        tool_name=fc_name,
                        tool_args=original_args,
                        status=log_status,
                        return_code=log_return_code,
                        result_message=log_result_message,
                        stdout=log_stdout,
                        stderr=log_stderr,
                        trigger_message_id=None # log_trigger_message_id
                    )
                except Exception as log_err:
                    logger.error(f"Failed to add tool execution log for '{fc_name}': {log_err}", exc_info=True)
            # --- /Логирование выполнения инструмента --- <<<

            # Добавляем FunctionResponse в список для отправки Gemini
            response_payload_for_gemini: Any = None # Переменная для данных, которые пойдут в response

            # Пытаемся сериализовать результат инструмента в JSON СТРОКУ
            try:
                # response_content - это словарь, возвращенный вашим инструментом
                response_payload_for_gemini = json.dumps(response_content, ensure_ascii=False)
                logger.debug(f"Serialized FunctionResponse payload to JSON string for '{fc_name}': '{response_payload_for_gemini[:200]}...'")
            except (TypeError, ValueError) as json_err:
                # Если сериализация не удалась (маловероятно для словаря от reading_user_info)
                logger.error(f"Failed to serialize tool result for FunctionResponse: {json_err}. Tool: {fc_name}. Result: {response_content}", exc_info=True)
                # Формируем JSON строку с ошибкой как fallback
                error_payload = {"error": f"Failed to serialize tool result: {json_err}"}
                response_payload_for_gemini = json.dumps(error_payload)
                logger.debug(f"Using error payload JSON string for '{fc_name}': {response_payload_for_gemini}")

            # Добавляем FunctionResponse в список для отправки Gemini
            # ВАЖНО: поле 'response' объекта FunctionResponse ожидает словарь (Struct).
            # Мы НЕ можем передать туда просто строку.
            # Оборачиваем нашу JSON-строку в словарь с ключом, например, 'result_json'.
            logger.debug(f"Preparing FunctionResponse Part for '{fc_name}' with serialized payload.")
            response_part_for_gemini = Part(function_response=FunctionResponse(
                name=fc_name,
                response={"result_json": response_payload_for_gemini} # <--- Отправляем JSON-строку внутри словаря
            ))
            response_parts_for_gemini.append(response_part_for_gemini)
        # --- /Цикл по результатам ---

        # Если не было частей для ответа (маловероятно, т.к. были FCs)
        if not response_parts_for_gemini:
             logger.warning("No response parts generated for Gemini despite processing FC results. Ending cycle.")
             break # Выход из цикла while

        # Отправляем собранные ответы Gemini
        logger.info(f"Sending {len(response_parts_for_gemini)} function responses back to Gemini for chat {original_chat_id}.")
        if gemini_api is None:
            logger.critical("gemini_api module is not available! Cannot send function responses.")
            break # Выход из цикла while
        try:
            # Создаем Content объект с ролью 'function'
            content_with_responses = Content(role="function", parts=response_parts_for_gemini)

            # <<< ИСПРАВЛЕНИЕ TypeError: ВОЗВРАЩАЕМ run_in_executor >>>
            loop = asyncio.get_running_loop()
            current_response = await loop.run_in_executor(
                 None, # Используем executor по умолчанию
                 gemini_api.send_message_to_gemini, # Имя синхронной функции
                 model_instance, # 1-й аргумент (model)
                 chat_session,   # 2-й аргумент (chat_session)
                 content_with_responses # 3-й аргумент (user_message / FR)
            )
            # <<< КОНЕЦ ИСПРАВЛЕНИЯ >>>

            logger.debug(f"Received response from Gemini after sending FRs for chat {original_chat_id}")
            response_parts_for_gemini = [] # Очищаем для след. шага
            # <<< ОБНОВЛЕНИЕ: Сохраняем историю после успешной отправки FR >>>
            final_history = getattr(chat_session, 'history', None)

        except Exception as api_err:
             logger.error(f"Error sending function responses to Gemini API: {api_err}", exc_info=True)
             current_response = None # Прерываем цикл при ошибке API
             break # <<< ИСПРАВЛЕНИЕ: Выход из цикла при ошибке API >>>

    # Цикл завершен (по шагам, отсутствию FC или ошибке)
    logger.info(f"FC processing cycle finished after {step} step(s) for chat {original_chat_id}.")
    # <<< ИСПРАВЛЕНИЕ ValueError: Всегда возвращаем 4 значения, используя инициализированные/обновленные переменные >>>
    return final_history, last_successful_fc_name, last_sent_text, last_successful_fc_result


# Утилита для извлечения текста (может быть перенесена)
# async def extract_final_text(history: List[Content]) -> Optional[str]:
#     ...

# Утилита для сохранения истории (может быть перенесена)
# async def save_final_history(chat_id: int, history: List[Content]):
#     ...
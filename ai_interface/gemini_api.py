import google.generativeai as genai
import logging
from typing import Optional, Dict, List, Any, Union, Sequence

logger = logging.getLogger(__name__)

# --- ИМПОРТ ТИПОВ ИЗ google.ai.generativelanguage ---
try:
    from google.ai import generativelanguage as glm
    # <<< ВОЗВРАЩАЕМ ИМПОРТЫ glm >>>
    Part = glm.Part
    FunctionResponse = glm.FunctionResponse
    FunctionDeclaration = glm.FunctionDeclaration
    Tool = glm.Tool
    Schema = glm.Schema
    Type = glm.Type
    try:
        FinishReason = glm.Candidate.FinishReason
        logger_types = logging.getLogger(__name__)
        logger_types.debug("Imported FinishReason from glm.Candidate")
    except AttributeError:
        logger_types = logging.getLogger(__name__)
        logger_types.warning("Could not import FinishReason from glm.Candidate. String comparison fallback needed.")
        FinishReason = None # Тип будет None

    # <<< ИМПОРТИРУЕМ GenerationConfig ОТДЕЛЬНО ИЗ types >>>
    from google.generativeai.types import ContentDict, GenerateContentResponse, GenerationConfig
    # <<< УБИРАЕМ Content отсюда, он будет через glm >>>
    # from google.generativeai import Content # НЕПРАВИЛЬНО

    logger_types = logging.getLogger(__name__)
    logger_types.debug("Successfully imported types from google.ai.generativelanguage and google.generativeai.types")

except ImportError as e:
    logger_types = logging.getLogger(__name__)
    logger_types.critical("CRITICAL: Failed to import required Google AI types. Functionality will be impaired.", exc_info=True)
    # Определяем заглушки Any
    Part, FunctionResponse, FunctionDeclaration, Tool, Schema, Type, FinishReason = Any, Any, Any, Any, Any, Any, Any
    ContentDict, GenerateContentResponse, GenerationConfig = Any, Any, Any
    # Content будет Any из-за структуры ниже
    Content = Any # Добавляем заглушку для Content

# <<< УБЕДИМСЯ, ЧТО Content тоже доступен (через glm) >>>
try:
    Content = glm.Content # Определяем Content через glm
    logger_types.debug("Defined Content via glm.Content")
except NameError: # Если glm не импортировался
    pass # Content останется Any
except AttributeError: # Если в glm нет Content
     logger_types.warning("Could not define Content via glm.Content")
     Content = Any
# Предполагаем, что settings импортируются там, где вызываются эти функции,
# или передаются как аргументы. Не импортируем settings напрямую здесь.
# from config import settings (Не рекомендуется здесь)

# --- Настройка модели ---
def setup_gemini_model(
    api_key: str,
    model_name: str,
    system_prompt: Optional[str] = None,
    function_declarations_data: Optional[List[Dict[str, Any]]] = None,
    generation_config: Optional[Dict[str, Any]] = None,
    safety_settings: Optional[List[Dict[str, Any]]] = None,
    enable_function_calling: bool = True
) -> Optional[genai.GenerativeModel]:
    """
    Настраивает и возвращает модель Gemini с инструментами, системным промптом,
    конфигурацией генерации и настройками безопасности.

    Args:
        api_key: API ключ Google AI.
        model_name: Имя модели Gemini (например, 'gemini-1.5-pro-latest').
        system_prompt: Текст системного промпта (может быть None).
        function_declarations_data: Список словарей с декларациями функций (может быть None).
        generation_config: Словарь с параметрами генерации (temperature, top_p, etc.).
        safety_settings: Список словарей с настройками безопасности.
        enable_function_calling: Включить ли Function Calling.

    Returns:
        Инициализированный объект genai.GenerativeModel или None при ошибке.
    """
    # Проверка импорта базовых типов
    if not all([genai, Tool, FunctionDeclaration, Schema, Type, GenerationConfig]):
        logger.critical(f"Cannot setup model '{model_name}': Missing essential Google AI types.")
        return None

    try:
        genai.configure(api_key=api_key)
        tools_list = None

        # Создание инструментов (Function Calling)
        if function_declarations_data and enable_function_calling:
            logger.info(f"Creating Tool configuration for model '{model_name}'...")
            declarations = []
            for func_decl_dict in function_declarations_data:
                if not isinstance(func_decl_dict, dict) or 'name' not in func_decl_dict or 'description' not in func_decl_dict:
                    logger.warning(f"Skipping incomplete function declaration: {func_decl_dict}")
                    continue
                try:
                    param_schema = None
                    parameters_dict = func_decl_dict.get('parameters', {})
                    properties_dict = parameters_dict.get('properties', {}) if isinstance(parameters_dict, dict) else {}
                    required_params_list = parameters_dict.get('required', []) if isinstance(parameters_dict, dict) else []

                    if isinstance(properties_dict, dict) and properties_dict:
                        param_properties = {}
                        for param_name, param_details in properties_dict.items():
                            if not isinstance(param_details, dict):
                                logger.warning(f"Parameter details for '{param_name}' in '{func_decl_dict['name']}' is not a dict. Skipping param.")
                                continue
                            param_type_str = param_details.get('type', 'STRING').upper()
                            schema_type_enum = getattr(Type, param_type_str, Type.STRING)
                            if schema_type_enum == Type.STRING and param_type_str != 'STRING':
                                logger.warning(f"Unknown type '{param_type_str}' for param '{param_name}'. Defaulting to STRING.")

                            param_properties[param_name] = Schema(type=schema_type_enum, description=param_details.get('description', ''))

                        # Валидация required параметров
                        valid_required = [p for p in required_params_list if isinstance(p, str) and p in param_properties]
                        if len(valid_required) != len(required_params_list):
                             invalid_req = set(required_params_list) - set(valid_required)
                             logger.warning(f"Required params {invalid_req} not found in properties for '{func_decl_dict['name']}'. Ignoring them in 'required'.")

                        param_schema = Schema(type=Type.OBJECT, properties=param_properties, required=valid_required)

                    declarations.append(FunctionDeclaration(name=func_decl_dict['name'], description=func_decl_dict['description'], parameters=param_schema))
                except Exception as e:
                    logger.error(f"Error creating FunctionDeclaration for '{func_decl_dict.get('name', 'UNKNOWN')}': {e}", exc_info=True)

            if declarations:
                tool_object = Tool(function_declarations=declarations)
                tools_list = [tool_object]
                logger.info(f"Tool object created for '{model_name}' with {len(declarations)} declarations.")
            else:
                logger.warning(f"No valid function declarations created for '{model_name}'. Function Calling might be unavailable.")
        elif not enable_function_calling:
            logger.info(f"Function Calling disabled for model '{model_name}'. No tools created.")
        else:
            logger.info(f"No function declaration data provided for '{model_name}'. No tools created.")

        # Формируем аргументы для инициализации модели
        init_args = {"model_name": model_name}
        if generation_config and isinstance(generation_config, dict):
            try:
                init_args["generation_config"] = GenerationConfig(**generation_config)
                logger.debug(f"Applying generation config for '{model_name}': {generation_config}")
            except Exception as conf_err:
                 logger.error(f"Failed to apply generation_config for '{model_name}': {conf_err}. Config: {generation_config}")
        if safety_settings and isinstance(safety_settings, list):
            init_args["safety_settings"] = safety_settings
            logger.debug(f"Applying safety settings for '{model_name}': {safety_settings}")
        if tools_list:
            init_args["tools"] = tools_list
        if system_prompt and isinstance(system_prompt, str) and system_prompt.strip():
            init_args["system_instruction"] = system_prompt
            logger.info(f"Applying system instruction for '{model_name}'.")

        # Инициализация модели
        model = genai.GenerativeModel(**init_args)
        logger.info(f"Gemini model '{model_name}' initialized successfully.")
        return model

    except Exception as e:
        logger.critical(f"Failed to initialize Gemini model '{model_name}': {e}", exc_info=True)
        return None


# --- Отправка сообщения (синхронная, для использования в executor) ---
def send_message_to_gemini(
    model: genai.GenerativeModel,
    chat_session: genai.ChatSession,
    user_message: Union[str, Part, List[Part], ContentDict, List[ContentDict]]
) -> Optional[GenerateContentResponse]:
    """
    Отправляет сообщение в чат-сессию Gemini.
    Эта функция синхронная и предназначена для вызова через loop.run_in_executor.

    Args:
        model: Экземпляр genai.GenerativeModel (формально не используется send_message сессии, но оставлен для контекста).
        chat_session: Активная сессия чата genai.ChatSession.
        user_message: Сообщение для отправки (строка, Part, список Part или ContentDict).

    Returns:
        Ответ от модели (GenerateContentResponse) или None при ошибке.
    """
    if not chat_session:
        logger.error("Cannot send message: chat_session is None.")
        return None
    if not user_message:
         logger.warning("Attempted to send an empty message to Gemini.")
         # Можно вернуть ошибку или пустой ответ в зависимости от желаемого поведения
         return None # Или создать пустой фейковый ответ

    try:
        # Тип user_message уже должен быть подготовлен вызывающей функцией
        response = chat_session.send_message(user_message) # Синхронный вызов

        if response is None:
            logger.error("Gemini API returned None response.")
            return None

        # Детальное логирование ответа
        try:
            parts_repr = []
            finish_reason_val = 'N/A'
            safety_ratings_repr = 'N/A'
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                finish_reason_val = getattr(candidate, 'finish_reason', 'N/A')
                safety_ratings_repr = str(getattr(candidate, 'safety_ratings', []))
                if hasattr(candidate, 'content') and candidate.content and hasattr(candidate.content, 'parts'):
                    for i, part in enumerate(candidate.content.parts):
                        part_info = f"Part {i}: Type={type(part).__name__}"
                        if hasattr(part, 'text') and part.text is not None: part_info += f", Text='{part.text[:80]}...'"
                        if hasattr(part, 'function_call') and part.function_call is not None: part_info += f", FunctionCall(Name='{getattr(part.function_call, 'name', 'N/A')}')"
                        if hasattr(part, 'function_response') and part.function_response is not None: part_info += f", FunctionResponse(Name='{getattr(part.function_response, 'name', 'N/A')}')"
                        parts_repr.append(part_info)
            elif hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                 feedback = response.prompt_feedback
                 finish_reason_val = getattr(feedback, 'block_reason', 'UNKNOWN_BLOCK')
                 safety_ratings_repr = str(getattr(feedback, 'safety_ratings', []))

            logger.info(f"Raw Gemini Response: Parts=[{'; '.join(parts_repr)}], FinishReason: {finish_reason_val}, Safety: {safety_ratings_repr}")
        except Exception as log_ex:
            logger.error(f"Error during detailed response logging: {log_ex}", exc_info=True)

        return response

    except Exception as e:
        logger.error(f"Error sending message to Gemini: {e}", exc_info=True)
        # Возвращаем None или перевыбрасываем исключение, чтобы вызывающий код мог его обработать (например, для retry)
        raise # Перевыбрасываем, чтобы run_gemini_interaction мог поймать ResourceExhausted


# --- Генерация описания изображения (асинхронная) ---
async def generate_image_description(
    api_key: str, # Добавляем API ключ как аргумент
    image_bytes: bytes,
    prompt: str,
    model_name: str = "gemini-1.5-pro-latest" # Используем модель с vision capabilities
) -> Optional[str]:
    """
    Генерирует текстовое описание для изображения, используя Gemini Vision.

    Args:
        api_key: API ключ Google AI.
        image_bytes: Изображение в виде байтов.
        prompt: Промпт для модели (например, "Опиши это изображение").
        model_name: Имя модели Gemini с поддержкой Vision (по умолчанию 'gemini-1.5-pro-latest').

    Returns:
        Сгенерированное описание или None в случае ошибки.
    """
    logger.info(f"Generating image description using model '{model_name}'...")
    if not image_bytes:
        logger.error("Cannot generate description: image_bytes is empty.")
        return None
    if not api_key:
         logger.error("Cannot generate description: Google API Key is missing.")
         return None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(model_name)

        # Определяем mime_type (упрощенно, можно добавить более надежное определение)
        # Библиотека google-generativeai может определить тип сама, если передать bytes
        image_part = {"mime_type": "image/jpeg", "data": image_bytes} # TODO: Определять mime_type надежнее

        # Вызов generate_content_async
        response = await model.generate_content_async([prompt, image_part])

        if response and response.text:
            description = response.text.strip()
            logger.info(f"Image description generated successfully (length: {len(description)}).")
            return description
        elif response and response.prompt_feedback and response.prompt_feedback.block_reason:
            reason = response.prompt_feedback.block_reason
            logger.error(f"Image description request blocked. Reason: {reason}")
            return f"[Описание заблокировано: {reason}]"
        else:
            logger.error("Image description generation returned empty or invalid response.")
            return None

    except Exception as e:
        logger.error(f"Error generating image description: {e}", exc_info=True)
        return None
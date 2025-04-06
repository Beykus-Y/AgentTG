# config.py
import logging
import os
from pathlib import Path
# Добавляем Union для type hinting в валидаторе
from typing import Dict, List, Any, Set, Optional, Union

# Импортируем нужные декораторы и типы из Pydantic
from pydantic import field_validator, ValidationError, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

# Загружаем переменные из .env файла в окружение ОС (необязательно для Pydantic >v2, но может быть полезно)
load_dotenv()

# Определяем базовую директорию проекта (предполагаем, что config.py находится в ./Agent/)
BASE_DIR = Path(__file__).parent.resolve()
logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """
    Основные настройки приложения, загружаемые из переменных окружения и файла .env.
    """
    # Конфигурация Pydantic для загрузки из .env
    model_config = SettingsConfigDict(
        env_file=BASE_DIR.parent / '.env', # Ищем .env в родительской директории
        env_file_encoding='utf-8',
        extra='ignore'  # Игнорировать лишние переменные в .env
    )

    # --- Основные ключи и ID ---
    bot_token: str
    google_api_keys: List[str] = Field(default_factory=list)
    # Оставляем тип Set[int] для конечного результата
    admin_ids: Set[int] = set()

    # --- Добавляем Валидатор ---
    @field_validator('google_api_keys', mode='before')
    @classmethod
    def parse_google_api_keys(cls, value: Any) -> List[str]:
        """
        Парсит google_api_keys из строки (через запятую) или списка.
        Фильтрует пустые ключи.
        """
        if isinstance(value, list):
            # Фильтруем пустые строки из списка
            keys = [key.strip() for key in value if isinstance(key, str) and key.strip()]
            if not keys:
                 raise ValueError("GOOGLE_API_KEYS list is empty or contains only invalid entries.")
            return keys
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("GOOGLE_API_KEYS string is empty.")
            # Разделяем по запятой и фильтруем пустые
            keys = [key.strip() for key in value.split(',') if key.strip()]
            if not keys:
                 raise ValueError("GOOGLE_API_KEYS string contains no valid keys after splitting.")
            return keys

        raise ValueError(f"Invalid type for GOOGLE_API_KEYS: {type(value)}. Expected str or list.")


    @field_validator('admin_ids', mode='before')
    @classmethod
    def parse_admin_ids(cls, value: Any) -> Set[int]:
        """
        Парсит admin_ids из строки (через запятую),
        одиночного числа или уже существующего set/list.
        """
        if isinstance(value, set):
            # Если уже set (например, значение по умолчанию)
            return value
        if isinstance(value, str):
            # Если строка, пытаемся разделить по запятой и конвертировать в int
            if not value.strip(): # Обработка пустой строки
                return set()
            try:
                # Убираем пробелы вокруг запятых, фильтруем пустые элементы после split
                return {int(admin_id.strip()) for admin_id in value.split(',') if admin_id.strip()}
            except ValueError as e:
                raise ValueError(f"Invalid integer found in ADMIN_IDS string: {e}") from e
        if isinstance(value, int):
            # Если это одно число, создаем set с ним
            return {value}
        if isinstance(value, list):
            # Если это список (менее вероятно из .env, но возможно)
             try:
                 return {int(item) for item in value}
             except ValueError as e:
                 raise ValueError(f"Invalid integer found in ADMIN_IDS list: {e}") from e

        # Если тип не подходит, выбрасываем ошибку
        raise ValueError(f"Invalid type for ADMIN_IDS: {type(value)}. Expected str, int, list or set.")

    # --- Пути ---
    db_path: str = str(BASE_DIR / "database/bot_db.sqlite") # Путь к БД по умолчанию
    env_dir_path: str = str(BASE_DIR / "env") # Путь к папке окружений по умолчанию
    prompts_dir: Path = BASE_DIR / "prompts"
    declarations_dir: Path = BASE_DIR / "declarations" # Директория для JSON-деклараций (если нужны)

    # --- Настройки AI моделей ---
    lite_gemini_model_name: str = "gemini-1.5-flash-latest"
    #pro_gemini_model_name: str = "gemini-2.0-flash-thinking-exp-01-21"
    pro_gemini_model_name: str = "gemini-2.0-flash"
    #pro_gemini_model_name: str = "gemini-1.5-flash-latest"
    #pro_gemini_model_name: str = "gemini-2.0-pro-exp"
    
    # Пути к файлам промптов
    lite_prompt_file: Path = prompts_dir / "lite_analyzer.txt" # Промпт для Lite-анализатора
    pro_prompt_file: Path = prompts_dir / "pro_assistant.txt"   # Основной промпт для Pro-модели
    deep_search_prompts_dir: Path = prompts_dir / "deep_search"

    # Пути к файлам деклараций функций (если они не встроены в код)
    # Если декларации будут генерироваться динамически или не нужны, эти пути можно убрать
    lite_func_decl_file: Optional[Path] = declarations_dir / "lite_functions.json" # Может быть None, если Lite без FC
    pro_func_decl_file: Optional[Path] = declarations_dir / "pro_functions.json"

    # Параметры генерации (можно переопределить в .env через JSON-строку)
    lite_generation_config: Dict[str, Any] = {"temperature": 0.2} # Более детерминированный для анализа
    pro_generation_config: Dict[str, Any] = {"temperature": 0.7}

    # Настройки безопасности (можно переопределить в .env через JSON-строку)
    lite_safety_settings: List[Dict[str, Any]] = [
        # Пример: BLOCK_NONE для всех категорий для Lite
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]
    pro_safety_settings: List[Dict[str, Any]] = [
        # Пример: BLOCK_MEDIUM_AND_ABOVE для Pro (стандартные настройки)
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
        {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    ]

    # Параметры Function Calling
    fc_enabled: bool = True # Глобальное включение/выключение FC
    max_lite_fc_steps: int = 1 # Максимум 1 шаг для Lite (если вообще используется)
    max_pro_fc_steps: int = 10 # Больше шагов для Pro

    # --- Настройки Бота и Интерфейса ---
    ai_timeout: int = 40 # Таймаут ожидания ответа от AI (для синхронных вызовов, если есть)
    max_message_length: int = 4000 # Макс. длина сообщения для отправки (близко к лимиту TG)
    max_history_length: int = 10 # Макс. кол-во пар сообщений (user+model) в истории для контекста

    # --- Лимиты Инструментов ---
    max_read_size_bytes: int = 150 * 1024  # 150 KB
    max_write_size_bytes: int = 500 * 1024 # 500 KB
    script_timeout_seconds: int = 45
    command_timeout_seconds: int = 75
    max_script_output_len: int = 6000
    max_command_output_len: int = 6000

    # --- Сервис Новостей ---
    rss_mapping: Dict[str, List[str]] = {
        # Добавьте сюда ваши реальные RSS URL, возможно, читая их из .env через os.getenv
        # Пример:
        "технологии": [
            os.getenv("RSS_TECH_1", "DEFAULT_TECH_URL_1"),
            os.getenv("RSS_TECH_2", "DEFAULT_TECH_URL_2")
        ],
        "наука": [
            os.getenv("RSS_SCIENCE_1", "DEFAULT_SCIENCE_URL_1")
        ],
        # ... другие категории ...
    }

    # --- Общие ---
    log_level: str = "INFO"


# Создаем экземпляр настроек
try:
    settings = Settings()
except ValidationError as e:
     # Выводим ошибки валидации (особенно важно для ключей)
     init_logger = logging.getLogger(__name__)
     init_logger.critical(f"FATAL: Configuration validation failed!")
     init_logger.critical(e)
     exit(1) # Выход, если конфиг некорректен # Теперь эта строка должна отработать корректно

# Настройка логирования
# Базовый формат, можно усложнить (добавить имя файла, номер строки и т.д.)
log_format = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
# Устанавливаем уровень логирования из настроек
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format=log_format,
    # handlers=[logging.StreamHandler(), logging.FileHandler("bot.log")] # Пример вывода в файл
)
logger = logging.getLogger(__name__)

# Выводим часть настроек в лог при старте (без секретов)
logger.info("Настройки приложения загружены.")
logger.info(f"Уровень логирования: {settings.log_level}")
logger.info(f"Путь к БД: {settings.db_path}")
logger.info(f"Путь к окружениям: {settings.env_dir_path}")
# Логируем результат работы валидатора
logger.info(f"ID Администраторов: {settings.admin_ids if settings.admin_ids else 'Не заданы'}")
logger.info(f"Function Calling включен: {settings.fc_enabled}")
# logger.debug(f"Полные настройки: {settings.model_dump()}") # Для отладки (может содержать секреты!)
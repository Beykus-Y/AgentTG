# config.py
import logging
import os
from pathlib import Path
from typing import Dict, List, Any, Set, Optional, Union

from pydantic import field_validator, ValidationError, Field, computed_field # <<< Добавляем computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).parent.resolve()
logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR.parent / '.env',
        env_file_encoding='utf-8',
        extra='ignore'
    )

    # --- Основные ключи и ID (Общие) ---
    bot_token: str
    # <<< ИЗМЕНЕНО: Тип для чтения из .env - строка >>>
    admin_ids_str: str = Field(default="", alias="ADMIN_IDS") # Читаем как строку, используем alias

    # --- Google Generative AI Settings ---
    google_api_keys: Optional[List[str]] = Field(default=None)

    # --- OpenAI Settings ---
    openai_api_key: Optional[str] = Field(default=None)
    openai_organization_id: Optional[str] = Field(default=None)

    # --- Валидаторы ---
    @field_validator('google_api_keys', mode='before')
    @classmethod
    def parse_google_api_keys(cls, value: Any) -> Optional[List[str]]:
        if value is None: return None
        if isinstance(value, list):
            keys = [key.strip() for key in value if isinstance(key, str) and key.strip()]
            return keys if keys else None
        if isinstance(value, str):
            if not value.strip(): return None
            keys = [key.strip() for key in value.split(',') if key.strip()]
            return keys if keys else None
        raise ValueError(f"Invalid type for GOOGLE_API_KEYS: {type(value)}. Expected str or list.")

    # <<< ИЗМЕНЕНО: Теперь это вычисляемое поле, парсит admin_ids_str >>>
    @computed_field # Используем @computed_field для создания поля Set[int]
    @property # Делаем его свойством
    def admin_ids(self) -> Set[int]:
        """Парсит admin_ids_str (из ADMIN_IDS env) в Set[int]."""
        value_str = self.admin_ids_str # Берем строку, прочитанную из .env
        if not value_str or not isinstance(value_str, str):
            return set() # Возвращаем пустое множество, если строка пустая или не строка
        try:
            # Логика парсинга строки с запятыми
            return {int(admin_id.strip()) for admin_id in value_str.split(',') if admin_id.strip()}
        except ValueError as e:
            # Логируем ошибку, если не удалось преобразовать в int
            logger.error(f"Invalid integer found in ADMIN_IDS string ('{value_str}'): {e}", exc_info=True)
            # Можно либо выбросить исключение, либо вернуть пустое множество
            # raise ValueError(f"Invalid integer found in ADMIN_IDS string: {e}") from e
            return set() # Возвращаем пустое, чтобы приложение могло запуститься

    # --- Пути (Общие) ---
    db_path: str = str(BASE_DIR / "database/bot_db.sqlite")
    env_dir_path: str = str(BASE_DIR / "env")
    prompts_dir: Path = BASE_DIR / "prompts"
    declarations_dir: Path = BASE_DIR / "declarations"

    # --- Настройки AI моделей (для обеих платформ) ---
    # Google
    lite_gemini_model_name: str = "gemini-1.5-flash-latest"
    pro_gemini_model_name: str = "gemini-1.5-pro-latest"
    # OpenAI
    lite_openai_model_name: str = "gpt-3.5-turbo"
    pro_openai_model_name: str = "gpt-4o"

    # --- Промпты и Декларации ---
    lite_prompt_file: Path = prompts_dir / "lite_analyzer.txt"
    pro_prompt_file: Path = prompts_dir / "pro_assistant.txt"
    deep_search_prompts_dir: Path = prompts_dir / "deep_search"
    pro_func_decl_file: Optional[Path] = declarations_dir / "pro_functions.json"

    # --- Параметры генерации ---
    # Google
    lite_generation_config: Dict[str, Any] = {"temperature": 0.2}
    pro_generation_config: Dict[str, Any] = {"temperature": 0.7}
    # OpenAI
    openai_temperature: float = 0.7
    openai_max_tokens: Optional[int] = None

    # --- Настройки безопасности (только для Google) ---
    lite_safety_settings: List[Dict[str, Any]] = [
        {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
        # ... (остальные настройки безопасности)
    ]
    pro_safety_settings: List[Dict[str, Any]] = [
         {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
         # ... (остальные настройки безопасности)
    ]

    # --- Параметры Function Calling / Tools (Общие концепции) ---
    fc_enabled: bool = True
    max_pro_fc_steps: int = 10

    # --- Настройки Бота и Интерфейса (Общие) ---
    ai_timeout: int = 40
    max_message_length: int = 4000
    max_history_length: int = 10

    # --- Лимиты Инструментов (Общие) ---
    max_read_size_bytes: int = 150 * 1024
    max_write_size_bytes: int = 500 * 1024
    script_timeout_seconds: int = 45
    command_timeout_seconds: int = 75
    max_script_output_len: int = 6000
    max_command_output_len: int = 6000

    # --- Сервис Новостей (Общие) ---
    rss_mapping: Dict[str, List[str]] = {
        "технологии": [
            os.getenv("RSS_TECH_1", "DEFAULT_TECH_URL_1"),
            os.getenv("RSS_TECH_2", "DEFAULT_TECH_URL_2")
        ],
        "наука": [
            os.getenv("RSS_SCIENCE_1", "DEFAULT_SCIENCE_URL_1")
        ],
    }

    # --- Общие ---
    log_level: str = "INFO"

# Создаем экземпляр настроек
try:
    settings = Settings()
except ValidationError as e:
     init_logger = logging.getLogger(__name__)
     init_logger.critical(f"FATAL: Configuration validation failed!")
     init_logger.critical(e)
     exit(1)

# Настройка логирования
log_format = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO), format=log_format)
logger = logging.getLogger(__name__)

# Выводим часть настроек в лог при старте (без секретов)
logger.info("Настройки приложения загружены.")
logger.info(f"Уровень логирования: {settings.log_level}")
logger.info(f"Путь к БД: {settings.db_path}")
logger.info(f"Путь к окружениям: {settings.env_dir_path}")
# <<< Используем вычисляемое поле admin_ids >>>
logger.info(f"ID Администраторов: {settings.admin_ids if settings.admin_ids else 'Не заданы'}")
logger.info(f"Function Calling/Tools включен: {settings.fc_enabled}")
logger.info(f"Google API Keys loaded: {'Yes' if settings.google_api_keys else 'No'}")
logger.info(f"OpenAI API Key loaded: {'Yes' if settings.openai_api_key else 'No'}")
# logger.debug(f"Полные настройки: {settings.model_dump()}")
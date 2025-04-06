# services/env_manager.py

import os
import logging
import asyncio # Импортируем для aiofiles.os
from typing import Optional, Tuple
from pathlib import Path # Добавим Path для настроек по умолчанию

# --- Зависимости ---
try:
    from config import settings # Импортируем объект настроек
except ImportError:
    # Заглушка на случай, если config.py еще не готов или недоступен
    class MockSettings:
        # Используем pathlib для более надежного пути по умолчанию
        env_dir_path: str = str(Path(__file__).resolve().parent.parent / "env")
        admin_ids: set[int] = set()
    settings = MockSettings()
    logging.warning("Could not import 'settings' from config.py in env_manager. Using mock settings.")

# Импортируем проверку админа (предполагается, что она есть в utils.helpers)
try:
    from utils.helpers import is_admin
except ImportError:
    # Заглушка для is_admin
    def is_admin(user_id: Optional[int]) -> bool:
        """Заглушка для проверки прав администратора."""
        if user_id is None: return False
        # logger.warning("Using mock admin check (always False) in env_manager.")
        return user_id in settings.admin_ids # Используем заглушку settings
    logging.warning("Could not import 'is_admin' from utils.helpers in env_manager. Using mock implementation.")

# Асинхронные файловые операции
try:
    import aiofiles
    import aiofiles.os
except ImportError:
    aiofiles = None # type: ignore
    logging.critical("CRITICAL: 'aiofiles' library not found. env_manager file operations might fail.")

# --- Константы и Логгер ---
logger = logging.getLogger(__name__)

# Глобальные переменные УДАЛЕНЫ
# _ABS_ENV_DIR: Optional[str] = None
# _ENV_DIR_INITIALIZED = False

# Функция _initialize_env_dir УДАЛЕНА

# services/env_manager.py
import os
import logging
import asyncio
from typing import Optional, Tuple
from pathlib import Path

# --- Зависимости ---
try:
    from config import settings # Импортируем объект настроек
    # Импортируем проверку админа
    from utils.helpers import is_admin
except ImportError:
    # Заглушки
    class MockSettings:
        env_dir_path: str = str(Path(__file__).resolve().parent.parent / "env")
        admin_ids: set[int] = set()
    settings = MockSettings()
    logging.warning("Could not import 'settings' from config.py in env_manager. Using mock settings.")
    def is_admin(user_id: Optional[int]) -> bool:
        if user_id is None: return False
        return user_id in settings.admin_ids
    logging.warning("Could not import 'is_admin' from utils.helpers in env_manager. Using mock implementation.")

# Асинхронные файловые операции
try:
    import aiofiles
    import aiofiles.os
except ImportError:
    aiofiles = None # type: ignore
    logging.critical("CRITICAL: 'aiofiles' library not found. env_manager file operations might fail.")

# --- Константы и Логгер ---
logger = logging.getLogger(__name__)

# --- Вспомогательная функция для создания директории чата (остается без изменений) ---
async def _ensure_specific_chat_dir_exists(chat_id: int) -> bool:
    # ... (код функции остается прежним) ...
    # Получаем базовый путь из настроек при каждом вызове
    if not settings.env_dir_path:
        logger.error("Cannot ensure chat directory: ENV directory path is not configured.")
        return False
    abs_env_dir = os.path.abspath(settings.env_dir_path)
    if not abs_env_dir:
         logger.error("Cannot ensure chat directory: Failed to resolve absolute path from settings.")
         return False

    if aiofiles is None:
         logger.error("Cannot ensure chat directory: aiofiles library missing.")
         return False
    if not isinstance(chat_id, int):
        logger.error(f"Cannot create directory for invalid chat_id type: {type(chat_id)}, value: {chat_id}")
        return False

    try:
        # 1. Убедимся, что базовая директория env существует
        await aiofiles.os.makedirs(abs_env_dir, exist_ok=True)
        logger.debug(f"Ensured base ENV directory exists: {abs_env_dir}")

        # 2. Создаем директорию чата
        chat_dir_relative = str(chat_id) # Имя папки = ID чата
        chat_dir_abs = os.path.abspath(os.path.join(abs_env_dir, chat_dir_relative))

        # Доп. проверка безопасности: убедимся, что создаем внутри abs_env_dir
        if not os.path.commonpath([abs_env_dir]) == os.path.commonpath([abs_env_dir, chat_dir_abs]):
             logger.critical(f"SECURITY ALERT: Attempt create dir outside ENV_DIR denied: "
                             f"chat_id={chat_id}, path='{chat_dir_abs}', base_env='{abs_env_dir}'")
             return False

        # Асинхронно создаем директорию чата
        await aiofiles.os.makedirs(chat_dir_abs, exist_ok=True)
        logger.debug(f"Ensured specific chat directory exists: {chat_dir_abs}")
        return True

    except OSError as e:
        chat_path_for_log = locals().get('chat_dir_abs', 'N/A')
        logger.error(f"Failed create/access directory for chat_id={chat_id} (base: {abs_env_dir}, specific: {chat_path_for_log}): {e}", exc_info=True)
        return False
    except ValueError: # Ошибка преобразования chat_id в строку
        logger.error(f"Invalid chat_id format for directory name: {chat_id}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error ensuring chat directory for chat_id={chat_id}: {e}", exc_info=True)
        return False
# --- КОНЕЦ ВСПОМОГАТЕЛЬНОЙ ФУНКЦИИ ---


async def get_safe_chat_path(
    chat_id: int,
    filename: str,
    user_id: Optional[int] = None,
    ensure_chat_dir_exists: bool = False
) -> Tuple[bool, Optional[str]]:
    """
    Асинхронно строит и проверяет путь к файлу/директории.
    - Корректно обрабатывает абсолютные пути для админов (ограничивая проектом).
    - Ограничивает не-админов их директорией чата внутри /env.
    - Опционально создает необходимые директории.
    """
    # --- Получение базовых путей и проверка зависимостей ---
    if not settings.env_dir_path:
        logger.error("Cannot check path safety: ENV directory path is not configured in settings.")
        return False, None
    if aiofiles is None:
         logger.error("Cannot check path safety: aiofiles library is missing.")
         return False, None

    abs_env_dir = os.path.abspath(settings.env_dir_path)
    # Определяем корневую директорию проекта (на уровень выше env)
    # Убедись, что структура проекта соответствует этому предположению
    abs_project_dir = os.path.abspath(os.path.join(abs_env_dir, ".."))
    logger.debug(f"Using ENV directory: {abs_env_dir}")
    logger.debug(f"Using Project directory: {abs_project_dir}")

    # --- Проверка входных данных ---
    if not isinstance(chat_id, int):
        logger.error(f"Invalid chat_id type for path check: {type(chat_id)}, value: {chat_id}")
        return False, None
    if not filename or not isinstance(filename, str):
         logger.error(f"Invalid filename provided for path check: {filename}")
         return False, None

    caller_is_admin = is_admin(user_id)
    target_abs_path: Optional[str] = None

    try:
        # --- Определяем целевой абсолютный путь ---
        if os.path.isabs(filename):
            # Обработка абсолютного пути
            if not caller_is_admin:
                logger.warning(f"Non-admin provided absolute path DENIED: user={user_id}, path='{filename}'")
                return False, None
            # Нормализуем и проверяем абсолютный путь
            target_abs_path = os.path.abspath(filename)
            logger.debug(f"Absolute path provided by admin: '{filename}'. Resolved to: '{target_abs_path}'")
        else:
            # Обработка относительного пути
            # Нормализуем, убирая начальные слеши и проверяя '..'
            normalized_relative_path = os.path.normpath(filename.lstrip('/' + os.sep))
            if '..' in normalized_relative_path.split(os.sep):
                logger.warning(f"Path traversal attempt using '..' DENIED: user={user_id}, filename='{filename}'")
                return False, None
            # По умолчанию строим путь внутри директории чата
            try: chat_dir_relative = str(chat_id)
            except ValueError: logger.error(f"Invalid chat_id format: {chat_id}"); return False, None
            target_abs_path = os.path.abspath(os.path.join(abs_env_dir, chat_dir_relative, normalized_relative_path))
            logger.debug(f"Relative path '{filename}' resolved to: '{target_abs_path}'")

        # --- Проверки безопасности на основе target_abs_path ---
        if target_abs_path is None: # На всякий случай
            logger.error("Internal logic error: target_abs_path is None after path determination.")
            return False, None

        # 1. Проверка нахождения в границах проекта
        is_within_project = target_abs_path == abs_project_dir or target_abs_path.startswith(abs_project_dir + os.sep)
        if not is_within_project:
             logger.warning(f"Path access DENIED (outside project root): path='{target_abs_path}', project_root='{abs_project_dir}'")
             return False, None

        # 2. Проверка ролей
        if caller_is_admin:
            # Админу разрешено все в пределах проекта (проверка is_within_project пройдена)
            logger.debug(f"Admin access GRANTED for path: '{target_abs_path}'")
            is_safe = True
        else:
            # Не-админ должен быть СТРОГО внутри своей директории чата в /env
            try: chat_dir_relative = str(chat_id)
            except ValueError: logger.error(...); return False, None # Повторная проверка
            chat_dir_abs = os.path.abspath(os.path.join(abs_env_dir, chat_dir_relative))
            is_within_chat_dir = target_abs_path == chat_dir_abs or target_abs_path.startswith(chat_dir_abs + os.sep)

            # Дополнительно убедимся, что он также внутри основной /env директории (хотя chat_dir_abs уже должен это гарантировать)
            is_within_env = target_abs_path.startswith(abs_env_dir + os.sep)

            if not (is_within_env and is_within_chat_dir):
                 logger.warning(f"Path access DENIED (non-admin outside chat dir or env dir): path='{target_abs_path}', allowed='{chat_dir_abs}'")
                 return False, None
            is_safe = True

        # --- Создание директорий (если нужно и путь безопасен) ---
        if is_safe:
            if ensure_chat_dir_exists:
                 # Всегда создаем директорию чата /env/{chat_id}
                 if not await _ensure_specific_chat_dir_exists(chat_id):
                      logger.error(f"Failed to ensure chat directory exists for chat_id {chat_id}. Cannot proceed.")
                      return False, None

                 # Если целевой путь находится вне этой директории (только для админа),
                 # создаем его родительскую директорию.
                 target_parent_dir = os.path.dirname(target_abs_path)
                 chat_dir_abs = os.path.abspath(os.path.join(abs_env_dir, str(chat_id))) # Получаем снова для сравнения

                 # Создаем родительскую папку файла, если она не является директорией чата
                 # и если она существует (не корневой слеш)
                 if target_parent_dir and target_parent_dir != chat_dir_abs and target_parent_dir != abs_env_dir:
                      try:
                           await aiofiles.os.makedirs(target_parent_dir, exist_ok=True)
                           logger.debug(f"Ensured target parent directory exists: {target_parent_dir}")
                      except Exception as mkdir_err:
                           logger.error(f"Failed to ensure target parent directory exists '{target_parent_dir}': {mkdir_err}")
                           return False, None # Не можем гарантировать местоположение

            # Все проверки и создания директорий пройдены
            return True, target_abs_path
        else:
            # Если is_safe остался False (не должно произойти при текущей логике)
            logger.error(f"Path safety check failed unexpectedly for user={user_id}, filename='{filename}'")
            return False, None

    except ValueError as ve:
        logger.error(f"Error processing chat_id or path for '{filename}': {ve}")
        return False, None
    except Exception as e:
        logger.error(f"Unexpected error checking path safety user={user_id}, file='{filename}': {e}", exc_info=True)
        return False, None


async def _ensure_specific_chat_dir_exists(chat_id: int) -> bool:
    """
    Асинхронно проверяет и создает базовую директорию env и директорию для указанного chat_id.
    Получает базовый путь ИЗ НАСТРОЕК внутри функции.

    Args:
        chat_id (int): ID чата, для которого создается директория.

    Returns:
        bool: True, если директория чата успешно создана или уже существует.
    """
    # Получаем базовый путь из настроек при каждом вызове
    if not settings.env_dir_path:
        logger.error("Cannot ensure chat directory: ENV directory path is not configured.")
        return False
    abs_env_dir = os.path.abspath(settings.env_dir_path)
    if not abs_env_dir:
         logger.error("Cannot ensure chat directory: Failed to resolve absolute path from settings.")
         return False

    if aiofiles is None:
         logger.error("Cannot ensure chat directory: aiofiles library missing.")
         return False
    if not isinstance(chat_id, int):
        logger.error(f"Cannot create directory for invalid chat_id type: {type(chat_id)}, value: {chat_id}")
        return False

    try:
        # 1. Убедимся, что базовая директория env существует
        # Эта проверка заменит функциональность удаленной _initialize_env_dir
        await aiofiles.os.makedirs(abs_env_dir, exist_ok=True)
        logger.debug(f"Ensured base ENV directory exists: {abs_env_dir}")

        # 2. Создаем директорию чата
        chat_dir_relative = str(chat_id) # Имя папки = ID чата
        chat_dir_abs = os.path.abspath(os.path.join(abs_env_dir, chat_dir_relative))

        # Доп. проверка безопасности: убедимся, что создаем внутри abs_env_dir
        # Сравниваем нормализованные пути для надежности
        if not os.path.commonpath([abs_env_dir]) == os.path.commonpath([abs_env_dir, chat_dir_abs]):
             logger.critical(f"SECURITY ALERT: Attempt create dir outside ENV_DIR denied: "
                             f"chat_id={chat_id}, path='{chat_dir_abs}', base_env='{abs_env_dir}'")
             return False

        # Асинхронно создаем директорию чата
        await aiofiles.os.makedirs(chat_dir_abs, exist_ok=True)
        logger.debug(f"Ensured specific chat directory exists: {chat_dir_abs}")
        return True

    except OSError as e:
        # Используем locals() для получения chat_dir_abs, если он был определен до ошибки
        chat_path_for_log = locals().get('chat_dir_abs', 'N/A')
        logger.error(f"Failed create/access directory for chat_id={chat_id} (base: {abs_env_dir}, specific: {chat_path_for_log}): {e}", exc_info=True)
        return False
    except ValueError: # Ошибка преобразования chat_id в строку
        logger.error(f"Invalid chat_id format for directory name: {chat_id}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error ensuring chat directory for chat_id={chat_id}: {e}", exc_info=True)
        return False

# (Удаляем блок if __name__ == '__main__', т.к. это теперь сервисный модуль)
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

async def get_safe_chat_path(
    chat_id: int,
    filename: str,
    user_id: Optional[int] = None,
    ensure_chat_dir_exists: bool = False
) -> Tuple[bool, Optional[str]]:
    """
    Асинхронно строит и проверяет путь к файлу/директории внутри ИЗОЛИРОВАННОГО окружения.

    - Проверяет выход за пределы корневой директории `env`.
    - Для не-администраторов ограничивает доступ директорией `env/{chat_id}`.
    - Администраторы могут получать доступ к любой поддиректории внутри `env`.
    - Опционально проверяет и создает директорию чата (и базовую директорию env).

    Args:
        chat_id (int): ID чата, для которого запрашивается путь.
        filename (str): Относительный путь к файлу/директории.
        user_id (Optional[int]): ID пользователя для проверки прав.
        ensure_chat_dir_exists (bool): Если True, создает директорию `env/{chat_id}`, если ее нет.

    Returns:
        tuple[bool, Optional[str]]: Кортеж (is_safe, absolute_path).
                                     is_safe=True, если путь безопасен.
                                     absolute_path - безопасный абсолютный путь или None.
    """
    # Получаем базовый путь из настроек при каждом вызове
    if not settings.env_dir_path:
        logger.error("Cannot check path safety: ENV directory path is not configured in settings.")
        return False, None
    if aiofiles is None:
         logger.error("Cannot check path safety: aiofiles library is missing.")
         return False, None

    abs_env_dir = os.path.abspath(settings.env_dir_path)
    logger.debug(f"Using absolute ENV directory path: {abs_env_dir}") # Debug log

    # Проверка входных данных
    if not isinstance(chat_id, int):
        logger.error(f"Invalid chat_id type for path check: {type(chat_id)}, value: {chat_id}")
        return False, None
    if not filename or not isinstance(filename, str):
         logger.error(f"Invalid filename provided for path check: {filename}")
         return False, None

    caller_is_admin = is_admin(user_id)
    target_abs_path: Optional[str] = None
    safe_path_determined = False

    try:
        # --- Логика определения целевого пути ---
        # Проверяем, содержит ли filename разделитель пути
        is_simple_filename = os.sep not in filename and '\\\\' not in filename # Добавим проверку для Windows

        if is_simple_filename:
            # Простой файл -> помещаем в директорию чата
            logger.debug(f"Simple filename detected ('{filename}'). Constructing path within chat dir {chat_id}.")
            try:
                # Используем chat_id как имя директории
                chat_dir_relative = str(chat_id)
            except ValueError: # На случай, если chat_id не может быть строкой
                 logger.error(f"Invalid chat_id format for directory name: {chat_id}")
                 return False, None

            # Строим путь внутри директории чата
            target_abs_path = os.path.abspath(os.path.join(abs_env_dir, chat_dir_relative, filename))

            # Проверка, что путь все еще внутри abs_env_dir (санити чек)
            if not target_abs_path.startswith(abs_env_dir + os.sep):
                 logger.critical(f"SECURITY CRITICAL: Path constructed for simple filename ('{filename}') ended up outside ENV_DIR: '{target_abs_path}'")
                 return False, None
            safe_path_determined = True
            logger.debug(f"Path for simple filename resolved to: {target_abs_path}")

        else:
            # Файл содержит путь -> используем существующую логику разрешения + проверки прав
            logger.debug(f"Filename with path detected ('{filename}'). Using standard resolution logic.")
            # Нормализуем относительный путь, который передала модель
            normalized_relative_path = os.path.normpath(filename.lstrip('/' + os.sep))

            # Убедимся, что путь не пытается выйти за пределы текущей директории через '..'
            if '..' in normalized_relative_path.split(os.sep):
                 logger.warning(f"Path traversal attempt using '..' detected after normalization: user={user_id}, filename='{filename}'")
                 return False, None

            # Формируем ПОЛНЫЙ предполагаемый абсолютный путь относительно КОРНЯ ENV
            target_abs_path = os.path.abspath(os.path.join(abs_env_dir, normalized_relative_path))

            # --- 1. Главная проверка безопасности: Путь ДОЛЖЕН быть ВНУТРИ abs_env_dir ---\
            if not (target_abs_path == abs_env_dir or target_abs_path.startswith(abs_env_dir + os.sep)):
                logger.warning(f"Path traversal attempt DENIED (outside root ENV_DIR): "
                               f"user={user_id}, admin={caller_is_admin}, filename='{filename}', "
                               f"resolved='{target_abs_path}', expected_prefix='{abs_env_dir + os.sep}'")
                return False, None

            # --- 2. Проверка для НЕ-администраторов: Путь должен быть ВНУТРИ директории ИХ чата ---\
            if not caller_is_admin:
                try:
                    chat_dir_relative = str(chat_id) # Имя папки = ID чата
                except ValueError:
                     logger.error(f"Invalid chat_id format for directory name: {chat_id}")
                     return False, None

                chat_dir_abs = os.path.abspath(os.path.join(abs_env_dir, chat_dir_relative))

                # Путь должен начинаться с директории чата или быть равен ей
                if not (target_abs_path == chat_dir_abs or target_abs_path.startswith(chat_dir_abs + os.sep)):
                    logger.warning(f"Path access DENIED (outside specific chat dir): "
                                   f"user={user_id} (NOT admin), filename='{filename}', "
                                   f"resolved='{target_abs_path}', expected_prefix='{chat_dir_abs + os.sep}'")
                    return False, None
                # Не-админ в своей папке - путь безопасен
                safe_path_determined = True

            # --- 3. Для Администраторов: Доступ разрешен, т.к. проверка 1 пройдена ---\
            elif caller_is_admin:
                logger.debug(f"Admin access GRANTED for path: user={user_id}, filename='{filename}', resolved='{target_abs_path}'")
                safe_path_determined = True

        # --- Финальные шаги, если путь признан безопасным ---\
        if safe_path_determined and target_abs_path:
            # --- (Опционально) Убедиться, что директория чата существует ---\
            if ensure_chat_dir_exists:
                 # Теперь _ensure_specific_chat_dir_exists также создаст базовую директорию, если нужно
                 if not await _ensure_specific_chat_dir_exists(chat_id):
                      logger.error(f"Failed to ensure chat directory exists for chat_id {chat_id}. Path was deemed safe, but dir creation failed.")
                      return False, None # Не можем гарантировать существование директории

            # Все проверки пройдены
            return True, target_abs_path
        else:
            # Сюда не должны попасть, если логика верна, но на всякий случай
            logger.error(f"Path safety check failed unexpectedly for user={user_id}, filename='{filename}'")
            return False, None

    except ValueError as ve: # Ошибка преобразования chat_id в строку
        logger.error(f"Error processing chat_id '{chat_id}' for path: {ve}")
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
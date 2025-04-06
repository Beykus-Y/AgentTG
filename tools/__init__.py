# tools/__init__.py

import logging
import asyncio
import importlib # <<< Добавляем importlib
from typing import Dict, Callable, Coroutine, Any

available_functions: Dict[str, Callable[..., Coroutine[Any, Any, Any]]] = {}
logger = logging.getLogger(__name__)
logger.info("Initializing tools...")

# Список модулей с инструментами
tool_module_paths = [
    ".basic_tools",
    ".communication_tools",
    ".user_data_tools",
    ".environment_tools",
    ".deep_search_tool",
    ".meta_tools"
    # Добавляйте сюда другие модули с инструментами, если они появятся
]

internal_function_names = {
    "get_safe_chat_path",
    "ensure_chat_dir",
    # Добавьте другие внутренние/вспомогательные функции, если они есть
}
# <<< ИЗМЕНЕНО: Список имен функций, для которых пока нет _ast_transformer.py >>>
# (Если вы выбрали вариант (б) для Исправления 1)
disabled_tool_names = {
    # "replace_code_block_ast" # <<< ЗАКОММЕНТИРОВАНО, т.к. файл _ast_transformer.py создан
}

# Итерируемся по модулям и собираем функции
for module_path in tool_module_paths:
    try:
        module = importlib.import_module(module_path, package=__name__)
        found_in_module = 0
        logger.debug(f"Processing tool module: {module_path}")

        for func_name, func_obj in module.__dict__.items():
            logger.debug(f"Checking item from {module_path}: name='{func_name}', is_coroutine={asyncio.iscoroutinefunction(func_obj)}")
            # <<< ИЗМЕНЕНО: Условие регистрации >>>
            if (asyncio.iscoroutinefunction(func_obj) and
                    not func_name.startswith('_') and
                    func_name not in internal_function_names and # Не регистрируем внутренние
                    func_name not in disabled_tool_names): # Не регистрируем отключенные
                if func_name in available_functions:
                    logger.warning(f"Duplicate tool function name '{func_name}' found in {module_path}. Overwriting.")
                available_functions[func_name] = func_obj
                found_in_module += 1

        if found_in_module > 0:
            logger.info(f"Registered {found_in_module} tools from {module_path}.")
        else:
             logger.debug(f"No valid async tool functions found or registered in {module_path}.")

    except ImportError as e:
        logger.warning(f"Could not import tool module {module_path}: {e}.")
    except Exception as e:
         logger.error(f"Unexpected error processing module {module_path}: {e}", exc_info=True)

logger.info(f"Total initialized tools: {len(available_functions)}. Names: {list(available_functions.keys())}")

# Явно экспортируем словарь для использования в других частях приложения
__all__ = ["available_functions"]
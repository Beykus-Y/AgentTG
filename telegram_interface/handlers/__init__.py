# telegram_interface/handlers/__init__.py

from aiogram import Router

# Импортируем роутеры из каждого модуля хендлеров
# Обернем в try-except на случай, если какие-то файлы еще не созданы
try: from . import admin_commands
except ImportError: admin_commands = None
try: from . import common_messages
except ImportError: common_messages = None
try: from . import error_handler
except ImportError: error_handler = None
try: from . import news_setup_fsm
except ImportError: news_setup_fsm = None
try: from . import user_commands
except ImportError: user_commands = None


# Собираем все существующие роутеры в один список для main.py
# (или можно создать главный роутер здесь и включать в него остальные)
# router_list = [
#     rt.router for rt in [admin_commands, common_messages, error_handler, news_setup_fsm, user_commands]
#     if rt and hasattr(rt, 'router') and isinstance(rt.router, Router)
# ]

# Альтернативно, main.py может импортировать каждый модуль отдельно.
# Оставим этот __init__.py для ясности структуры пакета.

# Можно определить __all__, если необходимо явно указать, что экспортируется
# __all__ = ['admin_commands', 'common_messages', ...]
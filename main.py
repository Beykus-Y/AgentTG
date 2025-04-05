# main.py

import asyncio
import logging
import os # <<< Добавлено для работы с путями >>>

# --- Загрузка основных компонентов ---
# Импортируем в первую очередь, чтобы настроить логирование и получить базовые объекты
try:
    from config import settings, logger # Импортируем настроенный логгер
    from bot_loader import bot, dp
    from bot_lifecycle import on_startup, on_shutdown
    # Импортируем сервисы для фоновых задач
    #from services.news_service import news_service
except ImportError as e:
    logging.basicConfig(level=logging.CRITICAL)
    init_logger = logging.getLogger(__name__)
    init_logger.critical(f"Failed import core components: {e}. Exiting.", exc_info=True)
    exit(1)
except Exception as e:
    logging.basicConfig(level=logging.CRITICAL)
    init_logger = logging.getLogger(__name__)
    init_logger.critical(f"Unexpected error during initial imports: {e}. Exiting.", exc_info=True)
    exit(1)

# --- Импорт и Регистрация Роутеров (Хендлеров) ---
# Порядок регистрации важен! Более специфичные роутеры должны идти раньше.
handlers_imported = False # Флаг для условной регистрации
try:
    from telegram_interface.handlers import (
        admin_commands,
        news_setup_fsm,
        user_commands,
        file_handler,       # Импортируем file_handler
        common_messages,
        error_handler       # Обработчик ошибок лучше регистрировать одним из первых
    )
    handlers_imported = True # Если импорт успешен
    logger.info("Handler modules imported successfully.")
except ImportError as e:
     logger.error(f"Failed to import handler modules: {e}. Some features will be unavailable.", exc_info=True)
except Exception as e:
     logger.error(f"Unexpected error during handler import: {e}.", exc_info=True)

# --- Регистрация роутеров (Только если импорт успешен!) ---
if handlers_imported:
    try:
        # Включаем роутер ошибок (если есть)
        if error_handler and hasattr(error_handler, 'router') and error_handler.router:
            dp.include_router(error_handler.router)
            logger.info("Error handler router registered.")
        else:
            logger.warning("Error handler router not found or invalid, skipping registration.")

        # Включаем роутеры команд и FSM
        if admin_commands and hasattr(admin_commands, 'router') and admin_commands.router:
            dp.include_router(admin_commands.router)
            logger.info("Admin commands router registered.")
        else:
            logger.warning("Admin commands router not found or invalid, skipping registration.")

        if news_setup_fsm and hasattr(news_setup_fsm, 'router') and news_setup_fsm.router:
            dp.include_router(news_setup_fsm.router)
            logger.info("News setup FSM router registered.")
        else:
            logger.warning("News setup FSM router not found or invalid, skipping registration.")

        if user_commands and hasattr(user_commands, 'router') and user_commands.router:
            dp.include_router(user_commands.router)
            logger.info("User commands router registered.")
        else:
            logger.warning("User commands router not found or invalid, skipping registration.")

        # <<< ВАЖНО: Включаем роутер файлов ЗДЕСЬ (перед common_messages) >>>
        if file_handler and hasattr(file_handler, 'router') and file_handler.router:
            dp.include_router(file_handler.router)
            logger.info("File handler router registered.") # <--- Этот лог должен появиться!
        else:
            logger.warning("File handler router not found or invalid, skipping registration.")

        # Роутер для общих сообщений (текст) регистрируем одним из последних
        if common_messages and hasattr(common_messages, 'router') and common_messages.router:
            dp.include_router(common_messages.router)
            logger.info("Common messages router registered.")
        else:
            logger.warning("Common messages router not found or invalid, skipping registration.")

        logger.info("Finished attempting to register all handlers/routers.")

    except Exception as e:
         # Эта ошибка может возникнуть, если сам вызов include_router падает
         logger.error(f"Unexpected error during handler registration process: {e}.", exc_info=True)
else:
    logger.error("Skipping all handler registration due to import errors.")

# --- Регистрация Middleware ---
try:
    # Импортируем классы Middleware
    from telegram_interface.middlewares.antiflood import AntiFloodMiddleware
    from telegram_interface.middlewares.stats_counter import StatsCounterMiddleware

    # Регистрация Middleware (outer - выполняются до фильтров и хендлеров)
    # Порядок важен: антифлуд лучше ставить раньше статистики
    dp.update.outer_middleware(AntiFloodMiddleware(rate_limit=0.7)) # Пример лимита 0.7 сек
    logger.info("AntiFloodMiddleware registered.")

    dp.update.outer_middleware(StatsCounterMiddleware())
    logger.info("StatsCounterMiddleware registered.")

    logger.info("All middlewares registered successfully.")

except ImportError as e:
     logger.warning(f"Could not import middleware: {e}. Running without custom middleware.", exc_info=True)
except Exception as e:
     logger.error(f"Unexpected error during middleware registration: {e}.", exc_info=True)


# --- Основная функция запуска ---
async def main():
    """Основная асинхронная функция запуска бота."""

    # --- Настройка логирования ---
    log_directory = "logs" # <<< Папка для логов >>>
    # <<< ИЗМЕНЕНИЕ: Логика для версионирования файлов логов >>>
    base_log_filename = "bot.log"
    log_file_path = os.path.join(log_directory, base_log_filename)
    log_counter = 1
    # Ищем следующий свободный номер файла
    while os.path.exists(log_file_path):
        log_file_path = os.path.join(log_directory, f"bot_{log_counter}.log")
        log_counter += 1
    # log_file_path теперь содержит путь к новому файлу лога
    # --- КОНЕЦ ИЗМЕНЕНИЯ ---
    # log_file_path = os.path.join(log_directory, "bot.log") # <<< Старый путь, УДАЛЕНО >>>

    # --- Создание папки logs, если не существует ---
    try:
        os.makedirs(log_directory, exist_ok=True)
    except OSError as e:
        # Используем стандартный логгер, т.к. наш еще не настроен
        print(f"CRITICAL: Could not create log directory '{log_directory}': {e}")
        # Попытка продолжить без файлового логгера

    # --- Настройка формата логов ---
    log_format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    log_date_format = '%Y-%m-%d %H:%M:%S'

    # --- Базовая конфигурация (вывод в консоль) ---
    logging.basicConfig(level=logging.INFO, # Устанавливаем INFO как базовый уровень
                        format=log_format,
                        datefmt=log_date_format)

    # --- Получаем корневой логгер ---
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG) # Устанавливаем DEBUG для корневого логгера

    # --- Настройка FileHandler (вывод в файл) ---
    try:
        # <<< ИЗМЕНЕНИЕ: Используем найденный log_file_path >>>
        file_handler = logging.FileHandler(log_file_path, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG) # Уровень для файла - DEBUG
        file_formatter = logging.Formatter(log_format, datefmt=log_date_format)
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
        print(f"INFO: Logging configured. Log file: {log_file_path}") # Сообщение в консоль
    except Exception as e:
        root_logger.critical(f"CRITICAL: Failed to set up file logging to '{log_file_path}': {e}", exc_info=True)

    logger = logging.getLogger(__name__) # Получаем логгер для main.py
    logger.info("--- Bot Startup Sequence Initiated ---")

    # --- Обертываем основной код в try для обработки KeyboardInterrupt/Exception ---
    try:
        # --- Регистрация функций жизненного цикла ---
        # Важно сделать это до запуска поллинга
        dp.startup.register(on_startup)
        dp.shutdown.register(on_shutdown)
        logger.info("Startup and shutdown handlers registered.")

        # --- Запуск фоновых задач ---
        # Запускаем сервис новостей после регистрации startup/shutdown,
        # чтобы он мог корректно запуститься/остановиться.
        # Передаем экземпляр бота в сервис новостей.
        #asyncio.create_task(news_service.start(bot))
        #logger.info("News service background task scheduled.")
        # Добавьте здесь запуск других фоновых сервисов, если они есть

        # --- Удаление вебхука и пропуск старых обновлений ---
        logger.info("Attempting to delete webhook and drop pending updates...")
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook deleted and pending updates dropped successfully.")
        except Exception as e:
            # Не критично, если не удалось, но логируем как warning
            logger.warning(f"Could not delete webhook or drop pending updates: {e}")

        # --- Запуск Поллинга (вложенный try/except/finally) ---
        logger.info("Starting bot polling...")
        try:
            # allowed_updates можно настроить для оптимизации,
            # чтобы получать только нужные типы обновлений.
            # dp.resolve_used_update_types() автоматически определяет используемые типы.
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            # Если не хотите автоопределение:
            # await dp.start_polling(bot)
        except Exception as polling_ex: # Ловим ошибки именно поллинга
            logger.critical(f"Polling failed critically: {polling_ex}", exc_info=True)
        finally:
            # Этот finally выполнится, когда поллинг остановится (штатно или из-за ошибки)
            logger.info("Polling stopped. Initiating shutdown sequence (inner finally)...")
            # Останавливаем фоновые задачи
            # await news_service.stop()
            # (Добавить остановку других сервисов)

            # Закрываем сессию бота (on_shutdown вызывается автоматически при остановке dp)
            # await bot.session.close() # Это обычно делается в on_shutdown
            logger.info("Inner shutdown sequence presumably completed via on_shutdown handler.")

        # Этот лог выполнится, если поллинг завершился штатно (не по Ctrl+C или Exception)
        logger.info("Bot polling finished normally.")

    # --- Обработка остановки (Ctrl+C) и других ошибок на уровне main ---
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (KeyboardInterrupt). Handling within main.")
        # Дополнительные действия при остановке Ctrl+C, если нужно
    except Exception as main_ex:
        logger.critical(f"CRITICAL ERROR during bot lifecycle inside main: {main_ex}", exc_info=True)
    finally:
        # Этот finally выполнится ВСЕГДА при выходе из main (штатно, Ctrl+C, Exception)
        logger.info("--- Entering main() finally block --- ")
        # Важно закрыть соединение с БД в любом случае, даже если поллинг упал
        # Импортируем здесь, чтобы избежать циклического импорта, если main вызовет ошибку раньше
        try:
            from database.connection import close_db
            await close_db()
            logger.info("--- Bot Lifecycle in main() finished. DB connection closed. ---")
        except ImportError:
             logger.error("Failed to import close_db in main finally block.")
        except Exception as db_close_err:
             logger.error(f"Error closing DB connection in main finally block: {db_close_err}")


if __name__ == '__main__':
    # Настраиваем базовое логирование на случай ошибок *до* настройки в main()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    temp_logger = logging.getLogger(__name__)
    temp_logger.info("Initializing bot application...")
    try:
        # Запускаем асинхронную функцию main
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        # Корректная остановка по Ctrl+C или SystemExit до или во время запуска main
        temp_logger.info("Bot stopped by user (KeyboardInterrupt/SystemExit) at top level.")
    except Exception as e:
        # Ловим все остальные неожиданные исключения на верхнем уровне (до старта main)
        temp_logger.critical(f"Unhandled exception at main level: {e}", exc_info=True)
        exit(1) # Завершение с кодом ошибки
    finally:
        temp_logger.info("Bot application finished.")
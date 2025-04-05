# database/connection.py
import aiosqlite
import logging
import os
from typing import Optional

# Импортируем настройки из корневого config.py
# Предполагается, что config.py находится на два уровня выше
try:
    from config import settings
except ImportError:
    # Заглушка на случай, если запуск идет из другого места или config.py нет
    class MockSettings:
        db_path: str = "database/bot_db.sqlite" # Путь по умолчанию
    settings = MockSettings()
    logging.warning("Could not import settings from config.py, using default DB path.")


logger = logging.getLogger(__name__)

_connection: Optional[aiosqlite.Connection] = None
BUSY_TIMEOUT_MS = 5000 # 5 секунд

async def get_connection() -> aiosqlite.Connection:
    """
    Получает или создает асинхронное соединение с БД SQLite.
    Использует путь из настроек (config.settings.db_path).
    """
    global _connection
    if _connection is None:
        db_file_path = os.path.abspath(settings.db_path)
        db_dir = os.path.dirname(db_file_path)
        logger.info(f"Database path configured to: {db_file_path}")

        try:
            # Убедимся, что директория существует
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
                logger.info(f"Ensured database directory exists: {db_dir}")

            _connection = await aiosqlite.connect(
                db_file_path,
                timeout=BUSY_TIMEOUT_MS / 1000.0 # timeout в секундах
            )
            # Включаем поддержку внешних ключей
            await _connection.execute("PRAGMA foreign_keys = ON;")
            # Устанавливаем Row Factory для доступа к колонкам по имени
            _connection.row_factory = aiosqlite.Row
            # Устанавливаем таймаут ожидания при блокировке БД
            await _connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS};")
            # Включаем WAL режим
            try:
                await _connection.execute("PRAGMA journal_mode=WAL;")
                logger.info("SQLite WAL mode enabled.")
            except Exception as wal_err:
                # Не критично, если не удалось, но логируем
                logger.warning(f"Could not enable WAL journal mode: {wal_err}", exc_info=True)
            await _connection.commit() # Важно закоммитить PRAGMA
            logger.info(f"Database connection established to {db_file_path} with busy_timeout={BUSY_TIMEOUT_MS}ms. Connection object ID: {id(_connection)}")
        except OSError as e:
            logger.critical(f"Failed to create/access database directory {db_dir}: {e}", exc_info=True)
            raise ConnectionError(f"Failed to create/access database directory: {e}") from e
        except aiosqlite.Error as e:
            logger.critical(f"Failed to connect to database {db_file_path}: {e}", exc_info=True)
            raise ConnectionError(f"Failed to connect to database: {e}") from e
        except Exception as e:
            logger.critical(f"Unexpected error connecting to database {db_file_path}: {e}", exc_info=True)
            raise ConnectionError(f"Unexpected error connecting to database: {e}") from e
    else:
        # Логируем ID существующего соединения
        logger.debug(f"Reusing existing DB connection. Connection object ID: {id(_connection)}")
    return _connection

async def close_db():
    """Закрывает активное соединение с БД."""
    global _connection
    if _connection:
        try:
            # <<< Добавляем явный checkpoint перед закрытием >>>
            try:
                logger.debug("Attempting WAL checkpoint before closing connection...")
                # TRUNCATE пытается уменьшить WAL файл, но может быть медленнее
                # Можно попробовать PASSIVE или FULL, если TRUNCATE вызывает проблемы
                await _connection.execute("PRAGMA wal_checkpoint(TRUNCATE);")
                logger.info("WAL checkpoint successful before closing.")
            except Exception as cp_err:
                logger.warning(f"WAL checkpoint failed before closing: {cp_err}", exc_info=True)
            # <<< Конец checkpoint >>>

            await _connection.close()
            _connection = None
            logger.info("Database connection closed.")
        except aiosqlite.Error as e:
             logger.error(f"Error closing database connection: {e}", exc_info=True)

async def init_db():
    """
    Инициализирует структуру базы данных, создавая все необходимые таблицы и индексы,
    если они еще не существуют.
    """
    logger.info("Initializing database schema...")
    conn = await get_connection()
    try:
        # 1. Таблица профилей пользователей
        logger.debug("Executing CREATE TABLE user_profiles...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                last_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                avatar_file_id TEXT,
                avatar_description TEXT
            );
        ''')
        logger.debug("Checked/Created table: user_profiles")

        # 2. Таблица заметок пользователей
        logger.debug("Executing CREATE TABLE user_notes...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_notes (
                note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                category TEXT NOT NULL COLLATE NOCASE, -- Категория без учета регистра
                value TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE,
                UNIQUE (user_id, category) -- Уникальная пара пользователь-категория
            );
        ''')
        logger.debug("Executing CREATE INDEX idx_user_notes_user_id...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_user_notes_user_id ON user_notes (user_id);')
        logger.debug("Checked/Created table and index: user_notes")

        # 3. Таблица истории чатов
        logger.debug("Executing CREATE TABLE chat_history...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'model', 'system', 'function')),
                user_id INTEGER, -- NULL для 'model', 'system' и 'function'
                parts_json TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE SET NULL -- При удалении профиля ставим NULL
            );
        ''')
        logger.debug("Executing CREATE INDEX idx_chat_history_chat_id_ts...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_chat_history_chat_id_ts ON chat_history (chat_id, timestamp);')
        logger.debug("Checked/Created table and index: chat_history")

        # 4. Таблица настроек чатов
        logger.debug("Executing CREATE TABLE chat_settings...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                custom_prompt TEXT,
                ai_mode TEXT DEFAULT 'pro', -- 'default' (g4f) или 'pro' (gemini)
                gemini_model TEXT,          -- Имя конкретной модели Gemini
                last_update_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        logger.debug("Checked/Created table: chat_settings")

        # 5. Таблица подписок на новости
        logger.debug("Executing CREATE TABLE news_subscriptions...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS news_subscriptions (
                subscription_id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL UNIQUE, -- ID канала Telegram
                topics_json TEXT NOT NULL,          -- Список тем в JSON
                schedule_json TEXT NOT NULL,        -- Список времени в JSON
                last_post_ts DATETIME             -- Время последней успешной отправки
            );
        ''')
        logger.debug("Executing CREATE INDEX idx_news_subs_channel...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_news_subs_channel ON news_subscriptions (channel_id);')
        logger.debug("Checked/Created table and index: news_subscriptions")

        # 6. Таблица отправленных GUID новостей
        logger.debug("Executing CREATE TABLE sent_news_guids...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS sent_news_guids (
                guid TEXT PRIMARY KEY,
                sent_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        logger.debug("Executing CREATE INDEX idx_sent_guids_ts...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_sent_guids_ts ON sent_news_guids (sent_ts);')
        logger.debug("Checked/Created table and index: sent_news_guids")

        # 7. Таблица статистики сообщений
        logger.debug("Executing CREATE TABLE message_stats...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS message_stats (
                stat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_count INTEGER DEFAULT 0,
                last_message_ts DATETIME DEFAULT CURRENT_TIMESTAMP
                -- Убираем FOREIGN KEY для теста
                -- FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE,
                -- UNIQUE (chat_id, user_id) -- Уникальная пара чат-пользователь
            );
        ''')
        logger.debug("Executing CREATE INDEX idx_msg_stats_chat_user...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_msg_stats_chat_user ON message_stats (chat_id, user_id);')
        logger.debug("Executing CREATE INDEX idx_msg_stats_chat_count...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_msg_stats_chat_count ON message_stats (chat_id, message_count DESC);')
        logger.debug("Checked/Created table and indexes: message_stats")

        # 8. Таблица предупреждений пользователей
        logger.debug("Executing CREATE TABLE user_warnings...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS user_warnings (
                warn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                warn_count INTEGER DEFAULT 0,
                last_warn_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user_profiles(user_id) ON DELETE CASCADE,
                UNIQUE (chat_id, user_id) -- Уникальная пара чат-пользователь
            );
        ''')
        logger.debug("Executing CREATE INDEX idx_user_warns_chat_user...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_user_warns_chat_user ON user_warnings (chat_id, user_id);')
        logger.debug("Checked/Created table and index: user_warnings")

        # 9. Таблица логов выполнения инструментов (Tool Executions)
        logger.debug("Executing CREATE TABLE tool_executions...")
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS tool_executions (
                execution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER, -- Пользователь, инициировавший взаимодействие (может быть NULL)
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                tool_name TEXT NOT NULL,
                tool_args_json TEXT, -- Аргументы вызова функции в JSON
                status TEXT NOT NULL CHECK(status IN ('success', 'error', 'not_found', 'warning', 'timeout')),
                return_code INTEGER, -- Код возврата для команд/скриптов
                result_message TEXT, -- Сообщение из словаря результата
                stdout TEXT, -- Стандартный вывод (ограничить длину при записи!)
                stderr TEXT, -- Стандартный вывод ошибок (ограничить длину при записи!)
                trigger_message_id INTEGER -- Опционально: ID сообщения пользователя
            );
        ''')
        logger.debug("Executing CREATE INDEX idx_tool_exec_chat_time...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_tool_exec_chat_time ON tool_executions (chat_id, timestamp DESC);')
        logger.debug("Executing CREATE INDEX idx_tool_exec_tool_name...")
        await conn.execute('CREATE INDEX IF NOT EXISTS idx_tool_exec_tool_name ON tool_executions (tool_name);')
        logger.debug("Checked/Created table and indexes: tool_executions")

        logger.debug("Committing schema changes...") # Лог перед коммитом
        await conn.commit()
        logger.info("Database schema initialization complete.")

    except aiosqlite.Error as e:
        logger.error(f"Error during database initialization: {e}", exc_info=True)
        try:
            await conn.rollback() # Откатываем изменения при ошибке
        except Exception as rb_e:
            logger.error(f"Error during rollback after DB init failure: {rb_e}")
        raise # Перевыбрасываем исключение, т.к. инициализация критична
    except Exception as e:
         logger.error(f"Unexpected error during database initialization: {e}", exc_info=True)
         raise
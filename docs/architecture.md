# Архитектура Проекта

Этот документ описывает высокоуровневую архитектуру и поток данных Telegram-бота на базе Google Gemini.

## 1. Ключевые Технологии

*   **Telegram Bot Framework:** `aiogram` (v3.x) - Асинхронный фреймворк для создания Telegram ботов.
*   **AI Модель:** Google Gemini API (`google-generativeai`) - Используется для понимания естественного языка, генерации ответов и Function Calling.
*   **База Данных:** SQLite (`aiosqlite`) - Для асинхронного хранения данных (история, профили, настройки и т.д.).
*   **Конфигурация:** `pydantic` и `pydantic-settings` - Для загрузки, валидации и управления настройками из `.env`.
*   **Асинхронность:** `asyncio` - Основа для асинхронной работы бота, I/O операций и взаимодействия с API.
*   **HTTP Клиент:** `aiohttp` - Для асинхронных HTTP-запросов (скачивание аватаров, парсинг RSS).
*   **Парсинг:** `feedparser`, `BeautifulSoup4`, `lxml` - Для обработки RSS и HTML.
*   **Файловые Операции:** `aiofiles` - Для асинхронной работы с файлами.

## 2. Обзор Архитектуры

Бот построен по модульному принципу с разделением ответственности между компонентами:

```mermaid
graph LR
    subgraph Telegram
        A[User Input (Message/Callback)] --> B(aiogram);
    end

    subgraph Bot Application
        B --> C{Dispatcher};
        C --> D[Middlewares (Antiflood, Stats)];
        D --> E[Filters (Admin, State, etc.)];
        E --> F[Handlers (Commands, Text, FSM, etc.)];
        F --> G[Core Agent Processor];
        G --> H[History Manager];
        H --> I[Database (CRUD Ops)];
        G --> J[AI Interaction];
        J --> K[Gemini API Interface];
        K --> L[External Gemini API];
        J --> M[Function Calling Processor];
        M --> N[Tools (Functions)];
        N --> I;
        N --> O[Env Manager];
        O --> P[File System (env/...)];
        N --> Q[External APIs (Weather, Stock, etc.)];
        N --> R[Communication Tool];
        R --> B;  // Send message back via aiogram
        J --> H; // Save history after interaction
        G --> F; // Potentially return text result to handler
    end

    subgraph Background Services
        S[News Service] --> I;
        S --> Q;
        S --> B; // Send news via aiogram
        T[Lifecycle (Startup/Shutdown)] --> I;
        T --> K; // Initialize models
        T --> S; // Start/Stop services
    end

    subgraph Configuration
        U[.env File] --> V[config.py (Pydantic)];
        V --> Bot Application;
        V --> Background Services;
    end

    L --> K;
    I --> H;
    P --> O;
    Q --> N;
```

**Поток обработки сообщения:**

1.  **Получение:** `aiogram` получает обновление от Telegram API.
2.  **Middleware:** Обновление проходит через Middlewares (Антифлуд, Статистика).
3.  **Диспетчеризация:** `Dispatcher` маршрутизирует обновление на основе фильтров (команда, тип контента, состояние FSM, права админа).
4.  **Обработчик:** Соответствующий хендлер (`telegram_interface/handlers/`) принимает управление.
5.  **Ядро Агента:** Хендлер (обычно `common_messages` или команды) вызывает `core_agent.agent_processor.handle_user_request`.
6.  **Решение Lite/Pro:** `agent_processor` решает, использовать ли Lite-модель для фильтрации (в группах) или сразу передать запрос Pro-модели.
7.  **Подготовка Контекста:** `core_agent.history_manager.prepare_history` загружает историю из БД, добавляет RAG-контекст (недавние действия) и заметки о пользователе/группе.
8.  **Взаимодействие с AI:** `core_agent.ai_interaction.process_request` запускает сессию с Gemini, отправляет подготовленную историю и запрос пользователя. Обрабатывает ошибки квоты (429) с ретраями.
9.  **Function Calling (если нужно):**
    *   Если Gemini возвращает `FunctionCall`, управление передается в `core_agent.fc_processing.process_gemini_fc_cycle`.
    *   `fc_processing` находит нужный инструмент (функцию) в `tools/`.
    *   Вызывает инструмент, передавая аргументы от AI и контекст (chat_id, user_id).
    *   Инструменты могут взаимодействовать с БД, `env_manager`, внешними API.
    *   Результат выполнения инструмента (`FunctionResponse`) отправляется обратно в Gemini.
    *   Цикл повторяется до `max_steps` или пока Gemini не вернет текстовый ответ.
10. **Обработка Результата:**
    *   `core_agent.result_parser.extract_text` извлекает финальный текстовый ответ (если есть).
    *   `agent_processor` решает, нужно ли отправлять ответ пользователю (например, подавляет текст, если последним действием была отправка сообщения через инструмент).
11. **Отправка Ответа:** Ответ отправляется пользователю либо инструментом `send_telegram_message`, либо напрямую из хендлера (если AI вернул текст).
12. **Сохранение Истории:** `core_agent.history_manager.save_history` сохраняет *новые* сообщения (модели) из финальной истории взаимодействия в БД.

## 3. Ключевые Компоненты

*   **`main.py`:**
    *   Точка входа приложения.
    *   Инициализирует `aiogram`, загружает настройки.
    *   Настраивает логирование (включая запись в файлы с ротацией).
    *   Регистрирует роутеры хендлеров, middlewares, функции жизненного цикла (`on_startup`, `on_shutdown`).
    *   Запускает `Dispatcher.start_polling()`.
*   **`config.py` / `.env`:**
    *   Определяет настройки с помощью `pydantic` (`BaseSettings`).
    *   Загружает переменные из `.env`.
    *   Валидирует типы настроек.
    *   Предоставляет централизованный доступ к конфигурации через объект `settings`.
*   **`bot_loader.py`:**
    *   Создает и предоставляет синглтон-экземпляры `aiogram.Bot` и `aiogram.Dispatcher`.
    *   Использует `MemoryStorage` для FSM (может быть заменен на RedisStorage и т.д.).
*   **`bot_lifecycle.py`:**
    *   `on_startup`: Инициализация БД (`database.init_db`), загрузка промптов и деклараций FC, инициализация моделей Gemini (`gemini_api.setup_gemini_model`), маппинг инструментов, запуск фоновых сервисов (NewsService).
    *   `on_shutdown`: Остановка сервисов, закрытие соединения с БД (`database.close_db`), закрытие сессии бота.
*   **`core_agent/`:**
    *   `agent_processor.py`: Оркестратор обработки запроса. Решает, какую модель вызвать, координирует подготовку/сохранение истории, вызывает AI-взаимодействие. Сохраняет начальное сообщение пользователя и профиль.
    *   `ai_interaction.py`: Управляет сессией с Gemini, отправляет запросы, вызывает обработку FC, реализует логику ретраев при ошибках квоты.
    *   `history_manager.py`: Отвечает за сбор контекста (БД, RAG, заметки) для AI и сохранение результата диалога в БД. Содержит логику форматирования истории.
    *   `fc_processing.py`: Исполнитель Function Calling. Находит и вызывает нужные функции-инструменты, обрабатывает их результаты, формирует `FunctionResponse` для Gemini.
    *   `result_parser.py`: Извлекает финальный текстовый ответ из структуры истории Gemini.
    *   `response_parsers.py`: Парсит специфический JSON-ответ от Lite-модели.
*   **`ai_interface/`:**
    *   `gemini_api.py`: Низкоуровневая обертка над `google-generativeai`. Функции для настройки моделей, отправки сообщений (синхронная для `run_in_executor`), генерации описаний изображений.
*   **`database/`:**
    *   `connection.py`: Управляет подключением к SQLite, создает таблицы, включает WAL.
    *   `crud_ops/`: Модули с функциями для выполнения операций Create, Read, Update, Delete для каждой таблицы БД. Использует `aiosqlite`.
*   **`tools/`:**
    *   Содержит Python-функции, соответствующие декларациям в `declarations/`.
    *   Используют `env_manager` для безопасного доступа к файлам, `database` для работы с данными, внешние библиотеки для своих задач.
    *   Регистрируются динамически в `tools/__init__.py`.
*   **`services/`:**
    *   `env_manager.py`: Ключевой компонент для безопасности файловых операций. Валидирует пути, проверяет права доступа (админ/пользователь), обеспечивает изоляцию окружений чатов.
    *   `news_service.py`: Фоновый сервис, работающий по расписанию для парсинга и постинга новостей.
*   **`telegram_interface/`:**
    *   `handlers/`: Обработчики различных событий Telegram (команды, текст, файлы, колбэки, ошибки, шаги FSM). Вызывают `agent_processor` или напрямую взаимодействуют с БД/сервисами для простых команд.
    *   `filters/`: Кастомные фильтры для роутинга `aiogram` (например, проверка админа).
    *   `middlewares/`: Дополнительная логика обработки перед/после хендлеров (антифлуд, статистика).
    *   `states/`: Определения состояний для Finite State Machines (`aiogram.fsm`).
*   **`utils/`:**
    *   `helpers.py`: Мелкие вспомогательные функции.
    *   `converters.py`: Критически важные функции для преобразования данных истории между форматом Gemini API (`Content`, `Part`) и форматом для хранения в БД (JSON-строка в `parts_json`).

## 4. Поток Данных (Упрощенно)

1.  **Вход:** `aiogram` -> `Dispatcher` -> `Handler`
2.  **Обработка:** `Handler` -> `agent_processor`
3.  **Контекст:** `agent_processor` -> `history_manager` -> `database` (получение истории, заметок, логов)
4.  **AI Запрос:** `agent_processor` -> `ai_interaction` -> `gemini_api` -> Gemini API
5.  **Ответ AI / FC:** Gemini API -> `gemini_api` -> `ai_interaction`
6.  **Выполнение FC (если нужно):** `ai_interaction` -> `fc_processing` -> `tools` -> (`database`, `env_manager`, External API) -> `fc_processing`
7.  **Ответ Инструмента -> AI:** `fc_processing` -> `ai_interaction` -> `gemini_api` -> Gemini API
8.  **Финальный Ответ AI:** Gemini API -> `gemini_api` -> `ai_interaction` -> `agent_processor`
9.  **Парсинг Ответа:** `agent_processor` -> `result_parser`
10. **Отправка Пользователю:** `agent_processor` (или инструмент `communication_tools`) -> `aiogram` -> Telegram API
11. **Сохранение Истории:** `agent_processor` -> `history_manager` -> `database`

## 5. Обработка Ошибок

*   Основной обработчик ошибок находится в `telegram_interface/handlers/error_handler.py`.
*   Он ловит исключения, возникающие во время обработки обновлений `aiogram`.
*   Логирует подробную информацию об ошибке (включая traceback).
*   Отправляет уведомление администраторам (ID которых указаны в `ADMIN_IDS`) с деталями ошибки.
*   Конкретные модули (например, `ai_interaction`, `database`, `tools`) также выполняют свое логирование ошибок.

## 6. Масштабируемость и Улучшения

*   **База данных:** SQLite может стать узким местом при очень высокой нагрузке. Возможен переход на PostgreSQL + `asyncpg`.
*   **FSM Хранилище:** `MemoryStorage` не подходит для работы бота на нескольких инстансах. Требуется переход на `RedisStorage` или другое персистентное хранилище.
*   **Контекст AI:** Текущая стратегия добавления RAG и заметок может переполнять контекстное окно. Рассмотреть суммирование или **векторный поиск** по логам/заметкам для предоставления только релевантной информации.
*   **Тестирование:** Критически важно добавить автоматические тесты (unit, integration).
*   **Мониторинг:** Внедрить более продвинутый мониторинг производительности и ошибок.
*   **Обработка Ошибок:** Сделать обработку ошибок более гранулярной, возможно, с разными ответами пользователю в зависимости от типа ошибки.

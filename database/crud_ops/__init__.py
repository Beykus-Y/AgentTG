# database/crud_ops/__init__.py

# Импортируем основные функции для удобного доступа
# Например: from database.crud_ops import history, profiles, ...

from .history import (
    add_message_to_history,
    get_chat_history,
    clear_chat_history
)
from .profiles import (
    upsert_user_profile,
    get_user_profile,
    update_avatar_description,
    find_user_id_by_profile
)
from .notes import (
    upsert_user_note,
    get_user_notes,
    delete_user_note,
    delete_user_note_nested,
    get_user_data_combined
)
from .settings import (
    upsert_chat_settings,
    get_chat_settings,
    delete_chat_settings,
    AI_MODE_PRO, # Экспортируем константы
    AI_MODE_DEFAULT
)
from .news import (
    add_or_update_subscription,
    get_subscription,
    get_all_subscriptions,
    update_subscription_last_post,
    delete_subscription,
    add_sent_guid,
    is_guid_sent,
    load_recent_sent_guids,
    cleanup_old_guids
)
from .stats import (
    increment_message_count,
    get_chat_stats_top_users,
    get_user_warn_count,
    add_user_warning,
    remove_user_warning,
    get_chat_warnings,
    reset_user_warnings
)

# Импорт из нового модуля логов
from .execution_logs import (
    add_tool_execution_log,
    get_recent_tool_executions
)

# Можно определить __all__ для явного экспорта
__all__ = [
    # history
    "add_message_to_history", "get_chat_history", "clear_chat_history",
    # profiles
    "upsert_user_profile", "get_user_profile", "update_avatar_description", "find_user_id_by_profile",
    # notes
    "upsert_user_note", "get_user_notes", "delete_user_note", "delete_user_note_nested", "get_user_data_combined",
    # settings
    "upsert_chat_settings", "get_chat_settings", "delete_chat_settings", "AI_MODE_PRO", "AI_MODE_DEFAULT",
    # news
    "add_or_update_subscription", "get_subscription", "get_all_subscriptions", "update_subscription_last_post",
    "delete_subscription", "add_sent_guid", "is_guid_sent", "load_recent_sent_guids", "cleanup_old_guids",
    # stats
    "increment_message_count", "get_chat_stats_top_users", "get_user_warn_count", "add_user_warning",
    "remove_user_warning", "get_chat_warnings", "reset_user_warnings",
    # execution_logs
    "add_tool_execution_log", "get_recent_tool_executions"
]
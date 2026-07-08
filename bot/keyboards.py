"""
Inline keyboards for source management.
"""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def source_management_keyboard(source_id: int) -> InlineKeyboardMarkup:
    """Keyboard for managing a single source."""
    keyboard = [
        [
            InlineKeyboardButton("🗑 Delete", callback_data=f"del_src:{source_id}"),
            InlineKeyboardButton("🧪 Re-test", callback_data=f"test_src:{source_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def stream_management_keyboard(stream_id: int) -> InlineKeyboardMarkup:
    """Keyboard for managing a stream."""
    keyboard = [
        [
            InlineKeyboardButton("📰 Sources", callback_data=f"src_stream:{stream_id}"),
            InlineKeyboardButton("🔬 Re-research", callback_data=f"research:{stream_id}"),
        ],
        [
            InlineKeyboardButton("📄 Latest Articles", callback_data=f"articles:{stream_id}"),
            InlineKeyboardButton("▶️ Run Pipeline", callback_data=f"pipeline:{stream_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def confirm_keyboard(action: str, item_id: int) -> InlineKeyboardMarkup:
    """Yes/No confirmation keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("✅ Yes", callback_data=f"confirm_{action}:{item_id}"),
            InlineKeyboardButton("❌ No", callback_data=f"cancel_{action}:{item_id}"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Main menu keyboard."""
    keyboard = [
        [
            InlineKeyboardButton("➕ New Stream", callback_data="newstream"),
            InlineKeyboardButton("📋 My Streams", callback_data="streams"),
        ],
        [
            InlineKeyboardButton("🗃️ All Sources", callback_data="sources_all"),
            InlineKeyboardButton("📰 Latest News", callback_data="latest"),
        ],
    ]
    return InlineKeyboardMarkup(keyboard)
from telegram import InlineKeyboardMarkup, InlineKeyboardButton, Message

from emojis import Emoji
from config import config


def secret_santa(chat_id: int, bot_username: str, participants_count: int = 0):
    # knowing the message id is not really needed because a caht can only have one ongoing secret chat
    deeplink_url = f"https://t.me/{bot_username}?start={chat_id}"
    keyboard = [
        [InlineKeyboardButton(f"{Emoji.LIST} Me apunto", url=deeplink_url)],
        [InlineKeyboardButton(f"{Emoji.CROSS} Cancelar", callback_data=f"cancel")],
    ]

    if participants_count:
        unsubscribe_button = InlineKeyboardButton(f"{Emoji.FREEZE} Salir", callback_data=f"leave")
        keyboard[0].append(unsubscribe_button)

    if participants_count >= config.santa.min_participants:
        start_button = InlineKeyboardButton(f"{Emoji.SANTA} Iniciar sorteo", callback_data=f"match")
        keyboard[1].append(start_button)

    return InlineKeyboardMarkup(keyboard)


def joined_message(chat_id: int):
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(f"{Emoji.FREEZE} Salir", callback_data=f"private:leave:{chat_id}"),
            InlineKeyboardButton(f"{Emoji.LIST} Actualiza tu nombre", callback_data=f"private:updatename:{chat_id}")
        ]]
    )


def revoke():
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{Emoji.CROSS} revoke", callback_data=f"revoke")]])


def new_santa():
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"{Emoji.TREE} Nuevo sorteo", callback_data=f"newsanta")]])


import datetime
import itertools
import json
import logging
import logging.config
import os
import random
import re
import threading
import time
from functools import wraps
from pathlib import Path
from random import choice
from typing import List, Callable, Optional, Union

from telegram import Update, TelegramError, Chat, ParseMode, Bot, BotCommandScopeAllPrivateChats, BotCommand, User, \
    BotCommandScopeAllChatAdministrators, ChatAction, ChatMemberLeft, ChatMemberUpdated, ChatMemberMember, \
    BotCommandScopeChatAdministrators, ChatMember
from telegram.error import BadRequest
from telegram.ext import Updater, CallbackContext, Filters, MessageHandler, CallbackQueryHandler, MessageFilter, \
    CommandHandler, ExtBot, Defaults, ChatMemberHandler
from telegram.utils.request import Request

import keyboards
import utilities
from emojis import Emoji
from santa import SecretSanta
from santa import NAME_MAX_LENGTH
from mwt import MWT
from config import config

ACTIVE_SECRET_SANTA_KEY = "active_secret_santa"
MUTED_KEY = "muted"
REMOVED_KEY = "removed"
BLOCKED_KEY = "blocked"
RECENTLY_LEFT_KEY = "recently_left"
RECENTLY_STARTED_SANTAS_KEY = "recently_closed_santas"

EMPTY_SECRET_SANTA_STR = f'{Emoji.SANTA}{Emoji.TREE} Nadie se ha apuntado al Amigo Invisible 2022 todavía. Pulsa el botón "<b>Me apunto</b>" de abajo para apuntarte.'


class Time:
    WEEK_4 = 60 * 60 * 24 * 7 * 4
    WEEK_2 = 60 * 60 * 24 * 7 * 2
    WEEK_1 = 60 * 60 * 24 * 7
    DAY_3 = 60 * 60 * 24 * 3
    DAY_1 = 60 * 60 * 24
    HOUR_48 = 60 * 60 * 48
    HOUR_12 = 60 * 60 * 12
    HOUR_6 = 60 * 60 * 6
    HOUR_1 = 60 * 60
    MINUTE_30 = 60 * 30
    MINUTE_1 = 60


class Error:
    SEND_MESSAGE_DISABLED = "no tiene permiso para enviar un mensaje"
    REMOVED_FROM_GROUP = "El bot fue expulsado por"  # it might continue with "group chat" or "supergroup chat"
    CANT_EDIT = "chat_write_forbidden"  # we receive this when we try to edit a message/answer a callback query but we are muted
    MESSAGE_TO_EDIT_NOT_FOUND = "mensaje a editar no encontrado"
    USER_BLOCKED_BOT = "El bot fue bloqueado por el usuario"


class Commands:
    PRIVATE = [BotCommand("help", "welcome message")]
    GROUP_ADMINISTRATORS = [
        BotCommand("newsanta", "create a new Secret Santa in this chat"),
        BotCommand("cancel", "cancel any ongoing Secret Santa"),
        BotCommand("hidecommands", "hide these commands"),
    ]


updater = Updater(
    bot=ExtBot(
        token=config.telegram.token,
        defaults=Defaults(parse_mode=ParseMode.HTML, disable_web_page_preview=True),
        # https://github.com/python-telegram-bot/python-telegram-bot/blob/8531a7a40c322e3b06eb943325e819b37ee542e7/telegram/ext/updater.py#L267
        request=Request(con_pool_size=config.telegram.get('workers', 1) + 4)
    ),
    workers=0,
    persistence=utilities.persistence_object()
)

BOT_LINK = f"https://t.me/{updater.bot.username}"


class NewGroup(MessageFilter):
    def filter(self, message):
        if message.new_chat_members:
            member: User
            for member in message.new_chat_members:
                if member.id == updater.bot.id:
                    return True


def load_logging_config(file_name='logging.json'):
    with open(file_name, 'r') as f:
        logging_config = json.load(f)

    logging.config.dictConfig(logging_config)


load_logging_config("logging.json")

logger = logging.getLogger(__name__)


@MWT(timeout=60 * 60)
def get_admin_ids(bot: Bot, chat_id: int):
    return [admin.user.id for admin in bot.get_chat_administrators(chat_id)]


def administrators(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id not in get_admin_ids(context.bot, update.effective_chat.id):
            logger.debug("admin check failed for callback <%s>", func.__name__)
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def superadmin(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id not in config.telegram.admins:
            logger.debug("superadmin check failed for callback <%s>", func.__name__)
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def users(func):
    @wraps(func)
    def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        if update.effective_user.id in get_admin_ids(context.bot, update.effective_chat.id):
            logger.debug("user check failed")
            return

        return func(update, context, *args, **kwargs)

    return wrapped


def bot_restricted_check():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            if MUTED_KEY in context.chat_data:
                logger.info("received an update from chat %d, but we are muted", update.effective_chat.id)
                return

            if REMOVED_KEY in context.chat_data:
                logger.info("received an update from chat %d, but we have been removed", update.effective_chat.id)
                return

            try:
                return func(update, context, *args, **kwargs)
            except (TelegramError, BadRequest) as e:
                error_str = str(e).lower()
                if Error.REMOVED_FROM_GROUP in error_str:
                    # we shouldn't receive these ever since we handle my_chat_member updates
                    logger.info("removed from chat chat %d: cleaning up", update.effective_chat.id)
                    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)
                elif Error.SEND_MESSAGE_DISABLED in error_str or Error.CANT_EDIT in error_str:
                    logger.info("can't send messages in chat %d: marking as muted", update.effective_chat.id)
                    context.chat_data[MUTED_KEY] = True

                    # can't edit messages if muted
                    # cancel_because_cant_send_messages(context, santa)
                else:
                    raise e

        return wrapped
    return real_decorator


def fail_with_message(answer_to_message=True):
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            try:
                return func(update, context, *args, **kwargs)
            except Exception as e:
                error_str = str(e)
                logger.error('error while running callback: %s', error_str, exc_info=True)

                error_str_message = f"Error during callback <code>{func.__name__}()</code> execution: <code>{utilities.escape(error_str)}</code>"
                if answer_to_message and update.message:
                    update.message.reply_html(error_str_message)
                elif answer_to_message and update.callback_query:
                    update.effective_message.reply_html(error_str_message)

                if config.telegram.log_chat:
                    context.bot.send_message(config.telegram.log_chat, f"#{context.bot.username} {error_str_message}")

        return wrapped
    return real_decorator


def fail_with_message_job(func):
    @wraps(func)
    def wrapped(context: CallbackContext, *args, **kwargs):
        try:
            return func(context, *args, **kwargs)
        except Exception as e:
            error_str = str(e)
            logger.error('error while running job: %s', error_str, exc_info=True)

            error_str_message = f"Error during job callback <code>{func.__name__}()</code> execution: <code>{utilities.escape(error_str)}</code>"
            if config.telegram.log_chat:
                context.bot.send_message(config.telegram.log_chat, f"#{context.bot.username} {error_str_message}")

    return wrapped


def get_secret_santa():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):

            if ACTIVE_SECRET_SANTA_KEY not in context.chat_data:
                santa = None
            else:
                santa = SecretSanta.from_dict(context.chat_data[ACTIVE_SECRET_SANTA_KEY])

            result_santa = func(update, context, santa, *args, **kwargs)
            if result_santa and isinstance(result_santa, SecretSanta):
                context.chat_data[ACTIVE_SECRET_SANTA_KEY] = result_santa.dict()

        return wrapped
    return real_decorator


def gen_participants_list(participants: dict, join_by: Optional[str] = None):
    participants_list = []
    i = 1
    for participant_id, participant in participants.items():
        string = f'<b>{i}</b>. {utilities.mention_escaped_by_id(participant_id, participant["name"])}'
        participants_list.append(string)
        i += 1

    if isinstance(join_by, str):
        return join_by.join(participants_list)

    return participants_list


def cancel_because_cant_send_messages(context: CallbackContext, santa: SecretSanta):
    text = "<i>Este Amigo Invisible fue cancelado porque no puedo enviar mensajes en este grupo</i>"
    if santa.get_participants_count():
        participants_list = gen_participants_list(santa.participants, join_by="\n")
        text = f"{text}\nParticipantes:\n\n{participants_list}"

    return context.bot.edit_message_text(
        chat_id=santa.chat_id,
        message_id=santa.santa_message_id,
        text=text,
        reply_markup=None
    )


def update_secret_santa_message(context: CallbackContext, santa: SecretSanta):
    participants_count = santa.get_participants_count()
    if not participants_count:
        text = EMPTY_SECRET_SANTA_STR
        reply_markup = keyboards.secret_santa(
            santa.chat_id,
            context.bot.username,
            participants_count=participants_count
        )
    elif santa.started:
        participants_list = gen_participants_list(santa.participants)

        base_text = '{santa} Este Amigo Invisible ha sido arrancado y todos ' \
                    '<a href="{bot_link}">han recibido su amigo invisible</a>!\n' \
                    'Lista de participantes:\n\n' \
                    '{participants}'

        text = base_text.format(
            santa=Emoji.SANTA,
            bot_link=BOT_LINK,
            participants="\n".join(participants_list),
            creator=santa.creator_name_escaped,
        )
        reply_markup = None
    else:
        participants_list = gen_participants_list(santa.participants)

        min_participants_text = ""
        if santa.get_missing_count() > 0:
            min_participants_text = f". Hacen falta <b>{santa.get_missing_count()}</b> participantes más para poder empezar el sorteo"

        base_text = '{santa} ¡Muy bien! ¡Un nuevo participante!\nLista de participantes:\n\n{participants}\n\n' \
                    'Para participar, utiliza el botón "<b>Me apunto</b>" de abajo y luego pulsa "<b>iniciar </b>".\n' \
                    'Solo {creator} puede iniciar este sorteo de Amigo Invisible. {min_participants}'

        text = base_text.format(
            santa=Emoji.SANTA,
            participants="\n".join(participants_list),
            creator=santa.creator_name_escaped,
            min_participants=min_participants_text
        )

        reply_markup = keyboards.secret_santa(
            santa.chat_id,
            context.bot.username,
            participants_count=participants_count
        )

    try:
        edited_message = context.bot.edit_message_text(
            chat_id=santa.chat_id,
            message_id=santa.santa_message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )
    except (BadRequest, TelegramError) as e:
        logger.error("exception while editing secret santa message (%d, %d): %s", santa.chat_id, santa.santa_message_id, str(e))
        return

    return edited_message


def create_new_secret_santa(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    if santa:
        text_message_exists = f"👆 Ya hay un sorteo <a href=\"{santa.link()}\">activo del Amigo Invisible</a> dentro " \
                              f"de este chat! " \
                              f"Puedes preguntar {santa.creator_name_escaped} para cancelar el sorteo utilizando el botón " \
                              f"del mensaje"
        try:
            context.bot.send_message(
                update.effective_chat.id,
                text_message_exists,
                reply_to_message_id=santa.santa_message_id,
                allow_sending_without_reply=False
            )
        except (TelegramError, BadRequest) as e:
            if str(e).lower() != "replied message not found":
                raise e

            update.message.reply_html(f"{Emoji.SANTA} Ya hay un sorteo del Amigo Invisble activo"
                                      f" en este chat. Puedes preguntar a {santa.creator_name_escaped} "
                                      f"(o a un administrador) para que lo cancele utilizando <code>/cancel</code>")

        return

    new_secret_santa = SecretSanta(
        origin_message_id=update.effective_message.message_id,
        user_id=update.effective_user.id,
        user_name=update.effective_user.first_name,
        chat_id=update.effective_chat.id,
        chat_title=update.effective_chat.title,
    )

    reply_markup = keyboards.secret_santa(update.effective_chat.id, context.bot.username)
    if update.callback_query:
        update.callback_query.edit_message_text(EMPTY_SECRET_SANTA_STR, reply_markup=reply_markup)
        santa_message_id = update.effective_message.message_id
    else:
        sent_message = update.message.reply_html(
            EMPTY_SECRET_SANTA_STR,
            reply_markup=reply_markup
        )
        santa_message_id = sent_message.message_id

    new_secret_santa.santa_message_id = santa_message_id

    return new_secret_santa


@fail_with_message()
@bot_restricted_check()
@get_secret_santa()
def on_new_secret_santa_command(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.info("/newsanta command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    return create_new_secret_santa(update, context, santa)


@fail_with_message()
@bot_restricted_check()
@get_secret_santa()
def on_new_secret_santa_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.info("new secret santa button: %d -> %d", update.effective_user.id, update.effective_chat.id)

    return create_new_secret_santa(update, context, santa)


def find_key(dispatcher_user_data: dict, target_chat_id: int, key_to_find: Union[int, str]) -> bool:
    for chat_data_chat_id, chat_data in dispatcher_user_data.items():
        if chat_data_chat_id != target_chat_id:
            continue

        return key_to_find in chat_data


def find_santa(dispatcher_chat_data: dict, santa_chat_id: int):
    for chat_data_chat_id, chat_data in dispatcher_chat_data.items():
        if chat_data_chat_id != santa_chat_id:
            continue

        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            logger.debug("chat_data for chat %d exists, but there is no active secret santa", santa_chat_id)
            return

        santa_dict = chat_data[ACTIVE_SECRET_SANTA_KEY]
        return SecretSanta.from_dict(santa_dict)


@fail_with_message()
def on_join_deeplink(update: Update, context: CallbackContext):
    santa_chat_id = int(context.matches[0].group(1))
    logger.info("join deeplink from %d, chat id: %d", update.effective_user.id, santa_chat_id)

    if find_key(context.dispatcher.chat_data, santa_chat_id, MUTED_KEY):
        update.message.reply_html(f"Parece que no puedo enviar mensajes en este grupo. No puedo "
                                  f"dejar a nuevos participantes unirse, hasta poder enviar mensajes. Lo siento {Emoji.SAD}")
        return

    santa = find_santa(context.dispatcher.chat_data, santa_chat_id)
    if not santa:
        # this might happen if the bot was removed from the group: the "join" button is still there
        # we should check if the chat is in the recently left chats in context.bot_data
        if RECENTLY_LEFT_KEY in context.bot_data and santa_chat_id in context.bot_data[RECENTLY_LEFT_KEY]:
            logger.debug(f"no active santa in {santa_chat_id} and the chat appears among the recently left chats")
            update.message.reply_html(f"Parece que me han eliminado de este grupo {Emoji.SAD}")
        else:
            # raise ValueError(f"user tried to join, but no secret santa is active in {santa_chat_id}")

            # it might happen that the bot is removed from the group, and then added again (so the chat_id
            # doesn't appear in the recently left groups), and an user uses the old "join" button from an
            # old secret santa

            logger.debug(f"no active santa in {santa_chat_id}")
            update.message.reply_html(f"Parece que no hay ningún Amigo Invisible activo en este grupo {Emoji.SAD} "
                                      f"Probablemente has utilizado el botón \"<b>Me apunto</b>\" de un antiguo o inactivo Amigo Invisible")
        return

    if config.santa.max_participants and santa.get_participants_count() >= config.santa.max_participants:
        text = f"Lo siento, lamentablemente {santa.inline_link('este Amigo Invisible')} ha llegado a su " \
               f"número máximo de participantes {Emoji.SAD}"
        update.message.reply_html(text)
        return

    santa.add(update.effective_user)
    context.dispatcher.chat_data[santa_chat_id][ACTIVE_SECRET_SANTA_KEY] = santa.dict()

    if santa.creator_id == update.effective_user.id:
        wait_for_start_text = f"\nPuedes iniciar el sorteo en cualquier momento con el botón \"<b>Iniciar sorteo</b>\" en el grupo, " \
                              f"cuando se hayan unido por lo menos {config.santa.min_participants} participantes"
    else:
        wait_for_start_text = f"Ahora espera a {santa.creator_name_escaped} para iniciar el sorteo"

    reply_markup = keyboards.joined_message(santa_chat_id)
    sent_message = update.message.reply_html(
        f"{Emoji.TREE} Te has unido al {santa.chat_title_escaped} {santa.inline_link('Amigo Invisible')}!\n"
        f"{wait_for_start_text}. Vas a recibir tu amigo invisble aquí, en este chat",
        reply_markup=reply_markup
    )

    santa.set_user_join_message_id(update.effective_user, sent_message.message_id)

    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_leave_button_group(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("leave button in group: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not santa.is_participant(update.effective_user):
        update.callback_query.answer(f"{Emoji.FREEZE} ¡No te has unido a este Amigo Invisible!", show_alert=True)
        return

    # we need this for later
    last_join_message_id = santa.get_user_join_message_id(update.effective_user)

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    update.callback_query.answer(f"Has sido retirado de este sorteo de Amigo Invisible")

    logger.debug("removing keyboard from last join message in private...")
    context.bot.edit_message_reply_markup(update.effective_user.id, last_join_message_id, reply_markup=None)

    return santa


def save_recently_started_santa(bot_data: dict, santa: SecretSanta):
    chat_id = santa.chat_id

    if RECENTLY_STARTED_SANTAS_KEY not in bot_data:
        bot_data[RECENTLY_STARTED_SANTAS_KEY] = {}
    if chat_id not in bot_data[RECENTLY_STARTED_SANTAS_KEY]:
        bot_data[RECENTLY_STARTED_SANTAS_KEY][chat_id] = {}

    bot_data[RECENTLY_STARTED_SANTAS_KEY][chat_id][santa.santa_message_id] = santa.dict()


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_match_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("start match button: %d -> %d", update.effective_user.id, update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Solo {santa.creator_name} puede utilizar este botón e iniciar el sorteo del Amigo Invisible",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    # we answer to the callback query so the user doesn't (hopefully) keep on tapping on the button
    # while the matches are generated
    update.callback_query.answer(f'{Emoji.HOURGLASS} Sorteando amigos invisibles...', cache_time=5)

    sent_message = update.effective_message.reply_html(f'{Emoji.HOURGLASS} <i>Sorteando participantes...</i>')

    blocked_by = []
    for user_id, user_data in santa.participants.items():
        try:
            context.bot.send_chat_action(user_id, ChatAction.TYPING)
        except (TelegramError, BadRequest) as e:
            if Error.USER_BLOCKED_BOT in str(e).lower():
                logger.debug("%d blocked the bot", user_id)
            else:
                # what to do?
                logger.warning("can't send chat action to %d: %s", user_id, str(e))

            blocked_by.append(utilities.mention_escaped_by_id(user_id, user_data["name"]))

    if blocked_by:
        users_list = ", ".join(blocked_by)
        text = f"No puedo iniciar el sorteo porque algún usuario de ({users_list}) ma ha bloqueado {Emoji.SAD}\n" \
               f"Deben desbloquearme para poder mandarles su amigo invisible"
        sent_message.edit_text(text)
        return

    matches = []
    max_attempts = 12
    failed_attempts = 0
    while failed_attempts < max_attempts:
        try:
            matches = utilities.draft(list(santa.participants.keys()))
            break
        except (utilities.TooManyInvalidPicks, utilities.StuckOnLastItem) as e:
            failed_attempts += 1
            logger.warning("drafting pairs error: %s (failed attempt %d/%d)", str(e), failed_attempts, max_attempts)

    if not matches:
        logger.error("match list still empty (failed attempts: %d/%d)", failed_attempts, max_attempts)

        utilities.log_tg(context.bot, f"#drafting_error while generating pairs for chat {update.effective_chat.id}")

        text = f"{Emoji.WARN} <i>{update.effective_user.mention_html()}, " \
               f"Algo ha salido mal durante el sorteo. Por favor, inténtalo de nuevo</i>"
        sent_message.edit_text(text)
        return

    logger.debug("gathered pairs matches, failed attempts: %d", failed_attempts)

    for receiver_id, match_id in matches:
        match_name = santa.get_user_name(match_id)
        match_mention = utilities.mention_escaped_by_id(match_id, match_name)

        text = f"{Emoji.SANTA}{Emoji.PRESENT} Eres el <a href=\"{santa.link()}\">Amigo Invisible</a> de {match_mention}"

        match_message = context.bot.send_message(receiver_id, text)
        santa.set_user_match_message_id(receiver_id, match_message.message_id)

    santa.start()  # doesn't do anything beside populating some datetimes

    logger.debug("removing active secret santa from chat_data and saving a copy in bot_data...")
    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    save_recently_started_santa(context.bot_data, santa)

    text = f"Todos los participantes han recibido su amigo invisible en su chat privado con el <a href=\"{BOT_LINK}\">bot del Amigo Invisible</a>"
    sent_message.edit_text(text)

    santa.start()
    update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_cancel_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("cancel button: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not santa:
        # scenarios where this might happen: the bot is removed from the chat, then added back, and the
        # user keeps using an old secret santa message's buttons
        logger.warning("cancel button, but there is no active secret chanta in the chat")
        update.callback_query.edit_message_text("<i>Este Amigo Invisible ya no está activo</i>", reply_markup=None)
        utilities.log_tg(context.bot, "cancel button used, but no active secret santa: check logs (especially whether "
                                      "we have been previously removed from the chat or not)!")
        return

    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Solo {santa.creator_name} puede utilizar este botón. Administradores pueden utilizar /cancel "
            f"para cancelar el sorteo actual",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    text = "<i>Este sorte de Amigo Invisble ha sido cancelado por su creador</i>"
    update.callback_query.edit_message_text(text, reply_markup=None)


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_revoke_button(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("revoke button: %d -> %d", update.effective_user.id, update.effective_chat.id)
    if santa.creator_id != update.effective_user.id:
        update.callback_query.answer(
            f"{Emoji.CROSS} Solo {santa.creator_name} puede utilizar este botón",
            show_alert=True,
            cache_time=Time.DAY_3
        )
        return

    return update.callback_query.answer(
        f"{Emoji.WARN} La posibilidad de revocar amigos invisibles ya enviados ha sido suspendido temporalmente",
        show_alert=True,
        cache_time=Time.DAY_1
    )


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
def on_hide_commands_command(update: Update, context: CallbackContext):
    logger.debug("/hidecommands command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    context.bot.set_my_commands(
        commands=[],
        scope=BotCommandScopeChatAdministrators(chat_id=update.effective_chat.id)
    )
    update.message.reply_html("Hecho. Puede tardar un rato hasta que desaparezcan. "
                              "Puedes utilizar <code>/showcommands</code> si quieres que los admins de este grupo lo"
                              "vuelvan a ver")


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
def on_show_commands_command(update: Update, context: CallbackContext):
    logger.debug("/showcommands command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    context.bot.set_my_commands(
        commands=Commands.GROUP_ADMINISTRATORS,
        scope=BotCommandScopeChatAdministrators(chat_id=update.effective_chat.id)
    )
    update.message.reply_html("Hecho. Puede tardar un rato hasta que aparezcan")


@fail_with_message(answer_to_message=False)
@bot_restricted_check()
@get_secret_santa()
def on_cancel_command(update: Update, context: CallbackContext, santa: Optional[SecretSanta] = None):
    logger.debug("/cancel command: %d -> %d", update.effective_user.id, update.effective_chat.id)

    if not santa:
        update.message.reply_html("<i>No hay ningún sorte de Amigo Invisible activo</i>")
        return

    user_id = update.effective_user.id
    if not santa.creator_id != user_id and user_id not in get_admin_ids(context.bot, update.effective_chat.id):
        logger.debug("user is not admin nor the creator of the secret santa")
        return

    context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    try:
        context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=santa.santa_message_id,
            text="<i>Este sorte de Amigo Invisble ha sido cancelado por su creador o un administrador</i>",
            reply_markup=None
        )
    except (TelegramError, BadRequest) as e:
        logger.warning("error while editing canceled secret santa message: %s", str(e))
        if Error.MESSAGE_TO_EDIT_NOT_FOUND not in str(e).lower():
            raise e

    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="<i>El Amigo Invisible de este chat ha sido cancelado</i>",
        reply_to_message_id=santa.santa_message_id,
        allow_sending_without_reply=True,  # send the message anyway even if the secret santa message has been deleted
    )


def private_chat_button():
    def real_decorator(func):
        @wraps(func)
        def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
            santa_chat_id = int(context.matches[0].group(1))
            logger.debug("private chat button, chat_id: %d", santa_chat_id)

            santa = find_santa(context.dispatcher.chat_data, santa_chat_id)
            if not santa:
                # we do not edit or delete this message when a Secrt Santa is started, so the buttons are still there
                logger.debug("user tapped on a private chat button, but there is no active secret santa for that chat")
                update.callback_query.answer(f"El Amigo Invisible de este chat ya no es válido", show_alert=True)
                update.callback_query.edit_message_reply_markup(reply_markup=None)
                return

            if not santa.is_participant(update.effective_user):
                # maybe the user left from the group's message
                update.callback_query.answer(f"{Emoji.FREEZE} Tú no estás participando en este Amigo Invisible!",
                                             show_alert=True)
                update.callback_query.edit_message_reply_markup(reply_markup=None)
                return

            return func(update, context, santa, *args, **kwargs)

        return wrapped
    return real_decorator


@fail_with_message(answer_to_message=True)
@private_chat_button()
def on_update_name_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("update name button in private: %d (santa chat id: %d)", update.effective_user.id, santa.chat_id)

    name = update.effective_user.first_name[:NAME_MAX_LENGTH]
    name_updated = False

    if name != santa.get_user_name(update.effective_user):
        santa.set_user_name(update.effective_user, name)
        name_updated = True

    update.callback_query.answer(f"Tu nombre ha sido actualizado: {name}\n\nEsta opción te permite cambiar tu "
                                 f"nombre de Telegram y atualizarlo en la lista (útil en el caso de tener participantes "
                                 f"con nombres similares)", show_alert=True)

    if name_updated:
        update_secret_santa_message(context, santa)


@fail_with_message(answer_to_message=True)
@private_chat_button()
def on_leave_button_private(update: Update, context: CallbackContext, santa: SecretSanta):
    logger.debug("leave button in private: %d (santa chat id: %d)", update.effective_user.id, santa.chat_id)

    santa.remove(update.effective_user)
    update_secret_santa_message(context, santa)

    text = f"{Emoji.FREEZE} Ha sido eliminado del <a href=\"{santa.link()}\">Amigo Invisible</a> " \
           f"del chat {santa.chat_title_escaped}'s"
    update.callback_query.edit_message_text(text, reply_markup=None)
    # update.callback_query.answer(f"You have been removed from this Secret Santa")

    return santa


@fail_with_message(answer_to_message=False)
def on_supergroup_migration(update: Update, context: CallbackContext):
    # we receive two updates when a migration happens: one with migrate_from_chat_id, and one with migrate_to_chat_id
    # we process only the one with migrate_to_chat_id, because effective_chat.id for this
    # update is the old chat id, therefore its chat_data contains the populated chat data
    if not update.message.migrate_to_chat_id:
        return

    logger.info(f"supergroup migration: {update.effective_chat.id} -> {update.message.migrate_to_chat_id}")

    old_chat_id = update.effective_chat.id
    new_chat_id = update.message.migrate_to_chat_id

    if ACTIVE_SECRET_SANTA_KEY not in context.chat_data:
        return

    logger.debug("old chat_id %d has an ongoing secret santa", old_chat_id)

    santa_dict = context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY)
    old_santa = SecretSanta.from_dict(santa_dict)

    # the api doesn't allow to delete the old santa message because the old group is no longer available

    new_secret_santa = SecretSanta(
        origin_message_id=update.effective_message.message_id,
        user_id=old_santa.creator_id,
        user_name=old_santa.creator_name,
        chat_id=new_chat_id,
        chat_title=update.effective_chat.title,
        participants=old_santa.participants
    )

    logger.debug("sending new message...")
    reply_markup = keyboards.secret_santa(new_chat_id, context.bot.username)
    sent_message = context.bot.send_message(new_chat_id, EMPTY_SECRET_SANTA_STR, reply_markup=reply_markup)
    new_secret_santa.santa_message_id = sent_message.message_id

    logger.debug("saving new chat_data for new supergroup %d...", new_chat_id)
    context.dispatcher.chat_data[new_chat_id] = {ACTIVE_SECRET_SANTA_KEY: new_secret_santa.dict()}

    # we need to update it as soon as we send it because there might be existing participants to list
    logger.debug("editing new message...")
    update_secret_santa_message(context, new_secret_santa)


@fail_with_message(answer_to_message=False)
def on_new_group_chat(update: Update, context: CallbackContext):
    logger.info("new group chat: %d", update.effective_chat.id)

    if config.telegram.exit_unknown_groups and update.effective_user.id not in config.telegram.admins:
        logger.info("unauthorized: leaving...")
        update.effective_chat.leave()
        return

    # always pop this key
    context.chat_data.pop(REMOVED_KEY, None)

    if RECENTLY_LEFT_KEY in context.bot_data:
        logger.debug("removing group from recently left groups list...")
        context.bot_data[RECENTLY_LEFT_KEY].pop(update.effective_chat.id, None)

    if not config.santa.start_button_on_new_group:
        return

    text = f"¡Hola a todos! Soy el bot que ayuda a los chats de grupo a organizar su" \
           f"Amigo Invisible {Emoji.SANTA}{Emoji.SHH}\n" \
           f"Cualquier miembro del grupo puede iniciar un nuevo sorteo con el botón de abajo." \
           f"O se puede utilizar el comando <code>/newsanta</code>"

    update.message.reply_html(
        text,
        reply_markup=keyboards.new_santa(),
        quote=False,
    )


@fail_with_message()
def on_help(update: Update, _):
    logger.info("/start or /help from: %s (text: %s)", update.effective_user.id, update.message.text)

    source_code = "https://github.com/tprumbs/tg-amigo-invisible-bot"
    text = f"¡Hola {utilities.html_escape(update.effective_user.first_name)}!" \
           f"\nPuedo ayudarte a orginzar un sorteo de Amigo Invisible 🤫🎅🏼🎁 en tu chat de grupo :)\n" \
           f"Añademe a un chat y utiliza <code>/newsanta</code> para iniciar un sorteo de Amigo Invisible." \
           f"\n\nCódigo fuente <a href=\"{source_code}\">aquí</a>"

    update.message.reply_html(text)


@fail_with_message()
@superadmin
def admin_ongoing_command(update: Update, context: CallbackContext):
    logger.info("/ongoing from %d", update.effective_user.id)

    santa_count = 0
    participants_count = 0
    for chat_data_chat_id, chat_data in context.dispatcher.chat_data.items():
        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            continue

        santa_count += 1
        santa = SecretSanta.from_dict(chat_data[ACTIVE_SECRET_SANTA_KEY])
        participants_count += santa.get_participants_count()

    text = f"• Sorte de Amigo Invisible en curso: {santa_count} ({participants_count} participantes)"

    if RECENTLY_STARTED_SANTAS_KEY in context.bot_data:
        recently_started_chats_count = len(context.bot_data[RECENTLY_STARTED_SANTAS_KEY])
        recently_started_santas_count = 0
        for _, santas_data in context.bot_data[RECENTLY_STARTED_SANTAS_KEY].items():
            recently_started_santas_count += len(santas_data)

        text = f"{text}\n• Sorteo iniciados: {recently_started_santas_count} en " \
               f"{recently_started_chats_count} grupos"

    update.message.reply_html(text)


def allowed(permission: Optional[bool]):
    if permission is None:
        # None means it's enabled
        return True

    return permission


def was_muted(chat_member_update: ChatMemberUpdated):
    could_send_messages = allowed(chat_member_update.old_chat_member.can_send_messages)
    can_send_messages = allowed(chat_member_update.new_chat_member.can_send_messages)
    if could_send_messages and not can_send_messages:
        return True
    return False


def was_unmuted(chat_member_update: ChatMemberUpdated):
    could_send_messages = allowed(chat_member_update.old_chat_member.can_send_messages)
    can_send_messages = allowed(chat_member_update.new_chat_member.can_send_messages)
    if not could_send_messages and can_send_messages:
        return True
    return False


@fail_with_message(answer_to_message=False)
def on_my_chat_member_update(update: Update, context: CallbackContext):
    logger.debug("my_chat_member update in %d", update.my_chat_member.chat.id)
    my_chat_member = update.my_chat_member

    if my_chat_member.chat.id > 0:
        # status == ChatMember.LEFT -> bot was blocked
        # status == ChatMember.MEMBER-> bot was unblocked
        if my_chat_member.new_chat_member.status in (ChatMember.LEFT, ChatMember.KICKED):
            logger.debug("bot was blocked by %d (new chat_member status: %s)", my_chat_member.chat.id, my_chat_member.new_chat_member.status)
            context.user_data[BLOCKED_KEY] = True
        elif my_chat_member.new_chat_member.status == ChatMember.MEMBER:
            logger.debug("bot was unblocked by %d", my_chat_member.chat.id)
            context.user_data.pop(BLOCKED_KEY, None)
        else:
            logger.debug("no relevant change happened (private chat): %s", my_chat_member)

        return

    # from pprint import pprint
    # pprint(update.to_dict())

    if my_chat_member.new_chat_member.status == ChatMember.LEFT:
        # we receive this kind of update also when the group is deleted
        logger.debug("old_chat_member: %s", my_chat_member.old_chat_member)
        logger.debug("new_chat_member: %s", my_chat_member.new_chat_member)
        logger.info("bot removed from %d, removing chat_data...", my_chat_member.chat.id)
        context.chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)
        context.chat_data.pop(MUTED_KEY, None)

        now = utilities.now()

        # keep track that we have been removed from the chat
        context.chat_data[REMOVED_KEY] = now

        if RECENTLY_LEFT_KEY not in context.bot_data:
            context.bot_data[RECENTLY_LEFT_KEY] = {}
        context.bot_data[RECENTLY_LEFT_KEY][my_chat_member.chat.id] = now
    elif was_muted(my_chat_member):
        logger.debug("bot muted in %d", my_chat_member.chat.id)
        context.chat_data[MUTED_KEY] = True

        # muted -> can't edit messages either
        #
        # if ongoing_secret_santa:
        #     logger.debug("ongoing secret santa: editing message...")
        #     santa = SecretSanta.from_dict(ongoing_secret_santa)
        #     cancel_because_cant_send_messages(context, santa)
    elif was_unmuted(my_chat_member):
        logger.debug("bot unmuted in %d", my_chat_member.chat.id)
        context.chat_data.pop(MUTED_KEY, None)
    else:
        logger.debug("no relevant change happened (group chat): %s", my_chat_member)


def secret_santa_expired(context: CallbackContext, santa: SecretSanta):
    if not santa.started:
        text = f"<i>Este sorte de Amigo Invisible ha caducado (Han pasado {config.santa.timeout} días desde su inicio)</i>"
    else:
        participants_list = gen_participants_list(santa.participants)
        text = '{hourglass} El sorteo de Amigo Invisible ha sido parado. Lista de participantes:\n\n{participants}'.format(
            hourglass=Emoji.HOURGLASS,
            participants="\n".join(participants_list)
        )

    try:
        edited_message = context.bot.edit_message_text(
            chat_id=santa.chat_id,
            message_id=santa.santa_message_id,
            text=text,
            reply_markup=None
        )
    except (BadRequest, TelegramError) as e:
        logger.error("exception while closing secret santa message (%d, %d): %s", santa.chat_id, santa.santa_message_id, str(e))
        return

    return edited_message


@fail_with_message_job
def close_old_secret_santas(context: CallbackContext):
    logger.info("inactive secret santa job...")

    for chat_id, chat_data in context.dispatcher.chat_data.items():
        if ACTIVE_SECRET_SANTA_KEY not in chat_data:
            continue

        santa = SecretSanta.from_dict(chat_data[ACTIVE_SECRET_SANTA_KEY])

        now = utilities.now()
        diff_seconds = (now - santa.created_on).total_seconds()
        if diff_seconds <= config.santa.timeout * Time.DAY_1:
            continue

        if MUTED_KEY in chat_data:
            logger.info("can't edit chat %d's expired santa message: the bot is marked as muted", chat_id)
        else:
            secret_santa_expired(context, santa)

        logger.debug("popping secret santa from chat %d", chat_id)
        chat_data.pop(ACTIVE_SECRET_SANTA_KEY, None)

    logger.info("...cleanup job end")


@fail_with_message_job
def bot_data_cleanup(context: CallbackContext):
    logger.info("executing job...")

    if RECENTLY_LEFT_KEY in context.bot_data:
        logger.info("cleaning up %s...", RECENTLY_LEFT_KEY)

        chat_ids_to_pop = []
        for chat_id, left_dt in context.dispatcher.bot_data[RECENTLY_LEFT_KEY].items():
            now = utilities.now()
            diff_seconds = (now - left_dt).total_seconds()
            if diff_seconds <= Time.WEEK_4:
                continue

            chat_ids_to_pop.append(chat_id)

        logger.debug("%d chats to pop", len(chat_ids_to_pop))
        for chat_id in chat_ids_to_pop:
            logger.debug("popping chat %d from recently left chats dict", chat_id)
            context.dispatcher.bot_data[RECENTLY_LEFT_KEY].pop(chat_id, None)

    if RECENTLY_STARTED_SANTAS_KEY in context.bot_data:
        logger.info("cleaning up %s...", RECENTLY_STARTED_SANTAS_KEY)

        chat_ids_to_pop = []
        logger.debug("currently stored chats: %d", len(context.bot_data[RECENTLY_STARTED_SANTAS_KEY]))
        for chat_id, chat_santas in context.bot_data[RECENTLY_STARTED_SANTAS_KEY].items():
            santa_ids_to_pop = []
            for santa_message_id, santa_dict in chat_santas.items():
                santa = SecretSanta.from_dict(santa_dict)
                now = utilities.now()
                diff_seconds = (now - santa.started_on).total_seconds()
                if diff_seconds <= Time.WEEK_2:
                    continue

                santa_ids_to_pop.append(santa_message_id)

            logger.debug("%d santa_ids to pop", len(santa_ids_to_pop))
            for santa_id in santa_ids_to_pop:
                logger.debug("popping santa_id %d from chat_id %d", santa_id, chat_id)
                chat_santas.pop(santa_id, None)

            if not chat_santas:
                # the chat dict is now empty, we can remove it
                chat_ids_to_pop.append(chat_id)

        logger.debug("%d chat_ids to pop", len(chat_ids_to_pop))
        for chat_id in chat_ids_to_pop:
            logger.debug("popping chat_id %d because its dict is now empty", chat_id)
            context.bot_data[RECENTLY_STARTED_SANTAS_KEY].pop(chat_id, None)

    logger.info("...job execution end")


def main():
    dispatcher = updater.dispatcher

    dispatcher.add_handler(MessageHandler(NewGroup(), on_new_group_chat))
    dispatcher.add_handler(MessageHandler(Filters.status_update.migrate, on_supergroup_migration))

    dispatcher.add_handler(CommandHandler(["ongoing"], admin_ongoing_command, filters=Filters.chat_type.private))

    dispatcher.add_handler(MessageHandler(Filters.chat_type.private & Filters.regex(r"^/start (-?\d+)"), on_join_deeplink))
    dispatcher.add_handler(CommandHandler(["start", "help"], on_help, filters=Filters.chat_type.private))

    dispatcher.add_handler(CommandHandler(["new", "newsanta", "santa"], on_new_secret_santa_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["cancel"], on_cancel_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["hidecommands"], on_hide_commands_command, filters=Filters.chat_type.groups))
    dispatcher.add_handler(CommandHandler(["showcommands"], on_show_commands_command, filters=Filters.chat_type.groups))

    dispatcher.add_handler(CallbackQueryHandler(on_new_secret_santa_button, pattern=r'^newsanta$'))
    dispatcher.add_handler(CallbackQueryHandler(on_match_button, pattern=r'^match$'))
    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_group, pattern=r'^leave$'))
    dispatcher.add_handler(CallbackQueryHandler(on_cancel_button, pattern=r'^cancel$'))
    dispatcher.add_handler(CallbackQueryHandler(on_revoke_button, pattern=r'^revoke$'))

    dispatcher.add_handler(CallbackQueryHandler(on_leave_button_private, pattern=r'^private:leave:(-\d+)$'))
    dispatcher.add_handler(CallbackQueryHandler(on_update_name_button_private, pattern=r'^private:updatename:(-\d+)$'))

    dispatcher.add_handler(ChatMemberHandler(on_my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    updater.job_queue.run_repeating(close_old_secret_santas, interval=Time.HOUR_6, first=Time.MINUTE_30)
    updater.job_queue.run_repeating(bot_data_cleanup, interval=Time.DAY_1, first=Time.HOUR_6)

    updater.bot.set_my_commands([])  # make sure the bot doesn't have any command set...
    updater.bot.set_my_commands(  # ...then set the scope for private chats
        commands=Commands.PRIVATE,
        scope=BotCommandScopeAllPrivateChats()
    )
    updater.bot.set_my_commands(  # ...then set the scope for group administrators
        commands=Commands.GROUP_ADMINISTRATORS,
        scope=BotCommandScopeAllChatAdministrators()
    )

    allowed_updates = ["message", "callback_query", "my_chat_member"]  # https://core.telegram.org/bots/api#getupdates

    logger.info("running as @%s, allowed updates: %s", updater.bot.username, allowed_updates)
    updater.start_polling(drop_pending_updates=True, allowed_updates=allowed_updates)
    updater.idle()


if __name__ == '__main__':
    main()

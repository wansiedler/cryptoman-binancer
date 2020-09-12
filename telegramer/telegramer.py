# coding: utf-8

import json
import logging
import os
import threading
import time

import redis
from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from redis import Redis

import telegram
from telegram.bot import Request
from telegram.error import TimedOut
from telegram.ext import Updater, CommandHandler, Dispatcher, MessageHandler, Filters
from telegram.update import Update

from config import settings

TOKEN = os.getenv('TELEGRAM_TOKEN')

logger = logging.getLogger('connectors.telegramer')

redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_password = os.getenv('REDIS_PASSWORD', '')

_r: Redis = redis.client.StrictRedis(host=redis_host, password=redis_password)

dispatcher: Dispatcher = None
handlers = []


@csrf_exempt
def hook(request: WSGIRequest):
    if request.method != 'POST':
        return HttpResponse()
    _r.publish('cryptoman.tg_hook', str(request.body, encoding='utf-8'))
    return HttpResponse()


class TGHook(threading.Thread):
    def __init__(self, bot, updater):
        super().__init__()
        self.bot = bot
        self.updater = updater

    def run(self):
        logger.debug('run')
        p = _r.pubsub()
        p.subscribe('cryptoman.tg_hook')
        p.subscribe('cryptoman.message')
        while True:
            message = p.get_message(timeout=1)
            # l.debug('checking messages...')
            if not message or message['type'] != 'message':
                continue

            # parsed = json.loads(message['data'])
            # msg = f"{msg['text']}"
            # l.debug(u'Got message: %s' % json.dumps(parsed['message'], indent=4, sort_keys=True))
            if message['channel'] == b'cryptoman.tg_hook':
                json_string = message['data']
                update = Update.de_json(json.loads(json_string), self.bot)
                if self.updater and self.updater.running:
                    self.updater.update_queue.put(update)
            elif message['channel'] == b'cryptoman.message':
                json_string = message['data']
                mes = json.loads(json_string)
                try:
                    logger.debug('Sending to {}'.format(mes['recipient']))
                    self.bot.send_message(mes['recipient'], mes['message'])
                except TimedOut:
                    logger.debug('time out...')
                    time.sleep(5)


def start(update: telegram.Update, context):
    logger.debug('/start')
    from cryptoman.models import NotifyRecipients, Strategy

    chat_id = update.message.chat_id
    contact = update.message.from_user.id or update.message.chat.id
    username = update.message.from_user.username or update.message.chat.id
    args = context.args
    if len(args) == 0:
        update.message.reply_text(
            f'Hello, {username}.\n'
            f'Start with the /start command with a list of levels and strategies subscribe to. For example:'
        )
        update.message.reply_text(f'/start 0 1 2 3 4 5 6 7 g28bnb\n'
                                  '/start 0 1 2 3 4 5 6 7 g38bnb\n'
                                  '/start 0 1 2 3 4 5 6 7 g38eth\n')
        update.message.reply_text(
            '\n'.join(f'{n[0]} = {n[1]}' for n in NotifyRecipients.NOTIFY_TYPE) + '\nto reset all subscriptions do\n'
                                                                                  '/clear')

    levels = []
    words = []
    strats = []
    is_letter = False
    for arg in args:
        if not is_letter and arg.isnumeric():
            levels.append(int(arg))
            continue
        words.append(arg)
        is_letter = True

    if len(words) > 0:
        try:
            strats.append(Strategy.objects.get(name=' '.join(words)))
            update.message.reply_text(f'Strategy {" ".join(words)} found')
        except:
            update.message.reply_text(f'Strategy {" ".join(words)} not found')
            update.message.reply_text(f'No workable strategy. Applying all.')
    else:
        update.message.reply_text(f'No workable strategy. Applying all.')

    created = None
    for level in levels:
        nt, created = NotifyRecipients.objects.get_or_create(contact=contact, level=level,
                                                             defaults={'active': True, 'username': username})
        if not nt.active:
            nt.active = True
            nt.save()

        for st in strats:
            nt.strategies.add(st)

    if created and not strats:
        update.message.reply_text(
            f'Notification recipient created')
        update.message.reply_text(
            f'Notifications on "{", ".join(NotifyRecipients.NOTIFY_TYPE[n][1] for n in levels)}"'
            f' successfully configured for all strategies')
    if levels and strats:
        update.message.reply_text(
            f'Notifications on "{", ".join(NotifyRecipients.NOTIFY_TYPE[n][1] for n in levels)}"'
            f' successfully configured for {",".join(s.name for s in strats)}')


# TODO
def info(update, context):
    logger.debug('/info')
    chat_id = update.message.chat_id
    contact = update.message.from_user.id
    username = update.message.from_user.username

    update.message.reply_text(
        text=f'Coming soon, {username}!'
    )

    #
    # strategies = nr.strategies
    # if nr:
    #     bot.send_message(chat_id, f'My info:')
    #     # for strategies in nr.stra:
    #     bot.send_message(chat_id, f'Notifications on {", ".join(strategies)}')
    # else:
    #     bot.send_message(chat_id, f'Nothing. /start')
    # pass


def clear(update, context):
    logger.debug('/clear')
    from cryptoman.models import NotifyRecipients

    chat_id = update.message.chat_id
    contact = update.message.from_user.id
    username = update.message.from_user.username
    if username:
        NotifyRecipients.objects.filter(contact=username).delete()
    elif contact:
        NotifyRecipients.objects.filter(contact=contact).delete()
    else:
        NotifyRecipients.objects.filter(contact=chat_id).delete()

    update.message.reply_text(
        # chat_id=update.message.chat_id,
        text=f'Unsubscribed. Byby, {username}...'
    )


def send_message(recipients, message):
    logger.debug('send_message')
    for recipient in recipients:
        # msg = f"{msg['text']}"
        # l.debug(u'Adding this message: %s' % json.dumps({'recipient': recipient, 'message': message}, indent=4, sort_keys=True))
        _r.publish('cryptoman.message', json.dumps({'recipient': recipient, 'message': message}))


def connect():
    request = Request(con_pool_size=8)
    bot = telegram.Bot(TOKEN, request=request)

    logger.debug("Starting the bot")
    bot.send_message("996224228", f'Ready to start working\n'
                                  f'/help\nor\n/start')

    # updater = Updater(bot=bot, use_context=True)
    updater = Updater(bot=bot, use_context=True)

    updater.running = True
    updater.job_queue.start()
    updater._init_thread(updater.dispatcher.start, "dispatcher"),

    logger.debug("Loading handlers for telegram bot")
    dispatcher = updater.dispatcher
    dispatcher.add_error_handler(error)

    dispatcher.add_handler(CommandHandler("help", help))
    dispatcher.add_handler(CommandHandler("echo", echo))
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(CommandHandler("clear", clear))
    dispatcher.add_handler(CommandHandler("info", info))

    unknown_handler = MessageHandler(Filters.command, unknown)
    dispatcher.add_handler(unknown_handler)

    echo_handler = MessageHandler(Filters.text, echo)
    dispatcher.add_handler(echo_handler)

    # from telegram.ext import InlineQueryHandler
    # inline_caps_handler = InlineQueryHandler(inline_caps)
    # dispatcher.add_handler(inline_caps_handler)

    handlers.append("/start")
    handlers.append("/help")
    handlers.append("/clear")
    handlers.append("/echo")
    handlers.append("/info")

    dns = os.getenv('TELEGRAM_DNS', 'localhost')
    hook = f'https://' + dns + f'/{settings.TG_HOOK_TOKEN}'
    logger.debug(hook)
    is_set = updater.bot.set_webhook(hook, allowed_updates=['message', 'callback_query'])

    hooker = TGHook(bot, updater)
    hooker.daemon = True
    hooker.start()
    logger.debug('ready to process messages')
    hooker.join()


def help(update, context):
    logger.debug('/help')
    """Send a message when the command /help is issued."""
    seperator = '\n'
    msg = f'You have these options:\n{seperator.join(handlers)}'
    update.message.reply_text(
        # chat_id=update.message.chat_id,
        text=msg
    )
    # for x in handlers:
    # update.message.reply_text(x)


# def echo(bot, update):
#     """Echo the user message."""
#     # bot.sendMessage(update.message.chat_id, text=update.message.text)
#     update.message.reply_text(update.message.text)

def echo(update, context):
    logger.debug('/echo')
    msg = f'Say what now? {update.message.text}?\nTry /help'
    update.message.reply_text(
        # chat_id=update.message.chat_id,
        text=msg
    )


def unknown(update, context):
    logger.debug('unknown')
    msg = "Say what now? Can't understand that command. Try /help"
    update.message.reply_text(
        # chat_id=update.message.chat_id,
        text=msg
    )


def error(update, context):
    """Log Errors caused by Updates."""
    logger.warning('Error "%s" caused error "%s"', update, context.error)

# from telegram import InlineQueryResultArticle, InputTextMessageContent

# def inline_caps(update, context):
#     query = update.inline_query.query
#     if not query:
#         return
#     results = list()
#     results.append(
#         InlineQueryResultArticle(
#             id=query.upper(),
#             title='Caps',
#             input_message_content=InputTextMessageContent(query.upper())
#         )
#     )
#     context.bot.answer_inline_query(update.inline_query.id, results)

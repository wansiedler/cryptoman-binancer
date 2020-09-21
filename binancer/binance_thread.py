import logging
import json
import threading
import atexit
import traceback
from queue import Queue
import typing
import redis
import os

from redis import Redis

from cryptoman.models import Order
from binancer.common import format_stacktrace
from binancer.binancer import process_signal, clients, connect, process_smart_order
from binancer.tasks import open_order, open_pending, close_order, cancel_order, add_order

# from binancer import logger

queue: typing.Optional[Queue] = None
# wqueue: typing.Optional[Queue] = None

TRADE_AMOUNT = 6
trading_threads = []

WORKERS_AMOUNT = 6
working_threads = []

logger = logging.getLogger("")

logger.debug("Starting thread")

redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_password = os.getenv('REDIS_PASSWORD', '')


def get_redis() -> Redis:
    if redis_password:
        redis_access: Redis = redis.client.StrictRedis(host=redis_host, password=redis_password)
    else:
        redis_access: Redis = redis.client.StrictRedis(host=redis_host)
    return redis_access


def init_thread():
    global queue
    queue = Queue()

    logger.debug(f'Создам тредов: {WORKERS_AMOUNT}')
    for i in range(WORKERS_AMOUNT):
        ticker_thread = TickerThread(i)
        ticker_thread.daemon = True
        ticker_thread.start()
        working_threads.append(ticker_thread)

    for i in range(TRADE_AMOUNT):
        binance_trade_thread = BinanceTradeThread(queue, i)
        binance_trade_thread.daemon = True
        binance_trade_thread.start()
        trading_threads.append(binance_trade_thread)

    atexit.register(exit_thread)

    connect()

    binance_thread = BinanceThread()
    binance_thread.daemon = True
    binance_thread.start()

    return binance_thread


def exit_thread():
    logger.debug('Binance threads are down...')
    for i in range(TRADE_AMOUNT):
        queue.put(('EXIT', 0, 0, False))


class BinanceThread(threading.Thread):
    def __init__(self):
        super().__init__()
        logger.debug('Создаю тред Binance')

        self.redis_access: Redis = get_redis()

    def run(self):
        logger.debug('Запускаю тред Binance')
        while True:
            try:
                # message = p.get_message(timeout=10)
                message = self.redis_access.blpop('binance')
                if not message:
                    continue
                message = json.loads(message[1])
                logger.debug(f'Есть задача на отработку: {message["channel"]} {message["data"]}')

                if message['channel'] == 'signal':
                    process_signal(message['data'])

                elif message['channel'] == 'sync':
                    account_id = int(message['data'])
                    if account_id not in clients:
                        connect()
                    else:
                        clients[account_id].sync_orders()

                elif message['channel'] == 'sync_order':
                    order_id = int(message['data'])
                    order = Order.objects.get(id=order_id)
                    if order.account_id not in clients:
                        connect()
                    else:
                        clients[order.account_id].sync_order(order)

                elif message['channel'] == 'reconnect':
                    account_id = int(message['data'])
                    clients[account_id].reconnect()

                elif message['channel'] == 'trade':
                    command, order_id, quantity, sync = message['data']
                    queue.put((command, order_id, quantity, sync))

                elif message['channel'] == 'smart_order':
                    logger.warning('###поступил новый ордер на отработку!###')
                    process_smart_order(int(message['data']))

            except Exception as e:
                stacktrace = format_stacktrace()
                logger.exception(f'Error in Binance Thread {e} {stacktrace}')
                # traceback.print_exc()
            logger.debug('Binance Thread done')


class BinanceTradeThread(threading.Thread):
    def __init__(self, incoming_queue, index):
        super().__init__()
        logger.debug('Создаю поток для торгов BinanceTradeThread #%s' % index)
        self.queue = incoming_queue
        self.index = index

        self.r: Redis = get_redis()

        self.r.hset('threads', str(self.index), 0)
        self.r.hset('thread_counters', str(self.index), 0)

    def run(self):
        logger.debug(f' Запускаю поток для торгов  BinanceTradeThread #{self.index}')
        while True:
            try:
                command, order_id, quantity, sync = self.queue.get()  # type: str, int, float, bool
                logger.debug(f'thread #{self.index} {command} {order_id} {quantity} {sync}')
                self.r.hset('threads', str(self.index), 1)
                if order_id:
                    order = Order.objects.get(id=order_id)
                    if order.account_id not in clients:
                        connect()
                    client = clients[order.account_id].client
                else:
                    order = None
                    client = None

                logger.info(f'Got a job to do: {command}')
                if command == 'OPEN_ORDER':
                    open_order(order_id, client)
                elif command == 'PENDING_ORDER':
                    open_pending(order_id, client)
                elif command == 'CLOSE_ORDER':
                    close_order(order_id, client, quantity)
                elif command == 'CANCEL_ORDER':
                    cancel_order(order_id, client)
                elif command == 'ADD_ORDER':
                    add_order(order_id, client, quantity)
                elif command == 'QUIT':
                    break
                else:
                    logger.debug('Unknown command ' + command)

                if sync and order:
                    clients[order.account_id].sync_order(Order.objects.get(id=order_id))


            except Exception as e:
                stacktrace = format_stacktrace()
                logger.exception(f'Error in BinanceTradeThread {e} {stacktrace}')
                # logger.debug(f'thread = {self.index}')
                traceback.print_exc()
                return

            logger.debug(f'thread #{self.index} done')
            self.r.hset('threads', str(self.index), 0)
            counter = int(self.r.hget('thread_counters', str(self.index))) if self.r.hexists('thread_counters',
                                                                                             str(self.index)) else 0
            self.r.hset('thread_counters', str(self.index), counter + 1)
            self.queue.task_done()


class TickerThread(threading.Thread):
    def __init__(self, index):
        super().__init__()
        self.index = index
        logger.debug(f'Запускаю поток тикеров TickerThread #{self.index}')
        self.r: Redis = get_redis()

    def run(self):
        while True:
            try:
                message = self.r.blpop('jobs')
                if not message:
                    continue
                # logger.debug(f'{message} @ thread #{self.index}')
                client_id = int(message[1])
                if client_id < 0:
                    break
                if client_id in clients:
                    clients[client_id].process_tickers()

            except Exception as e:
                stacktrace = format_stacktrace()
                logger.exception(f'Error in TickerThread #{self.index}: {e} {stacktrace}')
            # logger.debug(f'worker #{self.index}: done')
            # self.queue.task_done()

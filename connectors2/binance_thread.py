import json
import threading
import atexit
import traceback
from queue import Queue
import typing

import redis

queue: typing.Optional[Queue] = None
wqueue: typing.Optional[Queue] = None

from cryptoman.models import Order
from connectors.binancer import process_signal, clients, connect, process_smart_order
from cryptoman.tasks import open_order, open_pending, close_order, cancel_order, add_order

TRADE_AMOUNT = 8
threads = []

WORKERS_AMOUNT = 8
wthreads = []


class BinanceThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.r = redis.StrictRedis()

    def run(self):
        """
        p = self.r.pubsub()
        p.subscribe('binance.signal')
        p.subscribe('binance.sync')
        p.subscribe('binance.trade')
        """
        while True:
            try:
                # message = p.get_message(timeout=10)
                message = self.r.blpop('binance')
                if not message: continue
                message = json.loads(message[1])
                print(f'bthread: {message["channel"]} {message["data"]}')
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
                    process_smart_order(int(message['data']))

            except:
                traceback.print_exc()
            print('bthread done')


class BinanceTradeThread(threading.Thread):
    def __init__(self, queue, index):
        super().__init__()
        self.queue = queue
        self.index = index
        self.r = redis.StrictRedis()
        self.r.hset('threads', str(self.index), 0)
        self.r.hset('thread_counters', str(self.index), 0)

    def run(self):
        while True:
            try:
                command, order_id, quantity, sync = self.queue.get()  # type: str, int, float, bool
                print(f'thread #{self.index} {command} {order_id} {quantity} {sync}')
                self.r.hset('threads', str(self.index), 1)
                if order_id:
                    order = Order.objects.get(id=order_id)
                    if order.account_id not in clients:
                        connect()
                    client = clients[order.account_id].client
                else:
                    order = None
                    client = None

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
                    print('unknown command '+command)

                if sync and order:
                    clients[order.account_id].sync_order(Order.objects.get(id=order_id))
            except:
                print(f'thread = {self.index}')
                traceback.print_exc()
            print(f'thread #{self.index} done')
            self.r.hset('threads', str(self.index), 0)
            counter = int(self.r.hget('thread_counters', str(self.index))) if self.r.hexists('thread_counters', str(self.index)) else 0
            self.r.hset('thread_counters', str(self.index), counter+1)
            self.queue.task_done()


class TickerThread(threading.Thread):
    def __init__(self, index):
        super().__init__()
        self.r = redis.StrictRedis()
        self.index = index

    def run(self):
        while True:
            try:
                message = self.r.blpop('jobs')
                if not message: continue
                #print(f'worker #{self.index}: {message}')
                client_id = int(message[1])
                if client_id < 0:
                    break
                if client_id in clients:
                    clients[client_id].process_tickers()
            except:
                print(f'thread = {self.index}')
                traceback.print_exc()
            #print(f'worker #{self.index}: done')
            #self.queue.task_done()


def init_thread():
    global queue
    for i in range(WORKERS_AMOUNT):
        thread = TickerThread(i)
        thread.daemon = True
        thread.start()
        wthreads.append(thread)

    queue = Queue()
    for i in range(TRADE_AMOUNT):
        thread = BinanceTradeThread(queue, i)
        thread.daemon = True
        thread.start()
        threads.append(thread)

    atexit.register(exit_thread)

    connect()

    thread = BinanceThread()
    thread.daemon = True
    thread.start()

    return thread


def exit_thread():
    print('binance threads down...')
    for i in range(TRADE_AMOUNT):
        queue.put(('EXIT', 0, 0, False))
    #for i in range(WORKERS_AMOUNT):


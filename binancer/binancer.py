import json
import logging
import os
import sys
import traceback
from datetime import datetime, timedelta

import redis
from binance.exceptions import BinanceAPIException
from math import isclose
import typing
from dataclasses import dataclass, field
from threading import Lock
import atexit

import binancer.binancer_client as binancer_client
from binance.client import Client as Binance_Client
from binance.websockets import BinanceSocketManager
from django.db.models import Q, Count
from redis import Redis
from binancer.trade_mate import TradeMate
from cryptoman.models import *
from binancer.signals import order_error, tick_processing
from binancer.common import MakeDirty, lock_instance
from binancer.common import cfloat

clients: typing.Dict[int, 'AccountClient'] = {}
quotes_maker: typing.Optional[BinanceSocketManager] = None
subscriptions: typing.Dict[str, 'QuoteToTrack'] = {}

redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_password = os.getenv('REDIS_PASSWORD', '')
redis_access: Redis = redis.client.StrictRedis(host=redis_host, password=redis_password)

# logger = logging.getLogger("")
from binancer import logger


@dataclass
class QuoteToTrack:
    ask: float
    bid: float
    min_lot: float
    min_notional: float
    tick_size: float


@dataclass
class OrderToTrack:
    mode: 'TradingMode'
    symbol: str
    bot: typing.Optional[Bot]
    strategy: typing.Optional[Strategy]
    signal: typing.Optional[Signal]
    open_price: float
    order: Order = field(init=False)
    stoploss_price: float = field(init=False)
    pending_stop_time: datetime = field(init=False)
    takeprofit_price: float = field(init=False)
    takeprofit_order: Order = field(init=False)
    takeprofit_quantity: float = field(init=False)
    takebuy_price: float = field(init=False)
    tp_mode: int = field(init=False)
    trailing: float = field(init=False, default=0.0)
    trailing_level: float = field(init=False, default=0.0)
    autobuy_price: float = field(init=False)
    # hold_trailing: bool = field(init=False)
    last_mode: 'TradingMode' = field(init=False, default=None)


class TradingMode(Enum):
    PendingBuy = auto()
    BuyReady = auto()
    InMarket = auto()
    Ready = auto()
    Stop = auto()


# class Binancer():
# @staticmethod

def connect():
    """
    Синхронизирую и добавление аккаунтов в массив для отслеживания в потоках
    """
    binancer_client.connect()

    # _r_quotes.delete("orders")
    for account in Account.objects.all().order_by('is_demo'):
        if account.id in clients:
            continue

        if account.is_demo:
            binance_client = None
        else:
            binance_client = binancer_client.binance_clients[account.id]
            # logger.info(f'подключение к Binance успешно {account.name}')

        clients[account.id] = AccountClient(account, binance_client)
        clients[account.id].sync_orders()


@atexit.register
def disconnect():
    """
    Отключение бинанса
    """
    logger.info('Отключаю binancer')
    for client in clients.values():  # type: AccountClient
        if hasattr(client, 'bm'):
            client.binance_socket_manager.close()


def init_quotes_maker():
    global quotes_maker

    client = Binance_Client('', '')
    quotes_maker = BinanceSocketManager(client)
    quotes_maker.start_ticker_socket(process_multi_ticker)
    subscribe_symbol('')


def process_ticker(msg: dict):
    global subscriptions
    quote: QuoteToTrack = subscriptions[msg['s']]
    quote.ask = float(msg['a'])
    quote.bid = float(msg['b'])

    check = redis_access.lindex(f'track{msg["s"]}', -1)
    last_item = {}
    if check:
        last_item = json.loads(check)
    if not check or last_item["a"] != msg["a"] or last_item["b"] != msg["b"]:
        redis_access.hset('quotes', msg['s'], float(msg['b']))
        redis_access.hset('quotes_ask', msg['s'], float(msg['a']))

        redis_access.publish('common.quotes', json.dumps({
            'subscription': 'common.quotes',
            'data': {
                'symbol': msg['s'],
                'price': msg['b'],
                'ask': msg['a'],
            }
        }))

        redis_access.rpush(f'track{msg["s"]}', json.dumps({
            'E': int(msg["E"]),
            'a': msg['a'],
            'b': msg['b'],
        }))

        tick_processing.send(sender=QuoteToTrack, symbol=msg['s'], quote=quote)
    # logger.info(f'{msg["s"]} {msg["a"]}')
    """
    for client in list(clients.values()):  # type: AccountClient
        if msg['s'] in client.symbols:
            client.process_ticker(msg)
    """


def process_multi_ticker(messages: list):
    try:
        for message in messages:
            if message['s'] in subscriptions:
                process_ticker(message)
        for client in clients:
            # workers_queue.put(i)
            redis_access.rpush('jobs', client)
    except:
        logger.info('\n'.join(traceback.format_exception(*sys.exc_info())))


def subscribe_symbol(symbol: str):
    global subscriptions, quotes_maker
    if not quotes_maker:
        logger.error('отсутствует базовое соединение для подписки на котировки')
        return
    if symbol not in subscriptions:
        try:
            tickers = quotes_maker._client.get_ticker()
            for ticker in tickers:
                if ticker['symbol'] in subscriptions:
                    continue

                logger.info('подписка на символ ' + ticker['symbol'])
                sym, created = Symbol.objects.get_or_create(name=ticker['symbol'])
                if created:
                    with lock_instance(sym) as sym:
                        symbol_info = quotes_maker._client.get_symbol_info(ticker['symbol'])
                        min_lot = next(
                            (float(f['minQty']) for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), 0)
                        min_notional = next(
                            (float(f['minNotional']) for f in symbol_info['filters'] if
                             f['filterType'] == 'MIN_NOTIONAL'), 0)
                        tick_size = next(
                            (float(f['tickSize']) for f in symbol_info['filters'] if f['filterType'] == 'PRICE_FILTER'),
                            0)

                        sym.min_notional = min_notional
                        sym.min_lot = min_lot
                        sym.tick_size = tick_size
                        sym.base_asset = symbol_info['quoteAsset']
                        sym.save()

                quote = QuoteToTrack(
                    float(ticker['askPrice']),
                    float(ticker['bidPrice']),
                    sym.min_lot,
                    sym.min_notional,
                    sym.tick_size
                )
                # if quote.min_notional > 0.001:
                #    logger.info(f'{ticker["symbol"]} == {quote.min_notional} !')

                subscriptions[ticker['symbol']] = quote
                # logger.info('подписка выполнена')

                # quotes_maker.start_symbol_ticker_socket(symbol, process_ticker)

        except Exception as e:
            traceback.print_exc()
            logger.error(f'не удалось подписаться на пару {symbol}: Ошибка {e}')
            return

        # process_ticker({'s': symbol, 'a': quote.ask, 'b': quote.bid})
        # logger.info('пара подписана')


def check_in_work(order_id: int) -> bool:
    for client in clients.values():
        if any(hasattr(o, "order") and o.order.id == order_id and o.mode != TradingMode.Stop for o in client.orders):
            return True
    return False


def process_smart_order(order_id):
    """
    Создание ордера
    """
    order = Order.objects.get(id=order_id)
    logger.info(f'начинаю отработку ордера #{order_id}')
    logger.info(f'аккаунт ордера #{order_id}: {order.account_id}')
    if order.account_id not in clients:
        logger.error(f'аккаунт {order.account.name} не подключен')
    else:
        clients[order.account.id].smart_order(order)


class AccountClient:
    def __init__(self, account: Account, client: Binance_Client):
        # global quotes_maker
        self.account: Account = account
        self.client: Binance_Client = client
        if not account.is_demo and account.api_key and account.secret_key:
            binance_socket_manager = BinanceSocketManager(client)
            self.binance_socket_manager = binance_socket_manager
            try:
                binance_socket_manager.start_user_socket(self.process_trade)
            except BinanceAPIException as e:
                logger.error(f'ошибка подключения {account.name}: {e}')
            else:
                """
                if not quotes_maker:
                    quotes_maker = bm
                    bm.start_ticker_socket(process_multi_ticker)
                    #bm.start_multiplex_socket(['!ticker@arr'], process_multi_ticker)
                """
                binance_socket_manager.start()

        self.symbols: typing.List[str] = []
        self.orders_to_track: typing.List[OrderToTrack] = []

        self.lock = Lock()

    def smart_order(self, order: Order):
        logger.info(f'отрабатываю ордер #{order.id}')
        if order.symbol.name not in self.symbols:
            logger.warning(f'{order.symbol.name} нету в подписках. подписываюсь. ')
            subscribe_symbol(order.symbol.name)
            self.symbols.append(order.symbol.name)

        if order.symbol.name not in subscriptions:
            logger.error(f'не удалось открыть ордер по причине отсутствия подписки на пару {order.symbol.name}')
            return

        with self.lock:
            status = TradingMode.PendingBuy if order.status == OrderStatus.PENDING else TradingMode.BuyReady
            logger.info(f'статус ордера: {status}')
            order_to_track = OrderToTrack(status, order.symbol.name, None, None, None, order.open_price)
            order_to_track.tp_mode = 1
            order_to_track.order = order
            self.orders_to_track.append(order_to_track)

        logger.info(f"локальный ордер на дальнейшую отработку #{order.id} успешно создан", order=order)

        self.process_tickers(order.symbol.name, True)

    def process_tickers(self, symbol_name=None, new_signal=False):
        # logger.info("отрабатываю тикеры")
        # if task_queue:
        #    task_queue.join()

        if new_signal:
            logger.info('> aquire lock (new signal)')

        now = datetime.now()

        with self.lock:
            if new_signal:
                logger.info("> cycle")
            for order in self.orders_to_track:  # type: OrderToTrack
                if order.mode == TradingMode.Stop or new_signal and order.mode != TradingMode.BuyReady:
                    continue
                if symbol_name and order.symbol != symbol_name:
                    continue

                def error(msg, **kwargs):
                    logger.error(msg, strategy=order.strategy, signal=order.signal,
                                 order=order.order if hasattr(order, 'order') and order.order else None, **kwargs)

                def warning(msg, **kwargs):
                    logger.warning(msg, strategy=order.strategy, signal=order.signal,
                                   order=order.order if hasattr(order, 'order') and order.order else None, **kwargs)

                def info(msg, **kwargs):
                    logger.info(msg, strategy=order.strategy, signal=order.signal,
                                order=order.order if hasattr(order, 'order') and order.order else None, **kwargs)

                base_cur = Symbol.objects.get(name=order.symbol).base_asset
                symbol = subscriptions[order.symbol]
                ask = symbol.ask
                bid = symbol.bid

                if hasattr(order, 'order') and order.order:
                    if not hasattr(order.order, 'orderspecial'):
                        order.order.orderspecial = OrderSpecial.objects.create(order=order.order, max_price=bid, min_ask_price=ask, max_price_date=datetime.now(), min_ask_date=datetime.now())
                    else:
                        if order.order.orderspecial.max_price < bid:
                            order.order.orderspecial.max_price = bid
                            order.order.orderspecial.max_price_date = datetime.now()
                            order.order.orderspecial.save()
                        elif not order.order.orderspecial.min_ask_price or order.order.orderspecial.min_ask_price > ask:
                            order.order.orderspecial.min_ask_price = ask
                            order.order.orderspecial.min_ask_date = datetime.now()
                            order.order.orderspecial.save()

                def round_lot(lot):
                    return round(lot / symbol.min_lot) * symbol.min_lot

                def round_tick(price):
                    return round(price / symbol.tick_size) * symbol.tick_size

                # === Pending buy state ===
                if order.mode == TradingMode.PendingBuy:
                    info(f'ордер #{order.order.id} in pending buy state')
                    if order.order.order_kind == 0 and ask <= order.open_price or order.order.order_kind == 1 \
                            and ask >= order.open_price or order.order.order_kind == 2:
                        info(f'меняю статус на pending buy')
                        order.mode = TradingMode.BuyReady
                    elif order.order.preopen_stop_time and now >= order.order.preopen_stop_time:
                        info(f'таймаут отложки #{order.order.id}. отменяю ордер')
                        order.order.status = OrderStatus.CANCELLED
                        with MakeDirty(order.order):
                            order.order.save()
                        order.mode = TradingMode.Stop
                    else:
                        info(f'тип ордера: {order.order.order_kind}')
                        info(f'аск: {ask} пока больше цены открытия {order.open_price}. ждем?')

                # === Buy ready state ===
                if order.mode == TradingMode.BuyReady:
                    order.mode = TradingMode.Stop
                    info(f'ордер #{order.order.id} готов к открытию.')
                    if hasattr(order, "order"):
                        with MakeDirty(order.order):
                            info(f'отменяю ордер #{order.order.id}')
                            order.order.status = OrderStatus.CANCELLED
                            order.order.save()

                    if ask > order.open_price and not hasattr(order, "order") and (ask - order.open_price) * 100 / ask > order.strategy.corridor:
                        warning(f'невозможно открыть ордер за границами коридора {cfloat(order.open_price)} < {cfloat(ask)}')
                        # order.mode = TradingMode.Stop
                        continue

                    # check limits
                    look_limits = BalanceLimits.objects.filter(strategy=order.strategy, account=self.account)
                    if not order.strategy or order.strategy.balance_limit_mode == 0 or not look_limits.exists():
                        if not self.account.is_demo:
                            logger.info('> get account')
                            balances = self.client.get_account()['balances']
                            logger.info('> account ok')
                            balance = next(
                                (b['free'] for b in balances if b['asset'] == base_cur), 0)
                            balance = float(balance)
                        else:
                            self.account.refresh_from_db()
                            balance = self.account.balance[base_cur] if base_cur in self.account.balance else 0
                    else:
                        balance = look_limits.first().balance[base_cur]

                    if self.account.balance_control and (not balance or not float(balance)):
                        error(f'Баланс нулевой {base_cur} {self.account.name} для ордера по паре {order.symbol}')
                        # order.mode = TradingMode.Stop
                        continue

                    if order.strategy:
                        if not self.account.balance_control:
                            trade_balance = 1.0
                        elif order.strategy.lot_mode == 1:
                            trade_balance = order.strategy.lot_amount
                        else:
                            total_balance = binancer_client.total_btc(self.account, base_cur)
                            trade_balance = (total_balance[0] + total_balance[1]) * float(order.strategy.lot_amount) / 100

                        quantity = round_lot(trade_balance / ask)
                    else:
                        quantity = order.order.quantity
                        trade_balance = order.order.volume

                    if quantity < symbol.min_lot:
                        error(f'количество монет покупки меньше допустимого минимума {cfloat(quantity)} < {cfloat(symbol.min_lot)}'
                              f'trade_balance = {cfloat(trade_balance)} ask = {cfloat(ask)}')
                        # order.mode = TradingMode.Stop
                        continue

                    if trade_balance < symbol.min_notional:
                        error(f'объём ордера меньше допустимого минимума {cfloat(trade_balance)} < {cfloat(symbol.min_notional)}')
                        # order.mode = TradingMode.Stop
                        continue

                    if self.account.balance_control and trade_balance > balance:
                        error(f'недостаточно денег для покупки ордера {cfloat(trade_balance)} > {cfloat(balance)}')
                        # order.mode = TradingMode.Stop
                        continue

                    if not hasattr(order, 'order'):
                        stop_loss = round_tick(ask * (100 - order.strategy.stoploss) / 100)
                        if ask - stop_loss <= symbol.tick_size:
                            error(f'уровень стоп лосс слишком близок к цене открытия {cfloat(ask - stop_loss)} <= {cfloat(symbol.tick_size)}')
                            order.mode = TradingMode.Stop
                            continue
                        order.stoploss_price = stop_loss
                        order.pending_stop_time = datetime.now() + timedelta(
                            days=order.strategy.expire_time) if order.strategy.expire_time else None
                    else:
                        order.stoploss_price = order.order.stoploss_price
                        order.pending_stop_time = order.order.pending_stop_time

                    if order.stoploss_price and quantity * order.stoploss_price < symbol.min_notional:
                        error(
                            f'объём уровня (стоп-лосс) меньше допустимого минимума {cfloat(quantity * order.stoploss_price)} < {cfloat(symbol.min_notional)}')
                        # order.mode = TradingMode.Stop
                        continue

                    if not hasattr(order, 'order'):
                        stop_levels = [{'stop_price': round_tick(ask * (100 + level.percent) / 100),
                                        'quantity': round_lot(quantity * level.percent_to_close / 100),
                                        'sl_delta': round_tick(
                                            ask * level.trailing_percent / 100) if level.trailing_mode else None,
                                        'trailing': level.trailing_percent if level.trailing_mode else 0.0,
                                        'step_stop': level.step_stop,
                                        'percent': level.percent,
                                        } for level in order.strategy.takeprofitlevel_set.all()]

                        stop_levels = sorted(stop_levels, key=lambda item: item['stop_price'])

                        autotrade_levels = [{'price': round_tick(ask * (100 - level.percent) / 100),
                                             'quantity': round_lot(level.lot / round_tick(ask * (100 - level.percent) / 100)),
                                             } for level in order.strategy.takebuylevel_set.all()]
                        autotrade_levels = sorted(autotrade_levels, key=lambda item: item['price'], reverse=True)
                        order.autobuy_price = autotrade_levels and autotrade_levels[0]['price'] or None
                    else:
                        stop_levels = order.order.takeprofit_levels
                        autotrade_levels = None

                    ok = True
                    for level in stop_levels:
                        if level['sl_delta'] is not None:
                            if level['sl_delta'] <= symbol.tick_size:
                                error(f'разница в цене для скользящего стоп-лосса меньше допустимой {cfloat(level["sl_delta"])} <= {cfloat(symbol.tick_size)}')
                                ok = False
                                break

                    if not ok:
                        # order.mode = TradingMode.Stop
                        continue

                    # open order
                    if not hasattr(order, 'order'):
                        morder = Order(
                            account=self.account,
                            bot=order.bot,
                            strategy=order.strategy,
                            symbol=Symbol.objects.get(name=order.symbol),
                            signal=order.signal,
                            order_side=0,
                            open_price=ask,
                            quantity=quantity,
                            quantity_rest=quantity,
                            volume=trade_balance,
                            simulation=order.strategy.simulation,
                            stoploss_price=stop_loss,
                            pending_stop_time=order.pending_stop_time,
                            status=OrderStatus.ACTIVE,
                            takeprofit_levels=stop_levels,
                            autotrade_levels=autotrade_levels,
                        )
                        # setattr(morder, "no_sync", True)
                        morder.save()
                        morder.orderspecial = OrderSpecial.objects.create(order=morder, max_price=bid, min_ask_price=ask, max_price_date=datetime.now(), min_ask_date=datetime.now())
                        # order.order = morder
                    else:
                        morder = order.order
                        morder.status = OrderStatus.ACTIVE
                        morder.open_price = ask
                        morder.takeprofit_levels = stop_levels
                        # setattr(morder, "no_sync", True)
                        morder.save()

                        # if morder.takebuy_levels:
                        #    order.takebuy_price = morder.takebuy_levels[0]['price']

                    # order.trailing_level = next((l['stop_price'] for l in stop_levels if l['trailing'] and 'filled' not in l and not l.get('hidden')), None)
                    # order.mode = TradingMode.InMarket

                # === Market state ===
                elif order.mode == TradingMode.InMarket:
                    try:
                        order.order.refresh_from_db()
                    except:
                        logger.info(f'order_id = {order.order.order_id}')
                        traceback.print_exc()
                        order.mode = TradingMode.Stop
                        continue

                    if order.order.order_id:
                        order.mode = TradingMode.Ready

                        stop_levels = order.order.takeprofit_levels
                        stop_level = next(lvl for lvl in stop_levels if not lvl.get('hidden'))
                        order.takeprofit_price = stop_level['stop_price']
                        order.takeprofit_quantity = stop_level['quantity']

                        if order.strategy and order.strategy.tp_mode == 0:
                            for i, level in enumerate(stop_levels):
                                if level['quantity'] == 0:
                                    if i == 0:
                                        order.takeprofit_order = None
                                    continue
                                corder = Order(
                                    account=self.account,
                                    bot=order.bot,
                                    strategy=order.strategy,
                                    symbol=order.order.symbol,
                                    order_side=1,
                                    open_price=level['stop_price'],
                                    quantity=level['quantity'],
                                    volume=round_lot(level['quantity'] * level['stop_price']),
                                    simulation=order.strategy.simulation,
                                    status=OrderStatus.PENDING,
                                    parent_order=order.order,
                                    trailing_percent=level['trailing'],
                                )
                                setattr(corder, "no_sync", True)
                                corder.save()
                                if i == 0:
                                    order.takeprofit_order = corder

                    elif order.order.error_text:
                        order.mode = TradingMode.Stop
                    else:
                        if order.order.status != OrderStatus.ACTIVE and (
                                datetime.now() - order.order.open_time).seconds > 1800:
                            error(f'таймаут ожидания рыночной операции {self.account.name}')
                            order.mode = TradingMode.Stop

                # === Ready state ===
                elif order.mode == TradingMode.Ready:
                    # check stop-loss
                    need_stop = False
                    if order.stoploss_price and ask <= order.stoploss_price:
                        info(f'закрытие ордера по стоп-лосс ({order.order.get_stoploss_type_display()}) {cfloat(order.stoploss_price)}',
                             notify=NotifyType.Closed, sl=True)
                        need_stop = True
                    elif order.pending_stop_time and now >= order.pending_stop_time:
                        info(f'закрытие ордера по тайм-ауту',
                             notify=NotifyType.Closed, sl=True)
                        need_stop = True
                    if need_stop:
                        morder = order.order
                        for corder in morder.child_orders.filter(status=OrderStatus.PENDING):  # type: Order
                            setattr(corder, "no_sync", True)
                            corder.status = OrderStatus.CANCELLED
                            corder.save()

                        with lock_instance(morder) as morder:
                            setattr(morder, "no_sync", True)
                            morder.status = OrderStatus.CLOSED
                            morder.save()
                        order.mode = TradingMode.Stop
                        continue

                    # align trailing
                    if order.trailing > 0:
                        trail_price = round_tick(bid * (100 - order.trailing) / 100)
                        if not order.stoploss_price or trail_price > order.stoploss_price:
                            order.stoploss_price = trail_price
                            with MakeDirty(order.order):
                                order.order.refresh_from_db()
                                # lvl = next(lvl for lvl in order.order.takeprofit_levels if lvl['stop_price'] == order.takeprofit_price)
                                no_roll = False
                                '''
                                if lvl['step_stop']:
                                    idx = order.order.takeprofit_levels.index(lvl)
                                    if idx > 0:
                                        prev_lvl = order.order.takeprofit_levels[idx-1]
                                        if not prev_lvl.get('hidden'):
                                            order.order.trailing_active = False
                                            order.trailing = 0
                                            no_roll = True
                                '''
                                if not no_roll:
                                    order.order.stoploss_price = trail_price
                                order.order.save()

                    # check traling level
                    if order.trailing_level and bid >= order.trailing_level:
                        morder: Order = order.order
                        with lock_instance(morder) as morder:
                            with MakeDirty(morder):
                                level = next((lvl for lvl in morder.takeprofit_levels if 'filled' not in lvl and not lvl.get('hidden')), None)
                                if not level:
                                    order.trailing_level = None
                                    continue
                                morder.trailing_active = True
                                morder.trailing_percent = level['trailing']
                                morder.stoploss_type = 1
                                order.trailing = level['trailing']
                                order.trailing_level = next((l['stop_price'] for l in morder.takeprofit_levels if
                                                             l['trailing'] and l['stop_price'] > order.trailing_level),
                                                            None)
                                # if not level['quantity']:
                                #    level['filled'] = True
                                morder.save()
                                info(f'активирован ТРС на {morder.trailing_percent}%')

                    # check take-profit
                    if order.strategy and order.strategy.simulation or self.account.is_demo or order.tp_mode == 1:
                        if hasattr(order, "takeprofit_price") and order.takeprofit_price and bid >= order.takeprofit_price:
                            morder = order.order
                            morder.refresh_from_db()
                            levels_count = sum(1 for lvl in morder.takeprofit_levels if 'filled' not in lvl and not lvl.get('hidden'))

                            if levels_count == 0:
                                logger.info(f'количество уровней ордера {order.order.id} равно нулю')
                                order.mode = TradingMode.Stop
                                continue

                            if order.tp_mode == 0 and order.takeprofit_quantity > 0:
                                corder = order.takeprofit_order
                                if not corder:
                                    error(
                                        f'не найден закрывающий ордер #{morder.id}')
                                    with lock_instance(morder) as morder:
                                        setattr(morder, "no_sync", True)
                                        morder.status = OrderStatus.CLOSED
                                        morder.save()
                                    order.mode = TradingMode.Stop
                                    continue

                                with MakeDirty(corder):
                                    corder.status = OrderStatus.CANCELLED
                                    corder.close_price = bid
                                    corder.close_time = datetime.now()
                                    corder.save()

                            if order.takeprofit_quantity > 0:
                                profit = (bid - morder.open_price) * order.takeprofit_quantity
                                with lock_instance(morder) as morder:
                                    setattr(morder, "partial_quantity", order.takeprofit_quantity)
                                    setattr(morder, "no_sync", True)
                                    morder.save()
                                info(f'сработал тейк-профит #{morder.id} {cfloat(order.takeprofit_price)}', notify=NotifyType.Closed)

                            with lock_instance(morder) as morder:
                                lvl = next((lvl for lvl in morder.takeprofit_levels if 'filled' not in lvl and not lvl.get('hidden')), None)
                                lvl['filled'] = True

                                if levels_count > 1:
                                    if lvl.get('step_stop', False):
                                        idx = morder.takeprofit_levels.index(lvl)
                                        if idx:
                                            prev_lvl = morder.takeprofit_levels[idx - 1]
                                            if not prev_lvl.get('hidden'):
                                                morder.stoploss_price = prev_lvl['stop_price']
                                                morder.stoploss_type = 2
                                                order.stoploss_price = prev_lvl['stop_price']
                                                info(f'активирован шаговый стоп на {cfloat(order.stoploss_price)}')

                                    lvl = next(lvl for lvl in morder.takeprofit_levels if 'filled' not in lvl and not lvl.get('hidden'))
                                    order.takeprofit_price = lvl['stop_price']
                                    order.takeprofit_quantity = lvl['quantity']
                                    # order.trailing = lvl['trailing']

                                    if order.tp_mode == 0:
                                        if order.takeprofit_quantity > 0:
                                            order.takeprofit_order = morder.child_orders.filter(
                                                status=OrderStatus.PENDING).order_by('open_price').first()
                                        else:
                                            order.takeprofit_order = None
                                        """
                                        order.takeprofit_price = order.takeprofit_order.open_price
                                        order.takeprofit_quantity = order.takeprofit_order.quantity
                                        order.trailing = order.takeprofit_order.trailing_percent
                                        """
                                else:
                                    """
                                    if not isclose(morder.quantity_rest, 0, abs_tol=1e-8):
                                        warning(f'после срабатывания тейк-профита(ов) ордер закрыт не полностью #{morder.id}')

                                    morder.status = OrderStatus.CLOSED
                                    morder.save()
                                    """
                                    if order.takeprofit_quantity > 0:
                                        info(f'по ордеру все уровни ТП достигнуты #{morder.id} {cfloat(morder.profit)}', notify=NotifyType.Closed)
                                        order.mode = TradingMode.Stop
                                    else:
                                        order.takeprofit_price = 0

                                with MakeDirty(morder):
                                    # morder.trailing_active = order.trailing == 0.0
                                    # morder.trailing_percent = order.trailing
                                    '''
                                    for lvl in morder.takebuy_levels:
                                        if lvl['quantity'] and 'filled' not in lvl:
                                            morder.takebuy_levels.remove(lvl)
                                    '''
                                    # order.takebuy_price = 0
                                    morder.save()

                    if hasattr(order, "takebuy_price") and order.takebuy_price and ask <= order.takebuy_price:
                        with lock_instance(order.order) as morder:
                            with MakeDirty(morder):
                                lvl = next(l for l in morder.takebuy_levels if 'filled' not in l)
                                lvl['filled'] = True
                                lvl2 = next((l for l in morder.takebuy_levels if 'filled' not in l), None)
                                last_count = sum('filled' not in l for l in morder.takebuy_levels)
                                order.takebuy_price = lvl2 and lvl2['price']
                                morder.save()
                                info(f'достигнут ТБ {cfloat(lvl["price"])}', notify=NotifyType.Order, prev_tb=last_count == 1)
                        binancer_client.trade('ADD_ORDER', order.order.id, lvl['quantity'] * (lvl['average'] and -1 or 1), lvl['average'] or any(l.get('hidden') for l in order.order.takeprofit_levels))

                    if hasattr(order, 'autobuy_price') and order.autobuy_price and ask <= order.autobuy_price:
                        with lock_instance(order.order) as morder:
                            with MakeDirty(morder):
                                lvl = next(l for l in morder.autotrade_levels if l['price'] == order.autobuy_price)
                                lvl['filled'] = True
                                last_count = sum('filled' not in l for l in morder.autotrade_levels)
                                quantity = lvl['quantity']
                                morder.save()
                                info(f'достигнут auto-trade {cfloat(order.autobuy_price)}', notify=NotifyType.Order, prev_tb=last_count == 1)
                        order.mode = TradingMode.Stop
                        binancer_client.trade('ADD_ORDER', order.order.id, quantity, True)

            # refresh order states
            if new_signal:
                logger.info('> states')

            for order in self.orders_to_track:
                if order.mode != order.last_mode and hasattr(order, "order"):
                    redis_access.hset('orders', order.order.id, 1 if order.mode != TradingMode.Stop else 0)
                    order.last_mode = order.mode

    def reconnect(self):
        # global quotes_maker
        if not self.account.is_demo and self.account.api_key and self.account.secret_key:
            with self.lock:
                # deinit
                if self.binance_socket_manager:
                    self.binance_socket_manager.close()
                    # if quotes_maker == self.bm:
                    #    quotes_maker = None

                # reinit
                self.account.refresh_from_db()
                binancer_client.binance_clients[self.account.id] = Binance_Client(self.account.api_key, self.account.secret_key,
                                                                                  requests_params={'timeout': 10})
                self.client = binancer_client.binance_clients[self.account.id]
                bm = BinanceSocketManager(self.client)
                self.binance_socket_manager = bm
                bm.start_user_socket(self.process_trade)
                # if not quotes_maker:
                #    quotes_maker = bm
                #    bm.start_ticker_socket(process_multi_ticker)
                bm.start()

    def process_trade(self, msg: dict):
        # if 'x' not in msg:
        #    return

        if msg['e'] != 'executionReport' or msg['x'] not in ('REJECTED', 'TRADE'):
            return

        if not msg['c'].isdigit() and '_' not in msg['c']:
            return

        if '_' in msg['c']:
            takebuy_mode = True
            order_id = msg['c'].split('_')[0]
        else:
            takebuy_mode = False
            order_id = msg['c']

        try:
            morder_set = Order.objects.filter(id=order_id)
        except ValueError:
            logger.info(f'process_trade: wrong order_id: {order_id}')
            return

        if not morder_set.exists():
            return

        morder: Order = morder_set.first()
        order = next((order for order in self.orders_to_track if hasattr(order, "order") and order.order.id == morder.id), None)

        def error(msg, **kwargs):
            logger.error(msg, strategy=order.strategy, **kwargs)

        def warning(msg, **kwargs):
            logger.warning(msg, strategy=order.strategy, **kwargs)

        def info(msg, **kwargs):
            logger.info(msg, strategy=order.strategy, **kwargs)

        with self.lock:
            # catch open errors
            if msg['x'] == 'REJECTED':
                with lock_instance(morder) as morder:
                    with MakeDirty(morder):
                        if not takebuy_mode:
                            morder.status = OrderStatus.ERROR
                        morder.error_text = msg['r']
                        morder.save()
                error(f'ошибка открытия {"тейк-бай " if takebuy_mode else ""}ордера #{morder.id} {msg["r"]}')
                order_error.send(sender=Order, id=morder.id)

                if order and not takebuy_mode:
                    # del self.orders[self.orders.index(order)]
                    order.mode = TradingMode.Stop

            # catch take-profit
            elif morder.parent_order:
                corder = morder
                setattr(corder, "no_sync", True)
                morder = corder.parent_order
                setattr(morder, "no_sync", True)

                if msg['X'] in ('PARTIALLY_FILLED', 'FILLED'):
                    with lock_instance(morder) as morder:
                        with MakeDirty(morder):
                            if morder.profit is None:
                                morder.profit = 0
                            morder.profit += (float(msg['L']) - morder.open_price) * float(msg['l'])
                            morder.quantity_rest -= float(msg['l'])
                            morder.save()

                    if msg['X'] == 'FILLED':
                        with MakeDirty(corder):
                            corder.status = OrderStatus.CLOSED
                            corder.close_time = datetime.now()
                            corder.save()

                        profit = corder.quantity * (corder.open_price - morder.open_price)
                        info(f'сработал тейк-профит #{morder.id} {cfloat(profit)} {order.symbol} {self.account.name}',
                             extra={'notify': NotifyType.Closed})

                        pending_count = morder.child_orders.filter(status=OrderStatus.PENDING).count()
                        if pending_count == 0:
                            if not isclose(morder.quantity_rest, 0, abs_tol=1e-8):
                                warning(
                                    f'после срабатывания тейк-профита(ов) ордер закрыт не полностью #{morder.id} {order.symbol} {self.account.name}')

                            with lock_instance(morder) as morder:
                                with MakeDirty(morder):
                                    morder.status = OrderStatus.CLOSED
                                    morder.close_time = datetime.now()
                                    morder.save()
                            info(
                                f'ордер закрыт #{morder.id} {cfloat(morder.profit)} {order.symbol} {self.account.name}',
                                extra={'notify': NotifyType.Closed})
                            order.mode = TradingMode.Stop
                            redis_access.hset('orders', morder.id, 0)

                        else:
                            order.takeprofit_order = morder.child_orders.filter(status=OrderStatus.PENDING).order_by(
                                'open_price').first()
                            order.takeprofit_price = order.takeprofit_order.open_price
                            # order.trailing = order.takeprofit_order.trailing_percent

                            # with lock_instance(morder) as morder:
                            #    with MakeDirty(morder):
                            #        morder.trailing_active = order.trailing == 0.0
                            #        morder.trailing_percent = order.trailing
                            #        morder.save()

                        if order.strategy and not order.strategy.simulation:
                            look_limits = BalanceLimits.objects.filter(account=self.account,
                                                                       strategy=order.strategy)
                            if look_limits.exists():
                                limit = look_limits.first()
                                volume = float(msg['Y'])
                                limit.balance += volume
                                limit.save()

    def add_order(self, incoming_order_to_track):
        """
        Добавление ордера в массив для отслеживания в потоках
        """
        order_to_track = OrderToTrack(
            mode=TradingMode.Ready if incoming_order_to_track.status == OrderStatus.ACTIVE else TradingMode.PendingBuy,
            symbol=incoming_order_to_track.symbol.name,
            bot=incoming_order_to_track.bot,
            strategy=incoming_order_to_track.strategy,
            signal=incoming_order_to_track.signal,
            open_price=incoming_order_to_track.open_price,
        )
        if order_to_track.mode == TradingMode.Ready and not incoming_order_to_track.order_id:
            order_to_track.mode = TradingMode.InMarket
        order_to_track.last_mode = order_to_track.mode
        order_to_track.quantity = incoming_order_to_track.quantity_rest
        order_to_track.stoploss_price = incoming_order_to_track.stoploss_price
        order_to_track.pending_stop_time = incoming_order_to_track.pending_stop_time
        order_to_track.trailing = incoming_order_to_track.trailing_percent if incoming_order_to_track.trailing_active else 0.0
        order_to_track.trailing_level = next(
            (takeprofit_level['stop_price'] for takeprofit_level in incoming_order_to_track.takeprofit_levels if takeprofit_level['trailing'] and not takeprofit_level.get('filled')), None)
        try:
            order_to_track.order = Order.objects.get(id=incoming_order_to_track.id)
        except:
            traceback.print_exc()
            logger.info(f'order_id = {incoming_order_to_track.id}')
            return

        order_to_track.tp_mode = 0

        redis_access.hset('orders', incoming_order_to_track.id, 1)

        if incoming_order_to_track.symbol.name not in self.symbols:
            subscribe_symbol(incoming_order_to_track.symbol.name)
            self.symbols.append(incoming_order_to_track.symbol.name)

        corder_set = incoming_order_to_track.child_orders.filter(status=OrderStatus.PENDING).order_by('open_price')
        if corder_set.exists():
            corder: Order = corder_set.first()
            order_to_track.takeprofit_order = corder
            order_to_track.takeprofit_price = corder.open_price
            order_to_track.takeprofit_quantity = corder.quantity
        elif incoming_order_to_track.takeprofit_levels:
            tp = next((lvl for lvl in incoming_order_to_track.takeprofit_levels if 'filled' not in lvl and not lvl.get('hidden')), None)
            if tp:
                order_to_track.tp_mode = 1
                order_to_track.takeprofit_price = tp['stop_price']
                order_to_track.takeprofit_quantity = tp['quantity']
            elif order_to_track.trailing:
                order_to_track.takeprofit_price = 0
            else:
                return

        tb = incoming_order_to_track.takebuy_levels and next((lvl for lvl in incoming_order_to_track.takebuy_levels if 'filled' not in lvl), None)
        if tb:
            order_to_track.takebuy_price = tb['price']

        ap = incoming_order_to_track.autotrade_levels and next((lvl for lvl in incoming_order_to_track.autotrade_levels if 'filled' not in lvl), None)
        order_to_track.autobuy_price = ap and ap['price'] or None

        self.orders_to_track.append(order_to_track)
        # logger.info('out')

    def sync_orders(self):
        """
        Синхронизирую и добавление ордеров в массив для отслеживания в потоках
        """
        with self.lock:
            logger.info(f'Синхронизирую ордера для отслеживания {self.account.name}')
            self.orders_to_track.clear()
            self.symbols.clear()

            # reload orders info
            for morder in Order.objects.filter(Q(account=self.account) &
                                               (Q(status=OrderStatus.ACTIVE) & ~Q(order_id='') & ~Q(order_id__isnull=True) & Q(expert__isnull=True)
                                                | Q(status=OrderStatus.PENDING) & Q(parent_order__isnull=True))):  # type: Order
                self.add_order(morder)

    def sync_order(self, order):
        """
        Синхронизирую и добавление ордеров в массив для отслеживания в потоках
        """
        with self.lock:
            for i in reversed(range(len(self.orders_to_track))):
                if hasattr(self.orders_to_track[i], "order") and self.orders_to_track[i].order.id == order.id:
                    del self.orders_to_track[i]
            if order.status in (OrderStatus.CANCELLED, OrderStatus.CLOSED):
                redis_access.hset('orders', order.id, 0)
                return
            self.add_order(order)

    def open_order(self, symbol: Symbol, bot: Bot, strategy: Strategy, price: float, signal: Signal):
        logger.info("> open order " + self.account.name)

        # subscribe to quotes
        if symbol.name not in self.symbols:
            # logger.info(f'подписка на пару {symbol.name}')
            logger.info("> subscribe")
            subscribe_symbol(symbol.name)
            self.symbols.append(symbol.name)

        if symbol.name not in subscriptions:
            logger.error(f'не удалось открыть ордер по причине отсутствия подписки на пару {symbol.name}',
                         strategy=strategy, signal=signal)
            return

        if bot.one_order_per_symbol:
            has_order = next((order for order in self.orders_to_track
                              if order.mode in (TradingMode.BuyReady, TradingMode.InMarket, TradingMode.Ready)
                              and order.bot
                              and order.bot.id == bot.id
                              and order.symbol == symbol.name), None)
            if has_order:
                logger.warning(f'невозможно открыть ордер, поскольку пара уже в работе {symbol.name} {self.account}',
                               strategy=strategy, signal=signal)
                return

        with self.lock:
            order = OrderToTrack(TradingMode.BuyReady, symbol.name, bot, strategy, signal, price)
            order.tp_mode = strategy.tp_mode
            self.orders_to_track.append(order)

        logger.info("> process tickers")
        self.process_tickers(symbol.name, True)


def process_signal(signal_id):
    """
    Отработка сигнала
    """
    logger.debug(f'Отрабаотываю сигнал')
    connect()

    signal = Signal.objects.get(id=signal_id)

    for auth in signal.bot.bottmauth_set.all():
        try:
            res = TradeMate(auth).make_signal(signal)
        except:
            logger.info('trademate error')

    query = Strategy.objects \
        .exclude(active=False) \
        .filter(assigned_bots=signal.bot) \
        .filter(Q(base_coin='') | Q(base_coin=signal.symbol.base_asset)) \
        .annotate(Count('allowed_symbols')) \
        .filter(Q(allowed_symbols=signal.symbol) | Q(allowed_symbols__count=0)) \
        .exclude(denied_symbols=signal.symbol)

    if not query.exists():
        logger.info('нет стратегий для обработки сигнала', signal=signal)
        return

    for strategy in query:
        logger.info(f'сигнал {signal.bot} стратегия {strategy} символ {signal.symbol} цена {cfloat(signal.price)}', notify=NotifyType.Signal, strategy=strategy)
        if signal.price < strategy.satoshi_filter * 1e-8:
            logger.info(f'{strategy.name}: цена пары меньше допустимой {signal.price} <= {cfloat(strategy.satoshi_filter * 1e-8)}', signal=signal)
            continue
        if signal.symbol.daily_volume < strategy.daily_limit_filter:
            logger.info(f'{strategy.name}: дневной объём пары меньше допустимой {signal.symbol.daily_volume} < {strategy.daily_limit_filter}', signal=signal)
            continue
        if not strategy.frozen:
            for account in strategy.assigned_accounts.all():
                if account.id in clients:
                    clients[account.id].open_order(signal.symbol, signal.bot, strategy, signal.price, signal)
                else:
                    logger.info(f'аккаунт {account.name} не подключен')
        if strategy.guard_enabled and strategy.guard_test_account_id and strategy.frozen:
            if strategy.guard_test_account_id in clients:
                clients[strategy.guard_test_account_id].open_order(signal.symbol, signal.bot, strategy, signal.price, signal)
            else:
                logger.info(f'аккаунт {strategy.guard_test_account.name} не подключен')

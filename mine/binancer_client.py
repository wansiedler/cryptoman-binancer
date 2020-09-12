import json
import logging
import math
import os
import typing

import redis
from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException
from django.db import transaction
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from redis import Redis

from binancer.common import contains_only, lock_instance

# from tools.logger import logger
from cryptoman.models import *

redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_password = os.getenv('REDIS_PASSWORD', '')

if redis_password:
    _r: Redis = redis.client.StrictRedis(host=redis_host, password=redis_password)
else:
    _r: Redis = redis.client.StrictRedis(host=redis_host)

binance_clients: typing.Dict[int, BinanceClient] = {}

# dct
quotes_maker = None

logger = logging.getLogger("")


def connect():
    """
    Подключаем клиентов
    """
    connected = 0
    for account in Account.objects.all().order_by('is_demo'):
        if account.id in binance_clients:
            continue

        if account.is_demo:
            binance_client = None
        else:
            try:
                binance_client = BinanceClient(account.api_key, account.secret_key, requests_params={'timeout': 3})
                logger.debug(f"{account.name} connecting.\n{account.api_key}\n{account.secret_key}")
                connected += connected
            except Exception as e:
                logger.error(f'ошибка подключения к Binance {account.name} - {str(e)}')
                continue

        if not quotes_maker:
            quotes_maker = binance_client

        binance_clients[account.id] = binance_client

    logger.debug(f"CONNECTED {connected} accounts")


def get_balances(account: Account, filter_min: bool = False) -> typing.Dict[str, float]:
    """
    Запрос балансов
    """
    connect()
    logger.debug("Binancer_client get_balances")
    if account.is_demo:
        return account.balance

    if account.id not in binance_clients:
        return {}

    try:
        balances = binance_clients[account.id].get_account()['balances']
    except BinanceAPIException:
        return {}
    if filter_min:
        result = {}
        # all_info = binance_clients[account.id]._get('exchangeInfo')
        all_info = binance_clients[account.id].get_exchange_info()
        for b in balances:
            amount = float(b['free']) + float(b['locked'])
            if amount > 0.0:
                symbol_info = next((s for s in all_info['symbols'] if s['symbol'].startswith(b['asset'])), None)
                if not symbol_info:
                    result[b['asset']] = amount
                    continue
                min_lot = next((float(f['minQty']) for f in symbol_info['filters'] if f['filterType'] == 'LOT_SIZE'), 0)
                if amount > min_lot:
                    result[b['asset']] = amount
    else:
        result = dict([(b['asset'], float(b['free']) + float(b['locked'])) for b in balances])

    return result


def get_quotes():
    logger.debug("binancer_client get_quotes()")
    tickers = _r.hgetall('quotes')
    return dict((str(k, encoding='utf-8'), float(v)) for k, v in tickers.items())


def get_bid(symbol):
    logger.debug("binancer_client get_bid()")
    """
get bid
    :return: bid: float
    """
    price = _r.hget('quotes', symbol)
    return float(str(price, encoding='utf-8'))


def get_ask(symbol):
    # logger.debug("binancer_client get_ask()")
    """
get ask
    :return: ask: float
    """
    price = _r.hget('quotes_ask', symbol)
    return float(str(price, encoding='utf-8'))


def total_btc(account: Account, base_coin='BTC') -> typing.Tuple[float, float]:
    logger.debug("binancer_client total_btc()")
    """ 
get total btc
    :return: balance: Balance
    """
    if not account:
        return 0, 0
    logger.debug("Binancer_client connecting")
    tickers = get_quotes()
    balances = []
    if account.is_demo:
        balances = [{'asset': k, 'free': v, 'locked': 0} for k, v in account.balance.items()]
        # tickers = quotes_maker._client.get_symbol_ticker()
    else:
        if account.id not in binance_clients:
            connect()
        if account.id not in binance_clients:
            return 0, 0
        try:
            balances = binance_clients[account.id].get_account()['balances']
            # logger.debug(f'{format_json(balances)}')
            # tickers = clients[account.id].client.get_symbol_ticker()
        except BinanceAPIException as e:
            logger.warning(e.message)
    free = 0
    locked = 0
    for b in balances:
        asset: str = b['asset']
        if asset == base_coin:
            free += float(b['free'])
            locked += float(b['locked'])
            continue

        price = next((t[1] for t in tickers.items() if t[0] == asset + base_coin), None)
        if price:
            free += float(b['free']) * price
            locked += float(b['locked']) * price
            continue
    return free, locked


def get_futures_balances(account: Account) -> typing.Dict[str, float]:
    futures_balances_binance: typing.Dict[str, float] = {}
    try:
        futures_balances_binance = binance_clients[account.id].futures_account()['assets']
    except BinanceAPIException as e:
        logger.critical(f'{e}')
    return futures_balances_binance


def get_spot_balances(account: Account) -> typing.Dict[str, typing.Dict]:
    balances: typing.Dict[str, typing.Dict] = {}
    spot_balances_binance = binance_clients[account.id].get_account()['balances']

    balances_spot_binance = (balance for balance in spot_balances_binance if
                             float(balance['free']) != 0 or float(balance['locked']) != 0)

    for balance in balances_spot_binance:
        local_balance = {
            'free': 0,
            'locked': 0,
            'price': 0,
            'total': 0
        }
        balances[balance['asset']]: typing.Dict[str, float] = local_balance
        balances[balance['asset']]['free'] = float(balance['free'])
        balances[balance['asset']]['locked'] = float(balance['locked'])
        balances[balance['asset']]['price'] = in_dollar(account, balance['asset'])
        balances[balance['asset']]['total'] += (float(balance['free']) + float(balance['locked'])) * \
                                               balances['spot'][balance['asset']]['price']

    return balances


def get_margin_balances(account: Account) -> typing.Dict[str, typing.Dict]:
    balances: typing.Dict[str, float] = {}

    margin_balances_binance = binance_clients[account.id].get_margin_account()['userAssets']
    balances_margin_binance = (balance for balance in margin_balances_binance if
                               float(balance['free']) != 0 or float(balance['locked']) != 0)

    for balance in balances_margin_binance:
        local_balance = {
            'free': 0,
            'locked': 0,
            'price': 0,
            'total': 0,
            'borrowed': 0,
        }
        balances[balance['asset']]: type.Dict[float] = local_balance
        balances[balance['asset']]['free'] = float(balance['free'])
        balances[balance['asset']]['locked'] = float(balance['locked'])
        balances[balance['asset']]['borrowed'] = float(balance['borrowed'])
        balances[balance['asset']]['price'] = in_dollar(account, balance['asset'])
        balances[balance['asset']]['total'] += (float(balance['free']) + float(balance['locked'])) * \
                                               balances['spot'][balance['asset']]['price']

    return balances


def get_all_balances(account: Account) -> typing.Dict[str, typing.Dict]:
    logger.debug("binancer_client all_balances()")

    balances: typing.Dict[str, typing.Dict] = {
        'spot': {},
        'margin': {},
        'futures': {}
    }

    tickers = get_quotes()
    if account.id not in binance_clients:
        connect()

    balances['spot'] = get_spot_balances(account)
    balances['margin'] = get_margin_balances(account)
    balances['futures'] = get_futures_balances(account)

    return balances


def get_account_status(account: Account) -> float:
    if account.id not in binance_clients:
        connect()
    account_status = binance_clients[account.id].get_account_status()
    return account_status


def in_dollar(account: Account, symbol) -> float:
    if len(symbol) <= 4:
        symbol += "USDT"
    symbol_price = 0
    try:
        symbol_price = binance_clients[account.id].get_symbol_ticker(symbol=symbol)['price']
    except BinanceAPIException as e:
        logger.exception(e + symbol)
    finally:
        return float(symbol_price)


def check_in_work(order_id):
    logger.debug("binancer_client check_in_work()")
    """
get ask
    :return: inwork: bool
    """
    return bool(_r.hget('orders', order_id))


def account_buy(account: Account, symbol: Symbol, quantity: float, price: float):
    """
buy asset and update account balance
    :return:
    """
    # check side
    buy = ''
    sell = ''
    for asset in account.balance:
        if symbol.name.startswith(asset):
            buy = asset
            sell = symbol.name[len(asset):]
            break

    if not buy:
        return

    with lock_instance(account) as account:
        if buy not in account.balance:
            account.balance[buy] = 0.0
        account.balance[buy] += quantity
        if sell not in account.balance:
            account.balance[sell] = 0.0
        account.balance[sell] -= quantity * price
        account.save()


def calc_commission(instance: Order, result: dict, volume) -> float:
    """
calculate the commission
    :return: commission: float
    """

    def trunc_lot(lot):
        return math.floor(lot / instance.symbol.min_lot) * instance.symbol.min_lot

    commission = 0
    for fill in result['fills']:
        asset = fill['commissionAsset']
        qty = float(fill['commission'])
        if instance.symbol.name.startswith(asset):
            original = instance.quantity
            instance.quantity = trunc_lot(instance.quantity - qty)
            instance.takeprofit_levels[-1]['quantity'] -= original - instance.quantity
            commission += qty * float(fill['price'])
        elif instance.symbol.base_asset == asset:
            commission += qty
        elif asset == 'BNB':
            commission += volume * 0.00075
    return commission


def _r_publish(channel, data):
    _r.rpush('binance', json.dumps({
        'channel': channel,
        'data': data
    }))


def process_signal(signal: Signal):
    _r_publish('signal', signal.id)


def sync_orders(account_id):
    _r_publish('sync', account_id)


def sync_order(order_id):
    _r_publish('sync_order', order_id)


def trade(command, order_id: int, quantity, sync: bool):
    _r_publish('trade', [command, order_id, quantity, sync])


def account_reconnect(account_id):
    _r_publish('reconnect', account_id)


def smart_order(order_id):
    _r_publish('smart_order', order_id)


@receiver(post_save, sender=Signal)
def signal_post_save(sender, created: bool, instance: Signal, **kwargs):
    if created:
        if not transaction.get_connection().in_atomic_block:
            process_signal(instance)
        else:
            transaction.on_commit(lambda: process_signal(instance))


@receiver(pre_save, sender=Order)
def order_pre_save(sender, instance: Order, **kwargs):
    if not instance.original_price and instance.open_price:
        instance.original_price = instance.open_price


@receiver(post_save, sender=Order)
def order_post_save(sender, created: bool, instance: Order, **kwargs):
    # if not instance.order_id:
    #    logger.info(f'запись ордера без кода {instance.__dict__}', order=instance)

    if is_dirty(instance):
        return

    if not hasattr(instance, 'partial_quantity') and contains_only(instance.changed_fields,
                                                                   ['stoploss_price', 'pending_stop_time']):
        return

    sync = not hasattr(instance, "no_sync")

    if instance._ModelDiffMixin__initial['status'] in (
            OrderStatus.ACTIVE, OrderStatus.CLOSED, OrderStatus.PENDING) and instance.status in (
            OrderStatus.ACTIVE, OrderStatus.CLOSED):
        if instance.status == OrderStatus.ACTIVE:
            if hasattr(instance, 'partial_quantity'):
                trade('CLOSE_ORDER', instance.id, instance.partial_quantity, sync)
            else:
                trade('OPEN_ORDER', instance.id, 0, sync)
        else:
            if instance.order_id:
                trade('CLOSE_ORDER', instance.id, instance.quantity_rest, sync)

    elif instance._ModelDiffMixin__initial['status'] == OrderStatus.PENDING and instance.status in (
            OrderStatus.PENDING, OrderStatus.CANCELLED) and \
            intersect(instance.changed_fields, ['quantity', 'open_price', 'status', 'id']):

        # cancel is manual, no need to share async task
        trade('CANCEL_ORDER', instance.id, 0, sync)

        if instance.status == OrderStatus.PENDING:
            trade('PENDING_ORDER', instance.id, 0, sync)


@receiver(post_delete, sender=Order)
def order_post_delete(sender, instance: Order, **kwargs):
    if instance.status == OrderStatus.ACTIVE or instance.status == OrderStatus.PENDING:
        sync_orders(instance.account_id)


@receiver(post_save, sender=Strategy)
def strategy_post_save(sender, created, instance: Strategy, **kwargs):
    if instance.balance_limit_mode == 0:
        instance.balancelimits_set.all().delete()
    else:
        def round_lot(lot):
            return round(lot / 1e-8) * 1e-8

        exclude_accounts = instance.balancelimits_set.all().values_list('account_id', flat=True)
        for account in instance.assigned_accounts.exclude(id__in=exclude_accounts):  # type: Account
            balance: dict = get_balances(account, True)
            account.balance = balance
            account.save()

            limit_balance = dict(
                [(cur, round_lot(amount * instance.balance_limit_amount / 100)) for cur, amount in balance.items() if
                 amount > 0.0])
            BalanceLimits.objects.create(strategy=instance, account=account, balance=limit_balance)


@receiver(pre_save, sender=Account)
def account_pre_save(sender, instance: Account, **kwargs):
    if instance.is_demo:
        if not instance.balance:
            connect()
            info = quotes_maker.get_account()['balances']
            instance.balance = dict((s['asset'], 0.0) for s in info)
        else:
            instance.balance: Account.balance
            for k, v in instance.balance.items():
                if v == '':
                    instance.balance[k] = 0


@receiver(post_save, sender=Account)
def account_post_save(sender, instance: Account, **kwargs):
    if not instance.is_demo:
        if instance.id in binance_clients:
            connect()
            sync_orders(instance.id)
        else:
            binance_clients[instance.id] = BinanceClient(instance.api_key, instance.secret_key)
            account_reconnect(instance.id)

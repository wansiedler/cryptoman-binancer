import json
import math
import typing

import redis
from binance.exceptions import BinanceAPIException
from django.db import transaction
from django.db.models.signals import post_save, post_delete, pre_save
from django.dispatch import receiver
from binance.client import Client as BClient

from cryptoman.models import *
from tools import logger
from tools.common import contains_only, lock_instance

_r = redis.Redis()

clients: typing.Dict[int, BClient] = {}
quotes_maker: BClient = None


def connect():
    global quotes_maker

    for account in Account.objects.all().order_by('is_demo'):
        if account.id in clients:
            continue

        if account.is_demo:
            bclient = None
        else:
            try:
                bclient = BClient(account.api_key, account.secret_key, requests_params={'timeout': 3})
            except Exception as e:
                logger.error(f'ошибка подключения к Binance {account.name} - {str(e)}')
                continue

        if not quotes_maker:
            quotes_maker = bclient
        clients[account.id] = bclient


def get_balances(account: Account, filter_min: bool = False) -> typing.Dict[str, float]:
    connect()
    if account.is_demo:
        return account.balance

    if account.id not in clients:
        return {}

    try:
        balances = clients[account.id].get_account()['balances']
    except BinanceAPIException:
        return {}
    if filter_min:
        result = {}
        all_info = clients[account.id]._get('exchangeInfo')
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
    tickers = _r.hgetall('quotes')
    return dict((str(k, encoding='utf-8'), float(v)) for k,v in tickers.items())


def get_bid(symbol):
    price = _r.hget('quotes', symbol)
    return float(str(price, encoding='utf-8'))


def get_ask(symbol):
    price = _r.hget('quotes_ask', symbol)
    return float(str(price, encoding='utf-8'))


def check_in_work(order_id):
    return bool(_r.hget('orders', order_id))


def total_btc(account: Account, base_coin = 'BTC') -> typing.Tuple[float, float]:
    tickers = get_quotes()

    if account.is_demo:
        balances = [{'asset': k, 'free': v, 'locked': 0} for k, v in account.balance.items()]
        #tickers = quotes_maker._client.get_symbol_ticker()
    else:
        if account.id not in clients:
            connect()
        if account.id not in clients:
            return 0, 0
        balances = clients[account.id].get_account()['balances']
        #tickers = clients[account.id].client.get_symbol_ticker()

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

        price = next((t[1] for t in tickers.items() if t[0] == base_coin + asset), None)
        if price:
            free += float(b['free']) / price
            locked += float(b['locked']) / price

    return free, locked


def account_buy(account: Account, symbol: Symbol, quantity: float, price: float):
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
        account.balance[sell] -= quantity*price
        account.save()


def calc_commission(instance: Order, result: dict, volume):
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
    #if not instance.order_id:
    #    logger.info(f'запись ордера без кода {instance.__dict__}', order=instance)

    if is_dirty(instance):
        return

    if not hasattr(instance, 'partial_quantity') and contains_only(instance.changed_fields, ['stoploss_price', 'pending_stop_time']):
        return

    sync = not hasattr(instance, "no_sync")

    if instance._ModelDiffMixin__initial['status'] in (OrderStatus.ACTIVE, OrderStatus.CLOSED, OrderStatus.PENDING) and instance.status in (OrderStatus.ACTIVE, OrderStatus.CLOSED):
        if instance.status == OrderStatus.ACTIVE:
            if hasattr(instance, 'partial_quantity'):
                trade('CLOSE_ORDER', instance.id, instance.partial_quantity, sync)
            else:
                trade('OPEN_ORDER', instance.id, 0, sync)
        else:
            if instance.order_id:
                trade('CLOSE_ORDER', instance.id, instance.quantity_rest, sync)

    elif instance._ModelDiffMixin__initial['status'] == OrderStatus.PENDING and instance.status in (OrderStatus.PENDING, OrderStatus.CANCELLED) and \
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

            limit_balance = dict([(cur, round_lot(amount*instance.balance_limit_amount/100)) for cur, amount in balance.items() if amount > 0.0])
            BalanceLimits.objects.create(strategy=instance, account=account, balance=limit_balance)


@receiver(pre_save, sender=Account)
def account_pre_save(sender, instance: Account, **kwargs):
    if instance.is_demo:
        if not instance.balance:
            connect()
            info = quotes_maker.get_account()['balances']
            instance.balance = dict((s['asset'], 0.0) for s in info)
        else:
            for k, v in instance.balance.items():
                if v == '':
                    instance.balance[k] = 0


@receiver(post_save, sender=Account)
def account_post_save(sender, instance: Account, **kwargs):
    if not instance.is_demo:
        if instance.id in clients:
            connect()
            sync_orders(instance.id)
        else:
            clients[instance.id] = BClient(instance.api_key, instance.secret_key)
            account_reconnect(instance.id)


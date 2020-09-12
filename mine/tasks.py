import logging
import time
from datetime import datetime
from math import isclose

from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, TIME_IN_FORCE_GTC, ORDER_TYPE_LIMIT
from binance.exceptions import BinanceAPIException
from django.db import transaction

from binancer.binancer import BinanceClient, subscriptions
from binancer.binancer_client import account_buy, calc_commission, get_quotes, trade
from binancer.common import MakeDirty, lock_instance, format_json, format_stacktrace, cfloat
from cryptoman.models import *
from .signals import *

''' I don't enable order locking for pending orders yet, because of Binance queue promises
order_locks: typing.Dict[int, threading.Lock] = {}


def parent_lock(order: Order):
    order_id = order.parent_order_id if order.parent_order else order.id
    if order_id not in order_locks:
        order_locks[order_id] = threading.Lock()
    return order_locks[order_id]
'''

# from tools import logger
logger = logging.getLogger("")


# l = logging.getLogger("connectors.binancer.tasks")


def open_order(order_id: int, client: BinanceClient):
    logger.debug(f'Opening order on Binance')
    instance: Order = Order.objects.get(id=order_id)
    try:
        side = SIDE_BUY if instance.order_side == 0 else SIDE_SELL
        try:
            if not instance.account.is_demo:
                if not instance.simulation:
                    logger.debug(
                        f'TRYING {instance.symbol.name} | {side} | {cfloat(instance.quantity)}({instance.quantity})')
                    result = client.order_market(
                        symbol=instance.symbol.name,
                        side=side,
                        quantity=cfloat(instance.quantity),
                        newOrderRespType='FULL',
                        newClientOrderId=str(order_id),
                    )
                    logger.debug(f'RESULT? #{instance.id} {format_json(result)}\n')
                else:
                    logger.debug(
                        f'TEST {ORDER_TYPE_MARKET} | {instance.symbol.name} | {side} | {cfloat(instance.quantity)}({instance.quantity})')
                    result = client.create_test_order(
                        symbol=instance.symbol.name,
                        type=ORDER_TYPE_MARKET,
                        # timeInForce=TIME_IN_FORCE_GTC,
                        side=side,
                        quantity=cfloat(instance.quantity),
                        newOrderRespType='FULL',
                    )
        except BinanceAPIException as e:
            # market exception being processed in trading thread
            if instance.simulation or instance.account.is_demo:
                with lock_instance(instance) as instance:
                    with MakeDirty(instance):
                        instance.status = OrderStatus.ERROR
                        instance.error_text = str(e)
                        instance.save()
                mess = f'ERROR OPENNING #{instance.id} {instance.symbol.name} {instance.account.name} {e.message}\n{format_json(result)}'
                logger.error(mess, order=instance)
                order_error.send(sender=Order, id=order_id)
            exit(1)
        else:
            logger.info(f'BOUGHT #{instance.id} {format_json(result)}\n'
                        , order=instance
                        )
            if not instance.simulation and not instance.account.is_demo:
                filled = float(result['executedQty'])
                if not isclose(instance.quantity, filled, abs_tol=1e-8):
                    # logger.warning(f'купленный объём отличается от требуемого #{instance.id} {cfloat(instance.quantity)} <> {cfloat(filled)}', order=instance)
                    logger.warning(
                        f'BOUGHT! DIFF VOLUME! #{instance.id} {cfloat(instance.quantity)} <> {cfloat(filled)}\n'
                        # f'{format_json(result)}'
                        , order=instance
                    )
                    instance.quantity = filled
                    # instance.save()

                price = float(result['cummulativeQuoteQty']) / filled
                if not isclose(price, instance.open_price, abs_tol=1e-8):
                    logger.warning(
                        f'!BOUGHT! DIFF PRICE! #{instance.id} {cfloat(price)} <> {cfloat(instance.open_price)}\n'
                        # f'{format_json(result)}'
                        , order=instance
                    )
                    instance.open_price = price

                with lock_instance(instance) as instance:
                    with MakeDirty(instance):
                        instance.order_id = result['orderId']
                        # if 'USDT' in instance.symbol.name:
                        #     logger.debug('here')
                        commission = calc_commission(instance, result, instance.volume)
                        instance.quantity_rest = instance.quantity
                        instance.commission += commission
                        instance.save()

            else:
                logger.info(f'else')
                filled = instance.quantity
                price = instance.open_price
                with lock_instance(instance) as instance:
                    with MakeDirty(instance):
                        instance.order_id = f'SIM{instance.id}'
                        instance.save()

            # recount profit levels
            stop_levels = instance.takeprofit_levels
            # hide levels
            for level in stop_levels:
                if level['stop_price'] <= price:
                    logger.info(f'уровень ТП {cfloat(level["stop_price"])} скрыт'
                                , order=instance
                                )
                    level['hidden'] = True

            logger.debug(f'{stop_levels}')

            # shift levels
            for i in range(len(stop_levels)):
                if stop_levels[i].get('hidden'):
                    for k in reversed(range(len(stop_levels) - 1)):
                        p = stop_levels[k + 1]['stop_price']
                        h = stop_levels[k + 1]['hidden']
                        stop_levels[k + 1].update(stop_levels[k])
                        stop_levels[k + 1]['stop_price'] = p
                        stop_levels[k + 1]['hidden'] = h
                    stop_levels[i]['quantity'] = 0
                    stop_levels[i]['trailing'] = 0
                    stop_levels[i]['step_stop'] = False

            # wipe levels
            for lvl in stop_levels:
                if not lvl.get('hidden') and not lvl['quantity']:
                    stop_levels.remove(lvl)

            '''
            for lvl in stop_levels:
                if lvl['stop_price'] < ask and not lvl.get('hidden'):
                    warning(f'уровень ТП {cfloat(lvl["stop_price"])} ниже цены открытия {cfloat(
                        ask)}. Ордер не будет закрыт полностью!')
                    lvl['hidden'] = True
            '''

            # check levels
            for level in stop_levels:
                if level.get('hidden'):
                    continue
                if not level['trailing'] or level['quantity'] > 0:
                    if level['quantity'] < instance.symbol.min_lot:
                        logger.warning(
                            f'объём уровня (тейк-профит) {cfloat(level["stop_price"])} меньше допустимого минимума {cfloat(level["quantity"])} < {cfloat(instance.symbol.min_lot)}'
                            , order=instance
                        )
                        stop_levels.remove(level)
                    elif level['quantity'] * level['stop_price'] < instance.symbol.min_notional:
                        logger.warning(
                            f'объём уровня (тейк-профит) {cfloat(level["stop_price"])} меньше допустимого минимума {cfloat(level["quantity"] * level["stop_price"])} < {cfloat(instance.symbol.min_notional)}',
                            order=instance)
                        stop_levels.remove(level)

            # align levels
            if len(stop_levels) == 1 and stop_levels[0]['quantity'] > 0:
                stop_levels[0]['quantity'] = filled
            elif len(stop_levels) > 1:
                idx = len(stop_levels) - 1
                while idx >= 0:
                    if stop_levels[idx]['quantity'] > 0:
                        break
                if idx >= 0:
                    stop_levels[idx]['quantity'] = filled - sum(
                        [level['quantity'] for level in stop_levels[:idx]])

            # update
            with lock_instance(instance) as instance:
                with MakeDirty(instance):
                    instance.takeprofit_levels = stop_levels
                    instance.save()

            if instance.strategy:
                logger.debug(f'if instance.strategy')
                with transaction.atomic():
                    look_limits = BalanceLimits.objects.filter(account=instance.account, strategy=instance.strategy)
                    if look_limits.exists():
                        limit = look_limits.first()
                        volume = float(result['cummulativeQuoteQty'])
                        limit.balance -= volume
                        limit.save()

            if instance.account.is_demo:
                account_buy(instance.account, instance.symbol, instance.quantity, instance.open_price)

            logger.info(f"OPENED =) #{instance.id}"
                        , order=instance
                        )

            order_opened.send(sender=Order, id=order_id)
    except Exception as e:
        stacktrace = format_stacktrace()
        mess = f'ERROR openning the order #{instance.id} {e}\n{format_json(result)} {stacktrace}'
        logger.error(mess, order=instance)


def add_order(order_id: int, client: BinanceClient, quantity: float):
    instance: Order = Order.objects.get(id=order_id)
    # if True:
    try:
        side = SIDE_BUY if instance.order_side == 0 else SIDE_SELL
        try:
            if not instance.account.is_demo:
                if not instance.simulation:
                    result = client.order_market(
                        symbol=instance.symbol.name,
                        side=side,
                        quantity=cfloat(abs(quantity)),
                        newOrderRespType='FULL',
                        newClientOrderId=f'{order_id}_{instance.takebuy_counter + 1}',
                    )
                else:
                    result = client.create_test_order(
                        symbol=instance.symbol.name,
                        type=ORDER_TYPE_MARKET,
                        # timeInForce=TIME_IN_FORCE_GTC,
                        side=side,
                        quantity=cfloat(abs(quantity)),
                        newOrderRespType='FULL',
                    )
        except BinanceAPIException as e:
            # market exception being processed in trading thread
            # if instance.simulation or instance.account.is_demo:
            if True:
                with lock_instance(instance) as instance:
                    with MakeDirty(instance):
                        # instance.status = OrderStatus.ERROR
                        instance.error_text = str(e)
                        instance.save()
                logger.error(f'ERROR adding order #{instance.id} {e.message}\n'
                             f'{format_json(result)}'
                             , order=instance
                             )


        else:
            with lock_instance(instance) as instance:
                with MakeDirty(instance):
                    if not instance.simulation and not instance.account.is_demo:
                        filled = float(result['executedQty'])
                        if not isclose(quantity, filled, abs_tol=1e-8):
                            logger.warning(
                                f'купленный объём отличается от требуемого #{instance.id} {cfloat(quantity)} <> {cfloat(filled)}'
                                , order=instance
                            )
                            # instance.save()

                        volume = float(result['cummulativeQuoteQty'])
                        price = float(result['cummulativeQuoteQty']) / filled

                        instance.order_id = result['orderId']
                        commission = calc_commission(instance, result, volume)

                        instance.commission += commission

                    else:
                        filled = abs(quantity)
                        price = get_quotes()[instance.symbol.name]
                        volume = filled * price
                        # instance.order_id = f'SIM{instance.id}'

                    instance.open_price = instance.symbol.round_tick(
                        (instance.open_price * instance.quantity_rest + price * filled) / (
                                instance.quantity_rest + filled))

                    levels = [lvl for lvl in instance.takeprofit_levels if not hasattr(lvl, "filled")]
                    if quantity > 0:
                        # recalc tp quantity
                        for lvl in levels:
                            lvl['quantity'] = instance.symbol.round_lot(
                                lvl['quantity'] * (instance.quantity_rest + filled) / instance.quantity_rest)

                        if instance.is_smart:
                            # levels move
                            hidden_index = None
                            for i in reversed(range(len(instance.takeprofit_levels))):
                                lvl = instance.takeprofit_levels[i]
                                if not lvl.get('hidden') and lvl['quantity'] != 0:
                                    continue
                                if lvl['stop_price'] > price:
                                    hidden_index = i
                            if hidden_index is not None:
                                for i in reversed(range(hidden_index, len(instance.takeprofit_levels) - 1)):
                                    instance.takeprofit_levels[i + 1]['stop_price'] = instance.takeprofit_levels[i][
                                        'stop_price']
                                del instance.takeprofit_levels[hidden_index]
                                # logger.info(f'выполнен сдвиг уровней ТП на {cfloat(instance.takeprofit_levels[hidden_index]["stop_price"])}', order=instance)
                                logger.info(
                                    f'выполнен сдвиг уровней ТП на {cfloat(instance.takeprofit_levels[hidden_index]["stop_price"])}')

                        elif instance.strategy and instance.strategy.autotrade_mode:
                            # realign tp price levels
                            for lvl in instance.takeprofit_levels:
                                lvl['stop_price'] = instance.symbol.round_tick(
                                    instance.open_price * (100 + lvl['percent']) / 100)

                    else:
                        lvl = dict(
                            stop_price=instance.open_price,
                            quantity=instance.quantity_rest,
                            sl_delta=None,
                            trailing=0.0,
                            step_stop=False
                        )
                        instance.takeprofit_levels = [lvl]

                    instance.quantity += filled
                    instance.quantity_rest += filled
                    instance.volume += volume

                    if len(levels) > 1:
                        levels[-1]['quantity'] = instance.quantity_rest - sum(lvl['quantity'] for lvl in levels[:-1])

                    instance.takebuy_counter += 1
                    instance.save()

            if instance.strategy:
                with transaction.atomic():
                    look_limits = BalanceLimits.objects.filter(account=instance.account, strategy=instance.strategy)
                    if look_limits.exists():
                        limit = look_limits.first()
                        limit.balance -= volume
                        limit.save()

            if instance.account.is_demo:
                account_buy(instance.account, instance.symbol, abs(instance.quantity), instance.open_price)

            # logger.info(f'ADDED ORDER#{instance.id}', order=instance)
            logger.info(f'ADDED ORDER#{instance.id}')
            order_added.send(sender=Order, id=instance.id)

    except Exception as e:
        stacktrace = format_stacktrace()
        mess = f'ERROR adding the order #{instance.id} {e}\n{format_json(result)} {stacktrace}'
        logger.error(mess, order=instance)
        order_error.send(sender=Order, id=order_id)


def close_order(order_id: int, client: BinanceClient, quantity: float):
    result = {}
    try:
        instance: Order = Order.objects.get(id=order_id)
        side = SIDE_SELL if instance.order_side == 0 else SIDE_BUY
        try:
            if not instance.account.is_demo:
                if not instance.simulation:
                    result = client.order_market(
                        symbol=instance.symbol.name,
                        side=side,
                        quantity=cfloat(quantity),
                        newOrderRespType='FULL',
                    )
                else:
                    result = client.create_test_order(
                        symbol=instance.symbol.name,
                        type=ORDER_TYPE_MARKET,
                        # timeInForce=TIME_IN_FORCE_GTC,
                        side=side,
                        quantity=cfloat(quantity),
                        newOrderRespType='RESULT',
                    )
            else:
                result = {
                    'price': subscriptions[instance.symbol.name].bid,
                    'fills': [],
                    'cummulativeQuoteQty': quantity,
                }
        except BinanceAPIException as e:
            with MakeDirty(instance):
                with lock_instance(instance) as instance:
                    instance.status = OrderStatus.ERROR
                    instance.error_text = str(e)
                    instance.save()
            # logger.error(f'ERROR CLOSING #{instance.id} in close_order {e.message} {format_json(result)}', order=instance)
            logger.error(f'ERROR CLOSING #{instance.id} in close_order {e.message} {format_json(result)}')
            order_error.send(sender=Order, id=order_id)
        else:
            last_sell = False
            with lock_instance(instance) as instance:
                with MakeDirty(instance):
                    instance.quantity_rest -= quantity
                    price = float(result['price']) if instance.account.is_demo and float(result['price']) \
                        else sum(float(fill['price']) * float(fill['qty']) for fill in result['fills']) / sum(
                        float(fill['qty']) for fill in result['fills'])
                    if isclose(instance.quantity_rest, 0, abs_tol=1e-8) or instance.quantity_rest < 0:
                        instance.order_id = ''
                        instance.close_time = datetime.now()
                        instance.close_price = price
                        instance.quantity_rest = 0
                        instance.status = OrderStatus.CLOSED
                        last_sell = True

                    # if instance.account.is_demo or instance.simulation:
                    if instance.profit is None:
                        instance.profit = 0
                    instance.profit += (price - instance.open_price) * quantity

                    # price = float(result['cummulativeQuoteQty']) / float(result['executedQty'])
                    if not instance.simulation:
                        commission = calc_commission(instance, result, price * quantity)
                        instance.commission += commission
                    instance.save()

            if not instance.simulation and instance.strategy:
                look_limits = BalanceLimits.objects.filter(account=instance.account, strategy=instance.strategy)
                if look_limits.exists():
                    limit = look_limits.first()
                    volume = float(result['cummulativeQuoteQty'])
                    limit.balance += volume
                    limit.save()

            if instance.account.is_demo:
                account_buy(instance.account, instance.symbol, -quantity, result['price'])

            if not last_sell:
                logger.info(
                    f'ордер успешно уменьшен на {cfloat(quantity)}, остаток {cfloat(instance.quantity_rest)}, средняя цена: {cfloat(price)}',
                    order=instance)
            else:
                logger.info(f'CLOSED #{instance.id}, AVG PRICE: {cfloat(price)}', order=instance)

                # guard
                logger.debug('!GUARDING!')
                if instance.strategy and instance.strategy.guard_enabled:
                    with lock_instance(instance.strategy) as strategy:  # type: Strategy
                        if strategy.frozen != instance.account.balance_control:
                            if instance.profit < 0:
                                StrategyGuardLog(strategy=instance.strategy, order=instance, move=-1).save()
                                strategy.guard_counter = strategy.guard_counter - 1 if strategy.guard_counter < 0 else -1
                            elif instance.profit > 0:
                                StrategyGuardLog(strategy=instance.strategy, order=instance, move=1).save()
                                strategy.guard_counter = strategy.guard_counter + 1 if strategy.guard_counter > 0 else 1
                        if not strategy.frozen and -strategy.guard_counter > strategy.guard_loss_limit:
                            strategy.frozen = True
                            strategy.guard_last_update = datetime.now()
                            strategy.guard_counter = 0
                            strategy.save()
                            logger.info(f'#{order_id} инициировал защиту стратегии {strategy.name}', order=instance,
                                        guard=True)
                            for o in Order.objects.filter(status=OrderStatus.ACTIVE,
                                                          strategy=strategy):  # .values('id', 'quantity_rest'):
                                logger.info(f'ордер закрывается защитником стратегии ({instance.id})', order=o)
                                trade('CLOSE_ORDER', o.id, o.quantity_rest, True)
                                time.sleep(0.2)
                        elif strategy.frozen and strategy.guard_counter > strategy.guard_profit_pass:
                            strategy.frozen = False
                            strategy.guard_last_update = datetime.now()
                            strategy.guard_counter = 0
                            strategy.save()
                            logger.info(f'#{order_id} снимает защиту стратегии {strategy.name}', order=instance,
                                        guard=True)
                            for o in Order.objects.filter(status=OrderStatus.ACTIVE, strategy=strategy,
                                                          account=instance.account):  # .values('id', 'quantity_rest'):
                                logger.info(f'ордер закрывается защитником стратегии ({instance.id})', order=o)
                                trade('CLOSE_ORDER', o.id, o.quantity_rest, True)
                                time.sleep(0.11)
                        else:
                            strategy.save()
                logger.debug('!GUARD wokring!')
            order_closed.send(sender=Order, id=order_id)
    except Exception as e:
        stacktrace = format_stacktrace()
        mess = f'ERROR CLOSING #{instance.id} {e} {format_json(result)} {stacktrace}'
        logger.error(mess, order=instance)
        order_error.send(sender=Order, id=order_id)
        return


def open_pending(order_id: int, client: BinanceClient):
    instance = Order.objects.get(id=order_id)
    side = SIDE_BUY if instance.order_side == 0 else SIDE_SELL
    mess = f"PENDING ORDER on binance #{instance.id} | {side} | {instance.symbol.name} | {cfloat(instance.quantity)}\n"
    try:
        if not instance.account.is_demo and instance.parent_order_id:
            mess += f' {instance.account.is_demo} {instance.parent_order_id}'
            if not instance.simulation:
                mess += ' (REAL)'
                result = client.order_limit(
                    symbol=instance.symbol.name,
                    side=side,
                    quantity=cfloat(instance.quantity),
                    price=instance.open_price,
                    newOrderRespType='RESULT',
                    newClientOrderId=str(order_id),
                )
            else:
                mess += ' (TEST ORDER )'
                result = client.create_test_order(
                    symbol=instance.symbol.name,
                    type=ORDER_TYPE_LIMIT,
                    timeInForce=TIME_IN_FORCE_GTC,
                    side=side,
                    quantity=cfloat(instance.quantity),
                    price=instance.open_price,
                    newOrderRespType='RESULT',
                )
        else:
            mess += f' {instance.account.is_demo} {instance.parent_order_id}'
            mess += ' (SIMUL)'
            result = {'orderId': f'SIM{instance.id}'}

    except BinanceAPIException as e:
        with lock_instance(instance) as instance:
            with MakeDirty(instance):
                instance.status = OrderStatus.ERROR
                instance.error_text = str(e)
                instance.save()

        stacktrace = format_stacktrace()
        mess = f'ERROR openning pending the order #{instance.id} {e.message} {stacktrace}'
        logger.error(mess, order=instance)
        # logger.error(f'ERROR openning pending the order #{instance.id} {e.message}')
    else:
        with lock_instance(instance) as instance:
            with MakeDirty(instance):
                instance.order_id = result['orderId'] if not instance.simulation else f'SIM{instance.id}'
                instance.save()
        logger.debug(mess)
        logger.debug(result)
        # logger.info(mess, order=instance)
        logger.info(mess)


def cancel_order(order_id: int, client: BinanceClient):
    instance = Order.objects.get(id=order_id)
    if instance.order_id:
        if not instance.simulation and not instance.account.is_demo and instance.parent_order_id:
            try:
                result = client.cancel_order(
                    symbol=instance.symbol.name,
                    # orderId=instance.order_id,
                    origClientOrderId=str(order_id),
                )
            except Exception as e:
                stacktrace = format_stacktrace()
                mess = f'ERROR cancelling limit order #{instance.id} {instance.order_id} {e.message} {stacktrace}'
                logger.error(mess)

        with MakeDirty(instance):
            instance.order_id = ''
            instance.save()

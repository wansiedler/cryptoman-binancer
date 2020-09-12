import json
import logging
import os
import time
from datetime import datetime

import redis
from django.db.models import Count, Q
from redis import Redis
import logging.handlers

l = logging.getLogger('')

redis_host = os.getenv('REDIS_HOST', 'localhost')
redis_password = os.getenv('REDIS_PASSWORD', '')

_r: Redis = redis.client.StrictRedis(host=redis_host, password=redis_password, socket_connect_timeout=3, socket_timeout=3, retry_on_timeout=True,
                                     socket_keepalive=True, encoding_errors='ignore')


class TGHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        # l.debug(f'Log record {record}')
        from cryptoman.models import NotifyRecipients, NotifyType
        import telegramer
        if hasattr(record, 'notify'):
            if record.notify == NotifyType.Closed:
                levels = [1, 3]
            else:
                levels = [record.notify.value - 1]
        elif record.levelno == logging.ERROR:
            levels = [2]
        else:
            levels = [1]

        if hasattr(record, 'guard'):
            levels.append(4)

        if hasattr(record, 'sl'):
            levels.append(5)

        if hasattr(record, 'prev_tb') and record.prev_tb:
            levels.append(7)

        channel_name = ''
        if hasattr(record, 'channel'):
            # channel_name = f'#{record.channel} | '
            levels.append(6)
            if hasattr(record, "order") and record.order and record.levelno == logging.ERROR:
                from cryptoman.models import Order
                from binancer.signals import order_error

                order_error.send(sender=Order, id=record.order.id)

        elif hasattr(record, "order") and record.order:
            # channel_name = f'#{record.order.id} | {record.order.account.name} | '
            pass

        recipients = NotifyRecipients.objects.filter(level__in=levels, active=True)
        if hasattr(record, 'strategy') or hasattr(record, "order") and record.order:
            strategy = getattr(record, 'strategy', None) or record.order.strategy
            if strategy:
                recipients = recipients.annotate(Count('strategies')).filter(
                    Q(strategies=strategy) | Q(strategies__count=0))
        else:
            recipients = recipients.annotate(Count('strategies')).filter(strategies__count=0)
        recipients = recipients.values_list('contact', flat=True)
        if recipients.exists():
            # recipients = ';'.join(recipients)
            message = self.format(record)
            if message:
                # l.debug('Sending {} {}'.format(recipients, (channel_name + message)))
                telegramer.send_message(recipients, channel_name + message)


class TradeLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        from cryptoman.models import TradeLog
        if hasattr(record, 'signal') or hasattr(record, 'order'):
            # l.debug('Trade logged!')
            TradeLog.objects.create(
                datetime=datetime(*time.localtime(record.created)[:6]),
                signal=getattr(record, 'signal', None),
                order=getattr(record, 'order', None),
                message=self.format(record),
            )

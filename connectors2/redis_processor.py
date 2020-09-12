import random
import string

from django.core.handlers.wsgi import WSGIRequest
from django.utils.safestring import mark_safe

from core import settings
from connectors.binancer_client import total_btc, _r
from tools.format import cfloat

def processor(request: WSGIRequest):
    protocol = (request.is_secure() or request.META.get('HTTP_X_FORWARDED_HOST', None)) and 'wss://' or 'ws://'
    balances = total_btc(request.user.account) if request.user.is_authenticated and request.user.account else None
    if request.user.is_authenticated:
        token = _r.hget('private.users', request.user.id)
        if not token:
            token = ''.join(random.choice(string.ascii_letters) for _ in range(32))
            _r.hset('private.users', request.user.id, token)
            _r.hset('private.tokens', token, request.user.id)
        else:
            token = str(token, encoding="utf-8")
    else:
        token = ""
    return {
        'ws_location': mark_safe(f'"{protocol}{settings.WS_LOCATION}{token}"'),
        'balances': f'{cfloat(balances[0])} + {cfloat(balances[1])} = {cfloat(balances[0]+balances[1])}' if balances else 'Основной ТС не привязан'
    }


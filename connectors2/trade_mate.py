from base64 import b64encode
from datetime import datetime
from typing import Dict, Optional, Any, List
import json
import hashlib

import requests
from cryptoman.models import Signal, BotTMAuth
from tools.format import cfloat

URL = 'https://trade-mate.io/api/bot/v1'


symbols: Optional[Dict[str, str]] = None
bots: Dict[int, 'TradeMate'] = {}


class TradeMate:
    def __new__(cls, auth: BotTMAuth):
        global bots
        if auth.id in bots:
            b = bots[auth.id]
            if b.key == auth.key and b.secret == auth.secret and b.sl == auth.sl and b.tp == auth.tp and b.trailing == auth.trailing:
                return b

        o = super().__new__(cls)
        o.key = auth.key
        o.secret = auth.secret
        o.sl = auth.sl
        o.tp = auth.tp
        o.trailing = auth.trailing
        bots[auth.id] = o
        return o

    @staticmethod
    def get_nonce():
        return int(datetime.now().timestamp()*1000)

    def make_hash(self, parameters: Dict[str, Any], body: Optional[Dict] = None):
        params_string = ''
        params_arr = []
        for k in sorted(parameters.keys()):
            params_arr.append(f'{k}:{parameters[k]}')
        params_string += ':'.join(params_arr)

        if body:
            params_string += ':' + json.dumps(body) + ':'
        else:
            params_string += '::'
        params_string += self.secret

        signature = b64encode(hashlib.sha256(params_string.encode('ascii')).digest()).decode('ascii')
        return signature

    def get_symbols(self):
        nonce = self.get_nonce()
        parameters = {'nonce': nonce}
        url = URL + '/symbols'

        s = requests.Session()
        s.headers.update({
            'authKey': self.key,
            'authSignature': self.make_hash(parameters)
        })
        return s.get(url, params=parameters)

    def make_signal(self, signal: Signal):
        global symbols
        if not symbols:
            s = self.get_symbols().json()
            symbols = {}
            for k, v in s.items():
                symbols[v['currency']+v['baseCurrency']] = k

        nonce = self.get_nonce()
        url = URL + '/signal'
        parameters = {'nonce': nonce}
        body = {
            'buys': {
                signal.id: {
                    'price': cfloat(signal.price),
                    'type': "Buy",
                    'amount': "0.05"
                }
            },
            'takeProfits': {
                2: {
                    'amount': "0.05",
                    'threshold': cfloat(signal.symbol.round_tick(signal.price*(100+self.tp)/100)),
                    'type': "TakeProfitSell" if not self.trailing else 'TakeProfitTrailingSell',
                    'trailing': f'{self.trailing/100:.8f}',
                }
            },
            'stopLoss': {
                'amount': "0.05",
                'type': "StopLossSell",
                'threshold': cfloat(signal.symbol.round_tick(signal.price*(100-self.sl)/100)),
                'ladder': False
            },
            'symbolId': symbols[signal.symbol.name],
        }

        s = requests.Session()
        s.headers.update({
            'authKey': self.key,
            'authSignature': self.make_hash(parameters, body)
        })
        return s.post(url, params=parameters, data=json.dumps(body))

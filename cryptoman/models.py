from enum import Enum, auto

from django.contrib.auth.models import User, AbstractUser
from django.db.models import JSONField
from django.core.exceptions import ValidationError
from django.db import models

from binancer.common import ModelDiffMixin, intersect, is_dirty, cfloat


class Bot(models.Model):
    class Meta:
        verbose_name = 'bot'
        verbose_name_plural = 'bots'

    name = models.CharField('name', max_length=32)
    code = models.CharField('code', max_length=8)
    one_order_per_symbol = models.BooleanField('один ордер на пару', default=True)
    daily_limit_filter = models.IntegerField('фильтр пар по объёму BTC', default=0)
    active = models.BooleanField('активность', default=True)

    def __str__(self):
        return self.name


class BotTMAuth(models.Model):
    class Meta:
        verbose_name = 'trade-mate ключ'
        verbose_name_plural = 'trade-mate ключ'

    bot = models.ForeignKey(Bot, on_delete=models.CASCADE)
    key = models.CharField('key', max_length=128, default='')
    secret = models.CharField('secret', max_length=128, default='')
    tp = models.FloatField('ТП% (тейкпрофит)', default=2)
    sl = models.FloatField('СЛ% (стоплосс)', default=2)
    trailing = models.FloatField('ТРС% (трейлинг)', default=0)

    def __str__(self):
        return f'{self.bot.name} - {self.key}'


class Account(models.Model):
    class Meta:
        verbose_name = 'account'
        verbose_name_plural = 'accounts'

    name = models.CharField('название', max_length=64)
    api_key = models.CharField(max_length=64, default='', blank=True)
    secret_key = models.CharField(max_length=64, default='', blank=True)

    balance = JSONField('баланс', null=True, default=None, blank=True)
    balance_control = models.BooleanField('контроль баланса', default=True)

    is_demo = models.BooleanField('демо', default=False)

    active = models.BooleanField('активен', default=True)
    total = models.FloatField('всего в $', default=0)

    def clean(self):
        if not self.is_demo:
            errors = {}
            if not self.api_key:
                errors.update({'api_key': ValidationError('поле обязательно для заполнения', code='required')})
            if not self.secret_key:
                errors.update({'secret_key': ValidationError('поле обязательно для заполнения', code='required')})
            if errors:
                raise ValidationError(errors)

    def __str__(self):
        return self.name

    def get_assigned_strategies(self):
        return ", ".join([strategy.name for strategy in self.strategy_set.all()])

    # def get_assigned_accounts(self):
    #     return ", ".join([assigned_account.name for assigned_account in self.assigned_accounts.all()])


class MyUser(AbstractUser):
    account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, verbose_name='Основной ТС', blank=True)
    accounts = models.ManyToManyField(Account, through='AccountUsers', related_name='linked_accounts')
    disable_messages = models.BooleanField('отключить сообщения', default=True)
    orders_update = models.BooleanField('авто-обновление ордеров/сигналов', default=False)


class Symbol(models.Model):
    class Meta:
        verbose_name = 'pair'
        verbose_name_plural = 'pairs'

    name = models.CharField(max_length=16)
    min_lot = models.FloatField('мин. лот', default=0)
    min_notional = models.FloatField('мин. объём', default=0)
    tick_size = models.FloatField('размер тика', default=0)
    base_asset = models.CharField('базовая монета', max_length=6, default='')
    daily_volume = models.IntegerField('дневной объём BTC', default=0, blank=True)

    def __str__(self):
        return self.name

    def round_lot(self, lot):
        return round(lot / self.min_lot) * self.min_lot

    def round_tick(self, price):
        return round(price / self.tick_size) * self.tick_size


class Strategy(models.Model):
    BALANCE_LIMIT_MODE = (
        (0, 'без лимита'),
        (1, 'процент'),
    )

    LOT_MODE = (
        (0, 'процент'),
        (1, 'фикс'),
    )

    TP_MODE = (
        (0, 'лимитник'),
        (1, 'по рынку'),
    )

    BASE_COINS = (
        'BTC', 'BNB', 'ETH', 'XRP', 'USDT',
    )

    class Meta:
        verbose_name = 'strategy'
        verbose_name_plural = 'strategies'

    name = models.CharField('название', max_length=64)

    balance_limit_mode = models.IntegerField('режим ограничения', choices=BALANCE_LIMIT_MODE, default=0)
    balance_limit_amount = models.FloatField('процент ограничения', default=1)

    lot_mode = models.IntegerField('расчёт лота', choices=LOT_MODE, default=0)
    lot_amount = models.FloatField('значение лота', default=0)

    stoploss = models.FloatField('процент стоп-лосс', default=5)
    expire_time = models.IntegerField('время до экспирации (дней)', default=0)

    base_coin = models.CharField('базовая монета', max_length=10, default='', blank=True, choices=((v, v) for v in BASE_COINS))
    allowed_symbols = models.ManyToManyField(Symbol, verbose_name='разрешённые пары', related_name="allowed", blank=True)
    denied_symbols = models.ManyToManyField(Symbol, verbose_name='запрещённые пары', related_name="denied", blank=True)

    assigned_bots = models.ManyToManyField(Bot, verbose_name='боты')
    assigned_accounts = models.ManyToManyField(Account, verbose_name='аккаунты', )

    corridor = models.FloatField('коридор открытия', default=2.0)
    tp_mode = models.IntegerField('режим ТП', choices=TP_MODE, default=1)

    simulation = models.BooleanField('симуляция', default=False)
    active = models.BooleanField('активен', default=True)
    frozen = models.BooleanField('приостановлена', default=False)

    guard_enabled = models.BooleanField('включен', default=False)
    guard_test_account = models.ForeignKey(Account, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='тестовый тс', related_name='as_guard_strategy')
    guard_loss_limit = models.PositiveIntegerField('ограничитель убытков', default=5)
    guard_profit_pass = models.PositiveIntegerField('показатель прибыли', default=5)
    guard_counter = models.IntegerField('счетчик', default=0, blank=True)
    guard_last_update = models.DateTimeField('последнее обновление', null=True, blank=True)

    autotrade_mode = models.BooleanField('auto-trading', default=False)

    daily_limit_filter = models.IntegerField('фильтр пар по объёму', default=0, blank=True)
    satoshi_filter = models.IntegerField('фильтр мин. цены (сатоши)', default=0, blank=True)

    def clean(self):
        if self.autotrade_mode and self.lot_mode == 0:
            raise ValidationError('Для включения авто-трейдинга установите фикс режим расчёта лота')

    def __str__(self):
        return self.name

    def get_assigned_bots(self):
        return ", ".join([assigned_bot.name for assigned_bot in self.assigned_bots.all()])

    def get_assigned_accounts(self):
        return ", ".join([assigned_account.name for assigned_account in self.assigned_accounts.all()])


class StrategyGuardLog(models.Model):
    class Meta:
        verbose_name = 'лог защитника'
        verbose_name_plural = 'лог защитника'

    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE)
    datetime = models.DateTimeField('дата/время', auto_now=True)
    order = models.ForeignKey('Order', on_delete=models.CASCADE, verbose_name='ордер')
    move = models.SmallIntegerField('движение')

    def __str__(self):
        return f'{self.strategy.name} - {self.datetime}'


class BalanceLimits(models.Model):
    class Meta:
        verbose_name = 'ограничение баланса'
        verbose_name_plural = 'ограничения балансов'

    account = models.ForeignKey(Account, on_delete=models.CASCADE, verbose_name='тс')
    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, verbose_name='стратегия')
    balance = JSONField('балансы')

    def __str__(self):
        return f'{self.account.name} {self.strategy.name}'


class TakeProfitLevel(models.Model):
    class Meta:
        verbose_name = 'уровень ТП'
        verbose_name_plural = 'уровни ТП'

    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, verbose_name='стратегия')
    percent = models.FloatField('ТП')
    percent_to_close = models.FloatField('процент закрытия ордера')
    trailing_mode = models.BooleanField('включать трейлинг')
    trailing_percent = models.FloatField('процент трейлинга', default=0)
    step_stop = models.BooleanField('шаговый стоп', default=False)

    def __str__(self):
        return f'{self.strategy.name} - {self.percent}'


class TakeBuyLevel(models.Model):
    class Meta:
        verbose_name = 'уровень ТБ'
        verbose_name_plural = 'auto-trade'

    strategy = models.ForeignKey(Strategy, on_delete=models.CASCADE, verbose_name='стратегия')
    percent = models.FloatField('Усреднить')
    lot = models.FloatField('размер лота')

    def __str__(self):
        return f'{self.strategy.name} - {self.percent}'


class NotifyType(Enum):
    Signal = auto()
    Order = auto()
    Error = auto()
    Closed = auto()


class NotifyRecipients(models.Model):
    NOTIFY_TYPE = (
        (0, 'Сигналы'),
        (1, 'Ордера'),
        (2, 'Ошибки'),
        (3, 'Закрытые ордера'),
        (4, 'Защитник'),
        (5, 'Только SL'),
        (6, 'Каналы'),
        (7, 'Предпоследний ТБ'),
    )

    class Meta:
        verbose_name = 'Notif recipients'
        verbose_name_plural = 'Notif recipient'

    contact = models.CharField('contact', max_length=64, help_text='список контактов, разделённых запятыми')
    username = models.CharField('name', max_length=128, default='', blank=True)
    level = models.IntegerField('level', choices=NOTIFY_TYPE, default=0)
    active = models.BooleanField('activity', default=True)
    strategies = models.ManyToManyField(Strategy, verbose_name='strategy', blank=True)

    def __str__(self):
        return self.contact


class Signal(models.Model):
    class Meta:
        verbose_name = 'signal'
        verbose_name_plural = 'signals'

    bot = models.ForeignKey(Bot, on_delete=models.CASCADE, verbose_name='bot')
    symbol = models.ForeignKey(Symbol, on_delete=models.CASCADE, verbose_name='pair')
    datetime = models.DateTimeField('time', auto_now_add=True)
    price = models.FloatField('Price')

    def __str__(self):
        return f'#{self.id} - {self.bot.name} - {self.symbol.name} - {self.datetime}'


class OrderStatus:
    PENDING = 0
    ACTIVE = 1
    CLOSED = 2
    CANCELLED = 3
    ERROR = 4


class Order(models.Model, ModelDiffMixin):
    ORDER_TYPE = (
        (0, 'BUY'),
        (1, 'SELL'),
    )

    ORDER_KIND = (
        (0, 'LIMIT'),
        (1, 'STOP'),
        (2, 'MARKET'),
    )

    SL_TYPE = (
        (0, 'обычный'),
        (1, 'ТРС'),
        (2, 'ШС'),
    )

    ORDER_STATUS = (
        (OrderStatus.PENDING, 'Waiting'),
        (OrderStatus.ACTIVE, 'Active'),
        (OrderStatus.CLOSED, 'Closed'),
        (OrderStatus.CANCELLED, 'Cancelled'),
        (OrderStatus.ERROR, 'ERROR'),
    )

    class Meta:
        verbose_name = 'order'
        verbose_name_plural = 'orders'

        permissions = [
            ("view_all_columns", "All columns"),
        ]

    account = models.ForeignKey(Account, on_delete=models.CASCADE, verbose_name='тс')
    bot = models.ForeignKey(Bot, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='бот')
    strategy = models.ForeignKey(Strategy, on_delete=models.SET_NULL, null=True, blank=True, verbose_name='стратегия')
    symbol = models.ForeignKey(Symbol, on_delete=models.SET_NULL, null=True, verbose_name='пара')
    signal = models.ForeignKey(Signal, on_delete=models.SET_NULL, null=True, verbose_name='сигнал', blank=True)
    order_side = models.IntegerField('тип', choices=ORDER_TYPE, default=0)
    order_kind = models.IntegerField('вид', choices=ORDER_KIND, default=0, blank=True)
    open_time = models.DateTimeField('дата открытия', auto_now_add=True)
    close_time = models.DateTimeField('дата закрытия', null=True, blank=True, default=None)
    open_price = models.FloatField('цена безубытка')
    original_price = models.FloatField('исходная цена открытия', default=0, blank=True)
    close_price = models.FloatField('цена закрытия', null=True, blank=True, default=None)
    quantity = models.FloatField('количество', default=0)
    quantity_rest = models.FloatField('остаток количества', default=0)
    volume = models.FloatField('объём', default=0)

    profit = models.FloatField('прибыль/убыток', default=0, blank=True)
    commission = models.FloatField('комиссия', default=0, blank=True)

    simulation = models.BooleanField('симуляция', default=False)

    order_id = models.CharField('код ордера', max_length=255, null=True, blank=True)
    status = models.IntegerField('статус', choices=ORDER_STATUS, default=0, blank=True)

    stoploss_price = models.FloatField('стоп-лосс', null=True, blank=True)
    stoploss_type = models.IntegerField('тип СЛ', choices=SL_TYPE, default=0)
    preopen_stop_time = models.DateTimeField('дата ожидания закрытия отложки', null=True, blank=True)
    pending_stop_time = models.DateTimeField('дата ожидания закрытия', null=True, blank=True)
    trailing_active = models.BooleanField('трейлинг активен', default=False)
    trailing_percent = models.FloatField('процент трейлинга', default=0.0, blank=True)
    takeprofit_levels = JSONField('уровни тейк-профит', null=True, blank=True)
    takebuy_levels = JSONField('уровни тейк-бай', null=True, blank=True)
    autotrade_levels = JSONField('уровни auto-trade', null=True, blank=True)
    takebuy_counter = models.IntegerField('счётчик ТБ', default=0, blank=True)

    parent_order = models.ForeignKey('Order', on_delete=models.CASCADE, null=True, blank=True, verbose_name='родительский ордер', related_name='child_orders')

    error_text = models.CharField('текст ошибки', max_length=255, null=True, blank=True)

    is_smart = models.BooleanField('смарт-ордер', default=False)

    expert = models.ForeignKey('Expert', on_delete=models.SET_NULL, null=True, verbose_name='эксперт', blank=True)

    def clean(self):
        if self.id and \
                not is_dirty(self) and \
                (self.status > 1 or
                 self.status == 1 and
                 len(self.changed_fields) > 0
                 and not intersect(self.changed_fields, ['pending_close_time', 'stoploss_price', 'trailing_active', 'status', 'order_id', 'error_text'])):
            raise ValidationError(f'нельзя изменить активный или отработанный ордер')
        '''
        if self.id and self.order_id and \
                self._ModelDiffMixin__initial['status'] == 1 and self.status == 1:
            raise ValidationError('нельзя изменить активный ордер')
        '''

    def __str__(self):
        return f'#{self.id} {self.get_order_side_display()} {self.symbol.name} {cfloat(self.open_price)} {self.account.name} {self.open_time:%x %X}'


class OrderSpecial(models.Model):
    class Meta:
        verbose_name = 'ордер доп.'
        verbose_name_plural = 'ордер доп.'

    order = models.OneToOneField(Order, on_delete=models.CASCADE, primary_key=True, verbose_name='ордер')

    max_price = models.FloatField('макс. цена', default=0, blank=True)
    max_price_date = models.DateTimeField('дата макс. цены', null=True, blank=True)
    min_ask_price = models.FloatField('мин. цена ask', default=0, blank=True)
    min_ask_date = models.DateTimeField('дата мин. цены', null=True, blank=True)

    def __str__(self):
        return str(self.max_price)


class Expert(models.Model):
    class Meta:
        verbose_name = 'expert'
        verbose_name_plural = 'experts'

    name = models.CharField('name', max_length=128)
    code = models.CharField('code', max_length=128)

    def __str__(self):
        return self.name


class TradeLog(models.Model):
    class Meta:
        verbose_name = 'торговый лог'
        verbose_name_plural = 'торговые логи'

    datetime = models.DateTimeField('дата/время', auto_now=True)
    signal = models.ForeignKey(Signal, on_delete=models.CASCADE, null=True, blank=True, verbose_name='сигнал')
    order = models.ForeignKey(Order, on_delete=models.CASCADE, null=True, blank=True, verbose_name='ордер')
    message = models.CharField('сообщение', max_length=16384)

    def clean(self):
        if self.order and not self.signal and self.order.signal:
            self.signal = self.order.signal

    def __str__(self):
        return self.message


"""
class Averager(models.Model):
    class Meta:
        verbose_name = 'усреднитель'
        verbose_name_plural = 'усреднители'

    name = models.CharField('название', max_length=128)
    stoploss = models.FloatField('стоп-лосс')

    def __str__(self):
        return self.name


class AveragerLevel(models.Model):
    class Meta:
        verbose_name = 'уровень усреднителя'
        verbose_name_plural = 'уровни усреднителя'

    averager = models.ForeignKey(Averager, on_delete=models.CASCADE, verbose_name='усреднитель')
    level = models.FloatField('уровень')
    coef = models.FloatField('коэффициент', default=1.0)

    def __str__(self):
        return self.averager.name + cfloat(self.level)
"""


class AccountUsers(models.Model):
    class Meta:
        verbose_name = 'Привязанный ТС'
        verbose_name_plural = 'Привязанные ТС'

    account = models.ForeignKey(Account, on_delete=models.CASCADE, verbose_name='ТС')
    user = models.ForeignKey(MyUser, on_delete=models.CASCADE, verbose_name='пользователь')

    def __str__(self):
        return f'{self.user.username} - {self.account.name}'


class SmartTemplate(models.Model):
    class Meta:
        verbose_name = 'smart шаблон'
        verbose_name_plural = 'smart шаблоны'

    user = models.ForeignKey(MyUser, on_delete=models.CASCADE, verbose_name='пользователь')
    name = models.CharField('название', max_length=128)
    template = JSONField('шаблон')

    def __str__(self):
        return self.name

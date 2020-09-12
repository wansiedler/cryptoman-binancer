from django.dispatch import Signal

order_opened = Signal(providing_args=['id'])
order_added = Signal(providing_args=['id'])
order_closed = Signal(providing_args=['id'])
order_error = Signal(providing_args=['id'])
tick_processing = Signal(providing_args=['symbol', 'quote'])

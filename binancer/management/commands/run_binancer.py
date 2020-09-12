import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger("")


class Command(BaseCommand):
    help = 'Run binance trading bot process'

    def add_arguments(self, parser):
        # parser.add_argument('poll_id', nargs='+', type=int)
        pass

    def handle(self, *args, **options):
        logger.debug('Starting binancer')
        from binancer.binance_thread import init_thread
        from binancer.binancer import init_quotes_maker
        # from binancer.binancer import binance_client

        thread = init_thread()

        # for client in clients.values():
        #    client.sync_orders()

        init_quotes_maker()

        thread.join()

        self.stdout.write(self.style.SUCCESS('Exiting...'))

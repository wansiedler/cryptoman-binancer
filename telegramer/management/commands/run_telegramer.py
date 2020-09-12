import logging

from django.core.management.base import BaseCommand
from telegramer.telegramer import connect

logger = logging.getLogger("")


class Command(BaseCommand):
    help = 'Run telegramer bot process'

    def add_arguments(self, parser):
        # parser.add_argument('poll_id', nargs='+', type=int)
        pass

    def handle(self, *args, **options):
        logger.debug('Starting telegramer')
        connect()

        self.stdout.write(self.style.SUCCESS('Exiting...'))

import threading

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Placeholder process for the future transactional outbox dispatcher."

    def handle(self, *args, **options) -> None:
        self.stdout.write("Outbox dispatcher is idle; outbox delivery is not implemented.")
        threading.Event().wait()

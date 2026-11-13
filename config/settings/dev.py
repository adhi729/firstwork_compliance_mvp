import os

from .base import *  # noqa: F403

DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

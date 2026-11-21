import os
import sys

from django.apps import AppConfig


class JogoConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "jogo"
    verbose_name = "Sorte Animal"

    def ready(self):
        if "runserver" not in sys.argv:
            return
        if os.environ.get("RUN_MAIN") != "true" and "--noreload" not in sys.argv:
            return
        from .services import iniciar

        iniciar()

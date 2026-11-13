from importlib import import_module

from django.apps import AppConfig


class TenantsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenants"

    def ready(self) -> None:
        import_module("apps.tenants.schema")

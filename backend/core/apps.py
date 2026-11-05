from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"
    verbose_name = "JARVIS"

    def ready(self):
        from . import signals  # noqa: F401 — connects the alert -> push hook

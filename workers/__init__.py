"""Worker implementations load only when explicitly selected."""


def __getattr__(name):
    if name == "tasks":
        # Preserve the legacy tasks module for integrations without importing
        # Celery when the API starts with the native backend.
        from importlib import import_module
        return import_module("workers.tasks")
    raise AttributeError(name)

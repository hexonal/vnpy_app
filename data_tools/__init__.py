"""Headless data-plumbing tools for the vnpy_app stack.

Deliberately Qt-free: everything here must be runnable from a cron job, a
test, or a GUI button without importing PySide6. The Fluent widgets in
``fluent_ui`` may call into this package; this package never calls back.
"""

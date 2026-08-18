"""MergePilot GitHub App ingress package (M8-GH-1 MVP).

receiver/http_server: HMAC-verified webhook → INSERT-only
``github_deliveries``;checks_reporter: outbox → Checks API publisher.
This package NEVER imports the workflow controller, ``process_event`` or
``submit_task``, and never writes governance tables.
"""

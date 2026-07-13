"""Background tasks executed by the RQ worker — one module per task type.

Task modules must not import from ``backend.services.queue`` or
``backend.worker`` (circular-import rule).
"""

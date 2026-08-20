"""Honest, branded error envelopes.

Brand tone: direct and useful, no cuteness. Errors return a stable shape the
frontend can render as a designed empty/error state, not a stack trace and never a
bare ``--``.
"""
from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger('c360.api')


def exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is not None:
        response.data = {
            'error': {
                'status': response.status_code,
                'detail': _flatten(response.data),
            }
        }
        return response
    # Unhandled — LOG the full traceback so it lands in the container logs (docker logs
    # c360-backend), then return a clean envelope instead of an HTML 500 page. Without
    # this the generic message below is all anyone sees and the real cause is invisible.
    view = context.get('view') if context else None
    request = context.get('request') if context else None
    logger.exception('Unhandled error in %s (%s %s)', view,
                     getattr(request, 'method', '?'),
                     getattr(request, 'path', '?'), exc_info=exc)
    return Response(
        {'error': {'status': 500, 'detail': 'Unexpected error resolving this view.'}},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _flatten(data) -> str:
    if isinstance(data, dict):
        return '; '.join(f'{k}: {_flatten(v)}' for k, v in data.items())
    if isinstance(data, (list, tuple)):
        return '; '.join(_flatten(v) for v in data)
    return str(data)

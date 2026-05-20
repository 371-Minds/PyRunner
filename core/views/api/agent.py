"""
Agent-facing manifest and health endpoints.
"""

import logging

from django.http import JsonResponse, HttpRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import DataStore, GlobalSettings, Script
from core.views.api.decorators import (
    add_cors_headers,
    api_token_required,
)

logger = logging.getLogger(__name__)

# Workers are considered alive if heartbeat is within this many seconds
WORKER_ALIVE_THRESHOLD_SECONDS = 120


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
def health(request: HttpRequest) -> JsonResponse:
    """
    Public health/readiness endpoint — no authentication required.

    GET /api/v1/health/

    Returns:
        {
            "status": "ok",
            "workers_alive": true,
            "schedules_paused": false
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    try:
        gs = GlobalSettings.get_settings()
        workers_alive = (
            gs.worker_heartbeat_at is not None
            and (timezone.now() - gs.worker_heartbeat_at).total_seconds()
            <= WORKER_ALIVE_THRESHOLD_SECONDS
        )
        schedules_paused = gs.schedules_paused
    except Exception:
        workers_alive = False
        schedules_paused = False

    data = {
        "status": "ok",
        "workers_alive": workers_alive,
        "schedules_paused": schedules_paused,
    }
    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
def agent_manifest(request: HttpRequest) -> JsonResponse:
    """
    Return a full capability manifest for AI agents.

    GET /api/v1/agent/manifest/

    Includes:
      - available scripts (with input schemas)
      - available datastores (with entry counts)
      - system status (workers, pause state)
      - token scopes

    This endpoint is accessible with *any* valid token — the manifest
    content is scoped to whatever the token can actually access.
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    token = request.api_token

    # System status
    try:
        gs = GlobalSettings.get_settings()
        workers_alive = (
            gs.worker_heartbeat_at is not None
            and (timezone.now() - gs.worker_heartbeat_at).total_seconds()
            <= WORKER_ALIVE_THRESHOLD_SECONDS
        )
        schedules_paused = gs.schedules_paused
    except Exception:
        workers_alive = False
        schedules_paused = False

    # Scripts (only if token has scope_scripts)
    scripts_data = []
    if token.scope_scripts:
        scripts_qs = (
            Script.objects.filter(is_enabled=True, archived_at__isnull=True)
            .prefetch_related("tags")
            .order_by("name")
        )
        for s in scripts_qs:
            scripts_data.append({
                "id": str(s.id),
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "is_template": s.is_template,
                "api_execution_enabled": s.api_execution_enabled,
                "tags": [t.name for t in s.tags.all()],
                "input_schema": s.input_schema,
            })

    # Datastores (only if token has scope_datastores_read)
    datastores_data = []
    if token.scope_datastores_read:
        if token.datastore:
            ds_qs = [token.datastore]
        else:
            ds_qs = DataStore.objects.all().order_by("name")
        for ds in ds_qs:
            datastores_data.append({
                "name": ds.name,
                "description": ds.description,
                "entry_count": ds.entry_count,
            })

    data = {
        "system": {
            "workers_alive": workers_alive,
            "schedules_paused": schedules_paused,
        },
        "token": {
            "name": token.name,
            "scopes": {
                "scope_scripts": token.scope_scripts,
                "scope_datastores_read": token.scope_datastores_read,
                "scope_datastores_write": token.scope_datastores_write,
                "scope_runs_read": token.scope_runs_read,
            },
        },
        "scripts": scripts_data,
        "datastores": datastores_data,
    }
    response = JsonResponse(data)
    return add_cors_headers(response)

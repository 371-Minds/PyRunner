"""
API views for run management and status polling.
"""

import logging

from django.http import JsonResponse, HttpRequest
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import Run
from core.views.api.decorators import (
    add_cors_headers,
    api_token_required,
    require_scope,
)

logger = logging.getLogger(__name__)

# Pagination defaults
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
@require_scope("scope_runs_read")
def get_run(request: HttpRequest, run_id: str) -> JsonResponse:
    """
    Get full run details including structured output.

    GET /api/v1/runs/<run_id>/

    Returns:
        {
            "id": "...",
            "script_id": "...",
            "script_name": "...",
            "status": "success|failed|pending|running|timeout|cancelled",
            "trigger_type": "manual|scheduled|api",
            "agent_id": "...",
            "session_id": "...",
            "exit_code": 0,
            "duration_seconds": 1.23,
            "structured_output": {...},   // only when run is finished
            "created_at": "...",
            "started_at": "...",
            "ended_at": "..."
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    run = _get_run_or_404(run_id)
    if isinstance(run, JsonResponse):
        return run

    response = JsonResponse(_serialize_run(run))
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
@require_scope("scope_runs_read")
def get_run_output(request: HttpRequest, run_id: str) -> JsonResponse:
    """
    Get stdout, stderr, and exit_code for a completed run.

    GET /api/v1/runs/<run_id>/output/

    Returns 409 if the run has not finished yet.
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    run = _get_run_or_404(run_id)
    if isinstance(run, JsonResponse):
        return run

    if not run.is_finished:
        response = JsonResponse(
            {
                "error": {
                    "code": "RUN_NOT_FINISHED",
                    "message": f"Run is still {run.status}. Output is not yet available.",
                }
            },
            status=409,
        )
        return add_cors_headers(response)

    data = {
        "id": str(run.id),
        "status": run.status,
        "exit_code": run.exit_code,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "structured_output": run.structured_output,
        "duration_seconds": run.duration,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }
    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
@require_scope("scope_runs_read")
def get_run_status(request: HttpRequest, run_id: str) -> JsonResponse:
    """
    Lightweight run status polling endpoint (no output blobs).

    GET /api/v1/runs/<run_id>/status/

    Designed for frequent polling by agents — returns only minimal fields.

    Returns:
        {
            "id": "...",
            "status": "...",
            "started_at": "...",
            "ended_at": "...",
            "duration_seconds": 1.23
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    run = _get_run_or_404(run_id)
    if isinstance(run, JsonResponse):
        return run

    data = {
        "id": str(run.id),
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "duration_seconds": run.duration,
    }
    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@api_token_required
@require_scope("scope_runs_read")
def cancel_run(request: HttpRequest, run_id: str) -> JsonResponse:
    """
    Request cancellation of a queued or running run.

    POST /api/v1/runs/<run_id>/cancel/

    For queued (PENDING) runs: removes from django-q2 OrmQ and marks CANCELLED.
    For running runs: marks CANCELLED; the underlying subprocess continues until
    its own timeout (django-q2 limitation on Linux without SIGKILL).

    Returns:
        {"cancelled": true, "run_id": "..."}
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response, "POST, OPTIONS")

    run = _get_run_or_404(run_id)
    if isinstance(run, JsonResponse):
        return run

    if run.is_finished:
        response = JsonResponse(
            {
                "error": {
                    "code": "ALREADY_FINISHED",
                    "message": f"Run is already in terminal state: {run.status}",
                }
            },
            status=409,
        )
        return add_cors_headers(response, "POST, OPTIONS")

    from core.services.task_service import TaskService

    if run.task_id and run.status == Run.Status.PENDING:
        success, msg = TaskService.cancel_queued_task(run.task_id)
    else:
        success, msg = TaskService.force_stop_task(run.task_id or str(run.id))

    if not success:
        response = JsonResponse(
            {"error": {"code": "CANCEL_FAILED", "message": msg}},
            status=500,
        )
        return add_cors_headers(response, "POST, OPTIONS")

    response = JsonResponse({"cancelled": True, "run_id": str(run.id)})
    return add_cors_headers(response, "POST, OPTIONS")


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
@require_scope("scope_runs_read")
def list_runs(request: HttpRequest) -> JsonResponse:
    """
    List runs, optionally filtered by agent/session.

    GET /api/v1/runs/

    Query params:
      - agent_id:   filter by agent_id
      - session_id: filter by session_id
      - script_id:  filter by script UUID
      - status:     filter by status
      - page:       page number (default 1)
      - page_size:  items per page (default 50, max 100)
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    qs = Run.objects.select_related("script").order_by("-created_at")

    agent_id = request.GET.get("agent_id", "").strip()
    if agent_id:
        qs = qs.filter(agent_id=agent_id)

    session_id = request.GET.get("session_id", "").strip()
    if session_id:
        qs = qs.filter(session_id=session_id)

    script_id = request.GET.get("script_id", "").strip()
    if script_id:
        qs = qs.filter(script_id=script_id)

    status_filter = request.GET.get("status", "").strip()
    if status_filter and status_filter in dict(Run.Status.choices):
        qs = qs.filter(status=status_filter)

    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        page_size = min(MAX_PAGE_SIZE, max(1, int(request.GET.get("page_size", DEFAULT_PAGE_SIZE))))
    except (ValueError, TypeError):
        page_size = DEFAULT_PAGE_SIZE

    total_count = qs.count()
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    page = min(page, total_pages)
    offset = (page - 1) * page_size
    runs = qs[offset: offset + page_size]

    data = {
        "runs": [_serialize_run(r) for r in runs],
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }
    response = JsonResponse(data)
    return add_cors_headers(response)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_run_or_404(run_id: str):
    """Return Run or a JsonResponse(404)."""
    try:
        return Run.objects.select_related("script").get(pk=run_id)
    except (Run.DoesNotExist, ValueError):
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Run '{run_id}' not found"}},
            status=404,
        )
        return add_cors_headers(response)


def _serialize_run(run: Run) -> dict:
    data = {
        "id": str(run.id),
        "script_id": str(run.script_id),
        "script_name": run.script.name if run.script else None,
        "status": run.status,
        "trigger_type": run.trigger_type,
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "trigger_script_id": str(run.trigger_script_id) if run.trigger_script_id else None,
        "exit_code": run.exit_code,
        "duration_seconds": run.duration,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }
    if run.is_finished:
        data["structured_output"] = run.structured_output
    return data

"""
API views for script management and execution.
"""

import ipaddress
import json
import logging
import urllib.parse

from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import Run, Script, Tag
from core.tasks import queue_script_run
from core.views.api.decorators import (
    MAX_CONCURRENT_RUNS_PER_TOKEN,
    MAX_PAYLOAD_BYTES,
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
@require_scope("scope_scripts")
def list_scripts(request: HttpRequest) -> JsonResponse:
    """
    List all enabled, non-archived scripts.

    GET /api/v1/scripts/

    Query params:
      - q:         full-text search on name/description
      - tag:       filter by tag name
      - category:  filter by category slug (general, data, integration, ai, utility)
      - page:      page number (default 1)
      - page_size: items per page (default 50, max 100)

    Returns:
        {
            "scripts": [...],
            "count": <int>,
            "page": <int>,
            "page_size": <int>,
            "total_pages": <int>
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    qs = (
        Script.objects.filter(is_enabled=True, archived_at__isnull=True)
        .select_related("environment")
        .prefetch_related("tags")
        .order_by("name")
    )

    # Filter: full-text search
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q as DQ
        qs = qs.filter(DQ(name__icontains=q) | DQ(description__icontains=q))

    # Filter: tag
    tag_name = request.GET.get("tag", "").strip()
    if tag_name:
        qs = qs.filter(tags__name__iexact=tag_name)

    # Filter: category
    category = request.GET.get("category", "").strip()
    if category:
        qs = qs.filter(category=category)

    # Pagination
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
    scripts = qs[offset: offset + page_size]

    data = {
        "scripts": [_serialize_script(s) for s in scripts],
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }
    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
@require_scope("scope_scripts")
def get_script(request: HttpRequest, script_id: str) -> JsonResponse:
    """
    Get metadata for a single script.

    GET /api/v1/scripts/<script_id>/
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    script = _get_script_or_404(script_id)
    if isinstance(script, JsonResponse):
        return script

    response = JsonResponse(_serialize_script(script, detailed=True))
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@api_token_required
@require_scope("scope_scripts")
def trigger_script(request: HttpRequest, script_id: str) -> JsonResponse:
    """
    Trigger a script run via API.

    POST /api/v1/scripts/<script_id>/run/

    Body (all optional):
    {
        "inputs":       {<key>: <value>, ...},   // injected as PYRUNNER_INPUT + INPUT_<KEY> env vars
        "context":      {<key>: <value>, ...},   // injected as PYRUNNER_CONTEXT env var
        "agent_id":     "my-agent",              // stored on Run for traceability
        "session_id":   "session-xyz",           // stored on Run for grouping
        "trigger_script_id": "<uuid>",           // ID of calling script (chaining)
        "callback_url": "https://..."            // URL to POST completion event to
    }

    Returns:
        {
            "status": "queued",
            "run_id": "<uuid>",
            "script_id": "<uuid>",
            "script_name": "..."
        }
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response, "POST, OPTIONS")

    script = _get_script_or_404(script_id)
    if isinstance(script, JsonResponse):
        return script

    # Security: template scripts are not executable via API
    if script.is_template:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Template scripts cannot be triggered via API"}},
            status=403,
        )
        return add_cors_headers(response, "POST, OPTIONS")

    # Security: per-script API execution toggle
    if not script.api_execution_enabled:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "API execution is disabled for this script"}},
            status=403,
        )
        return add_cors_headers(response, "POST, OPTIONS")

    if not script.can_run:
        reason = "archived" if script.is_archived else "disabled"
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": f"Script is {reason}"}},
            status=403,
        )
        return add_cors_headers(response, "POST, OPTIONS")

    # Payload size guard
    if len(request.body) > MAX_PAYLOAD_BYTES:
        response = JsonResponse(
            {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body exceeds 1 MB limit"}},
            status=413,
        )
        return add_cors_headers(response, "POST, OPTIONS")

    # Parse body
    body = {}
    if request.body:
        try:
            body = json.loads(request.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = JsonResponse(
                {"error": {"code": "INVALID_JSON", "message": "Request body must be valid JSON"}},
                status=400,
            )
            return add_cors_headers(response, "POST, OPTIONS")

    inputs = body.get("inputs", {})
    context = body.get("context", {})
    agent_id = str(body.get("agent_id", ""))[:255]
    session_id = str(body.get("session_id", ""))[:255]
    callback_url = str(body.get("callback_url", ""))[:500]
    trigger_script_id = body.get("trigger_script_id")

    # Validate callback_url to prevent SSRF — must be an HTTPS/HTTP URL pointing
    # to a public (non-private/loopback) address.
    if callback_url:
        ssrf_error = _validate_callback_url(callback_url)
        if ssrf_error:
            response = JsonResponse(
                {"error": {"code": "INVALID_CALLBACK_URL", "message": ssrf_error}},
                status=400,
            )
            return add_cors_headers(response, "POST, OPTIONS")

    # Validate inputs against input_schema if provided
    if script.input_schema and inputs:
        error = _validate_inputs(inputs, script.input_schema)
        if error:
            response = JsonResponse(
                {"error": {"code": "INVALID_INPUTS", "message": error}},
                status=422,
            )
            return add_cors_headers(response, "POST, OPTIONS")

    # Deployment-wide concurrent API-triggered run limit — prevents runaway
    # agents from flooding the worker queue.  Without a token FK on Run we
    # cannot scope this per-token; it acts as a global API safeguard.
    concurrent = Run.objects.filter(
        trigger_type=Run.TriggerType.API,
        status__in=[Run.Status.PENDING, Run.Status.RUNNING],
    ).count()
    if concurrent >= MAX_CONCURRENT_RUNS_PER_TOKEN:
        response = JsonResponse(
            {
                "error": {
                    "code": "TOO_MANY_CONCURRENT_RUNS",
                    "message": f"Maximum of {MAX_CONCURRENT_RUNS_PER_TOKEN} concurrent runs reached. Try again later.",
                }
            },
            status=429,
        )
        return add_cors_headers(response, "POST, OPTIONS")

    # Resolve trigger_script FK
    trigger_script_obj = None
    if trigger_script_id:
        try:
            trigger_script_obj = Script.objects.get(pk=trigger_script_id)
        except (Script.DoesNotExist, ValueError):
            pass  # Non-fatal — just skip

    # Create Run record
    run = Run.objects.create(
        script=script,
        status=Run.Status.PENDING,
        triggered_by=None,
        trigger_type=Run.TriggerType.API,
        code_snapshot=script.code,
        agent_id=agent_id,
        session_id=session_id,
        trigger_script=trigger_script_obj,
        callback_url=callback_url,
    )

    # Build webhook-style data dict for injection
    api_data = {
        "inputs": inputs,
        "context": context,
        "agent_id": agent_id,
        "session_id": session_id,
    }

    try:
        queue_script_run(run, webhook_data=None, api_data=api_data)
        logger.info(
            f"API triggered run {run.id} for script '{script.name}' "
            f"(agent_id={agent_id!r}, session_id={session_id!r})"
        )
        data = {
            "status": "queued",
            "run_id": str(run.id),
            "script_id": str(script.id),
            "script_name": script.name,
        }
        response = JsonResponse(data, status=202)
        return add_cors_headers(response, "POST, OPTIONS")

    except Exception as e:
        run.status = Run.Status.FAILED
        run.stderr = f"Failed to queue task: {str(e)}"
        run.save()
        logger.error(f"API failed to queue run {run.id}: {e}")
        response = JsonResponse(
            {"error": {"code": "QUEUE_ERROR", "message": "Failed to queue script execution"}},
            status=500,
        )
        return add_cors_headers(response, "POST, OPTIONS")


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
@require_scope("scope_scripts")
def list_script_runs(request: HttpRequest, script_id: str) -> JsonResponse:
    """
    List recent runs for a script.

    GET /api/v1/scripts/<script_id>/runs/

    Query params:
      - status:    filter by status (pending/running/success/failed/timeout/cancelled)
      - page:      page number (default 1)
      - page_size: items per page (default 50, max 100)
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    if not request.api_token.scope_runs_read:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_runs_read"}},
            status=403,
        )
        return add_cors_headers(response)

    script = _get_script_or_404(script_id)
    if isinstance(script, JsonResponse):
        return script

    qs = script.runs.order_by("-created_at")
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
        "runs": [_serialize_run_summary(r) for r in runs],
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }
    response = JsonResponse(data)
    return add_cors_headers(response)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_script_or_404(script_id: str):
    """Return Script or a JsonResponse(404)."""
    try:
        return Script.objects.select_related("environment").prefetch_related("tags").get(pk=script_id)
    except (Script.DoesNotExist, ValueError):
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Script '{script_id}' not found"}},
            status=404,
        )
        return add_cors_headers(response)


def _serialize_script(script: Script, detailed: bool = False) -> dict:
    last_run = script.runs.order_by("-created_at").first()
    data = {
        "id": str(script.id),
        "name": script.name,
        "description": script.description,
        "category": script.category,
        "is_template": script.is_template,
        "api_execution_enabled": script.api_execution_enabled,
        "is_enabled": script.is_enabled,
        "tags": [t.name for t in script.tags.all()],
        "input_schema": script.input_schema,
        "environment": script.environment.name if script.environment else None,
        "last_run": {
            "id": str(last_run.id),
            "status": last_run.status,
            "created_at": last_run.created_at.isoformat(),
        } if last_run else None,
        "created_at": script.created_at.isoformat(),
        "updated_at": script.updated_at.isoformat(),
    }
    if detailed:
        data["success_rate"] = script.success_rate
        data["run_count"] = script.run_count
        try:
            schedule = script.schedule
            data["schedule"] = {
                "run_mode": schedule.run_mode,
                "is_active": schedule.is_active,
                "schedule_display": schedule.schedule_display,
                "next_run": schedule.next_run.isoformat() if schedule.next_run else None,
            }
        except Exception:
            data["schedule"] = None
    return data


def _serialize_run_summary(run: Run) -> dict:
    return {
        "id": str(run.id),
        "status": run.status,
        "trigger_type": run.trigger_type,
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "exit_code": run.exit_code,
        "duration_seconds": run.duration,
        "created_at": run.created_at.isoformat(),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }


def _validate_inputs(inputs: dict, schema: dict) -> str | None:
    """
    Basic JSON Schema validation for required properties and types.

    Returns an error string or None if valid.
    We intentionally keep this lightweight — no third-party library.
    """
    required = schema.get("required", [])
    for field in required:
        if field not in inputs:
            return f"Missing required input: '{field}'"

    properties = schema.get("properties", {})
    for field, prop in properties.items():
        if field not in inputs:
            continue
        expected_type = prop.get("type")
        if not expected_type:
            continue
        value = inputs[field]
        type_map = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        expected_python = type_map.get(expected_type)
        if expected_python and not isinstance(value, expected_python):
            return f"Input '{field}' must be of type '{expected_type}'"

    return None


def _validate_callback_url(url: str) -> str | None:
    """
    Validate a callback URL to prevent SSRF attacks.

    Rules:
    - Must be http:// or https://
    - Must have a non-empty hostname
    - Must not resolve to a loopback, link-local, or private-network address

    Returns an error message string on failure, or None if the URL is safe.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return "callback_url is not a valid URL"

    if parsed.scheme not in ("http", "https"):
        return "callback_url must use http or https scheme"

    hostname = parsed.hostname
    if not hostname:
        return "callback_url must have a valid hostname"

    # Block IP-literal addresses that point to private/loopback ranges
    try:
        addr = ipaddress.ip_address(hostname)
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            return "callback_url must not point to a private or loopback address"
    except ValueError:
        # Not an IP literal — it's a domain name; apply basic sanity checks
        # We don't do DNS resolution here (that would be a pre-connect SSRF
        # check, but DNS is mutable).  Deployers can add network-level egress
        # controls for stricter enforcement.
        lower = hostname.lower()
        if lower in ("localhost", "ip6-localhost", "ip6-loopback"):
            return "callback_url must not point to localhost"
        if lower.endswith(".local") or lower.endswith(".internal"):
            return "callback_url must not point to a private network hostname"

    return None

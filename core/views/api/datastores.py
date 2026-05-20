"""
API views for datastore access.
"""

import json
import logging
import re

from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import DataStore, DataStoreEntry
from core.views.api.decorators import (
    api_token_required,
    require_scope,
    write_rate_limit,
    add_cors_headers,
    MAX_PAYLOAD_BYTES,
)

logger = logging.getLogger(__name__)

# Pagination settings
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100

# Datastore name validation
VALID_NAME_PATTERN = re.compile(r'^[A-Za-z0-9_-]{1,100}$')


# ── Read endpoints ────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET", "POST", "OPTIONS"])
@api_token_required
def list_datastores(request: HttpRequest) -> JsonResponse:
    """
    List all datastores, or create a new one.

    GET  /api/v1/datastores/  — requires scope_datastores_read
    POST /api/v1/datastores/  — requires scope_datastores_write

    POST body: {"name": "my_store", "description": "optional"}
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response, "GET, POST, OPTIONS")

    if request.method == "POST":
        return _create_datastore(request)

    # GET — list
    if not request.api_token.scope_datastores_read:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_datastores_read"}},
            status=403,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    token = request.api_token

    if token.datastore:
        # Token restricted to single datastore
        datastores = [token.datastore]
    else:
        # Global token - list all datastores
        datastores = DataStore.objects.all()

    data = {
        "datastores": [
            {
                "name": ds.name,
                "description": ds.description,
                "entry_count": ds.entry_count,
                "created_at": ds.created_at.isoformat(),
                "updated_at": ds.updated_at.isoformat(),
            }
            for ds in datastores
        ],
        "count": len(datastores) if token.datastore else datastores.count(),
    }

    response = JsonResponse(data)
    return add_cors_headers(response, "GET, POST, OPTIONS")


def _create_datastore(request: HttpRequest) -> JsonResponse:
    """Create a new datastore (POST /api/v1/datastores/)."""
    if not request.api_token.scope_datastores_write:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_datastores_write"}},
            status=403,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    if len(request.body) > MAX_PAYLOAD_BYTES:
        response = JsonResponse(
            {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body exceeds 1 MB limit"}},
            status=413,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response = JsonResponse(
            {"error": {"code": "INVALID_JSON", "message": "Request body must be valid JSON"}},
            status=400,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    name = body.get("name", "").strip()
    if not name or not VALID_NAME_PATTERN.match(name):
        response = JsonResponse(
            {
                "error": {
                    "code": "INVALID_NAME",
                    "message": "name must be 1-100 characters: letters, digits, underscores, hyphens",
                }
            },
            status=400,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    if DataStore.objects.filter(name=name).exists():
        response = JsonResponse(
            {"error": {"code": "CONFLICT", "message": f"Datastore '{name}' already exists"}},
            status=409,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    ds = DataStore.objects.create(
        name=name,
        description=body.get("description", ""),
    )
    data = {
        "name": ds.name,
        "description": ds.description,
        "entry_count": 0,
        "created_at": ds.created_at.isoformat(),
        "updated_at": ds.updated_at.isoformat(),
    }
    response = JsonResponse(data, status=201)
    return add_cors_headers(response, "GET, POST, OPTIONS")


@csrf_exempt
@require_http_methods(["GET", "OPTIONS"])
@api_token_required
@require_scope("scope_datastores_read")
def get_datastore(request: HttpRequest, name: str) -> JsonResponse:
    """
    Get datastore metadata.

    GET /api/v1/datastores/<name>/
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response)

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore

    data = {
        "name": datastore.name,
        "description": datastore.description,
        "entry_count": datastore.entry_count,
        "created_at": datastore.created_at.isoformat(),
        "updated_at": datastore.updated_at.isoformat(),
    }

    response = JsonResponse(data)
    return add_cors_headers(response)


@csrf_exempt
@require_http_methods(["GET", "POST", "OPTIONS"])
@api_token_required
def list_entries(request: HttpRequest, name: str) -> JsonResponse:
    """
    List entries in a datastore (GET) or upsert a new entry (POST).

    GET  /api/v1/datastores/<name>/entries/  — requires scope_datastores_read
    POST /api/v1/datastores/<name>/entries/  — requires scope_datastores_write

    POST body: {"key": "my_key", "value": <any JSON>}
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response, "GET, POST, OPTIONS")

    if request.method == "POST":
        return _upsert_entry(request, name)

    # GET
    if not request.api_token.scope_datastores_read:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_datastores_read"}},
            status=403,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore

    # Parse pagination params
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (ValueError, TypeError):
        page = 1

    try:
        page_size = min(MAX_PAGE_SIZE, max(1, int(request.GET.get("page_size", DEFAULT_PAGE_SIZE))))
    except (ValueError, TypeError):
        page_size = DEFAULT_PAGE_SIZE

    # Get total count and calculate pagination
    total_count = datastore.entries.count()
    total_pages = max(1, (total_count + page_size - 1) // page_size)

    # Ensure page is within bounds
    page = min(page, total_pages)

    # Get paginated entries
    offset = (page - 1) * page_size
    entries = datastore.entries.all()[offset:offset + page_size]

    data = {
        "entries": [
            {
                "key": entry.key,
                "value": entry.get_value(),
                "created_at": entry.created_at.isoformat(),
                "updated_at": entry.updated_at.isoformat(),
            }
            for entry in entries
        ],
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }

    response = JsonResponse(data)
    return add_cors_headers(response, "GET, POST, OPTIONS")


def _upsert_entry(request: HttpRequest, name: str) -> JsonResponse:
    """Upsert an entry (POST /api/v1/datastores/<name>/entries/)."""
    if not request.api_token.scope_datastores_write:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_datastores_write"}},
            status=403,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    # Write rate limit check
    from core.views.api.decorators import API_WRITE_RATE_LIMIT, API_RATE_WINDOW
    from django.core.cache import cache
    write_key = f"api_write_rate_{request.api_token.id}"
    write_count = cache.get(write_key, 0)
    if write_count >= API_WRITE_RATE_LIMIT:
        response = JsonResponse(
            {"error": {"code": "RATE_LIMITED", "message": "Write rate limit exceeded. Try again later."}},
            status=429,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")
    cache.set(write_key, write_count + 1, API_RATE_WINDOW)

    if len(request.body) > MAX_PAYLOAD_BYTES:
        response = JsonResponse(
            {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body exceeds 1 MB limit"}},
            status=413,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response = JsonResponse(
            {"error": {"code": "INVALID_JSON", "message": "Request body must be valid JSON"}},
            status=400,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    key = body.get("key", "")
    if not key or not isinstance(key, str) or len(key) > 255:
        response = JsonResponse(
            {"error": {"code": "INVALID_KEY", "message": "key must be a non-empty string (max 255 chars)"}},
            status=400,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    if "value" not in body:
        response = JsonResponse(
            {"error": {"code": "MISSING_VALUE", "message": "value is required"}},
            status=400,
        )
        return add_cors_headers(response, "GET, POST, OPTIONS")

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore

    value_json = json.dumps(body["value"])
    entry, created = DataStoreEntry.objects.update_or_create(
        datastore=datastore,
        key=key,
        defaults={"value_json": value_json},
    )

    data = {
        "key": entry.key,
        "value": entry.get_value(),
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }
    response = JsonResponse(data, status=201 if created else 200)
    return add_cors_headers(response, "GET, POST, OPTIONS")


@csrf_exempt
@require_http_methods(["GET", "PUT", "DELETE", "OPTIONS"])
@api_token_required
def get_entry(request: HttpRequest, name: str, key: str) -> JsonResponse:
    """
    Get, update, or delete a single entry by key.

    GET    /api/v1/datastores/<name>/entries/<key>/  — requires scope_datastores_read
    PUT    /api/v1/datastores/<name>/entries/<key>/  — requires scope_datastores_write
    DELETE /api/v1/datastores/<name>/entries/<key>/  — requires scope_datastores_write

    PUT body: {"value": <any JSON>}
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    if request.method == "PUT":
        return _update_entry(request, name, key)

    if request.method == "DELETE":
        return _delete_entry(request, name, key)

    # GET
    if not request.api_token.scope_datastores_read:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_datastores_read"}},
            status=403,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore

    try:
        entry = DataStoreEntry.objects.get(datastore=datastore, key=key)
    except DataStoreEntry.DoesNotExist:
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Entry '{key}' not found"}},
            status=404,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    data = {
        "key": entry.key,
        "value": entry.get_value(),
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }

    response = JsonResponse(data)
    return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")


def _update_entry(request: HttpRequest, name: str, key: str) -> JsonResponse:
    """Update an existing entry's value (PUT)."""
    if not request.api_token.scope_datastores_write:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_datastores_write"}},
            status=403,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    if len(request.body) > MAX_PAYLOAD_BYTES:
        response = JsonResponse(
            {"error": {"code": "PAYLOAD_TOO_LARGE", "message": "Request body exceeds 1 MB limit"}},
            status=413,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    try:
        body = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        response = JsonResponse(
            {"error": {"code": "INVALID_JSON", "message": "Request body must be valid JSON"}},
            status=400,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    if "value" not in body:
        response = JsonResponse(
            {"error": {"code": "MISSING_VALUE", "message": "value is required"}},
            status=400,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore

    try:
        entry = DataStoreEntry.objects.get(datastore=datastore, key=key)
    except DataStoreEntry.DoesNotExist:
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Entry '{key}' not found"}},
            status=404,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    entry.value_json = json.dumps(body["value"])
    entry.save()

    data = {
        "key": entry.key,
        "value": entry.get_value(),
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }
    response = JsonResponse(data)
    return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")


def _delete_entry(request: HttpRequest, name: str, key: str) -> JsonResponse:
    """Delete an entry (DELETE)."""
    if not request.api_token.scope_datastores_write:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have the required scope: scope_datastores_write"}},
            status=403,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore

    deleted_count, _ = DataStoreEntry.objects.filter(datastore=datastore, key=key).delete()
    if deleted_count == 0:
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Entry '{key}' not found"}},
            status=404,
        )
        return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")

    response = JsonResponse({"deleted": True, "key": key})
    return add_cors_headers(response, "GET, PUT, DELETE, OPTIONS")


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
@api_token_required
@require_scope("scope_datastores_write")
@write_rate_limit
def clear_datastore_entries(request: HttpRequest, name: str) -> JsonResponse:
    """
    Delete all entries in a datastore.

    POST /api/v1/datastores/<name>/clear/
    """
    if request.method == "OPTIONS":
        response = JsonResponse({})
        return add_cors_headers(response, "POST, OPTIONS")

    datastore = _get_authorized_datastore(request, name)
    if isinstance(datastore, JsonResponse):
        return datastore

    deleted_count, _ = DataStoreEntry.objects.filter(datastore=datastore).delete()
    response = JsonResponse({"deleted": deleted_count, "datastore": name})
    return add_cors_headers(response, "POST, OPTIONS")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_authorized_datastore(request: HttpRequest, name: str):
    """
    Get datastore if token has access.

    Returns the DataStore object or a JsonResponse error.
    """
    token = request.api_token

    try:
        datastore = DataStore.objects.get(name=name)
    except DataStore.DoesNotExist:
        response = JsonResponse(
            {"error": {"code": "NOT_FOUND", "message": f"Datastore '{name}' not found"}},
            status=404,
        )
        return add_cors_headers(response)

    # Check token access
    if token.datastore and token.datastore != datastore:
        response = JsonResponse(
            {"error": {"code": "FORBIDDEN", "message": "Token does not have access to this datastore"}},
            status=403,
        )
        return add_cors_headers(response)

    return datastore

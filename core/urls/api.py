"""
URL patterns for the PyRunner REST API.
"""

from django.urls import path

from core.views.api import (
    list_datastores,
    get_datastore,
    list_entries,
    get_entry,
)
from core.views.api.datastores import clear_datastore_entries
from core.views.api.scripts import (
    list_scripts,
    get_script,
    trigger_script,
    list_script_runs,
)
from core.views.api.runs import (
    list_runs,
    get_run,
    get_run_output,
    get_run_status,
    cancel_run,
)
from core.views.api.agent import health, agent_manifest

app_name = "api"

urlpatterns = [
    # ── Health & manifest (no auth / any-auth) ────────────────────────────────
    path("health/", health, name="health"),
    path("agent/manifest/", agent_manifest, name="agent_manifest"),

    # ── Scripts ───────────────────────────────────────────────────────────────
    path("scripts/", list_scripts, name="list_scripts"),
    path("scripts/<uuid:script_id>/", get_script, name="get_script"),
    path("scripts/<uuid:script_id>/run/", trigger_script, name="trigger_script"),
    path("scripts/<uuid:script_id>/runs/", list_script_runs, name="list_script_runs"),

    # ── Runs ──────────────────────────────────────────────────────────────────
    path("runs/", list_runs, name="list_runs"),
    path("runs/<uuid:run_id>/", get_run, name="get_run"),
    path("runs/<uuid:run_id>/output/", get_run_output, name="get_run_output"),
    path("runs/<uuid:run_id>/status/", get_run_status, name="get_run_status"),
    path("runs/<uuid:run_id>/cancel/", cancel_run, name="cancel_run"),

    # ── Datastores ────────────────────────────────────────────────────────────
    path("datastores/", list_datastores, name="list_datastores"),
    path("datastores/<str:name>/", get_datastore, name="get_datastore"),
    path("datastores/<str:name>/entries/", list_entries, name="list_entries"),
    path("datastores/<str:name>/entries/<str:key>/", get_entry, name="get_entry"),
    path("datastores/<str:name>/clear/", clear_datastore_entries, name="clear_datastore_entries"),
]

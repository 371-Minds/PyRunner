# Generated migration for AI agent automation enhancements

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_field_updates"),
    ]

    operations = [
        # ── DataStoreAPIToken: add scope fields ──────────────────────────────
        migrations.AddField(
            model_name="datastoreapitoken",
            name="scope_scripts",
            field=models.BooleanField(
                default=False,
                help_text="Allow listing and triggering scripts via the API",
            ),
        ),
        migrations.AddField(
            model_name="datastoreapitoken",
            name="scope_datastores_read",
            field=models.BooleanField(
                default=True,
                help_text="Allow reading datastore entries via the API",
            ),
        ),
        migrations.AddField(
            model_name="datastoreapitoken",
            name="scope_datastores_write",
            field=models.BooleanField(
                default=False,
                help_text="Allow creating/updating/deleting datastore entries via the API",
            ),
        ),
        migrations.AddField(
            model_name="datastoreapitoken",
            name="scope_runs_read",
            field=models.BooleanField(
                default=False,
                help_text="Allow reading run status and output via the API",
            ),
        ),

        # ── Script: add agent / discovery fields ─────────────────────────────
        migrations.AddField(
            model_name="script",
            name="category",
            field=models.CharField(
                choices=[
                    ("general", "General"),
                    ("data", "Data"),
                    ("integration", "Integration"),
                    ("ai", "AI"),
                    ("utility", "Utility"),
                ],
                default="general",
                help_text="Script category for agent discovery and filtering",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="script",
            name="is_template",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Template scripts can be discovered by agents but cannot be "
                    "triggered via API"
                ),
            ),
        ),
        migrations.AddField(
            model_name="script",
            name="api_execution_enabled",
            field=models.BooleanField(
                default=True,
                help_text="Allow this script to be triggered via the REST API",
            ),
        ),
        migrations.AddField(
            model_name="script",
            name="input_schema",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text=(
                    "Optional JSON Schema describing expected inputs for this script. "
                    "Agents use this to know what to pass when triggering the script."
                ),
            ),
        ),

        # ── Run: add agent session / structured I-O fields ───────────────────
        migrations.AddField(
            model_name="run",
            name="agent_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Identifier of the AI agent that triggered this run",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="run",
            name="session_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Session/workflow identifier for grouping related runs",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="run",
            name="trigger_script",
            field=models.ForeignKey(
                blank=True,
                help_text="Script that triggered this run (for agent-orchestrated chaining)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="triggered_runs",
                to="core.script",
            ),
        ),
        migrations.AddField(
            model_name="run",
            name="structured_output",
            field=models.JSONField(
                blank=True,
                null=True,
                help_text=(
                    "Structured JSON output written by the script via pyrunner_output helpers"
                ),
            ),
        ),
        migrations.AddField(
            model_name="run",
            name="callback_url",
            field=models.URLField(
                blank=True,
                help_text="URL to POST a completion event to when this run finishes",
                max_length=500,
            ),
        ),
    ]

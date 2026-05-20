"""
Trello synchronization service for run Kanban tracking.
"""

import logging

import requests
from django.utils import timezone

from core.models import GlobalSettings, Run
from core.services.encryption_service import EncryptionError, EncryptionService

logger = logging.getLogger(__name__)


class TrelloService:
    """Service for syncing run states to Trello cards/lists."""

    API_BASE = "https://api.trello.com/1"
    REQUEST_TIMEOUT = 15

    @classmethod
    def _get_credentials(cls) -> tuple[str, str]:
        settings = GlobalSettings.get_settings()
        if not settings.trello_api_key_encrypted or not settings.trello_token_encrypted:
            raise ValueError("Trello API key/token are not configured")

        try:
            api_key = EncryptionService.decrypt(settings.trello_api_key_encrypted)
            token = EncryptionService.decrypt(settings.trello_token_encrypted)
        except EncryptionError as e:
            raise ValueError(f"Failed to decrypt Trello credentials: {e}")

        if not api_key or not token:
            raise ValueError("Trello API key/token are empty")

        return api_key, token

    @classmethod
    def _auth_params(cls) -> dict:
        api_key, token = cls._get_credentials()
        return {"key": api_key, "token": token}

    @classmethod
    def _request(cls, method: str, path: str, **kwargs) -> requests.Response:
        params = kwargs.pop("params", {})
        params.update(cls._auth_params())
        return requests.request(
            method=method,
            url=f"{cls.API_BASE}{path}",
            params=params,
            timeout=cls.REQUEST_TIMEOUT,
            **kwargs,
        )

    @classmethod
    def is_configured(cls) -> bool:
        """Check if Trello integration has required settings."""
        settings = GlobalSettings.get_settings()
        return bool(
            settings.trello_api_key_encrypted
            and settings.trello_token_encrypted
            and settings.trello_board_id
            and settings.trello_list_queued_id
            and settings.trello_list_running_id
            and settings.trello_list_success_id
            and settings.trello_list_failed_id
        )

    @classmethod
    def get_status(cls) -> dict:
        """Return Trello integration status for UI display."""
        settings = GlobalSettings.get_settings()
        return {
            "enabled": settings.trello_enabled,
            "configured": cls.is_configured(),
            "board_id": settings.trello_board_id or None,
            "list_ids": {
                "queued": settings.trello_list_queued_id or None,
                "running": settings.trello_list_running_id or None,
                "success": settings.trello_list_success_id or None,
                "failed": settings.trello_list_failed_id or None,
            },
            "last_synced": settings.trello_last_synced_at,
            "last_error": settings.trello_last_sync_error or None,
        }

    @classmethod
    def test_connection(cls) -> tuple[bool, str]:
        """Validate Trello credentials and board/list access."""
        settings = GlobalSettings.get_settings()

        if not cls.is_configured():
            return False, "Trello configuration is incomplete"

        try:
            board_res = cls._request("GET", f"/boards/{settings.trello_board_id}")
            if not board_res.ok:
                return False, f"Failed to access board ({board_res.status_code})"

            list_ids = [
                settings.trello_list_queued_id,
                settings.trello_list_running_id,
                settings.trello_list_success_id,
                settings.trello_list_failed_id,
            ]

            for list_id in list_ids:
                list_res = cls._request("GET", f"/lists/{list_id}")
                if not list_res.ok:
                    return False, f"Failed to access list {list_id} ({list_res.status_code})"

            return True, "Successfully connected to Trello board and lists"
        except Exception as e:
            return False, f"Trello connection failed: {e}"

    @classmethod
    def _get_target_list_id(cls, run: Run) -> str:
        settings = GlobalSettings.get_settings()
        if run.status == Run.Status.RUNNING:
            return settings.trello_list_running_id
        if run.status == Run.Status.SUCCESS:
            return settings.trello_list_success_id
        if run.status in [Run.Status.FAILED, Run.Status.TIMEOUT, Run.Status.CANCELLED]:
            return settings.trello_list_failed_id
        return settings.trello_list_queued_id

    @classmethod
    def _mark_sync_success(cls) -> None:
        settings = GlobalSettings.get_settings()
        settings.trello_last_synced_at = timezone.now()
        settings.trello_last_sync_error = ""
        settings.save(update_fields=["trello_last_synced_at", "trello_last_sync_error"])

    @classmethod
    def _mark_sync_error(cls, error: str) -> None:
        settings = GlobalSettings.get_settings()
        settings.trello_last_sync_error = error
        settings.save(update_fields=["trello_last_sync_error"])

    @classmethod
    def sync_run_status(cls, run: Run) -> bool:
        """
        Create/update Trello card for a run and move it to the mapped list.
        """
        settings = GlobalSettings.get_settings()
        if not settings.trello_enabled:
            return False
        if not cls.is_configured():
            cls._mark_sync_error("Trello sync enabled but configuration is incomplete.")
            return False

        target_list_id = cls._get_target_list_id(run)
        card_name = f"{run.script.name} [{run.status.upper()}]"
        card_desc = (
            f"Run ID: {run.id}\n"
            f"Script: {run.script.name}\n"
            f"Status: {run.status}\n"
            f"Task ID: {run.task_id or '-'}\n"
            f"Duration: {run.duration_display}\n"
        )

        try:
            if run.trello_card_id:
                response = cls._request(
                    "PUT",
                    f"/cards/{run.trello_card_id}",
                    params={
                        "idList": target_list_id,
                        "name": card_name,
                        "desc": card_desc,
                        "closed": "false",
                    },
                )
                if not response.ok:
                    run.trello_card_id = ""
                    run.save(update_fields=["trello_card_id"])
            if not run.trello_card_id:
                create_response = cls._request(
                    "POST",
                    "/cards",
                    params={
                        "idList": target_list_id,
                        "name": card_name,
                        "desc": card_desc,
                    },
                )
                if not create_response.ok:
                    error = (
                        f"Trello card sync failed ({create_response.status_code}): "
                        f"{create_response.text[:500]}"
                    )
                    cls._mark_sync_error(error)
                    logger.error(error)
                    return False

                data = create_response.json()
                run.trello_card_id = data.get("id", "")
                if run.trello_card_id:
                    run.save(update_fields=["trello_card_id"])

            cls._mark_sync_success()
            return True
        except Exception as e:
            error = f"Trello sync error for run {run.id}: {e}"
            cls._mark_sync_error(error)
            logger.error(error)
            return False

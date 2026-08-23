"""Fenced PRD-106 handler for deterministic configuration snapshot rebuilds."""

from __future__ import annotations

from metahub_shared.runtime.errors import ApiError
from pydantic import BaseModel, ConfigDict, Field

from metahub_control.platform_jobs.catalog import register_job_handler
from metahub_control.platform_jobs.runtime import (
    PlatformJobContext,
    PlatformJobFailure,
    PlatformJobRetry,
)

from .service import publish_rebuild_in_transaction


class ConfigSnapshotRebuildInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    rebuild_job_id: str = Field(pattern=r"^crj_[0-9a-f]{32}$")
    workspace_id: str = Field(pattern=r"^wsp_[0-9a-f]{32}$")
    target_generation: int = Field(ge=1)


def _rebuild_handler(context: PlatformJobContext, payload: ConfigSnapshotRebuildInput) -> str:
    context.checkpoint(
        {
            "phase": "canonicalize",
            "rebuild_job_id": payload.rebuild_job_id,
            "target_generation": payload.target_generation,
            "workspace_id": payload.workspace_id,
        }
    )
    result: dict[str, str] = {}

    def publish(connection) -> None:
        result["manifest_sha256"] = publish_rebuild_in_transaction(
            connection,
            context.config,
            rebuild_job_id=payload.rebuild_job_id,
            platform_job_id=context.lease.job_id,
            target_generation=payload.target_generation,
        )

    try:
        committed = context.commit_effect(
            "config-rebuild:" + payload.rebuild_job_id,
            {
                "rebuild_job_id": payload.rebuild_job_id,
                "target_generation": payload.target_generation,
                "workspace_id": payload.workspace_id,
            },
            publish,
        )
    except ApiError as exc:
        if exc.code in {
            "config_distribution.kms_unavailable",
            "config_distribution.unavailable",
        }:
            raise PlatformJobRetry(exc.code) from None
        raise PlatformJobFailure(exc.code) from None
    if not committed:
        from metahub_shared.db import connect_database

        connection = connect_database(context.config.database_url)
        try:
            row = connection.execute(
                "SELECT result_manifest_sha256,status FROM config_rebuild_jobs "
                "WHERE config_rebuild_job_id=?",
                (payload.rebuild_job_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row["status"] != "completed" or row["result_manifest_sha256"] is None:
            raise PlatformJobFailure("config_distribution.rebuild_stale")
        result["manifest_sha256"] = str(row["result_manifest_sha256"])
    context.checkpoint(
        {
            "manifest_sha256": result["manifest_sha256"],
            "phase": "published",
            "rebuild_job_id": payload.rebuild_job_id,
            "target_generation": payload.target_generation,
        }
    )
    return "config.rebuild_completed"


register_job_handler(
    "config.snapshot.rebuild",
    input_model=ConfigSnapshotRebuildInput,
    handler=_rebuild_handler,
)

__all__ = ["ConfigSnapshotRebuildInput"]

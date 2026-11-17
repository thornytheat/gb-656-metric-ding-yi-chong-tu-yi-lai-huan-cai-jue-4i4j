"""M8 编排：制作备份、预演差异、执行/续跑恢复、回看保全批次。

只做编排与对外形状，完整性判定在 checksum/guard，分步执行在 restore。
"""

from __future__ import annotations

from app import errors, store
from app.modules.m8_recovery import backup as backup_mod
from app.modules.m8_recovery import checksum, guard, preview as preview_mod
from app.modules.m8_recovery import restore as restore_mod


# ---------- 备份 ----------

def create_backup(note: str | None = None) -> dict:
    if store.count_runs() == 0 and not store.all_snapshot_payloads():
        raise errors.BadInput("当前库没有可备份的批次或快照",
                              detail={"reason": "NOTHING_TO_BACKUP"})
    return backup_mod.make_backup(note)


def list_backups() -> list[dict]:
    return store.list_backups()


def backup_detail(backup_id: str) -> dict:
    record = store.get_backup(backup_id)
    if record is None:
        raise errors.NotFound(f"备份不存在：{backup_id}",
                              detail={"backup_id": backup_id})
    payload = checksum.verify(record)
    return {
        "backup_id": record["backup_id"],
        "created_at": record["created_at"],
        "run_count": record["run_count"],
        "snapshot_count": record["snapshot_count"],
        "quarantine_count": record["quarantine_count"],
        "seq_max": record["seq_max"],
        "latest_run_id": record["latest_run_id"],
        "payload_sha256": record["payload_sha256"],
        "note": payload.get("note") or "",
        "table_checksums": payload["table_checksums"],
        "verified": True,
    }


# ---------- 预演 ----------

def preview(backup_id: str) -> dict:
    return preview_mod.preview(backup_id)


# ---------- 恢复 ----------

def start_restore(backup_id: str) -> dict:
    job = restore_mod.create_job(backup_id)
    return restore_mod.resume(job["job_id"])


def resume_restore(job_id: str) -> dict:
    return restore_mod.resume(job_id)


def job_status(job_id: str) -> dict:
    job = store.get_restore_job(job_id)
    if job is None:
        raise errors.NotFound(f"恢复单不存在：{job_id}", detail={"job_id": job_id})
    return job


def list_jobs() -> list[dict]:
    return store.list_restore_jobs()


def assert_batch_allowed(base_version_seq) -> None:
    """跑批入口的两道闸：恢复并发 + 过期基线。"""
    guard.assert_no_active_restore()
    guard.check_baseline(base_version_seq, store.current_version_seq())


# ---------- 保全凭据（只读回看） ----------

def preserved_runs(job_id: str | None = None) -> list[dict]:
    if job_id is not None and store.get_restore_job(job_id) is None:
        raise errors.NotFound(f"恢复单不存在：{job_id}", detail={"job_id": job_id})
    rows = store.list_preservation_jobs()
    if job_id is not None:
        rows = [row for row in rows if row["job_id"] == job_id]
    return rows


def preserved_run_detail(job_id: str, run_id: str) -> dict:
    payload = store.get_preserved_run_payload(job_id, run_id)
    if payload is None:
        raise errors.NotFound("保全批次不存在",
                              detail={"job_id": job_id, "run_id": run_id})
    return payload


def preserved_snapshots(job_id: str) -> list[dict]:
    if store.get_restore_job(job_id) is None:
        raise errors.NotFound(f"恢复单不存在：{job_id}", detail={"job_id": job_id})
    return store.list_preserved_snapshots(job_id)


def preserved_quarantine(job_id: str) -> list[dict]:
    if store.get_restore_job(job_id) is None:
        raise errors.NotFound(f"恢复单不存在：{job_id}", detail={"job_id": job_id})
    return store.list_preserved_quarantine(job_id)


def current_baseline() -> dict:
    return store.get_baseline() or {}


def seq_hwm() -> int:
    return store.get_seq_hwm()

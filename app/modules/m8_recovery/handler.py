"""M8 入口：备份恢复页、备份/预演/恢复/保全凭据接口。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app import errors, store, web
from app.modules.m8_recovery import restore as restore_mod
from app.modules.m8_recovery import service

router = APIRouter()

ALLOWED_BACKUP_KEYS = {"note"}
ALLOWED_RESTORE_KEYS = {"backup_id"}
ALLOWED_CRASH_KEYS = {"job_id", "after_step"}


@router.get("/recovery", response_class=HTMLResponse)
def recovery_page(request: Request):
    run = store.latest_run()
    return web.page_or_404(
        request,
        "recovery.html",
        "recovery",
        run,
        backups=service.list_backups(),
        jobs=service.list_jobs(),
        preserved=service.preserved_runs(),
        baseline=service.current_baseline(),
        seq_hwm=service.seq_hwm(),
        current_seq=store.current_version_seq(),
        steps=list(store.RESTORE_STEPS),
    )


@router.post("/api/recovery/backups")
def create_backup(payload: dict | None = None):
    body = payload or {}
    unknown = set(body) - ALLOWED_BACKUP_KEYS
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        raise errors.BadInput("note 必须是字符串", detail={"field": "note"})
    return service.create_backup(note)


@router.get("/api/recovery/backups")
def backups_api():
    return {"backups": service.list_backups(), "baseline": service.current_baseline(),
            "seq_hwm": service.seq_hwm(), "current_seq": store.current_version_seq()}


@router.get("/api/recovery/backups/{backup_id}")
def backup_detail_api(backup_id: str):
    return service.backup_detail(backup_id)


@router.get("/api/recovery/preview/{backup_id}")
def preview_api(backup_id: str):
    return service.preview(backup_id)


@router.post("/api/recovery/restore")
def start_restore_api(payload: dict | None = None):
    body = payload or {}
    unknown = set(body) - ALLOWED_RESTORE_KEYS
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})
    backup_id = str(body.get("backup_id") or "").strip()
    if not backup_id:
        raise errors.BadInput("未指定备份", detail={"field": "backup_id"})
    return service.start_restore(backup_id)


@router.post("/api/recovery/restore/{job_id}/resume")
def resume_api(job_id: str):
    return service.resume_restore(job_id)


@router.get("/api/recovery/jobs")
def jobs_api():
    return {"jobs": service.list_jobs()}


@router.get("/api/recovery/jobs/{job_id}")
def job_api(job_id: str):
    return service.job_status(job_id)


@router.get("/api/recovery/preserved")
def preserved_api(job_id: str | None = None):
    rows = service.preserved_runs(job_id)
    return {"preserved": rows, "count": len(rows)}


@router.get("/api/recovery/preserved/{job_id}/snapshots")
def preserved_snapshots_api(job_id: str):
    return {"snapshots": service.preserved_snapshots(job_id)}


@router.get("/api/recovery/preserved/{job_id}/quarantine")
def preserved_quarantine_api(job_id: str):
    return {"quarantine": service.preserved_quarantine(job_id)}


@router.get("/api/recovery/preserved/{job_id}/{run_id}")
def preserved_detail_api(job_id: str, run_id: str):
    return service.preserved_run_detail(job_id, run_id)


@router.post("/api/recovery/test/fault")
def fault_api(payload: dict | None = None):
    """测试钩子：让指定恢复单在某步落盘后中断一次，验证续跑不重复。"""
    body = payload or {}
    unknown = set(body) - ALLOWED_CRASH_KEYS
    if unknown:
        raise errors.BadInput("出现未知参数", detail={"keys": sorted(unknown)})
    job_id = str(body.get("job_id") or "").strip()
    after_step = str(body.get("after_step") or "").strip()
    if not job_id or after_step not in store.RESTORE_STEPS:
        raise errors.BadInput(
            "故障参数不合法",
            detail={"job_id": job_id, "after_step": after_step,
                    "valid_steps": list(store.RESTORE_STEPS)},
        )
    job = service.job_status(job_id)
    if job["status"] == "finished":
        raise errors.Conflict("恢复单已完成，无法注入中断",
                              detail={"job_id": job_id})
    restore_mod.arm_crash_after(job_id, after_step)
    return {"armed": True, "job_id": job_id, "after_step": after_step}

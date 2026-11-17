"""恢复守卫：跑批并发拦截、过期基线拒绝、备份预校验。

恢复期间跑批一律拒绝；跑批带的基线序号落后于当前基线也拒绝，
避免旧结果在恢复切换后覆盖新基线。判定不依赖页面，接口与测试共用。
"""

from __future__ import annotations

from app import errors, store
from app.modules.m6_batches import guard as stale_guard


class RestoreInProgress(errors.Conflict):
    status = 409
    code = "RESTORE_IN_PROGRESS"
    label = "恢复进行中"


class RestoreConflict(errors.Conflict):
    status = 409
    code = "RESTORE_CONFLICT"
    label = "恢复冲突"


def assert_no_active_restore() -> None:
    """跑批入口调用：恢复未跑完时拒绝新批次，防止与整表替换交叉。"""
    job = store.active_restore_job()
    if job is not None:
        raise RestoreInProgress(
            f"恢复单 {job['job_id']} 尚未完成，跑批已暂停",
            detail={"job_id": job["job_id"], "status": job["status"],
                    "steps_done": job["steps_done"], "steps_total": job["steps_total"],
                    "reason": "RESTORE_IN_PROGRESS"},
        )


def check_baseline(base_version_seq, current_seq: int) -> None:
    """复用 M6 的乐观锁：过期基线 409，恢复切换后旧序号自然失配。"""
    stale_guard.check_stale(base_version_seq, current_seq)


def assert_backup_usable(backup_id: str) -> dict:
    from app.modules.m8_recovery import backup as backup_mod

    return backup_mod.load_verified(backup_id)

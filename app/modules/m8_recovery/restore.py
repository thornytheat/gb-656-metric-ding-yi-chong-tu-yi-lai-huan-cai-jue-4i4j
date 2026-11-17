"""恢复执行：分步、可续跑、重试不重复。

每一步独立事务并立即落盘步骤号；中断后重新调用只补未完成的步。
保全数据写入 preserved_* 只读凭据表，业务表整体切回备份基线，
批次号高水位只增不减，已保全批次编号永不复用。
"""

from __future__ import annotations

import threading

from app import config, errors, manifest as manifest_mod, store
from app.modules.m8_recovery import guard

# 进程内串行：同一时刻只允许一个恢复执行，配合库内状态拦跨进程并发
_EXEC_LOCK = threading.Lock()

# 测试钩子：在指定步骤落盘后、下一步开始前注入一次中断
_CRASH_AFTER: dict[str, str] = {}


class _InjectedInterrupt(Exception):
    """模拟进程中断：步骤已落盘，状态置 interrupted，钩子只触发一次。"""


def arm_crash_after(job_id: str, step: str) -> None:
    _CRASH_AFTER[job_id] = step


def _crash_point(job_id: str, step: str) -> None:
    if _CRASH_AFTER.get(job_id) == step:
        _CRASH_AFTER.pop(job_id, None)
        raise _InjectedInterrupt(step)


def create_job(backup_id: str) -> dict:
    """建恢复单：校验备份、拒绝并发、冻结基线快照。"""
    with _EXEC_LOCK:
        active = store.active_restore_job()
        if active is not None:
            raise guard.RestoreConflict(
                f"已有未完成的恢复单 {active['job_id']}，请先续跑或查看状态",
                detail={"job_id": active["job_id"], "status": active["status"],
                        "reason": "RESTORE_ALREADY_RUNNING"},
            )
        payload = guard.assert_backup_usable(backup_id)
        live_runs = _live_runs()
        baseline_seq = store.current_version_seq()
        now = config.now_utc().isoformat()
        job_id = "rj-" + manifest_mod.sha256_bytes(
            f"{backup_id}|{now}|{baseline_seq}".encode("utf-8")
        )[:12]
        job = {
            "job_id": job_id,
            "backup_id": backup_id,
            "status": "running",
            "steps_total": len(store.RESTORE_STEPS),
            "steps_done": 0,
            "created_at": now,
            "updated_at": now,
            "finished_at": None,
            "baseline_seq": baseline_seq,
            "preserved_count": 0,
            "last_error": None,
        }
        store.save_restore_job(job)
        # 冻结计划，保证续跑时与首次看到的库状态一致
        store.set_kv(f"restore_plan:{job_id}", {
            "backup_id": backup_id,
            "preserve_run_ids": sorted(
                run["run_id"] for run in live_runs
                if run["run_id"] not in {r["run_id"] for r in payload["runs"]}
            ),
            "created_at": now,
        })
        return job


def resume(job_id: str) -> dict:
    """执行或续跑：已完成的步直接跳过，失败保留 interrupted 状态。"""
    with _EXEC_LOCK:
        job = store.get_restore_job(job_id)
        if job is None:
            raise errors.NotFound(f"恢复单不存在：{job_id}",
                                  detail={"job_id": job_id})
        if job["status"] == "finished":
            return job
        plan = store.get_kv(f"restore_plan:{job_id}")
        if plan is None:
            raise errors.BadInput("恢复计划丢失，无法续跑",
                                  detail={"job_id": job_id,
                                          "reason": "RESTORE_PLAN_MISSING"})
        # 续跑前再次验备份：损坏备份不得在重试时蒙混过关
        payload = guard.assert_backup_usable(job["backup_id"])

        steps = list(store.RESTORE_STEPS)
        job["status"] = "running"
        job["last_error"] = None
        store.save_restore_job(job)
        try:
            for index, step in enumerate(steps):
                if index < job["steps_done"]:
                    continue  # 已落盘的步不重复执行
                _run_step(step, job, payload, plan)
                job["steps_done"] = index + 1
                job["updated_at"] = config.now_utc().isoformat()
                store.save_restore_job(job)
                _crash_point(job_id, step)
        except _InjectedInterrupt as exc:
            # 模拟中断：到此前的步都已落盘，按 interrupted 留存，等续跑
            job["status"] = "interrupted"
            job["last_error"] = f"模拟中断于步骤 {exc.args[0]}"
            job["updated_at"] = config.now_utc().isoformat()
            store.save_restore_job(job)
            return store.get_restore_job(job_id)
        except errors.FieldmapError as exc:
            job["status"] = "interrupted"
            job["last_error"] = exc.message
            job["updated_at"] = config.now_utc().isoformat()
            store.save_restore_job(job)
            raise
        job["status"] = "finished"
        job["finished_at"] = config.now_utc().isoformat()
        job["updated_at"] = job["finished_at"]
        store.save_restore_job(job)
        return store.get_restore_job(job_id)


def _run_step(step: str, job: dict, payload: dict, plan: dict) -> None:
    if step == "restore_snapshots":
        store.replace_snapshots(payload["snapshots"])
    elif step == "preserve_runs":
        _preserve(job, payload, plan)
    elif step == "restore_runs":
        store.replace_runs(payload["runs"])
    elif step == "restore_quarantine":
        store.replace_quarantine(payload["quarantine"])
    elif step == "advance_seq_hwm":
        _advance_hwm_and_baseline(job, payload)
    else:  # pragma: no cover - 常量来自 store，未知步属于编程错误
        raise errors.BadInput("未知恢复步骤", detail={"step": step})


def _preserve(job: dict, payload: dict, plan: dict) -> None:
    """把备份之外的后续批次固化为只读凭据，整表替换前完成。"""
    now = config.now_utc().isoformat()
    backup_run_ids = {run["run_id"] for run in payload["runs"]}
    preserved = 0
    preserved_schemas: set[str] = set()
    for run in _live_runs():
        if run["run_id"] in backup_run_ids:
            continue
        if run["run_id"] not in set(plan["preserve_run_ids"]):
            continue
        store.save_preserved_run(job["job_id"], run, now)
        for schema_id in (run["old_schema_id"], run["new_schema_id"]):
            if schema_id and schema_id not in preserved_schemas:
                snapshot = store.load_snapshot(schema_id)
                if snapshot is not None:
                    store.save_preserved_snapshot(job["job_id"], snapshot, now)
                    preserved_schemas.add(schema_id)
        preserved += 1
    # 保全批次引用到、但备份里没有的快照隔离行一并凭据化
    for schema_id in sorted(preserved_schemas):
        rows = store.list_quarantine(schema_id)
        if rows:
            store.save_preserved_quarantine(job["job_id"], [
                dict(row, schema_id=schema_id) for row in rows
            ])
    job["preserved_count"] = preserved


def _advance_hwm_and_baseline(job: dict, payload: dict) -> None:
    backup_max = max((int(run["version_seq"]) for run in payload["runs"]), default=0)
    hwm = max(store.get_seq_hwm(), int(job["baseline_seq"]), backup_max)
    store.set_seq_hwm(hwm)
    store.set_baseline({
        "backup_id": job["backup_id"],
        "job_id": job["job_id"],
        "switched_at": config.now_utc().isoformat(),
        "baseline_seq": backup_max,
        "seq_hwm": hwm,
        "ruleset": config.RULESET_VERSION,
    })


def _live_runs() -> list[dict]:
    import json

    return [json.loads(text) for text in store.all_run_payloads()]

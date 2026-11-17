"""预演差异：不写库，只给出恢复将保全、删除、还原的清单与表级摘要。"""

from __future__ import annotations

import json

from app import errors, store
from app.modules.m8_recovery import checksum


def _keyed(items: list[dict], key: str) -> dict:
    return {item[key]: item for item in items}


def preview(backup_id: str) -> dict:
    record = store.get_backup(backup_id)
    if record is None:
        raise errors.NotFound(f"备份不存在：{backup_id}",
                              detail={"backup_id": backup_id})
    payload = checksum.verify(record)

    backup_runs = payload["runs"]
    backup_snapshots = payload["snapshots"]
    backup_quarantine = payload["quarantine"]

    live_runs = [json.loads(text) for text in store.all_run_payloads()]
    live_snapshots = [json.loads(text) for text in store.all_snapshot_payloads()]

    backup_run_ids = {run["run_id"] for run in backup_runs}
    live_run_ids = {run["run_id"] for run in live_runs}
    preserve = sorted(live_run_ids - backup_run_ids,
                      key=lambda rid: next(r["version_seq"] for r in live_runs
                                           if r["run_id"] == rid))
    restore = sorted(backup_run_ids,
                     key=lambda rid: next(r["version_seq"] for r in backup_runs
                                          if r["run_id"] == rid))
    backup_snapshot_ids = {s["schema_id"] for s in backup_snapshots}
    live_snapshot_ids = {s["schema_id"] for s in live_snapshots}
    preserve_snapshot_ids = sorted(live_snapshot_ids - backup_snapshot_ids)

    # 隔离区按 schema 汇总，避免逐行刷屏
    q_backup: dict[str, int] = {}
    for row in backup_quarantine:
        q_backup[row["schema_id"]] = q_backup.get(row["schema_id"], 0) + 1
    q_live: dict[str, int] = {}
    for row in store.all_quarantine_rows():
        q_live[row["schema_id"]] = q_live.get(row["schema_id"], 0) + 1

    seq_max_live = max((int(run["version_seq"]) for run in live_runs), default=0)
    seq_max_backup = max((int(run["version_seq"]) for run in backup_runs), default=0)
    hwm_now = store.get_seq_hwm()

    return {
        "backup_id": backup_id,
        "verified": True,
        "payload_sha256": record["payload_sha256"],
        "checksums": {
            "backup": payload["table_checksums"],
            "live": checksum.live_table_checksums(),
        },
        "runs": {
            "preserve": [_run_brief(run) for run in live_runs
                         if run["run_id"] in preserve],
            "restore": [_run_brief(run) for run in backup_runs
                        if run["run_id"] in restore],
            "preserve_count": len(preserve),
            "restore_count": len(restore),
        },
        "snapshots": {
            "preserve_count": len(preserve_snapshot_ids),
            "preserve_schema_ids": preserve_snapshot_ids,
            "restore_count": len(backup_snapshot_ids),
        },
        "quarantine": {
            "backup_rows": len(backup_quarantine),
            "live_rows": len(store.all_quarantine_rows()),
            "backup_by_schema": q_backup,
            "live_by_schema": q_live,
        },
        "seq": {
            "live_max": seq_max_live,
            "backup_max": seq_max_backup,
            "hwm_before": hwm_now,
            "hwm_after": max(hwm_now, seq_max_live, seq_max_backup),
        },
        "steps": list(store.RESTORE_STEPS),
    }


def _run_brief(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "version_seq": int(run["version_seq"]),
        "version_label_old": run.get("version_label_old"),
        "version_label_new": run.get("version_label_new"),
        "created_at": run.get("created_at"),
    }

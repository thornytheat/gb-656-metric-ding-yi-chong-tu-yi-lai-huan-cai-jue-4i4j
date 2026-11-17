"""备份单制作：把当前库的三张业务表固化成带校验和的只读记录。"""

from __future__ import annotations

import json

from app import config, manifest as manifest_mod, store
from app.modules.m8_recovery import checksum


def make_backup(note: str | None = None) -> dict:
    snapshots = [json.loads(text) for text in store.all_snapshot_payloads()]
    runs = [json.loads(text) for text in store.all_run_payloads()]
    quarantine = store.all_quarantine_rows()

    payload = {
        "format": checksum.BACKUP_FORMAT,
        "ruleset": config.RULESET_VERSION,
        "note": (note or "").strip()[:200],
        "snapshots": snapshots,
        "runs": runs,
        "quarantine": quarantine,
        "table_checksums": checksum.table_checksums(snapshots, runs, quarantine),
    }
    payload_text = checksum.canonical_dumps(payload)
    payload_sha = checksum.checksum_of(payload)
    created_at = config.now_utc().isoformat()
    backup_id = "bk-" + hashlib_short(payload_sha + "|" + created_at)
    seq_max = max((int(run["version_seq"]) for run in runs), default=0)
    latest_run_id = runs[-1]["run_id"] if runs else None

    record = {
        "backup_id": backup_id,
        "created_at": created_at,
        "run_count": len(runs),
        "snapshot_count": len(snapshots),
        "quarantine_count": len(quarantine),
        "seq_max": seq_max,
        "latest_run_id": latest_run_id,
        "payload_sha256": payload_sha,
        "payload_json": payload_text,
    }
    store.save_backup(record)
    return {
        "backup_id": backup_id,
        "created_at": created_at,
        "run_count": len(runs),
        "snapshot_count": len(snapshots),
        "quarantine_count": len(quarantine),
        "seq_max": seq_max,
        "latest_run_id": latest_run_id,
        "payload_sha256": payload_sha,
        "note": payload["note"],
    }


def hashlib_short(text: str) -> str:
    return manifest_mod.sha256_bytes(text.encode("utf-8"))[:12]


def load_verified(backup_id: str) -> dict:
    record = store.get_backup(backup_id)
    if record is None:
        from app import errors

        raise errors.NotFound(f"备份不存在：{backup_id}",
                              detail={"backup_id": backup_id})
    return checksum.verify(record)

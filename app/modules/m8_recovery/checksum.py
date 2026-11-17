"""备份完整性：载荷结构、表级校验和、损坏拒绝。

损坏判定只认结构缺失与校验和不符；调用方拿到的损坏原因固定落在 detail 里。
"""

from __future__ import annotations

import hashlib

from app import errors, store

BACKUP_FORMAT = "metricgraph-backup/1"
BACKUP_TABLES = ("snapshots", "runs", "quarantine")


def canonical_dumps(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def checksum_of(payload: dict) -> str:
    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()


def table_checksums(snapshots: list[dict], runs: list[dict],
                    quarantine: list[dict]) -> dict[str, str]:
    """每张表单算摘要，预演差异与损坏定位都靠它。"""
    return {
        "snapshots": hashlib.sha256(canonical_dumps(snapshots).encode()).hexdigest(),
        "runs": hashlib.sha256(canonical_dumps(runs).encode()).hexdigest(),
        "quarantine": hashlib.sha256(canonical_dumps(quarantine).encode()).hexdigest(),
    }


def verify(record: dict) -> dict:
    """核对备份单：整体摘要不符或结构缺失都拒绝使用。"""
    problems: list[str] = []
    payload_text = record.get("payload_json")
    if not payload_text:
        raise errors.BadInput("备份缺少载荷", detail={"backup_id": record.get("backup_id")})
    try:
        import json

        payload = json.loads(payload_text)
    except (ValueError, TypeError) as exc:
        raise errors.BadInput(
            "备份载荷无法解析，拒绝使用",
            detail={"backup_id": record.get("backup_id"), "reason": "BACKUP_UNREADABLE",
                    "cause": str(exc)},
        )
    if not isinstance(payload, dict):
        problems.append("BACKUP_NOT_OBJECT")

    if isinstance(payload, dict):
        if payload.get("format") != BACKUP_FORMAT:
            problems.append("BACKUP_FORMAT_MISMATCH")
        for table in BACKUP_TABLES:
            if not isinstance(payload.get(table), list):
                problems.append(f"BACKUP_TABLE_MISSING:{table}")
        checksums = payload.get("table_checksums")
        if not isinstance(checksums, dict):
            problems.append("BACKUP_CHECKSUMS_MISSING")
        else:
            for table in BACKUP_TABLES:
                actual = hashlib.sha256(
                    canonical_dumps(payload.get(table) or []).encode()
                ).hexdigest()
                if checksums.get(table) != actual:
                    problems.append(f"BACKUP_CHECKSUM_MISMATCH:{table}")

    overall = checksum_of(payload)
    if record.get("payload_sha256") != overall:
        problems.append("BACKUP_PAYLOAD_SHA_MISMATCH")

    if problems:
        raise errors.BadInput(
            "备份已损坏，拒绝使用",
            detail={"backup_id": record.get("backup_id"),
                    "reason": "BACKUP_CORRUPT", "problems": problems},
        )
    return payload


def live_table_checksums() -> dict[str, str]:
    import json

    snapshots = [json.loads(text) for text in store.all_snapshot_payloads()]
    runs = [json.loads(text) for text in store.all_run_payloads()]
    return table_checksums(snapshots, runs, store.all_quarantine_rows())

"""SQLite 读写：批次快照、运行清单、隔离行、键值状态。

只放数据访问，不放判定；调用方拿到的是普通字典。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

from app import config

_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    schema_id      TEXT PRIMARY KEY,
    version_label  TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    source_sha256  TEXT NOT NULL,
    encoding       TEXT NOT NULL,
    bom            INTEGER NOT NULL,
    field_count    INTEGER NOT NULL,
    blocked_count  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id         TEXT PRIMARY KEY,
    batch_key      TEXT NOT NULL UNIQUE,
    old_schema_id  TEXT NOT NULL,
    new_schema_id  TEXT NOT NULL,
    version_seq    INTEGER NOT NULL,
    input_sha256   TEXT NOT NULL,
    ruleset        TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    effective_from TEXT NOT NULL,
    effective_to   TEXT,
    summary_json   TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quarantine (
    schema_id     TEXT NOT NULL,
    line          INTEGER,
    table_name    TEXT,
    column_name   TEXT,
    reason        TEXT NOT NULL,
    blocking      INTEGER NOT NULL,
    detail        TEXT,
    raw           TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
-- M8 备份恢复：备份单、可续跑恢复单、只读保全凭据、批次号高水位
CREATE TABLE IF NOT EXISTS backups (
    backup_id      TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL,
    run_count      INTEGER NOT NULL,
    snapshot_count INTEGER NOT NULL,
    quarantine_count INTEGER NOT NULL,
    seq_max        INTEGER NOT NULL,
    latest_run_id  TEXT,
    payload_sha256 TEXT NOT NULL,
    payload_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS restore_jobs (
    job_id          TEXT PRIMARY KEY,
    backup_id       TEXT NOT NULL,
    status          TEXT NOT NULL,
    steps_total     INTEGER NOT NULL,
    steps_done      INTEGER NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    finished_at     TEXT,
    baseline_seq    INTEGER NOT NULL,
    preserved_count INTEGER NOT NULL,
    last_error      TEXT
);
CREATE TABLE IF NOT EXISTS preserved_runs (
    job_id      TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    version_seq INTEGER NOT NULL,
    preserved_at TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (job_id, run_id)
);
CREATE TABLE IF NOT EXISTS preserved_snapshots (
    job_id       TEXT NOT NULL,
    schema_id    TEXT NOT NULL,
    preserved_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (job_id, schema_id)
);
CREATE TABLE IF NOT EXISTS preserved_quarantine (
    job_id       TEXT NOT NULL,
    schema_id    TEXT NOT NULL,
    line         INTEGER,
    table_name   TEXT,
    column_name  TEXT,
    reason       TEXT NOT NULL,
    blocking     INTEGER NOT NULL,
    detail       TEXT,
    raw          TEXT
);
CREATE INDEX IF NOT EXISTS idx_quarantine_schema ON quarantine(schema_id);
CREATE INDEX IF NOT EXISTS idx_restore_jobs_status ON restore_jobs(status);
CREATE INDEX IF NOT EXISTS idx_preserved_runs_seq ON preserved_runs(version_seq);
CREATE INDEX IF NOT EXISTS idx_preserved_quar_job ON preserved_quarantine(job_id);
"""

# 恢复单分步：每步独立事务、独立落盘，中断后按已完成步续跑
# 保全必须先于任何整表替换，否则被切出的批次将找不到自己的快照
RESTORE_STEPS = (
    "preserve_runs",
    "restore_snapshots",
    "restore_runs",
    "restore_quarantine",
    "advance_seq_hwm",
)
SEQ_HWM_KEY = "seq_hwm"
BASELINE_KEY = "active_baseline"


def db_path() -> Path:
    raw = os.environ.get("METRICGRAPH_DB", "").strip()
    path = Path(raw) if raw else config.DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)


def reset() -> None:
    """测试与重建用：清空全部业务表。"""
    with _LOCK, _connect() as conn:
        conn.executescript(SCHEMA)
        for table in ("snapshots", "runs", "quarantine", "kv", "backups",
                      "restore_jobs", "preserved_runs", "preserved_snapshots",
                      "preserved_quarantine"):
            conn.execute(f"DELETE FROM {table}")


def _dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value):
    if not value:
        return None
    return json.loads(value)


# ---------- 快照 ----------

def save_snapshot(snapshot: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(schema_id) DO UPDATE SET
                   version_label=excluded.version_label,
                   source_path=excluded.source_path,
                   source_sha256=excluded.source_sha256,
                   encoding=excluded.encoding,
                   bom=excluded.bom,
                   field_count=excluded.field_count,
                   blocked_count=excluded.blocked_count,
                   created_at=excluded.created_at,
                   payload_json=excluded.payload_json""",
            (
                snapshot["schema_id"], snapshot["version_label"], snapshot["source_path"],
                snapshot["source_sha256"], snapshot["encoding"], int(bool(snapshot["bom"])),
                int(snapshot["field_count"]), int(snapshot["blocked_count"]),
                snapshot["created_at"], _dumps(snapshot),
            ),
        )


def load_snapshot(schema_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM snapshots WHERE schema_id=?", (schema_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_snapshots(limit: int = 20) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, version_label, source_path, source_sha256, encoding, bom,
                      field_count, blocked_count, created_at
               FROM snapshots ORDER BY created_at DESC, schema_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_quarantine(schema_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine WHERE schema_id=?", (schema_id,))
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    schema_id, int(row.get("line") or 0), row.get("table") or "",
                    row.get("column") or "", row["reason"], int(bool(row.get("blocking"))),
                    row.get("detail") or "", row.get("raw") or "",
                )
                for row in rows
            ],
        )


def list_quarantine(schema_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT line, table_name, column_name, reason, blocking, detail, raw
               FROM quarantine WHERE schema_id=? ORDER BY line, reason""",
            (schema_id,),
        ).fetchall()
    return [
        {
            "line": row["line"],
            "table": row["table_name"],
            "column": row["column_name"],
            "reason": row["reason"],
            "blocking": bool(row["blocking"]),
            "detail": row["detail"],
            "raw": row["raw"],
        }
        for row in rows
    ]


# ---------- 运行清单 ----------

def save_run(run: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id, version_seq,
                   input_sha256, ruleset, created_at, effective_from, effective_to,
                   summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run["run_id"], run["batch_key"], run["old_schema_id"], run["new_schema_id"],
                int(run["version_seq"]), run["input_sha256"], run["ruleset"],
                run["created_at"], run["effective_from"], run["effective_to"],
                _dumps(run["summary"]), _dumps(run),
            ),
        )


def find_run_by_key(batch_key: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE batch_key=?", (batch_key,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def run_by_id(run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def latest_run() -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq DESC LIMIT 1"
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_runs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT summary_json FROM runs ORDER BY version_seq DESC, run_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_loads(row["summary_json"]) for row in rows]


def count_runs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()
    return int(row["n"])


def next_version_seq() -> int:
    """下一个批次号：表内最大号 + 1，且不得低于保全高水位，保证编号不复用。"""
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
        hwm_row = conn.execute("SELECT v FROM kv WHERE k=?", (SEQ_HWM_KEY,)).fetchone()
    table_max = int(row["n"])
    hwm = int(_loads(hwm_row["v"])) if hwm_row else 0
    return max(table_max, hwm) + 1


def current_version_seq() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(version_seq), 0) AS n FROM runs").fetchone()
    return int(row["n"])


def list_windows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT run_id, effective_from, effective_to FROM runs ORDER BY version_seq"
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 键值 ----------

def set_kv(key: str, value) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, _dumps(value)),
        )


def get_kv(key: str, default=None):
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return _loads(row["v"]) if row else default


# ---------- M8 备份恢复：只放数据访问，判定在 m8_recovery 模块 ----------

def all_snapshot_payloads() -> list[str]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM snapshots ORDER BY schema_id"
        ).fetchall()
    return [row["payload_json"] for row in rows]


def all_run_payloads() -> list[str]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runs ORDER BY version_seq, run_id"
        ).fetchall()
    return [row["payload_json"] for row in rows]


def all_quarantine_rows() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, line, table_name, column_name, reason, blocking,
                      detail, raw
               FROM quarantine ORDER BY schema_id, line, reason"""
        ).fetchall()
    return [dict(row) for row in rows]


def save_backup(record: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO backups (backup_id, created_at, run_count, snapshot_count,
                   quarantine_count, seq_max, latest_run_id, payload_sha256, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(backup_id) DO NOTHING""",
            (
                record["backup_id"], record["created_at"], int(record["run_count"]),
                int(record["snapshot_count"]), int(record["quarantine_count"]),
                int(record["seq_max"]), record.get("latest_run_id"),
                record["payload_sha256"], record["payload_json"],
            ),
        )


def get_backup(backup_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM backups WHERE backup_id=?", (backup_id,)
        ).fetchone()
    return dict(row) if row else None


def list_backups(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT backup_id, created_at, run_count, snapshot_count,
                      quarantine_count, seq_max, latest_run_id, payload_sha256
               FROM backups ORDER BY created_at DESC, backup_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def save_restore_job(job: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO restore_jobs (job_id, backup_id, status, steps_total,
                   steps_done, created_at, updated_at, finished_at, baseline_seq,
                   preserved_count, last_error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(job_id) DO UPDATE SET
                   status=excluded.status,
                   steps_done=excluded.steps_done,
                   updated_at=excluded.updated_at,
                   finished_at=excluded.finished_at,
                   preserved_count=excluded.preserved_count,
                   last_error=excluded.last_error""",
            (
                job["job_id"], job["backup_id"], job["status"], int(job["steps_total"]),
                int(job["steps_done"]), job["created_at"], job["updated_at"],
                job.get("finished_at"), int(job["baseline_seq"]),
                int(job["preserved_count"]), job.get("last_error"),
            ),
        )


def get_restore_job(job_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM restore_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def list_restore_jobs(limit: int = 30) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT job_id, backup_id, status, steps_total, steps_done, created_at,
                      updated_at, finished_at, baseline_seq, preserved_count, last_error
               FROM restore_jobs ORDER BY created_at DESC, job_id LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def active_restore_job() -> dict | None:
    """未跑完的恢复单（running/interrupted），用于重启后续跑与并发拦截。"""
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM restore_jobs WHERE status IN ('running','interrupted') "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def count_restore_jobs() -> int:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM restore_jobs").fetchone()
    return int(row["n"])


def save_preserved_run(job_id: str, run: dict, preserved_at: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO preserved_runs (job_id, run_id, version_seq,
                   preserved_at, summary_json, payload_json)
               VALUES (?,?,?,?,?,?)""",
            (job_id, run["run_id"], int(run["version_seq"]), preserved_at,
             _dumps(run.get("summary") or {}), _dumps(run)),
        )


def list_preserved_runs(job_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT job_id, run_id, version_seq, preserved_at, summary_json
               FROM preserved_runs WHERE job_id=?
               ORDER BY version_seq, run_id""",
            (job_id,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["summary"] = _loads(row["summary_json"])
        out.append(item)
    return out


def get_preserved_run_payload(job_id: str, run_id: str) -> dict | None:
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT payload_json FROM preserved_runs WHERE job_id=? AND run_id=?",
            (job_id, run_id),
        ).fetchone()
    return _loads(row["payload_json"]) if row else None


def list_preservation_jobs() -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT job_id, run_id, version_seq, preserved_at, summary_json
               FROM preserved_runs ORDER BY version_seq, run_id"""
        ).fetchall()
    return [
        {"job_id": row["job_id"], "run_id": row["run_id"],
         "version_seq": row["version_seq"], "preserved_at": row["preserved_at"],
         "summary": _loads(row["summary_json"])}
        for row in rows
    ]


def save_preserved_snapshot(job_id: str, snapshot: dict, preserved_at: str) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO preserved_snapshots (job_id, schema_id,
                   preserved_at, payload_json) VALUES (?,?,?,?)""",
            (job_id, snapshot["schema_id"], preserved_at, _dumps(snapshot)),
        )


def list_preserved_snapshots(job_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT job_id, schema_id, preserved_at, payload_json
               FROM preserved_snapshots WHERE job_id=? ORDER BY schema_id""",
            (job_id,),
        ).fetchall()
    return [
        {"job_id": row["job_id"], "schema_id": row["schema_id"],
         "preserved_at": row["preserved_at"], "snapshot": _loads(row["payload_json"])}
        for row in rows
    ]


def save_preserved_quarantine(job_id: str, rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.executemany(
            """INSERT OR IGNORE INTO preserved_quarantine (job_id, schema_id, line,
                   table_name, column_name, reason, blocking, detail, raw)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    job_id, row["schema_id"], row.get("line"), row.get("table_name"),
                    row.get("column_name"), row["reason"], int(bool(row["blocking"])),
                    row.get("detail"), row.get("raw"),
                )
                for row in rows
            ],
        )


def list_preserved_quarantine(job_id: str) -> list[dict]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT schema_id, line, table_name, column_name, reason, blocking,
                      detail, raw
               FROM preserved_quarantine WHERE job_id=?
               ORDER BY schema_id, line, reason""",
            (job_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------- 恢复执行用的整表替换，全部在调用方的恢复锁内 ----------

def replace_snapshots(snapshots: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM snapshots")
        conn.executemany(
            """INSERT INTO snapshots (schema_id, version_label, source_path, source_sha256,
                   encoding, bom, field_count, blocked_count, created_at, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    s["schema_id"], s["version_label"], s["source_path"], s["source_sha256"],
                    s["encoding"], int(bool(s["bom"])), int(s["field_count"]),
                    int(s["blocked_count"]), s["created_at"], _dumps(s),
                )
                for s in snapshots
            ],
        )


def replace_runs(runs: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM runs")
        conn.executemany(
            """INSERT INTO runs (run_id, batch_key, old_schema_id, new_schema_id,
                   version_seq, input_sha256, ruleset, created_at, effective_from,
                   effective_to, summary_json, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    r["run_id"], r["batch_key"], r["old_schema_id"], r["new_schema_id"],
                    int(r["version_seq"]), r["input_sha256"], r["ruleset"],
                    r["created_at"], r["effective_from"], r["effective_to"],
                    _dumps(r.get("summary") or {}), _dumps(r),
                )
                for r in runs
            ],
        )


def replace_quarantine(rows: list[dict]) -> None:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM quarantine")
        conn.executemany(
            """INSERT INTO quarantine (schema_id, line, table_name, column_name, reason,
                   blocking, detail, raw) VALUES (?,?,?,?,?,?,?,?)""",
            [
                (
                    r["schema_id"], r.get("line"), r.get("table_name"),
                    r.get("column_name"), r["reason"], int(bool(r["blocking"])),
                    r.get("detail"), r.get("raw"),
                )
                for r in rows
            ],
        )


def get_seq_hwm() -> int:
    return int(get_kv(SEQ_HWM_KEY, 0) or 0)


def set_seq_hwm(value: int) -> None:
    set_kv(SEQ_HWM_KEY, int(value))


def get_baseline() -> dict | None:
    return get_kv(BASELINE_KEY)


def set_baseline(value: dict) -> None:
    set_kv(BASELINE_KEY, value)

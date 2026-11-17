"""M8 备份恢复：备份校验、预演、可续跑恢复、只读保全、编号不复用、并发与重启。

覆盖验收要点：
- 损坏备份拒绝使用（整体摘要 / 表级校验和 / 结构缺失）
- 恢复中断可续跑，重试不重复恢复
- 恢复与跑批并发时拒绝，过期基线 409
- 后续批次保全为只读凭据，业务库切回备份基线，新批次编号不复用
- 重启后保持已切换基线与高水位
"""

from __future__ import annotations

import sqlite3

import pytest

from app import errors, store
from app.modules.m8_recovery import backup as backup_mod
from app.modules.m8_recovery import checksum, guard, restore as restore_mod


# ---------- 夹具：污染后的库 + 备份 ----------

@pytest.fixture()
def polluted(client):
    """种子批次 seq=1，再造 seq=2、3 后备份，随后追加污染批次 seq=4、5。"""
    client.post("/api/runs", json={"params": {"wave": "one"}})
    client.post("/api/runs", json={"params": {"wave": "two"}})
    backup = client.post("/api/recovery/backups",
                         json={"note": "污染前基线"}).json()
    polluted_a = client.post("/api/runs",
                             json={"params": {"wave": "pollute-a"}}).json()
    polluted_b = client.post("/api/runs",
                             json={"params": {"wave": "pollute-b"}}).json()
    assert client.get("/api/batches").json()["current_version_seq"] == 5
    return {
        "client": client,
        "backup": backup,
        "polluted_run_ids": [polluted_a["run_id"], polluted_b["run_id"]],
    }


def _start_interrupted(client, backup_id: str, after_step: str) -> str:
    job = restore_mod.create_job(backup_id)
    resp = client.post("/api/recovery/test/fault",
                       json={"job_id": job["job_id"], "after_step": after_step})
    assert resp.status_code == 200
    client.post(f"/api/recovery/restore/{job['job_id']}/resume")
    return job["job_id"]


# ---------- 备份制作与完整性 ----------

def test_backup_captures_three_tables_and_checksums(client):
    result = client.post("/api/recovery/backups", json={"note": "基线"}).json()
    assert result["backup_id"].startswith("bk-")
    assert result["run_count"] == 1
    assert result["snapshot_count"] >= 2
    assert result["quarantine_count"] > 0
    assert len(result["payload_sha256"]) == 64

    detail = client.get(f"/api/recovery/backups/{result['backup_id']}").json()
    assert detail["verified"] is True
    assert set(detail["table_checksums"]) == {"snapshots", "runs", "quarantine"}
    assert all(len(value) == 64 for value in detail["table_checksums"].values())


def test_backup_listing_and_health_fields(client):
    client.post("/api/recovery/backups")
    listing = client.get("/api/recovery/backups").json()
    assert len(listing["backups"]) == 1
    assert listing["seq_hwm"] == 0
    health = client.get("/api/health").json()
    assert health["seq_hwm"] == 0 and health["baseline"] == {}


def test_corrupt_backup_payload_sha_rejected(polluted):
    client = polluted["client"]
    backup_id = polluted["backup"]["backup_id"]
    record = store.get_backup(backup_id)
    conn = sqlite3.connect(store.db_path())
    conn.execute("UPDATE backups SET payload_json=? WHERE backup_id=?",
                 (record["payload_json"].replace("fm-", "zz-", 1), backup_id))
    conn.commit()
    conn.close()

    for path in (f"/api/recovery/preview/{backup_id}",
                 f"/api/recovery/backups/{backup_id}"):
        response = client.get(path)
        assert response.status_code == 400
        body = response.json()
        assert body["detail"]["reason"] == "BACKUP_CORRUPT"
        assert "BACKUP_PAYLOAD_SHA_MISMATCH" in body["detail"]["problems"]

    assert client.post("/api/recovery/restore",
                       json={"backup_id": backup_id}).status_code == 400


def test_corrupt_backup_table_checksum_rejected(polluted):
    backup_id = polluted["backup"]["backup_id"]
    record = store.get_backup(backup_id)
    import json

    payload = json.loads(record["payload_json"])
    payload["runs"][0]["run_id"] = "fm-tampered0000"
    # 整体摘要同步伪造，只剩表级校验和能挡住
    new_text = checksum.canonical_dumps(payload)
    new_sha = checksum.checksum_of(payload)
    conn = sqlite3.connect(store.db_path())
    conn.execute("UPDATE backups SET payload_json=?, payload_sha256=? WHERE backup_id=?",
                 (new_text, new_sha, backup_id))
    conn.commit()
    conn.close()

    with pytest.raises(errors.BadInput) as caught:
        backup_mod.load_verified(backup_id)
    assert "BACKUP_CHECKSUM_MISMATCH:runs" in caught.value.detail["problems"]


def test_missing_backup_returns_404(client):
    assert client.get("/api/recovery/preview/bk-absent").status_code == 404
    assert client.post("/api/recovery/restore",
                       json={"backup_id": "bk-absent"}).status_code == 404


def test_unknown_params_rejected(client):
    assert client.post("/api/recovery/backups",
                       json={"note": "x", "evil": 1}).status_code == 400
    assert client.post("/api/recovery/restore",
                       json={"backup_id": "x", "evil": 1}).status_code == 400


# ---------- 预演 ----------

def test_preview_lists_preserve_restore_and_hwm(polluted):
    client = polluted["client"]
    preview = client.get(
        f"/api/recovery/preview/{polluted['backup']['backup_id']}"
    ).json()
    assert preview["verified"] is True
    assert preview["runs"]["restore_count"] == 3
    assert preview["runs"]["preserve_count"] == 2
    assert [item["version_seq"] for item in preview["runs"]["preserve"]] == [4, 5]
    assert preview["seq"]["live_max"] == 5
    assert preview["seq"]["backup_max"] == 3
    assert preview["seq"]["hwm_after"] == 5
    assert set(preview["checksums"]["backup"]) == {"snapshots", "runs", "quarantine"}
    # 预演不落库
    assert client.get("/api/batches").json()["current_version_seq"] == 5
    assert client.get("/api/recovery/jobs").json()["jobs"] == []


# ---------- 可续跑恢复 ----------

def test_interrupted_restore_resumes_without_repeating(polluted):
    client = polluted["client"]
    backup_id = polluted["backup"]["backup_id"]
    job_id = _start_interrupted(client, backup_id, "preserve_runs")

    job = client.get(f"/api/recovery/jobs/{job_id}").json()
    assert job["status"] == "interrupted"
    assert job["steps_done"] == 1 and job["steps_total"] == 5
    assert job["last_error"]

    # 再中断一次：已完成步被跳过，停在第 4 步（还原隔离行）
    client.post("/api/recovery/test/fault",
                json={"job_id": job_id, "after_step": "restore_quarantine"})
    second = client.post(f"/api/recovery/restore/{job_id}/resume").json()
    assert second["status"] == "interrupted" and second["steps_done"] == 4

    finished = client.post(f"/api/recovery/restore/{job_id}/resume").json()
    assert finished["status"] == "finished" and finished["steps_done"] == 5

    # 重试已完成的恢复单：原样返回，不重复执行
    retried = client.post(f"/api/recovery/restore/{job_id}/resume").json()
    assert retried["status"] == "finished" and retried["steps_done"] == 5


def test_preservation_runs_before_table_replacement(polluted):
    """保全是第一步：被切出批次引用的快照必须先凭据化，再做整表替换。"""
    client = polluted["client"]
    backup_id = polluted["backup"]["backup_id"]
    job_id = _start_interrupted(client, backup_id, "preserve_runs")

    # 仅完成保全步时，凭据已落盘，业务库尚未切换
    preserved = client.get("/api/recovery/preserved").json()
    assert preserved["count"] == 2
    snaps = client.get(
        f"/api/recovery/preserved/{job_id}/snapshots").json()["snapshots"]
    assert len(snaps) >= 1
    assert client.get("/api/batches").json()["current_version_seq"] == 5

    client.post(f"/api/recovery/restore/{job_id}/resume")
    # 切换后保全快照仍可读，且业务库已回到备份基线
    snaps_after = client.get(
        f"/api/recovery/preserved/{job_id}/snapshots").json()["snapshots"]
    assert {s["schema_id"] for s in snaps_after} == {
        s["schema_id"] for s in snaps}
    assert client.get("/api/batches").json()["current_version_seq"] == 3


def test_restore_switches_baseline_and_preserves_batches(polluted):
    client = polluted["client"]
    backup_id = polluted["backup"]["backup_id"]
    job_id = _start_interrupted(client, backup_id, "preserve_runs")
    client.post(f"/api/recovery/restore/{job_id}/resume")

    batches = client.get("/api/batches").json()
    assert batches["current_version_seq"] == 3
    assert len(batches["runs"]) == 3
    live_ids = {row["run_id"] for row in batches["runs"]}
    assert not (set(polluted["polluted_run_ids"]) & live_ids)

    preserved = client.get("/api/recovery/preserved").json()
    assert preserved["count"] == 2
    assert sorted(item["version_seq"] for item in preserved["preserved"]) == [4, 5]
    assert {item["run_id"] for item in preserved["preserved"]} == set(
        polluted["polluted_run_ids"])

    # 保全凭据可回看完整批次内容
    run_id = polluted["polluted_run_ids"][0]
    detail = client.get(
        f"/api/recovery/preserved/{job_id}/{run_id}").json()
    assert detail["run_id"] == run_id and detail["version_seq"] == 4
    assert detail["ledger"]["counts"]

    # 保全快照与隔离行一并留存
    snaps = client.get(
        f"/api/recovery/preserved/{job_id}/snapshots").json()["snapshots"]
    assert len(snaps) >= 1
    quarantine = client.get(
        f"/api/recovery/preserved/{job_id}/quarantine").json()["quarantine"]
    assert all(row["reason"] for row in quarantine)


def test_preserved_credentials_are_read_only(polluted):
    client = polluted["client"]
    backup_id = polluted["backup"]["backup_id"]
    job = client.post("/api/recovery/restore",
                      json={"backup_id": backup_id}).json()
    assert job["status"] == "finished"
    run_id = polluted["polluted_run_ids"][0]

    # 保全批次不在业务 runs 表里，只能走只读凭据接口
    assert store.run_by_id(run_id) is None
    detail = client.get(
        f"/api/recovery/preserved/{job['job_id']}/{run_id}").json()
    assert detail["version_seq"] == 4
    # 凭据表没有任何写接口，未知批次 404
    assert client.get(
        f"/api/recovery/preserved/{job['job_id']}/fm-missing").status_code == 404


# ---------- 编号不复用 ----------

def test_new_batch_never_reuses_preserved_seq(polluted):
    client = polluted["client"]
    backup_id = polluted["backup"]["backup_id"]
    client.post("/api/recovery/restore", json={"backup_id": backup_id})
    assert store.get_seq_hwm() == 5

    fresh = client.post("/api/runs",
                        json={"params": {"wave": "after-restore"}}).json()
    assert fresh["version_seq"] == 6
    again = client.post("/api/runs",
                        json={"params": {"wave": "after-restore-2"}}).json()
    assert again["version_seq"] == 7
    # 高水位只增不减：再恢复一次也不会把序号压回去
    second_backup = client.post("/api/recovery/backups").json()
    client.post("/api/runs", json={"params": {"wave": "more"}})
    client.post("/api/recovery/restore",
                json={"backup_id": second_backup["backup_id"]})
    assert store.get_seq_hwm() >= 7
    nxt = client.post("/api/runs",
                      json={"params": {"wave": "post-second"}}).json()
    assert nxt["version_seq"] >= 8


# ---------- 并发与过期基线 ----------

def test_batch_rejected_while_restore_in_progress(polluted):
    client = polluted["client"]
    job_id = _start_interrupted(
        client, polluted["backup"]["backup_id"], "restore_snapshots")

    response = client.post("/api/runs", json={"params": {"wave": "during"}})
    assert response.status_code == 409
    assert response.json()["error"] == "RESTORE_IN_PROGRESS"
    assert response.json()["detail"]["job_id"] == job_id

    # 恢复完成后跑批恢复
    client.post(f"/api/recovery/restore/{job_id}/resume")
    ok = client.post("/api/runs", json={"params": {"wave": "after"}})
    assert ok.status_code == 200


def test_guard_unit_rejects_active_restore():
    guard.assert_no_active_restore()  # 无恢复单时放行
    store.save_restore_job({
        "job_id": "rj-x", "backup_id": "bk-x", "status": "interrupted",
        "steps_total": 5, "steps_done": 1, "created_at": "2026-11-17T00:00:00+00:00",
        "updated_at": "2026-11-17T00:00:00+00:00", "finished_at": None,
        "baseline_seq": 1, "preserved_count": 0, "last_error": "x",
    })
    with pytest.raises(guard.RestoreInProgress):
        guard.assert_no_active_restore()
    # 置完成后放行
    finished = dict(store.get_restore_job("rj-x"), status="finished",
                    finished_at="2026-11-17T00:01:00+00:00")
    store.save_restore_job(finished)
    guard.assert_no_active_restore()


def test_concurrent_restore_jobs_rejected(polluted):
    client = polluted["client"]
    _start_interrupted(client, polluted["backup"]["backup_id"], "restore_runs")
    response = client.post("/api/recovery/restore",
                           json={"backup_id": polluted["backup"]["backup_id"]})
    assert response.status_code == 409
    assert response.json()["error"] == "RESTORE_CONFLICT"


def test_stale_baseline_still_rejected_after_restore(polluted):
    client = polluted["client"]
    client.post("/api/recovery/restore",
                json={"backup_id": polluted["backup"]["backup_id"]})
    # 恢复后当前序号回到 3，带旧序号 5 的提交必须拒
    stale = client.post("/api/runs", json={"base_version_seq": 5})
    assert stale.status_code == 409
    assert stale.json()["error"] == "VERSION_STALE"
    current = client.post("/api/runs", json={"base_version_seq": 3})
    assert current.status_code == 200


# ---------- 重启持久化 ----------

def test_baseline_and_hwm_survive_restart(polluted):
    client = polluted["client"]
    job = client.post("/api/recovery/restore",
                      json={"backup_id": polluted["backup"]["backup_id"]}).json()
    client.post("/api/runs", json={"params": {"wave": "after"}})

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as restarted:
        health = restarted.get("/api/health").json()
        assert health["seq_hwm"] == 5
        assert health["baseline"]["job_id"] == job["job_id"]
        assert health["baseline"]["backup_id"] == polluted["backup"]["backup_id"]
        preserved = restarted.get("/api/recovery/preserved").json()
        assert preserved["count"] == 2
        page = restarted.get("/recovery")
        assert page.status_code == 200
        assert "备份恢复单" in page.text
        # 重启不改变批次号
        nxt = restarted.post("/api/runs",
                             json={"params": {"wave": "post-restart"}}).json()
        assert nxt["version_seq"] == 7


# ---------- 恢复幂等：同输入重跑不产生重复 ----------

def test_restore_idempotent_on_rerun_with_same_backup(client):
    backup = client.post("/api/recovery/backups").json()
    client.post("/api/recovery/restore",
                json={"backup_id": backup["backup_id"]}).json()
    # 无保全批次时再恢复：第二个恢复单也能跑完，库内容不变
    second = client.post("/api/recovery/restore",
                         json={"backup_id": backup["backup_id"]}).json()
    assert second["status"] == "finished"
    assert second["preserved_count"] == 0
    assert client.get("/api/batches").json()["current_version_seq"] == 1
    assert store.count_runs() == 1


def test_checksum_helpers_stable():
    payload = {"a": 1, "b": [2, 3]}
    assert checksum.checksum_of(payload) == checksum.checksum_of(payload)
    assert checksum.canonical_dumps({"a": 1, "b": 2}) == \
        checksum.canonical_dumps({"b": 2, "a": 1})

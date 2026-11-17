# 运行手册 metricgraph

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18215/api/health
```

宿主端口 `18215`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18215/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409。
6. `/spec` 导出迁移单与三份账目 CSV。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `metricgraph-data` 挂着 `/data/metricgraph.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号与生效区间 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/reconcile` | 四账对账 |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |
| POST | `/api/recovery/backups` | 制作备份单（整体 + 表级 SHA-256） |
| GET | `/api/recovery/backups` | 备份清单与当前基线、高水位 |
| GET | `/api/recovery/backups/{id}` | 备份详情并复验校验和 |
| GET | `/api/recovery/preview/{id}` | 预演差异：保全/还原清单，不落库 |
| POST | `/api/recovery/restore` | 建恢复单并执行（分步、可续跑） |
| POST | `/api/recovery/restore/{job}/resume` | 中断恢复单续跑，已完成步不重复 |
| GET | `/api/recovery/jobs` | 恢复单清单与进度 |
| GET | `/api/recovery/preserved` | 保全批次只读凭据 |
| GET | `/api/recovery/preserved/{job}/{run}` | 单条保全批次完整内容 |
| GET | `/api/recovery/preserved/{job}/snapshots` | 保全快照凭据 |
| GET | `/api/recovery/preserved/{job}/quarantine` | 保全隔离行凭据 |

## 6 备份恢复（误采信退回）

场景：误采信后在线分析要退回备份基线，但备份之后已签发的批次不能丢、编号不能复用。

1. 在 `/recovery` 点「制作备份」，备份固化 `snapshots/runs/quarantine` 三张表，带整体与表级 SHA-256。
2. 继续跑批产生的后续批次不受影响；决定退回时先点「预演」，确认哪些批次保全、哪些批次还原、高水位推到几。
3. 点「恢复」：后续批次先保全进 `preserved_*` 只读凭据表，业务库再整体切回备份基线，最后推进批次号高水位。
4. 恢复分 5 步落盘（保全批次 → 还原快照 → 还原批次 → 还原隔离行 → 推进高水位），中断后打开页面点「续跑」，已完成步直接跳过。
5. 恢复后新批次序号从高水位 +1 开始，保全批次编号永不复用；保全批次只可在凭据接口回看，不参与在线分析。
6. 重启后基线与高水位从 `kv` 表恢复，已切换状态不回退。

约束：恢复进行中跑批返回 409 `RESTORE_IN_PROGRESS`；已有未完成恢复单时再建单返回 409 `RESTORE_CONFLICT`；损坏备份（摘要或表级校验和不符）一律 400 拒绝，不允许恢复。

## 7 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 400 `BACKUP_CORRUPT` | 备份整体摘要或表级校验和不符 | 备份不可用，换一份完好备份 |
| 409 `RESTORE_IN_PROGRESS` | 恢复未跑完时发起跑批 | 到 `/recovery` 续跑完成后再跑批 |
| 409 `RESTORE_CONFLICT` | 已有未完成恢复单又建新单 | 续跑原恢复单，不要重复建单 |
| 404 `NOT_FOUND` | 还没跑过批次或备份/恢复单不存在 | 先在 `/batches` 点「跑一次」 |

## 8 端口与并发

本服务固定 18215。同时起多个栈时改 `docker-compose.yml` 的端口映射与 `metricgraph-data` 卷名，避免与本机其他项目抢端口、串数据。

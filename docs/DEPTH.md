# 深度登记 metricgraph

- 规模：中型（8 个功能模块，99 个文件，107 条测试）
- 核心规则：同名同版本指标先做表达式稳定签名，再按依赖图检测环并裁决生效版本。
- 使用者与工作流：分析治理组在指标发布前复核定义冲突、缺依赖、依赖环与版本重叠；误采信时通过备份恢复单退回基线，后续批次保全为只读凭据。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 元数据采信 | 采信 DDL 或 JSON 元数据，看逐行诊断与隔离区 | POST /api/schemas/import | `app/modules/m1_intake/handler.py` `app/modules/m1_intake/service.py` `app/modules/m1_intake/reader.py` `app/modules/m1_intake/ddl.py` `app/modules/m1_intake/sheet.py` | `tests/test_m1_intake.py` |
| M2 类型格 | 查类型格的值域与刻度，或直接比两个类型写法 | GET /api/types | `app/modules/m2_types/handler.py` `app/modules/m2_types/service.py` `app/modules/m2_types/coerce.py` `app/modules/m2_types/range.py` `app/modules/m2_types/lattice.py` | `tests/test_m2_types.py` |
| M3 冲突判定 | 对同名指标做签名并列出定义冲突 | POST /api/metrics/conflicts | `app/modules/m3_compat/handler.py` `app/modules/m3_compat/metric_rules.py` `app/modules/m3_compat/priority.py` | `tests/test_m3_compat.py` `tests/test_metric_rules.py` |
| M4 字段映射 | 看旧名到新名的映射、别名 SQL、冲突项 | GET /api/mapping | `app/modules/m4_mapping/handler.py` `app/modules/m4_mapping/service.py` `app/modules/m4_mapping/mapping.py` `app/modules/m4_mapping/sqlalias.py` `app/modules/m4_mapping/rename.py` | `tests/test_m4_mapping.py` |
| M5 影响面 | 看受影响的下游节点、环、断链、单边登记 | GET /api/impact | `app/modules/m5_impact/handler.py` `app/modules/m5_impact/service.py` `app/modules/m5_impact/closure.py` `app/modules/m5_impact/cycle.py` `app/modules/m5_impact/broken.py` | `tests/test_m5_impact.py` |
| M6 版本批次 | 跑批次、看历史、验陈旧拒绝与区间重叠 | POST /api/runs | `app/modules/m6_batches/handler.py` `app/modules/m6_batches/service.py` `app/modules/m6_batches/window.py` `app/modules/m6_batches/guard.py` `app/modules/m6_batches/history.py` | `tests/test_m6_batches.py` |
| M7 指标目录 | 导出一份定义、依赖与状态清单 | GET /api/export/catalog.csv | `app/modules/m7_migration/handler.py` `app/modules/m7_migration/service.py` `app/modules/m7_migration/csv_out.py` `app/modules/m7_migration/ledger.py` | `tests/test_m7_migration.py` |
| M8 备份恢复 | 制作备份、预演差异、可续跑恢复、回看只读保全批次 | POST /api/recovery/backups | `app/modules/m8_recovery/handler.py` `app/modules/m8_recovery/service.py` `app/modules/m8_recovery/backup.py` `app/modules/m8_recovery/restore.py` `app/modules/m8_recovery/checksum.py` `app/modules/m8_recovery/preview.py` `app/modules/m8_recovery/guard.py` | `tests/test_m8_recovery.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现会怎样 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-02 | 导出件带字节序标记，首列名被污染 | 首列名带 `\ufeff`，按名取值全空 | 探测编码前先剥标记，再按回退链解码 | `app/modules/m1_intake/reader.py` | `tests/test_m1_intake.py::test_trap02_bom_stripped_before_decode` |
| TRAP-05 | 列定义行尾多一个分隔符 | 拆出空列，或者把坏行当正常列 | 顶层分隔符计数，命中即整行进隔离区 | `app/modules/m1_intake/ddl.py` | `tests/test_m1_intake.py::test_trap05_extra_separator_quarantined` |
| TRAP-09 | 字段身份是 2^53+1 这种大整数 | JSON 往返落进 float，末位被改 | 身份一律字符串保形，浮点身份直接拒绝 | `app/modules/m1_intake/sheet.py` | `tests/test_m1_intake.py::test_trap09_big_integer_field_id_kept_as_string` |
| TRAP-17 | 上游改列名、放宽类型 | 按列名对账，改名算删除加新增 | 只按 field_id 对齐，放宽另算兼容 | `app/modules/m3_compat/judge.py` | `tests/test_m3_compat.py::test_trap17_rename_is_not_delete_plus_add` |
| TRAP-19 | 一个字段同时改名、换类型、改必填 | 结论取决于规则书写顺序 | 显式优先级表，重排输入结论不变 | `app/modules/m3_compat/priority.py` | `tests/test_m3_compat.py::test_trap19_rule_order_does_not_change_primary` |
| TRAP-20 | 下游依赖成环、节点指向自己 | 递归爆栈或静默截断 | 三色 DFS，自引用单列一类 | `app/modules/m5_impact/cycle.py` | `tests/test_m5_impact.py::test_trap20_cycle_and_self_loop` |
| TRAP-21 | 上下游只登记一边 | 只查单向，另一半关系漏报 | 双向核验，缺边清单逐条列出 | `app/modules/m5_impact/broken.py` | `tests/test_m5_impact.py::test_trap21_one_sided_registration` |
| TRAP-22 | 同一字段被连续改过两次名 | 只认最近一跳，最早的名字解析不到 | 跨版本改名链一路解析到底 | `app/modules/m4_mapping/rename.py` | `tests/test_m4_mapping.py::test_trap22_alias_chain_resolves_multi_hop` |
| TRAP-25 | 两个版本生效区间重叠 | 取到哪版看运气 | 半开区间比较，重叠直接标记警告 | `app/modules/m6_batches/window.py` | `tests/test_m6_batches.py::test_trap25_overlap_flagged_but_adjacent_clean` |
| TRAP-26 | 基于旧版本号提交 | 后提交无条件覆盖新结果 | 版本号乐观锁，基线落后即 409 | `app/modules/m6_batches/guard.py` | `tests/test_m6_batches.py::test_trap26_stale_submission_rejected` |
| TRAP-27 | 整库覆盖恢复，备份后签发的批次记录丢失 | 恢复即删数据，已签发批次无法按原编号核对 | 恢复前把后续批次写入 preserved_* 只读凭据，业务表切回基线，凭据可回看 | `app/modules/m8_recovery/restore.py` | `tests/test_m8_recovery.py::test_restore_switches_baseline_and_preserves_batches` |
| TRAP-28 | 恢复后新批次复用了保全批次的旧编号 | 同一 version_seq 指向两批数据，对账错乱 | 批次号高水位只增不减，next_version_seq 取表内最大值与高水位的较大者 +1 | `app/store.py` | `tests/test_m8_recovery.py::test_new_batch_never_reuses_preserved_seq` |
| TRAP-29 | 备份字节被篡改仍被用于恢复 | 污染随备份回灌，恢复失去意义 | 整体 SHA-256 加三表表级校验和，任一不符即 400 拒绝 | `app/modules/m8_recovery/checksum.py` | `tests/test_m8_recovery.py::test_corrupt_backup_payload_sha_rejected` |
| TRAP-30 | 恢复进程在整表替换中途崩溃 | 库停在半替换状态，重试要么重复要么漏步 | 五步独立事务逐步落盘 steps_done，续跑跳过已完成步，重试不重复恢复 | `app/modules/m8_recovery/restore.py` | `tests/test_m8_recovery.py::test_interrupted_restore_resumes_without_repeating` |
| TRAP-31 | 恢复与跑批并发，旧基线结果覆盖切换后的库 | 基线被悄悄写回 | 未完成恢复单期间跑批一律 409，建恢复单也互斥 | `app/modules/m8_recovery/guard.py` | `tests/test_m8_recovery.py::test_batch_rejected_while_restore_in_progress` |

## 对账规则

- 字段账：旧版本 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 删除
- 字段账：新版本 = 未变 + 改名 + 换类型 + 改必填 + 冲突 + 新增
- 引用账：登记引用 = 可达 + 断链，其中可达 = 直接 + 经别名
- 采信账：列定义行 = 接受字段 + 阻断行
- 页面上四账并列显示，差额不为 0 即判不平

## 已知不覆盖

- SQL 只认 `别名.列名`；无别名的裸列名、子查询、CTE、函数包裹的列都不抽。
- DDL 只解析 `CREATE TABLE` 的列定义，不处理分区、约束、索引与 `ALTER`。
- 类型格只覆盖数值、字符、时间、布尔四族；映射表、数组、结构体一律算 `unknown`，保守判破坏。
- 生效区间重叠只标记不阻断，最终以版本序号为准。
- 打印页脚用 `counter(page)`，Chrome 打印预览不渲染该计数。

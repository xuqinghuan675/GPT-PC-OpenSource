# Fast Path V1 候选加速技术评估

> 复验日期：2026-09-06
> 原则：只保留对 GPT-PC 当前真实热路径有可验证收益的方案；不为了“技术清单完整”增加第二套 Runtime、额外索引服务或重复依赖。

## 结论矩阵

| 候选 | 适用热路径 | 实测/架构证据 | 决策 |
| --- | --- | --- | --- |
| ripgrep 15.2.0 | 大树文本搜索、简单 symbol/path 源码定位 | `C:\GPT-PC` 大树文本搜索约 5926 ms → 36.8 ms（≈161×）；`code_navigate(symbol)` Git HEAD 旧版 P50 194.32 ms → 当前 33.56 ms（≈5.79×） | **保留**；固定版本 + SHA-256，失败自动回退 Python |
| fd 10.4.2 | 大树文件发现 | `C:\GPT-PC` 文件发现约 630.9 ms → 38.8 ms（≈16×）；小树进程启动反而更慢，因此有自适应门 | **保留**；大树启用，小树 Python |
| ugrep | 文本搜索 | 当前 rg 已覆盖 MCP 搜索语义且大树收益远超目标；再引入第二搜索二进制会增加排序、ignore、编码、regex/parity 与供应链校验面，没有新的未解决热路径 | **不引入** |
| libgit2 / pygit2 / Dulwich | Git 元数据/状态 | 简单 symbol/path 已改为工作树源码优先，命中时完全不启动 Git；fallback 的 Git identity 已由 3 次进程合并为 1 次 `rev-parse`（另保留 1 次 status）。完整 Git tool 仍需要原生 Git 的 credential/config/worktree 行为 | **不替换 Git CLI**；避免高风险语义漂移 |
| go-git / gitoxide(gix) | Git 元数据/对象访问 | 需要新增独立语言运行时或 helper/API 维护；当前剩余 Git fallback 不是主瓶颈，且没有证据证明可抵消集成复杂度 | **不引入** |
| jwalk / walkdir | Rust 目录遍历 | 当前 Runtime 是 Python，若引入需额外 Rust helper；fd 已在同一热路径达到≈16×，没有功能缺口 | **不引入** |
| fast-glob | Node 目录遍历 | 需要 Node 子进程/依赖，和当前 Python MCP + fd 路径重复；小树 Python 已更快 | **不引入** |
| SQLite metadata | Lease/Job 状态 | Active Job Index 在 318 条历史 Job 下 warm P50≈0.106 ms；剩余短 read 瓶颈来自同步元数据链而不是历史查询数据库 | **不引入** |
| orjson | JSON 序列化 | `tool_e2e` 显示主要热区是 metadata/activity/continuation；MCP transport 序列化发生在 guard 外，没有证据表明它是当前主瓶颈 | **不引入** |
| `read_multiple_files` 内线程池 | 批量读取 | 15 小文件与 24×1 MiB 两组实测均比顺序读取慢 | **已撤回**；并发保留在外层 tool lane |

## 最终性能验收

- **独立短只读吞吐**：以 Git HEAD 旧 Runtime 和当前候选 Runtime 分别启动隔离 HTTP MCP，16 个预热独立 Client 同时执行同一短 `read_text_range`：旧版 0.4443 s / 36.0 RPS；候选 0.1129 s / 141.7 RPS，**≈3.94×**。热切换后的正式 v2.8.12 endpoint 再做 7 轮同规格复验，P50 **119.6 RPS（≈3.32× old HEAD）**，仍稳定进入原计划 3–8×。
- **代码导航**：同一真实 `C:\GPT-PC` 工作树、同一 `symbol=parse_visual_duration`，Git HEAD 旧版 P50 194.32 ms；当前源码优先快路 P50 33.56 ms，**≈5.79×**，达到原计划 2–8×。
- **多任务验证**：4 个独立 pytest 验证任务经 live MCP 串行 5.610 s、并行 2.083 s，**≈2.69×**，达到原计划 2–6×。
- **大树搜索**：文本≈161×、文件发现≈16×，超过目标。

## 最终实现约束

1. `runtime_admin`、项目 lease、`desktop_ops` 与其他租约 Job 可以并存，但资源所有权仍 fail-closed。
2. 同 lease 的文件/Git mutation 保持串行；只并发彼此独立的读取、搜索、导航与验证。
3. 只读 checkpoint 首次动作同步持久化；随后 burst 在进程内合并，并在下一次 progress/control/side-effect checkpoint 时落盘，避免同 lease JSON 重写风暴，同时保留恢复历史与完成门禁。
4. 只读 lease/status 使用原子 JSON snapshot，不再为了观察状态争用跨进程写锁；写操作仍使用跨进程 metadata lock。
5. 源码命中的 `code_navigate(symbol/path)` 不再先支付 Git/graph/index setup；关系型 intent 和源码 miss 才进入图谱/fallback。

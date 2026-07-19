# ADR-0012：分层 File 召回与渐进式 ContextPacket

- Status: Accepted
- Date: 2026-07-19

## Context

现有 `FileGitBackend.query_context(...)` 会读取全部状态目录中的完整 JSON，再执行 #7 的作用域、状态、current HEAD、敏感性、适用性、失效和冲突治理。它安全但扁平；知识增长后，为评分而读取大量正文会浪费 I/O 和上下文预算，也缺少可审计的导航轨迹。

OpenViking 的 L0/L1/L2 和目录递归只作为设计参考。本仓库不复制、vendoring 或依赖其 AGPL 核心代码，也不改变 Apache-2.0 与零额外依赖的核心边界。

## Decision

File/Git 继续是唯一权威源。发布 `opc-hierarchical-context-contract-v1` 与零依赖 `opc_hierarchical.py`：

```text
query
  -> canonical governance metadata snapshot + shared #7 hard filters
  -> explicit opc://global and opc://projects/{project_id} roots
  -> L0 approved summaries and L1 deterministic aggregates
  -> priority expansion of a bounded leaf set
  -> limited L2 canonical reads
  -> repeat exact HEAD/commit/hash/status/scope/applicability checks
  -> budgeted ContextPacket + body-free RecallTrace
```

虚拟树、父子关系、L0/L1、目录摘要和任何 Provider 索引都只是私有派生导航数据：可删除、可重建、不可作为事实、不可决定 authority，也不可绕过 Candidate → Approved → committed provenance。L2 才是 current-HEAD canonical File/Git 正文。

派生索引只允许写入显式 private `data_root/.opc/derived/hierarchical-recall-v1`。`data_root` 不得位于插件、canonical knowledge、任何 Git worktree 或项目源代码内。preview 零写；build/delete 绑定 exact token，使用 bounded single-link read、父目录 filesystem identity、exclusive temporary file、`fsync` 与 atomic replace。symlink、junction/reparse、hardlink、父目录替换、超限文件和重复 ID 均失败关闭；Windows 8.3 等价路径按 filesystem identity 处理。

索引缺失、非法或与 current HEAD 不同，安全降级到现有 flat File/Git。Mem0 或其他 `RecallProvider` 仍使用现有 `search(query, limit)` 薄接口，只建议 candidate ID；未安装、禁用、超时、错误、陈旧或与 File/Git 不一致都降级且不阻塞核心。此 ADR 不扩展 Provider 写协议，因此旧适配器无需迁移。

`ContextPacket` 严格包含 facts、decisions、experiences、procedures、canonical citations、conflicts、显式 budget 与 omitted summary。`RecallTrace` 只记录 root、expansion、整数分数、discard reason、fallback、final leaf 与 token/read cost；不记录正文、目录摘要、凭据、原始聊天、Hook payload、session/turn ID、用户主目录或未授权私人知识。

分层召回必须先复用 ADR-0011 的确定性 hard filter，再导航；任何进入 packet 的 L2 叶子在注入前必须再次通过 exact HEAD/commit/hash/status/scope/sensitivity/applicability/relations 验证。Shadow 仍是只读 candidate 证据消费者，不能利用分层索引授予正式 context 资格。

关系治理不得在 hierarchical 路径复制第二套算法。flat 与 hierarchical 共同调用 `evaluate_relation_governance(...)`，从冻结结构图同时计算 chain、branch、diamond、inverse supersession/invalidation 与 conflict effects。hierarchical 在导航前和读取 L2 前各取得一次 bounded canonical governance metadata snapshot；snapshot 不向 consumer 暴露正文，并与 derived 的 status/scope/applicability/relations/source path/HEAD/hash 全量绑定。derived 删除、替换或伪造关系，或两次 canonical snapshot 不一致时，显式降级到共享 flat File/Git，不能继续信任 derived graph。

build 的目录、owned `.gitignore`、temporary 与 replace 构成一个恢复事务：所有既有和新建目录都绑定 filesystem identity；mkdir/open/write/fsync/replace 任一点失败，只删除本次创建且 identity 未变化的对象，逆序恢复调用前目录树。未知 `.gitignore` 失败关闭，既有 owned marker 和其他文件必须 byte-identical 保留。

canonical governance snapshot 使用严格、schema-aware 的增量 JSON 扫描：为 current-HEAD/hash provenance 允许底层字节 I/O，但 `content` 值只校验字符串语法、UTF-8 和长度，不构造 Python 字符串、对象、导航特征、Trace 或 Token 上下文。只有最终选中的 L2 才能由 `read_authoritative(...)` materialize 正文。审计测试必须封锁完整 record parser，并在任何 `json.loads` payload 出现正文 sentinel 时失败。

`ContextPacket` 和 `RecallTrace` 既分别严格验证，也由联合 validator 验证 query/mode、item/citation identity、top citations、重算 token/budget、`canonical_reads`/read count、final leaves 与 injected cost。评测 result 在写入或渲染前必须从 strict cases、contract、fixture/hash 和单独 latency artifact 重算 aggregate、safety、status、rule 与 claim；renderer 不得信任已汇总字段。

## Consequences

- 默认核心仍为 Python 标准库与 Git，不增加依赖或许可证传播风险。
- 正常分层路径不会先 materialize 全库正文或用正文评分；metadata snapshot 的底层 provenance I/O 不进入 Python 正文对象、Trace 或 Token 上下文，仅最终少量 L2 叶子 materialize canonical body。
- derived 数据被删除或损坏不会丢失知识；降级状态是显式的。
- 分层质量、Token、安全和延迟使用同一公开合成 fixture 与当前 flat 基线比较；未满足版本化阈值时报告必须写 `not_superior`。
- L0/L1 可能漏路由，因此必须保留跨目录、relations 与回退评测，不能把一次 synthetic 优势推广成统计普适结论。

## Rejected Alternatives

- **把 L0/L1 当事实直接注入：** 摘要可能漂移，无法替代 current-HEAD canonical bytes。
- **查询时扫描全正文后声称渐进读取：** 只是包装 flat recall，不能证明节省读取或 Token。
- **让 Provider score 覆盖 scope/status/conflict：** 排名不是 authority，违反 ADR-0011。
- **自动重建并静默写项目 `.opc`：** 查询不应产生意外写入；私有 derived build 采用 preview/token。
- **直接集成 OpenViking：** 本 Issue 没有完成 AGPL、部署和数据流评审，且核心必须零额外依赖。

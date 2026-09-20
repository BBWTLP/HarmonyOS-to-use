# v3.2 进度台账（本实现仓库）

对照 `HarmonyOS_Agent_v3.2_完整项目开发计划.md` 与
`Codex_HarmonyOS_Agent_v3.2_无真机完整开发提示词.md`。

状态词汇（只使用这些值）：

```text
planned         已规划，未开始
in_progress     正在进行
implemented     代码已存在，未取得离线证据
verified_offline 离线测试/脚本通过
blocked_device  必须真机，当前无法执行
verified_device 真机证据通过（本仓库当前没有任何一项处于此状态）
deferred        明确推迟，不阻塞发布
```

本轮证据：`docs/v3.2/baseline-offline.md`、`.runtime/tests-current.json`
（本地，不提交）。真机待办：`docs/v3.2/blocked-device.md`。

## 1. 阶段总览

| Phase | 工作 | 状态 | 说明 |
|---:|---|---|---|
| 0 | Offline baseline | **verified_offline** | 变更前 346 / Phase 1-2 后 392 / 离线阶段全部完成后 562，全绿；见 `baseline-offline.md` |
| 1 | Decider 真正插件化 | **verified_offline** | 惰性 factory、协议类型依赖、泛化 diagnostics、故障矩阵 35 项 |
| 2 | Direct / Autonomous 边界冻结 | **verified_offline** | v1 六工具无宿主可用；宿主初始化失败降级；关闭宿主不影响 Runtime；11 项 |
| 3 | Observation / cache 语义审计 | **verified_offline** | `observation-cache-semantics.md`；14 项；性能 `blocked_device` |
| 4 | Grounding 离线 fixture 强化 | **verified_offline** | 23 个 fixture 用例；发现并记录 hint 限制 |
| 5 | Visual Target Authority ADR | **verified_offline** | ADR + Runtime 复核实现 + 19 项；真机执行 `blocked_device` |
| 6 | OCR adapter | **verified_offline（代码）** | `harmony_agent/ocr.py`；真机 OCR `blocked_device` |
| 7 | VLM visual grounding | **verified_offline（代码）** | `harmony_agent/vlm.py`；真机 VLM `blocked_device` |
| 8 | Risk Policy 统一 | **verified_offline** | `harmony_runtime/risk.py` 单一词表；17 项 |
| 9 | Burst / Wait / History 离线加固 | **verified_offline** | 10 项边界用例；真机时延 `blocked_device` |
| 10 | Autonomous Runner 离线收敛 | **verified_offline** | 15 项（记忆/Checker/任务级） |
| 11 | M2 任务规格（30 任务） | **verified_offline（规格）** | `evals/tasks/m2-30.json` + 13 项校验；执行 `blocked_device` |
| 12 | 离线故障注入矩阵 | **verified_offline** | 14 项；真机故障 `blocked_device` |
| 13 | 安装 / 迁移 / 回滚 | **verified_offline** | 9 项；干净环境安装与跨版本回滚仍待真机/新环境复跑 |
| — | 真机 Acceptance Sprint | **blocked_device** | 见 `blocked-device.md` |
| — | 三客户端对照（Codex/OpenCode/DeepSeek） | **deferred** | 非发布阻塞；Runtime 保持 client-agnostic |

## 2. Phase 0 — Offline Baseline

状态：`verified_offline`

```text
branch   feat/runtime-foundation
HEAD     8c401c07eede3686349eaa50ead3d0abbd135577（+ 未提交的 Phase 1/2 修改）
Python   3.11.9
tests    346（变更前）→ 392（Phase 1/2 后）→ 562（离线阶段全部完成后），
         0 failed / 0 error / 0 skip
pip      No broken requirements found.
```

已修正 `docs/agent-status.md` 中 336 / 346 的不一致口径。

## 3. Phase 1 — Decider Pluginization

状态：`verified_offline`

### 3.1 变更前后

| 维度 | before | after |
|---|---|---|
| 具体类型依赖 | `AgentHost` 顶层 `from .decision.providers.decider import …` | 只依赖 `DecisionProvider` Protocol |
| 默认构造 | `self.decider = decider or DeciderProvider(...)`（始终构造） | 惰性 factory，仅 `local_shadow` / `local_canary` 且请求时才构造 |
| Router 类型 | `decider: DeciderProvider \| None` | `fast_provider: DecisionProvider \| None`（`decider` 兼容别名） |
| diagnostics | 直接读 Decider 私有属性 `revision` / `breaker` | `provider_name` / `provider_revision` / `provider_health` / `provider_available` / `circuit_open` |
| 包导入 | `decision.providers` 顶层 import Decider | PEP 562 惰性 `__getattr__` |
| 默认 profile | `local_shadow` | `local_off`（当前开发默认，符合 v3.2 政策） |

### 3.2 组件

```text
src/harmony_agent/decision/factory.py   ProviderConfig / ProviderBuild /
                                        resolve_fast_provider / build_fast_provider /
                                        config_from_env
src/harmony_agent/decision/tokens.py    MAX_STATE_TOKENS / MAX_QUESTION_TOKENS /
                                        MAX_QUESTIONS / estimate_tokens（与 provider 无关）
```

Provider Profile 行为（未新增 `active`）：

```text
rules_only    rules only；不 import、不构造、不健康检查
local_off     rules only；同上（当前开发默认）
local_shadow  rules 决定执行；provider 可选调用；结果只记录、绝不派发
local_canary  provider 可选；需 calibration_version + 双阈值 + 低风险 +
              候选有效；最终仍走 Runtime Guard
```

### 3.3 失败隔离

`Router` 现在把三类 provider 故障统一降级为 rules 结论并记录原因码：

```text
ProviderUnavailable        -> fallback_<code>（如 model_busy / model_timeout）
其它异常（provider crash）  -> fallback_provider_error（fallback_reason=provider_error:<Type>）
返回值不符合 ProviderResult -> fallback_malformed_result
```

覆盖的故障：module missing、factory 返回 None、connection refused、timeout、503、
504、invalid/empty body、invalid schema、NaN、Inf、unknown candidate、expired
candidate、old epoch、late answer、revision mismatch、provider crash。

## 4. Phase 2 — Direct / Autonomous Boundary

状态：`verified_offline`

### 4.1 边界定义（冻结）

```text
Direct Runtime      = mobile_session / observe / act / wait / burst / history
                      不依赖 AgentHost / Planner / Supervisor / Memory / Checker / Decider
Autonomous Layer    = HARMONY_AGENT_TOOLS=1 才注册 mobile_run_task / task_* / mobile_decide
                      与 Direct 共用 Session / Observation / Epoch / Guard / Journal / Worker
```

### 4.2 本轮补齐的缺口

`service.serve` 过去在 `host_factory` 抛异常时会让整个常驻服务退出，等于“Agent 层坏了
顺手带走 v1”。现在：

```text
host_factory 抛异常
  -> HARMONY_AGENT_REQUIRED 未开启：host=None，stderr 记录一行，六工具继续服务
  -> HARMONY_AGENT_REQUIRED=1：fail fast，不静默降级
```

### 4.3 离线证据

```text
无宿主时六工具端到端可用（session/observe/act/wait/history）
无宿主时 agent_* RPC 名未注册（unknown method）
无宿主时 Runtime 生命周期独立可用
宿主初始化失败后六工具仍可用、writes=1（无重复派发）
关闭 AgentHost 不关闭 Runtime，之后仍可 observe/act
AgentHost 构造不打开设备
host_from_env 未开启返回 None；开启后默认 profile=local_off
```

## 5. Phase 3–13 摘要

详细说明分散在下列文件，避免本台账重复：

```text
Phase 3   docs/v3.2/observation-cache-semantics.md
Phase 4   tests/test_agent_grounding_fixtures.py（23 项 fixture 回归）
Phase 5   docs/v3.2/adr-visual-target-authority.md
Phase 6/7 docs/v3.2/visual-providers.md
Phase 8   docs/v3.2/risk-taxonomy.md
Phase 9/10/12/13  docs/v3.2/offline-hardening.md
Phase 11  docs/v3.2/m2-benchmark-spec.md
```

本阶段真正改动的**产品代码**（区别于纯测试/文档）：

```text
src/harmony_agent/decision/factory.py      新增：惰性 provider factory
src/harmony_agent/decision/tokens.py       新增：与 provider 无关的预算常量
src/harmony_agent/ocr.py                   新增：OCR 适配器
src/harmony_agent/vlm.py                   新增：VLM 视觉定位适配器
src/harmony_agent/visual_regions.py        新增：区域归一化
src/harmony_runtime/risk.py                新增：统一风险词表（唯一真源）
src/harmony_runtime/visual.py              扩展：区域裁剪与内容摘要
src/harmony_runtime/contracts.py           扩展：Target.visual / VisualRegion
src/harmony_runtime/observation.py         扩展：视觉目标复核
src/harmony_runtime/runtime.py             扩展：视觉 preflight/策略/运输异常归类
src/harmony_runtime/service.py             扩展：宿主构建失败降级
src/harmony_agent/grounding.py             扩展：可复核视觉候选 + 兜底
src/harmony_agent/supervisor.py            扩展：视觉句柄透传
src/harmony_agent/candidates.py            收敛：复用共享风险词表
```

## 6. 已知限制与风险

```text
真机：全部设备结论仍未取得（blocked_device）
视觉：链路已闭环（提议→复核→Guard→dispatch→verify），但真实 OCR/VLM 引擎与
      真机像素未验证；带图 preflight 的真实开销未知
视觉限制：只支持 tap/long_press；输入必须落到可观察输入框
风险策略：词表法只能防已知词，同义改写与多语言未覆盖；命中即拒绝（无可信批准流程）
Grounding 限制：输入框 placeholder（hint）不参与文本匹配，必须以 id/a11y 定位
观察缓存：Session.observations 仍是 authority cache，性能复用层未引入（有意）
任务级 model_profile：TaskSubmit 声明了该字段，runner 使用宿主 profile，未做差异审计
Phase 13：干净环境安装与跨版本回滚只在离线状态目录层面验证
```

## 7. 下一步

离线部分已按计划推进到 Phase 13。真机恢复后按 `blocked-device.md` 顺序执行
Device Acceptance Sprint；在此之前不因缺少真机而跳到 Decider 优化
（Decider 重评估为 P2，见 `blocked-device.md` 第 9 节）。

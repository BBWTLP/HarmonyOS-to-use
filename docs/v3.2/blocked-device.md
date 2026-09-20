# v3.2 真机待办（blocked_device）

以下项目**必须有真机证据**才能声明通过。当前环境没有可用设备，因此全部保持
`blocked_device`，本文件不记录任何“已完成”结论。

禁止做法（v3.2 明确禁止）：

```text
用 mock / fake / fixture 结果声称 verified_device
为了“完成验收”伪造 benchmark 或调整分母
修改历史真机报告来制造新证据
```

设备恢复后按本文顺序执行；App 版本、系统构建与设备串号按当次实测填写。

## 0. 设备就绪检查

```text
系统版本 / 软件发行版本
HDC 版本与连接方式
uitest 版本
设备能力（foreground / wake / unlock / screenshot）
被测应用版本
当前屏幕状态（screen_on / screen_locked）
```

入口：`harmony-runtime baseline --execute`、`harmony-runtime doctor`、
`harmony-runtime protocol --execute`、`tests/smoke_device.py`（只读）。

## 1. M0 原语矩阵

```text
每个原语 100 次：launch / tree / screenshot / swipe / tap / back / input
记录 attempted / failed / 失败分类
门槛：单原语成功率与失败原因均可解释
```

入口：`scripts/accept_m0_primitives.py`。当前仅有小样本（每原语 25 次），
样本量不足以证明 99%。

## 2. M1 冻结版本单批验收

```text
10 tasks × 3 = 30 次，单批不中断
中途不得因 worker 隔离而拆批
0 error completion（错误宣告成功）
```

入口：`scripts/accept_m1_weibo.py`。历史批次曾拆成两批，需单批复跑。

## 3. C01 受控性能基准

```text
controlled cold / controlled warm
FAST / FAST+image / FULL
记录 attempted / failed / P50 / P95 / 分阶段耗时
```

当前只有条件未受控的每组 3 次样本，不能宣称正式 P95。

## 4. C03 Burst 真机裁决

```text
1-step / 2-step / 3-step 分别在 3000ms 共享预算下的真实裁决
watch_timeout_ms 目标轮询的真机时序
动态控件与视频类页面
```

入口：`scripts/accept_c03_burst.py`。

## 5. 视觉链路

```text
真实视觉目标（image region）派发
真实 OCR 点击：坐标变换、旋转、小字号、中英文混排
真实 VLM 视觉定位：icon-only / Canvas / 自定义 ArkUI / WebView
ground → revalidate → guard → dispatch → verify 全链路
```

前提已满足：Phase 5 的 Visual Target Authority ADR 已落地
（`docs/v3.2/adr-visual-target-authority.md`），OCR/VLM 适配器已实现
（`docs/v3.2/visual-providers.md`）。仍然禁止 raw coordinate bypass。
真机执行前需要先接入真实 OCR/VLM 引擎。

## 6. M2 正式任务验收

```text
30 tasks × 10 = 300 runs
建议目标：overall ≥ 90%，L3 ≥ 80%
0 unauthorized actions
0 duplicate irreversible writes
0 false completion
```

任务规格已按 Phase 11 定义：`evals/tasks/m2-30.json`（L1/L2/L3 各 10，
含 3 个反向控制任务），校验见 `tests/test_m2_specification.py`。
执行留到设备恢复后；规格文件本身不含任何结果。

## 7. 100 步稳定性

```text
RSS / CPU / 线程数 / 句柄数
SQLite 大小与锁行为
artifact 增长与配额
worker 隔离次数
latency drift
```

## 8. 真机故障矩阵

```text
USB 断连
HDC 断连
设备重启
设备锁屏
真实 worker hang
屏幕状态突变
```

要求：不重复写、不错误宣告成功、unknown 状态保留、journal barrier 保持、可恢复。

## 9. Decider 真实数据采集（P2）

```text
minimum 300，preferred 500–1000 个真实 decision states
Rules / Pinned Decider / New revision / optional small LLM 对照
指标：Coverage / Wrong-Allow Rate / Abstention / Calibration / P50 / P95 /
      LLM call reduction / Task success delta / 端到端 latency delta
```

当前真实数据为 45 个状态，距门槛有差距；在此之前保持 `local_off`，
需要对照时用 `local_shadow`。

## 10. 客户端对照（deferred，非设备阻塞）

```text
Codex / OpenCode / DeepSeek Harness 三 Agent 对照
G05 multi-client formal acceptance
client ranking
```

标记 `deferred`、非发布阻塞；Runtime 必须保持 client-agnostic。

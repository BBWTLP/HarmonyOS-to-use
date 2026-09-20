# M0 harness 修复的真机验证 · RC `19ee190`

执行日期：2026-09-20（Asia/Shanghai）
构建：`19ee190`（`HEAD` 与工作树一致，tree clean）

## 1. 验证序列（按真机计划 Stage 16）

```text
离线回归        Ran 644 tests，exit 0，pip check clean（3.11.16 / 3.13.14 均验证）
Stage 5–7       doctor / baseline / protocol / smoke_device / probe / agent link：GREEN
M0 专项         --only back input --per-primitive 3
全量 M0 smoke   --per-primitive 3（7 原语 × 3）
```

## 2. M0 专项（back / input × 3）

```text
status=ok   valid_attempts=6   success=6   setup_attempts=7
setup_stale_refusals=3   setup_failures=0

back   valid=3 success=3 rate=100%   setup_attempts=6 stale_refusals=3 target_drift=3
input  valid=3 success=3 rate=100%   setup_attempts=1 stale_refusals=0
```

对比上一轮 RC `b4049f5`（back 0/3、input 1/3）：同样这台设备、同样这版微博，
`back`/`input` 现在 3/3。差别在于修复后的 setup 会在**每次拒绝后重新 observe 并重新定位**，
把「观察 → 派发」的窗口压到最小，并给出明确的 `SetupBudget`，而不是复用旧目标、反复空转。

## 3. 全量 M0 smoke（7 原语 × 3）

```text
status=ok   valid_attempts=21   success=21   rate=100%
setup_attempts=10   setup_stale_refusals=2   setup_failures=0
unresolved_actions=0   recovery_required=false
```

| 原语 | valid | success | rate | P50 ms | setup_attempts | stale_refusals |
|---|---:|---:|---:|---:|---:|---:|
| launch | 3 | 3 | 100% | 5880 | 0 | 0 |
| tree | 3 | 3 | 100% | 4601 | 0 | 0 |
| screenshot | 3 | 3 | 100% | 4398 | 0 | 0 |
| swipe | 3 | 3 | 100% | 6967 | 4 | 1 |
| tap | 3 | 3 | 100% | 5690 | 0 | 0 |
| back | 3 | 3 | 100% | 5870 | 5 | 1 |
| input | 3 | 3 | 100% | 8885 | 1 | 0 |

## 4. 安全侧

```text
execution_unknown      0
duplicate write        0
wrong target execution 0
false completion       0
unresolved_actions     0
recovery_required      false
```

`stale_observation` 依旧在派发前拒绝（2 次 setup 拒绝被正确吸收并计入 `setup_stale_refusals`），
Runtime 的 Guard / TTL / journal / unknown-write 语义未改动。

## 5. 口径说明（不得误读）

- 本文件是 **smoke**（每原语 3 次），不是正式 M0。正式门槛要求
  `valid_attempts == requested_samples`（100）且每原语 rate ≥ 0.99。
- setup 不进入原语成功率分母，但逐一列在表中；`setup_failures=0` 表示每次 setup 最终都成功，
  期间的拒绝记在 `setup_stale_refusals`。
- 本轮停在 RC 冻结；M0 25 / 100 与后续 Stage 按真机计划继续，不要用本文件的 3 次样本
  宣称正式 M0 通过。

原始报告（本地，不入库）：`.runtime/acceptance-20260920-140508/m0-back-input-rc4.json`、
`m0-full-smoke-rc-final.json`。

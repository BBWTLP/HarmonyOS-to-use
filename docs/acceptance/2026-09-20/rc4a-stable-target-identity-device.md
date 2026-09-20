# RC4-A 真机验证 · Stable Target Identity

RC：`9fb0c7557dca599d2e391f7dd7a4ecf08249346d`（tree clean，未 push）
设备：SGT-AL10 · 6.1.0.135(SP8C00E120R3P11) · OpenHarmony-6.1.1.120 · uitest 6.0.2.3
状态目录：`.runtime/acceptance-20260920-155617`（×3）、`.runtime/acceptance-20260920-172317`（×25）

## 1. 观测稳定性（20 次）

```text
consistent = 19/20（1 次不一致发生在 catalog 239→240 切换处）
skew       = 688–751 ms（阈值 1000 ms 未改）
catalog    = 239–240（稳定）
```

## 2. tap / swipe / back / input ×3

```text
status=ok   valid=12   success=12
tap 3/3 · swipe 3/3 · back 3/3 · input 3/3
setup_attempts=9   setup_stale_refusals=2   setup_failures=0
target_match: exact=6  stable_rebind=0  ambiguous=0  missing=0  changed=0
unresolved_actions=0   recovery_required=false
```

## 3. tap / swipe / back / input ×25

```text
status=not_ready   valid=75/100   success=75   （所有已测量样本全部成功）
tap    25/25  rate=1.00  p50=5799 ms  setup_attempts=0  refusals(stale)=2
swipe  25/25  rate=1.00  p50=6577 ms  setup_attempts=7  setup_stale=4  refusals(stale)=1
input  25/25  rate=1.00  p50=8248 ms  setup_attempts=2  setup_stale=1
back    0/25  setup_attempts=10  setup_stale=9  setup_sessions_failed=5
target_match: exact=7  stable_rebind=0  ambiguous=0  missing=0  changed=0
unresolved_actions=0   recovery_required=false   execution_unknown=0
```

`back` 的失败全部是 **setup**（`search_editor_unavailable` / `setup_stale_budget_exhausted`），
不是原语失败、也不是 matcher 失败：它没有产生任何 `missing` / `changed` / `ambiguous`。

## 4. back 无法进入搜索编辑页的原因（setup drift 元数据）

```text
selector=open_search_editor
action_id_changed=false  parent_action_id_changed=false
fields_changed = accessibility_id, hierarchy
subtree_fingerprint_changed = true
```

`hierarchy` 变了说明 UI tree 结构在两次观测之间变化 → **navigation fingerprint 改变**
→ 触发**页面级**守卫 `stale_observation: Page changed since the referenced observation`。
该判断发生在 StableTargetMatcher 之前，因此 RC4-A 的目标身份修复无法覆盖它。

## 5. 结论

```text
RC4-A 目标身份修复      真机有效：matcher 只在页面身份不变时运行，
                        7 次 exact rebind；0 ambiguous / 0 missing / 0 changed；
                        没有一次错误目标派发（unresolved=0, unknown=0）
剩余阻塞                navigation / page identity（微博「发现」页的树结构持续变化）
                        → 属于 RC4-B 范围（entire tree equality 拆成
                          window / navigation / actionable structure / dynamic content）
screenshot              RC4-A 未处理；仍为已知失败
```

规范：本文件只含元数据，不含 UI 文本、截图、输入内容或设备序列号。

---

## 6. 复测（同日稍后，设备重新可测）

状态目录：`.runtime/acceptance-20260920-182142`（×3）、`.runtime/acceptance-20260920-183403`（×25）

### 6.1 observe stability 20

```text
consistent = 19/20（不一致那一次 skew=848 ms，catalog 全程 247 稳定）
skew       = 662–848 ms
```

### 6.2 tap / swipe / back / input ×3

```text
第一次（App 处于切换中：起始是 foreign surface，需要先 launch+导航）
  status=not_ready  valid=0   setup_attempts=39  setup_stale=32  setup_sessions_failed=5/原语
  matcher: exact=1 其余 0        ← 全部卡在 setup，不是目标身份

第二次（App 已稳定停在搜索编辑页：4/4 采样 nav_equal=true、full_equal=true、catalog 118）
  status=ok  valid=12 success=12（tap/swipe/back/input 各 3/3）
  setup_attempts=10  setup_stale=2  setup_sessions_failed=0
  matcher: exact=3  stable_rebind=0  ambiguous=0  missing=0  changed=0
  unresolved_actions=0  recovery_required=false
```

### 6.3 tap / swipe / back / input ×25

```text
status=not_ready   valid=82/100   success=82（所有已测量样本 100% 成功）
tap    25/25  rate=1.00  p50=5851 ms  setup_attempts=0
swipe  25/25  rate=1.00  p50=6638 ms  setup_attempts=4  setup_stale=1
input  25/25  rate=1.00  p50=8586 ms  setup_attempts=0
back    7/25  insufficient_valid_samples
       setup: attempts=10 success=9 failures=0 stale_refusals=1 per session
              sessions_failed=5，最终码 search_editor_unavailable
matcher: exact=3  stable_rebind=0  ambiguous=0  missing=0  changed=0
unresolved_actions=0  recovery_required=false  execution_unknown=0
```

`back` 的失败特征与目标身份无关：**每个 setup session 的 10 个动作里 9 个成功、只有 1 次
stale refusal**，但循环用尽 10 个动作仍未进入「已聚焦的搜索编辑页」，5 个 session 后停止。
drift 显示该 selector 的目标为 `action_id` 未变、仅 `accessibility_id`/subtree 变化，
即 matcher 可匹配；瓶颈在 harness 的导航状态机（tabs → discover → 搜索框 → 聚焦编辑页）
在 10 步预算内没有收敛，而不是 Guard 拒绝。

### 6.4 结论（复测）

```text
RC4-A 目标身份            真机两次运行共 exact=10（3+7），
                          ambiguous=0 / missing=0 / changed=0 / stable_rebind=0
                          无错误目标派发、无未知写、无未决动作
动态写原语中可稳定到达页面的三个   tap / swipe / input 25/25（100%）
剩余阻塞 ①（harness）      back 的 setup 导航在 10 步内收敛不了搜索编辑页
剩余阻塞 ②（RC4-B）        App 处于切换中时导航身份漂移 → 页面级 stale，
                          该场景下 setup 会整批失败（6.2 第一次）
screenshot                仍未处理，保持已知失败
```

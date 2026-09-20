# v3.2 Phase 3 — Observation / Cache 语义

结论先说：**`Session.observations` 是授权缓存（authority cache），不是抓屏性能缓存。**
v3.2 不新增观察模式，也不改变它的语义。

## 1. 不新增观察模式

```text
FAST     默认结构观察；可选截图
FULL     完整 tree + 原图 + SoM 标注
TEMPORAL 3000ms 内最多五帧历史，帧不可作为动作句柄
```

`LIGHT` / `VISUAL` / `TURBO` 等名字会被拒绝（`unsupported_capability`），
见 `tests/test_observation_authority.py::ObservationModeTests`。

## 2. `Session.observations` 是什么

```text
observation_id -> (captured_monotonic, observation)
```

它承担四件事，全部与安全有关，与缓存命中率无关：

```text
1. 唯一权威 observation_id（只有 observe() 能签发）
2. action authority（act/burst 必须引用一个仍然有效的句柄）
3. freshness TTL（15s，过期即无授权）
4. pre-dispatch comparison（派发前重新观察并比对页面身份）
```

同一对象既返回给客户端，也留在缓存里，所以本地调用方不可能拿到一个与缓存句柄
页面身份不同的公开响应。

## 3. 失效规则

| 事件 | 行为 | 证据 |
|---|---|---|
| `pause` / `resume` / `close` | `generation += 1`，清空全部句柄 | `runtime.session` |
| `recover`（重连 worker） | `generation += 1`，清空全部句柄，重新观察 | `runtime.recover` |
| Runtime `close` | 所有会话代次提升并清空句柄 | `runtime.close` |
| 熄屏 / 锁屏 / 状态未知 | 清空句柄后抛 `screen_off` / `screen_locked` / `screen_state_unknown` | `_ready_screen` |
| 抓屏不可信（前台不一致、图树不一致） | `actionable=False`，**不进入缓存** | `_observe` |
| TTL 超过 15s | `cached_observation` 返回 `None`；`act` 报 `stale_observation` | `cached_observation` / `act` |
| 超过 8 个句柄 | 淘汰最旧的一个 | `_observe` |
| 页面/窗口/目标指纹变化 | 派发前重新观察比对，拒绝并报 `stale_observation` | `act` preflight |

## 4. 明确不做的事

```text
不把 Session.observations 改造成抓屏复用缓存
不复用旧 observation_id 给新动作授权
不因为缓存命中而跳过 pre-dispatch 校验
```

如果将来确实需要减少重复抓屏，必须新增独立的 `CaptureReuseCache`，只允许缓存：

```text
raw tree / display metadata / screenshot bytes / derived parse
```

并且复用后若要产生新的可执行观察，必须重新绑定：新的 `observation_id`、
controller epoch、采集时间戳、 freshness 与指纹。**本阶段不实现该缓存**，
因为真实收益只能在真机上测量，当前结论保持 `blocked_device`。

## 5. 离线证据

`tests/test_observation_authority.py`（14 项）：

```text
句柄即授权（缓存对象与返回对象同一）
未知句柄 = 未授权
跨会话读取句柄被拒
pause/resume 清空句柄
recover 提升代次并清空句柄
TTL 过期失去授权
缓存上限 8，淘汰最旧
不可信抓屏不进入缓存
锁屏清理既有句柄
TTL 内页面变化仍拒绝旧句柄
失效后重新观察恢复授权
观察模式仍只有 FAST/FULL/TEMPORAL
TEMPORAL 帧不可授权
capabilities 不虚报 ocr/webview/pro
```

## 6. 真机待办

```text
真实抓屏 P50/P95 与缓存收益（blocked_device）
真实页面变化下的 preflight 开销（blocked_device）
真实 OCR/视觉降级路径下的授权语义（blocked_device，且依赖 Phase 5-7）
```

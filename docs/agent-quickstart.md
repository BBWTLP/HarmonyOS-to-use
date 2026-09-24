# Agent 快速开始（Direct 六工具）

适用于 Codex / 其他 MCP 客户端 / 本会话 Agent 操控鸿蒙真机。

## 启动服务

```powershell
cd 'D:\music mv\HarmonyOS-to-use'
.venv\Scripts\python.exe -m harmony_runtime.cli serve --state-dir .runtime\agent-state
.venv\Scripts\python.exe -m harmony_runtime.cli service --state-dir .runtime\agent-state
```

## 六工具

`mobile_session` / `mobile_observe` / `mobile_act` / `mobile_wait` / `mobile_burst` / `mobile_history`

可选 Agent 工具需 `HARMONY_AGENT_TOOLS=1`。**Direct 路径不依赖内部 Actor / Decider。**

## 自动息屏恢复

无密码息屏后调用 `mobile_observe` 或 `mobile_session(operation=recover)`。Runtime 会无凭据唤醒并解锁，确认 `screen_on=true && screen_locked=false` 后取**新** observation 再继续。旧句柄全部失效；unknown 写入禁止盲重放。

## 微博搜索最小路径

1. session open → observe  
2. 必要时点「回到顶部」→「发现」（点可点父节点）  
3. 点宽搜索条（「猜你想搜」）→ `redesignedSearchInput`  
4. `replace_text` 目标 `{resource_id: redesignedSearchInput}`  
5. 返回用「取消」，用 `surface_is` / `foreground_is` 验证  

禁止：发送 / 去支付 / 删除 / 关注 / 点赞 / 读私人会话。

## 验收命令

```powershell
.venv\Scripts\python.exe scripts/accept_m0_primitives.py --execute --state-dir .runtime\agent-state --per-primitive 100
.venv\Scripts\python.exe scripts/accept_m1_weibo.py --execute --state-dir .runtime\agent-state --transport service --mode formal --runs 3
.venv\Scripts\python.exe scripts/accept_long_task.py --execute --state-dir .runtime\agent-state --tasks evals/tasks/long-device.json --runs 1
.venv\Scripts\python.exe scripts/accept_m2.py --execute --state-dir .runtime\agent-state --tasks evals/tasks/m2-30.json --runs 1 --transport service
```

回滚见 `docs/runbook-rollback.md`。当轮结果见 `docs/acceptance/current-run/README.md`。

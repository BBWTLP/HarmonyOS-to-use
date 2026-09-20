# v3.2 Phase 8 — Risk Policy Unification

## 问题

两个地方各自维护敏感词：

```text
harmony_agent/candidates.py  HIGH_RISK_TERMS + risk_class_for()
harmony_runtime/runtime.py   _policy() 内的 danger 元组
```

两侧词表不同，就会出现"Agent 认为低风险、Runtime 也放行"的漂移。

## 决策

唯一真源：`src/harmony_runtime/risk.py`

```text
CATEGORIES       payment / delete / send / permission / credential / session
RISK_CLASSES     low / medium / high
scan(label)      命中的敏感词
categories(label)命中的类别
is_sensitive()   Runtime Guard 使用的判定
risk_class_for() Agent 路由使用的分级
label_of(target) Runtime 检查的可读字段集合
```

权威关系：

```text
Runtime Guard  = 最终 Authority（唯一的放行/拒绝决定者）
Agent 分级     = 路由提示与预过滤
Agent 只能与 Runtime 同级或更保守，永远不能更宽松
```

因为两者调用同一个函数，`tests/test_risk_taxonomy.py::AuthorityParityTests`
可以直接证明"Agent 不可能比 Runtime 更宽松"。

## 匹配规则

```text
中文：子串匹配（"请输入支付密码" 命中"密码"）
英文：词边界匹配，且先 lower()
标识符分隔符（_ . / \ -）视为词边界
```

最后一条是**故意的保守选择**：`account_avatar`、`submit_button` 会被视为包含
`account` / `submit`。理由：

```text
误判为敏感 = 拒绝一次动作（可恢复）
漏判为安全 = 一次非预期写入（不可恢复）
可信批准流程在本版本尚未实现，因此宁可拒绝
```

## 覆盖范围

类别与代表性词（中英双语）：

| 类别 | 中文 | 英文 |
|---|---|---|
| payment | 支付 / 付款 / 转账 / 购买 / 下单 / 充值 | pay / payment / purchase / checkout / transfer |
| delete | 删除 / 卸载 / 清空 / 格式化 | delete / remove / uninstall / erase / reset |
| send | 发送 / 发表 / 提交 / 发布 / 上传 | send / submit / post / publish / upload |
| permission | 允许 / 授权 / 权限 | permission / authorize / grant / allow |
| credential | 密码 / 验证码 / 动态码 / 指纹 / 面容 | password / passcode / pin / otp / fingerprint |
| session | 退出登录 / 注销 / 登出 / 切换账号 / 账号 | logout / log out / sign out / account / login |

## 行为

命中后 Runtime 返回：

```text
RuntimeFault("approval_required",
             "Sensitive target blocked. Trusted approval flow is not implemented in this build.")
```

动作**不会派发**，也不会写入 journal。视觉目标的 label 同样参与判定，
因此 OCR/VLM 不能通过"只给区域不给文字"绕过风险检查。

## 离线证据

`tests/test_risk_taxonomy.py`（17 项）：

```text
每个类别同时含中英词条
中英文敏感词逐一命中且落入正确类别
低风险标签保持 low（搜索/首页/取消/Search/Home/Cancel/播放 …）
标识符分隔符视为词边界（保守命中）
连续字母串不拆分（pinsix 不命中，pin_six 命中）
空/缺失标签为 low
大小写与空白无关
中文短语在长文案中被命中
Agent 分级与 Runtime 分级逐词一致
Runtime 会看 target 的所有选择器字段
```

## 已知限制

```text
没有可信批准流程：命中即永久拒绝，无法在对话内确认
没有语义理解：只看词表，同义改写可能绕过（真机泛化评测前不声称完备）
中英之外的语言未覆盖
```

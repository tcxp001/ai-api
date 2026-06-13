# Ai-api 公益服管理：推进工作计划

> 日期：2026-06-13  
> 依据：`PRODUCT_PLAN.md`  
> 原则：聚焦、做减法；每做一个功能，都要明显提升个人管理公益服与使用 Codex/Hermes 的体验。

## 0. 当前阶段目标

当前阶段先推进 **V1：个人公益服管理闭环**。

V1 的目标不是做大而全平台，而是完成这条主路径：

```text
新增公益服 Provider
-> 配置 Base URL / Key / UA / Headers / Models
-> 测活与诊断
-> 生成本地 Provider URL
-> 一键同步或复制 Codex/Hermes 配置
-> 通过 AIProxy 测试可用
```

V1 完成标准：

> 拿到一个公益服账号后，可以在 1 分钟内配置好，并确认 Codex/Hermes 是否能用。

---

## 1. 工作阶段总览

| 阶段 | 名称 | 目标 |
|---|---|---|
| Phase 1 | 现有功能稳定化 | 先把 Provider 管理、测活、AIProxy、Codex/Hermes 同步这些已有能力梳理清楚、减少混乱 |
| Phase 2 | Provider 体验增强 | 提升新增、编辑、模型、UA/Header、复制 URL 的日常体验 |
| Phase 3 | 测活诊断闭环 | 让用户快速知道哪个 Provider 能用、为什么不能用、怎么修 |
| Phase 4 | Codex/Hermes 配置闭环 | 让配置生成、同步、备份、测试形成完整闭环 |
| Phase 5 | AIProxy 使用闭环 | 让本地代理状态、启动、重启、经代理测试更可靠 |
| Phase 6 | V1.5 同模型组 | 在 V1 稳定后增加同模型 Provider 池：fallback / round_robin |

---

## 2. Phase 1：现有功能稳定化

### 2.1 目标

先不加大功能，先把当前已经存在的功能整理稳定。

当前项目已有：

- `proxy.py`：本地 Provider 代理
- `dashboard.py`：管理台后端
- `dashboard.html`：管理台前端
- `api.py`：Provider 测试脚本逻辑
- `config.example.yaml`：Provider 配置样例

Phase 1 重点是：

```text
减少混乱
减少重复逻辑
明确当前功能状态
保证保存、测活、同步、重启不容易出错
```

### 2.2 任务清单

#### 任务 1：补充项目说明文档

- 新增或更新 `README.md`
- 说明：
  - 产品定位
  - 启动 dashboard
  - 启动 AIProxy
  - 配置文件位置
  - Provider URL 格式
  - Codex/Hermes 基本使用方式

验收标准：

- 新用户看 README 能跑起来。

#### 任务 2：整理配置字段说明

- 在文档中明确 Provider 字段含义：
  - `name`
  - `base_url`
  - `api_key`
  - `api_mode`
  - `models`
  - `headers`
  - `remove_headers`
  - `enabled`
  - `custom_endpoint`
  - `reasoning_effort`
  - `fallback_responses_to_chat`

验收标准：

- 不需要看代码也知道每个字段怎么填。

#### 任务 3：保存配置前校验

Provider 保存时校验：

- `name` 不能为空
- `name` 不重复
- `name` 适合作为 URL path
- `base_url` 必须是 `http://` 或 `https://`
- `api_key` 可以为空但要提示
- `models` 为空时要提示
- `headers` 必须是对象
- `remove_headers` 必须是数组

验收标准：

- 错误配置不会静默保存。
- 错误信息能指导用户修复。

#### 任务 4：保存配置后自动重启代理结果更清楚

当前保存配置后会尝试重启 AIProxy。

需要明确展示：

- 是否保存成功
- 是否创建了备份
- 是否同步了 Codex/Hermes
- 是否成功重启 AIProxy
- 如果重启失败，失败原因是什么

验收标准：

- 保存后用户知道“配置已生效”还是“需要手动重启”。

---

## 3. Phase 2：Provider 体验增强

### 3.1 目标

让 Provider 列表成为日常主页面。

用户打开页面后应该马上看到：

```text
哪些 Provider 启用了
哪些可用
最近哪里失败了
本地 URL 是什么
能否一键复制和测试
```

### 3.2 任务清单

#### 任务 1：Provider 列表增加关键信息

Provider 行展示：

- 名称
- 启用状态
- API 模式
- 模型数量
- 最近健康状态
- 最近错误
- 最近检测时间
- 本地 URL

验收标准：

- 不点编辑也能判断 Provider 是否值得使用。

#### 任务 2：一键复制本地 URL

每个 Provider 提供：

```text
复制本地 URL
```

格式：

```text
http://127.0.0.1:18006/{provider}/v1
```

验收标准：

- 用户不用手拼 URL。

#### 任务 3：UA/Header 预设

Provider 编辑器中提供预设：

- 默认
- 空 UA
- Curl UA
- Chrome UA
- Codex UA
- 自定义

验收标准：

- 用户不用记常见 UA。
- 切换预设后能直观看到最终 headers。

#### 任务 4：模型获取和勾选加入

优化“获取模型”：

```text
点击获取模型
-> 展示远端模型列表
-> 勾选需要的模型
-> 加入当前 Provider
```

验收标准：

- 用户尽量不用手动输入模型名。

---

## 4. Phase 3：测活诊断闭环

### 4.1 目标

测活不只是返回成功/失败，而是帮助用户快速定位问题。

目标体验：

```text
这个 Provider 能不能用？
哪个 endpoint 能用？
哪个 UA 能用？
失败原因是什么？
下一步怎么修？
```

### 4.2 任务清单

#### 任务 1：单 Provider 测活入口更明显

每个 Provider 行提供：

- 测当前默认模型
- 测全部模型
- 测 UA 矩阵

验收标准：

- 用户不需要进入复杂页面才能测一个 Provider。

#### 任务 2：错误分类标准化

统一错误分类：

| 类型 | 说明 |
|---|---|
| `ok` | 可用 |
| `unauthorized` | 401，Key 可能无效 |
| `forbidden` | 403，可能是 UA/WAF/权限 |
| `not_found` | 404，路径或模型可能不支持 |
| `rate_limited` | 429，限流或额度 |
| `timeout` | 超时 |
| `server_error` | 5xx，上游服务异常 |
| `network_error` | 连接失败、DNS、SSL 等 |
| `invalid_response` | 返回格式不符合预期 |

验收标准：

- 前端展示不直接裸露难懂异常。

#### 任务 3：错误修复建议

针对常见错误展示建议：

- 401：检查 API Key
- 403：尝试空 UA / Codex UA / Originator
- 404：检查 base_url 是否包含 `/v1`、模型名、api_mode
- 429：暂时停用或降低优先级
- 5xx：上游异常，稍后重试或换 Provider
- timeout：提高 timeout 或换 Provider

验收标准：

- 失败后用户知道下一步该做什么。

#### 任务 4：通过 AIProxy 测试

Provider 测活分两类：

- 直连上游测试
- 经本地 AIProxy 测试

验收标准：

- 能区分是“上游本身不通”，还是“AIProxy 配置/服务有问题”。

---

## 5. Phase 4：Codex/Hermes 配置闭环

### 5.1 目标

让 Codex/Hermes 配置从“手工改文件”变成“复制或一键同步 + 自动备份 + 测试”。

### 5.2 任务清单

#### 任务 1：Provider 一键复制 Codex 配置片段

每个 Provider 支持复制 Codex 配置。

配置目标：

```text
base_url = http://127.0.0.1:18006/{provider}/v1
```

验收标准：

- 用户可以直接粘贴到 Codex 配置文件中使用。

#### 任务 2：Provider 一键复制 Hermes 配置片段

每个 Provider 支持复制 Hermes 配置。

验收标准：

- 用户可以直接粘贴到 Hermes 配置中使用。

#### 任务 3：一键同步 Codex

同步前：

- 显示预览
- 备份旧配置

同步后：

- 显示结果
- 提供测试按钮

验收标准：

- 用户不用手改 Codex 配置。
- 出问题可恢复。

#### 任务 4：一键同步 Hermes

同 Codex。

验收标准：

- 用户不用手改 Hermes 配置。
- 出问题可恢复。

#### 任务 5：同步后测试

同步完成后，可以直接测试：

```text
用该 Provider/Model 发一个最小请求
```

验收标准：

- 配置不是“写入成功”就结束，而是确认“真的能用”。

---

## 6. Phase 5：AIProxy 使用闭环

### 6.1 目标

让 AIProxy 服务状态清晰，出问题时能快速恢复。

### 6.2 任务清单

#### 任务 1：AIProxy 状态展示增强

展示：

- 服务是否存在
- 是否运行
- 监听地址
- 端口
- 进程 PID
- HTTP 探测状态
- 当前配置文件

验收标准：

- 用户一眼知道 AIProxy 是否可用。

#### 任务 2：一键重启更可靠

重启后自动探测：

```text
http://127.0.0.1:{port}/
```

或：

```text
http://127.0.0.1:{port}/{provider}/v1/models
```

验收标准：

- 点击重启后明确知道成功或失败。

#### 任务 3：端口和 URL 显示统一

所有地方的本地 URL 都使用当前 AIProxy 配置端口。

验收标准：

- 修改端口后，Provider URL 和配置片段同步更新。

---

## 7. Phase 6：V1.5 同模型组

### 7.1 目标

实现同模型 Provider 池：

```text
多个 Provider 的同一个模型
-> 一个模型组
-> 一个本地入口
-> fallback 或 round_robin
-> Codex/Hermes 使用这个组
```

### 7.2 数据结构建议

在配置中增加：

```yaml
groups:
  - name: gpt-5.5-pool
    enabled: true
    model: gpt-5.5
    strategy: fallback
    members:
      - provider: provider-a
        enabled: true
        priority: 10
      - provider: provider-b
        enabled: true
        priority: 20
      - provider: provider-c
        enabled: true
        priority: 30
```

只支持同模型。

### 7.3 任务清单

#### 任务 1：自动发现同模型

扫描所有 Provider 的 models，展示：

```text
gpt-5.5：8 个 Provider 支持
glm-5.1：3 个 Provider 支持
deepseek-v4-pro：4 个 Provider 支持
```

验收标准：

- 用户能一键基于同模型创建模型组。

#### 任务 2：模型组管理页面

支持：

- 创建模型组
- 编辑成员
- 调整优先级
- 启用/停用成员
- 设置策略：fallback / round_robin
- 复制组 URL

验收标准：

- 用户可以不用改 YAML 就管理模型组。

#### 任务 3：代理支持 `/group/{group}/v1`

新增入口：

```text
/group/{group}/v1
```

例如：

```text
http://127.0.0.1:18006/group/gpt-5.5-pool/v1
```

验收标准：

- Codex/Hermes 可以通过模型组入口调用。

#### 任务 4：fallback 策略

规则：

- 按 priority 从小到大尝试
- 当前成员失败且还未向客户端输出正文时，尝试下一个
- 如果已经开始 streaming 输出，不中途切换

验收标准：

- 主 Provider 不可用时，可以自动尝试备用 Provider。

#### 任务 5：round_robin 策略

规则：

- 按成员顺序轮询
- 跳过 disabled 成员
- 当前成员失败时尝试下一个

验收标准：

- 多个同模型公益服可以分散请求。

#### 任务 6：模型组配置 Codex/Hermes

模型组也支持：

- 复制 Codex 配置
- 复制 Hermes 配置
- 一键同步
- 同步后测试

验收标准：

- Codex 可以直接使用 `gpt-5.5-pool`。

---

## 8. 暂不排期事项

以下功能明确不进入当前工作计划。

### 8.1 平台化功能

- 登录系统
- 多用户
- 用户 API Key
- 计费
- Token 成本
- 公网 API 网关

### 8.2 大而全工具管理

- Claude Code 全量管理
- Gemini CLI
- OpenCode
- OpenClaw
- MCP
- Skills
- Prompts
- 桌面 App
- 系统托盘

### 8.3 复杂路由

- 全局 `/v1`
- 混合模型组
- 跨模型 fallback
- 模型改写
- health_first
- latency_first
- cost_first
- adaptive routing
- 复杂熔断器
- 流式响应中途切换 Provider

### 8.4 自动化账号行为

- 自动注册
- 自动签到
- CAPTCHA 绕过
- 风控绕过
- 批量账号自动化

---

## 9. 推荐执行顺序

建议按以下顺序推进：

```text
1. README 和配置字段说明
2. Provider 保存校验
3. Provider 列表状态增强
4. 一键复制 Provider URL
5. UA/Header 预设优化
6. 测活错误分类和修复建议
7. 经 AIProxy 测试链路优化
8. Codex 配置复制/同步/备份/测试闭环
9. Hermes 配置复制/同步/备份/测试闭环
10. AIProxy 状态和重启体验增强
11. 同模型自动发现
12. 模型组管理页面
13. `/group/{group}/v1` 代理入口
14. fallback
15. round_robin
```

---

## 10. 每个阶段的验收方式

### V1 验收

拿一个新公益服账号，完成：

```text
新增 Provider
-> 选择 UA/Header
-> 获取模型
-> 测活
-> 复制本地 URL
-> 同步到 Codex
-> 同步到 Hermes
-> 经 AIProxy 测试成功
```

如果整个过程顺畅，V1 达标。

### V1.5 验收

拿三个支持 `gpt-5.5` 的 Provider，完成：

```text
创建 gpt-5.5-pool
-> 设置 fallback
-> 配置 Codex 使用 /group/gpt-5.5-pool/v1
-> 主 Provider 停用后自动走备用
-> 切换 round_robin 后请求能轮询
```

如果体验清晰稳定，V1.5 达标。


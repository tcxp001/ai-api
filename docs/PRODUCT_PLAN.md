# Ai-api 公益服管理：产品定位与工作规划

> 版本：v0.1  
> 日期：2026-06-13  
> 状态：已收敛的产品边界与阶段规划

## 1. 最终产品定位

**Ai-api 公益服管理** 是一个面向个人使用的本地工具，用于管理自己手里的多个公益服 Provider，并通过本地 AIProxy 快速、明确、稳定地供 Codex / Hermes 使用。

一句话定位：

> 个人本地工具，只解决“多个公益服 Provider 如何快速配置、测活、分组，并稳定给 Codex/Hermes 使用”这一件事。

产品不追求成为多人 API 平台、商业网关或桌面全家桶，而是聚焦个人高频工作流：

```text
添加公益服
-> 配好 Header / UA / 模型
-> 测活和诊断
-> 自动生成本地 Provider URL（供配置生成内部使用）
-> 同步或复制 Codex/Hermes 配置
-> 通过本地 AIProxy 使用
-> 多个同模型 Provider 可组成模型组做 fallback/round_robin
```

## 2. 核心用户与使用环境

### 2.1 核心用户

只有一个核心用户：

> 个人用户，也就是自己。

初期不考虑多人协作、账号体系、权限体系和公网服务。

### 2.2 部署形态

产品形态：

```text
本地 Web 管理台 + 本地 AIProxy 服务
```

典型访问方式：

```text
http://127.0.0.1:18080
```

本地代理典型地址：

```text
http://127.0.0.1:18006
```

默认应优先监听本机地址，避免无意暴露到公网或局域网。

## 3. 核心场景

产品只服务以下核心场景。

### 3.1 添加一个公益服 Provider

用户拿到一个公益服账号后，可以快速完成：

```text
新增 Provider
-> 填 base_url / api_key
-> 选择 UA/Header 预设
-> 获取或填写模型
-> 保存
-> 测活
```

目标体验：

> 1 分钟内知道这个公益服能不能用、该怎么用。

### 3.2 让 Codex/Hermes 使用某个公益服

用户选择一个 Provider 后，可以：

```text
自动生成本地 Provider URL
生成 Codex 配置片段
复制 Hermes 配置片段
一键同步 Codex/Hermes 配置
同步前自动备份
同步后测试
```

目标体验：

> 不再手动拼 base_url、headers、api_mode，不再反复改配置排错。

### 3.3 多个 Codex CLI 分别使用不同 Provider

用户会同时开启多个 Codex CLI，并希望每个 CLI 使用不同公益服 Provider。

因此产品必须保留并强化固定 Provider 入口：

```text
/{provider}/v1
```

示例：

```text
http://127.0.0.1:18006/provider-a/v1
http://127.0.0.1:18006/provider-b/v1
http://127.0.0.1:18006/provider-c/v1
```

这个模式是核心能力，不是临时方案。

### 3.4 同模型多个公益服组成模型组

用户倾向把同一个模型在多个公益服上的 Provider 组成一个池，例如：

```text
provider-a / gpt-5.5
provider-b / gpt-5.5
provider-c / gpt-5.5
provider-d / gpt-5.5
```

组成：

```text
gpt-5.5-pool
```

通过模型组入口使用：

```text
http://127.0.0.1:18006/group/gpt-5.5-pool/v1
```

支持策略：

- `fallback`：按优先级主备切换
- `round_robin`：按顺序轮询分散压力

目标体验：

> 同一个模型有多个公益服时，可以自动备用或轮询，不用手动来回切。

### 3.5 维护本地 AIProxy

用户可以在 Web 管理台中完成：

```text
查看 AIProxy 状态
启动 / 停止 / 重启
修改端口
创建或更新 systemd 服务
通过代理测试 Provider 或模型组
```

目标体验：

> 本地代理状态清楚、可控，不需要频繁命令行折腾。

## 4. 产品原则

后续所有功能都按以下原则筛选。

### 4.1 聚焦个人

只服务个人本地使用，不做多人平台。

### 4.2 本地优先

默认本机使用，不以公网部署为前提。

### 4.3 可控优先

用户应该清楚知道当前用的是哪个 Provider 或哪个模型组。

### 4.4 稳定优先

不要为了自动化牺牲可调试性。

### 4.5 配置安全优先

任何写入配置的行为都必须：

- 可预览
- 自动备份
- 可恢复

涉及：

- `config.yaml`
- Codex 配置
- Hermes 配置
- AIProxy 服务配置

### 4.6 兼容性优先

公益服差异大，产品价值就在于处理这些差异：

- endpoint 差异
- header 差异
- User-Agent 差异
- Chat/Responses 支持差异
- reasoning 支持差异
- stream 稳定性差异

### 4.7 不碰账号获取和风控绕过

只管理用户已有的合法 API 配置。

不做：

- 自动注册公益服账号
- 自动签到刷额度
- CAPTCHA 绕过
- 风控绕过
- 批量账号自动化操作

### 4.8 做减法

只做能明显提升日常使用体验的功能。

判断标准：

1. 是否让配置更快？
2. 是否让排错更快？
3. 是否直接服务 Codex/Hermes？
4. 是否保持 Provider 可控？
5. 是否每天都会用？

如果答案不明确，先不做。


### 4.9 页面职责清晰优先

不是功能越多越好。每个页面只做自己职责内的事情，避免把低频、调试、跨页面动作塞到主流程里。

页面边界：

- **Provider 维护页**：只做 Provider 的新增、编辑、复制、删除、启用/停用和配置字段维护。不放测活按钮，不放手动复制本地代理 URL 等调试动作。
- **Provider 测活页**：只做测活、结果展示、错误诊断和修复建议。测活动作集中在这里。
- **AIProxy 页**：只做本地代理服务状态、启动、停止、重启、端口和服务配置。
- **Codex/Hermes 配置能力**：应围绕完整配置闭环设计，不随意在 Provider 行里堆按钮。

新增任何按钮或入口前，必须先回答：

1. 它是否属于当前页面职责？
2. 它是否减少核心路径步骤？
3. 它是否会增加视觉干扰或心智负担？
4. 它是否可以由自动生成或后续配置闭环替代？
5. 如果不是高频动作，是否应该隐藏、延后或不做？

如果答案不明确，默认不做。

## 5. 做什么

### 5.1 Provider 管理

Provider 是核心对象。

需要维护：

- `name`
- `base_url`
- `api_key`
- `api_mode`
- `enabled`
- `models`
- `headers`
- `remove_headers`
- `custom_endpoint`
- `reasoning_effort`
- `fallback_responses_to_chat`
- 备注或标签（后续可选）

Provider 页面应支持：

- 新增
- 编辑
- 删除
- 复制
- 启用/停用
- 获取模型
- 单 Provider 测活
- 复制 Codex/Hermes 配置
- 同步到 Codex/Hermes

### 5.2 UA/Header 预设

内置少量高频预设：

- 默认
- 空 UA
- Curl UA
- Chrome UA
- Codex UA
- 自定义

这个功能直接服务公益服兼容性，是高价值功能。

### 5.3 Provider 测活

支持：

- 单 Provider 测活
- 全部启用 Provider 测活
- 单模型测活
- Chat endpoint 测试
- Responses endpoint 测试
- reasoning 测试
- UA 矩阵测试

测活结果要直接可读：

```text
可用
失败
超时
限流
Key 无效
UA/WAF 可能有问题
路径或模型不支持
上游网关错误
```

错误提示要给出修复建议，例如：

```text
403：可能是 UA 或 Originator 不匹配，建议尝试 Codex UA 或空 UA。
404：可能是 endpoint 或模型不支持，建议切换 chat/responses 模式。
429：可能被限流，建议暂时停用或放到低优先级。
401：API Key 可能无效。
```

### 5.4 固定 Provider 本地入口

必须保留：

```text
/{provider}/v1
```

示例：

```text
http://127.0.0.1:18006/provider-a/v1
```

用途：

- 多个 Codex CLI 分别绑定不同公益服
- 明确可控
- 方便调试
- 方便复制给 Hermes/Codex

### 5.5 Codex/Hermes 配置生成与同步

只聚焦 Codex 和 Hermes。

支持：

- 复制配置片段
- 一键同步配置
- 同步前自动备份
- 同步后测试
- 显示当前同步状态
- 保留用户已有自定义配置

不扩展到其他工具，直到 Codex/Hermes 体验足够好。

### 5.6 AIProxy 服务管理

支持：

- 查看服务状态
- 创建服务
- 启动
- 停止
- 重启
- 修改端口
- 检查监听地址
- 通过代理测试

### 5.7 备份恢复

写配置前自动备份。

支持：

- 备份列表
- 恢复备份
- 导入配置
- 导出配置

配置安全是核心体验，不是附属功能。

## 6. 同模型组

### 6.1 定义

模型组是多个 Provider 上同名模型的集合。

示例：

```text
gpt-5.5-pool:
  provider-a / gpt-5.5
  provider-b / gpt-5.5
  provider-c / gpt-5.5
```

模型组入口：

```text
/group/{group}/v1
```

示例：

```text
http://127.0.0.1:18006/group/gpt-5.5-pool/v1
```

### 6.2 只支持同模型

只做同模型组，不做自定义混合组。

允许：

```text
provider-a / gpt-5.5
provider-b / gpt-5.5
provider-c / gpt-5.5
```

不允许：

```text
provider-a / gpt-5.5
provider-c / deepseek-v4-pro
provider-d / glm-5.1
```

### 6.3 不做模型改写

客户端请求：

```json
{
  "model": "gpt-5.5"
}
```

上游仍请求：

```json
{
  "model": "gpt-5.5"
}
```

初期不做：

- 跨模型 fallback
- 模型别名自动改写
- 能力池
- 混合模型组

### 6.4 支持策略

模型组只支持两种策略：

#### fallback

按优先级从小到大尝试。

适合主备模式。

#### round_robin

按顺序轮询。

适合分散请求压力。

暂时不做：

- health_first
- latency_first
- cost_first
- adaptive routing
- 复杂熔断器

## 7. 明确不做

以下功能全部先不做。

### 7.1 产品层面不做

- 登录系统
- 多用户
- 权限体系
- 用户 API Key
- 团队管理
- 公网 SaaS
- 商业 API 网关
- 计费系统
- Token 成本统计
- 价格表
- 发票/充值

### 7.2 工具层面不做

- Claude Code 全量管理
- Claude Desktop 管理
- Gemini CLI 管理
- OpenCode
- OpenClaw
- Cursor
- Continue
- Cline
- MCP 管理
- Skills 管理
- Prompts 管理

### 7.3 路由层面不做

- 全局 `/v1`
- 自定义混合模型组
- 跨模型 fallback
- 自动模型改写
- health_first
- latency_first
- cost_first
- adaptive routing
- 复杂熔断器
- 流式响应中途切换 Provider
- `/profile/{profile}/v1` 代理路径

### 7.4 运维层面不做

- 周期性自动测活
- 复杂 Dashboard 大屏
- 云同步
- 桌面 App
- 系统托盘
- Provider 市场
- 自动注册账号
- 自动签到
- 风控绕过

## 8. 页面结构

### 8.1 Provider 页面

主页面。

功能：

- Provider 列表
- 状态展示
- 最近错误
- 最近检测时间
- 本地 URL 由系统自动生成，供配置生成使用
- 新增/编辑/删除
- 启用/停用
- 获取模型
- 测活
- 自动生成本地 URL（供配置片段/同步使用）
- 生成 Codex 配置
- 生成 Hermes 配置
- 同步到 Codex/Hermes

### 8.2 模型组页面

P1/P1.5 阶段做。

功能：

- 自动发现同模型
- 创建同模型组
- 编辑成员
- 调整优先级
- 设置 `fallback` / `round_robin`
- 测试模型组
- 生成 Codex/Hermes 配置

### 8.3 AIProxy 页面

功能：

- 服务状态
- 监听地址
- 端口
- 启动/停止/重启
- 创建/更新服务
- 通过代理测试

### 8.4 配置与备份

可以独立页面，也可以先融入 Provider/AIProxy 页面。

功能：

- 当前配置路径
- 导入配置
- 导出配置
- 备份列表
- 恢复备份

## 9. 阶段规划

### 9.1 V1：个人公益服管理闭环

目标：

> 我有一个公益服账号，1 分钟内让 Codex/Hermes 用上。

范围：

- Provider 管理
- UA/Header 预设
- 模型管理
- 获取模型
- Provider 测活
- 固定 Provider 入口：`/{provider}/v1`
- Codex/Hermes 配置生成
- Codex/Hermes 一键同步
- 同步前备份
- 同步后测试
- AIProxy 服务管理
- 配置导入导出
- 备份恢复

不做：

- 模型组
- 请求统计
- 全局 `/v1`
- 多用户

### 9.2 V1.5：同模型池

目标：

> 我有多个 gpt-5.5 公益服，可以组合成一个稳定入口。

范围：

- 同模型自动发现
- 同模型组创建
- 模型组入口：`/group/{group}/v1`
- `fallback`
- `round_robin`
- 模型组测活
- 模型组 Codex/Hermes 配置生成
- 模型组 Codex/Hermes 同步

不做：

- 混合模型组
- 模型改写
- health_first
- 全局 `/v1`

### 9.3 V2：排错和稳定性增强

目标：

> Codex/Hermes 失败时，我马上知道是哪一路、为什么、怎么修。

范围：

- 轻量请求日志
- 最近错误聚合
- 健康历史
- 简单失败冷却
- 更好的配置 diff
- 更好的备份恢复体验
- 更明确的错误修复建议

仍不做：

- 成本统计
- 商业网关
- 多用户系统
- 大而全工具管理

## 10. 高价值小功能清单

这些功能优先级高，因为小但体验提升明显。

1. 生成 Codex 配置片段
2. 生成 Hermes 配置片段
3. 一键同步 Codex 并自动备份
4. 一键同步 Hermes 并自动备份
5. 同步后立即测试
6. Header/UA 预设
7. 获取模型后勾选加入
8. 测活错误显示修复建议
9. AIProxy 一键重启

## 11. 当前结论

最终收敛后的产品路线是：

```text
Provider 管理
+ Codex/Hermes 配置
+ 本地 AIProxy
+ 测活诊断
+ 同模型组 fallback/round_robin
```

明确不走：

```text
Octopus 式多人 API 网关
CC Switch 式桌面全家桶
New API / One API 式商业中转平台
```

产品成功标准不是功能多，而是：

> 每做一个功能，日常使用公益服和 Codex/Hermes 的体验都有明显提升。


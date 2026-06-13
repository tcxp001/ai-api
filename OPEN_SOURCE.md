# 开源规则与公开仓库安全边界

本项目计划作为开源项目发布。公开仓库只应包含通用代码、脱敏示例和文档，不应包含任何个人真实 Provider 信息。

## 开源协议

本项目采用 MIT License，见根目录 [`LICENSE`](../LICENSE)。

使用 MIT License 的含义：

- 允许他人使用、复制、修改、分发和二次开发本项目。
- 分发时需要保留版权声明和许可证文本。
- 软件按“原样”提供，不提供担保。

## 第三方项目参考说明

本项目产品讨论阶段参考过 Octopus 与 CC Switch 的产品形态和设计思路，但公开仓库不应直接复制其源码。

如后续引入第三方代码、配置片段或资源，必须先确认其许可证，并在必要时保留版权声明、许可证文本和来源说明。

尤其注意：Octopus 使用 AGPL-3.0，不能把其源码直接复制进本项目后再以 MIT 方式发布。

## 绝不能提交的内容

以下内容不得进入公开仓库：

- 真实 `config.yaml` / `config.json`
- 真实 Provider 名称、域名、Base URL
- API Key、Token、Cookie、账号信息
- `.env` / `.env.*`
- 测活结果、运行状态、请求历史
- `runtime-status.json`
- `runtime-history.json`
- `monitor.json`
- `checkins.json`
- 日志文件
- 本地备份文件
- systemd 本地实例配置

## 公开示例要求

公开示例必须使用脱敏占位值，例如：

```yaml
- name: provider-a
  base_url: https://example-a.invalid/v1
  api_key: ${PROVIDER_A_API_KEY}
```

不要使用真实公益服名称、真实域名或可反推出站点的信息。

## 提交前检查

提交或推送前建议运行：

```bash
rg -n "https?://|API_KEY|TOKEN|SECRET|Bearer|sk-" . \
  --glob '!__pycache__/**' \
  --glob '!*.pyc' \
  --glob '!config.yaml' \
  --glob '!config.json' \
  --glob '!*.bak-*' \
  --glob '!runtime-*.json' \
  --glob '!monitor.json' \
  --glob '!checkins.json'
```

并确认输出中没有真实 Provider 域名、真实 Key 或真实账号信息。

## 使用者责任

本项目只用于管理用户自己已有的合法 API 配置。使用者应自行遵守相关上游服务条款。

项目不提供也不鼓励：

- 自动注册账号
- 自动签到刷额度
- CAPTCHA 绕过
- 风控绕过
- 未授权批量账号操作

# Ai-api 公益服管理

个人本地公益服 Provider 管理与 AIProxy 配置工具。

本项目聚焦个人使用：维护自己手里的多个公益服 API Provider，并通过本地 AIProxy 让 Codex、Hermes 等客户端快速、明确、稳定地使用不同 Provider。

## 项目边界

当前重点只做：

- Provider 维护：名称、上游地址、API Key、模型、Headers、启停等基础配置。
- Provider 测活：在独立测活页检查接口与模型可用性。
- 本地 AIProxy：为不同 Provider 提供稳定的本地代理入口。
- 配置安全：真实配置保存在本地，不提交到公开仓库。

暂不做多人账号体系、计费、商业网关、复杂混合模型组、MCP/Skills/Prompts 管理等能力。

## 文件说明

| 文件 | 说明 |
|---|---|
| `dashboard.py` | 本地 Web 管理台后端 |
| `dashboard.html` | 本地 Web 管理台前端 |
| `proxy.py` | 本地 AIProxy |
| `api.py` | Provider 测活逻辑 |
| `config.example.yaml` | 脱敏示例配置 |
| `docs/OPEN_SOURCE.md` | 开源与公开仓库规则 |

真实配置文件 `config.yaml` 不应提交到公开仓库。

## 安装

```bash
pip install -r requirements.txt
```

## 初始化配置

```bash
cd /mnt/ai-api
cp config.example.yaml config.yaml
```

然后编辑 `config.yaml`，填入自己的 Provider 信息，或通过 Web 管理台维护。

最小示例：

```yaml
- name: provider-a
  base_url: https://example-a.invalid/v1
  api_key: ${PROVIDER_A_API_KEY}
  api_mode: codex_responses
  models:
    gpt-5.5:
      context_length: 400000
      reasoning_effort: high
  headers:
    User-Agent: curl/8.0
  enabled: true
```

## 启动 Web 管理台

```bash
cd /mnt/ai-api
python3 dashboard.py --host 127.0.0.1 --port 18080
```

访问：

```text
http://127.0.0.1:18080
```

如需局域网访问：

```bash
python3 dashboard.py --host 0.0.0.0 --port 18080 --public-host <你的局域网IP>
```

管理台会读取和写入 API Key，建议仅在可信网络中使用。

## 启动 AIProxy

```bash
cd /mnt/ai-api
python3 proxy.py --config config.yaml --listen 127.0.0.1 --port 18006
```

本地 Provider 入口格式：

```text
http://127.0.0.1:18006/{provider}/v1
```

例如：

```text
http://127.0.0.1:18006/provider-a/v1
```

可用于客户端配置 `base_url`，真实上游 Key 和 Headers 由 AIProxy 从本地配置注入。

## systemd

正式部署可使用两个服务：

| 服务 | 作用 |
|---|---|
| `aiproxy.service` | 本地 AIProxy |
| `ai-api-dashboard.service` | Web 管理台 |

常用命令：

```bash
systemctl status aiproxy.service --no-pager
systemctl restart aiproxy.service
systemctl status ai-api-dashboard.service --no-pager
systemctl restart ai-api-dashboard.service
```

路径迁移后请确认 unit 中的 `WorkingDirectory`、`ExecStart`、`--config` 都指向实际项目目录。

## 开源与安全

- 本项目使用 MIT License。
- 公开仓库只保留脱敏示例，不提交真实 Provider 域名、API Key、运行状态和内部工作文档。
- 详细规则见 [`docs/OPEN_SOURCE.md`](docs/OPEN_SOURCE.md)。

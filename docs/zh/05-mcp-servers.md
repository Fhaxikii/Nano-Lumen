# 05 · 接入 MCP server

**这篇讲什么**：MCP 在本项目中的接入方式、配置形状、授权流程，以及我们对它的约束。
**读完你能做什么**：新增一个 MCP server，或修改现有 server 的接入逻辑。
**前置**：`02-architecture.md`、`03-configuration.md`。

> 语言：中文 · [English](../en/05-mcp-servers.md)

---

## MCP 是什么

Model Context Protocol，一个把外部工具暴露给模型的开放协议。
本项目作为客户端接入符合该协议的 server，从而在不改动自身代码的前提下扩展能力。

协议本身不在本文档范围内，可查阅其官方规范。

## 配置

server 列表在 `config/mcp_servers.json`，沿用生态通行的 `mcpServers` 结构。
这意味着从某个 server 的 README 里复制的那段 JSON 通常可以直接粘贴使用。

两种类型：

**本地进程（stdio）**
```json
{
  "type": "stdio",
  "command": "python",
  "args": ["-m", "mcp_server_fetch"],
  "enabled": true
}
```

**远程服务（http）**
```json
{
  "type": "http",
  "url": "https://example.com/mcp",
  "headers": {},
  "enabled": true
}
```

`type` 可以省略，由 `command` 或 `url` 的存在推断。
值中支持 `${VAR}` 形式的环境变量展开。

## 内置的 server

当前内置五个，均标记 `builtin: true`：

| 名称 | 类型 | 用途 |
|---|---|---|
| `fetch` | stdio | 网页抓取，Python 实现，不需要 Node |
| `playwright` | stdio | 浏览器自动化，基于无障碍树与 DOM，需要 Node |
| `playwright-headless` | stdio | 同上，无界面模式 |
| `microsoft-learn` | http | 微软官方文档检索 |
| `context7` | http | 开源库文档检索 |

内置 server 的选择标准见下方「我们对 MCP 的约束」。

## 授权

需要授权的 server 采用"在场授权"：粘贴配置时只做登记，不预先弹出登录。
第一次真正用到它时，才在对话中出现授权卡。

这样做的原因是：用户在粘贴配置的那一刻还没有产生使用意图，
此时要求他登录，授权与用途之间的因果关系是倒置的。

添加流程的实现见 `core/mcp_client.py` 的 `add_server_from_json`。
该方法在授权之前只做三件事：解析 JSON、取出身份、取出它声明的能力。

接入外部能力的授权弹窗不受自动模式影响，即使用户开启了自动模式也会弹出。
原因是这一步引入的是一个此前不存在的外部依赖，属于需要用户明确知情的改变。

## 连接状态

server 可能处于以下状态之一，界面上会如实显示：

已连接、连接中、连接失败、需要登录、已禁用、未连接、正在重连。

需要区分「按需不连」和「连不上」：前者是正常状态，后者是故障。
把两者都显示为「未连接」会导致用户去修一个没有坏的东西。

401 与 403 类错误会被判定为需要授权，此时停止自动重连，
避免对服务端持续发起注定失败的请求。判定逻辑见 `core/mcp_client.py`。

## 我们对 MCP 的约束

新增内置 server 需要同时满足：

- 免费。
- 不需要注册账号。
- 不需要 API key。

不满足的 server 可以由用户自行添加，但不会内置。
这一条同样适用于 PR，详见 `11-contributing.md`。

## 新增一个内置 server

1. 确认它满足上述三条约束。
2. 在 `config/mcp_servers.json` 中添加条目，标记 `builtin: true`。
3. 如果它提供某类通用能力，填写 `provides` 字段，供能力发现使用。
4. 如果它依赖 Node 或其他运行时，在 `01-getting-started.md` 中补充说明。

---

## 怎么验证你改对了

1. 启动后在「设置 → MCP 连接」中确认该 server 出现，状态正确。
2. 触发一次它提供的工具调用，工具卡中结果符合预期。
3. 故意写错 `command` 或 `url`，确认状态显示为「连接失败」而不是「未连接」。
4. 对需要授权的 server，确认粘贴配置时不弹登录，首次使用时才弹。
5. 运行 `bash run_tests.sh`。

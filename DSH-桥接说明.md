# DSH 远程操控（QQ 机器人插件）

当前网络出口使用本机代理 `127.0.0.1:10808`（当前节点 `LA-Hysteria2-Fast`），主聊天与 Gemini 识图已切换为 `gemini-4-hydrogen`，缓存策略及验证结果见 [NETWORK.md](NETWORK.md)。

通过 QQ 远程驱动一个独立的 **DSH（DeepSeek Harness）** 工作会话：

- 新建 / 列出 / 切换 / 关闭 **会话** 与 **工作区**
- 发送指令、接收**流式输出**
- **审批** DSH 的授权请求（同意 / 拒绝）
- **回答** DSH 的选项提问（`ask_user_question`）
- 切换模型、取消任务

底层走 DSH 官方的 **ACP**（Agent Client Protocol）接口（`dsh --profile acp`，
JSON-RPC over stdio）；ACP 本身不支持 elicitation，由一个配套的 **DSH 插件**
`dsh-qq-bridge` 补齐。

## 架构

```
QQ 用户 ⇄ NapCat(OneBot) ⇄ NoneBot2 插件 ⇄ ACP 客户端 ⇄ dsh --profile acp ⇄ DSH agent
                            (本项目)        (JSON-RPC)         │
                                                               │ ask_user_question
                                                               ▼
                                          dsh-qq-bridge(DSH 插件, userQuestions answerer)
                                                               │ HTTP POST /ask
                                                               ▼
                                          本项目内置的 asyncio HTTP 端点(127.0.0.1:8090)
                                                               │
                                                               ▼
                                                        转发到 QQ 等回答
```

## 目录结构

```
C:\path\to\workspace\
├── qqbot\                          NoneBot2 机器人
│   ├── bot.py                      入口
│   ├── .env                        配置
│   ├── 一键启动.cmd               一键启动（纯 ASCII）
│   └── plugins\
│       ├── echo.py                 示例插件
│       └── dsh_bridge\             ★ DSH 远程操控插件
│           ├── __init__.py         QQ 命令层 + 生命周期
│           ├── acp_client.py       ACP 客户端（JSON-RPC over stdio）
│           ├── bridge.py           会话绑定 / 审批等待 / 提问等待 / 输出节流
│           └── ask_server.py       提问接收端点（asyncio HTTP）
└── dsh-qq-bridge\                  ★ DSH 侧插件（userQuestions answerer）
    ├── package.json                dsh.bundle.patch 声明
    ├── cordis.patch.yml            挂载 answerer
    └── lib\index.js                监听 user-questions/request 转发到 QQ
```

## 配置（`qqbot\.env`）

```ini
# ---- OneBot 连接 ----
DRIVER=~websockets
ONEBOT_ACCESS_TOKEN=<与 NapCat 的 Token 一致>
ONEBOT_WS_URLS=["ws://127.0.0.1:3001"]

# ---- DSH 桥接 ----
DSH_BRIDGE_ENABLED=1
DSH_BRIDGE_NODE=C:\path\to\node.exe
DSH_BRIDGE_REPO=C:\path\to\DeepSeek-Harness
DSH_BRIDGE_HOME=C:\path\to\workspace\dsh-acp-home        # 需含凭据的 DSH_HOME
DSH_BRIDGE_WORKSPACE=C:\path\to\workspace
DSH_BRIDGE_PROFILE=acp
DSH_BRIDGE_ALLOWLIST=123456789              # 允许的 QQ 号（逗号分隔）
DSH_BRIDGE_ASK_HOST=127.0.0.1                # 可选
DSH_BRIDGE_ASK_PORT=8090                     # 可选
```

> **安全**：`DSH_BRIDGE_ALLOWLIST` 为空 = 不限制，任何人都能让 DSH 执行任意代码，
> 生产环境务必填写。

## QQ 命令

| 命令 | 作用 |
|---|---|
| `/dsh` | 查看状态与帮助 |
| `/dsh new [工作区]` | 新建会话 |
| `/dsh ls [工作区]` | 列出可恢复会话 |
| `/dsh use <会话号> [工作区]` | 切换会话 |
| `/dsh close` | 关闭当前会话 |
| `/dsh model <名称>` | 切换模型 |
| `/dsh cancel` | 取消当前任务 |
| `/dsh yes` / `/dsh no` | 同意 / 拒绝授权请求 |
| `/dsh ans <答案>` | 回答选项提问（选项号，多个用逗号分隔） |
| `/dsh run <内容>` | 向 DSH 发送指令 |

## 插件热重载（改代码不用重启）

由 `plugins/hot_reload.py` 提供，**进程不重启、QQ 连接不断**：

| 命令 | 作用 |
|---|---|
| `/plugins` | **插件总览**（已加载 / 目录存在未加载 / 已禁用 / 配置声明） |
| `/reload` | 重载**所有**可热重载的插件 |
| `/reload <插件名>` | 只重载指定插件 |
| `/unload <插件名>` | **卸载**插件（立即失效，**文件保留**，重启后恢复） |

`/plugins` 的输出长这样：

```
📦 已加载（4 个）
  ⏭️ dsh_bridge
  ✅ echo
  ✅ hot_reload
  ✅ random_tools

🚫 已禁用（`_` 前缀，1 个）
  _old_experiment

⚙️ 配置（pyproject.toml）
  扫描目录：plugins
  商店插件：（无）
  内置插件：（无）
```

- **已加载**：运行时真正生效的
- **目录存在但未加载**：被 `/unload` 卸过、或加载失败的（会提示怎么恢复）
- **已禁用**：文件名以 `_` 开头的（NoneBot 官方规则：不加载）
- **配置**：`pyproject.toml` 里声明的（含 `nb plugin install` 装的商店插件）

**完整的"卸载"有三种方式**：

| 方式 | 操作 | 特点 |
|---|---|---|
| **删文件** | 删除 `plugins/xxx.py` | 永久卸载，重启后不再加载 |
| **改名** | `xxx.py` → `_xxx.py` | **临时禁用**（NoneBot 不加载 `_` 开头的插件），改回即恢复 |
| **`/unload`** | QQ 里发命令 | 运行时卸载，**不重启不删文件**，重启后自动恢复 |

**不能热卸载的插件**：`dsh_bridge`（依赖 `@driver.on_startup` 启动钩子，热卸载后会半残）。名单可用 `.env` 的 `QQBOT_RELOAD_SKIP` 调整。

> 💡 重载时会看到 `Duplicated prefix rule "/xxx"` 警告，这是**无害的**（命令前缀树不去重，但 matcher 已销毁，旧命令不会响应）。

> 💡 **私聊发图片/文件给机器人**：会自动保存到当前工作区的 `.qq-inbox/`。
> **群聊里发的一律不收**（静默忽略），避免把群里的文件存到本机。
>
> 💡 **戳一戳提醒**：在三个重要节点会自动「戳一戳」你（QQ 的提醒功能）——
> **需要你同意**（授权请求）、**需要你选择**（选项提问）、**任务完成**时。

## 文件 / 图片外发

除了文字，DSH 产生的**图片和文件也能发给用户**。做法是「**outbox 目录约定**」：

- agent 只要把文件放进 **`<会话工作区>/.qq-outbox/`**，桥接就会在**几秒内**捕获并通过 QQ 发出：
  - `.png .jpg .jpeg .gif .webp .bmp` → 按**图片消息**发送（QQ 内直接预览）
  - 其它类型 → 走 OneBot 的**文件上传**（`upload_group_file` / `upload_private_file`）
- 发送成功后文件会被归档到 `.qq-outbox/.sent/`，不会重复发送
- 失败的文件会被标记，避免反复刷屏；重新放入即可重试

这条约定已写进 acp profile 的 `system-prompt`（见
`<DSH_HOME>\profiles\acp\cordis.patch.yml`），所以 agent 知道该这么做。

### 自动判断（不需要你说「发给我」）

system-prompt 里定义了明确的判断规则，agent 会**主动**从你的话里识别索取意图：

| 你可能的说法 | agent 的行为 |
|---|---|
| 「发给我」「传过来」「导出」「下载」「打包发我」 | 直接发 |
| 「那个图呢」「报告发我」「让我看看效果」「刚才那个文件」 | 直接发 |
| 你刚让它做的成品（图片/报表/导出文档）**即使你没再提要发** | 主动发 |
| 「文件在 D:\... 你自己看」 | ❌ 禁止这样回 |
| 把文件内容粘贴进回复冒充发送 | ❌ 禁止 |

同时提示里明确告诉 agent：**文件放进 outbox 后会被自动取走（移到 `.sent/`），这是
发送成功的标志，不是写入失败**——避免它误以为出错而反复重写、重复发送。

> 为什么用目录约定而不是新增工具：DSH 的插件若要 `import` 内部包
> （`@deepseek-ai/dsh-tools` 等），需要在插件目录里建立指向仓库源码包的
> Junction，跨环境部署很脆弱；outbox 约定不依赖任何内部导入，且对
> 「agent 用现有工具产出的文件」天然适用。

## 接收你发来的图片 / 文件

**默认只在私聊接收**：给机器人发私聊，把图片或文件发过去，它会自动存下来。

> ⚠️ **群聊里的附件一律不收**（静默忽略，不保存也不回复）。
> 原因：群里任何人都可能发东西，存到本机既不安全也容易刷屏；而且回复里带本地路径会暴露你的电脑目录。

- 保存位置：**当前会话工作区**下的 `.qq-inbox/`
- 支持的来源（按优先级）：
  1. **本地路径**（NapCat 与机器人同机时最常见）——直接复制
  2. **URL 下载**——附件只给了 http 链接时
- **后缀名按文件头判断**，不信任 URL 后缀（QQ 常给不出正确扩展名，会把 PNG 存成 `.bin`）
- 同名文件不会被覆盖，会加时间戳前缀
- 权限沿用 `DSH_BRIDGE_ALLOWLIST`（不在名单里的用户发的附件不会被保存）
- 保存后机器人回复文件名和目录，你接着说「把 `.qq-inbox` 里的图转成 PNG」即可让 DSH 处理

控制策略（写在 `.env`）：

```ini
DSH_BRIDGE_ACCEPT_FILES=private   # private(默认) / none / all
```

| 值 | 行为 |
|---|---|
| `private` | **只在私聊接收**（默认，推荐） |
| `none` | 完全不接收 |
| `all` | 私聊和群聊都接收（**不建议**） |

## 戳一戳提醒

在三个**需要你注意**的节点，机器人会顺手「戳一戳」你（QQ 的提醒功能）：

| 节点 | 触发时机 |
|---|---|
| **同意** | DSH 请求授权时（要你回 `/dsh yes` / `/dsh no`） |
| **选择** | DSH 提出选项问题时（要你回 `/dsh ans`） |
| **完成** | 一轮任务结束时 |

- 群聊用 `send_poke`，私聊优先 `friend_poke`、失败回退 `send_poke`
- 戳一戳**失败不影响主流程**（只记日志），因为不同 OneBot 实现支持度不一

## 会话记忆与自动恢复

机器人重启后**不需要重新 `/dsh new` 或 `/dsh use`**：每个用户的「当前会话 + 工作区」
会落盘，启动时自动读回，下次发指令时自动 `resume` 该会话。

- 状态文件：默认 `qqbot/state/dsh_sessions.json`（可用 `DSH_BRIDGE_STATE` 覆盖）
- 写入时机：`/dsh new`、`/dsh use`、`/dsh close` 时
- 首次发指令时若会话尚未激活，会先 `resume`，并回一条 `♻️ 已自动恢复上次的会话`
- 若该会话已无法恢复（被删除等），自动清掉绑定并新建，同时告知用户
- 写盘是「临时文件 + 替换」的原子操作，中断也不会留下半个文件
- 若默认位置不可写（受限环境），自动降级到机器人目录或临时目录

`/dsh` 会显示 `状态：🕓 待恢复（下次发指令时自动 resume）`。

## 部署步骤

### 1. 机器人侧（每次改配置后）

1. 先连接 **v2rayN**（固定代理 `127.0.0.1:10808`）
2. 双击 `qqbot\一键启动.cmd`
3. 看到这两行即成功：
   ```
   dsh_bridge: DSH ACP 已就绪
   dsh_bridge: 提问端点 http://127.0.0.1:8090/ask
   ```

### 2. DSH 插件侧（一次性）

把 `dsh-qq-bridge` 装进 acp profile：

```powershell
$env:DSH_HOME = "C:\path\to\workspace\dsh-acp-home"
node C:\path\to\DeepSeek-Harness\apps\cli\lib\bin.js plugin --profile acp add file:D:/dshex/dsh-qq-bridge
```

若 `ask_user_question` 工具在该 profile 里未挂载，再往
`<DSH_HOME>\profiles\acp\cordis.patch.yml` 追加：

```yaml
- insert:
    - id: tool-ask-user
      name: '@deepseek-ai/dsh-tool-ask-user'
```

### 3. 验证

```powershell
$env:DSH_HOME = "C:\path\to\workspace\dsh-acp-home"
node C:\path\to\DeepSeek-Harness\apps\cli\lib\bin.js --profile acp --dump-config
```

应能看到 `dsh-qq-bridge` 与 `tool-ask-user` 两行。

## 注意事项

- ACP 每个会话同一时刻只允许一个进行中的 prompt。
- 审批请求只带 `toolCallId`，工具名由插件从先前的 `tool_call` 更新中关联。
- 提问/审批等待超时分别为 600s / 300s，超时会安全取消。
- `~websockets` 驱动不支持 HTTP server，因此提问端点由插件自建（独立端口 8090），
  与 OneBot 的 WS 连接互不影响。


回复性能与 QQ 自动重登：见 [RECOVERY.md](RECOVERY.md)。


启动入口已统一：双击 `一键启动.cmd`；查看状态、重启、登录和维护请用 `管理工具.cmd`。见 [使用说明.md](使用说明.md)。旧批处理已归档到 `backups`，不再作为启动入口。

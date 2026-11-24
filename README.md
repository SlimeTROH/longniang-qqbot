# 龙娘 QQBot

基于 NoneBot2 和 OneBot v11 的 QQ 机器人。包含龙娘聊天、图像工具、做题模式、好感度与记忆，以及「无尽电梯」搜打撤游戏。电梯的 QQ 群和网页版共享游戏存档。

## 启动

使用 Python 3.12。在本机启动 OneBot v11 正向 WebSocket，然后在仓库根目录执行以下命令。下面以 Windows PowerShell 为例。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . httpx "websockets>=15" python-dotenv qrcode google-auth requests
Copy-Item .env.example .env
```

在 `.env` 中填写 OneBot 地址和 Token、机器人 QQ 号、允许的群号与管理员 QQ。使用 OpenAI 聊天时填写自己的 `CHAT_OPENAI_API_KEY`。完成配置后运行 `.\.venv\Scripts\python.exe bot.py`。

## 无尽电梯

QQ 群内发「电梯」进入大厅。网页版可在本机试跑，打开 [localhost 上的无尽电梯](http://127.0.0.1:8092/elevator/)。

```powershell
.\.venv\Scripts\python.exe elevator_web.py --local-http
```

正式站点的反向代理、群验证和游戏接口见 [网页版部署与接口](无尽电梯网页版-部署与接口.md)。可选的桌面代理桥接见 [DSH 桥接说明](DSH-桥接说明.md)。

本仓库保存代码、配置模板和资源。迁移现有玩家时，游戏及网页登录的 SQLite 存档需要从原部署单独恢复。

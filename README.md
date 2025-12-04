## Telegram SMS Relay Bot

Python 实现的 Telegram 机器人，会定时访问 `http://sms.szfangmm.com:3000/api/smslist` 接口，识别短信内容中的关键词后，转发到指定的 Telegram 群组 / 频道。

### 功能

- 通过自定义键盘菜单配置
  - 多个短信 `token`（可切换生效的 token）
  - 目标群组 / 频道 chat ID
  - 关键词（匹配到才转发，留空则全部转发）
- 后台定时轮询接口（默认 5 秒一次，避免重复转发）
- 自动记住最近一次短信 ID，防止重复推送

### 运行方式

1. 安装依赖

   ```bash
   cd /Users/wanghaixin/Development/telegramBotWork/nuomijiema
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. 复制并编辑 `.env`

   ```bash
   cp env.template .env
   # 打开 .env 填写 TELEGRAM_BOT_TOKEN=xxxx:yyyy
   # 可选：填写 ADMIN_USER_ID=你的 Telegram 数字 user id（或 ADMIN_USER_IDS=id1,id2）
   ```

3. 启动机器人

   ```bash
   python bot.py
   ```

4. 在 Telegram 中与机器人对话，使用菜单按钮配置短信 token、关键词、目标群组（可输入 chat id 或 @群名）等信息，然后点击“开始转发”即可。

### 其他

- 配置保存在 `config.json`，如需重置可以删除该文件重新配置。
- `.env` 仅存放本地敏感信息，请勿提交到版本库。
- 目标 chat id 获取方式：把机器人加入目标群组并发送任意消息，然后访问 `https://api.telegram.org/bot<token>/getUpdates` 查到 `chat.id`，或使用 @RawDataBot。
- 机器人需要持续运行才能转发，可以托管在服务器并使用 `pm2` / `systemd` 等守护。

# nuomi

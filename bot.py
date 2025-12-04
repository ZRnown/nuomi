import asyncio
import json
import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import urllib3
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from urllib3.util import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s", level=logging.WARNING
)
LOG = logging.getLogger("sms-bot")
LOG.setLevel(logging.INFO)
for noisy in ("httpx", "telegram.ext.application", "apscheduler"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

SMS_API_BASE = "http://sms.szfangmm.com:3000/api/smslist"
CONFIG_PATH = Path("config.json")
POLL_JOB_NAME = "sms_poll_job"

_admin_raw = os.getenv("ADMIN_USER_IDS") or os.getenv("ADMIN_USER_ID") or ""
ADMIN_USER_IDS = {
    int(part.strip())
    for part in _admin_raw.split(",")
    if part.strip().isdigit()
}

MAIN_MENU = [
    ["➕ 添加短信 Token", "🔄 切换短信 Token"],
    ["🗑 删除短信 Token"],
    ["🎯 设置目标群组", "🔑 设置关键词"],
    ["▶️ 开始转发", "⏹ 停止转发"],
    ["ℹ️ 查看配置"],
]

RETURN_MENU = [["⬅️ 返回主菜单"]]


@dataclass
class BotConfig:
    sms_tokens: List[str] = field(default_factory=list)
    active_sms_token: Optional[str] = None
    target_chat_id: Optional[int] = None
    keywords: List[str] = field(default_factory=list)
    last_seen_id: Optional[int] = None
    poll_interval: int = 5
    forwarding_enabled: bool = False

    @classmethod
    def load(cls, path: Path) -> "BotConfig":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls(**data)
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("配置文件损坏，使用默认配置: %s", exc)
        return cls()

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")


class BotState:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.config = BotConfig.load(config_path)
        self.lock = asyncio.Lock()

    async def update(self, **kwargs: Any) -> None:
        async with self.lock:
            for key, value in kwargs.items():
                setattr(self.config, key, value)
            self.config.save(self.config_path)

    async def read(self) -> BotConfig:
        async with self.lock:
            return BotConfig(**asdict(self.config))


def build_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-HK,zh-CN;q=0.9,zh;q=0.8,en-US;q=0.7,en;q=0.6",
        "Connection": "keep-alive",
        "If-None-Match": '"xvuhd1kkf0c4"',
        "Referer": "http://sms.szfangmm.com:3000/cYxPNDG8ePDviFN6exuS8L",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
    }


def create_http_session() -> requests.Session:
    session = requests.Session()
    # 忽略系统代理，避免被本地 127.0.0.1:7897 之类的代理影响
    session.trust_env = False
    retry = Retry(
        total=1,
        backoff_factor=0.1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


HTTP_SESSION = create_http_session()


def is_authorized(update: Update) -> bool:
    if not ADMIN_USER_IDS:
        return True
    user = update.effective_user
    if not user:
        return False
    return user.id in ADMIN_USER_IDS


def fetch_sms(sms_token: str) -> List[Dict[str, Any]]:
    url = f"{SMS_API_BASE}?token={sms_token}"
    response = HTTP_SESSION.get(url, headers=build_headers(), timeout=5, verify=False)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        raise ValueError("API 响应不是列表")
    return data


async def poll_sms(context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["bot_state"]
    config = await state.read()

    if not (config.forwarding_enabled and config.active_sms_token and config.target_chat_id):
        return

    try:
        messages = await asyncio.to_thread(fetch_sms, config.active_sms_token)
    except Exception as exc:
        LOG.warning("获取短信失败: %s", exc)
        return

    if not messages:
        return

    messages.sort(key=lambda item: item.get("id", 0))
    new_messages = []
    for msg in messages:
        msg_id = msg.get("id")
        if msg_id is None:
            continue
        if config.last_seen_id is None or msg_id > config.last_seen_id:
            new_messages.append(msg)

    if not new_messages:
        return

    keywords = [kw.lower() for kw in config.keywords]
    last_seen = config.last_seen_id or 0

    for msg in new_messages:
        last_seen = max(last_seen, msg.get("id", last_seen))
        content = msg.get("content", "")
        if keywords and not any(kw in content.lower() for kw in keywords):
            continue

        text = (
            f"📲 *收到短信*\n"
            f"ID: `{msg.get('id')}`\n"
            f"号码: {msg.get('number')}\n"
            f"接收号码: {msg.get('simnum')}\n"
            f"时间: {msg.get('time')}\n"
            f"内容: {content}"
        )
        try:
            await context.bot.send_message(
                chat_id=config.target_chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
            LOG.info("转发短信到 %s：%s", config.target_chat_id, content)
        except Exception as exc:
            LOG.error("发送到 Telegram 失败: %s", exc)

    await state.update(last_seen_id=last_seen)


def main_menu_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(MAIN_MENU, resize_keyboard=True)


def return_menu_markup() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(RETURN_MENU, resize_keyboard=True)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_authorized(update):
        chat = update.effective_chat
        if chat:
            await chat.send_message("无权限使用此机器人。")
        return
    context.user_data.clear()
    text = (
        "欢迎使用短信转发机器人 ✉️\n"
        "使用下面的键盘按钮完成所有设置，然后点击“开始转发”。"
    )
    await update.message.reply_text(text, reply_markup=main_menu_markup())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    if not is_authorized(update):
        chat = update.effective_chat
        if chat:
            await chat.send_message("无权限使用此机器人。")
        return

    text = update.message.text.strip()
    pending = context.user_data.get("mode")

    if pending:
        await handle_pending_input(update, context, pending, text)
        return

    handlers = {
        "➕ 添加短信 Token": ask_sms_token,
        "🔄 切换短信 Token": choose_sms_token,
        "🗑 删除短信 Token": delete_sms_token,
        "🎯 设置目标群组": ask_chat_id,
        "🔑 设置关键词": ask_keywords,
        "▶️ 开始转发": start_forwarding,
        "⏹ 停止转发": stop_forwarding,
        "ℹ️ 查看配置": show_config,
        "⬅️ 返回主菜单": back_to_menu,
    }

    handler = handlers.get(text)
    if handler:
        await handler(update, context)
    else:
        await update.message.reply_text(
            "请使用键盘中的按钮进行操作。", reply_markup=main_menu_markup()
        )


async def resolve_chat_id(bot, value: str) -> int:
    value = value.strip()
    if value.startswith("@"):
        chat = await bot.get_chat(value)
        return chat.id
    return int(value)


async def handle_pending_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str, text: str
) -> None:
    state: BotState = context.application.bot_data["bot_state"]

    if mode == "add_sms_token":
        token = text.strip()
        if not token:
            await update.message.reply_text("Token 不能为空，请重新输入。")
            return
        config = await state.read()
        tokens = config.sms_tokens
        if token not in tokens:
            tokens.append(token)
        await state.update(sms_tokens=tokens, active_sms_token=token)
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            f"已添加并启用短信 token：{token}", reply_markup=main_menu_markup()
        )
        return

    if mode == "set_chat_id":
        try:
            chat_id = await resolve_chat_id(context.bot, text)
        except ValueError:
            await update.message.reply_text("请输入 chat id 或 @群组用户名。")
            return
        except Exception as exc:
            await update.message.reply_text(f"解析群组失败：{exc}")
            return
        await state.update(target_chat_id=chat_id)
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            f"目标 chat id 已设置为：{chat_id}", reply_markup=main_menu_markup()
        )
        return

    if mode == "set_keywords":
        normalized = text.replace("，", ",")
        keywords = [part.strip() for part in normalized.split(",") if part.strip()]
        await state.update(keywords=keywords)
        context.user_data.pop("mode", None)
        if keywords:
            await update.message.reply_text(
                f"关键词已更新：{', '.join(keywords)}", reply_markup=main_menu_markup()
            )
        else:
            await update.message.reply_text(
                "关键词列表已清空（将转发全部短信）。", reply_markup=main_menu_markup()
            )
        return

    if mode == "select_sms_token":
        if text == "⬅️ 返回主菜单":
            context.user_data.pop("mode", None)
            await update.message.reply_text("已取消选择。", reply_markup=main_menu_markup())
            return
        config = await state.read()
        if text not in config.sms_tokens:
            await update.message.reply_text("无效的 token，请重新选择。")
            return
        await state.update(active_sms_token=text)
        context.user_data.pop("mode", None)
        await update.message.reply_text(
            f"已切换短信 token：{text}", reply_markup=main_menu_markup()
        )
        return

    if mode == "delete_sms_token":
        if text == "⬅️ 返回主菜单":
            context.user_data.pop("mode", None)
            await update.message.reply_text("已取消删除。", reply_markup=main_menu_markup())
            return
        state_config = await state.read()
        tokens = state_config.sms_tokens
        if text not in tokens:
            await update.message.reply_text("无效的 token，请重新选择。")
            return
        tokens = [t for t in tokens if t != text]
        active = state_config.active_sms_token
        if active == text:
            active = tokens[0] if tokens else None
        await state.update(sms_tokens=tokens, active_sms_token=active)
        context.user_data.pop("mode", None)
        msg = f"已删除短信 token：{text}"
        if active:
            msg += f"\n当前启用 token：{active}"
        else:
            msg += "\n当前没有启用的 token。"
        await update.message.reply_text(msg, reply_markup=main_menu_markup())
        return

    await update.message.reply_text("状态异常，已返回主菜单。", reply_markup=main_menu_markup())
    context.user_data.pop("mode", None)


async def ask_sms_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "add_sms_token"
    await update.message.reply_text(
        "请发送新的短信 token：", reply_markup=ReplyKeyboardRemove()
    )


async def choose_sms_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["bot_state"]
    config = await state.read()
    if not config.sms_tokens:
        await update.message.reply_text("还没有 token，请先添加。", reply_markup=main_menu_markup())
        return

    buttons: List[List[str]] = [config.sms_tokens[i : i + 2] for i in range(0, len(config.sms_tokens), 2)]
    buttons.append(["⬅️ 返回主菜单"])
    context.user_data["mode"] = "select_sms_token"
    await update.message.reply_text(
        "请选择要启用的短信 token：", reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )


async def delete_sms_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["bot_state"]
    config = await state.read()
    if not config.sms_tokens:
        await update.message.reply_text("当前没有可删除的 token。", reply_markup=main_menu_markup())
        return

    buttons: List[List[str]] = [config.sms_tokens[i : i + 2] for i in range(0, len(config.sms_tokens), 2)]
    buttons.append(["⬅️ 返回主菜单"])
    context.user_data["mode"] = "delete_sms_token"
    await update.message.reply_text(
        "请选择要删除的短信 token：", reply_markup=ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    )


async def ask_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "set_chat_id"
    await update.message.reply_text(
        "请输入目标群组/频道 chat id 或 @群组用户名：", reply_markup=ReplyKeyboardRemove()
    )


async def ask_keywords(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["mode"] = "set_keywords"
    await update.message.reply_text(
        "请输入关键词，多个用逗号分隔（留空表示转发全部）：",
        reply_markup=ReplyKeyboardRemove(),
    )


async def start_forwarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["bot_state"]
    config = await state.read()
    if not config.active_sms_token:
        await update.message.reply_text("请先配置短信 token。", reply_markup=main_menu_markup())
        return
    if not config.target_chat_id:
        await update.message.reply_text("请先设置目标 chat id。", reply_markup=main_menu_markup())
        return

    job_queue = context.application.job_queue
    if job_queue is None:
        await update.message.reply_text(
            "当前环境未启用定时任务模块，请先运行：\n"
            "`pip install \"python-telegram-bot[job-queue]\"`\n"
            "然后重启机器人。",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=main_menu_markup(),
        )
        return

    existing = job_queue.get_jobs_by_name(POLL_JOB_NAME)
    if not existing:
        job_queue.run_repeating(
            poll_sms,
            interval=config.poll_interval,
            first=0,
            name=POLL_JOB_NAME,
        )

    await state.update(forwarding_enabled=True)
    await update.message.reply_text("已开始转发。", reply_markup=main_menu_markup())


async def stop_forwarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    job_queue = context.application.job_queue
    for job in job_queue.get_jobs_by_name(POLL_JOB_NAME):
        job.schedule_removal()

    state: BotState = context.application.bot_data["bot_state"]
    await state.update(forwarding_enabled=False)
    await update.message.reply_text("已停止转发。", reply_markup=main_menu_markup())


async def show_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state: BotState = context.application.bot_data["bot_state"]
    config = await state.read()
    text = (
        "当前配置：\n"
        f"- Token 数量：{len(config.sms_tokens)}\n"
        f"- 正在使用 Token：{config.active_sms_token or '未设置'}\n"
        f"- 目标 chat id：{config.target_chat_id or '未设置'}\n"
        f"- 关键词：{', '.join(config.keywords) if config.keywords else '未设置'}\n"
        f"- 转发状态：{'进行中' if config.forwarding_enabled else '已停止'}"
    )
    await update.message.reply_text(text, reply_markup=main_menu_markup())


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("mode", None)
    await update.message.reply_text("已返回主菜单。", reply_markup=main_menu_markup())


def ensure_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("请先在环境变量 TELEGRAM_BOT_TOKEN 中设置机器人 Token")
    return token


def build_application() -> Application:
    token = ensure_token()
    application = ApplicationBuilder().token(token).build()
    application.bot_data["bot_state"] = BotState(CONFIG_PATH)

    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(CommandHandler("menu", handle_start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return application


def run() -> None:
    application = build_application()
    LOG.info("机器人已启动，等待 Telegram 事件...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run()


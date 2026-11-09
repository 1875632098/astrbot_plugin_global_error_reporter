from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import queue
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.core.message.message_event_result import MessageChain

from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig


class _ErrorHandler(logging.Handler):
    def __init__(self, plugin):
        # 黄色 WARNING、红色 ERROR/CRITICAL 全部进入监控。
        super().__init__(level=logging.WARNING)
        self.plugin = plugin

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = str(getattr(record, "name", "") or "")
            if "global_error_reporter" in name:
                return
            self.plugin.enqueue(
                source="日志",
                module=record.name,
                function=getattr(record, "funcName", ""),
                error=self.format(record),
                level=record.levelname,
                path=getattr(record, "pathname", ""),
            )
        except Exception:
            # 错误监控器自身绝不能再次制造错误日志循环。
            pass


class GlobalErrorReporter(Star):
    """全局错误汇总器：尽量捕获日志、未捕获异常和线程异常。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.notify_group = str(self.config.get("notify_group", "") or "").strip()
        self.dedupe_seconds = int(self.config.get("dedupe_seconds", 300) or 300)
        self.max_length = int(self.config.get("max_message_length", 3000) or 3000)
        self._queue: deque[dict] = deque(maxlen=100)
        self._seen: dict[str, float] = {}
        self._send_lock = asyncio.Lock()
        self._old_excepthook = sys.excepthook
        self._old_thread_hook = getattr(threading, "excepthook", None)
        self._handler = _ErrorHandler(self)
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        # 捕获 astrbot、root 和常见 uvicorn 日志，避免重复安装。
        self._log_names = ("", "astrbot", "astrbot.api", "astrbot.core", "uvicorn")
        for log in (logging.getLogger(n) for n in self._log_names):
            log.addHandler(self._handler)
        self._attach_all()
        # 自检：挂载成功后发一条警告，确认监控链路通不通
        logger.info(
            "【监控自检】错误监控已挂载，警告级及以上日志都会转发到通知群"
        )
        sys.excepthook = self._excepthook
        if self._old_thread_hook:
            threading.excepthook = self._thread_excepthook

    async def initialize(self):
        """后台轮询发送队列：不依赖是否有新消息进来。"""
        try:
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("[监控] 后台发送任务已启动")
        except Exception as exc:
            logger.warning(f"[监控] 后台任务启动失败: {exc}")

    def _attach_all(self) -> int:
        """把监控 handler 挂到所有 logger（插件 logger 默认 propagate=False，挂根节点收不到）。"""
        count = 0
        try:
            for name, obj in list(logging.Logger.manager.loggerDict.items()):
                if not isinstance(obj, logging.Logger):
                    continue
                if "global_error_reporter" in str(name):
                    continue
                if self._handler in obj.handlers:
                    continue
                obj.addHandler(self._handler)
                count += 1
        except Exception:
            pass
        return count

    async def _worker(self):
        tick = 0
        while True:
            try:
                await asyncio.sleep(2)
                tick += 1
                if tick % 8 == 0:
                    self._attach_all()
                await self._drain()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[监控] 发送循环异常: {exc}")

    def _drain_now(self):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._drain())
        except Exception:
            pass

    async def _drain(self):
        if not self._queue:
            return
        if not self.notify_group.isdigit():
            return
        group_id = int(self.notify_group)
        async with self._send_lock:
            while self._queue:
                item = self._queue.popleft()
                text = self._format(item)
                sent = False
                bot = getattr(self, "_bot", None)
                if bot is not None:
                    try:
                        await bot.send_group_msg(group_id=group_id, message=text)
                        sent = True
                    except Exception as exc:
                        logger.warning(f"[监控] 群发送失败，放回队列: {exc}")
                if not sent:
                    umo = str(getattr(self, "_umo", "") or "")
                    if f"GroupMessage:{group_id}" not in umo:
                        # 没有可用的群通道，也不往别的会话乱发
                        self._queue.appendleft(item)
                        break
                    try:
                        await self.context.send_message(
                            self._umo, MessageChain([Plain(text)])
                        )
                    except Exception as exc:
                        logger.warning(f"[监控] 发送失败，放回队列: {exc}")
                        self._queue.appendleft(item)
                        break

    def _excepthook(self, exc_type, exc_value, exc_tb):
        try:
            self.enqueue("未捕获异常", "主线程", "", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        finally:
            if self._old_excepthook:
                self._old_excepthook(exc_type, exc_value, exc_tb)

    def _thread_excepthook(self, args):
        try:
            self.enqueue("线程异常", getattr(args.thread, "name", "线程"), "", "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        finally:
            if self._old_thread_hook:
                self._old_thread_hook(args)

    @staticmethod
    def _plugin_name(module: str, path: str) -> str:
        text = f"{module} {path}"
        parts = Path(path).parts
        for i, part in enumerate(parts):
            if part == "plugins" and i + 1 < len(parts):
                return parts[i + 1]
        for token in module.split("."):
            if token.startswith("astrbot_plugin_"):
                return token
        if "astrbot" in text.lower():
            return "AstrBot核心"
        return module or "未知来源"

    def enqueue(self, source: str, module: str, function: str, error: str, path: str = "", level: str = "ERROR"):
        # AstrBot 工具执行超过等待时间后会产生 CancelledError/TimeoutError 链。
        # 这是工具层的正常超时反馈，不作为插件故障发送，避免维护群刷屏。
        if source == "日志" and str(level).upper() not in {"WARNING", "ERROR", "CRITICAL"}:
            return
        benign_tool_timeout = (
            "tool astrbot_execute_shell execution timeout" in error
            or "tool astrbot_execute_python execution timeout" in error
            or "astrbot_execute_shell execution timeout" in error
            or "astrbot_execute_python execution timeout" in error
        )
        if benign_tool_timeout:
            return
        plugin = self._plugin_name(module, path)
        raw = f"{plugin}|{module}|{function}|{level}|{error}"
        key = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()
        now = time.time()
        if now - self._seen.get(key, 0) < self.dedupe_seconds:
            return
        self._seen[key] = now
        self._queue.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "plugin": plugin, "module": module, "function": function, "source": source, "level": level, "error": error})
        self._drain_now()

    @staticmethod
    def _level_label(level: str) -> str:
        level = str(level or "ERROR").upper()
        if level == "WARNING":
            return "警告（黄色）"
        if level in {"ERROR", "CRITICAL"}:
            return f"错误（红色/{level}）"
        return level

    def _format(self, item: dict) -> str:
        text = str(item.get("error", ""))
        if len(text) > self.max_length:
            text = text[: self.max_length] + "\n……（已截断）"
        return ("⚠️【全局错误监控】\n"
                f"插件/来源：{item.get('plugin')}\n"
                f"模块：{item.get('module') or '未知'}\n"
                f"函数：{item.get('function') or '未知'}\n"
                f"级别：{self._level_label(item.get('level', 'ERROR'))}\n"
                f"类型：{item.get('source')}\n"
                f"时间：{item.get('time')}\n"
                f"错误：{text}")

    async def flush(self, event: AstrMessageEvent):
        if not self._queue or not self.notify_group.isdigit():
            return
        async with self._send_lock:
            while self._queue:
                item = self._queue.popleft()
                try:
                    await event.bot.send_group_msg(group_id=int(self.notify_group), message=self._format(item))
                except Exception:
                    # QQ 连接不可用时放回队首，下次有事件再尝试。
                    self._queue.appendleft(item)
                    break

    @filter.event_message_type(filter.EventMessageType.ALL, priority=-10000)
    async def flush_errors(self, event: AstrMessageEvent):
        self._umo = event.unified_msg_origin
        try:
            self._bot = event.bot
        except Exception:
            self._bot = getattr(self, "_bot", None)
        await self._drain()

    async def terminate(self):
        for log in (logging.getLogger(n) for n in getattr(self, "_log_names", ("", "astrbot", "uvicorn"))):
            try:
                log.removeHandler(self._handler)
            except Exception:
                pass
        if sys.excepthook is self._excepthook:
            sys.excepthook = self._old_excepthook
        if self._old_thread_hook and threading.excepthook is self._thread_excepthook:
            threading.excepthook = self._old_thread_hook

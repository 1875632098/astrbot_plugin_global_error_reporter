# 全局错误监控（astrbot_plugin_global_error_reporter）

把 AstrBot 运行时的异常统一收集、格式化，然后推送到你指定的 QQ 群。插件报错、核心警告、未捕获异常都能第一时间看到，不用一直盯着后台日志。

无指令、无工具，安装并填好通知群后自动运行。

## 功能

- **捕获范围**：日志系统中警告级及以上的记录（`WARNING` / `ERROR` / `CRITICAL`）、主线程未捕获异常、子线程未捕获异常。
- **来源标注**：自动识别错误来自哪个插件、哪个模块、哪个函数；AstrBot 自身的报错标注为「AstrBot核心」。
- **去重合并**：同一条错误在设定时间内只推送一次，避免同一个错误刷屏。
- **长度截断**：超长堆栈按配置截断，避免超出 QQ 消息长度限制。
- **自动补挂**：运行期间周期性检查并补挂新出现的 logger，插件热加载后也能继续捕获。
- **静默自身**：监控器自己产生的日志不会再次进入队列，不会形成日志循环。
- **安全兜底**：通知群号没填或不是纯数字时不会发送，也不会往其它会话乱发。

## 安装

把本仓库放到 AstrBot 的 `data/plugins/astrbot_plugin_global_error_reporter/` 目录下，重载插件即可。

也可以在插件市场搜索「全局错误监控」安装。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `notify_group` | string | 空 | 错误通知群号，只填数字；留空则不推送 |
| `dedupe_seconds` | int | 300 | 重复错误合并时间（秒） |
| `max_message_length` | int | 3000 | 单条错误最大长度，超出截断 |

`notify_group` 必须是纯数字群号。填错或留空时插件不会报错，只是不发送，错误仍然会进 AstrBot 自己的日志。

## 推送格式

```
⚠️【全局错误监控】
插件/来源：astrbot_plugin_xxx
模块：data.plugins.astrbot_plugin_xxx.main
函数：some_handler
级别：错误（红色/ERROR）
类型：日志
时间：2026-01-01 12:00:00
错误：Traceback (most recent call last): ...
```

级别为 `WARNING` 时显示「警告（黄色）」，`ERROR` / `CRITICAL` 显示「错误（红色/级别）」。

## 工作原理

插件在初始化时创建一个 `logging.Handler`，挂到 AstrBot 的根 logger、`astrbot.*` 和 `uvicorn` 上，同时遍历当前已存在的所有 logger 各挂一份（插件 logger 通常 `propagate=False`，只挂根节点收不到）。

日志进入 handler 后先做三件事：跳过监控器自身产生的日志、过滤工具执行超时这类正常反馈、按「插件+模块+函数+级别+错误内容」的哈希在去重窗口内去重。通过后进入队列，由后台任务每 2 秒轮询发送，另有消息事件触发的即时发送路径作为补充。

发送优先用当前 bot 的群消息接口；如果当前事件不在目标群，则回退到 `context.send_message`，且只在 UMO 与目标群匹配时才发，避免把错误发到无关会话。发送失败会放回队首，下次再试。

插件卸载时会移除所有 handler、还原 `sys.excepthook` 和 `threading.excepthook`。

## 注意事项

- 本插件是「监控 + 转发」，不会自动修复任何问题。
- 高频报错时建议适当调大 `dedupe_seconds`，避免维护群被刷屏。
- 堆栈里可能包含文件路径、配置片段等信息，请确认通知群是可信范围。
- 只支持群号推送，暂不支持私聊或 Webhook。

## 兼容性

- AstrBot `>= 4.24.4`
- 已在 `aiocqhttp` 平台验证；推送依赖平台的群消息接口。

## 许可

MIT

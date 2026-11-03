# 蔗糖的表情包小屋~

独立表情包库插件：WebUI 面板上传、LLM 视觉自动打标、快速通道一次回复即配图、可选 QQ 表情格式。

## 功能

- **WebUI 管理面板**：上传 / 删除 / 编辑标注 / 下载原图 / 状态筛选（失败一键重试），面板顶部显示实际存储路径
- **LLM 视觉自动打标**：接入任意 OpenAI 兼容视觉 API，自动生成名称 / 标签 / 情绪 / 用途描述，支持中文容错解析
- **批量导入友好**：并发上限 + 全局最小请求间隔 + 429/503 全队列冷却 + 指数退避重试，上百张一次导入不会被限流打崩
- **快速通道配图**：AI 回复末尾输出 `<sticker>` 标签即自动本地检索配图，一次回复就带图，省 token 速度快
- **AI 工具调用**：`sticker_pick` / `sticker_search` / `sticker_list`，支持会话冷却、发送概率、全局写锁防刷屏

## 上传方式（桌面端 / 浏览器通用）

1. 点击上传区选择文件（可多选）
2. 截图后直接 `Ctrl+V` 粘贴
3. 粘贴本地文件夹路径，一键批量导入（递归扫描、内容去重、单次上限 500 张）
4. 在浏览器中打开面板（面板内有入口），支持拖拽图片 / 文件夹到页面任意位置

> 桌面端 Launcher（Tauri）会在窗口层拦截系统拖拽，属宿主限制，面板内已提供浏览器打开入口与文件夹导入兜底。

## 安装

在 AstrBot WebUI 插件市场或插件管理中通过仓库地址安装：

```
SucRoseZT/astrbot_plugin_sticker_kit
```

## 主要配置

| 配置项 | 说明 | 默认 |
| --- | --- | --- |
| `llm_api_base` / `llm_api_key` / `llm_model` | 打标用视觉模型（OpenAI 兼容） | 空（不自动打标） |
| `llm_concurrency` | 同时请求 LLM 的图片数上限 | 2 |
| `llm_min_interval` | 相邻打标请求最小间隔（秒），0 关闭 | 1.0 |
| `protocol_mode` | 快速通道开关 | 开 |
| `delivery_mode` | 表情包单独一条 / 跟随正文 | separate_after |
| `image_format` | image / qq_emoji（OneBot 平台） | qq_emoji |
| `send_probability` | 快速通道发图概率 | 0.9 |
| `send_cooldown_seconds` | 同会话发送冷却 | 30 |
| `write_lock_seconds` | 发送后全局写锁 | 60 |
| `admin_only` | 仅管理员可触发 LLM 工具 | 关 |

## 数据存储

标注与图片保存在 `data/plugin_data/astrbot_plugin_sticker_kit/`（`index.json` + `images/`），面板内可见完整路径，可随实例整体迁移备份。

## 更新日志

### v1.2.1
- 新增 `llm_min_interval` 全局限流：相邻打标请求强制最小间隔，防止批量导入触发 503/429
- 429/503 触发全队列冷却（尊重 `Retry-After`），冷却期请求自动挂起
- 快速通道冷却 / 权限拦截补充日志，便于排查"群聊发不出表情包"

### v1.2.0
- 修复面板删除按钮无效（自绘确认弹窗替代被沙箱禁用的 `confirm`）
- 打标失败率优化：并发闸 + 退避重试 + 失败即时落盘 + pending 状态自动重置
- 面板新增状态筛选与一键重试所有失败；显示存储路径；卡片支持下载原图
- 上传方式扩展：Ctrl+V 粘贴、文件夹路径批量导入、浏览器中拖拽上传

## License

MIT

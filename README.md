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
5. 浏览器登录 WebUI 后也可从侧边栏直接进入面板

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
| `protocol_mode` | 快速通道总开关 | 开 |
| `sticker_frequency` | 配图频率档位：关闭 / 很少 / 适中 / 较多 / 每条都发 | 适中 |
| `delivery_mode` | 文字先发/图片后发，或同一条消息（两条路径都遵守） | separate_after |
| `separate_send_gap_ms` | 分段间隔（毫秒），0 为不等待 | 350 |
| `image_format` | image / qq_emoji（OneBot 平台） | image |
| `send_probability` | 快速通道发图概率（1.0=写出标签必发） | 0.9 |
| `send_cooldown_seconds` | 同会话发送冷却（0=不限） | 30 |
| `named_request_bypass` | 用户点名要表情包时跳过概率与冷却 | 开 |
| `named_request_keywords` | 点名判定关键词，英文逗号分隔 | 表情包,斗图,发个图… |
| `protocol_prompt_extra` | 追加在协议末尾的自定义约束 | 空 |
| `custom_protocol_prompt` | 整段替换内置协议文案 | 空 |
| `inject_to_user_prompt` | 兜底注入用户消息，防协议被覆盖 | 关 |
| `write_lock_seconds` | 发送后全局写锁 | 60 |
| `admin_only` | 仅管理员可触发 LLM 工具 | 关 |

## 配图频率怎么调

频率由四层叠加决定，从软到硬：

1. **档位**（`sticker_frequency`）：只影响提示词里对 AI 的倾向描述，是"软"引导。选「关闭」则完全不注入协议，AI 不会主动配图。
2. **概率**（`send_probability`）：AI 已经写出标签后，再按概率决定发不发。`0.9` 意味着每十次吞掉一次，想要稳定就设 `1.0`。
3. **冷却**（`send_cooldown_seconds`）：同一会话两次发图的最小间隔，`30` 意味着半分钟内第二张一定发不出去。
4. **点名豁免**（`named_request_bypass`）：用户消息命中关键词时，第 2、3 层直接跳过——要就发得出来。

只想让 AI 少发一点 → 降档位；发现它写了标签却没图 → 先把概率调到 1.0、冷却调到 0 再观察。
`/sticker freq 较多` 可在聊天里直接切档位，`/sticker status` 会显示当前档位、概率、冷却以及最近一次协议注入和标签命中的时间，用来判断是"模型没写标签"还是"插件没注入"。

## 投递方式

`delivery_mode` 对两条路径都生效：

- **快速通道**（AI 自己在回复末尾写标签）：`separate_after` 时正文先发，正文发出后再补一条图片
- **搜索工具**（AI 调 `sticker_search` + `sticker_pick`）：`separate_after` 时先发 `caption` 文字，隔 `separate_send_gap_ms` 毫秒再发图片；`same_message` 时两者合并成一条

想两条消息之间停顿更自然就把 `separate_send_gap_ms` 调到 500~800；被平台风控或吞图时优先调大间隔。设为 `same_message` 则回到"文字+图一条"的旧观感。

## 数据存储

标注与图片保存在 `data/plugin_data/astrbot_plugin_sticker_kit/`（`index.json` + `images/`），面板内可见完整路径，可随实例整体迁移备份。

## 更新日志

### v1.5.0
- 面板页面目录更名：`pages/表情包面板` → `pages/表情包小屋`，与插件显示名统一（旧收藏的面板地址会失效，请从插件详情页重新进入）
- README 补齐完整配置表、「配图频率怎么调」「投递方式」两节，以及 1.2.0 起的全部更新日志
- 本版将 v1.2.0 ~ v1.4.0 的改动合并发布到仓库（仓库此前停留在 1.1.0）

### v1.4.0
- 修复 `sticker_pick` 忽略 `delivery_mode` 的 bug：此前走搜索工具时文字与图片被写死拼成一条消息，现在按配置分两条发送（新增 `separate_send_gap_ms` 控制两条之间的停顿）
- 修复协议注入被覆盖的隐患：`on_llm_request` 改为 `priority=-99999`（框架按 priority 从大到小执行，越小越晚执行），保证在其他插件之后写入；注入前做幂等检查，避免重复叠加
- 新增 `inject_to_user_prompt` 兜底：可选把一行极简标签说明同时写进用户消息，防止别的插件整体赋值 `system_prompt` 后协议失效
- 配图频率全面可配：新增 `sticker_frequency` 五档、`named_request_bypass` 点名豁免、`named_request_keywords` 自定义关键词、`protocol_prompt_extra` 附加指令、`custom_protocol_prompt` 整段自定义
- 协议文案改为正向引导，去掉「不要每条都配」这类否定起手（小模型会把它理解成默认不发）
- `/sticker status` 增加档位、概率、冷却、点名豁免、最近一次协议注入与标签命中时间；新增 `/sticker freq` 快速切档
- 面板配置分组重排为「配图频率 / 协议与文案 / AI 工具与发送 / 检索与导入 / LLM 视觉打标」

### v1.3.0
- 新增 WebUI 插件面板「表情包小屋」：上传 / 批量导入 / 标注编辑 / 状态筛选 / 下载原图，内置配置页，改动即存即生效
- 面板内可直接编辑插件配置（按分组渲染表单，保存即生效），不必再回 WebUI 插件配置页
- 新增 `fuzzy_search` 模糊检索：关键词与名称/标签近似也能命中（如搜「生气」可带出「气鼓鼓」「哼气」）

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

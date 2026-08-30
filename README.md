# astrbot_plugin_xiaohongshu

基于 Playwright 的 AstrBot 小红书插件：搜索 / 笔记 / 评论 / 点赞 / 收藏 / 关注 / 浏览养号，自带 WUI 远程浏览器面板。

## 功能特性

- 笔记搜索、笔记详情、评论获取
- 点赞 / 收藏 / 关注 / 发评论互动
- 浏览养号：模拟真实浏览行为，点赞 / 收藏概率可调
- WUI 内嵌远程浏览器：真浏览器实时画面，点击拖动过验证
- 扫码登录 / Cookie 注入双通道，无需手动填 Cookie
- Cookie 自动抓取、自动续期、掉线通知、可配置自动重登
- 浏览器依赖自动检测，面板一键自动安装

## 环境要求

- Python 3.10+
- AstrBot v3.4.20+（WUI 面板需 v4）
- Playwright + Chromium（面板里可一键自动安装）

## 安装

1. 下载本仓库 zip 包（或 Release 附件）
2. AstrBot 管理面板 → 插件管理 → 安装插件 → 上传 zip
3. 或解压到服务器插件目录后重启 AstrBot

## 快速开始

1. 插件配置开启 auto_start
2. 面板 → 插件 → 小红书 Bot → 打开 console 页面
3. 点击「扫码登录」，用小红书 App 扫码
4. 登录后即可在 QQ 群 / 私聊中使用命令

## 命令

| 命令 | 说明 |
| --- | --- |
| 小红书登录 | 打开登录面板 / 发起扫码登录 |
| 小红书状态 | 查看登录状态与浏览器状态 |
| 小红书退出 | 退出登录并清理会话 |
| 小红书搜索 关键词 | 搜索笔记 |
| 小红书笔记 链接 | 获取笔记详情 |
| 小红书评论 链接 [页数] | 获取笔记评论 |
| 小红书点赞 链接 | 点赞笔记 |
| 小红书收藏 链接 | 收藏笔记 |
| 小红书评 链接 内容 | 发表评论 |
| 小红书关注 链接 | 关注作者 |
| 小红书浏览 [轮次] | 启动浏览养号 |

## WUI 面板

插件详情页会出现 console 页面入口，提供：

- 扫码登录 / Cookie 注入
- 浏览器实时画面（可点击、拖动）
- 登录状态、依赖状态检测
- 一键自动安装 Playwright + Chromium

## 配置项

| 配置 | 说明 | 默认 |
| --- | --- | --- |
| auto_start | 启动时自动拉起浏览器 | false |
| cookie_auto_refresh | Cookie 自动续期 | true |
| cookie_refresh_interval | 续期检查间隔（分钟） | 30 |
| offline_notify | 掉线时发送通知 | true |
| offline_relogin | 掉线自动重登 | false |
| notify_target | 通知目标（群号 / QQ 号） | 空 |
| nurture_enabled | 浏览养号开关 | true |
| nurture_rounds | 单轮养号浏览条数 | 10 |
| nurture_wait | 养号间隔（秒） | 30 |
| nurture_like_prob | 点赞概率 0-1 | 0.3 |
| nurture_collect_prob | 收藏概率 0-1 | 0.2 |

## 常见问题

Q: 面板里看不到 WUI 入口？
A: WUI 只识别 pages/ 下的子目录结构（pages/console/index.html）。确认 AstrBot 为 v4 版本，插件为最新版。

Q: 提示没有浏览器？
A: 打开 console 面板点「自动安装依赖」，或在服务器执行 pip install playwright 和 playwright install chromium。

Q: 登录掉线了怎么办？
A: 插件自动检测掉线并通知（可配置自动重登），也可在面板重新扫码登录。

## 致谢

架构参考 astrbot_plugin_douyin，功能参考 astrbot_plugin_xhs_browser。

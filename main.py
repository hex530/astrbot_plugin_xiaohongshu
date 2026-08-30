"""小红书 Bot - AstrBot 插件主入口

功能：
- Playwright 无头浏览器扫码登录小红书网页版，登录态持久化
- 搜索笔记 / 笔记详情 / 评论抓取
- 点赞 / 收藏 / 评论 / 关注
- 浏览推荐流（养号）
- WUI 内嵌远程浏览器（真浏览器画面/点击拖动/文本输入/Cookie 注入）
- 浏览器依赖自动安装

指令：
- /小红书登录   生成扫码登录二维码
- /小红书状态   查看登录态
- /小红书退出   清除登录态
- /小红书搜索 <关键词> [数量]   搜索笔记
- /小红书笔记 <链接>   查看笔记详情
- /小红书评论 <链接> [数量]   抓取笔记评论
- /小红书点赞 <链接>   点赞笔记
- /小红书收藏 <链接>   收藏笔记
- /小红书评 <链接> <内容>   评论笔记
- /小红书关注 <链接>   关注用户
- /小红书浏览 [轮数]   浏览推荐流
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Star, register
from astrbot.api.web import error_response, json_response, request

from .core.xhs_browser import BrowserManager
from .core.xhs import XhsClient

P = "astrbot_plugin_xiaohongshu"
PLATFORM = "xhs"
VERSION = "0.1.0"

WUI_CFG_KEYS = [
    "auto_start", "cookie_auto_refresh", "cookie_refresh_interval",
    "offline_notify", "offline_relogin", "notify_target",
    "nurture_enabled", "nurture_rounds", "nurture_wait",
    "nurture_like_prob", "nurture_collect_prob",
]


@register(P, "黯渊", "小红书搜索/笔记/评论/点赞/收藏/关注/浏览养号 + WUI 内嵌远程浏览器（真浏览器画面/点击拖动/文本输入/Cookie 导入）+ 扫码登录 + 浏览器依赖自动安装", VERSION)
class XiaohongshuPlugin(Star):
    def __init__(self, context, config=None):
        super().__init__(context)
        self._cfg = config or {}
        self._data_dir = self._resolve_data_dir()
        self._browser = BrowserManager(self._data_dir, headless=True)
        self._client = XhsClient(self._browser)
        self._register_web_apis(context)
        self._maintenance_task: Optional[asyncio.Task] = None
        self._offline_notified = False
        self._last_cookie_refresh = 0.0
        self._deps_installing = False
        # 浏览养号状态
        self._browsing = False
        self._browse_stats = {"likes": 0, "collects": 0, "views": 0}

    # ── 基础 ────────────────────────────────────────────────────
    def _resolve_data_dir(self) -> str:
        try:
            return self.context.get_data_dir()
        except Exception:
            return os.path.join(os.getcwd(), "data", "plugin_data", P)

    def _cfg_get(self, key, default=None):
        cfg = self._cfg
        if isinstance(cfg, dict):
            return cfg.get(key, default)
        return getattr(cfg, key, default)

    async def _real_auth(self) -> bool:
        """登录态判定：浏览器在线时实时校验，否则读文件。"""
        try:
            if self._browser.is_started:
                return await self._browser._check_login_status()
        except Exception:
            pass
        try:
            sf = os.path.join(self._data_dir, "xhs_storage.json")
            if os.path.exists(sf):
                data = json.loads(Path(sf).read_text(encoding="utf-8"))
                for c in data.get("cookies", []):
                    if c.get("name") == "web_session" and c.get("value"):
                        return True
        except Exception:
            pass
        return False

    # ── 生命周期 ────────────────────────────────────────────────
    async def initialize(self):
        if self._cfg_get("auto_start", True):
            try:
                if not self._browser.is_started:
                    await self._browser.start()
            except Exception as exc:
                logger.warning(f"[xhs] 浏览器启动失败（可用 WUI 安装依赖）: {exc}")
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def _maintenance_loop(self) -> None:
        """后台维护：掉线检测 + Cookie 自动刷新。"""
        logger.info("[xhs] 维护任务启动（Cookie 自动刷新 / 掉线检测）")
        while True:
            try:
                await self._maintenance_tick()
            except Exception as exc:
                logger.warning(f"[xhs] 维护任务异常: {exc}")
            await asyncio.sleep(60)

    async def _maintenance_tick(self) -> None:
        cfg_refresh = self._cfg_get("cookie_auto_refresh", True)
        cfg_notify = self._cfg_get("offline_notify", True)
        cfg_relogin = self._cfg_get("offline_relogin", True)
        online = False
        if self._browser.is_started:
            try:
                online = await self._browser._check_login_status()
            except Exception:
                online = False
        if not online:
            if cfg_relogin and self._browser.is_authenticated:
                try:
                    ok = await self._browser.reconnect()
                    if ok:
                        online = True
                        logger.info("[xhs] 自动重连成功")
                except Exception as exc:
                    logger.warning(f"[xhs] 自动重连失败: {exc}")
            if not online:
                if cfg_notify and not self._offline_notified:
                    self._offline_notified = True
                    await self._send_notify("⚠️ 小红书登录已掉线，自动重连失败。请到 WebUI 重新扫码或注入 Cookie。")
                return
        if self._offline_notified:
            self._offline_notified = False
            if cfg_notify:
                await self._send_notify("✅ 小红书登录已恢复在线。")
        if cfg_refresh and self._browser.is_started:
            interval = max(300, int(self._cfg_get("cookie_refresh_interval", 3600) or 3600))
            now = time.time()
            if now - self._last_cookie_refresh >= interval:
                try:
                    await self._browser._save_storage_state()
                    self._last_cookie_refresh = now
                    logger.info("[xhs] Cookie 已自动刷新保存")
                except Exception as exc:
                    logger.warning(f"[xhs] Cookie 自动刷新失败: {exc}")

    async def _send_notify(self, text: str) -> None:
        target = str(self._cfg_get("notify_target", "") or "").strip()
        if not target:
            return
        try:
            from astrbot.core.message.message_event_result import MessageEventResult
            await self.context.send_message(target, MessageEventResult().message(str(text)))
            logger.info(f"[xhs] 通知已发送: {text[:40]}")
        except Exception as exc:
            logger.warning(f"[xhs] 通知发送失败: {exc}")
    # ── 指令 ────────────────────────────────────────────────────
    @filter.command("小红书登录")
    async def xhs_login(self, event: AstrMessageEvent):
        """生成扫码登录二维码并发送图片到当前会话。"""
        yield event.plain_result("开始准备小红书扫码登录，稍等…")
        try:
            if not self._browser.is_started:
                await self._browser.start()
            await self._browser.ensure_authenticated()
            qr_path = await self._browser.wait_qr_screenshot()
            if os.path.exists(qr_path):
                yield event.chain_result([Image(file=qr_path), Plain("用小红书 App 扫码登录，登录后我会自动保存登录态。")])
            else:
                yield event.plain_result("二维码截图失败，请重试。")
        except Exception as exc:
            logger.exception(f"[xhs] 登录流程异常: {exc}")
            yield event.plain_result(f"❌ 登录流程出错：{exc}")

    @filter.command("小红书状态")
    async def xhs_status(self, event: AstrMessageEvent):
        auth = await self._real_auth()
        st = await self._client.status()
        lines = [
            "📕 小红书 Bot 状态",
            f"登录态：{'✅ 已登录' if auth else '❌ 未登录'}",
            f"浏览器：{'✅ 已启动' if self._browser.is_started else '❌ 未启动'}",
            f"昵称：{st.get('nickname') or '未知'}",
            f"浏览养号：{'🔄 运行中' if self._browsing else '⏸ 空闲'}",
            f"版本：{VERSION}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("小红书退出")
    async def xhs_logout(self, event: AstrMessageEvent):
        """清除登录态。"""
        try:
            state_file = os.path.join(self._data_dir, "xhs_storage.json")
            if os.path.exists(state_file):
                os.remove(state_file)
        except Exception:
            pass
        if self._browser.is_started:
            try:
                await self._browser.context.clear_cookies()
            except Exception:
                pass
            try:
                await self._browser.close()
            except Exception:
                pass
        yield event.plain_result("已退出小红书登录。")

    @filter.command("小红书搜索")
    async def xhs_search(self, event: AstrMessageEvent, keyword: str = "", num: str = "8"):
        """搜索小红书笔记。"""
        keyword = (keyword or "").strip()
        if not keyword:
            yield event.plain_result("用法：/小红书搜索 <关键词> [数量]")
            return
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        try:
            limit = max(1, min(int(num or 8), 15))
        except Exception:
            limit = 8
        yield event.plain_result(f"正在搜索「{keyword}」，稍等…")
        try:
            if not self._browser.is_started:
                await self._browser.start()
            results = await self._client.search_notes(keyword, limit)
            if not results:
                yield event.plain_result("没搜到结果，换个关键词试试。")
                return
            lines = [f"🔍 「{keyword}」的搜索结果（{len(results)} 条）:"]
            for i, r in enumerate(results, 1):
                author = f"（{r['author']}）" if r.get("author") else ""
                lines.append(f"{i}. {r['title'][:40]}{author}")
                lines.append(f"   {r['link']}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.exception(f"[xhs] 搜索异常: {exc}")
            yield event.plain_result(f"❌ 搜索出错：{exc}")

    @filter.command("小红书笔记")
    async def xhs_note(self, event: AstrMessageEvent, url: str = ""):
        """查看笔记详情。"""
        url = (url or "").strip()
        if not url:
            yield event.plain_result("用法：/小红书笔记 <笔记链接>")
            return
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        yield event.plain_result("正在打开笔记，稍等…")
        try:
            if not self._browser.is_started:
                await self._browser.start()
            info = await self._client.get_note_detail(url)
            if not info:
                yield event.plain_result("笔记解析失败，链接可能无效或页面加载失败。")
                return
            lines = [
                f"📝 {info['title'] or '（无标题）'}",
                f"👤 {info['author'] or '未知作者'}",
                f"🔗 {info['url']}",
            ]
            if info.get("desc"):
                lines.append(f"📄 {info['desc'][:200]}")
            if info.get("images"):
                lines.append(f"🖼 图片 {len(info['images'])} 张")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.exception(f"[xhs] 笔记详情异常: {exc}")
            yield event.plain_result(f"❌ 解析出错：{exc}")

    @filter.command("小红书评论")
    async def xhs_comments(self, event: AstrMessageEvent, url: str = "", num: str = "8"):
        """抓取笔记热门评论。"""
        url = (url or "").strip()
        if not url:
            yield event.plain_result("用法：/小红书评论 <笔记链接> [数量]")
            return
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        try:
            limit = max(1, min(int(num or 8), 15))
        except Exception:
            limit = 8
        yield event.plain_result("正在抓取评论，稍等…")
        try:
            if not self._browser.is_started:
                await self._browser.start()
            comments = await self._client.fetch_comments(url, limit)
            if not comments:
                yield event.plain_result("没抓到评论，可能笔记无评论或页面未加载。")
                return
            lines = [f"💬 热门评论（{len(comments)} 条）:"]
            for i, c in enumerate(comments, 1):
                lines.append(f"{i}. {c['text']}")
            yield event.plain_result("\n".join(lines))
        except Exception as exc:
            logger.exception(f"[xhs] 评论抓取异常: {exc}")
            yield event.plain_result(f"❌ 评论抓取出错：{exc}")

    @filter.command("小红书点赞")
    async def xhs_like(self, event: AstrMessageEvent, url: str = ""):
        """点赞笔记。"""
        url = (url or "").strip()
        if not url:
            yield event.plain_result("用法：/小红书点赞 <笔记链接>")
            return
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        try:
            if not self._browser.is_started:
                await self._browser.start()
            ok = await self._client.like_note(url)
            yield event.plain_result("✅ 点赞成功" if ok else "❌ 点赞失败，可能页面结构变了")
        except Exception as exc:
            yield event.plain_result(f"❌ 点赞出错：{exc}")

    @filter.command("小红书收藏")
    async def xhs_collect(self, event: AstrMessageEvent, url: str = ""):
        """收藏笔记。"""
        url = (url or "").strip()
        if not url:
            yield event.plain_result("用法：/小红书收藏 <笔记链接>")
            return
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        try:
            if not self._browser.is_started:
                await self._browser.start()
            ok = await self._client.collect_note(url)
            yield event.plain_result("✅ 收藏成功" if ok else "❌ 收藏失败，可能页面结构变了")
        except Exception as exc:
            yield event.plain_result(f"❌ 收藏出错：{exc}")

    @filter.command("小红书评")
    async def xhs_comment(self, event: AstrMessageEvent, url: str = "", text: str = ""):
        """评论笔记。"""
        url = (url or "").strip()
        text = (text or "").strip()
        if not url or not text:
            yield event.plain_result("用法：/小红书评 <笔记链接> <评论内容>")
            return
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        try:
            if not self._browser.is_started:
                await self._browser.start()
            ok = await self._client.comment_note(url, text)
            yield event.plain_result("✅ 评论已发出" if ok else "❌ 评论发送失败")
        except Exception as exc:
            yield event.plain_result(f"❌ 评论出错：{exc}")

    @filter.command("小红书关注")
    async def xhs_follow(self, event: AstrMessageEvent, url: str = ""):
        """关注用户。"""
        url = (url or "").strip()
        if not url:
            yield event.plain_result("用法：/小红书关注 <用户主页链接>")
            return
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        try:
            if not self._browser.is_started:
                await self._browser.start()
            ok = await self._client.follow_user(url)
            yield event.plain_result("✅ 关注成功" if ok else "❌ 关注失败，可能已关注或页面结构变了")
        except Exception as exc:
            yield event.plain_result(f"❌ 关注出错：{exc}")

    @filter.command("小红书浏览")
    async def xhs_browse(self, event: AstrMessageEvent, rounds: str = "5"):
        """浏览推荐流（养号）。"""
        if not await self._real_auth():
            yield event.plain_result("还没登录小红书，先 /小红书登录 扫码。")
            return
        try:
            n = max(1, min(int(rounds or 5), 30))
        except Exception:
            n = 5
        if self._browsing:
            yield event.plain_result("已在浏览中，先停止再启动。")
            return
        yield event.plain_result(f"开始浏览推荐流（{n} 轮），约 {n * 6} 秒…")
        try:
            if not self._browser.is_started:
                await self._browser.start()
            self._browsing = True
            try:
                viewed = await self._client.browse_feed(rounds=n, wait_sec=6)
                self._browse_stats["views"] += viewed
            finally:
                self._browsing = False
            yield event.plain_result(f"✅ 浏览完成，共 {viewed} 轮。累计浏览 {self._browse_stats['views']} 轮。")
        except Exception as exc:
            self._browsing = False
            logger.exception(f"[xhs] 浏览异常: {exc}")
            yield event.plain_result(f"❌ 浏览出错：{exc}")
    # ── WUI 路由注册 ─────────────────────────────────────────────
    def _register_web_apis(self, context):
        apis = [
            (f"/{P}/status", self.api_status, ["GET"], "小红书状态"),
            (f"/{P}/config", self.api_config, ["GET", "POST"], "小红书配置"),
            (f"/{P}/qr", self.api_qr, ["GET"], "小红书扫码登录"),
            (f"/{P}/deps", self.api_deps, ["GET", "POST"], "浏览器依赖检测/安装"),
            (f"/{P}/logout", self.api_logout, ["POST"], "小红书退出登录"),
            (f"/{P}/cookie_login", self.api_cookie_login, ["POST"], "小红书Cookie登录"),
            (f"/{P}/search", self.api_search, ["GET"], "小红书搜索"),
            (f"/{P}/note", self.api_note, ["GET"], "笔记详情"),
            (f"/{P}/comments", self.api_comments, ["GET"], "笔记评论"),
            (f"/{P}/like", self.api_like, ["POST"], "点赞"),
            (f"/{P}/collect", self.api_collect, ["POST"], "收藏"),
            (f"/{P}/comment", self.api_comment, ["POST"], "评论"),
            (f"/{P}/follow", self.api_follow, ["POST"], "关注"),
            (f"/{P}/activity", self.api_activity, ["GET"], "浏览记录"),
            # 远程登录面板
            (f"/{P}/panel", self._serve_panel, ["GET"], "小红书远程登录面板"),
            (f"/{P}/screenshot", self.api_screenshot, ["GET"], "浏览器实时截图"),
            (f"/{P}/open_login", self.api_open_login, ["GET", "POST"], "打开小红书登录页"),
            (f"/{P}/browser_action", self.api_browser_action, ["POST"], "远程浏览器交互"),
            (f"/{P}/browser_info", self.api_browser_info, ["GET"], "远程浏览器状态"),
            (f"/{P}/grab_cookie", self.api_grab_cookie, ["POST"], "抓取并保存Cookie"),
        ]
        for path, handler, methods, desc in apis:
            try:
                context.register_web_api(path, handler, methods=methods, desc=desc)
            except Exception as e:
                logger.warning(f"注册 WUI API {path} 失败: {e}")

    async def api_status(self):
        return json_response({
            "authenticated": await self._real_auth(),
            "browser_started": self._browser.is_started,
            "browsing": self._browsing,
            "views": self._browse_stats.get("views", 0),
            "version": VERSION,
        })

    async def api_config(self):
        if request.method == "POST":
            payload = await request.json(default={})
            try:
                for k in WUI_CFG_KEYS:
                    if k in payload:
                        self._cfg[k] = payload[k]
                save = getattr(self._cfg, "save_config", None)
                if callable(save):
                    save()
                return json_response({"ok": True, "message": "配置已保存，后台已生效"})
            except Exception as exc:
                logger.exception(f"[xhs] WUI 保存配置失败: {exc}")
                return error_response(f"保存失败：{exc}", status_code=500)
        return json_response({
            "auto_start": bool(self._cfg_get("auto_start", True)),
            "cookie_auto_refresh": bool(self._cfg_get("cookie_auto_refresh", True)),
            "cookie_refresh_interval": int(self._cfg_get("cookie_refresh_interval", 3600) or 3600),
            "offline_notify": bool(self._cfg_get("offline_notify", True)),
            "offline_relogin": bool(self._cfg_get("offline_relogin", True)),
            "notify_target": str(self._cfg_get("notify_target", "") or ""),
            "nurture_enabled": bool(self._cfg_get("nurture_enabled", True)),
            "nurture_rounds": int(self._cfg_get("nurture_rounds", 5) or 5),
            "nurture_wait": int(self._cfg_get("nurture_wait", 6) or 6),
            "nurture_like_prob": float(self._cfg_get("nurture_like_prob", 0.2) or 0.2),
            "nurture_collect_prob": float(self._cfg_get("nurture_collect_prob", 0.15) or 0.15),
        })

    async def api_qr(self):
        """生成登录二维码（不阻塞等待扫码，前端轮询 /status）。"""
        try:
            if await self._real_auth():
                return json_response({"ok": True, "image": "", "tip": "当前已登录，无需扫码", "already": True})
            if not self._browser.is_started:
                await self._browser.start()
            page = self._browser.page
            await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(2.5)
            for sel in ["text=登录", ".login-btn", "[class*='login']",
                        "button:has-text('登录')", "span:has-text('登录')", "a:has-text('登录')"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=3000)
                    if btn:
                        await btn.click()
                        break
                except Exception:
                    continue
            await asyncio.sleep(1.5)
            qr_path = await self._browser.wait_qr_screenshot(timeout=25)
            if os.path.exists(qr_path):
                data = _b64.b64encode(Path(qr_path).read_bytes()).decode("ascii")
                asyncio.create_task(self._monitor_login())
                return json_response({"ok": True, "image": data, "tip": "用小红书 App 扫码，成功后自动保存登录态"})
            return error_response("二维码截图失败，请重试", status_code=500)
        except Exception as exc:
            logger.exception(f"[xhs] WUI 二维码生成异常: {exc}")
            msg = str(exc)
            err_type = "unknown"
            if "浏览器启动失败" in msg or "playwright" in msg.lower() or "chromium" in msg.lower():
                err_type = "browser_missing"
            elif "二维码" in msg or "qrcode" in msg.lower():
                err_type = "qr_not_found"
            return error_response(f"二维码生成失败：{msg}", status_code=500, extra={"err_type": err_type, "need_browser": err_type == "browser_missing"})
    async def api_deps(self):
        """GET: 检测浏览器依赖；POST: 触发自动安装（异步）。"""
        import astrbot.api.web as _w
        if _w.request.method == "POST":
            asyncio.create_task(self._install_deps_task())
            return json_response({"ok": True, "message": "依赖安装已启动，可轮询 /deps 查看进度"})
        deps = BrowserManager.check_deps()
        return json_response({
            "ok": deps["ok"],
            "playwright": deps["playwright"],
            "chromium": deps["chromium"],
            "browser_path": deps["browser_path"],
            "detail": deps["detail"],
            "installing": getattr(self, "_deps_installing", False),
        })

    async def _install_deps_task(self) -> None:
        if getattr(self, "_deps_installing", False):
            return
        self._deps_installing = True
        try:
            logger.info("[xhs] 开始自动安装浏览器依赖…")
            result = await BrowserManager.install_deps()
            logger.info(f"[xhs] 依赖安装结果: ok={result['ok']} detail={result['detail']}")
            if result["ok"] and not self._browser.is_started:
                try:
                    await self._browser.start()
                    logger.info("[xhs] 依赖安装完成，浏览器已自动启动")
                except Exception as exc:
                    logger.warning(f"[xhs] 浏览器启动失败: {exc}")
        finally:
            self._deps_installing = False

    async def _monitor_login(self):
        """后台轮询扫码结果，成功后保存登录态。"""
        for _ in range(90):
            await asyncio.sleep(2)
            try:
                if await self._browser._check_login_status():
                    if await self._browser.verify_online():
                        await self._browser._save_storage_state()
                        logger.info("[xhs] WUI 扫码登录成功，登录态已保存")
                        return
                    else:
                        logger.warning("[xhs] cookie 存在但页面未确认登录，继续等待扫码")
            except Exception:
                pass

    async def api_logout(self):
        try:
            state_file = os.path.join(self._data_dir, "xhs_storage.json")
            if os.path.exists(state_file):
                os.remove(state_file)
        except Exception:
            pass
        if self._browser.is_started:
            try:
                await self._browser.context.clear_cookies()
            except Exception:
                pass
            try:
                await self._browser.close()
            except Exception:
                pass
        return json_response({"ok": True, "message": "已退出登录"})

    async def api_cookie_login(self):
        """手动导入 Cookie 登录（绕开扫码风控）。"""
        try:
            payload = await request.json(default={})
            cookie_str = str(payload.get("cookie", "") or "").strip()
            if not cookie_str:
                return error_response("缺少 cookie 参数", status_code=400)
            ok, msg = await self._browser.apply_cookies(cookie_str)
            if ok:
                return json_response({"ok": True, "message": msg})
            return error_response(msg, status_code=400)
        except Exception as exc:
            logger.exception(f"[xhs] WUI Cookie 登录异常: {exc}")
            return error_response(f"Cookie 登录失败：{exc}", status_code=500)

    async def _require_login(self):
        if not self._browser.is_authenticated and not await self._real_auth():
            return error_response("未登录小红书，请先扫码登录", status_code=401)
        return None

    async def _ensure_browser_started(self):
        if not self._browser.is_started:
            await self._browser.start()

    async def api_search(self):
        err = await self._require_login()
        if err:
            return err
        kw = request.query.get("kw", "").strip()
        if not kw:
            return error_response("缺少 kw 参数", status_code=400)
        try:
            n = max(1, min(int(request.query.get("n", "8") or 8), 15))
        except Exception:
            n = 8
        try:
            await self._ensure_browser_started()
            results = await self._client.search_notes(kw, n)
            return json_response({"results": results})
        except Exception as exc:
            logger.exception(f"[xhs] WUI 搜索异常: {exc}")
            return error_response(f"搜索出错：{exc}", status_code=500)

    async def api_note(self):
        err = await self._require_login()
        if err:
            return err
        url = request.query.get("url", "").strip()
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            await self._ensure_browser_started()
            info = await self._client.get_note_detail(url)
            if not info:
                return error_response("笔记解析失败，链接可能无效", status_code=404)
            return json_response(info)
        except Exception as exc:
            logger.exception(f"[xhs] WUI 笔记详情异常: {exc}")
            return error_response(f"解析出错：{exc}", status_code=500)

    async def api_comments(self):
        err = await self._require_login()
        if err:
            return err
        url = request.query.get("url", "").strip()
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            n = max(1, min(int(request.query.get("n", "8") or 8), 15))
        except Exception:
            n = 8
        try:
            await self._ensure_browser_started()
            comments = await self._client.fetch_comments(url, n)
            return json_response({"comments": comments})
        except Exception as exc:
            logger.exception(f"[xhs] WUI 评论异常: {exc}")
            return error_response(f"评论抓取出错：{exc}", status_code=500)

    async def api_like(self):
        err = await self._require_login()
        if err:
            return err
        try:
            body = await request.json(default={})
        except Exception:
            body = {}
        url = str(body.get("url") or "").strip()
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            await self._ensure_browser_started()
            ok = await self._client.like_note(url)
            return json_response({"ok": ok})
        except Exception as exc:
            logger.exception(f"[xhs] WUI 点赞异常: {exc}")
            return error_response(f"点赞失败：{exc}", status_code=500)

    async def api_collect(self):
        err = await self._require_login()
        if err:
            return err
        try:
            body = await request.json(default={})
        except Exception:
            body = {}
        url = str(body.get("url") or "").strip()
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            await self._ensure_browser_started()
            ok = await self._client.collect_note(url)
            return json_response({"ok": ok})
        except Exception as exc:
            logger.exception(f"[xhs] WUI 收藏异常: {exc}")
            return error_response(f"收藏失败：{exc}", status_code=500)

    async def api_comment(self):
        err = await self._require_login()
        if err:
            return err
        try:
            body = await request.json(default={})
        except Exception:
            body = {}
        url = str(body.get("url") or "").strip()
        text = str(body.get("text") or "").strip()
        if not url or not text:
            return error_response("缺少 url/text 参数", status_code=400)
        try:
            await self._ensure_browser_started()
            ok = await self._client.comment_note(url, text)
            return json_response({"ok": ok})
        except Exception as exc:
            logger.exception(f"[xhs] WUI 评论发送异常: {exc}")
            return error_response(f"评论失败：{exc}", status_code=500)

    async def api_follow(self):
        err = await self._require_login()
        if err:
            return err
        try:
            body = await request.json(default={})
        except Exception:
            body = {}
        url = str(body.get("url") or "").strip()
        if not url:
            return error_response("缺少 url 参数", status_code=400)
        try:
            await self._ensure_browser_started()
            ok = await self._client.follow_user(url)
            return json_response({"ok": ok})
        except Exception as exc:
            logger.exception(f"[xhs] WUI 关注异常: {exc}")
            return error_response(f"关注失败：{exc}", status_code=500)

    async def api_activity(self):
        try:
            return json_response({"activity": {"browsing": self._browsing, **self._browse_stats}})
        except Exception as exc:
            return error_response(f"读取失败：{exc}", status_code=500)
    # ================================================================
    #  远程登录面板（内置浏览器实时画面 + 扫码/Cookie 双通道）
    # ================================================================

    async def _serve_panel(self):
        """远程登录面板页面。"""
        from starlette.responses import Response
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pages", "panel.html")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return Response(content=f.read(), media_type="text/html", headers={"Cache-Control": "no-store, max-age=0"})
        return Response(content="panel not found", status_code=404)

    async def api_screenshot(self):
        """浏览器实时截图。默认 JPEG base64 JSON；?raw=1 直接返回图片流。"""
        try:
            if not self._browser.is_started:
                if request.query.get("raw"):
                    from starlette.responses import Response
                    return Response(content=b"", media_type="image/jpeg")
                return json_response({"ok": False, "image": "", "tip": "浏览器未启动"})
            page = self._browser.active_page
            buf = await page.screenshot(type="jpeg", quality=72)
            if request.query.get("raw"):
                from starlette.responses import Response
                return Response(content=buf, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})
            data = _b64.b64encode(buf).decode("ascii")
            return json_response({"ok": True, "image": data, "ts": int(time.time())})
        except Exception as exc:
            logger.warning(f"[xhs] 远程面板截图失败: {exc}")
            return error_response(f"截图失败：{exc}", status_code=500)

    async def api_open_login(self):
        """打开小红书登录页（供远程面板手动过验证/扫码）。"""
        try:
            if not self._browser.is_started:
                await self._browser.start()
            page = self._browser.page
            await page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=25000)
            await asyncio.sleep(2.5)
            for sel in ["text=登录", ".login-btn", "[class*='login']",
                        "button:has-text('登录')", "span:has-text('登录')", "a:has-text('登录')"]:
                try:
                    btn = await page.wait_for_selector(sel, timeout=3000)
                    if btn:
                        await btn.click()
                        break
                except Exception:
                    continue
            await asyncio.sleep(1.5)
            for sel in ["text=扫码登录", "text=扫一扫登录", "div:has-text('扫码登录')"]:
                try:
                    tab = await page.wait_for_selector(sel, timeout=2500)
                    if tab:
                        await tab.click()
                        break
                except Exception:
                    continue
            await asyncio.sleep(1)
            return json_response({"ok": True, "message": "已打开小红书登录页"})
        except Exception as exc:
            logger.exception(f"[xhs] 远程面板打开登录页异常: {exc}")
            return error_response(f"打开登录页失败：{exc}", status_code=500)

    async def api_browser_action(self):
        """远程浏览器交互：点击/拖动/滚动/输入/按键/刷新。"""
        try:
            if not self._browser.is_started:
                return json_response({"ok": False, "message": "浏览器未启动，请先打开登录页"})
            payload = await request.json(default={})
            act = str(payload.get("type") or "").lower()
            page = self._browser.page
            extra = {}
            if act == "click":
                diag = await self._browser.mouse_click(float(payload.get("x", 0)), float(payload.get("y", 0)))
                extra = {"diag": diag}
            elif act == "drag":
                await self._browser.mouse_drag(
                    float(payload.get("x1", 0)), float(payload.get("y1", 0)),
                    float(payload.get("x2", 0)), float(payload.get("y2", 0)),
                    int(payload.get("steps", 12) or 12),
                )
            elif act == "move":
                await self._browser.mouse_move(float(payload.get("x", 0)), float(payload.get("y", 0)))
            elif act == "scroll":
                await self._browser.scroll_page(float(payload.get("dx", 0) or 0), float(payload.get("dy", 300) or 300))
            elif act == "type":
                text = str(payload.get("text") or "")
                idx = payload.get("index")
                if idx is not None and str(idx) != "":
                    typed = await self._browser.type_into(int(idx), text)
                    extra = {"typed": typed}
                else:
                    await self._browser.type_text(float(payload.get("x", 0)), float(payload.get("y", 0)), text)
            elif act == "find_inputs":
                inputs = await self._browser.find_inputs()
                extra = {"inputs": inputs, "count": len(inputs)}
            elif act == "focus_input":
                idx = int(payload.get("index", 0) or 0)
                res = await self._browser.focus_input(idx)
                extra = {"focus": res}
            elif act == "key":
                await self._browser.press_key(str(payload.get("key") or "Enter"))
            elif act == "reload":
                await self._browser.reload_page()
            elif act == "nav":
                url = str(payload.get("url") or "").strip()
                if not url:
                    return error_response("缺少 url 参数", status_code=400)
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                await self._browser.navigate(url)
            elif act == "back":
                await self._browser.go_back()
            elif act == "forward":
                await self._browser.go_forward()
            elif act == "home":
                await self._browser.navigate("https://www.xiaohongshu.com")
            else:
                return error_response(f"未知动作类型：{act}", status_code=400)
            return json_response({"ok": True, "message": f"动作 {act} 已执行", **extra})
        except Exception as exc:
            logger.warning(f"[xhs] 浏览器交互失败: {exc}")
            return error_response(f"交互失败：{exc}", status_code=500)

    async def api_browser_info(self):
        """远程浏览器状态：当前 URL、标题、登录态、cookie 概览。"""
        try:
            if not self._browser.is_started:
                return json_response({"ok": True, "started": False, "url": "", "title": "", "authenticated": await self._real_auth(), "cookie_count": 0})
            url = await self._browser.current_url()
            title = await self._browser.page_title()
            auth = await self._browser._check_login_status()
            try:
                cookies = await self._browser.context.cookies()
                ccount = len(cookies)
            except Exception:
                ccount = 0
            return json_response({"ok": True, "started": True, "url": url, "title": title, "authenticated": auth, "cookie_count": ccount})
        except Exception as exc:
            logger.warning(f"[xhs] 浏览器状态获取失败: {exc}")
            return error_response(f"获取浏览器状态失败：{exc}", status_code=500)

    async def api_grab_cookie(self):
        """抓取当前浏览器全部 cookie 并保存登录态。"""
        try:
            if not self._browser.is_started:
                return error_response("浏览器未启动，请先打开小红书登录页", status_code=400)
            dump = await self._browser.dump_cookies()
            if not dump.get("ok"):
                return error_response(dump.get("error") or "Cookie 抓取失败", status_code=500)
            if not dump.get("web_session"):
                return json_response({"ok": False, "message": "当前浏览器没有有效 web_session，可能还没登录成功", "count": dump.get("count", 0)})
            await self._browser._save_storage_state()
            return json_response({"ok": True, "message": f"已抓取 {dump.get('count', 0)} 条 Cookie 并保存登录态", "count": dump.get("count", 0)})
        except Exception as exc:
            logger.exception(f"[xhs] Cookie 抓取异常: {exc}")
            return error_response(f"Cookie 抓取失败：{exc}", status_code=500)

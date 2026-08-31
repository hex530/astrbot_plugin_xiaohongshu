"""小红书插件 - Playwright 浏览器管理模块

负责：
- 管理 Playwright chromium 浏览器实例生命周期
- 处理小红书网页版扫码登录流程
- 持久化/恢复 storage_state（cookie + localStorage）
- 提供已登录的 BrowserContext 给操作模块使用

适配 AstrBot 容器环境：优先用系统 chromium，其次 Playwright 自带。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

logger = logging.getLogger("xhs.browser")

XHS_URL = "https://www.xiaohongshu.com"
MESSAGES_URL = "https://www.xiaohongshu.com/explore"
QR_TIMEOUT = 180  # 等待扫码超时（秒）

# 反检测脚本：绕过小红书基础反爬
STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = {
    runtime: {},
    loadTimes: function() {},
    csi: function() {},
    app: {}
};
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications'
        ? Promise.resolve({ state: Notification.permission })
        : originalQuery(parameters)
);
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});
Object.defineProperty(navigator, 'languages', {
    get: () => ['zh-CN', 'zh', 'en']
});
Object.defineProperty(navigator, 'platform', {
    get: () => 'MacIntel'
});
"""

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-web-security",
    "--disable-features=BlockInsecurePrivateNetworkRequests",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-gpu",
]


class BrowserManager:
    """Playwright 浏览器管理器。"""

    def __init__(self, data_dir: str, headless: bool = True) -> None:
        self._data_dir = Path(data_dir)
        self._headless = headless
        self._storage_path = self._data_dir / "xhs_storage.json"
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._pages: list = []  # 所有已打开页面（含 popup），按打开顺序

    # ── 属性 ────────────────────────────────────────────────────────
    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserContext 未初始化，请先 start()")
        return self._context

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Page 未初始化，请先 start()")
        return self._page

    @property
    def active_page(self) -> Page:
        """当前应操作的页面：优先最近打开且未关闭的页面（含 popup 弹窗）。

        小红书登录/验证码可能以新窗口形式弹出，只操作主页面会漏掉。
        """
        for p in reversed(self._pages):
            try:
                if not p.is_closed():
                    return p
            except Exception:
                continue
        return self._page

    @property
    def is_authenticated(self) -> bool:
        """是否已保存登录态文件。"""
        return self._storage_path.exists()

    @property
    def is_started(self) -> bool:
        """浏览器实例是否已启动。"""
        return self._page is not None

    def _storage_state_path(self) -> Path:
        return self._storage_path

    # ── 生命周期 ────────────────────────────────────────────────────
    async def start(self) -> None:
        """启动浏览器并创建 Context，若有登录态则恢复。"""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()

        args = list(LAUNCH_ARGS)
        if self._headless:
            args.append("--headless=new")

        user_data_dir = tempfile.mkdtemp(prefix="xhs_plugin_")
        ua = (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        )

        base_kwargs = dict(
            user_data_dir=user_data_dir,
            headless=self._headless,
            args=args,
            viewport={"width": 1440, "height": 900},
            user_agent=ua,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        # 1) 优先用环境变量指定 chromium
        env_path = os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", "")
        # 2) 常见系统路径
        candidates = []
        if env_path:
            candidates.append(env_path)
        candidates += [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/snap/bin/chromium",
        ]

        launched = False
        for exe in candidates:
            if not os.path.exists(exe):
                continue
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    executable_path=exe, **base_kwargs
                )
                logger.info(f"通过系统 Chromium 启动成功: {exe}")
                launched = True
                break
            except Exception as exc:
                logger.warning(f"Chromium {exe} 启动失败: {exc}")
                continue

        if not launched:
            # 回退：Playwright 自带 chromium
            try:
                self._context = await self._playwright.chromium.launch_persistent_context(
                    **base_kwargs
                )
                logger.info("通过 Playwright 自带 Chromium 启动成功")
                launched = True
            except Exception as exc:
                logger.error(f"所有浏览器启动方式均失败: {exc}")
                await self._safe_stop()
                raise RuntimeError(f"浏览器启动失败: {exc}")

        self._browser = self._context.browser
        try:
            await self._context.add_init_script(STEALTH_SCRIPT)
        except Exception:
            pass
        self._page = await self._context.new_page()
        self._page.set_default_timeout(30000)
        self._pages = [self._page]
        # 监听新开的页面（popup 弹窗，如验证码窗口）
        try:
            self._context.on("page", lambda p: self._pages.append(p))
        except Exception:
            pass

        # 恢复登录态
        if self.is_authenticated:
            try:
                state = json.loads(self._storage_path.read_text(encoding="utf-8"))
                cookies = state.get("cookies", [])
                if cookies:
                    await self._context.add_cookies(cookies)
                    logger.info(f"已恢复 {len(cookies)} 个 cookies")
            except Exception as exc:
                logger.warning(f"登录态读取失败，将重新登录: {exc}")

    async def ensure_authenticated(self) -> bool:
        """确保登录，返回是否已登录（未登录会进入扫码流程等待）。"""
        if self._page is None:
            await self.start()
        if not self.is_authenticated:
            logger.info("未检测到登录态，开始扫码登录")
            await self._login_flow()
            return True

        # 有 storage_state，快速验证是否有效
        try:
            await self._page.goto(XHS_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            if await self._check_login_status():
                logger.info("登录态有效")
                return True
        except Exception as exc:
            logger.warning(f"登录态验证失败: {exc}")

        logger.warning("登录态已过期，需要重新扫码")
        await self._login_flow()
        return True

    # ── 扫码登录 ────────────────────────────────────────────────────
    async def _login_flow(self) -> None:
        """打开登录页 → 等待二维码 → 保存二维码截图 → 轮询登录成功。"""
        await self._page.goto(XHS_URL, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(3)

        # 点击登录按钮
        login_selectors = [
            "text=登录",
            ".login-button",
            "[class*='login']",
            "button:has-text('登录')",
            "span:has-text('登录')",
            "a:has-text('登录')",
        ]
        clicked = False
        for sel in login_selectors:
            try:
                btn = await self._page.wait_for_selector(sel, timeout=4000)
                if btn:
                    await btn.click()
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            logger.info("未找到登录按钮，假设登录面板已显示")
        await asyncio.sleep(2)

        # 等待二维码并截图
        await self._wait_qr_and_screenshot()

        # 等待登录成功
        await self._wait_for_login()

        # 保存登录态
        await self._save_storage_state()
        logger.info("扫码登录完成，登录态已保存")

    async def _wait_qr_and_screenshot(self, timeout: int = 30) -> str:
        """等待二维码出现并截图，返回截图路径。

        优先对二维码元素区域截图，找不到二维码时抛异常，由上层返回明确错误。
        """
        screenshot_path = self._data_dir / "xhs_login_qrcode.png"
        qr_selectors = [
            "img[src*='qrcode']",
            "img[class*='qrcode']",
            "[class*='qrcode'] img",
            "canvas",
            "img[alt*='扫码']",
            "[class*='login'] [class*='code'] img",
            "[class*='qr-code']",
            "[class*='qrcode']",
        ]
        el = None
        deadline = asyncio.get_event_loop().time() + timeout
        for qs in qr_selectors:
            try:
                remain = max(3, int(deadline - asyncio.get_event_loop().time()))
                candidate = await self._page.wait_for_selector(qs, timeout=remain * 1000)
                if candidate:
                    el = candidate
                    break
            except Exception:
                continue
        if el is None:
            logger.warning("未检测到二维码元素，扫码流程判定失败")
            raise RuntimeError("未在页面中找到二维码，可能被小红书风控拦截或页面结构变化")
        await asyncio.sleep(0.8)
        try:
            await el.screenshot(path=str(screenshot_path))
        except Exception:
            try:
                await self._page.screenshot(path=str(screenshot_path))
            except Exception as exc:
                logger.warning(f"截图失败: {exc}")
        return str(screenshot_path)

    async def wait_qr_screenshot(self, timeout: int = 30) -> str:
        """对外：确保登录页二维码已加载并截图（供指令用）。"""
        await self._wait_qr_and_screenshot(timeout=timeout)
        return str(self._data_dir / "xhs_login_qrcode.png")

    async def _wait_for_login(self, timeout: int = QR_TIMEOUT) -> None:
        """轮询检测登录成功。"""
        start = asyncio.get_event_loop().time()
        while True:
            elapsed = asyncio.get_event_loop().time() - start
            if elapsed > timeout:
                raise TimeoutError(f"扫码登录超时（{timeout}秒），请重试")
            try:
                if await self._check_login_status():
                    return
            except Exception:
                pass
            # 也检查页面元素
            try:
                logged_in_selectors = [
                    "[class*='user-info']",
                    "[class*='avatar']",
                    "img[class*='avatar']",
                ]
                for sel in logged_in_selectors:
                    try:
                        el = await self._page.wait_for_selector(sel, timeout=2000)
                        if el:
                            return
                    except Exception:
                        continue
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _check_login_status(self) -> bool:
        """严格校验登录态：只认非空的 web_session（游客 cookie 不算登录）。"""
        try:
            cookies = await self._context.cookies()
            for c in cookies:
                if c["name"] == "web_session" and c.get("value"):
                    return True
        except Exception:
            pass
        try:
            token = await self._page.evaluate("() => localStorage.getItem('web_session')")
            if token and len(token) > 10:
                return True
        except Exception:
            pass
        return False

    async def verify_online(self) -> bool:
        """页面级强校验：访问主页，确认真的处于登录态（防止游客态误判）。

        先做 cookie 快速判断，再做页面元素判断，两者都过才算真登录。
        """
        try:
            if not self._page:
                await self.start()
            await self._page.goto(XHS_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(2.5)
            # 1) cookie 必须有非空 sessionid
            ok_cookie = False
            try:
                cookies = await self._context.cookies()
                for c in cookies:
                    if c["name"] == "web_session" and c.get("value"):
                        ok_cookie = True
                        break
            except Exception:
                pass
            if not ok_cookie:
                logger.info("verify_online: 无有效 web_session，判定未登录")
                return False
            # 2) 页面元素：登录后才有头像/用户信息，未登录会有登录按钮
            try:
                logged_in = await self._page.locator(
                    "[class*='user-info'], [class*='avatar'], [class*='userAvatar']"
                ).first.is_visible(timeout=5000)
            except Exception:
                logged_in = False
            if logged_in:
                return True
            # 3) 兜底：页面里没有登录按钮（登录态下通常不显示登录入口）
            try:
                login_btn = await self._page.locator(
                    "text=登录, [class*='login-button'], [class*='loginBtn']"
                ).first.is_visible(timeout=3000)
            except Exception:
                login_btn = True
            return not login_btn
        except Exception as exc:
            logger.warning(f"verify_online 异常: {exc}")
            return False

    async def _save_storage_state(self) -> None:
        """保存 storage_state 到文件。"""
        state = await self._context.storage_state()
        self._storage_path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def reconnect(self) -> bool:
        """掉线后自动重连：重新注入已保存的登录态并验证。

        浏览器未启动时直接 start()（内部会恢复登录态）；
        已启动时先清空旧 cookie 再注入文件里的 cookie，然后打开主页校验。
        返回 True 表示恢复成功。
        """
        try:
            if self._page is None:
                await self.start()
            if not self.is_authenticated:
                return False
            # 重新注入已保存的登录态
            state = json.loads(self._storage_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies", [])
            if not cookies:
                return False
            try:
                await self._context.clear_cookies()
            except Exception:
                pass
            await self._context.add_cookies(cookies)
            await self._page.goto(XHS_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            if await self._check_login_status():
                await self._save_storage_state()
                logger.info("reconnect: 登录态恢复成功")
                return True
            logger.warning("reconnect: cookie 注入后仍无有效 web_session")
            return False
        except Exception as exc:
            logger.warning(f"reconnect 异常: {exc}")
            return False

    async def apply_cookies(self, cookie_str: str) -> tuple[bool, str]:
        """手动导入 Cookie 登录（绕开扫码风控）。

        支持两种格式：
        1. 浏览器复制的一行：name=value; name2=value2; ...
        2. JSON 数组：[{"name": "...", "value": "...", "domain": "..."}, ...]
        """
        cookie_str = (cookie_str or "").strip()
        if not cookie_str:
            return False, "Cookie 为空"

        cookies = []
        # 格式2：JSON 数组
        if cookie_str.startswith("["):
            try:
                raw = json.loads(cookie_str)
                for item in raw:
                    if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                        cookies.append(item)
            except Exception as exc:
                return False, f"JSON 解析失败：{exc}"
        else:
            # 格式1：name=value; ... 忽略属性如 Domain=/ 等
            for seg in cookie_str.split(";"):
                seg = seg.strip()
                if not seg or "=" not in seg:
                    continue
                k, _, v = seg.partition("=")
                k = k.strip()
                if not k or k.lower() in ("domain", "path", "expires", "max-age", "samesite", "secure", "httponly", "priority"):
                    continue
                cookies.append({"name": k, "value": v.strip()})

        if not cookies:
            return False, "没有解析出有效 Cookie 项"

        # 确保浏览器已启动
        if not self.is_started:
            try:
                await self.start()
            except Exception as exc:
                return False, f"浏览器启动失败：{exc}"

        try:
            # 先清掉旧 cookie，再注入新 cookie（domain 覆盖 xhs.com 主域）
            try:
                await self._context.clear_cookies()
            except Exception:
                pass
            valid = []
            for c in cookies:
                domain = c.get("domain") or ".xhs.com"
                if domain.startswith("."):
                    domain = domain[1:]
                valid.append({
                    "name": c["name"],
                    "value": str(c["value"]),
                    "domain": domain,
                    "path": c.get("path") or "/",
                })
            await self._context.add_cookies(valid)
            # 打开首页验证登录态
            await self._page.goto(XHS_URL, wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)
            if not await self._check_login_status():
                return False, "Cookie 已注入，但未检测到有效 web_session，请确认复制的 Cookie 是登录后的"
            # 页面级强校验
            online = await self.verify_online()
            if not online:
                return False, "Cookie 有 web_session 但页面校验未通过，可能已过期，请重新复制"
            await self._save_storage_state()
            return True, f"登录成功，已注入 {len(valid)} 条 Cookie 并保存登录态"
        except Exception as exc:
            logger.exception(f"[xhs] Cookie 导入失败: {exc}")
            return False, f"Cookie 导入失败：{exc}"
        logger.info(f"登录态已保存到 {self._storage_path}")

    async def navigate(self, url: str) -> None:
        """导航并等待。"""
        await self.active_page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

    async def go_back(self) -> None:
        """浏览器后退。"""
        try:
            await self.active_page.go_back(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

    async def go_forward(self) -> None:
        """浏览器前进。"""
        try:
            await self.active_page.go_forward(wait_until="domcontentloaded", timeout=20000)
        except Exception:
            pass
        await asyncio.sleep(1.5)

    async def current_url(self) -> str:
        """当前页面 URL。"""
        try:
            return self.active_page.url
        except Exception:
            return ""

    async def page_title(self) -> str:
        """当前页面标题。"""
        try:
            return await self.active_page.title()
        except Exception:
            return ""

    async def dump_cookies(self) -> dict:
        """抓取当前浏览器全部 cookie（含 HttpOnly），返回 {ok, count, web_session, cookies}。"""
        try:
            cookies = await self._context.cookies()
            has_session = any(
                c.get("name") == "sessionid" and c.get("value") for c in cookies
            )
            return {
                "ok": True,
                "count": len(cookies),
                "sessionid": has_session,
                "cookies": cookies,
            }
        except Exception as exc:
            return {"ok": False, "count": 0, "sessionid": False, "error": str(exc), "cookies": []}

    # ── 远程浏览器交互（WUI 面板用） ──────────────────────────────────────
    async def _hit_test(self, x: float, y: float) -> dict:
        """坐标命中测试：穿透 iframe 找到真实元素。

        小红书登录框/验证码经常在 iframe 里，顶层 elementFromPoint 只能拿到
        iframe 外壳。这里递归进 iframe 内部（同域）找真实元素，拿不到就
        退化为顶层信息，点击仍走物理事件（Playwright 鼠标事件能命中 iframe）。

        返回:
            {found, in_iframe, frame_index, tag, id, cls, text, disabled, clickable, w, h}
        """
        try:
            return await self.active_page.evaluate(
                """([x, y]) => {
                    const describe = (el, inIframe) => {
                        if (!el) return null;
                        let target = el;
                        for (let i = 0; i < 5; i++) {
                            if (!target) break;
                            const t = target.tagName ? target.tagName.toLowerCase() : '';
                            if (['button','a','input','textarea','select','label'].includes(t)) break;
                            if (t === 'div' || t === 'span' || t === 'li') {
                                const cs = window.getComputedStyle(target);
                                if (target.getAttribute('role') || cs.cursor === 'pointer') break;
                            }
                            target = target.parentElement;
                        }
                        const el2 = target || el;
                        const cs2 = window.getComputedStyle(el2);
                        const rect = el2.getBoundingClientRect();
                        return {
                            found: true,
                            in_iframe: !!inIframe,
                            frame_index: inIframe || 0,
                            tag: el2.tagName ? el2.tagName.toLowerCase() : '',
                            id: el2.id || '',
                            cls: (el2.className && typeof el2.className === 'string') ? el2.className.split(' ').slice(0, 3).join('.') : '',
                            text: (el2.textContent || '').trim().slice(0, 30),
                            disabled: el2.disabled === true || el2.getAttribute('aria-disabled') === 'true',
                            clickable: cs2.cursor === 'pointer' || !!el2.getAttribute('role') || ['button','a','input','textarea','select'].includes((el2.tagName || '').toLowerCase()),
                            w: Math.round(rect.width), h: Math.round(rect.height),
                        };
                    };
                    // 顶层命中
                    let el = document.elementFromPoint(x, y);
                    if (!el) return { found: false };
                    // 若命中 iframe，尝试进入内部（同域可访问；跨域抛错则退回顶层）
                    let frameIndex = 0;
                    let cursor = el;
                    while (cursor && cursor.tagName && cursor.tagName.toLowerCase() === 'iframe') {
                        try {
                            const doc = cursor.contentDocument;
                            if (!doc) break;
                            const r = cursor.getBoundingClientRect();
                            const ix = x - r.left, iy = y - r.top;
                            const inner = doc.elementFromPoint(ix, iy);
                            if (!inner) break;
                            frameIndex++;
                            el = inner;
                            cursor = inner;
                        } catch (e) {
                            break; // 跨域 iframe，无法访问内部
                        }
                    }
                    const info = describe(el, frameIndex > 0);
                    info.frame_index = frameIndex;
                    return info;
                }""",
                [x, y],
            )
        except Exception as exc:
            logger.warning(f"命中测试失败: {exc}")
            return {"found": False, "err": str(exc)}

    async def _js_click_fallback(self, x: float, y: float) -> bool:
        """JS 兜底点击：对命中元素直接触发 click 事件。

        小红书的按钮大多是 React 合成事件，物理点击通常有效，但个别遮罩/
        iframe 场景物理事件会被吞，用 JS 直接 dispatch 兜底一次。
        """
        try:
            ok = await self.active_page.evaluate(
                """([x, y]) => {
                    const find = (doc, cx, cy) => {
                        let el = doc.elementFromPoint(cx, cy);
                        if (!el) return null;
                        let t = el;
                        for (let i = 0; i < 5 && t; i++) {
                            const tag = (t.tagName || '').toLowerCase();
                            if (['button','a','input','textarea','select','label'].includes(tag)) return t;
                            if (tag === 'div' || tag === 'span' || tag === 'li') {
                                const cs = window.getComputedStyle(t);
                                if (t.getAttribute('role') || cs.cursor === 'pointer') return t;
                            }
                            t = t.parentElement;
                        }
                        return el;
                    };
                    let el = find(document, x, y);
                    if (!el) return false;
                    // 若是 iframe 且同域，进内部找
                    let frameIndex = 0;
                    while (el.tagName && el.tagName.toLowerCase() === 'iframe') {
                        try {
                            const doc = el.contentDocument;
                            if (!doc) break;
                            const r = el.getBoundingClientRect();
                            const inner = find(doc, x - r.left, y - r.top);
                            if (!inner) break;
                            frameIndex++;
                            el = inner;
                        } catch (e) { break; }
                    }
                    try {
                        if (typeof el.click === 'function') {
                            el.click();
                            return true;
                        }
                        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
                        return true;
                    } catch (e) {
                        return false;
                    }
                }""",
                [x, y],
            )
            return bool(ok)
        except Exception:
            return False

    async def mouse_click(self, x: float, y: float) -> dict:
        """在指定坐标点击（hover → 物理点击 → JS 兜底）。

        流程：
        1. 鼠标移动到目标位置停留（hover 态元素才能点到）
        2. 物理 mouse.click（Playwright 鼠标事件能穿透 iframe）
        3. JS 兜底：对命中元素再触发一次 click（覆盖 React/遮罩场景）
        4. 返回命中元素诊断信息（含 iframe 内元素）
        """
        diag = {"ok": True, "tag": "", "id": "", "cls": "", "text": "", "disabled": False, "iframe": False, "clickable": True, "frame_index": 0}
        try:
            await self.active_page.mouse.move(x, y)
            await asyncio.sleep(0.12)
            info = await self._hit_test(x, y)
            if isinstance(info, dict):
                for k in ("tag", "id", "cls", "text", "disabled", "clickable", "w", "h", "frame_index"):
                    if k in info and info[k] not in (None, ""):
                        diag[k] = info[k]
                diag["iframe"] = bool(info.get("in_iframe"))
        except Exception as exc:
            logger.warning(f"点击元素探测失败: {exc}")
        try:
            await self.active_page.mouse.click(x, y)
            await asyncio.sleep(0.35)
        except Exception as exc:
            logger.warning(f"物理点击失败: {exc}")
            diag["ok"] = False
            diag["err"] = str(exc)
        # JS 兜底（物理点击后补一次，确保 React 事件触发）
        if diag["ok"]:
            try:
                await self._js_click_fallback(x, y)
                await asyncio.sleep(0.25)
            except Exception as exc:
                logger.warning(f"JS 兜底点击失败: {exc}")
        return diag

    async def mouse_drag(
        self, x1: float, y1: float, x2: float, y2: float, steps: int = 12
    ) -> None:
        """从 (x1,y1) 拖动到 (x2,y2)，模拟人类滑块拖动轨迹。"""
        await self.active_page.mouse.move(x1, y1)
        await asyncio.sleep(0.15)
        await self.active_page.mouse.down()
        # 分段移动 + 轻微抖动，更像真人
        for i in range(1, steps + 1):
            progress = i / steps
            # ease-out 曲线：先快后慢
            eased = 1 - (1 - progress) ** 2
            cx = x1 + (x2 - x1) * eased
            cy = y1 + (y2 - y1) * eased + (0.5 - (i % 3) * 0.5) * 1.5
            await self.active_page.mouse.move(cx, cy)
            await asyncio.sleep(0.02 + (i % 4) * 0.008)
        await self.active_page.mouse.move(x2, y2)
        await asyncio.sleep(0.1)
        await self.active_page.mouse.up()
        await asyncio.sleep(0.4)

    async def mouse_move(self, x: float, y: float) -> None:
        """仅移动鼠标到坐标（悬停）。"""
        await self.active_page.mouse.move(x, y)
        await asyncio.sleep(0.2)

    async def scroll_page(self, dx: float = 0, dy: float = 300) -> None:
        """滚动页面。dy 正数向下滚。"""
        await self.active_page.mouse.wheel(dx, dy)
        await asyncio.sleep(0.3)

    async def type_text(self, x: float, y: float, text: str) -> None:
        """点击坐标后输入文本（含 iframe 内聚焦兜底）。

        小红书短信验证码输入框在登录 iframe 内，物理点击能聚焦（Playwright
        鼠标事件穿透 iframe），这里再补一次 JS 聚焦，确保键盘输入进去。
        """
        await self.active_page.mouse.move(x, y)
        await asyncio.sleep(0.15)
        await self.active_page.mouse.click(x, y)
        await asyncio.sleep(0.3)
        # JS 兜底聚焦：同域 iframe 内的输入框也能拿到焦点
        try:
            await self.active_page.evaluate(
                """([x, y]) => {
                    const find = (doc, cx, cy) => {
                        let el = doc.elementFromPoint(cx, cy);
                        if (!el) return null;
                        let t = el;
                        for (let i = 0; i < 6 && t; i++) {
                            const tag = (t.tagName || '').toLowerCase();
                            if (['input','textarea','select','button','a'].includes(tag)) return t;
                            t = t.parentElement;
                        }
                        return el;
                    };
                    let el = find(document, x, y);
                    let guard = 0;
                    while (el && el.tagName && el.tagName.toLowerCase() === 'iframe' && guard < 5) {
                        try {
                            const doc = el.contentDocument;
                            if (!doc) break;
                            const r = el.getBoundingClientRect();
                            const inner = find(doc, x - r.left, y - r.top);
                            if (!inner) break;
                            el = inner;
                            guard++;
                        } catch (e) { break; }
                    }
                    try {
                        if (el && typeof el.focus === 'function') {
                            el.focus();
                            if (el.tagName && ['INPUT','TEXTAREA'].includes(el.tagName)) {
                                el.select && el.select();
                            }
                        }
                    } catch (e) {}
                }""",
                [x, y],
            )
        except Exception:
            pass
        await asyncio.sleep(0.2)
        await self.active_page.keyboard.type(text, delay=30)
        await asyncio.sleep(0.3)

    async def find_inputs(self) -> list:
        """扫描当前活跃页面所有可见输入框（含跨域 iframe），返回列表。

        基于 Playwright 原生 page.frames 遍历——不受同源策略限制，
        小红书登录/验证码的跨域 iframe 也能扫到。

        每项: {
            index, frame_index, frame_local_idx, frame_url,
            tag, type, placeholder, x, y, w, h
        }
        x/y 为该输入框中心点在其所在 frame 视口内的坐标（展示用，
        实际操作走 frame_index + frame_local_idx，不依赖坐标）。
        """
        try:
            results = []
            page = self.active_page
            for fi, frame in enumerate(page.frames):
                try:
                    infos = await frame.locator(
                        "input:visible, textarea:visible"
                    ).evaluate_all(
                        """els => els.map((el) => {
                            const r = el.getBoundingClientRect();
                            return {
                                tag: el.tagName.toLowerCase(),
                                type: el.type || 'text',
                                placeholder: el.placeholder || el.name || el.id || '',
                                x: Math.round(r.left + r.width / 2),
                                y: Math.round(r.top + r.height / 2),
                                w: Math.round(r.width),
                                h: Math.round(r.height),
                            };
                        })"""
                    )
                    for li, info in enumerate(infos):
                        info["frame_index"] = fi
                        info["frame_local_idx"] = li
                        try:
                            info["frame_url"] = (frame.url or "")[:100]
                        except Exception:
                            info["frame_url"] = ""
                        results.append(info)
                except Exception:
                    continue
            for i, r in enumerate(results):
                r["index"] = i
            return results
        except Exception as exc:
            logger.warning(f"扫描输入框失败: {exc}")
            return []

    async def focus_input(self, index: int) -> dict:
        """聚焦指定索引的输入框（与 find_inputs 顺序一致），返回 {ok, x, y, info}。

        基于 Playwright 原生 frame locator 操作——跨域 iframe 也能点，
        不再依赖坐标 + JS contentDocument（跨域被浏览器禁止）。
        """
        inputs = await self.find_inputs()
        if index < 0 or index >= len(inputs):
            return {"ok": False, "err": f"输入框索引 {index} 超出范围（共 {len(inputs)} 个）"}
        info = inputs[index]
        try:
            frame = self.active_page.frames[info["frame_index"]]
            loc = frame.locator("input:visible, textarea:visible").nth(info["frame_local_idx"])
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(0.2)
            await loc.click(timeout=5000)
            await asyncio.sleep(0.3)
            # JS 兜底聚焦（frame 内直接执行，同域/跨域都行）
            try:
                await frame.evaluate(
                    """() => {
                        const el = document.activeElement;
                        if (el && typeof el.focus === 'function') {
                            el.focus();
                            if (el.tagName && ['INPUT','TEXTAREA'].includes(el.tagName)) {
                                el.select && el.select();
                            }
                            return true;
                        }
                        return false;
                    }"""
                )
            except Exception:
                pass
            await asyncio.sleep(0.2)
            return {"ok": True, "x": float(info.get("x", 0)), "y": float(info.get("y", 0)), "info": info}
        except Exception as exc:
            logger.warning(f"聚焦输入框失败: {exc}")
            return {"ok": False, "err": str(exc)}

    async def type_into(self, index: int, text: str) -> dict:
        """直接向指定索引的输入框输入文本（含跨域 iframe）。

        基于 Playwright 原生 frame locator——先点击聚焦再逐字输入，
        不依赖坐标换算，小红书跨域验证码 iframe 也能输。
        """
        inputs = await self.find_inputs()
        if index < 0 or index >= len(inputs):
            return {"ok": False, "err": f"输入框索引 {index} 超出范围（共 {len(inputs)} 个）"}
        info = inputs[index]
        try:
            frame = self.active_page.frames[info["frame_index"]]
            loc = frame.locator("input:visible, textarea:visible").nth(info["frame_local_idx"])
            try:
                await loc.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(0.2)
            # 先清空再点（Ctrl+A 全选）
            try:
                await loc.click(timeout=5000)
                await asyncio.sleep(0.15)
                await frame.keyboard.press("Control+a")
                await asyncio.sleep(0.1)
            except Exception:
                pass
            await frame.keyboard.type(text, delay=25)
            await asyncio.sleep(0.3)
            return {"ok": True, "info": info}
        except Exception as exc:
            logger.warning(f"向输入框输入失败: {exc}")
            return {"ok": False, "err": str(exc)}

    async def click_text(self, text: str) -> dict:
        """按文本点击页面按钮/链接（含跨域 iframe 遍历）。

        用于点「获取验证码」「同意并继续」这类文案按钮。
        基于 Playwright 原生 frame locator——跨域 iframe 也能点。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "err": "缺少文本参数"}
        page = self.active_page
        selectors = [
            f"button:has-text('{text}')",
            f"a:has-text('{text}')",
            f"text={text}",
            f"span:has-text('{text}')",
            f"div:has-text('{text}')",
        ]
        for fi, frame in enumerate(page.frames):
            for sel in selectors:
                try:
                    loc = frame.locator(sel).first
                    if await loc.is_visible(timeout=1200):
                        try:
                            await loc.scroll_into_view_if_needed(timeout=2000)
                        except Exception:
                            pass
                        await asyncio.sleep(0.2)
                        await loc.click(timeout=3000)
                        await asyncio.sleep(0.5)
                        return {"ok": True, "frame": fi, "selector": sel}
                except Exception:
                    continue
        return {"ok": False, "err": f"没找到可点击的「{text}」"}

    async def press_key(self, key: str) -> None:
        """按键，如 Enter / Tab / Escape。"""
        await self.active_page.keyboard.press(key)
        await asyncio.sleep(0.3)

    async def reload_page(self) -> None:
        """刷新当前页面。"""
        try:
            await self.active_page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(2)

    # ── 依赖检测与自动安装 ────────────────────────────────────────
    @staticmethod
    def check_deps() -> dict:
        """检测浏览器运行依赖是否就绪。

        返回:
            {"playwright": bool, "chromium": bool, "detail": str, "ok": bool}
        """
        # 1) playwright 包
        pw_ok = False
        try:
            import playwright  # noqa: F401

            pw_ok = True
        except Exception:
            pw_ok = False

        # 2) chromium 可执行文件
        chromes = [
            os.environ.get("PLAYWRIGHT_CHROMIUM_PATH", ""),
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome",
            "/snap/bin/chromium",
        ]
        # playwright 自带的浏览器目录（按平台推断）
        try:
            import playwright

            pw_root = Path(playwright.__file__).resolve().parent.parent
            candidates = [
                pw_root / ".local-browsers" / "chromium-*" / "chrome-linux" / "chrome",
                pw_root / ".local-browsers" / "chromium-*" / "chrome",
                Path.home() / ".cache" / "ms-playwright" / "chromium-*" / "chrome-linux" / "chrome",
                Path.home() / ".cache" / "ms-playwright" / "chromium-*" / "chrome",
            ]
        except Exception:
            candidates = []

        found = None
        for c in chromes:
            if c and os.path.exists(c):
                found = c
                break
        if found is None:
            import glob

            for pat in candidates:
                try:
                    hits = glob.glob(str(pat))
                    if hits:
                        found = hits[0]
                        break
                except Exception:
                    continue

        detail = []
        detail.append("playwright: " + ("✅" if pw_ok else "❌ 未安装"))
        detail.append("chromium: " + ("✅ " + found if found else "❌ 未找到"))
        return {
            "playwright": pw_ok,
            "chromium": found is not None,
            "browser_path": found or "",
            "detail": " | ".join(detail),
            "ok": pw_ok and found is not None,
        }

    @staticmethod
    async def install_deps() -> dict:
        """自动安装浏览器依赖（playwright 包 + chromium）。

        通过 subprocess 依次执行：
        1) pip install playwright（缺失时）
        2) python -m playwright install chromium（缺失时）
        3) playwright install-deps chromium（可选系统库，失败忽略）

        返回安装过程日志与最终状态。
        """
        import subprocess
        import sys

        logs: list[str] = []
        deps = BrowserManager.check_deps()

        async def _run(cmd: list[str], timeout: int = 600) -> bool:
            logs.append("$ " + " ".join(cmd))
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc.kill()
                    logs.append("⏱ 命令超时，已终止")
                    return False
                text = (stdout or b"").decode("utf-8", errors="replace")
                tail = "\n".join(text.strip().splitlines()[-15:])
                logs.append(tail if tail else "(无输出)")
                return proc.returncode == 0
            except Exception as exc:
                logs.append(f"❌ 执行失败: {exc}")
                return False

        # 1) 装 playwright 包
        if not deps["playwright"]:
            ok = await _run([sys.executable, "-m", "pip", "install", "-U", "playwright"], 600)
            if not ok:
                return {"ok": False, "logs": logs, "detail": "playwright 安装失败，请手动执行 pip install playwright"}

        # 2) 装 chromium
        if not deps["chromium"]:
            ok = await _run(
                [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
                900,
            )
            if not ok:
                # 兜底：不带 --with-deps 再试一次（部分容器无 apt 权限）
                ok = await _run([sys.executable, "-m", "playwright", "install", "chromium"], 900)

        final = BrowserManager.check_deps()
        logs.append("最终状态: " + final["detail"])
        return {"ok": final["ok"], "logs": logs, "detail": final["detail"]}

    async def close(self) -> None:
        """清理资源。"""
        await self._safe_stop()

    async def _safe_stop(self) -> None:
        try:
            if self._page:
                await self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

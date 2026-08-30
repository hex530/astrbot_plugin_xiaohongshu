"""小红书网页版操作客户端

负责：
- 搜索笔记 / 笔记详情 / 评论抓取
- 点赞 / 收藏 / 评论 / 关注
- 浏览推荐流（养号基础）
- 状态查询

依赖 xhs_browser.BrowserManager 提供已登录浏览器。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Optional

logger = logging.getLogger("xhs.client")

XHS_HOME = "https://www.xiaohongshu.com"
SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={kw}&type=51"
EXPLORE_URL = "https://www.xiaohongshu.com/explore/{note_id}"
USER_URL = "https://www.xiaohongshu.com/user/profile/{user_id}"

# 搜索页笔记卡片选择器（多级兜底）
NOTE_CARD_SELECTORS = [
    "section.note-item",
    "div.note-item",
    "a[href*='/explore/']",
    "div[class*='note-item']",
    "div[class*='feeds'] a[href*='/explore/']",
]

# 点赞 / 收藏 / 评论 图标（笔记详情页）
LIKE_SELECTORS = [
    "svg[class*='like']",
    "span[class*='like']",
    "div[class*='like']",
    "svg[data-testid*='like']",
]
COLLECT_SELECTORS = [
    "svg[class*='collect']",
    "span[class*='collect']",
    "div[class*='collect']",
    "svg[data-testid*='collect']",
]
COMMENT_SELECTORS = [
    "div[class*='comment-input']",
    "textarea",
    "div[contenteditable='true']",
    "input[placeholder*='评论']",
]
SEND_SELECTORS = [
    "button:has-text('发布')",
    "button:has-text('发送')",
    "div[class*='send']",
    "button[class*='submit']",
]
FOLLOW_SELECTORS = [
    "button:has-text('关注')",
    "div[class*='follow']",
    "span:has-text('关注')",
]

# 笔记标题 / 作者
TITLE_SELECTORS = [
    "span[class*='title']",
    "div[class*='title']",
    "h1",
    "#detail-title",
]
AUTHOR_SELECTORS = [
    "a[class*='author']",
    "span[class*='author']",
    "div[class*='author']",
    "a[href*='/user/profile/']",
]


class XhsClient:
    """小红书网页版客户端。"""

    def __init__(self, browser_manager) -> None:
        self._bm = browser_manager

    # ── 页面准备 ────────────────────────────────────────────────
    async def ensure_ready(self) -> bool:
        """确保浏览器已启动且已登录。"""
        try:
            return await self._bm.ensure_authenticated()
        except Exception as exc:
            logger.error(f"ensure_ready 失败: {exc}")
            return False

    async def navigate(self, url: str) -> None:
        """导航到指定页面（带基础等待）。"""
        await self._bm.navigate(url)
        await asyncio.sleep(2.5)

    # ── 状态 ────────────────────────────────────────────────────
    async def status(self) -> dict:
        """当前登录账号信息（从页面提取，失败返回空）。"""
        try:
            if not self._bm.is_started:
                return {}
            page = self._bm.page
            name = ""
            try:
                # 小红书登录后右上角有头像，hover 或读取 aria-label
                avatar = await page.query_selector("[class*='user-info'] img, img[class*='avatar']")
                if avatar:
                    alt = await avatar.get_attribute("alt") or ""
                    name = alt.strip()
            except Exception:
                pass
            return {"nickname": name}
        except Exception as exc:
            logger.debug(f"status 失败: {exc}")
            return {}

    # ── 搜索 ────────────────────────────────────────────────────
    async def search_notes(self, keyword: str, limit: int = 8) -> list[dict]:
        """搜索笔记，返回 [{title, author, link, cover, likes}]。"""
        page = self._bm.page
        kw = keyword.strip()
        if not kw:
            return []
        url = SEARCH_URL.format(kw=kw)
        try:
            await self.navigate(url)
        except Exception as exc:
            logger.error(f"打开搜索页失败: {exc}")
            return []

        # 兜底：若 URL 带中文被编码问题，尝试直接输入搜索框
        try:
            if "keyword=" not in page.url or kw not in page.url:
                box = await page.query_selector("input[placeholder*='搜索']")
                if box:
                    await box.fill(kw)
                    await box.press("Enter")
                    await asyncio.sleep(3)
        except Exception:
            pass

        notes: list[dict] = []
        try:
            await page.wait_for_selector(NOTE_CARD_SELECTORS[0], timeout=12000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        seen = set()
        for sel in NOTE_CARD_SELECTORS:
            try:
                cards = await page.query_selector_all(sel)
                for card in cards:
                    try:
                        href = ""
                        a = await card.query_selector("a[href*='/explore/']") or card
                        href = await a.get_attribute("href") or ""
                        if href and not href.startswith("http"):
                            href = "https://www.xiaohongshu.com" + href
                        if not href or href in seen:
                            continue
                        seen.add(href)
                        title = (await self._text(card, TITLE_SELECTORS)) or ""
                        author = (await self._text(card, AUTHOR_SELECTORS)) or ""
                        img = await card.query_selector("img")
                        cover = await img.get_attribute("src") if img else ""
                        notes.append({
                            "title": title,
                            "author": author,
                            "link": href,
                            "cover": cover,
                            "likes": "",
                        })
                        if len(notes) >= limit:
                            break
                    except Exception:
                        continue
                if notes:
                    break
            except Exception:
                continue
        return notes[:limit]

    # ── 笔记详情 ────────────────────────────────────────────────
    async def get_note_detail(self, note_url: str) -> Optional[dict]:
        """打开笔记详情页，返回标题/作者/正文/图片。"""
        page = self._bm.page
        note_id = self.extract_note_id(note_url)
        if not note_id:
            return None
        try:
            await self.navigate(EXPLORE_URL.format(note_id=note_id))
        except Exception as exc:
            logger.error(f"打开笔记失败: {exc}")
            return None
        await asyncio.sleep(2)
        try:
            title = (await self._text(page, TITLE_SELECTORS)) or ""
            author = (await self._text(page, AUTHOR_SELECTORS)) or ""
            desc = ""
            try:
                d = await page.query_selector("#detail-desc, div[class*='desc']")
                if d:
                    desc = (await d.inner_text()).strip()
            except Exception:
                pass
            images = []
            try:
                imgs = await page.query_selector_all("#detail-image-container img, div[class*='slide'] img")
                for im in imgs[:9]:
                    src = await im.get_attribute("src") or await im.get_attribute("data-src")
                    if src:
                        images.append(src)
            except Exception:
                pass
            return {
                "note_id": note_id,
                "title": title,
                "author": author,
                "desc": desc,
                "images": images,
                "url": EXPLORE_URL.format(note_id=note_id),
            }
        except Exception as exc:
            logger.error(f"解析笔记详情失败: {exc}")
            return None

    # ── 点赞 / 收藏 ─────────────────────────────────────────────
    async def like_note(self, note_url: str) -> bool:
        """点赞笔记（已点赞会取消）。返回是否成功。"""
        return await self._click_action(note_url, LIKE_SELECTORS, "点赞")

    async def collect_note(self, note_url: str) -> bool:
        """收藏笔记。"""
        return await self._click_action(note_url, COLLECT_SELECTORS, "收藏")

    async def _click_action(self, note_url: str, selectors: list, action: str) -> bool:
        page = self._bm.page
        note_id = self.extract_note_id(note_url)
        if not note_id:
            return False
        try:
            await self.navigate(EXPLORE_URL.format(note_id=note_id))
            await asyncio.sleep(1.5)
            for sel in selectors:
                try:
                    el = await page.wait_for_selector(sel, timeout=5000)
                    if el:
                        await el.click()
                        logger.info(f"{action}成功: {note_id}")
                        return True
                except Exception:
                    continue
            # 兜底：通过 JS 找可点击的 svg
            try:
                clicked = await page.evaluate(f"""() => {{
                    const svgs = document.querySelectorAll("svg");
                    for (const s of svgs) {{
                        const cls = (s.getAttribute('class') || '').toLowerCase();
                        if (cls.includes('{action}') || cls.includes('like') || cls.includes('collect')) {{
                            s.closest('div,button,span')?.click();
                            return true;
                        }}
                    }}
                    return false;
                }}""")
                if clicked:
                    return True
            except Exception:
                pass
            return False
        except Exception as exc:
            logger.error(f"{action}失败: {exc}")
            return False

    # ── 评论 ────────────────────────────────────────────────────
    async def comment_note(self, note_url: str, text: str) -> bool:
        """评论笔记。"""
        page = self._bm.page
        note_id = self.extract_note_id(note_url)
        if not note_id or not text.strip():
            return False
        try:
            await self.navigate(EXPLORE_URL.format(note_id=note_id))
            await asyncio.sleep(1.5)
            for sel in COMMENT_SELECTORS:
                try:
                    box = await page.wait_for_selector(sel, timeout=5000)
                    if box:
                        await box.click()
                        await box.fill(text)
                        await asyncio.sleep(0.8)
                        break
                except Exception:
                    continue
            for sel in SEND_SELECTORS:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        logger.info(f"评论成功: {note_id}")
                        return True
                except Exception:
                    continue
            # 兜底：回车发送
            try:
                await page.keyboard.press("Enter")
                return True
            except Exception:
                return False
        except Exception as exc:
            logger.error(f"评论失败: {exc}")
            return False

    async def fetch_comments(self, note_url: str, limit: int = 8) -> list[dict]:
        """抓取笔记热门评论。"""
        page = self._bm.page
        note_id = self.extract_note_id(note_url)
        if not note_id:
            return []
        try:
            await self.navigate(EXPLORE_URL.format(note_id=note_id))
        except Exception as exc:
            logger.error(f"打开笔记失败: {exc}")
            return []
        comments: list[dict] = []
        try:
            await asyncio.sleep(2)
            items = await page.query_selector_all("div[class*='comment-item'], div[class*='comment'] div[class*='content']")
            for it in items[:limit]:
                try:
                    t = (await it.inner_text()).strip()
                    if t:
                        comments.append({"text": t[:200]})
                except Exception:
                    continue
        except Exception as exc:
            logger.debug(f"评论解析失败: {exc}")
        return comments

    # ── 关注 ────────────────────────────────────────────────────
    async def follow_user(self, user_url: str) -> bool:
        """关注用户主页。"""
        page = self._bm.page
        try:
            await self.navigate(user_url)
            await asyncio.sleep(2)
            for sel in FOLLOW_SELECTORS:
                try:
                    el = await page.wait_for_selector(sel, timeout=5000)
                    if el:
                        text = (await el.inner_text()).strip() if await el.inner_text() else ""
                        if "已关注" in text:
                            logger.info("已关注过该用户")
                            return True
                        await el.click()
                        logger.info(f"关注成功: {user_url}")
                        return True
                except Exception:
                    continue
            return False
        except Exception as exc:
            logger.error(f"关注失败: {exc}")
            return False

    # ── 浏览推荐流 ──────────────────────────────────────────────
    async def browse_feed(self, rounds: int = 3, wait_sec: int = 6) -> int:
        """浏览推荐流，滚动 N 轮，返回看到的笔记数。"""
        page = self._bm.page
        try:
            await self.navigate(XHS_HOME)
            await asyncio.sleep(3)
            count = 0
            for i in range(rounds):
                await page.mouse.wheel(0, 1200)
                await asyncio.sleep(wait_sec)
                count += 1
            return count
        except Exception as exc:
            logger.error(f"浏览推荐流失败: {exc}")
            return 0

    # ── 工具 ────────────────────────────────────────────────────
    @staticmethod
    def extract_note_id(url: str) -> Optional[str]:
        """从链接提取笔记 ID。支持 /explore/{id} 和 /discovery/item/{id}。"""
        if not url:
            return None
        m = re.search(r"/(?:explore|discovery/item|item)/([0-9a-zA-Z]{10,})", url)
        if m:
            return m.group(1)
        return None

    async def _text(self, root, selectors: list) -> str:
        """在 root 下按选择器列表取文本。"""
        for sel in selectors:
            try:
                el = await root.query_selector(sel)
                if el:
                    t = (await el.inner_text()).strip()
                    if t:
                        return t
            except Exception:
                continue
        return ""

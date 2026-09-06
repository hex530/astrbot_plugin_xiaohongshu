"""小红书图文发布扩展（XhsClient monkey-patch）。

独立模块避免改动大文件；通过 XhsClient.publish_note 挂载。

发布流程（creator.xiaohongshu.com）：
1. 确保已登录
2. 图片来源支持：本地路径 / http(s) URL（URL 先下载到临时目录）
3. 打开创作服务平台发布页 -> 上传图文 -> 填标题/正文/话题 -> 发布
4. 返回 {ok, stage, message, note_url}
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import urllib.parse
from pathlib import Path

logger = logging.getLogger("xhs.publish")

PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"

UPLOAD_BTN_SELECTORS = [
    "text=上传图文",
    "div:has-text('上传图文')",
    "span:has-text('上传图文')",
    "button:has-text('上传')",
    "text=发布图文",
]
FILE_INPUT_SELECTORS = [
    "input[type='file']",
    "input[accept*='image']",
    "input[type='file'][multiple]",
]
TITLE_SELECTORS = [
    "input[placeholder*='标题']",
    "input[placeholder*='填写标题']",
    "input[class*='title']",
    "textarea[placeholder*='标题']",
]
CONTENT_SELECTORS = [
    "div[contenteditable='true']",
    "textarea[placeholder*='正文']",
    "div[class*='editor'] [contenteditable='true']",
    "#post-content",
]
PUBLISH_BTN_SELECTORS = [
    "button:has-text('发布')",
    "button:has-text('发布笔记')",
    "div[class*='publish'] button",
    "button[class*='submit']",
]
TOPIC_SELECTORS = [
    "input[placeholder*='话题']",
    "input[placeholder*='#']",
    "input[placeholder*='添加话题']",
]


def _pick_url_scheme(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return "url"
    return "file"


async def _download_image(page, url: str, dest: str) -> bool:
    """用 Playwright 内置请求下载图片，避免额外依赖。"""
    try:
        resp = await page.request.get(url, timeout=30000)
        if resp.ok:
            body = await resp.body()
            Path(dest).write_bytes(body)
            return len(body) > 0
    except Exception as exc:
        logger.warning(f"图片下载失败 {url}: {exc}")
    return False


async def publish_note_impl(
    self,
    images: list,
    title: str = "",
    content: str = "",
    topics: list = None,
    wait_upload: int = 45,
) -> dict:
    """发布图文笔记。images 元素可为本地路径或 http(s) URL。"""
    topics = topics or []
    if not images:
        return {"ok": False, "stage": "param", "message": "至少需要一张图片"}
    title = (title or "").strip()
    content = (content or "").strip()
    if not title and not content:
        return {"ok": False, "stage": "param", "message": "标题和正文不能都为空"}

    bm = self._bm
    if not bm.is_started:
        try:
            await bm.start()
        except Exception as exc:
            return {"ok": False, "stage": "browser", "message": f"浏览器启动失败: {exc}"}
    try:
        auth = await bm._check_login_status()
    except Exception:
        auth = False
    if not auth and not bm.is_authenticated:
        return {"ok": False, "stage": "auth", "message": "未登录小红书，请先扫码登录"}

    page = bm.page

    # 1) 准备图片文件
    tmp_dir = os.path.join(bm._data_dir, "tmp_publish")
    os.makedirs(tmp_dir, exist_ok=True)
    local_files = []
    for i, img in enumerate(images):
        img = str(img).strip()
        if not img:
            continue
        if _pick_url_scheme(img) == "file":
            if not os.path.exists(img):
                alt = os.path.join(bm._data_dir, img)
                if os.path.exists(alt):
                    img = alt
                else:
                    return {"ok": False, "stage": "images", "message": f"图片不存在: {img}"}
            local_files.append(img)
        else:
            ext = ".jpg"
            m = re.search(r"\.(jpe?g|png|webp|gif)", urllib.parse.urlparse(img).path, re.I)
            if m:
                ext = "." + m.group(1).lower()
            dest = os.path.join(tmp_dir, f"pub_{int(time.time())}_{i}{ext}")
            ok = await _download_image(page, img, dest)
            if not ok:
                return {"ok": False, "stage": "images", "message": f"图片下载失败: {img}"}
            local_files.append(dest)

    if not local_files:
        return {"ok": False, "stage": "images", "message": "没有可用的图片"}

    # 2) 打开发布页
    try:
        await page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=40000)
    except Exception as exc:
        return {"ok": False, "stage": "open", "message": f"打开发布页失败: {exc}"}
    await asyncio.sleep(4)

    # 3) 触发上传（点“上传图文”使 file input 出现）
    input_el = None
    for sel in FILE_INPUT_SELECTORS:
        try:
            input_el = await page.wait_for_selector(sel, timeout=3000)
            if input_el:
                break
        except Exception:
            continue
    if not input_el:
        for sel in UPLOAD_BTN_SELECTORS:
            try:
                btn = await page.wait_for_selector(sel, timeout=2500)
                if btn:
                    await btn.click()
                    break
            except Exception:
                continue
        await asyncio.sleep(1.5)
        for sel in FILE_INPUT_SELECTORS:
            try:
                input_el = await page.wait_for_selector(sel, timeout=5000)
                if input_el:
                    break
            except Exception:
                continue
    if not input_el:
        return {"ok": False, "stage": "upload", "message": "找不到图片上传入口（页面结构可能变化）"}

    try:
        await input_el.set_input_files(local_files)
    except Exception as exc:
        return {"ok": False, "stage": "upload", "message": f"图片上传失败: {exc}"}

    # 4) 等待上传完成
    await asyncio.sleep(3)
    for _ in range(wait_upload):
        try:
            uploading = await page.query_selector(
                "text=上传中, .upload-progress, [class*='progress']:not([class*='0%']), [class*='uploading']"
            )
            if not uploading:
                break
        except Exception:
            break
        await asyncio.sleep(2)
    await asyncio.sleep(2)

    # 5) 填标题
    title_ok = False
    if title:
        for sel in TITLE_SELECTORS:
            try:
                t = await page.wait_for_selector(sel, timeout=2500)
                if t:
                    await t.click()
                    await t.fill(title)
                    title_ok = True
                    break
            except Exception:
                continue
    else:
        title_ok = True

    # 6) 填正文 + 话题
    content_ok = False
    if content:
        for sel in CONTENT_SELECTORS:
            try:
                c = await page.wait_for_selector(sel, timeout=2500)
                if c:
                    await c.click()
                    text = content
                    if topics:
                        tstr = " ".join(f"#{t}#" for t in topics if str(t).strip())
                        if tstr:
                            text = text + " " + tstr
                    await c.type(text, delay=30)
                    content_ok = True
                    break
            except Exception:
                continue
    else:
        content_ok = True

    if not title_ok or not content_ok:
        return {"ok": False, "stage": "fill", "message": "标题或正文填写失败（页面结构可能变化）", "title_ok": title_ok, "content_ok": content_ok}

    # 7) 发布
    await asyncio.sleep(1)
    clicked = False
    for sel in PUBLISH_BTN_SELECTORS:
        try:
            btn = await page.wait_for_selector(sel, timeout=3500)
            if btn:
                disabled = await btn.get_attribute("disabled")
                if disabled is None:
                    await btn.click()
                    clicked = True
                    break
        except Exception:
            continue
    if not clicked:
        return {"ok": False, "stage": "publish_btn", "message": "找不到可点击的发布按钮（可能图片还在上传或页面变化）"}

    # 8) 等待发布结果（最多 60 秒）
    await asyncio.sleep(2)
    for _ in range(30):
        await asyncio.sleep(2)
        try:
            url_now = page.url
            if "success" in url_now or "publish/success" in url_now:
                return {"ok": True, "stage": "done", "message": "发布成功（已跳转成功页）", "url": url_now}
            toast = await page.query_selector("text=发布成功, text=已发布, [class*='success']")
            if toast:
                txt = (await toast.inner_text()).strip()
                return {"ok": True, "stage": "done", "message": f"发布成功提示: {txt[:100]}", "url": url_now}
        except Exception:
            continue
    return {"ok": False, "stage": "wait", "message": "已点击发布但未确认结果，请到创作服务平台查看（可能已发布或需人工过审）"}


def install_publish(client_class) -> None:
    """把发布能力挂到 XhsClient 类上。"""
    if not hasattr(client_class, "publish_note"):
        client_class.publish_note = publish_note_impl

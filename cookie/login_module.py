#!/usr/bin/env python3
"""
天翼云登录模块
"""

import asyncio
import json
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CTYUN_ORIGIN = "https://pm.ctyun.cn"
CTYUN_HOME_URL = f"{CTYUN_ORIGIN}/#/home"
LOGIN_TIMEOUT = 120
SMS_WAIT_TIMEOUT = 300
CAPTCHA_RETRY = 3
SESSION_TTL = 600


@dataclass
class LoginResult:
    success: bool
    message: str
    data: dict = field(default_factory=dict)
    raw_json: str = ""
    state_file: str = ""


@dataclass
class SessionState:
    session_id: str
    phone: str
    password: str
    created_at: float = field(default_factory=time.time)
    status: str = "starting"
    message: str = ""
    result: Optional[LoginResult] = None
    ready_event: threading.Event = field(default_factory=threading.Event)
    done_event: threading.Event = field(default_factory=threading.Event)
    sms_queue: "queue.Queue[str]" = field(default_factory=queue.Queue)


class LoginSessionManager:
    def __init__(self):
        self._sessions: dict[str, SessionState] = {}
        self._lock = threading.Lock()

    def create(self, phone: str, password: str) -> SessionState:
        self.cleanup()
        session = SessionState(session_id=uuid.uuid4().hex, phone=phone, password=password)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            return self._sessions.get(session_id)

    def pop(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            return self._sessions.pop(session_id, None)

    def cleanup(self):
        now = time.time()
        expired = []
        with self._lock:
            for session_id, session in self._sessions.items():
                if now - session.created_at > SESSION_TTL:
                    expired.append(session_id)
            for session_id in expired:
                self._sessions.pop(session_id, None)


SESSION_MANAGER = LoginSessionManager()


def format_browser_launch_error(exc: Exception) -> str:
    raw = str(exc)
    hint_markers = (
        "Operation not permitted",
        "SIGTRAP",
        "sandbox_host_linux.cc",
        "crashpad",
        "Target page, context or browser has been closed",
    )
    if any(marker in raw for marker in hint_markers):
        return (
            "浏览器启动失败，当前运行环境可能启用了 seccomp/沙箱限制，"
            "导致 Playwright Chromium 被系统拦截。"
            "请在普通主机终端、screen/tmux、systemd 或 nohup 环境中运行。"
            f"\n原始错误: {raw}"
        )
    return f"浏览器启动失败: {raw}"


async def launch_browser(playwright):
    launch_options = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    }
    errors = []
    for extra in ({"channel": "chromium"}, {}):
        try:
            return await playwright.chromium.launch(**launch_options, **extra)
        except Exception as e:
            errors.append(format_browser_launch_error(e))
    raise RuntimeError(" | ".join(errors))


def start_login_session(phone: str, password: str) -> LoginResult:
    session = SESSION_MANAGER.create(phone, password)
    thread = threading.Thread(
        target=_run_login_session_sync,
        args=(session,),
        daemon=True,
        name=f"ctyun-login-{session.session_id[:8]}",
    )
    thread.start()

    if not session.ready_event.wait(LOGIN_TIMEOUT):
        SESSION_MANAGER.pop(session.session_id)
        return LoginResult(success=False, message=f"登录超时 ({LOGIN_TIMEOUT}秒)")

    if session.result:
        SESSION_MANAGER.pop(session.session_id)
        return session.result

    if session.status == "awaiting_sms":
        return LoginResult(
            success=False,
            message="需要短信验证码",
            data={
                "require_sms_code": True,
                "session_id": session.session_id,
                "expires_in": SESSION_TTL,
            },
        )

    SESSION_MANAGER.pop(session.session_id)
    return LoginResult(success=False, message=session.message or "登录失败")


def submit_sms_code(session_id: str, sms_code: str) -> LoginResult:
    session = SESSION_MANAGER.get(session_id)
    if not session:
        return LoginResult(success=False, message="登录会话不存在或已过期，请重新登录")

    if session.status != "awaiting_sms":
        if session.result:
            SESSION_MANAGER.pop(session_id)
            return session.result
        return LoginResult(success=False, message="当前会话不需要短信验证码")

    session.sms_queue.put(sms_code)

    if not session.done_event.wait(LOGIN_TIMEOUT):
        SESSION_MANAGER.pop(session_id)
        return LoginResult(success=False, message=f"短信验证超时 ({LOGIN_TIMEOUT}秒)")

    SESSION_MANAGER.pop(session_id)
    return session.result or LoginResult(success=False, message="短信验证失败")


def _run_login_session_sync(session: SessionState):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(_login_flow(session))
    except Exception as exc:
        result = LoginResult(success=False, message=f"登录出错: {exc}")
    finally:
        loop.close()

    if not session.result:
        session.result = result
    session.status = "finished"
    session.ready_event.set()
    session.done_event.set()


async def _login_flow(session: SessionState) -> LoginResult:
    print(f"[登录] 开始登录账号: {session.phone}, session={session.session_id}")

    try:
        return await asyncio.wait_for(_do_login_flow(session), timeout=LOGIN_TIMEOUT + SMS_WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        return LoginResult(success=False, message=f"登录超时 ({LOGIN_TIMEOUT + SMS_WAIT_TIMEOUT}秒)")


async def _do_login_flow(session: SessionState) -> LoginResult:
    async with _get_async_playwright()() as p:
        browser = await launch_browser(p)
        context = await browser.new_context()
        page = await context.new_page()
        api_responses: list[dict[str, str]] = []

        async def capture_api(response):
            url = response.url
            if "desk.ctyun.cn" in url or "ctyun.cn" in url:
                try:
                    body = await response.text()
                    api_responses.append({"url": url, "body": body})
                except Exception:
                    pass

        page.on("response", capture_api)

        try:
            print("[登录] 步骤1: 访问登录页...")
            await page.goto("https://pm.ctyun.cn/", timeout=60000)
            await asyncio.sleep(2)

            print("[登录] 步骤2: 点击登录按钮...")
            await _open_login_form(page)
            await asyncio.sleep(2)

            print("[登录] 步骤3: 填写登录信息...")
            await page.locator('input[type="text"]').fill(session.phone)
            await page.locator('input[type="password"]').fill(session.password)

            print("[登录] 步骤4: 提交登录...")
            await page.locator("button").filter(has_text="登").first.click()
            await asyncio.sleep(3)

            current_url = page.url
            login_msg = await _latest_feedback(page, api_responses, "api/auth/client/login", "captcha")
            if (
                "login" in current_url
                and (
                    "图形" in login_msg
                    or "captcha" in login_msg.lower()
                    or await page.locator('input[placeholder*="图形"]').count() > 0
                )
            ):
                solved, login_msg = await _solve_login_page_captcha(page, api_responses)
                if not solved:
                    return LoginResult(success=False, message=login_msg)
                current_url = page.url
                if login_msg and "login" in current_url and "device_verify" not in current_url:
                    return LoginResult(success=False, message=login_msg)

            current_url = page.url
            print(f"[登录] 当前URL: {current_url}")

            if "device_verify" in current_url:
                sms_error = await _prepare_device_verify(page, api_responses)
                if sms_error:
                    return LoginResult(success=False, message=sms_error)

                session.status = "awaiting_sms"
                session.message = "需要短信验证码"
                session.ready_event.set()

                try:
                    sms_code = await asyncio.to_thread(session.sms_queue.get, True, SMS_WAIT_TIMEOUT)
                except queue.Empty:
                    return LoginResult(success=False, message=f"短信验证码等待超时 ({SMS_WAIT_TIMEOUT}秒)")

                verify_error = await _submit_device_verify(page, api_responses, sms_code)
                if verify_error:
                    return LoginResult(success=False, message=verify_error)

            await asyncio.sleep(2)
            current_url = page.url
            if "device_verify" in current_url or "login" in current_url:
                detail_msg = await _latest_feedback(
                    page, api_responses, "api/auth/client/login", "verify", "captcha", "sms"
                )
                if detail_msg:
                    return LoginResult(success=False, message=detail_msg)
                return LoginResult(success=False, message="登录失败，请检查账号密码、图形验证码或短信验证码")

            print("[登录] 步骤8: 保存登录状态...")
            state = await context.storage_state()
            raw_json = json.dumps(state, ensure_ascii=False)
            latest_file, raw_json = _write_state_files(session.phone, session.session_id, raw_json)

            return LoginResult(
                success=True,
                message="登录成功",
                data={"session_id": session.session_id},
                raw_json=raw_json,
                state_file=str(latest_file),
            )
        finally:
            await browser.close()


def _latest_api_message(api_responses: list[dict[str, str]], *url_keywords: str) -> str:
    keywords = [kw.lower() for kw in url_keywords if kw]
    for response in reversed(api_responses):
        url = response.get("url", "").lower()
        if keywords and not any(kw in url for kw in keywords):
            continue
        body = response.get("body", "")
        if not body:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if isinstance(data, dict):
            for key in ("msg", "message", "errorMsg", "errmsg"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


async def _open_login_form(page):
    candidates = (
        page.get_by_role("button", name="登录/注册"),
        page.get_by_text("登录/注册"),
        page.get_by_role("button", name="登录"),
        page.locator("button").filter(has_text="登录").first,
    )
    for locator in candidates:
        try:
            if await locator.count() > 0:
                await locator.click()
                return
        except Exception:
            continue
    raise RuntimeError("未找到登录入口")


async def _latest_ui_message(page) -> str:
    selectors = (
        ".van-toast__text",
        ".van-notify__text",
        ".van-field__error-message",
        '[role="alert"]',
        ".error",
        ".message",
    )
    for selector in selectors:
        locator = page.locator(selector)
        count = await locator.count()
        for index in range(count - 1, -1, -1):
            try:
                item = locator.nth(index)
                if not await item.is_visible():
                    continue
                text = (await item.inner_text()).strip()
                if text:
                    return text
            except Exception:
                continue
    return ""


async def _latest_feedback(page, api_responses: list[dict[str, str]], *url_keywords: str) -> str:
    ui_message = await _latest_ui_message(page)
    if ui_message:
        return ui_message
    return _latest_api_message(api_responses, *url_keywords)


async def _wait_for_feedback(page, api_responses: list[dict[str, str]], *url_keywords: str, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = await _latest_feedback(page, api_responses, *url_keywords)
        if message:
            return message
        await asyncio.sleep(0.2)
    return await _latest_feedback(page, api_responses, *url_keywords)


async def _solve_login_page_captcha(page, api_responses: list[dict[str, str]]) -> tuple[bool, str]:
    captcha_input = page.locator(
        'input[placeholder*="图形"], input[aria-label*="图形"], input[id*="field-3"]'
    ).first
    captcha_img = page.locator('img[alt="图形码"], img[src*="captcha"]').first

    if await captcha_input.count() == 0 or await captcha_img.count() == 0:
        return False, "登录页要求图形验证码，但未找到输入框或验证码图片"

    print("[登录] 处理登录页图形验证码...")

    for retry in range(CAPTCHA_RETRY):
        if retry > 0:
            print(f"[登录] 登录页图形验证码重试 ({retry + 1}/{CAPTCHA_RETRY})...")
            api_responses.clear()

        try:
            captcha_data = await captcha_img.screenshot(type="png")
        except Exception:
            captcha_data = None

        if not captcha_data:
            return False, "无法获取登录页图形验证码"

        try:
            import ddddocr

            ocr = ddddocr.DdddOcr(show_ad=False)
            captcha_code = ocr.classification(captcha_data)
            print(f"[登录] 登录页图形验证码: {captcha_code}")
        except Exception as e:
            return False, f"验证码识别失败: {e}"

        await captcha_input.fill(captcha_code)
        submit_btn = page.locator("button.login, button").filter(has_text="登").first
        await submit_btn.click()
        await asyncio.sleep(1)

        current_msg = await _wait_for_feedback(page, api_responses, "api/auth/client/login", "captcha")
        captcha_still_visible = await captcha_input.count() > 0
        current_url = page.url

        if not captcha_still_visible or "login" not in current_url:
            return True, current_msg

        if "图形" in current_msg or "验证码" in current_msg or "captcha" in current_msg.lower():
            try:
                await captcha_img.click()
                await asyncio.sleep(1)
            except Exception:
                pass
            continue

        return True, current_msg

    final_msg = _latest_api_message(api_responses, "api/auth/client/login", "captcha")
    return False, final_msg or f"登录页图形验证码重试 {CAPTCHA_RETRY} 次后仍失败"


async def _prepare_device_verify(page, api_responses: list[dict[str, str]]) -> str:
    print("[登录] 步骤5: 需要设备验证...")

    for retry in range(CAPTCHA_RETRY):
        if retry > 0:
            print(f"[登录] 验证码重试 ({retry + 1}/{CAPTCHA_RETRY})...")
            api_responses.clear()

        captcha_data = None
        captcha_handler_ref = [None]

        def capture_captcha(response):
            nonlocal captcha_data
            if "captcha" in response.url.lower():
                ct = response.headers.get("content-type", "")
                if "image" in ct:
                    captcha_data = response

        captcha_handler_ref[0] = capture_captcha
        page.on("response", capture_captcha)

        try:
            captcha_img = page.locator("img").first
            await captcha_img.click()
            await asyncio.sleep(1)

            if not captcha_data:
                for _ in range(5):
                    await asyncio.sleep(0.5)
                    if captcha_data:
                        break
        finally:
            if captcha_handler_ref[0]:
                try:
                    page.remove_listener("response", captcha_handler_ref[0])
                except Exception:
                    pass

        if not captcha_data:
            return "无法获取图形验证码"

        try:
            import ddddocr

            ocr = ddddocr.DdddOcr(show_ad=False)
            body = await captcha_data.body()
            captcha_code = ocr.classification(body)
            print(f"[登录] 图形验证码: {captcha_code}")
        except Exception as e:
            return f"验证码识别失败: {e}"

        captcha_input = page.locator(
            'input[placeholder*="图形"], input[placeholder*="验证码"], input[aria-label*="图形"], input[aria-label*="验证码"], input[name*="captcha"], input[id*="captcha"]'
        ).first
        if await captcha_input.count() > 0:
            await captcha_input.fill(captcha_code)
        else:
            inputs = await page.locator("input").all()
            if len(inputs) >= 2:
                await inputs[1].fill(captcha_code)
            else:
                return "页面结构异常"

        print("[登录] 步骤6: 发送短信验证码...")
        try:
            await page.locator("button").filter(has_text="获取").click()
            await asyncio.sleep(1)
        except Exception:
            return "发送短信按钮未找到"

        sms_sent = False
        sms_error = await _wait_for_feedback(page, api_responses, "sms", "captcha", timeout=2.0)
        for response in api_responses:
            if "getsmscode" in response["url"].lower() or "sms" in response["url"].lower():
                try:
                    data = json.loads(response["body"])
                    if data.get("edata") or data.get("code") in (0, 200):
                        print("[登录] 短信发送成功!")
                        sms_sent = True
                    else:
                        sms_error = data.get("msg", data.get("message", "未知错误"))
                        print(f"[登录] 发送失败: {sms_error}")
                except Exception:
                    pass

        if sms_sent:
            return ""

        if "验证码" in sms_error or "captcha" in sms_error.lower():
            continue
        return f"短信发送失败: {sms_error}"

    return f"验证码重试 {CAPTCHA_RETRY} 次后仍失败"


async def _submit_device_verify(page, api_responses: list[dict[str, str]], sms_code: str) -> str:
    print("[登录] 步骤7: 验证身份...")

    sms_input = page.locator(
        'input[placeholder*="短信"], input[placeholder*="验证码"], input[aria-label*="短信"], input[aria-label*="验证码"], input[name*="sms"], input[id*="sms"], input[name*="code"], input[id*="code"]'
    ).first
    if await sms_input.count() > 0:
        await sms_input.fill(sms_code)
    else:
        inputs = await page.locator("input").all()
        if len(inputs) >= 3:
            await inputs[2].fill(sms_code)
        elif inputs:
            await inputs[-1].fill(sms_code)
        else:
            return "未找到验证码输入框"

    await page.locator("button").filter(has_text="确认").click()
    await asyncio.sleep(1)

    current_url = page.url
    if "device_verify" not in current_url:
        return ""

    verify_error = await _wait_for_feedback(page, api_responses, "verify", "sms", "captcha", timeout=2.0)
    for response in api_responses:
        if "verify" in response["url"].lower():
            try:
                data = json.loads(response["body"])
                verify_error = data.get("msg", "") or verify_error
            except Exception:
                pass

    if "验证码" in verify_error or "code" in verify_error.lower():
        return verify_error or "短信验证码错误"
    return f"验证失败: {verify_error}" if verify_error else "验证失败"


def _get_async_playwright():
    try:
        from playwright.async_api import async_playwright

        return async_playwright
    except ImportError as e:
        raise RuntimeError(
            "缺少依赖 playwright。请先运行: pip install playwright && playwright install chromium"
        ) from e


DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _write_state_files(phone: str, session_id: str, raw_json: str) -> tuple[Path, str]:
    unique_file = DATA_DIR / f"ctyun_state_{phone}_{session_id}.json"
    latest_file = DATA_DIR / f"ctyun_state_{phone}.json"
    unique_file.write_text(raw_json, encoding="utf-8")
    shutil.copyfile(unique_file, latest_file)
    saved_json = unique_file.read_text(encoding="utf-8")
    return unique_file, saved_json

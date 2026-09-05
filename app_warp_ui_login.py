#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# WARP 出口 + Discord UI 跳转登录版本（由 app_warp.py 复制）：
# - 工作流中通过 fscarmen/warp-on-actions 启用全局 WARP，浏览器和 requests 均直接走 WARP 出口；
# - Discord 登录方式改为 UI 跳转（参考 liveqte/Freeze-Renew）：先注入 Token 预登录 Discord，
#   再打开 bot-hosting.net 的 OAuth 入口，在 Discord 授权页 UI 点击 Authorize 完成登录，
#   不再直接调用 Discord 授权 API；若触发风控(fraud)会重启 WARP 更换出口 IP 后重试。

import os, re, sys, time, json, requests, subprocess
import urllib.request, urllib.parse, urllib.error
from datetime import datetime
from seleniumbase import SB

# 环境变量配置(可以直接私库在双引号里填写)
EMAIL         = os.environ.get("EMAIL") or ""           # 邮箱,只用于通知使用，可随意填写
SESSION_TOKEN = os.environ.get("SESSION_TOKEN") or ""   # session token，默认登录方式,非必须
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN") or ""   # Discord Token 备用登录方式, 失败时才使用,必须填写
GH_TOKEN      = os.environ.get("GH_TOKEN") or ""        # GitHub PAT token,用于自动更新session token,可选
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID") or ""      # TG chat id,不填写不通知，需和bot token一起填写生效
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN") or ""    # TG bot token 

# 解析 DISCORD_TOKEN
DC_TOKEN = ""
if DISCORD_TOKEN:
    _parts = DISCORD_TOKEN.split(",", 1)
    DC_TOKEN = _parts[-1].strip()

if not SESSION_TOKEN and not DC_TOKEN:
    print("ℹ️ 未配置 SESSION_TOKEN 和 DISCORD_TOKEN,脚本终止。")
    sys.exit(1)

# 构造cookie
COOKIES = {
    "session_token": SESSION_TOKEN,
    "login": "true",
    "theme": "system",
}

# 记录本次登录方式（用于通知）
_LOGIN_METHOD = "SESSION_TOKEN"

# 获取cookie到期时间
def get_cookie_info(sb, name):
    cookies = sb.get_cookies()
    for c in cookies:
        if c.get('name') == name:
            value = c.get('value')
            expiry_ts = c.get('expiry')
            expiry_dt = datetime.fromtimestamp(expiry_ts) if expiry_ts else None
            return value, expiry_dt
    return None, None

# 检查是否需要更新cookie
def should_update_cookie(new_value, old_value, expiry_dt, days_threshold=3):
    if new_value is None:
        return False
    if new_value != old_value:
        return True
    if expiry_dt:
        remaining = (expiry_dt - datetime.now()).total_seconds()
        if remaining < days_threshold * 24 * 3600:
            return True
    return False

# 更新cookie到secrets
def update_github_secret(secret_name, new_value):
    if not new_value:
        print(f"⚠️ 跳过更新 {secret_name}：新值为空")
        return False
    masked = new_value[:4] + "..." + new_value[-4:] if len(new_value) > 8 else "***"
    print(f"🔄 更新 Secret: {secret_name} (新值: {masked})")
    try:
        env = os.environ.copy()
        if GH_TOKEN:
            env["GH_TOKEN"] = GH_TOKEN
        proc = subprocess.run(
            ["gh", "secret", "set", secret_name, "--body", new_value],
            capture_output=True, text=True, timeout=30, check=False,
            env=env
        )
        if proc.returncode == 0:
            return True
        else:
            print(f"❌ 更新失败: {proc.stderr.strip()}")
            return False
    except Exception as e:
        print(f"❌ 异常: {e}")
        return False

# 发送tg通知
def send_telegram_message(message: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("⚠️ Telegram 未配置，跳过通知")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": message}, timeout=10)
        print("✅ Telegram 通知已发送")
    except Exception as e:
        print(f"❌ Telegram 发送失败: {e}")

# 通知格式
def format_notification(status: str, extra: str = "", error: str = "", expiry_date: str = "") -> str:
    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    else:
        masked_email = EMAIL[:2] + '****' 
    
    lines = [
        "🇫🇮 Bot-hosting 续期通知",
        "",
        f"{status}",
        f"👤 登录账户: {masked_email}",
    ]
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    if expiry_date:
        lines.append(f"📅 到期时间: {expiry_date}")
    if extra:
        lines.append(extra)
    if error:
        lines.append(f"⚠️ 错误信息: {error}")
    lines.append(f"⏱️ 登录时间: {now}")
    return "\n".join(lines)

# 等待Turnstile验证通过
def wait_for_turnstile_pass(sb, timeout=30):
    start = time.time()
    cf_indicators = ["verify you are human", "确认您是真人", "troubleshoot", "just a moment"]
    while time.time() - start < timeout:
        page_lower = sb.get_page_source().lower()
        if not any(x in page_lower for x in cf_indicators):
            print("✅ Turnstile 验证已通过")
            # sb.save_screenshot("turnstile_passed.png")
            return True
        sb.sleep(1)
    print("❌ Turnstile 验证超时未通过")
    return False
    
# 获取当前出口ip（WARP 全局接管，无需代理参数）
def get_current_ip() -> str:
    response = requests.get("https://api.ip.sb/ip", timeout=15)
    response.raise_for_status()
    return response.text.strip()

# 重启 WARP 以更换出口 IP（参考 wisp-sb.py 的 restart_warp）
def restart_warp() -> bool:
    print("🔄 正在重启 WARP 以更换出口 IP...")
    try:
        old_ip = get_current_ip()
        print(f"📍 当前出口 IP: {old_ip}")
    except Exception:
        old_ip = "未知"
    try:
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "disconnect"],
                       check=False, timeout=30, capture_output=True)
        time.sleep(3)
        try:
            subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "delete"],
                           check=True, timeout=30, capture_output=True)
        except subprocess.CalledProcessError:
            print("⚠️ 删除 WARP 注册失败（可能未注册），继续...")
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "registration", "new"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(3)
        subprocess.run(["sudo", "warp-cli", "--accept-tos", "connect"],
                       check=True, timeout=30, capture_output=True)
        time.sleep(10)
        new_ip = get_current_ip()
        print(f"✅ WARP 重连成功，出口 IP 已更换: {old_ip} -> {new_ip}")
        return True
    except Exception as e:
        print(f"❌ WARP 重启失败: {e}")
        return False

# 时间格式化
def format_countdown(countdown_str: str) -> str:
    try:
        h, m, _ = countdown_str.split(':')
        h = int(h)
        m = int(m)
        if h > 0:
            return f"{h}h{m}min"
        else:
            return f"{m}min"
    except:
        return countdown_str

# 获取过期日期
def extract_expiry_date(page_source: str) -> str:
    patterns = [
        r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})",   # Expires 2026/07/07
        r"[Ee]xpires\s*[:\-]?\s*(\d{2}/\d{2}/\d{4})",   # Expires 07/07/2026 (MM/DD/YYYY)
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew",        # 2026/07/07 - renew
        r"(\d{2}/\d{2}/\d{4})\s*[\-–]\s*renew",        # 07/07/2026 - renew
        r"(\d{4}/\d{2}/\d{2})\s*[\-–]\s*renew manually to extend for 4 days", # 2026/07/07 - renew manually to extend for 4 days
    ]
    for pattern in patterns:
        match = re.search(pattern, page_source)
        if match:
            date_str = match.group(1)
            # 如果是 MM/DD/YYYY 格式，转换为 YYYY/MM/DD
            if len(date_str.split('/')[-1]) == 4:  # 年份长度4
                parts = date_str.split('/')
                if len(parts[0]) == 2:  # 第一部分是2位（月）
                    # 修正：将 MM/DD/YYYY 转为 YYYY/MM/DD
                    return f"{parts[2]}/{parts[0]}/{parts[1]}"
            return date_str
    return None


# ====================== Discord UI 跳转登录（参考 liveqte/Freeze-Renew） ======================
# 思路：不再直接调用 Discord 授权 API，而是全程走浏览器 UI 跳转：
#   1) 将 Discord Token 注入 localStorage，预登录 Discord（token login）
#   2) 打开 bot-hosting.net 的 Discord OAuth 入口，浏览器自动跳转到 Discord 授权页
#   3) 在授权页 UI 点击 "Authorize/授权" 按钮完成授权，跳回 bot-hosting.net

def prelogin_discord(sb) -> bool:
    """Step 1: 借 about:blank iframe 向同源 localStorage 注入 Token，预登录 Discord"""
    print("🔑 使用 Token 预登录 Discord (UI)...")
    sb.uc_open_with_reconnect("https://discord.com/login", reconnect_time=6)
    time.sleep(3)

    sb.execute_script(
        "var iframe = document.createElement('iframe');"
        "document.body.appendChild(iframe);"
        "iframe.contentWindow.localStorage.setItem('token', '\"' + arguments[0] + '\"');",
        DC_TOKEN,
    )
    time.sleep(1)

    # 刷新验证 Token 是否有效（失效会停留在 /login）
    sb.uc_open_with_reconnect("https://discord.com/login", reconnect_time=6)
    time.sleep(4)

    url = sb.get_current_url()
    if "/login" in url:
        print("❌ Discord Token 失效或被踢出，预登录失败")
        sb.save_screenshot("discord_prelogin_fail.png")
        return False
    print("✅ Discord Token 验证有效（Discord 侧已登录）")
    return True


def _click_authorize_button(sb) -> dict:
    """在 Discord 授权页查找并点击授权按钮（兼容中英文界面，跳过取消/拒绝按钮）"""
    try:
        result = sb.execute_script('''
            var RESULT = (function() {
                var denyWords = ['取消', 'cancel', 'deny'];
                var authWords = ['authorize', '授权', 'accept', '接受'];
                var buttons = document.querySelectorAll('button');
                var candidates = [];
                for (var i = 0; i < buttons.length; i++) {
                    var b = buttons[i];
                    var cs = window.getComputedStyle(b);
                    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
                    var text = (b.innerText || b.textContent || '').trim();
                    if (!text) continue;
                    var low = text.toLowerCase();
                    var deny = false;
                    for (var j = 0; j < denyWords.length; j++) {
                        if (low.indexOf(denyWords[j]) !== -1) { deny = true; break; }
                    }
                    if (deny) continue;
                    var isAuth = false;
                    for (var j = 0; j < authWords.length; j++) {
                        if (low.indexOf(authWords[j]) !== -1) { isAuth = true; break; }
                    }
                    var inFooter = !!b.closest('div[class*="footer"]');
                    var isSubmit = b.getAttribute('type') === 'submit';
                    if (isAuth || inFooter || isSubmit) {
                        candidates.push({b: b, text: text, disabled: !!b.disabled,
                                         p: isAuth ? 0 : (inFooter ? 1 : 2)});
                    }
                }
                if (!candidates.length) return {found: false};
                candidates.sort(function(a, c) { return a.p - c.p; });
                var c = candidates[0];
                if (c.disabled) return {found: true, clicked: false, text: c.text, disabled: true};
                c.b.click();
                return {found: true, clicked: true, text: c.text, disabled: false};
            })()
            return RESULT
        ''')
        return result or {}
    except Exception as e:
        print(f"⚠️ 查找授权按钮异常: {e}")
        return {}


def handle_oauth_page(sb, timeout: int = 75) -> bool:
    """Step 3: 在 Discord 授权页循环点击 Authorize，直到跳回 bot-hosting.net"""
    start = time.time()
    attempt = 0
    while time.time() - start < timeout:
        attempt += 1
        try:
            url = sb.get_current_url()
        except Exception:
            url = ""
        if "discord.com" not in url:
            print("✅ 已离开 Discord，授权跳转完成")
            return True

        print(f"🔄 第 {attempt} 次尝试授权，URL: {url[:90]}")
        try:
            sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass

        info = _click_authorize_button(sb)
        if info.get("found"):
            if info.get("disabled"):
                print(f"⏳ 按钮 '{info.get('text', '')}' 暂不可用（disabled），等待...")
            elif info.get("clicked"):
                print(f"✅ 已点击: '{info.get('text', '')}'")
        else:
            # 授权页可能嵌入了 Turnstile 人机验证，尝试处理
            print("ℹ️ 未找到授权按钮，尝试处理可能的人机验证...")
            try:
                sb.uc_gui_click_captcha()
            except Exception:
                pass

        # 等待页面跳转
        for _ in range(4):
            time.sleep(1)
            try:
                if "discord.com" not in sb.get_current_url():
                    print("✅ 授权成功，已跳转")
                    return True
            except Exception:
                pass

    print(f"⚠️ Discord 授权页处理超时，URL: {sb.get_current_url()[:90]}")
    sb.save_screenshot("oauth_timeout.png")
    return False


def do_discord_login(sb, retried: bool = False) -> bool:
    """通过 Discord Token 以 UI 跳转方式登录 bot-hosting.net（参考 Freeze-Renew 的登录流程）"""
    print("\n🔑 通过 Discord Token 进行 UI 跳转登录...")

    # Step 1: 预登录 Discord
    if not prelogin_discord(sb):
        return False

    # Step 2: 打开 bot-hosting 的 Discord OAuth 入口（浏览器跳转到 Discord 授权页）
    print("🔗 打开 bot-hosting.net/login/discord（UI 跳转）...")
    sb.uc_open_with_reconnect("https://bot-hosting.net/login/discord", reconnect_time=6)
    time.sleep(4)

    # Step 3: 处理 Discord 授权页（已授权过会静默跳回，无需点击）
    url = sb.get_current_url()
    if "discord.com" in url:
        print(f"🔍 停留在 Discord 授权页: {url[:90]}")
        if not handle_oauth_page(sb, timeout=75):
            return False
    else:
        print("✅ 静默授权完成，未停留在 Discord 授权页")

    # Step 4: 等待跳回 bot-hosting.net
    for _ in range(15):
        if "bot-hosting.net" in sb.get_current_url():
            break
        time.sleep(1)

    url = sb.get_current_url()

    if "/error/banned" in url:
        print("🚫 账号已被封禁")
        sb.save_screenshot("login_banned.png")
        return False

    if "bot-hosting.net" not in url:
        print(f"❌ 授权后未跳转至 bot-hosting.net，当前 URL：{url}")
        sb.save_screenshot("login_no_redirect.png")
        return False

    try:
        body_text = sb.get_text("body")
    except Exception:
        body_text = ""
    if "fraud" in body_text.lower():
        print("🚫 触发风控（fraud attempt），可能是当前出口 IP 被拦截")
        sb.save_screenshot("login_fraud.png")
        if not retried and restart_warp():
            print("🔄 已更换 WARP 出口 IP，重试 Discord 登录...")
            return do_discord_login(sb, retried=True)
        return False

    for _ in range(30):
        url = sb.get_current_url()
        path = urllib.parse.urlparse(url).path
        if "bot-hosting.net" in url and path != "/login" and not path.startswith("/login/discord"):
            print(f"✅ Discord OAuth 登录成功！当前页面：{url}")
            return True
        time.sleep(0.5)

    print(f"❌ 登录超时或未跳转成功，最终停留在：{url}")
    try:
        body_text = sb.get_text("body")
        print(f"📄 页面正文片段：{body_text[:200].strip()!r}")
    except Exception:
        pass
    sb.save_screenshot("login_timeout.png")
    return False


# 主流程
def main():
    print("#" * 25)
    print("   Bot-hosting 自动续期 (WARP 出口 + Discord UI 登录)")
    print("#" * 25)

    HEADLESS = os.environ.get("HEADLESS", "false").lower() == "true" 

    sb_kwargs = {"uc": True, "headless": HEADLESS}
    print("🌐 使用 WARP 全局出口（由工作流启用，无需挂载代理）")

    global _LOGIN_METHOD

    with SB(**sb_kwargs) as sb:
        try:
            ip = get_current_ip()
            print(f"📍 当前出口IP: {ip}")
        except Exception as e:
            print(f"⚠️ 获取出口 IP 失败: {e}")

        login_ok = False

        # 方式1: SESSION_TOKEN Cookie 登录（默认）
        if SESSION_TOKEN:
            print("🚀 启动浏览器...")
            sb.open("https://bot-hosting.net/")
            sb.wait_for_ready_state_complete()
            sb.sleep(2)

            print("📝 注入 Cookie...")
            for name, value in COOKIES.items():
                if value:
                    sb.add_cookie({"name": name, "value": value, "domain": "bot-hosting.net"})

            print("🌐 访问 https://bot-hosting.net/a/billings ...")
            sb.open("https://bot-hosting.net/a/billings")
            sb.wait_for_ready_state_complete()
            sb.sleep(3)
            current_url = sb.get_current_url()
            current_title = sb.get_title()
            print(f"📝 当前URL: {current_url}, Title: {current_title}")

            if "/a/billings" in current_url and "/login" not in current_url and "error=" not in current_url:
                login_ok = True
                print("✅ SESSION_TOKEN 登录成功, 当前已到达账单页")
            else:
                print(f"❌ SESSION_TOKEN 登录失败，当前URL: {current_url}, 当前标题: {current_title}")

        # 方式2: Discord OAuth 登录（备用，UI 跳转方式）
        if not login_ok and DC_TOKEN:
            _LOGIN_METHOD = "Discord Token (UI)"
            print("\n🔄 SESSION_TOKEN 登录失败或未配置，尝试 Discord UI 跳转登录...")
            if do_discord_login(sb):
                print("🌐 访问 https://bot-hosting.net/a/billings ...")
                sb.open("https://bot-hosting.net/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                current_url = sb.get_current_url()
                current_title = sb.get_title()
                print(f"📝 当前URL: {current_url}, Title: {current_title}")

                if "a/billings" in current_url:
                    login_ok = True
                    print("✅ Discord OAuth 登录成功,当前已到达账单页")
                else:
                    print(f"❌ Discord OAuth 登录后仍未到达账单页，当前URL: {current_url}")
            else:
                print("❌ Discord OAuth 登录失败")

        if not login_ok:
            error_msg = "Cookie 已失效或页面异常"
            if not SESSION_TOKEN and DC_TOKEN:
                error_msg = "Discord OAuth 登录失败"
            elif SESSION_TOKEN and DC_TOKEN:
                error_msg = "SESSION_TOKEN 和 Discord OAuth 均失败"
            send_telegram_message(format_notification("❌ 登录失败", error=error_msg))
            return

        if _LOGIN_METHOD != "SESSION_TOKEN":
            print("ℹ️ 本次使用 Discord OAuth 登录，新的 SESSION_TOKEN 将自动更新到 Secrets")

        # 提取当前到期日期
        sb.sleep(2)
        page_source = sb.get_page_source()
        current_expiry = extract_expiry_date(page_source)
        if current_expiry:
            print(f"📅 当前到期日期: {current_expiry}")
        else:
            print("⚠️ 未能提取当前到期日期")

        # 寻找外部续期按钮
        outer_renew_selector = None
        countdown_text = None
        possible_selectors = [
            'button:contains("Renew")',
            'button:contains("Renew free plan")',
            'a:contains("Renew")',
            '[class*="renew"]',
            '[class*="Renew"]',
        ]

        for selector in possible_selectors:
            try:
                if sb.is_element_visible(selector):
                    button_text = sb.get_text(selector)
                    if "Renew in" in button_text:
                        match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", button_text)
                        if match:
                            countdown_text = match.group(1)
                        break
                    elif "Renew" in button_text and "in" not in button_text.lower():
                        outer_renew_selector = selector
                        print(f"✅ 续期按钮可用: '{button_text}'")
                        break
            except Exception as e:
                pass

        # 点击外部续期按钮等待弹窗
        if outer_renew_selector:
            print("🔄 点击外部续期按钮，等待验证窗口...")
            try:
                sb.sleep(2)
                sb.click(outer_renew_selector)
                sb.sleep(15)  # 等待模态框加载，可能因网络因素加载慢
            except Exception as e:
                print(f"❌ 点击外部按钮失败: {e}")
                send_telegram_message(format_notification("❌ 续期失败", error="点击外部续期按钮出错"))
                return

            # 处理弹窗中的 Turnstile
            print("🔒 检测弹窗中的 Turnstile 验证...")
            turnstile_passed = False
            for attempt in range(1, 4):
                try:
                    sb.uc_gui_click_captcha()
                    time.sleep(12)
                except Exception as e:
                    print(f"⚠️ 点击 Turnstile 出错: {e}")

                if wait_for_turnstile_pass(sb, timeout=20):
                    turnstile_passed = True
                    break
                else:
                    print(f"⏳ 第 {attempt} 次未通过，重试点击...")

            if not turnstile_passed:
                print("❌ Turnstile 验证最终未通过，脚本退出")
                send_telegram_message(format_notification("❌ 续期失败", error="Turnstile 验证未通过"))
                return

            # 点击续期按钮
            print("⏳ 等待续期按钮可用并点击...")
            time.sleep(5) 

            modal_button_clicked = False
            try:
                sb.click('button:contains("Renew for 4 days")', timeout=8)
                modal_button_clicked = True
                print("✅ 已点击续期按钮")
            except Exception as e:
                print(f"续期按钮点击失败: {e}")

            print("⏳ 等待新的过期时间...")
            sb.sleep(6)

            # 提取新的到期日期和倒计时
            new_page_text = sb.get_page_source()
            new_expiry = extract_expiry_date(new_page_text)
            new_match = re.search(r"Renew in (\d{2}:\d{2}:\d{2})", new_page_text)
            if new_match:
                new_countdown = new_match.group(1)
                print(f"✅ 续期成功！新的倒计时: {new_countdown}")
                if new_expiry:
                    print(f"📅 新的到期日期: {new_expiry}")
                send_telegram_message(
                    format_notification(
                        "✅ 续期成功",
                        extra=f"⏱️ 可续期时间: {format_countdown(new_countdown)}后",
                        expiry_date=new_expiry or "（未获取到）"
                    )
                )
            else:
                if new_expiry and new_expiry != current_expiry:
                    print(f"✅ 续期成功，到期日期已更新为: {new_expiry}")
                    send_telegram_message(
                        format_notification(
                            "✅ 续期成功",
                            extra="到期日期已更新",
                            expiry_date=new_expiry
                        )
                    )
                else:
                    print("⚠️ 续期结果未知，到期日期未变化，请手动检查")
                    send_telegram_message(
                        format_notification(
                            "⚠️ 续期可能未成功",
                            extra="请登录后台检查",
                            expiry_date=current_expiry or "（未获取到）"
                        )
                    )

        else:
            if countdown_text:
                friendly = format_countdown(countdown_text)
                print(f"⏳ 未到续期时间，倒计时: {countdown_text} ({friendly})")
                send_telegram_message(
                    format_notification(
                        "⏳ 未到续期时间",
                        extra=f"⏱️ 可续期时间: {friendly}后",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )
            else:
                print("ℹ️ 未找到续期按钮或倒计时，状态未知")
                send_telegram_message(
                    format_notification(
                        "ℹ️ 无需续期",
                        extra="当前状态未知，请手动检查",
                        expiry_date=current_expiry or "（未获取到）"
                    )
                )

        # 更新SESSION_TOKEN 
        print("🔄 检查 SESSION_TOKEN 是否需要更新")
        new_token, token_expiry = get_cookie_info(sb, "session_token")
        old_token = SESSION_TOKEN

        if should_update_cookie(new_token, old_token, token_expiry):
            print("🔄 SESSION_TOKEN 需要更新")
            if GH_TOKEN:
                if update_github_secret("SESSION_TOKEN", new_token):
                    print("✅ SESSION_TOKEN 更新成功")
                else:
                    print("⚠️ 更新失败，请检查 GH_TOKEN 权限")
            else:
                print("⚠️ 未设置 GH_TOKEN，无法自动更新")
                print(f"📋 请手动设置 SESSION_TOKEN = {new_token[:4]}...{new_token[-4:]}")
        else:
            print("✅ SESSION_TOKEN 无需更新")
        
        print("🏁 脚本执行完毕")

if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
iKuai 路由器 API 抓包脚本
用 Selenium 打开真实浏览器 → 登录 → 抓所有 XHR 请求 → 打印出来
"""
import json, time, hashlib
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ── 配置 ───────────────────────────────────────────────────────────────────────
IKUAI_URL   = "http://192.168.31.254"
USERNAME     = "admin"
PASSWORD     = "lixin2324"
# ─────────────────────────────────────────────────────────────────────────────────

def main():
    print("启动浏览器...")
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")          # 无头模式，设为 False 可看到界面
    # options.add_argument("--headless")  # 注释这行可以看到浏览器操作过程
    driver = webdriver.Chrome(options=options)

    captured = []

    # ── 启用 CDP 网络监听 ────────────────────────────────────────────────────
    driver.execute_cdp_cmd("Network.enable", {})

    def capture_request(params):
        req = params.get("request", {})
        url = req.get("url", "")
        if "/Action/" in url:
            captured.append({
                "type": "request",
                "url": url,
                "method": req.get("method"),
                "headers": req.get("headers", {}),
                "postData": req.get("postData", ""),
            })
            print(f"\n📤 请求: {req.get('method')} {url}")
            if req.get("postData"):
                print(f"   Body: {req['postData'][:200]}")

    def capture_response(params):
        resp = params.get("response", {})
        url = resp.get("url", "")
        if "/Action/" in url:
            req_id = params.get("requestId")
            try:
                body = driver.execute_cdp_cmd("Network.getResponseBody",
                                              {"requestId": req_id})
                body_text = body.get("body", "")[:300]
            except:
                body_text = "(无法获取响应体)"
            captured.append({
                "type": "response",
                "url": url,
                "status": resp.get("status"),
                "body": body_text,
            })
            print(f"  📥 响应: {resp.get('status')} {url}")
            print(f"     Body: {body_text[:200]}")

    driver.add_cdp_listener("Network.requestWillBeSent", capture_request)
    driver.add_cdp_listener("Network.responseReceived", capture_response)

    try:
        # ── 1. 打开登录页 ────────────────────────────────────────────────────
        print(f"\n打开 {IKUAI_URL}")
        driver.get(IKUAI_URL)
        time.sleep(2)

        # ── 2. 登录 ──────────────────────────────────────────────────────────
        print("填写登录信息...")
        user_input = driver.find_element(By.CSS_SELECTOR, "input[type='text'], input[name='username']")
        pass_input = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='passwd']")
        user_input.clear()
        user_input.send_keys(USERNAME)
        pass_input.clear()
        pass_input.send_keys(PASSWORD)
        time.sleep(0.5)

        login_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit'], .login-btn, .btn-login")
        login_btn.click()
        print("已点击登录，等待跳转...")
        time.sleep(3)

        # ── 3. 进入链路监控页面（触发 API 调用）────────────────────────────
        print("\n导航到链路监控页面...")
        driver.get(f"{IKUAI_URL}/#/linkMonitor")
        time.sleep(3)

        # 也试试状态概览
        print("导航到状态概览页面...")
        driver.get(f"{IKUAI_URL}/#/statusOverview")
        time.sleep(3)

        # ── 4. 打印所有抓到的请求 ────────────────────────────────────────────
        print("\n" + "="*60)
        print(f"共抓到 {len(captured)} 个相关请求")
        print("="*60)
        for i, item in enumerate(captured):
            print(json.dumps(item, ensure_ascii=False, indent=2))
            print("-" * 40)

    finally:
        time.sleep(2)
        driver.quit()
        print("\n浏览器已关闭")

if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""Playwright 浏览器自动化测试引擎 —— 部署在 Linux 服务器上。

用法（命令行）:
  python test_runner.py --url="https://bpd.test.autobestdevops.com/privacy-policy" \
      --check="content" --expected="Last Updated: Sept 01, 2026" \
      --screenshot-dir=./screenshots

用法（Python 模块）:
  from test_runner import TestRunner
  runner = TestRunner(headless=True)
  result = await runner.run_test_case({
      "id": 1,
      "test_url": "https://bpd.test.autobestdevops.com/privacy-policy",
      "check_type": "content",
      "expected": "Last Updated: Sept 01, 2026",
  })
  print(result["status"], result["screenshot"])  # screenshot 是 base64
"""
import sys, os, base64, json, asyncio

try:
    from playwright.async_api import async_playwright
except ImportError:
    print("请先安装 Playwright: pip install playwright && playwright install chromium")
    raise


# ─── 测试引擎 ──────────────────────────────────────────────────────────────────

class TestRunner:
    """Playwright 浏览器自动化测试引擎。

    check_type 支持:
      - "content"  : 页面文本包含 expected
      - "element"  : CSS 选择器存在，且文本包含 expected（可选）
      - "url"      : 当前 URL 包含 expected
      - "screenshot": 仅截图，不验证
    """

    def __init__(self, headless=True, viewport=None, timeout=30000):
        self.headless = headless
        self.viewport = viewport or {"width": 1440, "height": 900}
        self.timeout = timeout
        self._playwright = None
        self._browser = None

    async def start(self):
        """启动浏览器。"""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

    async def stop(self):
        """关闭浏览器。"""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def run_test_case(self, test_case: dict) -> dict:
        """执行单个测试用例，返回结果字典。

        test_case 格式:
          {
            "id": 1,
            "description": "验证 Privacy Policy 日期",
            "test_url": "https://bpd.test.autobestdevops.com/privacy-policy",
            "check_type": "content",      # content | element | url | screenshot
            "expected": "Last Updated: Sept 01, 2026",  # 可选
            "selector": "h1",             # 可选，check_type=element 时使用
            "wait_until": "networkidle",  # 可选，默认 networkidle
          }

        返回:
          {
            "id": 1,
            "status": "pass" | "fail" | "error",
            "screenshot": "base64..." | None,
            "actual": "页面实际内容摘要",
            "detail": "验证结果详细说明",
            "error": "错误信息（仅 status=error 时）",
          }
        """
        tc_id = test_case.get("id", "?")
        test_url = test_case.get("test_url", "")
        check_type = test_case.get("check_type", "screenshot")
        expected = test_case.get("expected", "")
        selector = test_case.get("selector", "")
        wait_until = test_case.get("wait_until", "networkidle")

        result = {
            "id": tc_id,
            "status": "error",
            "screenshot": None,
            "actual": "",
            "detail": "",
            "error": "",
        }

        if not test_url:
            result["error"] = "缺少 test_url"
            return result

        context = None
        page = None
        try:
            context = await self._browser.new_context(
                viewport=self.viewport,
                ignore_https_errors=True,
            )
            page = await context.new_page()
            await page.set_default_timeout(self.timeout)

            # 打开页面
            await page.goto(test_url, wait_until=wait_until)
            # 额外等 2 秒，确保 JS 渲染完毕
            await asyncio.sleep(2)

            # 截图
            screenshot_bytes = await page.screenshot(full_page=True)
            result["screenshot"] = base64.b64encode(screenshot_bytes).decode("utf-8")

            # 根据 check_type 执行验证
            if check_type == "screenshot":
                result["status"] = "pass"
                result["detail"] = "截图已保存（仅截图模式）"
                return result

            elif check_type == "content":
                body_text = await page.inner_text("body")
                result["actual"] = body_text[:500]
                if expected and expected in body_text:
                    result["status"] = "pass"
                    result["detail"] = "页面包含预期内容: %s" % expected
                else:
                    result["status"] = "fail"
                    result["detail"] = "页面不包含预期内容: %s" % expected

            elif check_type == "element":
                if not selector:
                    result["error"] = "element 模式需要 selector 参数"
                    return result
                try:
                    el = await page.wait_for_selector(selector, timeout=self.timeout)
                    el_text = await el.inner_text() if el else ""
                    result["actual"] = el_text[:500]
                    if expected:
                        if expected in el_text:
                            result["status"] = "pass"
                            result["detail"] = "元素 %s 包含预期内容: %s" % (selector, expected)
                        else:
                            result["status"] = "fail"
                            result["detail"] = "元素 %s 不包含预期内容: %s，实际: %s" % (
                                selector, expected, el_text[:200]
                            )
                    else:
                        result["status"] = "pass"
                        result["detail"] = "元素 %s 存在" % selector
                except Exception as e:
                    result["status"] = "fail"
                    result["detail"] = "元素 %s 未找到: %s" % (selector, str(e))

            elif check_type == "url":
                current_url = page.url
                result["actual"] = current_url
                if expected and expected in current_url:
                    result["status"] = "pass"
                    result["detail"] = "URL 包含预期: %s" % expected
                else:
                    result["status"] = "fail"
                    result["detail"] = "URL 不包含预期: %s，实际: %s" % (expected, current_url)

            else:
                result["error"] = "不支持的 check_type: %s" % check_type

        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)
            result["detail"] = "测试执行异常: %s" % str(e)

        finally:
            if page:
                await page.close()
            if context:
                await context.close()

        return result

    async def run_batch(self, test_cases: list) -> list:
        """批量执行测试用例，返回结果列表。"""
        results = []
        for tc in test_cases:
            r = await self.run_test_case(tc)
            results.append(r)
        return results

    async def screenshot_only(self, url: str) -> str:
        """仅截图指定 URL，返回 base64 图片。"""
        context = await self._browser.new_context(
            viewport=self.viewport,
            ignore_https_errors=True,
        )
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="networkidle")
            await asyncio.sleep(2)
            screenshot_bytes = await page.screenshot(full_page=True)
            return base64.b64encode(screenshot_bytes).decode("utf-8")
        finally:
            await page.close()
            await context.close()


# ─── 命令行入口 ────────────────────────────────────────────────────────────────

async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="Playwright 测试引擎")
    parser.add_argument("--url", required=True, help="测试页面 URL")
    parser.add_argument("--check", default="screenshot",
                        choices=["content", "element", "url", "screenshot"],
                        help="验证类型")
    parser.add_argument("--expected", default="", help="预期内容")
    parser.add_argument("--selector", default="", help="CSS 选择器（check=element 时）")
    parser.add_argument("--screenshot-dir", default="./screenshots", help="截图保存目录")
    parser.add_argument("--headed", action="store_true", help="有头模式（调试用）")
    args = parser.parse_args()

    runner = TestRunner(headless=not args.headed)
    await runner.start()

    try:
        tc = {
            "id": "cli",
            "test_url": args.url,
            "check_type": args.check,
            "expected": args.expected,
            "selector": args.selector,
        }
        result = await runner.run_test_case(tc)

        print(json.dumps({"status": result["status"], "detail": result["detail"]},
                         ensure_ascii=False, indent=2))

        # 保存截图到文件
        if result["screenshot"] and args.screenshot_dir:
            os.makedirs(args.screenshot_dir, exist_ok=True)
            filename = "%s_%s.png" % (args.check, result["status"])
            filepath = os.path.join(args.screenshot_dir, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(result["screenshot"]))
            print("截图: %s" % filepath)
    finally:
        await runner.stop()


if __name__ == "__main__":
    asyncio.run(_main())
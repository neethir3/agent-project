# -*- coding: utf-8 -*-
"""Playwright 浏览器自动化测试引擎 —— 同步 API，部署在 Linux 服务器上。

用法（命令行）:
  python test_runner.py --url="https://bpd.test.autobestdevops.com/privacy-policy" \
      --check="content" --expected="Last Updated: Sept 01, 2026"

用法（Python 模块）:
  from test_runner import TestRunner
  runner = TestRunner(headless=True)
  runner.start()
  result = runner.run_test_case({
      "id": 1,
      "test_url": "https://bpd.test.autobestdevops.com/privacy-policy",
      "check_type": "content",
      "expected": "Last Updated: Sept 01, 2026",
  })
  runner.stop()
"""
import sys, os, base64, json, time

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("请先安装 Playwright: pip install playwright && playwright install chromium")
    raise


# ─── 测试引擎 ──────────────────────────────────────────────────────────────────

class TestRunner:
    """Playwright 浏览器自动化测试引擎（同步 API）。

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

    def start(self):
        """启动浏览器。"""
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )

    def stop(self):
        """关闭浏览器。"""
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def run_test_case(self, test_case: dict) -> dict:
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
            context = self._browser.new_context(
                viewport=self.viewport,
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout)

            # 打开页面
            page.goto(test_url, wait_until=wait_until)
            # 额外等 2 秒，确保 JS 渲染完毕
            time.sleep(2)

            # 截图
            screenshot_bytes = page.screenshot(full_page=True)
            result["screenshot"] = base64.b64encode(screenshot_bytes).decode("utf-8")

            # 根据 check_type 执行验证
            if check_type == "screenshot":
                result["status"] = "pass"
                result["detail"] = "截图已保存（仅截图模式）"
                return result

            elif check_type == "content":
                body_text = page.inner_text("body")
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
                    el = page.wait_for_selector(selector, timeout=self.timeout)
                    el_text = el.inner_text() if el else ""
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
                page.close()
            if context:
                context.close()

        return result

    def run_batch(self, test_cases: list) -> list:
        """批量执行测试用例，返回结果列表。"""
        results = []
        for tc in test_cases:
            r = self.run_test_case(tc)
            results.append(r)
        return results

    def explore_page(self, url: str, wait_until: str = "networkidle") -> dict:
        """探索页面：提取所有链接和页面结构信息。

        返回:
          {
            "url": "页面 URL",
            "title": "页面标题",
            "links": [{"text": "链接文字", "href": "链接地址", "selector": "CSS选择器"}, ...],
            "total_links": 42,
            "internal_links": [只包含同域名的链接],
            "error": "",
          }
        """
        result = {
            "url": url,
            "title": "",
            "links": [],
            "total_links": 0,
            "internal_links": [],
            "error": "",
        }

        context = None
        page = None
        try:
            context = self._browser.new_context(
                viewport=self.viewport,
                ignore_https_errors=True,
            )
            page = context.new_page()
            page.set_default_timeout(self.timeout)
            page.goto(url, wait_until=wait_until)

            import time
            time.sleep(2)  # 等 JS 渲染完毕

            result["title"] = page.title()

            # 提取所有链接
            from urllib.parse import urlparse
            base_domain = urlparse(url).netloc

            links = page.evaluate("""() => {
                const links = [];
                document.querySelectorAll('a[href]').forEach((el, i) => {
                    const href = el.href || '';
                    const text = (el.innerText || el.textContent || '').trim().substring(0, 200);
                    // 生成唯一选择器
                    let selector = '';
                    if (el.id) selector = '#' + el.id;
                    else if (el.className && typeof el.className === 'string') {
                        selector = el.tagName.toLowerCase() + '.' + el.className.split(' ').filter(c => c).slice(0, 2).join('.');
                    }
                    if (!selector || document.querySelectorAll(selector).length > 1) {
                        selector = el.tagName.toLowerCase() + '[href="' + el.getAttribute('href') + '"]';
                    }
                    links.push({
                        text: text,
                        href: href,
                        selector: selector
                    });
                });
                return links;
            }""")

            result["links"] = links
            result["total_links"] = len(links)

            # 分离内部链接
            internal = []
            for link in links:
                href = link.get("href", "")
                if not href:
                    continue
                try:
                    parsed = urlparse(href)
                    if parsed.netloc == base_domain or not parsed.netloc:
                        # 同域名或相对路径
                        if parsed.path and parsed.path != "/":
                            internal.append(link)
                except Exception:
                    pass

            result["internal_links"] = internal

        except Exception as e:
            result["error"] = str(e)

        finally:
            if page:
                page.close()
            if context:
                context.close()

        return result


# ─── 命令行入口 ────────────────────────────────────────────────────────────────

def main():
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
    runner.start()

    try:
        tc = {
            "id": "cli",
            "test_url": args.url,
            "check_type": args.check,
            "expected": args.expected,
            "selector": args.selector,
        }
        result = runner.run_test_case(tc)

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
        runner.stop()


if __name__ == "__main__":
    main()
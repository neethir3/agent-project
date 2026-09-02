# -*- coding: utf-8 -*-
"""LangGraph Agent 主控 —— 部署在 Linux 服务器上。

工具:
  - analyze_pr   : 下载 PR diff → 上传 Dify → 检索 → 生成测试计划
  - run_test     : Playwright 浏览器自动化测试
  - search_kb    : 检索 Dify 知识库

LLM: 兼容 OpenAI API 格式（支持任何 /compatible-mode/v1 端点）。

用法:
  python agent.py  # 启动交互式命令行 Agent

环境变量:
  LLM_API_KEY          LLM API Key（必填）
  LLM_BASE_URL         LLM 服务地址（必填，如 https://xxx/compatible-mode/v1）
  LLM_MODEL            模型名称（默认 qwen-plus）
  AZURE_DEVOPS_PAT     Azure DevOps PAT Token（analyze_pr 需要）
  DIFY_API_KEY         Dify 知识库 API Key（search_kb 需要）
  DIFY_BASE_URL        Dify 服务地址（默认 https://dify-test.uat.autobestdevops.com）
"""
import sys, os, json, asyncio, base64
from typing import TypedDict, Annotated, Optional

# 自动加载 .env 文件
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

try:
    from openai import OpenAI
except ImportError:
    print("请先安装 openai: pip install openai")
    raise

# 可选依赖: langgraph（用于更复杂的 Agent 编排）
try:
    from langgraph.graph import StateGraph, END
    from langgraph.prebuilt import ToolNode
    HAS_LANGGRAPH = True
except ImportError:
    HAS_LANGGRAPH = False
    print("提示: pip install langgraph 可启用 LangGraph 编排模式")


# ─── 配置 ──────────────────────────────────────────────────────────────────────

LLM_API_KEY = os.environ.get("LLM_API_KEY", "") or os.environ.get("DASHSCOPE_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "") or os.environ.get("DASHSCOPE_BASE_URL", "")
MODEL = os.environ.get("LLM_MODEL", "") or os.environ.get("DASHSCOPE_MODEL", "qwen-plus")

AZURE_DEVOPS_PAT = os.environ.get("AZURE_DEVOPS_PAT", "")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")
DIFY_BASE_URL = os.environ.get("DIFY_BASE_URL", "http://localhost:8080")

# Dify 知识库 ID（检索用）
DIFY_DATASET_REQUIREMENTS = os.environ.get("DIFY_DATASET_REQUIREMENTS",
    "6316f2b8-f840-4fea-959f-d67b99a75b37")  # 全页面需求_20260828_v6 (164 docs)
DIFY_DATASET_CHANGES = os.environ.get("DIFY_DATASET_CHANGES",
    "5c22985e-90da-4545-bc91-18b1016167f4")  # 需求变更_20260829 (2 docs)
DIFY_DATASET_TESTS = os.environ.get("DIFY_DATASET_TESTS",
    "52beeba0-3c79-4e31-afc2-1e67f8c6a200")  # 测试方案_20260828_v3 (42 docs)
DIFY_DATASET_POLICY = os.environ.get("DIFY_DATASET_POLICY",
    "895ff3bf-c5ac-447e-8f23-41c713b26929")  # Policy需求_20260901_v2 (9 docs)

# 站点 → test 环境 URL 映射（从文件路径自动匹配站点，生成真实测试 URL）
# test 环境统一格式: https://{site}.uat.autobestdevops.com

def _site_test_url(site: str) -> str:
    """根据站点代码获取 test 环境 URL。"""
    s = site.lower()
    return SITE_DOMAINS.get(s, "https://%s.uat.autobestdevops.com" % s)

SITE_DOMAINS = {
    # 加州网站
    "apw": "https://apw.uat.autobestdevops.com",
    "bpd": "https://bpd.uat.autobestdevops.com",
    "fpg": "https://fpg.uat.autobestdevops.com",
    "gpg": "https://gpg.uat.autobestdevops.com",
    "hpd": "https://hpd.uat.autobestdevops.com",
    "hpn": "https://hpn.uat.autobestdevops.com",
    "ipd": "https://ipd.uat.autobestdevops.com",
    "kpn": "https://kpn.uat.autobestdevops.com",
    "lpn": "https://lpn.uat.autobestdevops.com",
    "mpg": "https://mpg.uat.autobestdevops.com",
    "npd": "https://npd.uat.autobestdevops.com",
    "spd": "https://spd.uat.autobestdevops.com",
    "tpd": "https://tpd.uat.autobestdevops.com",
    # 新网站
    "adpg": "https://adpg.uat.autobestdevops.com",
    "mbpg": "https://mbpg.uat.autobestdevops.com",
    "mzpn": "https://mzpn.uat.autobestdevops.com",
    "mtpg": "https://mtpg.uat.autobestdevops.com",
    "vpg": "https://vpg.uat.autobestdevops.com",
    "vwpg": "https://vwpg.uat.autobestdevops.com",
    # 第二网站
    "cpd": "https://cpd.uat.autobestdevops.com",
    "fpd": "https://fpd.uat.autobestdevops.com",
    "jpd": "https://jpd.uat.autobestdevops.com",
    "tpn": "https://tpn.uat.autobestdevops.com",
}

# ═══════════════════════════════════════════════════════════════════════════════════
# 站点页面结构（告知 LLM 每个站点的页面和 URL，避免猜测）
# ═══════════════════════════════════════════════════════════════════════════════════
# 所有 23 个站点共享同一套代码，URL 结构相同，仅品牌前缀不同
# URL 模板: /service/{brand}-{page_slug}.html
# 部分页面无品牌前缀，如 Contact Us: /service/contact/us.html

# 站点代码 → 品牌前缀（URL 中的品牌标识）
SITE_BRAND = {
    "bpd": "bmw",      # BMWPartsDeal
    "apw": "audi",     # AudiPartsDeal (待确认)
    "fpg": "ford",     # FordPartsGiant (待确认)
    "gpg": "gm",       # GMPartsGiant (待确认)
    "hpd": "honda",    # HondaPartsDeal (待确认)
    "hpn": "honda",    # HondaPartsNow (待确认)
    "ipd": "infiniti", # InfinitiPartsDeal (待确认)
    "kpn": "kia",      # KiaPartsNow (待确认)
    "lpn": "lexus",    # LexusPartsNow (待确认)
    "mpg": "mopar",    # MoparPartsGiant (待确认)
    "npd": "nissan",   # NissanPartsDeal (待确认)
    "spd": "subaru",   # SubaruPartsDeal (待确认)
    "tpd": "toyota",   # ToyotaPartsDeal (待确认)
    "adpg": "audi",    # (待确认)
    "mbpg": "mercedes",# (待确认)
    "mzpn": "mazda",   # (待确认)
    "mtpg": "mitsubishi", # (待确认)
    "vpg": "volvo",    # (待确认)
    "vwpg": "vw",      # (待确认)
    "cpd": "chevrolet",# (待确认)
    "fpd": "ford",     # (待确认)
    "jpd": "jeep",     # (待确认)
    "tpn": "toyota",   # (待确认)
}

# 页面类型 → URL 模板（{brand} 替换为 SITE_BRAND 中的品牌前缀）
PAGE_URL_TEMPLATES = {
    # Policy 页面
    "privacy policy":    "/service/{brand}-privacy_policy.html",
    "terms of use":      "/service/{brand}-terms_of_use.html",
    "sales policy":      "/service/{brand}-sales_policy.html",
    "return policy":     "/service/{brand}-return_policy.html",
    "shipping policy":   "/service/{brand}-shipping_policy.html",
    "warranty policy":   "/service/{brand}-warranty_policy.html",
    # 信息页面
    "about us":          "/service/{brand}-about_us.html",
    "contact us":        "/service/contact/us.html",       # 无品牌前缀
    "help center":       "/service/{brand}-help_center.html",
    "faq":               "/service/{brand}-faq.html",
    "accessibility":     "/service/{brand}-accessibility.html",
    "customer reviews":  "/service/{brand}-customer_reviews.html",
    "site map":          "/sitemap.html",
    # 功能页面
    "track order":       "/online/track/order",
    "login":             "/online/login",
    "my account":        "/online/account/dashboard",
    "vin decoder":       "/vin-decoder.html",
    "parts availability":"/online/tool/pa/check",
    "rma":               "/online/tool/rma",
    # 首页
    "home":              "/",
    "footer":            "/",                              # Footer 在首页
}


def _page_url(site: str, page_name: str) -> str:
    """根据站点代码和页面名称，返回实际 UAT URL。"""
    base = _site_test_url(site)
    brand = SITE_BRAND.get(site.lower(), site.lower())
    page_key = page_name.lower().strip()

    # 精确匹配
    template = PAGE_URL_TEMPLATES.get(page_key)
    if not template:
        # 模糊匹配
        for key, tmpl in PAGE_URL_TEMPLATES.items():
            if key in page_key or page_key in key:
                template = tmpl
                break

    if template:
        path = template.replace("{brand}", brand)
        return base + path
    return base + "/"


def _build_site_structure_text() -> str:
    """生成站点页面结构描述文本，注入 LLM system prompt。"""
    lines = ["## 站点页面结构（所有站点共享同一套 URL 模板）\n"]
    lines.append("每个站点域名: https://{site_code}.uat.autobestdevops.com")
    lines.append("品牌前缀: 每个站点有不同的品牌标识，如 bpd 对应 bmw\n")
    lines.append("### 页面 URL 模板（{brand} 替换为站点品牌前缀）:\n")
    for page_name, template in sorted(PAGE_URL_TEMPLATES.items()):
        lines.append(f"- {page_name}: `{template}`")
    lines.append("\n### 站点 → 品牌前缀:\n")
    for site, brand in sorted(SITE_BRAND.items()):
        lines.append(f"- {site}: `{brand}`")
    lines.append("\n### 从 PR 变更文件推断页面:\n")
    lines.append("PR 变更文件路径如 `/Frontend_正厂整合需求/Policy/1.第一网站/Privacy Policy.md`")
    lines.append("→ 文件名去掉 .md 即为页面名称 → 查上表得 URL 模板 → 替换品牌前缀 → 得实际 URL")
    lines.append("例如: Privacy Policy → /service/{brand}-privacy_policy.html → bpd 站点 → /service/bmw-privacy_policy.html")
    return "\n".join(lines)


SITE_STRUCTURE_TEXT = _build_site_structure_text()

# ─── 工具定义 ──────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze_pr",
            "description": "分析 Azure DevOps PR：下载 diff → 按文件路径检索 Dify 知识库 → 返回变更文件+需求上下文。注意：返回的 suggested_tests 不含精确 URL，需先用 explore_site 探索站点获取实际链接，匹配 file_name 后再用 run_test 执行测试。标准流程: analyze_pr → explore_site → 匹配链接 → run_test",
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_url": {
                        "type": "string",
                        "description": "Azure DevOps PR 链接，如 https://dev.azure.com/autobest/AutoBestChina/_git/PrdReq/pullrequest/29921",
                    },
                },
                "required": ["pr_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test",
            "description": "在 UAT 环境执行浏览器自动化测试（Playwright）。重要：不要猜测 URL！必须先用 explore_site 探索站点获取实际链接，匹配到正确 URL 后再调用此工具。测试失败时分析 actual 内容判断是 404 还是内容缺失。",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_cases": {
                        "type": "array",
                        "description": "测试用例列表，每项包含 test_url, check_type, expected 等字段",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "description": {"type": "string"},
                                "test_url": {"type": "string"},
                                "check_type": {"type": "string", "enum": ["content", "element", "url", "screenshot"]},
                                "expected": {"type": "string"},
                            },
                        },
                    },
                },
                "required": ["test_cases"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_kb",
            "description": "检索 Dify 知识库中的需求文档和需求变更记录，返回相关上下文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索关键词或问题",
                    },
                    "dataset_id": {
                        "type": "string",
                        "description": "知识库 ID（可选，不填则检索所有知识库）",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_kb",
            "description": "从 Azure DevOps Git 仓库读取需求文档，自动创建/更新 Dify 知识库。适用场景：初次建库、增量更新知识库。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {
                        "type": "string",
                        "description": "Azure DevOps 仓库中的目录路径，如 /Frontend_正厂整合需求/Policy",
                    },
                    "kb_name": {
                        "type": "string",
                        "description": "知识库名称，如 Policy需求",
                    },
                    "branch": {
                        "type": "string",
                        "description": "分支名，默认 vnext-b",
                    },
                    "dataset_id": {
                        "type": "string",
                        "description": "追加到已有知识库的 ID（不填则新建）",
                    },
                },
                "required": ["repo_path", "kb_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_site",
            "description": "用 Playwright 打开一个页面，自动提取所有链接。用于发现页面的实际 URL 结构，避免猜测路径。返回页面标题、所有内部链接（文字+URL），供后续匹配 PR 变更文件并精准测试。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "页面入口 URL，如 https://bpd.uat.autobestdevops.com/",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "db_lookup_url",
            "description": "从数据库查站点页面的精确 URL。AutoPart_DecodeUrl_{SITE} 表包含所有页面类型和 URL。不要猜 URL，用这个工具查！用法: site=站点代码(如HPN), keyword=页面关键词(如privacy_policy), type=页面类型(默认Service)。返回完整的 https:// URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {
                        "type": "string",
                        "description": "站点代码，如 HPN, BPD, APW, FPG 等",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "页面关键词，如 privacy_policy, sales_policy, about_us, contact, warranty 等",
                    },
                    "type": {
                        "type": "string",
                        "description": "页面类型，默认 Service（policy页面）。可选: Home, Model, Category, PL, YearPD, Sitemap 等",
                    },
                },
                "required": ["site", "keyword"],
            },
        },
    },
]


# ─── 工具实现 ──────────────────────────────────────────────────────────────────

def _analyze_pr_handler(pr_url: str) -> dict:
    """工具: 分析 PR —— 下载 diff，生成测试计划。"""
    if not AZURE_DEVOPS_PAT:
        return {"error": "未配置 AZURE_DEVOPS_PAT 环境变量"}

    try:
        # 复用 download_pr_diff.py 的函数
        from download_pr_diff import (
            parse_pr_url, get_repo_id, get_pr_info, get_commit_changes,
            get_file_content, generate_diff,
        )

        org, project, repo_name, pr_id = parse_pr_url(pr_url)
        base_url = "https://dev.azure.com/%s" % org

        # 处理 token
        token = base64.b64encode((":" + AZURE_DEVOPS_PAT).encode("ascii")).decode("ascii")

        # 获取 repo id
        repo_id = get_repo_id(base_url, token, project, repo_name)
        if not repo_id:
            return {"error": "无法获取仓库 ID"}

        # 获取 PR 信息
        pr_info = get_pr_info(base_url, token, repo_id, pr_id)
        if not pr_info:
            return {"error": "获取 PR 信息失败"}

        title = pr_info.get("title", "")
        source_commit = pr_info.get("lastMergeSourceCommit", {}).get("commitId", "")
        target_commit = pr_info.get("lastMergeTargetCommit", {}).get("commitId", "")
        source_branch = pr_info.get("sourceRefName", "").replace("refs/heads/", "")
        target_branch = pr_info.get("targetRefName", "").replace("refs/heads/", "")
        status = pr_info.get("status", "")

        if not source_commit or not target_commit:
            return {"error": "无法获取 commit 信息（PR 可能未合并）"}

        # 获取变更文件列表
        changes = get_commit_changes(base_url, token, repo_id, source_commit)
        blobs = {p: v for p, v in changes.items() if v["changeType"] != "delete"}

        # 分析每个变更文件
        changed_files = []
        for file_path, info in sorted(blobs.items()):
            ct = info["changeType"]
            old_content = None
            new_content = None

            if ct == "add":
                new_content = get_file_content(base_url, token, repo_id, file_path, source_commit)
            elif ct == "edit":
                old_content = get_file_content(base_url, token, repo_id, file_path, target_commit)
                new_content = get_file_content(base_url, token, repo_id, file_path, source_commit)
            else:
                continue

            if old_content is None and new_content is None:
                continue

            diff_text = generate_diff(old_content, new_content, file_path)
            changed_files.append({
                "path": file_path,
                "change_type": ct,
                "diff_summary": _summarize_diff(diff_text),
                "diff_lines": diff_text.count("\n"),
            })

        # 生成建议的测试用例
        suggested_tests = _generate_test_suggestions(changed_files)

        # 自动检索知识库，用 diff 文件路径精确匹配需求文档
        kb_context = []
        try:
            for f in changed_files:
                file_path = f["path"]
                # 取文件名（去掉 .md）和父目录名
                file_name = file_path.split("/")[-1].replace(".md", "")
                parts = file_path.strip("/").split("/")
                parent_dir = parts[-2] if len(parts) >= 2 else ""
                # 构造多个检索词，从精确到模糊
                search_queries = []
                if parent_dir and parent_dir not in file_name:
                    search_queries.append("%s %s" % (parent_dir, file_name))
                search_queries.append(file_name)
                if parent_dir:
                    search_queries.append(parent_dir)  # 模糊兜底
                # 逐个尝试，找到第一个高分匹配
                for q in search_queries:
                    r = _search_kb_handler(q)
                    if r.get("results") and r["results"][0]["score"] > 0.3:
                        kb_context.append({
                            "file": file_path,
                            "kb_match": r["results"][0]["content"][:500],
                            "score": r["results"][0]["score"],
                            "query": q,
                        })
                        break  # 找到匹配就下一个文件
        except Exception:
            pass  # KB 检索失败不影响主流程

        return {
            "pr_id": pr_id,
            "pr_title": title,
            "status": status,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "changed_files": changed_files,
            "changed_file_count": len(changed_files),
            "suggested_tests": suggested_tests,
            "kb_context": kb_context,  # 知识库关联上下文
        }

    except Exception as e:
        return {"error": "analyze_pr 执行失败: %s" % str(e)}


def _summarize_diff(diff_text: str) -> str:
    """从 diff 文本中提取简要摘要。"""
    added = []
    removed = []
    for line in diff_text.split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:].strip())
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:].strip())

    summary_parts = []
    if added:
        summary_parts.append("新增: %s" % "; ".join(added[:3]))
    if removed:
        summary_parts.append("删除: %s" % "; ".join(removed[:3]))
    if not summary_parts:
        summary_parts.append("仅格式变化")
    return " | ".join(summary_parts)


def _generate_test_suggestions(changed_files: list) -> list:
    """根据变更文件生成测试建议，自动匹配站点结构生成精确 URL。

    使用 SITE_BRAND + PAGE_URL_TEMPLATES 查表，不再硬编码或猜测。
    """
    tests = []
    tid = 0

    import re as re_mod

    # 从变更文件路径提取站点
    all_sites = set()
    for f in changed_files:
        path = f["path"]
        sites = re_mod.findall(r'\b(%s)\b' % "|".join(SITE_DOMAINS.keys()), path, re_mod.IGNORECASE)
        all_sites.update(s.lower() for s in sites)
    if not all_sites:
        all_sites = {"bpd"}

    for f in changed_files:
        path = f["path"]
        file_name = path.split("/")[-1].replace(".md", "").strip()
        diff_summary = f.get("diff_summary", "")

        # 从 diff 提取关键词
        keywords = []
        if "新增:" in diff_summary:
            added = diff_summary.split("新增:")[1].split("|")[0].strip()
            for word in added.split("; ")[:2]:
                word = word.strip().strip('"').strip("'")
                if len(word) >= 5 and not word.startswith("http"):
                    keywords.append(word[:80])

        for site in sorted(all_sites):
            tid += 1
            # 用页面结构查表生成精确 URL
            test_url = _page_url(site, file_name)

            tests.append({
                "id": tid,
                "description": "验证 %s 页面更新 [%s]" % (file_name, site.upper()),
                "test_url": test_url,
                "file_name": file_name,
                "keywords": keywords,
                "check_type": "content" if keywords else "screenshot",
                "expected": keywords[0] if keywords else "",
                "sites": [site],
            })

    return tests


def _run_test_handler(test_cases: list) -> dict:
    """工具: 执行浏览器自动化测试，支持 test_urls 列表自动展开。"""
    try:
        from test_runner import TestRunner

        # 展开 test_urls（如果有）为多个独立测试用例
        expanded = []
        for tc in test_cases:
            if "test_urls" in tc and tc["test_urls"]:
                for url in tc["test_urls"]:
                    expanded.append({
                        "id": tc.get("id", 0),
                        "description": "%s [%s]" % (tc.get("description", ""), url),
                        "test_url": url,
                        "check_type": tc.get("check_type", "content"),
                        "expected": tc.get("expected", ""),
                        "selector": tc.get("selector", ""),
                    })
            elif "test_url" in tc and tc["test_url"]:
                expanded.append(tc)
            else:
                return {"error": "测试用例缺少 test_url 或 test_urls"}

        runner = TestRunner(headless=True)
        runner.start()
        try:
            results = runner.run_batch(expanded)
        finally:
            runner.stop()

        # 不传截图 base64 给 LLM（太大），只返回状态摘要
        for r in results:
            r.pop("screenshot", None)

        passed = sum(1 for r in results if r["status"] == "pass")
        failed = sum(1 for r in results if r["status"] == "fail")
        errors = sum(1 for r in results if r["status"] == "error")

        return {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "results": results,
        }
    except Exception as e:
        return {"error": "run_test 执行失败: %s" % str(e)}


def _search_kb_handler(query: str, dataset_id: str = "") -> dict:
    """工具: 检索 Dify 知识库。"""
    if not DIFY_API_KEY:
        return {"error": "未配置 DIFY_API_KEY 环境变量"}

    try:
        import dify_upload
        # 注入配置到 dify_upload 模块
        dify_upload.KEY = DIFY_API_KEY
        dify_upload.BASE = DIFY_BASE_URL
        dify_req = dify_upload.req

        # 优先检索核心知识库
        if dataset_id:
            datasets = [dataset_id]
        else:
            datasets = [DIFY_DATASET_REQUIREMENTS, DIFY_DATASET_CHANGES, DIFY_DATASET_TESTS, DIFY_DATASET_POLICY]

        all_results = []
        for ds_id in datasets:
            body = {
                "query": query,
                "retrieval_model": {
                    "search_method": "hybrid_search",
                    "reranking_enable": True,
                    "top_k": 5,
                    "score_threshold_enabled": True,
                    "score_threshold": 0.3,
                },
            }
            st, res = dify_req(
                "/v1/datasets/%s/retrieve" % ds_id, "POST", body
            )
            if st == 200 and isinstance(res, dict):
                for record in res.get("records", []):
                    all_results.append({
                        "content": record.get("content", "")[:500],
                        "score": record.get("score", 0),
                        "dataset_id": ds_id,
                    })

        # 按分数排序，取 top 5
        all_results.sort(key=lambda x: x["score"], reverse=True)
        return {
            "query": query,
            "total_hits": len(all_results),
            "results": all_results[:5],
        }

    except Exception as e:
        return {"error": "search_kb 执行失败: %s" % str(e)}


def _build_kb_handler(repo_path: str, kb_name: str, branch: str = "vnext-b", dataset_id: str = "") -> dict:
    """工具: 从 Azure DevOps 仓库读取需求文档 → 建 Dify 知识库。"""
    try:
        from build_kb import build_kb
        result = build_kb(repo_path, kb_name, branch=branch, dataset_id=dataset_id or None)
        return result
    except Exception as e:
        return {"error": "build_kb 执行失败: %s" % str(e)}


# 工具分发
def _explore_site_handler(url: str) -> dict:
    """工具: 用 Playwright 探索页面，提取所有链接。"""
    try:
        from test_runner import TestRunner
        runner = TestRunner(headless=True)
        runner.start()
        try:
            result = runner.explore_page(url)
        finally:
            runner.stop()
        return result
    except Exception as e:
        return {"error": "explore_site 执行失败: %s" % str(e)}


# AutoPart_DecodeUrl 表名映射（每个站点的主表）
DB_DECODE_TABLE = {
    "adpg": "AutoPart_DecodeUrl_ADPG",
    "apw": "AutoPart_DecodeUrl_APW_Prod",
    "bpd": "AutoPart_DecodeUrl_BPD_Prod_2024_0723",
    "cpd": "AutoPart_DecodeUrl_CPD",
    "cpg": "AutoPart_DecodeUrl_CPG_Prod",
    "fpd": "AutoPart_DecodeUrl_FPD",
    "fpg": "AutoPart_DecodeUrl_FPG",
    "gpg": "AutoPart_DecodeUrl_GPG",
    "hpd": "AutoPart_DecodeUrl_HPD",
    "hpn": "AutoPart_DecodeUrl_HPN",
    "ipd": "AutoPart_DecodeUrl_IPD_Prod",
    "jpd": "AutoPart_DecodeUrl_JPD",
    "kpn": "AutoPart_DecodeUrl_KPN",
    "lpn": "AutoPart_DecodeUrl_LPN",
    "mbpg": "AutoPart_DecodeUrl_MBPG",
    "mpg": "AutoPart_DecodeUrl_MPG_Prod",
    "mtpg": "AutoPart_DecodeUrl_MTPG",
    "mzpn": "AutoPart_DecodeUrl_MZPN",
    "npd": "AutoPart_DecodeUrl_NPD",
    "spd": "AutoPart_DecodeUrl_SPD",
    "tpd": "AutoPart_DecodeUrl_TPD",
    "tpn": "AutoPart_DecodeUrl_tpn_2026_2_13",
    "vpg": "AutoPart_DecodeUrl_VPG",
    "vwpg": "AutoPart_DecodeUrl_VWPG",
}

# 数据库连接配置
DB_CONFIG = {
    "server": "172.24.200.163",
    "port": 1433,
    "user": "testteam",
    "password": "D5swwikd1*0#",
    "database": "AutobestSeo",
}


def _db_lookup_url_handler(site: str, keyword: str, type: str = "Service") -> dict:
    """工具: 从数据库查站点页面的精确 URL。"""
    try:
        import pymssql

        table = DB_DECODE_TABLE.get(site.lower())
        if not table:
            return {"error": "站点 %s 无 DecodeUrl 表（BPD/TPN 等站点可能使用其他机制）" % site.upper()}

        conn = pymssql.connect(
            server=DB_CONFIG["server"],
            port=DB_CONFIG["port"],
            user=DB_CONFIG["user"],
            password=DB_CONFIG["password"],
            database=DB_CONFIG["database"],
            login_timeout=10,
            autocommit=True,
        )
        cursor = conn.cursor()

        # 查询匹配的 URL
        query = (
            "SELECT TOP 10 'https://%s.uat.autobestdevops.com'+url AS FullUrl, url, Type "
            "FROM [%s] "
            "WHERE Type='%s' AND url LIKE '%%%s%%' "
            "ORDER BY url"
        ) % (site.lower(), table, type, keyword.lower())
        cursor.execute(query)
        rows = cursor.fetchall()
        cols = [d[0] for d in cursor.description]

        results = []
        for row in rows:
            results.append(dict(zip(cols, row)))

        conn.close()

        return {
            "site": site.upper(),
            "keyword": keyword,
            "type": type,
            "count": len(results),
            "urls": results,
        }
    except Exception as e:
        return {"error": "db_lookup_url 失败: %s" % str(e)}


TOOL_HANDLERS = {
    "analyze_pr": _analyze_pr_handler,
    "run_test": _run_test_handler,
    "search_kb": _search_kb_handler,
    "build_kb": _build_kb_handler,
    "explore_site": _explore_site_handler,
    "db_lookup_url": _db_lookup_url_handler,
}


# ─── Agent 核心 ────────────────────────────────────────────────────────────────

class Agent:
    """LangGraph Agent 主控。

    支持两种模式:
      - simple: 直接调用 LLM + 工具循环（无需 langgraph）
      - graph: 使用 LangGraph 编排（需 pip install langgraph）
    """

    def __init__(self, model=MODEL, use_graph=False):
        if not LLM_API_KEY:
            raise ValueError("请设置 LLM_API_KEY 环境变量")
        if not LLM_BASE_URL:
            raise ValueError("请设置 LLM_BASE_URL 环境变量")

        self.client = OpenAI(
            api_key=LLM_API_KEY,
            base_url=LLM_BASE_URL,
        )
        self.model = model
        self.use_graph = use_graph and HAS_LANGGRAPH
        self.messages = [
            {
                "role": "system",
                "content": (
                    "你是 AutoBest 前端需求测试专家。\n\n"
                    "## 核心工作流\n"
                    "1. analyze_pr → 获取 PR 变更文件 + diff + Dify 知识库需求上下文\n"
                    "2. db_lookup_url → 从数据库查每个变更文件对应的精确 URL（不要猜！）\n"
                    "   例如: db_lookup_url(site='HPN', keyword='privacy_policy', type='Service')\n"
                    "3. run_test → 用查到的精确 URL 执行测试\n\n"
                    "## 规则\n"
                    "- 永远不要猜测 URL！用 db_lookup_url 从数据库查\n"
                    "- 变更文件路径如 /Frontend_正厂整合需求/Policy/1.第一网站/Privacy Policy.md\n"
                    "  文件名 = Privacy Policy → keyword='privacy_policy'\n"
                    "- 测试失败时先确认 URL 正确，再判断内容问题\n"
                    "- 结合 Dify KB 需求原文 + diff 变更做综合分析\n"
                    "- 所有 23 个站点共享同一套代码，URL 结构相同仅品牌前缀不同\n"
                ),
            }
        ]

        if self.use_graph:
            self._build_graph()

    def _build_graph(self):
        """构建 LangGraph 状态图。"""
        from langgraph.graph import StateGraph, END
        from langgraph.prebuilt import ToolNode

        class State(TypedDict):
            messages: list

        def call_model(state: State) -> State:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=state["messages"],
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message
            new_messages = state["messages"] + [msg.model_dump()]
            return {"messages": new_messages}

        def should_continue(state: State) -> str:
            last_msg = state["messages"][-1]
            if last_msg.get("tool_calls"):
                return "tools"
            return END

        def call_tools(state: State) -> State:
            last_msg = state["messages"][-1]
            new_messages = state["messages"][:]
            for tc in last_msg.get("tool_calls", []):
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"]["arguments"])
                handler = TOOL_HANDLERS.get(fn_name)
                if handler:
                    result = handler(**fn_args)
                else:
                    result = {"error": "未知工具: %s" % fn_name}
                new_messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False),
                })
            return {"messages": new_messages}

        graph = StateGraph(State)
        graph.add_node("agent", call_model)
        graph.add_node("tools", call_tools)
        graph.set_entry_point("agent")
        graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
        graph.add_edge("tools", "agent")
        self.graph = graph.compile()

    def chat(self, user_message: str) -> str:
        """与 Agent 对话，返回 Agent 的文本回复。"""
        self.messages.append({
            "role": "user",
            "content": user_message,
        })

        if self.use_graph:
            return self._chat_graph()

        # 简单模式: 循环调用 LLM + 工具
        max_turns = 10
        for _ in range(max_turns):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            msg = response.choices[0].message

            # 如果 LLM 直接回复（无工具调用）
            if not msg.tool_calls:
                self.messages.append({"role": "assistant", "content": msg.content})
                return msg.content or ""

            # 有工具调用
            self.messages.append(msg.model_dump())

            for tc in msg.tool_calls:
                fn_name = tc.function.name
                fn_args = json.loads(tc.function.arguments)
                print(f"  🔧 调用工具: {fn_name}({json.dumps(fn_args, ensure_ascii=False)})")

                handler = TOOL_HANDLERS.get(fn_name)
                if handler:
                    result = handler(**fn_args)
                else:
                    result = {"error": "未知工具: %s" % fn_name}

                self.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False, indent=2),
                })
                print(f"  ✅ 工具返回: {json.dumps(result, ensure_ascii=False)[:200]}...")

        return "已达到最大工具调用次数，请检查任务是否过于复杂。"

    def _chat_graph(self) -> str:
        """LangGraph 模式对话。"""
        result = self.graph.invoke({"messages": self.messages})
        final_msgs = result["messages"]
        last_msg = final_msgs[-1]
        if isinstance(last_msg, dict):
            self.messages = final_msgs
            return last_msg.get("content", "")
        return ""


# ─── 命令行入口 ────────────────────────────────────────────────────────────────

def main():
    """交互式命令行 Agent。"""
    print("=" * 60)
    print("  Agent 工作台")
    print("  模型: %s  |  工具: %s" % (MODEL, ", ".join(TOOL_HANDLERS.keys())))
    print("  输入 'quit' 退出, 'tools' 查看工具列表")
    print("=" * 60)

    agent = Agent(use_graph=False)

    while True:
        try:
            user_input = input("\n🧑 你: ").strip()
            if not user_input:
                continue
            if user_input.lower() in ("quit", "exit", "q"):
                print("再见!")
                break
            if user_input.lower() == "tools":
                for t in TOOLS:
                    print("  📋 %s: %s" % (t["function"]["name"], t["function"]["description"]))
                continue

            print()
            reply = agent.chat(user_input)
            print("🤖 Agent: %s" % reply)

        except KeyboardInterrupt:
            print("\n再见!")
            break
        except Exception as e:
            print("❌ 错误: %s" % e)


if __name__ == "__main__":
    main()
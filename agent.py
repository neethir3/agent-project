# -*- coding: utf-8 -*-
"""LangGraph Agent 主控 —— 部署在 Linux 服务器上。

工具:
  - analyze_pr   : 下载 PR diff → 上传 Dify → 检索 → 生成测试计划
  - run_test     : Playwright 浏览器自动化测试
  - search_kb    : 检索 Dify 知识库

LLM: 阿里百炼 (DashScope)，兼容 OpenAI API 格式。

用法:
  python agent.py  # 启动交互式命令行 Agent

环境变量:
  DASHSCOPE_API_KEY    阿里百炼 API Key（必填）
  AZURE_DEVOPS_PAT     Azure DevOps PAT Token（analyze_pr 需要）
  DIFY_API_KEY         Dify 知识库 API Key（search_kb 需要）
  DIFY_BASE_URL        Dify 服务地址（默认 https://dify-test.uat.autobestdevops.com）
"""
import sys, os, json, asyncio, base64
from typing import TypedDict, Annotated, Optional

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

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL = os.environ.get("DASHSCOPE_MODEL", "qwen-plus")

AZURE_DEVOPS_PAT = os.environ.get("AZURE_DEVOPS_PAT", "")
DIFY_API_KEY = os.environ.get("DIFY_API_KEY", "")
DIFY_BASE_URL = os.environ.get("DIFY_BASE_URL", "https://dify-test.uat.autobestdevops.com")

# ─── 工具定义 ──────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "analyze_pr",
            "description": "分析 Azure DevOps PR：下载 diff、上传 Dify 知识库、检索相关需求、生成结构化测试计划。",
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
            "description": "在 test 环境执行浏览器自动化测试（Playwright），验证页面内容、元素、URL 等。",
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

        return {
            "pr_id": pr_id,
            "pr_title": title,
            "status": status,
            "source_branch": source_branch,
            "target_branch": target_branch,
            "changed_files": changed_files,
            "changed_file_count": len(changed_files),
            "suggested_tests": suggested_tests,
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
    """根据变更文件生成测试建议。"""
    tests = []
    tid = 0

    # 从变更文件路径推断站点和测试范围
    for f in changed_files:
        path = f["path"]
        # 提取站点信息
        import re
        sites = re.findall(r'\b(apw|bpd|gpg|npa|npp|wpi|ape|bpe|npe|wpe)\b', path, re.IGNORECASE)
        sites = list(set(s.lower() for s in sites))

        # 根据文件路径推断测试 URL 模式
        if "privacy-policy" in path.lower() or "privacy policy" in path.lower():
            tid += 1
            tests.append({
                "id": tid,
                "description": "验证 Privacy Policy 页面更新",
                "test_url": "{site}/privacy-policy",
                "check_type": "content",
                "expected": "Last Updated",
                "sites": sites or ["bpd"],
            })
        elif "terms" in path.lower():
            tid += 1
            tests.append({
                "id": tid,
                "description": "验证 Terms of Use 页面更新",
                "test_url": "{site}/terms-of-use",
                "check_type": "content",
                "expected": "Terms",
                "sites": sites or ["bpd"],
            })
        elif "footer" in path.lower():
            tid += 1
            tests.append({
                "id": tid,
                "description": "验证 Footer 更新",
                "test_url": "{site}",
                "check_type": "element",
                "selector": "footer",
                "sites": sites or ["bpd"],
            })
        else:
            tid += 1
            tests.append({
                "id": tid,
                "description": "验证变更文件 %s 对应页面" % path.split("/")[-1].replace(".md", ""),
                "test_url": "{site}",
                "check_type": "screenshot",
                "expected": "",
                "sites": sites or ["bpd"],
            })

    return tests


def _run_test_handler(test_cases: list) -> dict:
    """工具: 执行浏览器自动化测试。"""
    try:
        from test_runner import TestRunner

        async def _run():
            runner = TestRunner(headless=True)
            await runner.start()
            try:
                results = await runner.run_batch(test_cases)
                return results
            finally:
                await runner.stop()

        results = asyncio.run(_run())

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
        from dify_upload import req as dify_req

        # 搜索知识库
        if dataset_id:
            datasets = [dataset_id]
        else:
            # 获取所有知识库列表
            st, data = dify_req(DIFY_BASE_URL, DIFY_API_KEY, "/v1/datasets?page=1&limit=50")
            if isinstance(data, list):
                datasets = [d["id"] for d in data]
            elif isinstance(data, dict):
                datasets = [d["id"] for d in data.get("data", [])]
            else:
                return {"error": "获取知识库列表失败"}

        all_results = []
        for ds_id in datasets[:2]:  # 最多检索 2 个知识库
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
                DIFY_BASE_URL, DIFY_API_KEY,
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


# 工具分发
TOOL_HANDLERS = {
    "analyze_pr": _analyze_pr_handler,
    "run_test": _run_test_handler,
    "search_kb": _search_kb_handler,
}


# ─── Agent 核心 ────────────────────────────────────────────────────────────────

class Agent:
    """LangGraph Agent 主控。

    支持两种模式:
      - simple: 直接调用 LLM + 工具循环（无需 langgraph）
      - graph: 使用 LangGraph 编排（需 pip install langgraph）
    """

    def __init__(self, model=MODEL, use_graph=False):
        if not DASHSCOPE_API_KEY:
            raise ValueError("请设置 DASHSCOPE_API_KEY 环境变量")

        self.client = OpenAI(
            api_key=DASHSCOPE_API_KEY,
            base_url=DASHSCOPE_BASE_URL,
        )
        self.model = model
        self.use_graph = use_graph and HAS_LANGGRAPH
        self.messages = []

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
        max_turns = 5
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
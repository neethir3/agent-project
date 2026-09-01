# -*- coding: utf-8 -*-
"""FastAPI HTTP 服务 —— Agent 工作台后端，部署在 Linux 服务器上。

启动:
  python server.py
  uvicorn server:app --host 127.0.0.1 --port 8800 --reload

端点:
  GET  /api/health             健康检查
  POST /api/analyze-pr         分析 PR → 生成测试计划
  POST /api/run-test           执行浏览器自动化测试
  POST /api/chat               Agent 对话（通用入口）
  GET  /api/screenshot         获取截图（by base64 或 URL）

环境变量:
  DASHSCOPE_API_KEY    阿里百炼 API Key
  AZURE_DEVOPS_PAT     Azure DevOps PAT Token
  DIFY_API_KEY         Dify 知识库 API Key
  DIFY_BASE_URL        Dify 服务地址
  PORT                 服务端口（默认 8800）
"""
import sys, os, json, asyncio, base64
from datetime import datetime

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel, Field
except ImportError:
    print("请先安装 fastapi: pip install fastapi uvicorn pydantic")
    raise

# ─── 配置 ──────────────────────────────────────────────────────────────────────

PORT = int(os.environ.get("PORT", "8800"))
HOST = os.environ.get("HOST", "127.0.0.1")

# ─── FastAPI 应用 ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Agent 工作台 API",
    description="PR 分析 + 自动化测试 Agent 后端服务",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── 请求/响应模型 ─────────────────────────────────────────────────────────────

class AnalyzePRRequest(BaseModel):
    pr_url: str = Field(..., description="Azure DevOps PR 链接")

class AnalyzePRResponse(BaseModel):
    pr_id: str = ""
    pr_title: str = ""
    status: str = ""
    source_branch: str = ""
    target_branch: str = ""
    changed_files: list = []
    changed_file_count: int = 0
    suggested_tests: list = []
    error: str = ""


class TestCase(BaseModel):
    id: int = Field(..., description="测试用例 ID")
    description: str = Field(default="", description="用例描述")
    test_url: str = Field(..., description="测试页面 URL")
    check_type: str = Field(default="content", description="验证类型: content | element | url | screenshot")
    expected: str = Field(default="", description="预期内容")
    selector: str = Field(default="", description="CSS 选择器（check_type=element 时）")
    sites: list = Field(default_factory=list, description="适用站点列表")

class RunTestRequest(BaseModel):
    test_cases: list[TestCase] = Field(..., description="测试用例列表")
    headless: bool = Field(default=True, description="是否无头模式")

class TestResult(BaseModel):
    id: int
    status: str
    screenshot: str | None = None
    actual: str = ""
    detail: str = ""
    error: str = ""

class RunTestResponse(BaseModel):
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    results: list[TestResult] = []
    error: str = ""


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    history: list = Field(default_factory=list, description="对话历史")


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    timestamp: str = ""
    tools: list = []
    model: str = ""


# ─── 懒加载 Agent 组件 ─────────────────────────────────────────────────────────

_agent = None
_test_runner = None


def get_agent():
    global _agent
    if _agent is None:
        from agent import Agent, MODEL, TOOL_HANDLERS
        _agent = Agent(use_graph=False)
    return _agent


async def get_test_runner():
    global _test_runner
    if _test_runner is None:
        from test_runner import TestRunner
        _test_runner = TestRunner(headless=True)
        await _test_runner.start()
    return _test_runner


# ─── API 端点 ──────────────────────────────────────────────────────────────────

@app.get("/api/health", response_model=HealthResponse)
async def health_check():
    """健康检查 + 服务信息。"""
    from agent import MODEL, TOOL_HANDLERS
    return HealthResponse(
        status="ok",
        version="0.1.0",
        timestamp=datetime.now().isoformat(),
        tools=list(TOOL_HANDLERS.keys()),
        model=MODEL,
    )


@app.post("/api/analyze-pr", response_model=AnalyzePRResponse)
async def analyze_pr(req: AnalyzePRRequest):
    """分析 Azure DevOps PR，下载 diff，检索知识库，生成测试计划。"""
    from agent import _analyze_pr_handler

    try:
        result = _analyze_pr_handler(req.pr_url)
        if "error" in result:
            return AnalyzePRResponse(error=result["error"])
        return AnalyzePRResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/run-test", response_model=RunTestResponse)
async def run_test(req: RunTestRequest):
    """执行浏览器自动化测试。"""
    from agent import _run_test_handler

    try:
        # 转换为 dict 列表
        test_cases = [tc.model_dump() for tc in req.test_cases]
        result = _run_test_handler(test_cases)
        if "error" in result:
            return RunTestResponse(error=result["error"])

        return RunTestResponse(
            total=result.get("total", 0),
            passed=result.get("passed", 0),
            failed=result.get("failed", 0),
            errors=result.get("errors", 0),
            results=[TestResult(**r) for r in result.get("results", [])],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Agent 对话入口 —— 通用 LLM + 工具调用。"""
    try:
        agent = get_agent()
        reply = agent.chat(req.message)
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/screenshot")
async def screenshot(url: str):
    """对指定 URL 截图，返回 base64 图片。"""
    try:
        from test_runner import TestRunner
        runner = TestRunner(headless=True)
        await runner.start()
        try:
            img = await runner.screenshot_only(url)
            return {"url": url, "screenshot": img}
        finally:
            await runner.stop()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── 启动入口 ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("=" * 60)
    print("  Agent 工作台 API 服务")
    print("  地址: http://%s:%d" % (HOST, PORT))
    print("  文档: http://%s:%d/docs" % (HOST, PORT))
    print("=" * 60)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
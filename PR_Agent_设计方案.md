# PR 自动化测试 Agent 设计方案

## 1. 概述

开发一个 AI Agent，接收 Azure DevOps PR 链接后自动：
1. 下载 PR diff，分析需求变更
2. 检索 Dify 知识库（需求文档 + 历史变更），理解完整上下文
3. 生成结构化测试计划
4. 在 test 环境网站执行自动化测试（Playwright 浏览器）
5. 返回测试结果（PASS/FAIL + 截图）

用户通过 Dify Chatbot 界面操作，全程无需离开浏览器。

## 2. 架构

```
┌─ 用户浏览器 ───────────────────────────────────────────────┐
│  https://dify-test.uat.autobestdevops.com                 │
│  粘贴 PR 链接 → 查看测试计划 → 确认 → 查看测试结果          │
└──────────────────────┬────────────────────────────────────┘
                       │ HTTPS
                       ▼
┌─ Dify 服务器 (美国, us-uat-test-node-02) ──────────────────┐
│  Docker: dify-api, dify-web, dify-agent-backend           │
│  ┌──────────────────────────────────────────────────┐     │
│  │  Dify Chatbot (Agent 模式)                        │     │
│  │  - 知识库: 需求库 + 需求变更库                      │     │
│  │  - LLM: 已配模型                                   │     │
│  │  - 工具: 2个 HTTP 接口(调用本地 Agent)               │     │
│  └──────────────────────────────────────────────────┘     │
└────────────────────────────────────────────────────────────┘
                       │ HTTP (Dify Agent 工具调用)
                       ▼
┌─ Agent 服务器 (上海, 待申请) ───────────────────────────────┐
│  ┌──────────────────────────────────────────────────┐     │
│  │  server.py          FastAPI HTTP 服务 (端口 8800) │     │
│  │  test_runner.py     Playwright 浏览器自动化        │     │
│  │  download_pr_diff.py  Azure DevOps PR diff 下载   │     │
│  │  Chromium           headless 浏览器               │     │
│  └──────────────────────────────────────────────────┘     │
│                                                          │
│  网络要求:                                                │
│  → 出站: test 环境网站 (https://*.test.autobestdevops.com)│
│  → 出站: Azure DevOps (https://dev.azure.com)            │
│  → 出站: Dify 美国服务器 (https://dify-test.uat...)       │
│  ← 入站: 仅 127.0.0.1:8800 (Dify 通过公网域名回调)        │
└──────────────────────────────────────────────────────────┘
```

## 3. 数据流

```
用户: "分析 PR https://dev.azure.com/autobest/AutoBestChina/_git/PrdReq/pullrequest/29921"
    │
    ▼
Dify Chatbot (LLM 推理)
    │
    ├─(1)─→ HTTP POST http://<agent-ip>:8800/api/analyze-pr
    │       { "pr_url": "https://..." }
    │       
    │       本地 Agent:
    │       ├─ 下载 PR diff (Azure DevOps API)
    │       ├─ 上传到 Dify 需求变更库
    │       ├─ 检索 Dify 两个知识库
    │       └─ 返回结构化分析
    │       ← { "pr_title": "Policy AI", "files": [...], "suggested_tests": [...] }
    │
    Dify 展示测试计划给用户
    │
用户: "确认"
    │
    ├─(2)─→ HTTP POST http://<agent-ip>:8800/api/run-test
    │       { "test_cases": [...], "base_url": "https://bpd.test.autobestdevops.com" }
    │       
    │       本地 Agent:
    │       ├─ Playwright 打开 test 环境页面
    │       ├─ 逐项验证（截图、内容比对）
    │       └─ 返回结果
    │       ← { "results": [{ "status": "pass", "screenshot": "...", "detail": "..." }] }
    │
    Dify 展示测试结果（PASS/FAIL + 截图）
```

## 4. 组件说明

### 4.1 Dify 侧（已有，仅需配置）

| 组件 | 说明 |
|---|---|
| 需求知识库 | 全量产品需求文档，hybrid search + rerank |
| 需求变更知识库 | 每次 PR 的 diff，增量追加 |
| Chatbot (Agent 模式) | 挂两个知识库 + 2个 HTTP 工具 |
| LLM | 负责推理，生成测试计划 |

### 4.2 Agent 侧（待开发，部署在上海）

| 文件 | 功能 | 技术 |
|---|---|---|
| `server.py` | HTTP API 服务 | FastAPI + uvicorn |
| `test_runner.py` | 浏览器自动化测试 | Playwright + Chromium |
| `download_pr_diff.py` | PR diff 下载 + Dify 上传 | 已开发完成 |

### 4.3 API 接口

#### POST /api/analyze-pr
```
输入: { "pr_url": "https://dev.azure.com/..." }
输出: {
  "pr_title": "Policy AI",
  "pr_date": "2026-08-22",
  "source_branch": "Grady",
  "target_branch": "vnext-b",
  "changed_files": [
    {
      "path": "/Frontend_正厂整合需求/Policy/1.第一网站/Privacy Policy.md",
      "change_type": "edit",
      "diff_summary": "增加AI声明，删除Privacy Email",
      "affected_sites": ["apw", "bpd", "gpg", ...],
      "test_scope": "所有站点 Privacy Policy 页面"
    }
  ],
  "suggested_tests": [
    {
      "id": 1,
      "description": "验证 Privacy Policy 页面日期已更新为 Sept 01, 2026",
      "test_url": "{site}/privacy-policy",
      "check_type": "content",
      "expected": "Last Updated: Sept 01, 2026"
    },
    ...
  ]
}
```

#### POST /api/run-test
```
输入: {
  "test_case": {
    "id": 1,
    "description": "验证 Privacy Policy 日期",
    "test_url": "https://bpd.test.autobestdevops.com/privacy-policy",
    "check_type": "content",
    "expected": "Last Updated: Sept 01, 2026"
  }
}
输出: {
  "id": 1,
  "status": "pass" | "fail",
  "screenshot": "base64 或文件路径",
  "actual": "页面实际内容摘要",
  "detail": "验证结果详细说明"
}
```

## 5. 服务器需求

### Agent 服务器（上海）

| 资源 | 要求 | 备注 |
|---|---|---|
| CPU | 2核+ | 建议 4核，Chromium 多标签并行 |
| 内存 | 4GB+ | Chromium 稳定运行 |
| 磁盘 | 10GB 可用 | 截图存储 + Python 依赖 |
| OS | CentOS 7+ / Ubuntu 20+ | 与 Dify 服务器一致用 CentOS |
| Python | 3.9+ | |
| 浏览器 | Chromium (Playwright 自带) | |

### 网络要求

| 方向 | 目标 | 端口 | 用途 |
|---|---|---|---|
| 出站 | *.test.autobestdevops.com | 443 | 测试环境网站 |
| 出站 | dev.azure.com | 443 | PR diff 下载 |
| 出站 | dify-test.uat.autobestdevops.com | 443 | Dify 知识库 API |
| 入站 | （仅本机） | 8800 | Agent HTTP 服务 |

> **注意**：Agent 服务建议只监听 127.0.0.1。如果 Dify 和 Agent 不在同一台机器，需确认 Dify 能否通过内网访问 Agent 的 8800 端口。

## 6. 安全

- Agent 不暴露公网端口，仅内网通信
- Azure DevOps PAT token 使用只读权限（Code Read）
- 测试仅在 test 环境执行，不触碰生产
- Dify API Key 通过环境变量注入，不硬编码

## 7. 实施计划

| 阶段 | 内容 | 依赖 |
|---|---|---|
| **1. 代码开发** | server.py + test_runner.py 开发 | 无 |
| **2. 本地验证** | 开发者本机端到端跑通 | 阶段 1 |
| **3. 服务器申请** | 上海 Linux 服务器，网络打通 | 阶段 2 |
| **4. 部署上线** | 安装依赖，部署 agent，配置 systemd | 阶段 3 |
| **5. Dify 配置** | 建 Chatbot Agent，挂工具，端到端验证 | 阶段 4 |

## 8. 待确认事项

1. **Agent 部署位置**：上海是否有服务器能同时访问 test 环境和 Dify 美国服务器？
2. **test 环境域名格式**：`*.test.autobestdevops.com` 是否正确？需要完整列表
3. **Dify 模型**：Dify 配置的 LLM 是什么模型？是否支持 Function Calling？
4. **Agent 与 Dify 通信**：如果不在同一台机器，Dify 如何调用 Agent？通过公网还是内网？
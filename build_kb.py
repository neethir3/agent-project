# -*- coding: utf-8 -*-
"""从 Azure DevOps Git 仓库读取需求文档，自动建 Dify 知识库。

用法:
  python build_kb.py <repo_path> <kb_name> [选项]

  repo_path: Azure DevOps 仓库中的目录路径，如 "/Frontend_正厂整合需求/Policy"
  kb_name:   知识库名称，如 "Policy需求"

选项:
  --branch=BRANCH         分支名（默认 vnext-b）
  --dataset-id=ID         追加到已有知识库（不填则新建）
  --skip=PATTERN          跳过匹配的文件/目录
  --dry                   仅预览，不实际上传

环境变量:
  AZURE_DEVOPS_PAT    Azure DevOps PAT Token
  DIFY_API_KEY        Dify 知识库 API Key
  DIFY_BASE_URL       Dify 服务地址

示例:
  python build_kb.py "/Frontend_正厂整合需求/Policy" "Policy需求"
  python build_kb.py "/Frontend_正厂整合需求" "全页面需求" --skip=NoUse
"""
import sys, os, json, base64, urllib.request, urllib.error, urllib.parse
from datetime import date

# ─── 配置 ──────────────────────────────────────────────────────────────────────

AZURE_ORG = "autobest"
AZURE_PROJECT = "AutoBestChina"
AZURE_REPO = "PrdReq"
AZURE_REPO_ID = "4aab5e4b-fa13-4c12-bbe4-4fb676595360"
AZURE_BASE = "https://dev.azure.com/%s" % AZURE_ORG

AZURE_PAT = os.environ.get("AZURE_DEVOPS_PAT", "")
DIFY_KEY = os.environ.get("DIFY_API_KEY", "")
DIFY_BASE = os.environ.get("DIFY_BASE_URL", "https://dify-test.uat.autobestdevops.com")


# ─── Azure DevOps API ──────────────────────────────────────────────────────────

def azure_token():
    return base64.b64encode((":" + AZURE_PAT).encode("ascii")).decode("ascii")


def azure_req(path, method="GET"):
    """调用 Azure DevOps REST API。"""
    headers = {"Authorization": "Basic " + azure_token()}
    r = urllib.request.Request(AZURE_BASE + path, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)


def list_files(repo_path, branch="vnext-b"):
    """递归列出仓库目录下所有 .md 文件。"""
    encoded = urllib.parse.quote(repo_path, safe="")
    api_path = "/%s/_apis/git/repositories/%s/items?scopePath=%s&recursionLevel=full&versionDescriptor.version=%s&versionDescriptor.versionType=branch&api-version=7.0" % (
        AZURE_PROJECT, AZURE_REPO_ID, encoded, branch
    )
    st, data = azure_req(api_path)
    if st != 200:
        print("获取文件列表失败: %s" % str(data)[:200])
        return []

    items = data if isinstance(data, list) else data.get("value", [])
    md_files = []
    for item in items:
        if item.get("gitObjectType") == "blob" and item["path"].lower().endswith(".md"):
            md_files.append(item["path"])
    return sorted(md_files)


def read_file(file_path, branch="vnext-b"):
    """读取仓库中单个文件的内容。"""
    encoded = urllib.parse.quote(file_path, safe="")
    api_path = "/%s/_apis/git/repositories/%s/items?path=%s&versionDescriptor.version=%s&versionDescriptor.versionType=branch&api-version=7.0" % (
        AZURE_PROJECT, AZURE_REPO_ID, encoded, branch
    )
    headers = {"Authorization": "Basic " + azure_token()}
    r = urllib.request.Request(AZURE_BASE + api_path, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print("  读取失败: %s - %s" % (file_path, str(e)[:100]))
        return None


# ─── Dify API ──────────────────────────────────────────────────────────────────

def dify_req(path, method="GET", body=None):
    headers = {"Authorization": "Bearer " + DIFY_KEY}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(DIFY_BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)


def create_kb(name):
    """创建知识库，返回 (id, actual_name)。"""
    body = {
        "name": name,
        "indexing_technique": "high_quality",
        "retrieval_model": {
            "search_method": "hybrid_search",
            "reranking_enable": True,
            "reranking_mode": "reranking_model",
            "reranking_model": {
                "reranking_provider_name": "langgenius/xinference/xinference",
                "reranking_model_name": "bge-reranker-v2-m3",
            },
            "top_k": 8,
            "score_threshold_enabled": True,
            "score_threshold": 0.3,
        },
    }

    def _try_create(b):
        st, r = dify_req("/v1/datasets", "POST", b)
        if isinstance(r, dict) and r.get("id"):
            return r["id"], b["name"]
        return None, None, st, r

    did, actual_name, st, res = _try_create(body)
    if did:
        return did, actual_name

    res_str = str(res) if not isinstance(res, dict) else str(res.get("message", ""))
    if "already exists" in res_str.lower():
        for n in range(2, 100):
            new_name = "%s_v%d" % (name, n)
            body["name"] = new_name
            did, actual_name, st2, res2 = _try_create(body)
            if did:
                return did, actual_name
            res2_str = str(res2) if not isinstance(res2, dict) else str(res2.get("message", ""))
            if "already exists" not in res2_str.lower():
                print("create err (retry %s) %s %s" % (new_name, st2, str(res2)[:200]))
                return None, None
        print("create err: tried _v2.._v99, all taken")
        return None, None
    print("create err", st, str(res)[:200])
    return None, None


def upload_doc(dataset_id, name, text):
    """上传文档到知识库。"""
    body = {
        "name": name,
        "text": text,
        "indexing_technique": "high_quality",
        "process_rule": {"mode": "automatic"},
    }
    st, res = dify_req("/v1/datasets/%s/document/create-by-text" % dataset_id, "POST", body)
    return st, res


# ─── 大文本切片 ────────────────────────────────────────────────────────────────

MAX_CHARS = 50000

def split_text(text):
    """超大文本按标题边界切分。"""
    if len(text) <= MAX_CHARS:
        return [text]

    import re
    parts = re.split(r"(?=\n#{1,6}(?:\s|$))", text)
    chunks, buf = [], ""
    for blk in parts:
        if not blk.strip():
            continue
        if buf and len(buf) + len(blk) > MAX_CHARS:
            if buf.strip():
                chunks.append(buf)
            buf = blk
            while len(buf) > MAX_CHARS:
                paras = buf.split("\n\n")
                chunks.append(paras[0])
                buf = "\n\n".join(paras[1:]) if len(paras) > 1 else ""
        else:
            buf = (buf + blk) if buf else blk
    if buf and buf.strip():
        chunks.append(buf)
    return chunks if chunks else [text]


# ─── Main ──────────────────────────────────────────────────────────────────────

def build_kb(repo_path, kb_name, branch="vnext-b", dataset_id=None, skip=None, dry=False):
    """核心函数：从 Azure DevOps 读取 → 建 Dify 知识库。"""
    if not AZURE_PAT:
        return {"error": "未配置 AZURE_DEVOPS_PAT"}
    if not DIFY_KEY:
        return {"error": "未配置 DIFY_API_KEY"}

    print("=" * 60)
    print("  从 Azure DevOps 构建 Dify 知识库")
    print("  仓库路径: %s" % repo_path)
    print("  知识库名: %s" % kb_name)
    print("=" * 60)

    # 1. 列出文件
    print("\n[1/3] 扫描仓库文件...")
    files = list_files(repo_path, branch)
    if skip:
        files = [f for f in files if skip not in f]

    print("  找到 %d 个 .md 文件" % len(files))
    if dry:
        for f in files:
            print("  [DRY] %s" % f)
        return {"status": "dry", "files": files, "count": len(files)}

    if not files:
        return {"error": "未找到 .md 文件"}

    # 2. 创建/获取知识库
    print("\n[2/3] 准备知识库...")
    if not dataset_id:
        kb_full_name = "%s_%s" % (kb_name, date.today().strftime("%Y%m%d"))
        dataset_id, actual_name = create_kb(kb_full_name)
        if not dataset_id:
            return {"error": "创建知识库失败"}
        print("  新建知识库: %s (id=%s)" % (actual_name, dataset_id))
    else:
        print("  使用已有知识库: %s" % dataset_id)

    # 3. 上传文件
    print("\n[3/3] 上传文档...")
    ok = 0
    for i, f in enumerate(files, 1):
        content = read_file(f, branch)
        if content is None:
            continue

        # 文档名用相对路径
        doc_name = f.replace(repo_path.rstrip("/") + "/", "").replace("/", "__")
        parts = split_text(content)

        for pi, part in enumerate(parts, 1):
            pnm = doc_name if len(parts) == 1 else "%s_p%d" % (doc_name, pi)
            st, res = upload_doc(dataset_id, pnm, part)
            if st == 200 and isinstance(res, dict):
                doc = res.get("document", {})
                tag = " -> doc %s" % doc.get("id") if len(parts) == 1 else " [part %d/%d]" % (pi, len(parts))
                print("  [%d/%d] OK  %s%s" % (i, len(files), pnm, tag))
                ok += 1
            else:
                print("  [%d/%d] ERR %s  %s %s" % (i, len(files), pnm, st, str(res)[:100]))

    result = {
        "status": "done",
        "dataset_id": dataset_id,
        "kb_name": kb_name,
        "files_total": len(files),
        "docs_uploaded": ok,
        "repo_path": repo_path,
        "branch": branch,
    }

    print("\n===== 完成 =====")
    print("知识库: %s (id=%s)" % (kb_name, dataset_id))
    print("文档数: %d (来自 %d 个 .md 文件)" % (ok, len(files)))
    return result


# ─── 命令行入口 ────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        return

    repo_path = args[0]
    kb_name = args[1]
    branch = "vnext-b"
    dataset_id = None
    skip = None
    dry = False

    for a in args[2:]:
        if a == "--dry":
            dry = True
        elif a.startswith("--branch="):
            branch = a.split("=", 1)[1]
        elif a.startswith("--dataset-id="):
            dataset_id = a.split("=", 1)[1]
        elif a.startswith("--skip="):
            skip = a.split("=", 1)[1]

    result = build_kb(repo_path, kb_name, branch=branch, dataset_id=dataset_id, skip=skip, dry=dry)
    if result.get("error"):
        print("错误: %s" % result["error"])


if __name__ == "__main__":
    main()
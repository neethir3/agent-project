# -*- coding: utf-8 -*-
"""从 Azure DevOps 下载 PR diff，可选择上传到 Dify 需求变更知识库。

用法:
  python download_pr_diff.py <PR链接> <PAT_token> [选项]

选项:
  --out=DIR           输出目录（默认 ./PR{id}）
  --token-file=FILE   从文件读取 PAT token
  --upload            上传到 Dify 知识库
  --dataset-id=ID     Dify 知识库 ID（上传时必填）
  --dify-key=KEY      Dify API Key（上传时必填，或设环境变量 DIFY_KEY）
  --dify-base=URL     Dify 服务地址（默认 https://dify-test.uat.autobestdevops.com）
  --dry               仅预览，不实际上传

PR 链接格式:
  https://autobest.visualstudio.com/AutoBestChina/_git/PrdReq/pullrequest/29921
  https://dev.azure.com/autobest/AutoBestChina/_git/PrdReq/pullrequest/29921

示例:
  python download_pr_diff.py "https://..." "PAT" --out=./PR29921
  python download_pr_diff.py "https://..." "PAT" --upload --dataset-id=xxx --dify-key=xxx
"""
import sys, os, re, json, base64, urllib.request, urllib.error, urllib.parse, difflib
from datetime import date

# ─── Azure DevOps ───────────────────────────────────────────────────────────

def parse_pr_url(url):
    """解析 PR URL，返回 (org, project, repo, pr_id)。"""
    m = re.match(
        r"https?://(?:dev\.azure\.com/([^/]+)|([^.]+)\.visualstudio\.com)"
        r"/([^/]+)/_git/([^/]+)/pullrequest/(\d+)",
        url,
    )
    if not m:
        raise ValueError("无法解析 PR URL: %s" % url)
    org = m.group(1) or m.group(2)
    project = m.group(3)
    repo = m.group(4)
    pr_id = m.group(5)
    return org, project, repo, pr_id


def azure_api_req(base_url, token, path, method="GET", body=None):
    """调用 Azure DevOps REST API，返回 (status, data)。"""
    headers = {"Authorization": "Basic " + token}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base_url + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)


def get_pr_info(base_url, token, repo_id, pr_id):
    path = "/_apis/git/repositories/%s/pullRequests/%s?api-version=7.0" % (repo_id, pr_id)
    st, data = azure_api_req(base_url, token, path)
    return data if (st == 200 and isinstance(data, dict)) else None


def get_repo_id(base_url, token, project, repo_name):
    path = "/%s/_apis/git/repositories/%s?api-version=7.0" % (project, repo_name)
    st, data = azure_api_req(base_url, token, path)
    return data.get("id") if (st == 200 and isinstance(data, dict)) else None


def get_commit_changes(base_url, token, repo_id, commit_id):
    path = "/_apis/git/repositories/%s/commits/%s/changes?api-version=7.0&top=500" % (
        repo_id, commit_id,
    )
    st, data = azure_api_req(base_url, token, path)
    if st != 200 or not isinstance(data, dict):
        return {}
    result = {}
    for c in data.get("changes", []):
        item = c.get("item", {})
        if item.get("gitObjectType") == "blob":
            result[item.get("path", "")] = {
                "objectId": item.get("objectId"),
                "originalObjectId": item.get("originalObjectId"),
                "changeType": c.get("changeType"),
            }
    return result


def get_file_content(base_url, token, repo_id, file_path, commit_id):
    """获取指定 commit 中某个文件的原始内容。"""
    encoded_path = urllib.parse.quote(file_path, safe="")
    url = base_url + "/_apis/git/repositories/%s/items?path=%s&versionDescriptor.version=%s&versionDescriptor.versionType=commit&api-version=7.0" % (
        repo_id, encoded_path, commit_id,
    )
    headers = {"Authorization": "Basic " + token}
    r = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            if raw.startswith("{") and "message" in raw[:200]:
                print("  获取文件内容失败 [%s]: %s" % (file_path, raw[:200]))
                return None
            return raw
    except urllib.error.HTTPError as e:
        print("  获取文件内容失败 [%s]: HTTP %s" % (file_path, e.code))
        return None
    except Exception as e:
        print("  获取文件内容失败 [%s]: %s" % (file_path, repr(e)))
        return None


def generate_diff(old_content, new_content, file_path):
    """生成 unified diff。"""
    old = (old_content or "").splitlines(keepends=True)
    new = (new_content or "").splitlines(keepends=True)
    if old and not old[-1].endswith("\n"):
        old[-1] += "\n"
    if new and not new[-1].endswith("\n"):
        new[-1] += "\n"
    return "".join(difflib.unified_diff(old, new, fromfile="a" + file_path, tofile="b" + file_path))


# ─── Dify ────────────────────────────────────────────────────────────────────

def dify_req(base, key, path, method="GET", body=None):
    """调用 Dify API。"""
    headers = {"Authorization": "Bearer " + key}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)


def dify_upload_text(base, key, dataset_id, name, text):
    """上传文本到 Dify 知识库，返回 (status, response_dict)。"""
    body = {
        "name": name,
        "text": text,
        "indexing_technique": "high_quality",
        "process_rule": {"mode": "automatic"},
    }
    return dify_req(base, key, "/v1/datasets/%s/document/create-by-text" % dataset_id, "POST", body)


def format_diff_for_dify(pr_id, pr_title, pr_date, source_branch, target_branch, file_path, diff_text):
    """将 diff 格式化为 Dify 友好的 Markdown。"""
    # 用 # 标题分段，与 Dify 分段标识符 \n# 对齐
    lines = [
        "# PR #%s: %s" % (pr_id, pr_title),
        "日期: %s | %s → %s" % (pr_date, source_branch, target_branch),
        "",
        "# %s" % file_path,
        "",
        "```diff",
        diff_text.strip(),
        "```",
    ]
    return "\n".join(lines)


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print(__doc__)
        return

    pr_url = args[0]
    token_arg = args[1]

    out_dir = None
    do_upload = False
    dataset_id = None
    dify_key = None
    dify_base = "https://dify-test.uat.autobestdevops.com"
    dry = False

    for a in args[2:]:
        if a.startswith("--out="):
            out_dir = a.split("=", 1)[1]
        elif a.startswith("--token-file="):
            with open(a.split("=", 1)[1], "r", encoding="utf-8") as f:
                token_arg = f.read().strip()
        elif a == "--upload":
            do_upload = True
        elif a == "--dry":
            dry = True
        elif a.startswith("--dataset-id="):
            dataset_id = a.split("=", 1)[1]
        elif a.startswith("--dify-key="):
            dify_key = a.split("=", 1)[1]
        elif a.startswith("--dify-base="):
            dify_base = a.split("=", 1)[1]

    if do_upload:
        if not dify_key:
            dify_key = os.environ.get("DIFY_KEY", "")
        if not dataset_id or not dify_key:
            print("上传需要 --dataset-id 和 --dify-key（或环境变量 DIFY_KEY）")
            return

    # 解析 PR URL
    org, project, repo_name, pr_id = parse_pr_url(pr_url)
    base_url = "https://dev.azure.com/%s" % org

    # 处理 token: PAT 原文 → base64(:TOKEN)
    if ":" not in token_arg:
        token = base64.b64encode((":" + token_arg).encode("ascii")).decode("ascii")
    else:
        token = base64.b64encode(token_arg.encode("ascii")).decode("ascii")

    print("PR #%s: %s/%s/%s" % (pr_id, org, project, repo_name))

    # 获取 repo id
    repo_id = get_repo_id(base_url, token, project, repo_name)
    if not repo_id:
        print("无法获取仓库 ID")
        return
    print("Repo ID: %s" % repo_id)

    # 获取 PR 信息
    pr_info = get_pr_info(base_url, token, repo_id, pr_id)
    if not pr_info:
        print("获取 PR 信息失败")
        return
    title = pr_info.get("title", "")
    source_commit = pr_info.get("lastMergeSourceCommit", {}).get("commitId", "")
    target_commit = pr_info.get("lastMergeTargetCommit", {}).get("commitId", "")
    merge_commit = pr_info.get("lastMergeCommit", {}).get("commitId", "")
    status = pr_info.get("status", "")
    source_branch = pr_info.get("sourceRefName", "").replace("refs/heads/", "")
    target_branch = pr_info.get("targetRefName", "").replace("refs/heads/", "")
    # 合并日期
    pr_date = pr_info.get("closedDate", "")[:10] or date.today().strftime("%Y-%m-%d")

    print("标题: %s" % title)
    print("状态: %s" % status)
    print("分支: %s → %s" % (source_branch, target_branch))
    print("日期: %s" % pr_date)

    if not source_commit or not target_commit:
        print("无法获取 commit 信息（PR 可能未合并）")
        return

    # 获取变更文件列表
    changes = get_commit_changes(base_url, token, repo_id, source_commit)
    blobs = {p: v for p, v in changes.items() if v["changeType"] != "delete"}
    if not blobs:
        print("没有文件变更")
        return

    print("\n变更文件 (%d 个):" % len(blobs))
    for p, v in sorted(blobs.items()):
        print("  [%s] %s" % (v["changeType"], p))

    # 输出目录
    prefix = "PR%s" % pr_id
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    else:
        out_dir = "./%s" % prefix

    dify_docs = []  # 收集要上传的文档
    ok = 0

    for file_path, info in sorted(blobs.items()):
        ct = info["changeType"]
        safe_name = file_path.strip("/").replace("/", "__").replace(" ", "_")
        print("\n处理: %s [%s]" % (file_path, ct))

        if ct == "add":
            old_content = ""
            new_content = get_file_content(base_url, token, repo_id, file_path, source_commit)
        elif ct == "edit":
            old_content = get_file_content(base_url, token, repo_id, file_path, target_commit)
            new_content = get_file_content(base_url, token, repo_id, file_path, source_commit)
        else:
            continue

        if old_content is None and new_content is None:
            print("  跳过（无法获取内容）")
            continue

        diff_text = generate_diff(old_content, new_content, file_path)

        # 保存本地 .diff 文件
        diff_file = "%s_%s.diff" % (prefix, safe_name)
        diff_path = os.path.join(out_dir, diff_file)
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(diff_text)
        print("  -> %s (%d 行)" % (diff_file, diff_text.count("\n")))

        # 生成 Dify 文档
        if do_upload:
            dify_content = format_diff_for_dify(
                pr_id, title, pr_date, source_branch, target_branch, file_path, diff_text
            )
            doc_name = "%s__%s" % (prefix, file_path.strip("/"))
            dify_docs.append((doc_name, dify_content, file_path))
        ok += 1

    # 写汇总
    summary_path = os.path.join(out_dir, "%s_summary.md" % prefix)
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("# PR #%s: %s\n\n" % (pr_id, title))
        f.write("- 状态: %s\n" % status)
        f.write("- 分支: %s → %s\n" % (source_branch, target_branch))
        f.write("- 日期: %s\n" % pr_date)
        f.write("\n## 变更文件 (%d)\n\n" % ok)
        for p, v in sorted(blobs.items()):
            f.write("- **[%s]** %s\n" % (v["changeType"], p))

    print("\n===== 下载完成 =====")
    print("输出: %s  (%d 个文件)" % (out_dir, ok))

    # ── 上传到 Dify ──
    if do_upload and dify_docs:
        print("\n===== 上传 Dify =====")
        print("知识库: %s" % dataset_id)
        if dry:
            print("[DRY] 仅预览，不上传：")
            for doc_name, content, fp in dify_docs:
                print("  [DRY] %s  (%d 字符)" % (doc_name, len(content)))
            return

        up_ok = 0
        for i, (doc_name, content, fp) in enumerate(dify_docs, 1):
            print("[%d/%d] %s" % (i, len(dify_docs), doc_name))
            st, res = dify_upload_text(dify_base, dify_key, dataset_id, doc_name, content)
            if st == 200 and isinstance(res, dict):
                doc = res.get("document", {})
                print("  OK -> doc %s" % doc.get("id"))
                up_ok += 1
            else:
                print("  ERR %s %s" % (st, str(res)[:200]))
        print("\n上传完成: %d/%d" % (up_ok, len(dify_docs)))


if __name__ == "__main__":
    main()
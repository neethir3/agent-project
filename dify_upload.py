# -*- coding: utf-8 -*-
"""把 .md 文件批量喂进 Dify 知识库（永远新建库，不删旧库，安全第一）。
用法:
  python dify_upload.py <api_key> "<库名>" <路径> [--skip=子串] [--limit=N] [--suffix=后缀] [--dry]
  路径 = 单个 .md 文件 或 目录（递归扫 *.md）
  --suffix: 库名后缀，用于版本管理（如 --suffix=v2 → 库名变为 "xxx_v2"；默认用日期）
  目录模式下文档名用相对路径（分隔符替换为 __）保证唯一，避免 Dify 按名去重
  导致跨站点同名文件只保留一份。
  超大文本按空行边界切分成多个文档（create-by-text 有长度上限）。
示例:
  python dify_upload.py dataset-xxx "全页面需求" "D:/PrdReq" --skip=NoUse
  python dify_upload.py dataset-xxx "全页面需求" "D:/PrdReq" --suffix=v2
"""
import sys, os, json, re, urllib.request, urllib.error
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://dify-test.uat.autobestdevops.com"
KEY = None
MAX_CHARS = 50000  # create-by-text 单文档文本上限；超出按空行切分


def req(path, method="GET", body=None):
    headers = {"Authorization": "Bearer " + KEY}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, repr(e)


def list_datasets():
    st, res = req("/v1/datasets?page=1&limit=100")
    if isinstance(res, list):
        return res
    if isinstance(res, dict):
        return res.get("data") or []
    print("list err", st, str(res)[:200])
    return []


def create_dataset(name):
    """创建知识库，返回 (id, actual_name)。actual_name 可能与 name 不同（自动加后缀避免冲突）。
    创建时自动配置：分段规则 + 混合检索 + bge-reranker-v2-m3 重排序。"""
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
        st, r = req("/v1/datasets", "POST", b)
        if isinstance(r, dict) and r.get("id"):
            did = r["id"]
            _patch_dataset(did, b)
            return did, b["name"], st, r
        return None, None, st, r

    did, actual_name, st, res = _try_create(body)
    if did:
        return did, actual_name

    # 如果是名字冲突，尝试加 _v2, _v3 ...
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
                print("create err (retry %s) %s %s" % (new_name, st2, str(res2)[:300]))
                return None, None
        print("create err: tried _v2.._v99, all taken")
        return None, None
    print("create err", st, str(res)[:300])
    return None, None


def _patch_dataset(did, body):
    """事后补刀：确保分段/检索设置生效。分段设置是知识库级的，走 PATCH 打上。"""
    patch = {
        "indexing_technique": "high_quality",
        "process_rule": {
            "mode": "custom",
            "rules": {
                "pre_processing_rules": [
                    {"id": "remove_extra_spaces", "enabled": False},
                    {"id": "remove_urls_emails", "enabled": False},
                ],
                "segmentation": {
                    "separator": "\n#",
                    "max_tokens": 1000,
                    "chunk_overlap": 50,
                },
            },
        },
        "retrieval_model": body.get("retrieval_model", {}),
    }
    st, _ = req("/v1/datasets/%s" % did, "PATCH", patch)
    if st == 200:
        print("  (分段+检索设置已同步)")
    else:
        print("  (设置同步返回 %s，可能需要 UI 手动确认)" % st)


def upload_text(did, name, text):
    body = {
        "name": name,
        "text": text,
        "indexing_technique": "high_quality",
        "process_rule": {"mode": "automatic"},
    }
    st, res = req("/v1/datasets/%s/document/create-by-text" % did, "POST", body)
    return st, res


def gather(path, skip):
    files = []
    if os.path.isfile(path):
        files = [path]
    else:
        for root, _, fs in os.walk(path):
            for f in fs:
                if f.lower().endswith(".md"):
                    p = os.path.join(root, f)
                    if skip and skip in p:
                        continue
                    files.append(p)
    return sorted(files)


def split_text(text):
    """超大文本按标题边界切分成 <= MAX_CHARS 的片段，与 Dify 分段标识符 \\n# 对齐，
    保持 Markdown 章节 / 测试用例块的完整性。
    单个块仍超长时回退到按空行切分。"""
    if len(text) <= MAX_CHARS:
        return [text]

    # 按 Dify 分段标识符 \\n# 切分（匹配 # ~ ###### 各级标题）
    parts = re.split(r"(?=\n#{1,6}(?:\s|$))", text)

    chunks, buf = [], ""
    for blk in parts:
        if not blk.strip():
            continue
        if buf and len(buf) + len(blk) > MAX_CHARS:
            if buf.strip():
                chunks.append(buf)
            buf = blk
            # 单个块仍超长 → 回退到按空行切分
            while len(buf) > MAX_CHARS:
                sub = _split_by_paragraphs(buf)
                chunks.append(sub[0])
                buf = "\n\n".join(sub[1:]) if len(sub) > 1 else ""
        else:
            buf = (buf + blk) if buf else blk

    if buf and buf.strip():
        chunks.append(buf)
    return chunks if chunks else [text]


def _split_by_paragraphs(text):
    """回退方案：按空行边界切分。"""
    if len(text) <= MAX_CHARS:
        return [text]
    chunks, buf = [], ""
    for blk in text.split("\n\n"):
        if buf and len(buf) + len(blk) + 2 > MAX_CHARS:
            chunks.append(buf)
            buf = blk
        else:
            buf = (buf + "\n\n" + blk) if buf else blk
    if buf:
        chunks.append(buf)
    return chunks


def main():
    global KEY
    args = sys.argv[1:]
    if len(args) < 3:
        print(__doc__)
        return
    KEY = args[0]
    base_name = args[1]
    path = args[2]
    skip = None
    limit = None
    dry = False
    suffix = None
    for a in args[3:]:
        if a == "--dry":
            dry = True
        elif a.startswith("--skip="):
            skip = a.split("=", 1)[1]
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
        elif a.startswith("--suffix="):
            suffix = a.split("=", 1)[1]

    # 库名：base_name + suffix（默认用日期）
    if suffix:
        dataset_name = "%s_%s" % (base_name, suffix)
    else:
        dataset_name = "%s_%s" % (base_name, date.today().strftime("%Y%m%d"))

    files = gather(path, skip)
    msg = "%d .md under %s" % (len(files), path) + ((" (skip '%s')" % skip) if skip else "")
    print(msg)
    if limit:
        files = files[:limit]
        print("limit -> %d" % len(files))
    if dry:
        print("[DRY] 目标库名: %s" % dataset_name)
        for f in files:
            print("  DRY", f)
        return

    # 永远新建库
    print("创建知识库: %s ..." % dataset_name)
    did, actual_name = create_dataset(dataset_name)
    if not did:
        print("创建知识库失败，终止")
        return
    print("知识库 '%s' id=%s (新建)" % (actual_name, did))

    is_dir = os.path.isdir(path)
    ok = 0
    for i, f in enumerate(files, 1):
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        # 唯一文档名：目录模式用相对路径(分隔符->__)，单文件用 basename
        try:
            nm = os.path.relpath(f, path).replace("\\", "/").replace("/", "__") if is_dir else os.path.basename(f)
        except Exception:
            nm = os.path.basename(f)
        parts = split_text(text)
        for pi, part in enumerate(parts, 1):
            pnm = nm if len(parts) == 1 else "%s_p%d" % (nm, pi)
            st, res = upload_text(did, pnm, part)
            if st == 200 and isinstance(res, dict):
                doc = res.get("document", {})
                tag = "  -> doc %s" % doc.get("id") if len(parts) == 1 else "  [part %d/%d] -> doc %s" % (pi, len(parts), doc.get("id"))
                print("[%d/%d] OK  %s%s" % (i, len(files), pnm, tag))
                ok += 1
            else:
                print("[%d/%d] ERR %s  %s %s" % (i, len(files), pnm, st, str(res)[:200]))
    print("\n===== 上传完成 =====")
    print("知识库: %s  (id=%s)" % (actual_name, did))
    print("文档数: %d  (来自 %d 个 .md 文件)" % (ok, len(files)))
    print("⚠️  旧库未动，请人工确认新库内容无误后，再手动删除旧库。")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""feishu-doc-studio —— 本地 Web 工具:用模型优化/改写 Markdown,再一键写入飞书文档。

- 多「飞书 Bot」:每个 = 一组 app_id/app_secret + 身份(tenant/user),可写进不同租户。
- 模型:复用 cc switch 的 provider(读 ~/.cc-switch/cc-switch.db),页面上可下拉切换,
  选哪个模型就自动用那个 provider 的端点与密钥。
- 复用 feishu_md2doc.py 的 Markdown→飞书 block 逻辑(parse/write_md/api,均按 tok 参数化)。

零第三方依赖,只用标准库。仅监听 127.0.0.1(本机个人工具)。
"""
import os, re, sys, json, time, threading, traceback, importlib.util, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
MD2DOC_PATH = os.path.expanduser("~/feishu_md2doc.py")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
FEISHU_DOC_BASE = os.environ.get("FEISHU_DOC_BASE", "https://feishu.cn/docx/")
ADDR, PORT = "127.0.0.1", 8801

# ---- 载入 md2doc 模块(复用其 markdown→block 逻辑) ----------------------------
def load_md2doc():
    # 优先用仓库内自带的副本(自包含),没有再回退到 ~/feishu_md2doc.py
    local = os.path.join(HERE, "feishu_md2doc.py")
    path = local if os.path.exists(local) else MD2DOC_PATH
    spec = importlib.util.spec_from_file_location("feishu_md2doc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

MD = load_md2doc()

# ---- 配置存取 ----------------------------------------------------------------
_cfg_lock = threading.Lock()

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return {"bots": []}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg):
    with _cfg_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)

def public_config(cfg):
    """给前端的视图:抹掉密钥明文,只暴露 hasSecret 标记;附带当前生效的模型与候选列表。"""
    def bot(b):
        return {"id": b["id"], "name": b.get("name", ""), "appId": b.get("appId", ""),
                "identity": b.get("identity", "tenant"),
                "userTokenFile": b.get("userTokenFile", ""),
                "hasSecret": bool(b.get("appSecret"))}
    m = active_model()
    cands = []
    env = claude_env()
    seen_cur = set()
    for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
              "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
        v = env.get(k) or os.environ.get(k, "")
        if v and v not in seen_cur:
            seen_cur.add(v)
            cands.append({"model": v, "provider": ""})  # provider 空 = 跟随当前
    # 与「当前」端点相同的 provider 不再重复列(上面「跟随当前」已覆盖)
    cur_base = (env.get("ANTHROPIC_BASE_URL") or os.environ.get("ANTHROPIC_BASE_URL", "")).rstrip("/")
    for p in cc_providers():
        if p["baseUrl"].rstrip("/") == cur_base:
            continue
        for mm in p["models"]:
            cands.append({"model": mm, "provider": p["name"]})
    return {"bots": [bot(b) for b in cfg.get("bots", [])],
            "model": {"model": m["model"], "baseUrl": m["baseUrl"],
                      "ready": bool(m["token"]), "candidates": cands}}

# ---- 当前模型:直接复用 cc switch 写进 ~/.claude/settings.json 的 provider ------
def claude_env():
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            return json.load(f).get("env", {})
    except Exception:
        return {}

CC_DB = os.path.expanduser("~/.cc-switch/cc-switch.db")

def cc_providers():
    """读 cc switch 数据库里全部 Claude provider(端点 + 凭据 + 模型清单),只读。
    页面上的模型下拉就是从这里来的:选哪个模型,就用那个 provider 的端点和密钥。
    按最近一次成功调用时间排序,健康的 provider 排前面。"""
    import sqlite3
    try:
        db = sqlite3.connect("file:" + CC_DB + "?mode=ro", uri=True)
        rows = db.execute("SELECT id, name, settings_config FROM providers "
                          "WHERE app_type='claude' ORDER BY sort_index").fetchall()
        health = {pid: (ls or "") for pid, ls in db.execute(
            "SELECT provider_id, last_success_at FROM provider_health WHERE app_type='claude'")}
        db.close()
    except Exception:
        return []
    out = []
    for pid, name, cfg in rows:
        try:
            env = (json.loads(cfg) or {}).get("env", {})
        except Exception:
            continue
        base = env.get("ANTHROPIC_BASE_URL", "")
        tok = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY", "")
        if not base or not tok:
            continue
        models = []
        for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                  "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL"):
            v = env.get(k)
            if v and v not in models:
                models.append(v)
        if models:
            out.append({"name": name, "baseUrl": base, "token": tok,
                        "authKind": "bearer" if env.get("ANTHROPIC_AUTH_TOKEN") else "apikey",
                        "models": models, "lastOk": health.get(pid, "")})
    out.sort(key=lambda p: p["lastOk"], reverse=True)
    return out

def active_model(model_override=None, provider_name=None):
    """默认跟随 cc switch 当前 provider(settings.json);
    页面上选了具体模型(+provider)时,用那个 provider 的端点和密钥。"""
    override = (model_override or "").strip()
    if override:
        for p in cc_providers():
            if override in p["models"] and (not provider_name or p["name"] == provider_name):
                return {"baseUrl": p["baseUrl"], "token": p["token"],
                        "model": override, "authKind": p["authKind"]}
        for p in cc_providers():  # 指定 provider 没命中时退回任意拥有该模型的
            if override in p["models"]:
                return {"baseUrl": p["baseUrl"], "token": p["token"],
                        "model": override, "authKind": p["authKind"]}
    env = claude_env()
    def pick(k):
        return env.get(k) or os.environ.get(k, "")
    token = pick("ANTHROPIC_AUTH_TOKEN") or pick("ANTHROPIC_API_KEY")
    return {
        "baseUrl": pick("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
        "token": token,
        "model": override or pick("ANTHROPIC_MODEL") or "claude-opus-4-8",
        "authKind": "bearer" if pick("ANTHROPIC_AUTH_TOKEN") else "apikey",
    }

# ---- 飞书 token(按 bot 凭据) -----------------------------------------------
def tenant_token(app_id, app_secret):
    r = MD.api("POST", "/auth/v3/tenant_access_token/internal", "",
               {"app_id": app_id, "app_secret": app_secret})
    if r.get("code") != 0:
        raise RuntimeError("拿 tenant token 失败: " + json.dumps(r, ensure_ascii=False))
    return r["tenant_access_token"]

def user_token(bot):
    path = os.path.expanduser(bot.get("userTokenFile") or "~/.feishu_user_token.json")
    if not os.path.exists(path):
        raise RuntimeError(f"该 bot 用「用户身份」,但缺 token 文件 {path}。先对这个 app 跑 feishu_oauth.py 授权。")
    d = json.load(open(path))
    if int(time.time()) - d.get("_obtained_at", 0) > d.get("expires_in", 7200) - 120:
        r = MD.api("POST", "/authen/v2/oauth/token", "",
                   {"grant_type": "refresh_token", "client_id": bot["appId"],
                    "client_secret": bot["appSecret"], "refresh_token": d["refresh_token"]})
        if r.get("access_token"):
            r["_obtained_at"] = int(time.time())
            json.dump(r, open(path, "w"))
            d = r
        else:
            raise RuntimeError("刷新用户 token 失败,请重新授权: " + json.dumps(r, ensure_ascii=False))
    return d["access_token"]

def bot_token(bot):
    if bot.get("identity") == "user":
        return user_token(bot)
    return tenant_token(bot["appId"], bot["appSecret"])

# ---- 调用模型(复用 cc switch 的端点;Anthropic 优先,404 时回退 OpenAI 兼容) ----
def clean_model(name):
    """cc switch 用「模型名[1M]」标注上下文窗口等变体,API 不认这个后缀,发请求前去掉。"""
    return re.sub(r"\s*\[[^\]]*\]\s*$", "", (name or "")).strip()

def model_url(base, kind):
    """拼请求路径:有的 provider base 自带 /v1(如 callapi8.com/v1),不能再叠一层。"""
    b = (base or "").rstrip("/")
    suffix = "/messages" if kind == "anthropic" else "/chat/completions"
    if b.endswith("/v1"):
        return b + suffix
    return b + ("/v1" if kind == "anthropic" else "/v1") + suffix

def _post_json(url, headers, body_dict):
    # 带上正常浏览器 UA:部分中转端点挂在 Cloudflare 后面,会拦 Python 默认 UA(1010)
    headers.setdefault("User-Agent",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    req = urllib.request.Request(url, data=json.dumps(body_dict).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)

def openai_chat(m, system, messages, max_tokens=16000):
    """OpenAI 兼容格式(/v1/chat/completions):部分中转端点只有这个协议。"""
    url = model_url(m["baseUrl"], "openai")
    headers = {"Content-Type": "application/json"}
    if m["token"]:
        headers["Authorization"] = "Bearer " + m["token"]
    r = _post_json(url, headers, {"model": clean_model(m["model"]), "max_tokens": max_tokens,
                                  "messages": [{"role": "system", "content": system}] + messages})
    return r["choices"][0]["message"]["content"]

def anthropic_chat(m, system, messages, max_tokens=16000):
    """通用多轮调用。messages = [{"role":"user"/"assistant","content":str}, ...]
    端点不支持 /v1/messages(404/405)时自动改走 OpenAI 兼容协议。"""
    if not m["token"]:
        raise RuntimeError("没读到模型凭据。请确认 cc switch 已切好 provider(会写入 ~/.claude/settings.json)。")
    url = model_url(m["baseUrl"], "anthropic")
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if m["authKind"] == "bearer":
        headers["Authorization"] = "Bearer " + m["token"]
    else:
        headers["x-api-key"] = m["token"]
    body = {"model": clean_model(m["model"]), "max_tokens": max_tokens, "system": system, "messages": messages}
    try:
        r = _post_json(url, headers, body)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        if e.code in (404, 405):  # 该端点可能是纯 OpenAI 兼容格式
            try:
                return openai_chat(m, system, messages, max_tokens)
            except Exception as e2:
                raise RuntimeError(f"模型 {m['model']} 两种协议都失败。Anthropic: {e.code} {detail};OpenAI 兼容: {e2}")
        raise RuntimeError(f"模型 {m['model']} 返回 {e.code}: {detail}")
    except Exception as e:
        raise RuntimeError(f"连不上模型 {url}: {e}")
    return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")

def anthropic_messages(m, system, user, max_tokens=16000):
    return anthropic_chat(m, system, [{"role": "user", "content": user}], max_tokens)

def model_stream(m, system, messages, max_tokens=16000):
    """流式调用,yield ("thinking"|"text", chunk)。
    连接级失败在还没吐出任何内容时自动重试一次;端点不支持流式/Anthropic 协议时兜底。"""
    if not m["token"]:
        raise RuntimeError("没读到模型凭据。请确认 cc switch 已切好 provider。")
    url = model_url(m["baseUrl"], "anthropic")
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    if m["authKind"] == "bearer":
        headers["Authorization"] = "Bearer " + m["token"]
    else:
        headers["x-api-key"] = m["token"]
    body = {"model": clean_model(m["model"]), "max_tokens": max_tokens,
            "system": system, "messages": messages, "stream": True}
    yielded = False
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
            resp = urllib.request.urlopen(req, timeout=600)
            ctype = resp.headers.get("Content-Type", "")
            if "text/event-stream" not in ctype:
                # 端点忽略了 stream 参数,按普通 JSON 响应处理
                r = json.load(resp)
                txt = "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")
                if not txt:  # 可能是 OpenAI 格式
                    txt = (r.get("choices") or [{}])[0].get("message", {}).get("content", "")
                yield ("text", txt)
                return
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    ev = json.loads(data)
                except Exception:
                    continue
                t = ev.get("type")
                if t == "content_block_delta":
                    d = ev.get("delta", {}) or {}
                    if d.get("type") == "text_delta" and d.get("text"):
                        yield ("text", d["text"]); yielded = True
                    elif d.get("type") == "thinking_delta" and d.get("thinking"):
                        yield ("thinking", d["thinking"]); yielded = True
                elif t == "error":
                    raise RuntimeError("模型返回错误: " + json.dumps(ev, ensure_ascii=False)[:300])
            return
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (404, 405):  # 纯 OpenAI 兼容端点:退回非流式
                yield ("text", openai_chat(m, system, messages, max_tokens))
                return
            raise RuntimeError(f"模型 {m['model']} 返回 {e.code}: {detail}")
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if yielded or attempt == 1:
                raise RuntimeError(f"模型连接中断({m['model']}): {e}")
            time.sleep(1)

OPTIMIZE_SYSTEM = (
    "你是资深中文技术文档编辑。请优化用户给的 Markdown:改善结构层次、措辞与可读性,"
    "补全过渡,修正明显笔误,保持技术事实不变。输出必须是可直接写入飞书的 Markdown,"
    "只用这些语法:# ## ### 标题、段落、**加粗**、`行内代码`、- 无序列表、1. 有序列表、"
    "> 引用、表格、--- 分割线、``` 代码块。只输出优化后的 Markdown 正文,不要任何解释或额外说明,"
    "不要用 ```markdown 包裹整篇。")

def strip_fence(text):
    """模型有时把整篇包在 ```markdown ... ``` 里,去掉最外层。"""
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        if lines[0].startswith("```") and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return t

def guard_against_envelope(markdown):
    """最后一道防线:别把模型没解析开的原始 JSON 信封写进飞书。"""
    head = (markdown or "").strip()[:2000]
    if head.startswith("{") and '"doc_markdown"' in head:
        raise RuntimeError("待写入内容像是模型的原始 JSON 信封(没解析开),已拦住。"
                           "请重发一次需求重新生成改稿。")

# ---- 发布:创建/定位文档 → 写入 --------------------------------------------
def resolve_link(target, tok):
    """把用户粘的链接/ID 解析成 docx document_id(wiki 链接先解析)。"""
    import re
    target = (target or "").strip()
    if "/wiki/" in target:
        return MD.resolve_wiki(target, tok)
    m = re.search(r"/(?:docx|docs)/([A-Za-z0-9]+)", target)
    return m.group(1) if m else target

def preflight(doc, tok):
    chk = MD.api("GET", f"/docx/v1/documents/{doc}", tok)
    if chk.get("code") != 0:
        raise RuntimeError(
            f"打不开目标文档(doc_id={doc}):{chk.get('msg')}。"
            "常见原因:① 这其实是 wiki/知识库链接,请粘 wiki 链接让工具自动解析;"
            "② 用应用身份写别人的文档,需先把文档加该 bot 为可编辑协作者。")
    return (chk.get("data") or {}).get("document", {}).get("title", "")

def publish(bot, dest, markdown, replace):
    guard_against_envelope(markdown)
    tok = bot_token(bot)
    mode = dest.get("mode", "new")
    if mode == "new":
        r = MD.api("POST", "/docx/v1/documents", tok, {"title": dest.get("title") or "未命名文档"})
        if r.get("code") != 0:
            raise RuntimeError("新建文档失败: " + json.dumps(r, ensure_ascii=False))
        doc = r["data"]["document"]["document_id"]
    else:  # doc / wiki:粘什么都自动解析
        doc = resolve_link(dest.get("target"), tok)
        preflight(doc, tok)
    if replace:
        MD.clear_doc(doc, tok)
    ns, nt, nq = MD.write_md(doc, tok, markdown)
    return {"url": FEISHU_DOC_BASE + doc, "docId": doc,
            "stats": {"blocks": ns, "tables": nt, "quotes": nq}}

# ---- 读文档:飞书 blocks → Markdown(反向还原,保住结构再交给模型改) -----------
HEAD_LVL = {3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6, 9: 7, 10: 8, 11: 9}
CONTENT_KEY = {2: "text", 12: "bullet", 13: "ordered", 14: "code",
               3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
               7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8", 11: "heading9"}
# 飞书代码块语言编号 → 围栏语言名(与 feishu_md2doc.CODE_LANG 对应)
CODE_LANG_REV = {34: "python", 20: "javascript", 44: "typescript", 21: "json", 3: "bash",
                 15: "go", 19: "java", 42: "sql", 48: "yaml", 17: "html", 7: "css",
                 41: "scss", 28: "markdown", 47: "xml", 4: "c", 6: "cpp", 36: "ruby",
                 37: "rust", 31: "php", 23: "kotlin", 43: "swift", 39: "scala", 35: "r",
                 26: "lua", 32: "powershell", 5: "csharp"}

def _elems_md(block):
    key = CONTENT_KEY.get(block.get("block_type"))
    if not key:
        return ""
    out = []
    for e in (block.get(key) or {}).get("elements", []):
        tr = e.get("text_run")
        if not tr:
            continue
        t = tr.get("content", "")
        st = tr.get("text_element_style", {}) or {}
        if st.get("inline_code"):
            t = f"`{t}`"
        elif st.get("bold"):
            t = f"**{t}**"
        out.append(t)
    return "".join(out)

def doc_to_markdown(doc, tok):
    r = MD.api("GET", f"/docx/v1/documents/{doc}/blocks?page_size=500", tok)
    if r.get("code") != 0:
        raise RuntimeError("读取文档失败: " + json.dumps(r, ensure_ascii=False))
    items = (r.get("data") or {}).get("items", [])
    bmap = {b["block_id"]: b for b in items}
    page = next((b for b in items if b.get("block_type") == 1), None)
    order = page.get("children", []) if page else \
        [b["block_id"] for b in items if b.get("block_type") != 1]
    lines = []
    for bid in order:
        b = bmap.get(bid)
        if not b:
            continue
        bt = b.get("block_type")
        if bt in HEAD_LVL:
            lines += ["#" * HEAD_LVL[bt] + " " + _elems_md(b), ""]
        elif bt == 2:
            lines += [_elems_md(b), ""]
        elif bt == 12:
            lines.append("- " + _elems_md(b))
        elif bt == 13:
            lines.append("1. " + _elems_md(b))
        elif bt == 22:
            lines += ["---", ""]
        elif bt == 14:  # 代码块 → ``` 围栏(带语言标签)
            lang_id = ((b.get("code") or {}).get("style") or {}).get("language")
            content = _elems_md(b)
            lines += ["```" + CODE_LANG_REV.get(lang_id, ""), content.rstrip("\n"), "```", ""]
        elif bt == 19:  # callout → 引用
            for cid in b.get("children", []):
                c = bmap.get(cid)
                if c:
                    lines.append("> " + _elems_md(c))
            lines.append("")
        elif bt == 31:  # table
            prop = (b.get("table") or {}).get("property", {})
            cols = prop.get("column_size", 0)
            cells = b.get("children", [])
            if cols:
                grid = [cells[i:i + cols] for i in range(0, len(cells), cols)]
                for ri, row in enumerate(grid):
                    txt = []
                    for cid in row:
                        cell = bmap.get(cid, {})
                        parts = [_elems_md(bmap[t]) for t in cell.get("children", []) if t in bmap]
                        txt.append(" ".join(p for p in parts if p))
                    lines.append("| " + " | ".join(txt) + " |")
                    if ri == 0:
                        lines.append("| " + " | ".join(["---"] * cols) + " |")
                lines.append("")
    # 收尾:去掉多余空行
    md, blank = [], False
    for ln in lines:
        if ln == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        md.append(ln)
    return "\n".join(md).strip()

# ---- 对话改稿:模型读全文 + 用户需求 → 结构化结果(改动 / 需澄清) ---------------
EDIT_SYSTEM = (
    "你是飞书文档编辑助手。用户给你一篇文档的当前 Markdown 全文和一个修改需求,你要读懂全文再按需求改。\n"
    "必须只输出一个 JSON 对象(不要用 ``` 包裹,不要任何额外文字),字段:\n"
    '- "reply": 字符串,给用户看的简短中文说明(你做了什么,或你的疑问)。\n'
    '- "clarify": 需要用户确认时填 {"question": "问题", "options": ["选项A", "选项B"]},否则为 null。\n'
    "  当需求有歧义、有多种合理改法、或可能删除/大改重要内容时,务必先 clarify,不要擅自动手。\n"
    '- "doc_markdown": 修改后的【完整】Markdown 全文(不是片段);仅当这轮只是提问或确实无改动时才允许为 null。\n'
    '重要:只要做了任何改动,就必须在 doc_markdown 里给出完整改后文档;严禁只在 reply 里说改了而不输出 doc_markdown。\n'
    "只用飞书支持的语法:# ## ### #### 标题、段落、**加粗**、`行内代码`、- 无序列表、1. 有序列表、"
    "> 引用、表格、--- 分割线、``` 代码块(保留语言标签)。\n"
    "处理代码块务必保持 ``` 围栏成对完整,除需求涉及的改动外代码内容逐字保留,不要把代码拆成普通段落。"
    "代码块围栏必须用三个反引号 ```(第一行 ```语言名),严禁用单个或两个反引号当围栏。"
    "保持与原文一致的技术事实,不要杜撰。")

def _unescape_json_string(s):
    try:
        return json.loads('"' + s + '"')
    except Exception:
        pass
    out = s
    for a, b in (("\\n", "\n"), ("\\t", "\t"), ('\\"', '"'), ("\\\\", "\\")):
        out = out.replace(a, b)
    return re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), out)

def parse_envelope(text):
    import re
    t = strip_fence(text).strip()
    if t.startswith("{"):
        try:
            return json.loads(t)
        except Exception:
            pass
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        # JSON 信封被输出上限截断:抢救 doc_markdown 字段里的正文
        m2 = re.search(r'"doc_markdown"\s*:\s*"(.*?)"\s*[,}\n]', t, re.S) \
            or re.search(r'"doc_markdown"\s*:\s*"(.*)\Z', t, re.S)
        if m2:
            mr = re.search(r'"reply"\s*:\s*"([^"]*)"', t)
            return {"reply": (mr.group(1) if mr else "") +
                    "(模型输出到一半被长度截断,已抢救出改稿,结尾可能不全)",
                    "clarify": None, "doc_markdown": _unescape_json_string(m2.group(1))}
        return {"reply": "模型返回的 JSON 无法解析:" + t[:200], "clarify": None, "doc_markdown": None}
    # 兜底:模型没按 JSON 输出,直接吐了整篇文档(长文本)→ 当文档正文用
    if len(t) > 300:
        return {"reply": "(模型直接输出了文档正文,已自动识别)", "clarify": None, "doc_markdown": t}
    return {"reply": t, "clarify": None, "doc_markdown": None}

CLAIM_WORDS = ("添加", "修改", "删除", "调整", "优化", "重写", "补充", "替换", "加入",
               "改为", "改成", "加上", "增加", "更新", "移至", "拆分", "合并", "精简",
               "扩写", "改写", "插入", "移除", "移到", "放在")

def claims_change(reply):
    r = reply or ""
    return any(w in r for w in CLAIM_WORDS)

def _collect_edit(model, msgs, on_chunk, max_tokens):
    full = ""
    for kind, chunk in model_stream(model, EDIT_SYSTEM, msgs, max_tokens=max_tokens):
        if kind == "text":
            full += chunk
        on_chunk(kind, chunk)
    return full

def edit_with_retry(model, msgs, on_chunk, on_info):
    """跑一轮;若模型声称改了却没给 doc_markdown,自动追问一轮要全文。
    改稿要输出全文,默认 32k 上限;端点嫌大报 max_tokens 错时退回 16k。"""
    try:
        full = _collect_edit(model, msgs, on_chunk, 32000)
    except RuntimeError as e:
        if "max_tokens" not in str(e):
            raise
        full = _collect_edit(model, msgs, on_chunk, 16000)
    env = parse_envelope(full)
    if env.get("doc_markdown") is None and not env.get("clarify") and claims_change(env.get("reply", "")):
        on_info("模型声称改了但没给改后全文,自动追问一次…")
        retry_msgs = msgs + [
            {"role": "assistant", "content": full},
            {"role": "user", "content": "你上一轮的回复缺少改后的完整文档。请重新输出 JSON,"
                                        "务必在 doc_markdown 字段给出修改后的完整 Markdown 全文。"}]
        try:
            full2 = _collect_edit(model, retry_msgs, on_chunk, 32000)
        except RuntimeError as e:
            if "max_tokens" not in str(e):
                raise
            full2 = _collect_edit(model, retry_msgs, on_chunk, 16000)
        env2 = parse_envelope(full2)
        if env2.get("doc_markdown"):
            env = env2
        elif env2.get("reply"):
            env["reply"] = (env.get("reply") or "") + "\n" + env2["reply"]
    return env

def build_edit_messages(doc_markdown, history, message):
    msgs = []
    convo = f"【当前文档全文 Markdown】\n{doc_markdown or '(空文档,还没有内容)'}\n"
    msgs.append({"role": "user", "content": convo + "\n请等待我的修改需求。"})
    msgs.append({"role": "assistant", "content": '{"reply":"已读完全文,请说需求。","clarify":null,"doc_markdown":null}'})
    for h in (history or []):
        role = "assistant" if h.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": h.get("content", "")})
    msgs.append({"role": "user", "content": message})
    return msgs

def doc_chat(doc_markdown, history, message, model_name=None, provider_name=None):
    model = active_model(model_name, provider_name)
    msgs = build_edit_messages(doc_markdown, history, message)
    return edit_with_retry(model, msgs, on_chunk=lambda k, c: None, on_info=lambda t: None)

# ---- HTTP ------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

    def _sse(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n")
        self.wfile.flush()

    def log_message(self, *a):
        pass  # 静默

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            p = os.path.join(HERE, "index.html")
            return self._send(200, open(p, "rb").read(), "text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._send(200, public_config(load_config()))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            if self.path == "/api/config":
                incoming = self._read_json()
                cfg = load_config()
                # 合并密钥:bot appSecret 前端留空则沿用旧值
                old_bots = {b["id"]: b for b in cfg.get("bots", [])}
                for b in incoming.get("bots", []):
                    if not b.get("appSecret"):
                        b["appSecret"] = old_bots.get(b["id"], {}).get("appSecret", "")
                merged = {"bots": incoming.get("bots", [])}
                save_config(merged)
                return self._send(200, public_config(merged))

            if self.path == "/api/optimize":
                d = self._read_json()
                model = active_model(d.get("model"), d.get("provider"))
                instruction = (d.get("instruction") or "").strip()
                user_msg = (("额外要求:" + instruction + "\n\n") if instruction else "") + \
                           "下面是待优化的 Markdown:\n\n" + d["markdown"]
                out = anthropic_messages(model, OPTIMIZE_SYSTEM, user_msg)
                return self._send(200, {"markdown": strip_fence(out)})

            if self.path == "/api/publish":
                d = self._read_json()
                cfg = load_config()
                bot = next((b for b in cfg["bots"] if b["id"] == d["botId"]), None)
                if not bot:
                    return self._send(400, {"error": "bot 不存在"})
                res = publish(bot, d["dest"], d["markdown"], bool(d.get("replace")))
                return self._send(200, res)

            if self.path == "/api/doc/load":
                d = self._read_json()
                cfg = load_config()
                bot = next((b for b in cfg["bots"] if b["id"] == d["botId"]), None)
                if not bot:
                    return self._send(400, {"error": "bot 不存在"})
                tok = bot_token(bot)
                doc = resolve_link(d.get("link"), tok)
                title = preflight(doc, tok)
                md = doc_to_markdown(doc, tok)
                return self._send(200, {"docId": doc, "url": FEISHU_DOC_BASE + doc,
                                        "title": title, "markdown": md, "empty": not md.strip()})

            if self.path == "/api/doc/chat":
                d = self._read_json()
                env = doc_chat(d.get("markdown", ""), d.get("history", []), d["message"], d.get("model"), d.get("provider"))
                return self._send(200, env)

            if self.path == "/api/doc/chat/stream":
                d = self._read_json()
                model = active_model(d.get("model"), d.get("provider"))
                msgs = build_edit_messages(d.get("markdown", ""), d.get("history", []), d["message"])
                self._sse_start()
                try:
                    env = edit_with_retry(model, msgs,
                                          on_chunk=lambda k, c: self._sse({"kind": k, "text": c}),
                                          on_info=lambda t: self._sse({"kind": "info", "text": t}))
                    self._sse({"kind": "done", "env": env})
                except Exception as e:
                    traceback.print_exc()
                    self._sse({"kind": "error", "error": str(e)})
                return

            if self.path == "/api/optimize/stream":
                d = self._read_json()
                model = active_model(d.get("model"), d.get("provider"))
                instruction = (d.get("instruction") or "").strip()
                user_msg = (("额外要求:" + instruction + "\n\n") if instruction else "") + \
                           "下面是待优化的 Markdown:\n\n" + d["markdown"]
                self._sse_start()
                full = ""
                try:
                    for kind, chunk in model_stream(model, OPTIMIZE_SYSTEM,
                                                    [{"role": "user", "content": user_msg}]):
                        if kind == "text":
                            full += chunk
                        self._sse({"kind": kind, "text": chunk})
                    self._sse({"kind": "done", "markdown": strip_fence(full)})
                except Exception as e:
                    traceback.print_exc()
                    self._sse({"kind": "error", "error": str(e)})
                return

            if self.path == "/api/doc/apply":
                d = self._read_json()
                cfg = load_config()
                bot = next((b for b in cfg["bots"] if b["id"] == d["botId"]), None)
                if not bot:
                    return self._send(400, {"error": "bot 不存在"})
                guard_against_envelope(d["markdown"])
                tok = bot_token(bot)
                doc = d["docId"]
                MD.clear_doc(doc, tok)
                ns, nt, nq = MD.write_md(doc, tok, d["markdown"])
                return self._send(200, {"url": FEISHU_DOC_BASE + doc, "docId": doc,
                                        "stats": {"blocks": ns, "tables": nt, "quotes": nq}})

            return self._send(404, {"error": "not found"})
        except SystemExit as e:  # md2doc 内部用 sys.exit 报错
            traceback.print_exc()
            return self._send(500, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})


def main():
    if not os.path.exists(CONFIG_PATH):
        save_config({"bots": []})
    srv = ThreadingHTTPServer((ADDR, PORT), Handler)
    print(f"feishu-doc-studio 运行中 → http://{ADDR}:{PORT}")
    print(f"配置文件: {CONFIG_PATH}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()

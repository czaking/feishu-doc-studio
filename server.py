#!/usr/bin/env python3
"""feishu-doc-studio —— 本地 Web 工具:用本地模型优化 Markdown,再一键写入飞书文档。

- 多「飞书 Bot」:每个 = 一组 app_id/app_secret + 身份(tenant/user),可写进不同租户。
- 多「本地模型」:任意 OpenAI 兼容 endpoint(Ollama / vLLM / LM Studio …)。
- 复用 ~/feishu_md2doc.py 的 Markdown→飞书 block 逻辑(parse/write_md/api,均按 tok 参数化)。

零第三方依赖,只用标准库。仅监听 127.0.0.1(本机个人工具)。
"""
import os, sys, json, time, threading, traceback, importlib.util, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
MD2DOC_PATH = os.path.expanduser("~/feishu_md2doc.py")
CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
FEISHU_DOC_BASE = "https://presence.feishu.cn/docx/"
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
    """给前端的视图:抹掉密钥明文,只暴露 hasSecret 标记;附带当前生效的模型(只读)。"""
    def bot(b):
        return {"id": b["id"], "name": b.get("name", ""), "appId": b.get("appId", ""),
                "identity": b.get("identity", "tenant"),
                "userTokenFile": b.get("userTokenFile", ""),
                "hasSecret": bool(b.get("appSecret"))}
    m = active_model()
    return {"bots": [bot(b) for b in cfg.get("bots", [])],
            "model": {"model": m["model"], "baseUrl": m["baseUrl"],
                      "ready": bool(m["token"])}}

# ---- 当前模型:直接复用 cc switch 写进 ~/.claude/settings.json 的 provider ------
def active_model():
    """读 cc switch 当前切到的 Anthropic 端点(settings.json 的 env 块,回退到进程环境变量)。
    用户在 cc switch 里切哪个,这里就用哪个,无需在本工具重复配置。"""
    env = {}
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as f:
            env = json.load(f).get("env", {})
    except Exception:
        pass
    def pick(k):
        return env.get(k) or os.environ.get(k, "")
    token = pick("ANTHROPIC_AUTH_TOKEN") or pick("ANTHROPIC_API_KEY")
    return {
        "baseUrl": pick("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
        "token": token,
        "model": pick("ANTHROPIC_MODEL") or "claude-opus-4-8",
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

# ---- 调用模型(Anthropic Messages,复用 cc switch 的端点) -------------------
def anthropic_chat(m, system, messages, max_tokens=16000):
    """通用多轮调用。messages = [{"role":"user"/"assistant","content":str}, ...]"""
    if not m["token"]:
        raise RuntimeError("没读到模型凭据。请确认 cc switch 已切好 provider(会写入 ~/.claude/settings.json)。")
    url = m["baseUrl"].rstrip("/") + "/v1/messages"
    body = json.dumps({"model": m["model"], "max_tokens": max_tokens,
                       "system": system, "messages": messages}).encode()
    headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if m["authKind"] == "bearer":
        headers["Authorization"] = "Bearer " + m["token"]
    else:
        headers["x-api-key"] = m["token"]
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            r = json.load(resp)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"模型返回 {e.code}: {e.read().decode()[:400]}")
    except Exception as e:
        raise RuntimeError(f"连不上模型 {url}: {e}")
    return "".join(b.get("text", "") for b in r.get("content", []) if b.get("type") == "text")

def anthropic_messages(m, system, user, max_tokens=16000):
    return anthropic_chat(m, system, [{"role": "user", "content": user}], max_tokens)

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
CONTENT_KEY = {2: "text", 12: "bullet", 13: "ordered",
               3: "heading1", 4: "heading2", 5: "heading3", 6: "heading4",
               7: "heading5", 8: "heading6", 9: "heading7", 10: "heading8", 11: "heading9"}

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
    '- "doc_markdown": 修改后的【完整】Markdown 全文(不是片段);若这轮只是提问或无改动则为 null。\n'
    "只用飞书支持的语法:# ## ### #### 标题、段落、**加粗**、`行内代码`、- 无序列表、1. 有序列表、"
    "> 引用、表格、--- 分割线。保持与原文一致的技术事实,不要杜撰。")

def parse_envelope(text):
    import re
    t = strip_fence(text).strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
    return {"reply": t, "clarify": None, "doc_markdown": None}

def doc_chat(doc_markdown, history, message):
    model = active_model()
    msgs = []
    convo = f"【当前文档全文 Markdown】\n{doc_markdown or '(空文档,还没有内容)'}\n"
    msgs.append({"role": "user", "content": convo + "\n请等待我的修改需求。"})
    msgs.append({"role": "assistant", "content": '{"reply":"已读完全文,请说需求。","clarify":null,"doc_markdown":null}'})
    for h in (history or []):
        role = "assistant" if h.get("role") == "assistant" else "user"
        msgs.append({"role": role, "content": h.get("content", "")})
    msgs.append({"role": "user", "content": message})
    # active_model 走 Anthropic:把 system + 多轮 messages 一起发
    out = anthropic_chat(model, EDIT_SYSTEM, msgs)
    return parse_envelope(out)

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
                model = active_model()
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
                env = doc_chat(d.get("markdown", ""), d.get("history", []), d["message"])
                return self._send(200, env)

            if self.path == "/api/doc/apply":
                d = self._read_json()
                cfg = load_config()
                bot = next((b for b in cfg["bots"] if b["id"] == d["botId"]), None)
                if not bot:
                    return self._send(400, {"error": "bot 不存在"})
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

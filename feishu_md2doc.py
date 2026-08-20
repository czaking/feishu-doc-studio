#!/usr/bin/env python3
"""feishu_md2doc.py — 把 Markdown 写入飞书 docx / wiki 文档（复用工具）
用法:
  python3 feishu_md2doc.py --file X.md --doc  <document_id>
  python3 feishu_md2doc.py --file X.md --wiki <wiki节点token或完整URL>
  python3 feishu_md2doc.py --file X.md --new  "标题"     # 应用身份新建(自己拥有,用于测试)
读取 ~/.feishu.env 里的 FEISHU_APP_ID / FEISHU_APP_SECRET。
支持: # ## ### 标题 / 段落 / **加粗** / `行内代码` / - 无序 / 1. 有序 / > 引用(callout) / 表格 / --- 分割线 / 代码块
"""
import os,sys,json,re,time,urllib.request,urllib.error,argparse

BASE="https://open.feishu.cn/open-apis"

def load_env():
    p=os.path.expanduser("~/.feishu.env"); env={}
    for line in open(p):
        line=line.strip()
        if "=" in line and not line.startswith("#"):
            k,v=line.split("=",1); env[k]=v
    return env["FEISHU_APP_ID"],env["FEISHU_APP_SECRET"]

def api(method,path,tok,body=None):
    url=BASE+path
    data=json.dumps(body).encode() if body is not None else None
    req=urllib.request.Request(url,data=data,method=method,
        headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"})
    try:
        raw=urllib.request.urlopen(req).read().decode()
    except urllib.error.HTTPError as e:
        raw=e.read().decode()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 飞书网关的 404/5xx 有时返回 HTML/纯文本,别让 json 解析崩掉
            return {"code":e.code,"msg":f"HTTP {e.code}: {raw[:200]}"}
    return json.loads(raw)

def get_token():
    aid,sec=load_env()
    r=api("POST","/auth/v3/tenant_access_token/internal","",{"app_id":aid,"app_secret":sec})
    if r.get("code")!=0: sys.exit("拿 token 失败: "+json.dumps(r,ensure_ascii=False))
    return r["tenant_access_token"]

def get_user_token():
    p=os.path.expanduser("~/.feishu_user_token.json")
    if not os.path.exists(p): sys.exit("没有用户 token,先跑 feishu_oauth.py 授权")
    d=json.load(open(p))
    if int(time.time())-d.get("_obtained_at",0) > d.get("expires_in",7200)-120:
        # 过期则用 refresh_token 刷新
        aid,sec=load_env()
        r=api("POST","/authen/v2/oauth/token","",{"grant_type":"refresh_token",
            "client_id":aid,"client_secret":sec,"refresh_token":d["refresh_token"]})
        if r.get("access_token"):
            r["_obtained_at"]=int(time.time()); json.dump(r,open(p,"w")); d=r
        else:
            sys.exit("刷新 token 失败,请重新授权: "+json.dumps(r,ensure_ascii=False))
    return d["access_token"]

def resolve_wiki(node,tok):
    m=re.search(r"/wiki/([A-Za-z0-9]+)",node)
    if m: node=m.group(1)
    r=api("GET",f"/wiki/v2/spaces/get_node?token={node}&obj_type=wiki",tok)
    if r.get("code")!=0: sys.exit("解析 wiki 节点失败: "+json.dumps(r,ensure_ascii=False))
    n=r["data"]["node"]
    return n["obj_token"]

# ---------- inline 解析: **bold** 和 `code` ----------
def inline(text):
    els=[]; i=0
    for tok_ in re.split(r"(\*\*.+?\*\*|`.+?`)",text):
        if not tok_: continue
        if tok_.startswith("**") and tok_.endswith("**"):
            els.append({"text_run":{"content":tok_[2:-2],"text_element_style":{"bold":True}}})
        elif tok_.startswith("`") and tok_.endswith("`"):
            els.append({"text_run":{"content":tok_[1:-1],"text_element_style":{"inline_code":True}}})
        else:
            els.append({"text_run":{"content":tok_}})
    return els or [{"text_run":{"content":""}}]

HEAD={1:("heading1",3),2:("heading2",4),3:("heading3",5),4:("heading4",6)}

# ---------- markdown -> 操作序列 ----------
def parse(md):
    lines=md.split("\n"); ops=[]; i=0
    while i<len(lines):
        ln=lines[i]
        s=ln.strip()
        if not s:
            i+=1; continue
        # 分割线
        if re.fullmatch(r"-{3,}",s):
            ops.append(("divider",None)); i+=1; continue
        # 标题
        m=re.match(r"(#{1,4})\s+(.*)",s)
        if m:
            lvl=len(m.group(1)); ops.append(("head",(lvl,m.group(2)))); i+=1; continue
        # 引用(可多行)
        if s.startswith(">"):
            buf=[]
            while i<len(lines) and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip()[1:].strip()); i+=1
            ops.append(("quote"," ".join(buf))); continue
        # 表格
        if s.startswith("|") and i+1<len(lines) and re.match(r"^\s*\|?[\s:|-]+\|",lines[i+1]):
            rows=[]
            while i<len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip()); i+=1
            cells=[]
            for r_ in rows:
                cols=[c.strip() for c in r_.strip().strip("|").split("|")]
                cells.append(cols)
            # 去掉分隔行(第二行)
            if len(cells)>=2 and re.match(r"^[\s:-]+$",cells[1][0]):
                header=cells[0]; body=cells[2:]
                cells=[header]+body
            ops.append(("table",cells)); continue
        # 有序列表
        m=re.match(r"\d+\.\s+(.*)",s)
        if m:
            ops.append(("ordered",m.group(1))); i+=1; continue
        # 无序列表
        m=re.match(r"[-*]\s+(.*)",s)
        if m:
            ops.append(("bullet",m.group(1))); i+=1; continue
        # 普通段落
        ops.append(("text",s)); i+=1
    return ops

# ---------- 生成飞书 block ----------
def simple_block(op):
    kind,val=op
    if kind=="divider": return {"block_type":22,"divider":{}}
    if kind=="head":
        lvl,txt=val; field,bt=HEAD.get(lvl,HEAD[4])
        return {"block_type":bt,field:{"elements":inline(txt)}}
    if kind=="text": return {"block_type":2,"text":{"elements":inline(val)}}
    if kind=="bullet": return {"block_type":12,"bullet":{"elements":inline(val)}}
    if kind=="ordered": return {"block_type":13,"ordered":{"elements":inline(val)}}
    return None

def clear_doc(doc,tok):
    """清空文档所有顶层块(用于 --replace 重写)"""
    r=api("GET",f"/docx/v1/documents/{doc}/blocks?page_size=500",tok)
    items=(r.get("data") or {}).get("items",[])
    if not items: return
    kids=items[0].get("children",[])
    if not kids: return
    api("DELETE",f"/docx/v1/documents/{doc}/blocks/{doc}/children/batch_delete",tok,
        {"start_index":0,"end_index":len(kids)})
    time.sleep(0.3)

def append_children(doc,tok,blocks):
    """批量把简单块追加到文末"""
    if not blocks: return
    for k in range(0,len(blocks),45):
        chunk=blocks[k:k+45]
        r=api("POST",f"/docx/v1/documents/{doc}/blocks/{doc}/children",tok,{"children":chunk})
        if r.get("code")!=0: sys.exit("追加块失败: "+json.dumps(r,ensure_ascii=False))
        time.sleep(0.2)

def append_quote(doc,tok,text):
    """引用 -> callout(带子文本)，走 descendant 一次建"""
    desc=[{"block_id":"co","block_type":19,"callout":{},"children":["t"]},
          {"block_id":"t","block_type":2,"text":{"elements":inline(text)}}]
    body={"children_id":["co"],"descendants":desc}
    r=api("POST",f"/docx/v1/documents/{doc}/blocks/{doc}/descendant",tok,body)
    if r.get("code")!=0:  # callout 不行就退回普通加粗文本
        append_children(doc,tok,[{"block_type":2,"text":{"elements":[{"text_run":{"content":text,"text_element_style":{"bold":True}}}]}}])
    time.sleep(0.2)

def append_table(doc,tok,cells):
    rows=len(cells); cols=max(len(r) for r in cells)
    desc=[]; cid=[]
    for r in range(rows):
        for c in range(cols):
            content=cells[r][c] if c<len(cells[r]) else ""
            cb=f"c_{r}_{c}"; tb=f"t_{r}_{c}"; cid.append(cb)
            desc.append({"block_id":cb,"block_type":32,"table_cell":{},"children":[tb]})
            desc.append({"block_id":tb,"block_type":2,"text":{"elements":inline(content)}})
    table={"block_id":"tbl","block_type":31,
           "table":{"property":{"row_size":rows,"column_size":cols,"header_row":True}},
           "children":cid}
    body={"children_id":["tbl"],"descendants":[table]+desc}
    r=api("POST",f"/docx/v1/documents/{doc}/blocks/{doc}/descendant",tok,body)
    if r.get("code")!=0: sys.exit("建表失败: "+json.dumps(r,ensure_ascii=False))
    time.sleep(0.3)

def write_md(doc,tok,md):
    ops=parse(md); batch=[]; n_simple=n_tbl=n_q=0
    for op in ops:
        if op[0]=="table":
            append_children(doc,tok,batch); n_simple+=len(batch); batch=[]
            append_table(doc,tok,op[1]); n_tbl+=1
        elif op[0]=="quote":
            append_children(doc,tok,batch); n_simple+=len(batch); batch=[]
            append_quote(doc,tok,op[1]); n_q+=1
        else:
            b=simple_block(op)
            if b: batch.append(b)
    append_children(doc,tok,batch); n_simple+=len(batch)
    return n_simple,n_tbl,n_q

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--file",required=True)
    ap.add_argument("--user",action="store_true",help="用用户身份(~/.feishu_user_token.json)")
    ap.add_argument("--replace",action="store_true",help="写入前清空文档现有内容")
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--doc"); g.add_argument("--wiki"); g.add_argument("--new")
    a=ap.parse_args()
    tok=get_user_token() if a.user else get_token()
    md=open(a.file,encoding="utf-8").read()
    if a.new:
        r=api("POST","/docx/v1/documents",tok,{"title":a.new})
        doc=r["data"]["document"]["document_id"]; print("新建文档:",doc)
    elif a.wiki:
        doc=resolve_wiki(a.wiki,tok); print("wiki -> docx:",doc)
    else:
        doc=a.doc
    if a.replace:
        clear_doc(doc,tok); print("已清空原内容")
    ns,nt,nq=write_md(doc,tok,md)
    print(f"✅ 写入完成: 普通块 {ns} / 表格 {nt} / 引用 {nq}")
    print("文档: https://feishu.cn/docx/"+doc)

if __name__=="__main__":
    main()

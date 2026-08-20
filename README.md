# 飞书文档工作台 (feishu-doc-studio)

**English** | [中文](#中文)

A zero-dependency local web tool that optimizes / conversationally rewrites Markdown with an LLM, then writes it straight into Feishu (Lark) docs — docx or wiki.

- **✍️ Create mode**: paste Markdown → optimize with a model → write into Feishu (new or existing doc).
- **💬 Chat-edit mode**: paste an existing Feishu doc link → load the full text → revise in natural language over multiple turns → get a confirmation popup when the model is unsure → preview, then write back.
- **Model follows [cc switch](https://github.com/farion1231/cc-switch)**: the optimization model is read directly from the currently active Anthropic provider in `~/.claude/settings.json` — no need to configure it here. Switch models via cc switch.

Pure Python standard library, binds to `127.0.0.1` only.

## Run

```bash
python3 server.py
# open http://127.0.0.1:8801
```

## Configuration

The first run creates `config.json` (**gitignored — it holds secrets, never commit it**). Use `config.example.json` as a template, or fill it in via the "⚙️ 配置 Bot" panel in the top-right of the page:

| Field | Meaning |
|---|---|
| `appId` / `appSecret` | Credentials of a Feishu custom app |
| `identity` | `user` = write as yourself (doc owned by you, needs OAuth first); `tenant` = write as the app (doc owned by the bot) |
| `userTokenFile` | Token file for user identity, defaults to `~/.feishu_user_token.json` |

The Feishu app needs document permissions enabled; user identity additionally requires a one-time OAuth flow to obtain `~/.feishu_user_token.json`.

## Dependency

`feishu_md2doc.py` handles the Markdown ↔ Feishu block conversion and ships with the repo.

## Notes

- Before writing, the target doc's accessibility is checked first; wiki links are auto-resolved to the underlying docx.
- Chat-edit writes back by **full overwrite** (clear, then rebuild from the new Markdown) — clean structure, but it re-lays-out the whole document.

---

## 中文

[English](#飞书文档工作台-feishu-doc-studio) | **中文**

一个零依赖的本地 Web 工具:用大模型优化 / 对话式改写 Markdown,再一键写入飞书文档(docx / wiki)。

- **✍️ 创作新文档**:粘 Markdown → 模型优化 → 写入飞书(新建 / 已有文档)。
- **💬 对话改稿**:粘一篇已有飞书文档链接 → 全文读进来 → 用自然语言多轮修改 → 拿不准时弹窗让你选 → 预览确认后写回。
- **模型跟随 [cc switch](https://github.com/farion1231/cc-switch)**:优化用的模型直接读 `~/.claude/settings.json` 里当前生效的 Anthropic provider,无需在本工具重复配置。切模型就用 cc switch 切。

纯 Python 标准库,只监听 `127.0.0.1`。

### 运行

```bash
python3 server.py
# 打开 http://127.0.0.1:8801
```

### 配置

首次运行会生成 `config.json`(**已被 gitignore,内含密钥,勿提交**)。可参照 `config.example.json`,或直接在页面右上「⚙️ 配置 Bot」里填:

| 字段 | 说明 |
|---|---|
| `appId` / `appSecret` | 飞书开放平台自建应用的凭据 |
| `identity` | `user`=以你本人身份写(文档归你,需先 OAuth 授权);`tenant`=以应用身份写(文档归 bot) |
| `userTokenFile` | 用户身份时的 token 文件,默认 `~/.feishu_user_token.json` |

飞书应用需开通「文档」相关权限;用户身份还需先跑一次 OAuth 授权拿到 `~/.feishu_user_token.json`。

### 依赖

`feishu_md2doc.py` 提供 Markdown ↔ 飞书 block 的转换,已随仓库自带。

### 说明

- 写入飞书前会先探测目标文档可访问性;wiki 链接会自动解析成底层 docx。
- 「对话改稿」的写回是**整篇覆盖重写**(清空后按新 Markdown 重建),结构干净,但会重排全文。

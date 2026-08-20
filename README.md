# 飞书文档工作台 (feishu-doc-studio)

| 中文说明 | English |
|---|---|
| [🇨🇳 中文文档](./README_zh-CN.md) | [🇬🇧 English](./README.md) |
| 一个零依赖的本地 Web 工具:用大模型优化 / 对话式改写 Markdown,再一键写入飞书文档(docx / wiki)。创作、改稿两种模式,模型跟随 cc switch。 | A zero-dependency local web tool that optimizes / conversationally rewrites Markdown with an LLM, then writes it straight into Feishu (Lark) docs — docx or wiki. The model follows cc switch. |

## Features

- **✍️ Create mode**: paste Markdown → optimize with a model → write into Feishu (new or existing doc).
- **💬 Chat-edit mode**: paste an existing Feishu doc link → load the full text → revise in natural language over multiple turns → get a confirmation popup when the model is unsure → preview, then write back.
- **Model follows [cc switch](https://github.com/farion1231/cc-switch)**: the optimization model is read directly from the currently active Anthropic provider in `~/.claude/settings.json` — no model config needed here; switch models via cc switch.

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

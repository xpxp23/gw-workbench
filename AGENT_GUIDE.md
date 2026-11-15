# 🤖 公文与材料智能工作台 (GW-Workbench) — 本地 Agent 操控指南 (`AGENT_GUIDE.md`)

> **给本地 AI Agent（Claude Code / Cline / Cursor / OpenCode / 自定义 Agent）的系统指令**：
> 当用户要求你“把这篇公文/通知填入工作台”、“查看工作台里有哪些待办”、“更新某项工作的进展”、“检索以前写过的某类材料”或“根据某项工作要求起草材料并存进工作台”时，请直接使用本目录下的 **`wb.py` 命令行工具** 或 **本机免密 HTTP JSON API (`http://127.0.0.1:8090`)**，或者直接读写 **`./workspace_files/` 物理文件夹**。

## 一、三大黄金法则（最快最稳的操作方式）

1. **本机免密特权**：所有来自 `127.0.0.1` 的 HTTP 请求或通过 `python3 wb.py` 执行的操作均**自动拥有最高主理人读写权限**，无需处理任何密码或 Cookie。
2. **本地物理文件夹 1:1 双向同步**：
   - 所有工作事项和材料都明文存放在 `./workspace_files/<分类目录>/` 下。
   - 每个工作事项都有一个独立文件夹（如 `workspace_files/01_防灾减灾/2026-11-15_关于开展冬春自然灾害风险形势会商的通知/`），里面自带 `00_事项要求与进展.md`。
   - **当你帮用户写好一份材料（如 `.docx` / `.md` / `.txt`）时，直接把文件保存到该事项的本地文件夹内，然后执行一次 `python3 wb.py sync`（或调用 `wb.py attach`），工作台网页端就会立即显示该材料并自动建立全文索引！**
3. **输出全为标准 JSON**：`wb.py` 的所有子命令均输出结构化 UTF-8 JSON，极易解析。

## 二、`wb.py` 命令行速查表（推荐首选）

### 1. 查看当前待办与进行中事项
```bash
# 列出所有进行中的事项（含本地文件夹绝对路径 folder_abs_path、待办细项 checklist、参考旧稿 reference_docs）
python3 wb.py list --status in_progress

# 列出全部事项
python3 wb.py list
```

### 2. 把上级来文或微信/电话通知填入工作台
```bash
# 方式 A：直接传入公文文件路径（支持 .ofd / .docx / .pdf / .wps / .doc / .txt / .md），自动提取正文与要素
python3 wb.py add --file "/path/to/上级来文.ofd" --progress "已收文，准备起草方案"

# 方式 B：把微信群或电话通知直接填入工作台（可让工作台自动解析，也可由你解析后显式指定各字段）
python3 wb.py add \
  --title "报送安全生产隐患排查周调度表" \
  --source "微信群" \
  --org "县安委办" \
  --priority "紧急" \
  --deadline "2026-11-20" \
  --category "02_隐患排查与安全生产" \
  --summary "11月20日下班前报送周调度简表及300字工作亮点" \
  --checklist "汇总各领域隐患排查数据填写周调度简表;撰写300字以内工作亮点小结" \
  --text "【微信群原文】..." \
  --no-ai
```

### 3. 实时更新工作进展、勾选细项或标记“已完成（归档）”
```bash
# 追加一条实时进展记录
python3 wb.py progress <事项ID> "已完成初稿撰写并呈分管领导审阅"

# 切换某条待办细项的完成勾选状态（如 chk_1）
python3 wb.py progress <事项ID> --check-id chk_1

# 将事项标记为已完成（归档）
python3 wb.py progress <事项ID> "材料已正式报送，办结归档" --status archived
```

### 4. 把写好的材料挂载到工作事项下
```bash
# 将你为用户起草的材料自动复制归档到该事项的本地专属文件夹，并建立全文索引
python3 wb.py attach <事项ID> "/path/to/我写的汇报材料.docx" --role draft
```

### 5. 搜索历史公文与以前写过的材料（支持仅标题、全文检索、AI 上下文检索）
```bash
# 按【标题 + 正文内容】全文搜索
python3 wb.py search "防灾减灾 救灾物资" --scope full

# 仅按【标题/文号】搜索
python3 wb.py search "鲁应急发" --scope title

# AI 上下文检索（一次性返回匹配事项及公文材料的前 2000 字正文切片，方便你参考旧稿写新材料！）
python3 wb.py search "找一下以前写过的冬春自然灾害会商或防灾减灾总结" --ai
```

### 6. 触发本地物理文件夹同步
如果你直接在 `./workspace_files/` 下的文件夹里新建、修改或移动了文档，执行以下命令即可刷新数据库与全文索引：
```bash
python3 wb.py sync
```

## 三、RESTful JSON API 速查表（默认端口 `8090`）

| 功能 | 方法与路径 | 请求体 / 参数示例 |
| :--- | :--- | :--- |
| 获取全量看板与资料库数据 | `GET /api/bootstrap` | 无（自动执行一次本地目录增量同步） |
| 创建工作事项 | `POST /api/tasks` | `{"title":"...", "raw_notice_text":"...", "category":"01_防灾减灾", "auto_ai":true}` |
| 修改事项 / 填进展 / 归档 | `PUT /api/tasks/<id>` | `{"append_progress":"已报送", "status":"archived", "toggle_check_id":"chk_1"}` |
| 常规搜索（标题 / 全文） | `GET /api/search?q=关键词&scope=full` | `scope` 可选 `title`（仅标题文号）或 `full`（标题+正文） |
| Agent 专用全文切片检索 | `POST /api/agent/context-search` | `{"query": "森林防火排查方案", "category": ""}` |
| 查看单份文件完整提取正文 | `GET /api/docs/<doc_id>/preview` | 返回 `document.extracted_text` 完整正文与 `abs_path` |
| 触发本地文件夹双向同步 | `POST /api/sync` | `{}` |
| 生成阶段工作总结初稿 | `POST /api/ai/summary` | `{"period_label": "2026年第四季度"}` |

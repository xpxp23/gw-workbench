# 🤖 公文与材料智能工作台 (GW-Workbench v1.2.0) — 本地 Agent 与 MCP 操控指南 (`AGENT_GUIDE.md`)

> **给本地 AI Agent（Claude Code / Cline / Roo Code / Cursor / OpenCode / 自定义 Agent）的系统指令**：
> 当用户要求你“把公文/通知填入工作台”、“查看工作台待办”、“更新工作进展”、“检索以前写过的材料”或“起草材料并排版成国标公文存进工作台”时，你可以直接使用 **原生 MCP Server (`python3 wb.py mcp`)**、**`wb.py` 命令行工具** 或 **本机免密 HTTP JSON API (`http://127.0.0.1:8090`)**。

## 〇、写入纪律（先读这一条，违反等于给用户添乱）

工作台里每个事项只有两块内容需要 Agent 认真写：

1. **核心要求（summary）**——一段话说清：谁要什么、交几件、报给谁、什么时间、以什么形式（盖章PDF/电子版）。
2. **待办要求细项（checklist）**——逐条可勾选的动作，一条一个动作（核对/起草/填报/审签/盖章/报送/电话确认），带时间节点或报送对象。

**不要再往事项文件夹里生成“办理要点”“说明书骨架”“职责原文摘录”“分办清单”这类中间产物**，也不要用 `wb_add_task`/`wb_write_draft` 把它们挂到“我写的材料”下。理由很实在：用户自己会写材料，这些派生文件只会把“我写的材料”栏塞满噪音，删起来还得翻文件路径。

- 只有用户明确说“要出这份表 / 要这份复函草稿 / 把 X 存进工作台”时，才写文件，且**一份就够**，不要 01/02/03 三件套。
- 办事过程写进 `progress`（进展流水），不要写成文件；结论性要求写进 `summary` 与 `checklist`。
- 真正的报出件（盖章 docx、填好的 xlsx）才是“我写的材料”。


## 一、挂载原生 MCP Server（推荐零配置方式）

在你的本地 Agent（如 Claude Desktop / Cline / Roo Code / Cursor）的 MCP 配置文件中加入：

```json
{
  "mcpServers": {
    "gw-workbench": {
      "command": "python3",
      "args": ["/你的绝对路径/gw-workbench/wb.py", "mcp"]
    }
  }
}
```
挂载后，Agent 将自动获得以下 8 个原生工具：
- `wb_list_tasks`：列出待办与进行中事项（含本地文件夹路径、Checklist、关联材料、参考旧稿）
- `wb_add_task`：将上级来文（`.ofd`/`.docx`/`.pdf`/`.wps`）或微信/电话通知自动拆解填入工作台
- `wb_update_progress`：追加实时进展、勾选 Checklist 细项或将事项标记为 `archived`（已完成归档）
- `wb_write_draft`：直接为事项写入材料草稿（`.md`/`.txt`），设置 `export_gb9704_docx: true` 可**同时自动排版生成符合 GB/T 9704—2012 国标格式的 `.docx` 正式公文**！
- `wb_format_gb9704_docx`：将库内任意已有文档一键转换为符合 GB/T 9704—2012 国标的 `.docx` 公文（支持 `report` 汇报模式与 `redhead` 红头文件模式，支持联动 `xkl-gw`）
- `wb_search`：支持 `title`（仅标题文号）、`full`（SQLite FTS5 全文检索）及 `ai_context`（两段式 AI 语义检索并返回正文切片）
- `wb_read_document`：读取单份公文或材料的完整纯文本正文与本机绝对路径
- `wb_sync_folders`：触发本地物理文件夹与数据库双向增量同步及周期性工作派生检查

## 二、`wb.py` 命令行速查表

### 1. 查看当前待办与进行中事项
```bash
python3 wb.py list --status in_progress
```

### 2. 把上级来文或微信/电话通知填入工作台
```bash
# 直接传入公文原件路径（支持 .ofd / .docx / .pdf / .wps / .doc / .txt / .md）
python3 wb.py add --file "/path/to/上级来文.ofd" --progress "已收文，准备起草方案"

# 传入微信或电话通知文本
python3 wb.py add --text "【工作群通知】请于11月20日前报送隐患排查表..." --source "微信群"
```

### 3. 直接为某项工作撰写材料并一键生成国标 GB/T 9704 `.docx` 正式公文
```bash
python3 wb.py draft <事项ID> \
  --filename "我县冬春防灾减灾总结汇报_初稿.md" \
  --content "# 关于我县冬春防灾减灾工作的汇报\n\n一、总体情况\n..." \
  --to-docx \
  --mode report
```

### 4. 将已有草稿转换为 GB/T 9704—2012 国标 `.docx` 公文
```bash
# 综合汇报/方案模式 (report) 或 正式红头文件模式 (redhead)
python3 wb.py format-gw <文档ID> --mode redhead --doc-number "鲁应急发〔2026〕45号" --org "县应急管理局"
```

### 5. 更新进展、勾选清单或办结归档
```bash
python3 wb.py progress <事项ID> "已完成初稿并导出国标docx，呈领导审阅" --check-id chk_1
python3 wb.py progress <事项ID> "已正式报送，办结归档" --status archived
```

### 6. 查看或立即触发周期性/常态化工作派生（周报/月报/值班）
```bash
python3 wb.py recurring
python3 wb.py recurring --spawn <规则ID>
```

### 7. 全文检索与 AI 语义上下文检索
```bash
python3 wb.py search "救灾物资" --scope full
python3 wb.py search "找一下以前写过的防灾减灾总结或森林防灭火汇报" --ai
```

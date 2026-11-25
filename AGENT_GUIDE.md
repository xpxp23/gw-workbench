# 🤖 公文与材料智能工作台 (GW-Workbench v3.1.0) — 本地 Agent 与 MCP 操控指南 (`AGENT_GUIDE.md`)

> **给本地 AI Agent（Claude Code / Cline / Roo Code / Cursor / OpenCode / 自定义 Agent）的系统指令**：
> 当用户要求你“把公文/通知填入工作台”、“存入会议记录/培训课件/下沉照片”、“查看或更新重要事项记录（如高密汛情基本情况、地震应急预案）”、“更新工作进展”或“起草材料并排版成国标公文存进工作台”时，你可以直接使用 **原生 MCP Server (`python3 wb.py mcp`)**、**`wb.py` 命令行工具** 或 **本机免密 HTTP JSON API (`http://127.0.0.1:8090`)**。

## 〇、写入纪律与四维定位（先读这一条）

### A. 硬规则：不要生成“笔记文件”（用户明确要求，违反等于给用户添乱）

工作台待办里每个事项只有两块内容需要 Agent 认真写：

1. **核心要求（summary）**——一段话说清：谁要什么、交几件、报给谁、什么时间、以什么形式（盖章PDF/电子版）。
2. **待办要求细项（checklist）**——逐条可勾选的动作，一条一个动作（核对/起草/填报/审签/盖章/报送/电话确认），带时间节点或报送对象。

**不要再往事项文件夹里生成“办理要点”“说明书骨架”“职责原文摘录”“分办清单”这类中间产物**，也不要用 `wb_add_task` / `wb_write_draft` 把它们挂到“我写的材料”下。理由很实在：用户自己会写材料，这些派生文件只会把“我写的材料”栏塞满噪音，删起来还得翻文件路径。

- 只有用户明确说“要出这份表 / 要这份复函草稿 / 把 X 存进工作台”时，才写文件，且**一份就够**，不要 01/02/03 三件套。
- 办事过程写进 `progress`（进展流水），不要写成文件；结论性要求写进 `summary` 与 `checklist`。
- 真正的报出件（盖章 docx、填好的 xlsx）才是“我写的材料”。
- 上述纪律同样适用于**资料存档**与**重要事项记录**：不要在归档包或事项记录文件夹里堆派生笔记，纪要/正文各一份即可。

### B. 四维定位：工作台共有四类核心数据资产，请按性质精准操作

1. **📋 工作台待办（Tasks）**——有明确时限和交账要求的流转事项。只需认真写好 **核心要求（summary）** 与 **待办要求细项（checklist）** 两块，办事过程写进 `progress`，真正的报出件才挂入“我写的材料”。
2. **📦 资料存档（Archives）**——按“标题/次”存入的多媒体活动归档包（如 `11.17会议记录`、`11.18xx事项下沉照片`），支持自定义分类（会议记录、培训课件、下沉督导照片等），一个包里可同时存入会议纪要文本、录音、多张照片、PPT 课件、Word/Excel。
3. **📌 重要事项记录（Important Records）**——常备常查、点进去直观看到内容的业务底数与应急预案库（如 `高密汛情基本情况`、`地震应急预案`）。**你在起草防汛、抗震、安全汇报材料前，应优先调用 `wb_get_record` 或 `wb_search` 查阅重要事项记录中的真实底数数据，严禁凭空编造全县水库、企业或物资数字！**
4. **🗂️ 公文与材料库（Documents）**——全库所有公文、汇报定稿与常用范文模板。

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
挂载后，Agent 将自动获得以下 **14 个原生工具**：
- **待办与公文工具**：
  - `wb_list_tasks`：列出待办与进行中事项
  - `wb_add_task`：将上级来文或微信/电话通知拆解填入工作台待办
  - `wb_update_progress`：追加实时进展、勾选 Checklist 细项或标记 `archived`
  - `wb_write_draft`：直接起草材料草稿，设置 `export_gb9704_docx: true` 同时生成符合 GB/T 9704—2012 国标格式的 `.docx` 正式公文
  - `wb_format_gb9704_docx`：将库内已有文档一键转换为符合 GB/T 9704—2012 国标的 `.docx` 公文（`report` 汇报模式 / `redhead` 红头文件模式）
- **📦 资料存档工具（新增）**：
  - `wb_list_archives`：按分类或关键词列出资料存档包（含包内录音、照片、PPT、文档清单与路径）
  - `wb_add_archive`：按标题一次存入资料存档包（如 `11.17会议记录`），支持同时写入纪要并传入多份录音/照片/PPT/文档路径
  - `wb_append_archive`：向已有的资料存档包追加照片、录音、课件或补充纪要
- **📌 重要事项记录工具（新增）**：
  - `wb_list_records`：列出重要事项记录目录与摘要
  - `wb_get_record`：按 ID 或标题（如 `高密汛情基本情况`、`地震应急预案`）读取完整正文与关联 Word/PDF/Excel 结构化表格数据
  - `wb_upsert_record`：新建或更新重要事项记录（支持自定义分类、写入直读正文、挂载 Word/PDF/Excel 原件）
- **全局检索与同步工具**：
  - `wb_search`：四维全库检索（同时召回待办事项、资料存档包、重要事项记录、公文材料）
  - `wb_read_document`：读取单份文件的完整纯文本与结构化表格/幻灯片预览数据
  - `wb_sync_folders`：触发本地物理文件夹与数据库四维双向增量同步

## 二、`wb.py` 命令行速查表

### 1. 资料存档（按标题一次次存入会议记录、培训课件、下沉照片）
```bash
# 按标题一次存入包含录音、照片、PPT课件与会议纪要的归档包
python3 wb.py archive add \
  --title "11.17全县防汛会商会议记录" \
  --category "01_会议记录" \
  --notes "会议议定：1. 11月20日前完成物资清点；2. 严格落实24小时值班。" \
  --files "/path/to/录音.m4a" "/path/to/培训课件.pptx" "/path/to/现场照片1.jpg"

# 向已有存档包追加补拍照片或文件
python3 wb.py archive append <存档包ID> --files "/path/to/补充照片2.jpg" --notes "补充下午现场核查情况"

# 查看或搜索资料存档
python3 wb.py archive list --query "11.17"
```

### 2. 重要事项记录（高密汛情基本情况、地震应急预案等常备直读库）
```bash
# 新建或更新重要事项记录（支持直接写正文 + 挂载 Word/PDF/Excel）
python3 wb.py record set \
  --title "高密汛情基本情况" \
  --category "01_汛情与防灾底数" \
  --content "# 高密汛情基本情况\n\n全县共有大中小型水库32座..." \
  --files "/path/to/防汛水库底数表.xlsx" \
  --pin

# 读取重要事项记录完整内容与表格底数
python3 wb.py record get "高密汛情基本情况"
python3 wb.py record list
```

### 3. 待办事项、国标公文排版与四维检索
```bash
python3 wb.py list --status in_progress
python3 wb.py add --file "/path/to/上级来文.ofd" --progress "已收文，准备起草方案"
python3 wb.py draft <事项ID> --filename "我县防灾减灾总结汇报_初稿.md" --content "# 汇报标题..." --to-docx --mode report
python3 wb.py search "高密汛情" --ai
```

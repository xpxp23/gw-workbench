# 🤖 公文与材料智能工作台 (GW-Workbench v3.3.5) — 本地 Agent 与 MCP 操控指南 (`AGENT_GUIDE.md`)

> **给本地 AI Agent（Claude Code / Cline / Roo Code / Cursor / OpenCode / 手机端 Agent）的系统指令**：
> 当用户要求你“把公文/通知填入工作台”、“记住或修改邮箱/电话/红头/发文对象”、“我在外面，把待办发我看看”、“存入会议记录/培训课件/下沉照片”、“查看或更新重要事项记录”、“更新工作进展”或“起草材料并排版成国标公文”时，优先使用 **原生 MCP Server (`python3 wb.py mcp`)** 或 **`wb.py` 命令行工具**（本机 HTTP API `http://127.0.0.1:8090` 需携带 `X-Agent-Token` 请求头）。

---

## 〇、常驻记忆与写入纪律（动手前先读这三条）

### 1. 先读透明常驻记忆，不再使用 Agent 隐式黑盒记忆
工作台内置【🧠 记忆与规则】（`agent_memories`），专门记录用户的**邮箱、联系电话、常用红头、发文对象、注意事项**，用户在界面随时可审可改。
- **首轮先查简报**：每个新会话首次处理公文或起草材料前，先调用一次 `wb_mem_brief(scene="...")`（CLI：`python3 wb.py mem brief --scene draft`）。
- **版本短路零负担**：记住返回的 `rev=N`，同会话后续调用传入 `since_rev=N`，若规则未变仅返回 `OK rev=N (unchanged)`（约 10 Tokens）。
- **实时修改**：用户明确说“把报送邮箱改成 xxx”或“以后森防通知用森防指红头”时，立即调用 `wb_mem_set(..., explicit_user_request=True)` 实时落库，UI 秒级同步可见；若仅是你自己归纳的习惯，传 `explicit_user_request=False` 进入待审队列。
- **优先级**：用户本轮指令 > `P1` 硬规则/名片 > `P2` 常规规则 > 默认行为。

### 2. 手机端导出待办：一律用 `wb_task_digest`，不要拉全库、不要生成文件
当用户在外面用手机让你发待办（全部、紧急、某几项或某分类）时：
- **严禁调用 `wb_list_tasks` 拉取数万 Token 全量 JSON**，也**不要生成文件**。
- 直接调用 `wb_task_digest`（CLI：`python3 wb.py digest`），工具返回的纯文本已按手机竖屏排版完成，**直接原样发给用户即可**；用户回复 `#ID` 追问时再调 `wb_task_digest(ids="12", template="detail")`。

### 3. 硬规则：不要生成“中间笔记文件”
- 每个待办只认真写好 **核心要求 (`summary`)** 与 **待办细项 (`checklist`)**，办事过程写进 **`progress`**。
- **严禁**往事项文件夹里生成“办理要点”“分办清单”“职责摘录”等中间笔记文件；只有用户明确要出草稿/报表时才写文件，且一份就够。
- 起草防汛、抗震、安全汇报前，优先查 `wb_get_record` / `wb_search` 获取真实底数，严禁编造数字。

---

## 一、挂载原生 MCP Server（共 19 个工具）

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

### 工具全景（v3.3.5）
- **🧠 透明常驻记忆与红头规则（新增 3 个，极低 Token）**：
  - `wb_mem_brief(scene, since_rev)`：拉取 L0 常驻简报（邮箱/电话/红头/发文对象/红线），支持版本号短路
  - `wb_mem_search(query, kind, top_k)`：按关键词检索红头模板、联络名片与专项口径
  - `wb_mem_set(kind, title, rule_text, mem_key, scope, priority, meta, explicit_user_request)`：实时新增/更新透明记忆，UI 即时同步
- **📱 外出手机端待办摘要导出（新增 1 个，省 90%+ Token）**：
  - `wb_task_digest(preset, ids, category, due, status, template, limit, max_chars)`：零文件直出手机端排版文本（`mobile` / `checklist` / `report` / `detail`）
- **📋 待办与公文（6 个）**：
  - `wb_list_tasks`、`wb_add_task`（手动拆解好字段时建议传 `auto_ai: false`）、`wb_update_progress`、`wb_attach_file`、`wb_write_draft`、`wb_format_gb9704_docx`
- **📦 资料存档（3 个）**：
  - `wb_list_archives`、`wb_add_archive`、`wb_append_archive`
- **📌 重要事项记录（3 个）**：
  - `wb_list_records`、`wb_get_record`、`wb_upsert_record`
- **🔍 全库检索与同步（3 个）**：
  - `wb_search`、`wb_read_document`、`wb_sync_folders`

---

## 二、`wb.py` 命令行速查表

### 1. 透明常驻业务记忆与红头规则
```bash
# 首轮速读常驻记忆（纯文本，约 180 Tokens）
python3 wb.py mem brief --scene draft
# 同会话复查（未变更仅返回 OK rev=15 (unchanged)）
python3 wb.py mem brief --since-rev 15

# 新增/实时更新科室邮箱与电话
python3 wb.py mem set --key "contact.office" --kind contact --priority 1 \
  --title "科室报送邮箱与电话" \
  --rule "报送邮箱 yjglk@gaomi.gov.cn，办公电话 0536-232xxxx，报市局后须致电确认。"

# 新增“一岗多红头”模板（自动联动 gw_formatter.py 排版）
python3 wb.py mem set --key "redhead.anwei" --kind redhead --scope draft --priority 1 \
  --title "安委办红头" \
  --rule "安全生产综合性通知用安委办红头，发各镇街区及安委会成员单位。" \
  --meta '{"display_header":"高密市安全生产委员会办公室文件","doc_num_prefix":"高安办发","default_recipients":"各镇街区，市安委会有关成员单位","default_cc":"市政府办公室","issuer_name":"高密市安委会办公室"}'

# 按关键词检索 / 停用 / 批准
python3 wb.py mem search "安委办 红头"
python3 wb.py mem toggle redhead.anwei --off
```

### 2. 外出手机端低成本导出待办摘要（零文件直出）
```bash
# 外出速览：只看逾期/紧急/3日内必盯事项（默认 mobile 模板）
python3 wb.py digest --preset urgent

# 全部在办极简清单（每条 1 行，Token 最低）
python3 wb.py digest --preset all --template checklist

# 按分类与期限导出领导汇报体
python3 wb.py digest --category "防灾减灾" --due 3d --template report

# 按 ID 下钻看单条详情（无需拉全库）
python3 wb.py digest --ids 12,15 --template detail
```

### 3. 待办、档案、重要事项与国标排版
```bash
python3 wb.py list --status in_progress
python3 wb.py add --file "/path/to/上级来文.ofd" --progress "已收文，准备起草方案"
python3 wb.py draft 12 --filename "总结汇报_初稿.md" --content $'# 汇报标题\n\n正文内容...' --to-docx --mode report
python3 wb.py archive add --title "11.17防汛会商记录" --category "01_会议记录" --notes "议定事项..." --files "/path/to/录音.m4a"
python3 wb.py record set --title "高密汛情基本情况" --category "01_汛情与防灾底数" --content $'# 高密汛情基本情况\n\n全县共有水库32座...' --pin
python3 wb.py search "高密汛情" --ai
```

# TA 评语 Prompt 改造 + 写回前校验 — 可落地技术方案

> 仓库：`/workspace/canvas-homework-grading`（分支 `feat/ta-grading-v1`）  
> 对照代码：`app/multimodal_client.py`（SYSTEM_PROMPT / schema）、`app/agent.py`（合并评语 / 清分）、`app/scoring.py`（`build_comment` / `evaluate_questions`）、`app/writeback.py`（`can_writeback` / `WRITEBACK_MAX_BATCH`）、`app/main.py`（`grade_run` / `patch_student` / `writeback`）、`app/question_bank.py`（答案导入）  
> 产品定稿：评语学生可见；写回前人工确认终稿；打分仍依赖官方答案；占位答案禁止全班批改；错题须「改正方向 + 正确要点」；禁止内部话术。  
> **勿 push；勿联系他人。** 实施可委派本机 Grok Build 对仓 `/workspace/canvas-homework-grading`。

---

## 1. 目标与边界

### 1.1 目标

1. **学生可见评语质量**：模型产出的 `comment_draft`（及规则兜底评语）符合助教口吻中文、按题格式、错题含改正方向与正确结果/要点；全对可简短。
2. **写回前硬闸门**：在助教确认写回前，对**终稿评语**（`effective_comment()` = `final_comment` 优先，否则 `proposed_comment`）做确定性校验；不通过则阻断写回并给出可读原因。
3. **打分策略不变**：分数仍只由 `app/scoring.py` 规则引擎计算；模型不得给分；无官方答案 → `needs_review` 且 `proposed_score is None`；禁止无答案时 AI 全权打分。
4. **占位答案闸门**：答案为占位（如「略」、TODO、stub、过短无实质内容）时，**不得全班批改**；须先换真实答案再 `POST .../grade`。

### 1.2 边界（本次不做）

| 不做 | 说明 |
|------|------|
| 改扣分公式 / 保底 95 | 仍用 `RULES_VERSION=100-minus-unique-big-questions-floor95-v1` |
| 自动发布成绩 | 仍只写 grade + text_comment |
| 放宽 `WRITEBACK_MAX_BATCH` | 默认保持 **1** |
| 模型输出最终分数 | Schema 继续忽略 score 类字段 |
| 绕过助教确认 | 写回仍需 `confirm=true` + 人工可改终稿 |
| 跨学生信息进评语 | Prompt + 校验双禁 |

### 1.3 现状缺口（对照代码）

| 现状 | 文件 | 产品差距 |
|------|------|----------|
| SYSTEM_PROMPT 仅要求「简短评语草稿」 | `multimodal_client.py` L39–61 | 无「第X题」格式、无错题「方向+正确要点」、无长度/禁止话术 |
| `build_comment` 仅「全部正确。」 / 「扣分题号：…」 | `scoring.py` L60–68 | 错题无正确要点；不宜直接当学生终稿 |
| stub 评语含「stub」内部词 | `agent.py` `_STUB_COMMENT` | 禁止话术；且 stub 本就不可写回建议分 |
| 无答案走 stub → needs_review | `agent.py` L192–198, L334–357 | ✅ 已满足「无答案不出建议分」；评语仍可能带 stub 原文 |
| 占位「略」仍可 grade，模型可能 uncertain | 实跑见 `local/qwen38-max-acceptance.md` | ❌ 须**先阻断全班批改**，不只靠 needs_review |
| 写回只查 status/score/comment 有无 | `writeback.can_writeback` | ❌ 无禁止词/长度/错题要点启发式 |
| PATCH 可改 final_comment 后写回 | `main.py` patch_student | ✅ 人工确认路径在；缺校验 |

---

## 2. Prompt 改造

### 2.1 系统提示（SYSTEM_PROMPT）要点

在保留现有硬规则（禁输出分数；status ∈ {correct,incorrect,missing,uncertain}；覆盖题号列表；只输出 JSON）基础上，**追加评语专章**：

```
评语硬性规则（comment_draft，学生可见）：
1. 助教口吻、简体中文；总篇幅 ≤800 字。
2. 按题组织，格式必须以「第X题：」起行（X 为题号）；可多行，每题一段。
3. status=incorrect 的题：两句以内，必须同时包含
   - 改正方向（指出错在哪类步骤/概念），以及
   - 该题正确结果或关键要点（可摘自官方答案，勿整段粘贴无关题）。
4. 全对（所有题 correct）：可简短，如「全部正确，继续保持。」不必逐题展开。
5. missing / uncertain：只提示「该题未作答/辨认不清，请助教复核」，不要编造正确解；此类通常整体 needs_review=true。
6. 禁止出现（含子串）：参考答案缺失、答案库暂无、占位、略（单独作答内容时）、TODO、stub、needs_review、model_call_failed、待接入、内部复核标记原文等系统/工程话术。
7. 禁止讽刺、挖苦；禁止提及其他学生姓名/学号/成绩；禁止输出密钥或 URL 令牌。
8. 不要在评语里写分数、得分、扣几分（分数由服务端规则算）。
```

可选：要求模型额外输出结构化字段（见 §2.3），服务端拼装 `comment_draft`，降低格式漂移。

### 2.2 用户提示（build_user_text）要点

现有 payload：`student_id`、`question_numbers`、`official_answer`、附件元数据。改造：

1. **切分官方答案**：按题号从 `answer_text` 抽出每题要点（启发式：行首 `第N题` / `N.` / `N、`）；拼进 user JSON 的 `official_answer_by_question: { "1": "...", ... }`，全文仍可保留作兜底。
2. **明确指令一句**：`对 incorrect 题，comment 必须含改正方向与官方正确要点；勿输出内部状态词。`
3. **不要**把 `review_reasons` 英文 token 或 stub 说明塞进评语示例。
4. 图片部分不变（page images）。

实现位置：`app/multimodal_client.py` 的 `SYSTEM_PROMPT`、`build_user_text`；可选小函数 `_split_answer_by_question(answer_text, question_numbers)` 同文件或 `app/comment_policy.py`。

### 2.3 输出 Schema 字段

**兼容现有** `REQUIRED_TOP`（勿破坏 `parse_model_payload`）：

| 字段 | 类型 | 变更 |
|------|------|------|
| `questions[]` | `{number,status,confidence,evidence}` | 不变；`evidence` 仍给服务端/助教看，**不写入学生评语** |
| `comment_draft` | string | **强化语义**：须已是学生可见终稿候选；服务端再校验 |
| `overall_confidence` | float 0–1 | 不变 |
| `needs_review` | bool | 不变 |
| `review_reasons` | string[] | 仍仅允许服务端白名单词；**禁止**模型把这些词写进 comment |
| `flags` | object | 不变 |

**建议新增（可选，向后兼容）**：

```json
"question_comments": [
  {"number": 6, "text": "符号用反了，正确结果应为……"}
]
```

- 解析：若存在且合法，服务端用其拼装规范化 `comment_draft`（「第{n}题：{text}」换行连接），覆盖或优先于原始 `comment_draft`。
- 若不存在：沿用 `comment_draft` 字符串。
- `REQUIRED_TOP` **不要**把 `question_comments` 设为必填，避免旧测试/旧缓存全挂。

分数相关字段继续：**忽略**。

### 2.4 服务端评语组装优先级

在 `GradingAgent.grade_student`（现 L372–376）调整为：

1. 若规则结果 `status == "proposed"` 且存在 incorrect 列表：  
   - **优先**模型规范化评语（`question_comments` 拼装或合格的 `comment_draft`）；  
   - 不再把裸 `build_comment("扣分题号：…")` 当作唯一终稿（可作助教 UI 内部摘要字段，或仅在模型评语为空时作极简兜底，但兜底若无法含正确要点 → 标 `needs_review` 或 `comment_incomplete`，**清 proposed_score 或阻断写回**——产品要求错题必须有正确要点，故「仅扣分题号」不得进入可写回终稿）。
2. 全对：允许短评「全部正确。」（≤120 字总评亦可）。
3. `needs_review` / stub / 无答案：  
   - `proposed_score = None`（已有）；  
   - **评语**：助教内部可见可用中性文案「待助教复核后填写评语。」；**禁止** `_STUB_COMMENT` 中的 `stub` 字样进入 `proposed_comment`（改文案或写回前剥掉）。学生不可见写回路径本就被 block。

---

## 3. 写回前校验器

### 3.1 模块与挂载点

新建：`app/comment_validation.py`（纯函数，无 I/O）。

挂载：

1. **`can_writeback` 之前或之内**（`writeback.py`）：对 `student.effective_comment()` + 该生 `questions` + run 级 `answer_source` 做校验；失败返回 `(False, "comment_validation:<rule_id>")`。
2. **`PATCH .../students/{sid}`**（可选但推荐）：保存 `final_comment` 时同步跑校验，API 返回 `comment_validation` 警告/错误，便于 UI 提前提示（写回仍以写回闸门为准）。
3. **`grade_run` 结束后**（可选）：对 `status==proposed` 的条目预跑；不合格者降为 `needs_review` 并 `proposed_score=None`，reason 加 `comment_failed_validation`（避免「看起来可写回其实评语违规」）。

### 3.2 规则列表

| ID | 规则 | 失败动作 |
|----|------|----------|
| `R_NO_ANSWER` | run/`answer_source.imported_text` 为空或仅空白 → 不得有可写回建议分；`proposed_score` 必须为 None；写回若无人工 `final_score`+合格评语且非 force 路径则阻断 | 阻断写回 / grade 阶段已 needs_review |
| `R_PLACEHOLDER_ANSWER` | 答案判定为占位（见 §3.3）→ 禁止本 run 进入全班 grade；已 grade 的清分并 needs_review | grade 入口 400 或 short-circuit |
| `R_FORBIDDEN_LEXICON` | 评语匹配禁止词/模式（见下表） | 阻断写回；grade 后降级 needs_review |
| `R_TOTAL_LEN` | `len(comment) > 800`（按 Unicode 码位） | 阻断 |
| `R_PER_Q_LEN` | 每个「第X题：」段落正文 `> 120` | 阻断 |
| `R_FORMAT_WHEN_INCORRECT` | 存在 `status==incorrect` 的题时，评语须能解析到对应「第{n}题」段（允许全角数字） | 阻断或降级 |
| `R_CORRECT_POINT_HEURISTIC` | 对每个 incorrect 题段落，须通过「正确要点」启发式（见 §3.4） | 阻断或降级 |
| `R_NO_SCORE_TALK` | 评语匹配 `(得分\|分数\|扣\s*\d+\s*分\|拟给\|suggested score)` 等 | 阻断（可选 warning→error） |
| `R_SARCASM_LIGHT` | 轻量：匹配「这么简单都不会」等嘲讽模板表（可配置短列表） | 阻断 |
| `R_PII_CROSS` | 评语含其他 `student_id` / 明显「另一位同学」+ 学号模式 | 阻断 |
| `R_EMPTY_WHEN_PROPOSED` | `status` 可写回且 comment 空 | 已有 `no_comment`；保持 |

**禁止词/模式（子串或正则，大小写不敏感处用 IGNORECASE）**：

- `参考答案缺失`、`答案库暂无`、`答案库没有`、`官方答案未导入`
- `needs_review`、`model_call_failed`、`model_parse_error`、`stub_multimodal`、`model_hook_unwired`
- `\bstub\b`、`TODO`、`FIXME`、`placeholder`
- 单独成段或作答位的 `略`（见占位检测；评语里「策略」等含略字的正常词用词边界/`^略$`/`：略` 精细匹配，避免误杀）
- `【待复核】未接入`、`请助教人工评分与评语`（旧 stub 文案）
- `内部`+`话术` 类（可选）

返回结构建议：

```python
@dataclass
class CommentValidationResult:
    ok: bool
    rule_ids: list[str]
    messages: list[str]  # 中文，给助教 UI
```

### 3.3 占位答案检测（全班批改闸门）

函数：`is_placeholder_answer(text: str | None) -> bool`

判定为占位（任一命中）：

1. `text` 空或 `strip` 后长度 `< 20`（可配置 `ANSWER_MIN_CHARS=20`）
2. strip 后整文匹配：`^(略|暂无|待补充|TODO|TBD|placeholder|stub)\s*$`（忽略大小写）
3. 去掉空白后，唯一实质 token 属于 `{略, TODO, TBD, stub, ...}`
4. 正文中「第N题」段落 ≥50% 内容仅为「略」/「略。」（相对本 run `question_numbers`）

**挂载**：`POST /api/runs/{id}/grade` 开头：

```python
if is_placeholder_answer(run.answer_source.imported_text):
    raise HTTPException(400, "placeholder_answer_replace_required")
```

另：`POST /api/question-bank/answers` 导入时可返回 `warning: placeholder_detected`（仍允许保存，但 UI 红字提示不可批改）。

### 3.4 错题「正确要点」启发式

对 incorrect 题号 `n`，取出评语中「第n题：…」段落 `seg`（到下一「第m题」或文末）：

**通过**（满足其一即可，偏召回、可调）：

1. `seg` 含「正确」「应为」「答案是」「结果为」「要点」等提示词之一，**且**长度 ≥ 8；或  
2. `seg` 与该题 `official_answer_by_question[n]` 的字符 bigram Jaccard ≥ 0.15（或连续 4-gram 命中 ≥1）；或  
3. `seg` 同时含「改正/应为/错在/少写/符号」类「方向」词 **与** 至少 1 个来自官方答案的关键词（从答案句抽名词/算式碎片，简单按非中文标点切分取长≥2 token）。

**失败**：仅有「第6题：错误」或仅与 `build_comment` 同类的「扣分题号」→ 不通过。

全对：不跑本规则。

---

## 4. 与现有 needs_review / proposed_score / WRITEBACK_MAX_BATCH=1 的衔接

```
答案导入
  ├─ placeholder? → UI 警告；grade 直接 400（不全班批改）
  └─ 真实答案
        ↓
grade_run → GradingAgent
  ├─ 无答案 / stub / 模型失败 / 低置信 / flags
  │     → status=needs_review, proposed_score=None
  │     → 评语：中性「待助教复核…」（无 stub 字样）
  ├─ 规则引擎 evaluate_questions → proposed_score（仅非 clear 路径）
  └─ 合并模型评语 → 预校验 comment
        ├─ 失败 → needs_review + proposed_score=None + reason comment_failed_validation
        └─ 成功 → status=proposed，final_* 预填供助教改
              ↓
助教 PATCH final_score / final_comment（可改）
              ↓
writeback_batch(confirm=True, max_batch=1)
  ├─ can_writeback：skipped / needs_review(无 force) / 无分 / …
  ├─ NEW: validate_comment_for_writeback(...)
  └─ 通过 → conflict_check → put_grade → put_comment → verify
```

| 已有机制 | 衔接方式 |
|----------|----------|
| `_CLEAR_SCORE_TOKENS` | 保持；新增 `comment_failed_validation`、`placeholder_answer` 纳入清分集合 |
| `needs_review` + `force_authorized` | 人工放行仍允许写回，但 **评语校验不因 force 跳过**（学生可见，必须干净） |
| `proposed_score is None` | 无答案/占位/校验失败路径保持 |
| `WRITEBACK_MAX_BATCH=1` | `Settings.writeback_max_batch` 不改；单测继续断言 |
| `build_comment` | 降为内部摘要或不合格兜底；学生终稿以校验通过的模型/助教文为准 |
| Suggest vs writeback mode | 不变；suggest 仍 dry_run |

`force_authorized` 语义收紧说明：可放行「规则不确定」类 needs_review，但 **不能**放行禁止话术评语；若助教坚持，须先改掉 `final_comment` 使校验通过。

---

## 5. 测试用例表

| # | 场景 | 输入要点 | 期望 |
|---|------|----------|------|
| T1 | 无官方答案 | `imported_text=None`，有附件 | `needs_review`；`proposed_score is None`；评语无 stub/needs_review 原文；不可写回 |
| T2 | 占位答案「略」 | 答案全文 `略` | `grade` 返回 400 `placeholder_answer_replace_required`；不调用模型全班批改 |
| T3 | 占位 TODO | 答案 `TODO: fill later` | 同上占位阻断 |
| T4 | 有答案全对 | 模型全 correct + 短评 | `proposed`；分 100；评语可无逐题；校验通过 |
| T5 | 有答案判错且要点齐全 | incorrect={6}；评语「第6题：符号用反，正确应为 P(A∪B)=…」 | `proposed`；分 99；写回前校验 ok |
| T6 | 判错但无正确要点 | 评语「第6题：错了」 | 预校验失败 → needs_review 或写回 `comment_validation:R_CORRECT_POINT_HEURISTIC` |
| T7 | 评语含禁止词 | 含 `stub` / `needs_review` / `参考答案缺失` | 校验失败；不可写回 |
| T8 | 超长 | 总评 >800 字 | `R_TOTAL_LEN` 失败 |
| T9 | 单题超长 | 某「第X题」段 >120 | `R_PER_Q_LEN` 失败 |
| T10 | 助教改稿后通过 | 先失败评语，PATCH 合格终稿 | 校验通过；`confirm`+batch=1 可 dry_run complete |
| T11 | needs_review 无 force | 任意 | `needs_review_blocked` |
| T12 | WRITEBACK_MAX_BATCH | 一次 2 个 student_ids | 拒绝，`WRITEBACK_MAX_BATCH=1` |
| T13 | 模型 JSON 仍无 score | payload 带 score=87 | 忽略；分只来自 scoring |
| T14 | stub 路径评语 | 无 key | 评语无 `\bstub\b`；清分 |
| T15 | 启发式命中官方答案 n-gram | 评语复述答案关键式 | `R_CORRECT_POINT_HEURISTIC` 通过 |

单测文件建议：

- `tests/test_comment_validation.py`（纯规则）
- `tests/test_placeholder_answer_gate.py`（grade 入口）
- 扩展 `tests/test_multimodal_qwen.py`（prompt 常量含关键禁令句；组装评语）
- 扩展 `tests/test_writeback_state.py`（写回挂钩校验）
- 扩展 `tests/test_agent_stub.py`（stub 评语无禁止词）

---

## 6. 给实施 Bot 的有序任务

> 委派方式：对本机仓 **`/workspace/canvas-homework-grading`**（分支 `feat/ta-grading-v1`）使用 **Grok Build** 实施。勿 push；勿改无关课程策略；保持 `WRITEBACK_MAX_BATCH` 默认 1。

### Task 0 — 基线

1. `cd /workspace/canvas-homework-grading && git branch --show-current` 确认 `feat/ta-grading-v1`。
2. `pytest -q` 记录基线绿灯。

### Task 1 — `app/comment_validation.py`

1. 实现 `is_placeholder_answer`、`FORBIDDEN_PATTERNS`、`validate_student_comment(comment, *, questions, answer_by_question=None) -> CommentValidationResult`。
2. 单测 T2/T3/T6/T7/T8/T9/T15（不依赖网络）。

### Task 2 — Prompt + schema

1. 改 `SYSTEM_PROMPT` / `build_user_text`（§2）。
2. 可选解析 `question_comments`；拼装规范化 `comment_draft`。
3. 单测：解析仍忽略 score；prompt 字符串含「第X题」与禁止内部话术要点。

### Task 3 — Agent / scoring 衔接

1. 替换 `_STUB_COMMENT` 为无禁止词中性文案。
2. `_CLEAR_SCORE_TOKENS` 增加 `comment_failed_validation`、`placeholder_answer`。
3. `grade_student`：合并评语后跑预校验；失败则 needs_review + 清分。
4. 弱化裸 `build_comment` 作为可写回终稿的路径（incorrect 且无要点 → 不得 proposed）。

### Task 4 — Grade 入口占位闸门

1. `main.grade_run`：占位答案 → HTTP 400，body 明确 `placeholder_answer_replace_required`。
2. 答案导入 API 可选 warning 字段。

### Task 5 — 写回挂钩

1. `can_writeback` 或 `writeback_student` 开头调用校验；失败不写 Canvas。
2. **force_authorized 不跳过评语校验**。
3. 扩展 writeback 单测 T10/T11/T12。

### Task 6 — PATCH UX（可选同 PR）

1. `patch_student` 返回 `comment_validation` 字段，便于前端展示。

### Task 7 — 验收跑通

1. `pytest -q` 全绿。
2. 对照 §7 清单自检，在 PR/本地笔记勾选（仍勿 push，除非主会话另有指示）。

---

## 7. 验收清单

- [ ] **无答案**：不产出可写回 `proposed_score`；`needs_review`；评语无禁止话术（无 stub/needs_review 原文等）。
- [ ] **占位答案**（略/TODO/过短）：全班 `grade` 被阻断；提示先换真实答案。
- [ ] **有答案且判错**：可写回路径上的评语含改正方向 + 正确要点（启发式 + 人工抽检至少 1 例）。
- [ ] **全对**：短评可通过校验。
- [ ] **禁止话术**：注入禁止词的草稿无法写回。
- [ ] **长度**：总评≤800、单题段≤120 强制执行。
- [ ] **助教可改后确认写回**：PATCH `final_comment` → `confirm=true` → batch≤1；分数与评语仍分步核验。
- [ ] **打分策略未改**：分只来自 `scoring.py`；模型 score 忽略；保底 95 逻辑不变。
- [ ] **`WRITEBACK_MAX_BATCH=1`** 仍生效。
- [ ] **学生可见**：文档/UI 保持「评语将对同学可见」提示（若已有文案则不删）。
- [ ] 新增/扩展单测覆盖 §5 表中 T1–T15 核心项；`pytest` 通过。
- [ ] 未 push；未向他人发送真实学生评语或密钥。

---

## 8. 回报路径与摘要

| 项 | 内容 |
|----|------|
| 方案路径 | `/workspace/canvas-ta-grading-comment-prompt-validation.md` |
| 实施仓 | `/workspace/canvas-homework-grading` @ `feat/ta-grading-v1` |
| 核心改动面 | `multimodal_client.py`（prompt/schema）、新建 `comment_validation.py`、`agent.py`（清分+评语）、`main.py`（占位闸门）、`writeback.py`（写回校验）、tests |
| 摘要 | 用 Prompt 约束学生可见评语格式与错题要点；用确定性校验器+占位答案闸门保证无答案/占位不出可写回建议分且无内部话术；写回仍人工确认且 batch=1；打分规则不动。 |
| 委派 | 实施 Bot 按 §6 有序任务，经 **Grok Build** 改本机仓；完成后对照 §7 勾选并向主会话回报。 |


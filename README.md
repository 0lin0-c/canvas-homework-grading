# Canvas 作业批改项目

面向交大 Canvas 的可复用作业批改流程设计：复用浏览器登录，获取教材官方答案，核对全部学生附件，按课程规则评分，将分数与所需评论提交到 Canvas，并回读验证保存结果。

**当前阶段：设计文档与演练原型 + V1 助教批改应用（本地可运行）。真实读取和本地模拟已验证，真实分数/评论写回尚未验收。**

## 文档导航

| 文档 | 内容 |
|---|---|
|[流程设计](docs/01-workflow-design.md)|范围、答案、附件读取、建议、写回与完成条件|
|[课程与评分策略](docs/02-course-policy.md)|100分起、每错一题扣1分、通常最低95分，以及待明确规则|
|[运行记录与恢复](docs/03-run-record.md)|提交版本、逐题证据、保存状态、重复运行和人工修改保护|
|[验证与验收](docs/04-validation.md)|真实只读证据、14项模拟结果、首次真实写回门槛|
|[辅助工具设计](docs/05-supporting-tools.md)|二维码解码工具、虚构数据演练台及运行方法|

## V1 助教批改页（快速开始）

本机单助教应用：粘贴作业链接 → 加密保管 Canvas Token → 拉提交 → Agent 建议分/评语 → 复核改分 → **一点确认**写回（分数+评语分步核验）。主通道为 Canvas REST API（`https://oc.sjtu.edu.cn/api/v1`），不依赖浏览器自动化写回。

### 产品约束（已锁定）

1. 评语使用 Canvas `comment[text_comment]`（对学生可见）
2. **不**点击成绩发布/publish
3. 所有写回需人工确认（高置信度仅排序置顶）
4. 大题去重扣分，保底 95；太多空白/异常 → `needs_review`，不自动低分
5. 答案优先助教粘贴/上传；不依赖出版社登录墙

### 安装

```bash
cd canvas-homework-grading
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：设置 GRADING_SECRET_KEY（长随机串）
```

生成主密钥示例：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

可选：将题库 PDF 放到 `local/question-bank.pdf`（目录已 gitignore）。

### 启动

```bash
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。

### 对样例作业跑通（只读 → 建议）

样例：`https://oc.sjtu.edu.cn/courses/94139/assignments/428317`

1. 在 Canvas：账户 → 设置 → **新建访问令牌**（需课程 94139 评分权限）
2. 在应用「凭证」页粘贴 token（密码框）；服务端 AEAD 加密写入 `local/secrets/`，**永不回显完整 token / 不进前端**
3. （推荐）粘贴官方答案文本到「答案导入」；无答案时 Agent 会标 `needs_review`
4. 粘贴作业 URL →「拉取提交并创建 Run」→ 打开复核页 →「运行 Agent 批改」
5. 改分/改评语后，将 run 模式切到 `writeback`，勾选 **至多 WRITEBACK_MAX_BATCH 人**（默认 **1**）→「确认写回」
6. 首条真实写回回读通过后，再在 `.env` 提高 `WRITEBACK_MAX_BATCH`

也可用环境变量种子 token（仍会加密落盘，勿提交 `.env`）：

```bash
# .env
CANVAS_TOKEN=your-token-here
```

### 测试

```bash
pytest -q
```

覆盖评分规则（对齐 docs/04 本地模拟场景）与写回状态机（冲突、门控、评语去重、分步恢复）。

### 无模型 API Key 时

若未设置 `MODEL_API_KEY` / `OPENAI_API_KEY`，使用**确定性规则引擎 + stub 多模态钩子**：仍产出 `questions[]` / `proposed_score` / `proposed_comment` / `confidence` / `review_reasons` schema。答案文本中可用测试指令：`INCORRECT:6,7`、`MISSING:1,2`、`UNCERTAIN:15`。

### 安全要点

- 默认只监听 `127.0.0.1`
- Token 仅服务端 vault；日志与导出 JSON 剥离签名 URL
- 真实写回默认 `WRITEBACK_MAX_BATCH=1`

## 典型流程（设计文档）

确认作业和题号 → 获取相应官方答案 → 清点提交版本与全部附件 → 逐页核对 → 生成评分与评语草稿 → 按授权写回 → 重新进入页面核验 → 保存进度并处理异常。

运行模式分为演练、建议和正式写回。正式写回不能以“已生成建议”结束；需要评论时必须完成评论提交，而不仅是填入输入框。

## 验证现状

- 已验证：登录复用、作业读取、二维码解码、答案预览及过期恢复、5页样本完整翻阅、多附件切换、未提交分支。
- 本地模拟：评分、异常分流、保存与刷新回读、中断恢复、重复评论防护、人工改分冲突、新版本失效，共14项。
- 未验证：真实Canvas分数保存、真实评论提交、成绩发布和在线PDF批注。
- V1 应用：本地 uvicorn + pytest 可跑；真实 API 写回仍受首条门控约束。

## 仓库边界

设计源自已完成的浏览器演练，按项目设计文档组织。V1 应用代码在 `app/`。真实学生名单、答卷、成绩、登录信息、临时签名地址和个人绝对路径不纳入仓库；运行结果仅保存在本机 `local/`、`work/`、`outputs/`。

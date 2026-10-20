# Qwen 3.8 Max 接入

批改模型固定为 `qwen3.8-max`，使用官方 OpenAI 兼容 Chat Completions 图片接口。当前采用非思考模式 `enable_thinking=false`、JSON 输出；真实账号模型权限、地区及这些参数的可用性，需配置密钥后验收，不自动降级到其他模型。

## 启动

1. 安装 `tools/requirements.txt` 中的依赖（批改后端使用 Pillow，其余为既有二维码工具依赖）。
2. 将 `.env.example` 复制为项目根目录 `.env`，填写 `DASHSCOPE_API_KEY` 和控制台给出的 `QWEN_BASE_URL`。也支持同名进程环境变量，环境变量优先。密钥不进入前端、Git、模型输入或运行报告。
3. 停止旧的纯静态预览服务后运行：

```powershell
python tools/serve.py --port 8766
```

打开 `http://127.0.0.1:8766/review-workbench.html`。“批量修改作业”调用本地 `/api/grade`，逐位调用 Qwen；请求期间禁用启动按钮。无密钥时给出配置提示，不使用模拟评分替代真实模型结果。

## 输入与输出

`prototypes/local/batch-source.json` 是批改输入，独立于展示结果。包含作业链接、题号、公共原题/官方答案截图、学生标识和全部附件页面。每个页面有 `id`、`attachment_id`、`page`、`path`。跨屏长页可用同一附件页号的多张图片，页面 ID 必须唯一。本次5人输入已准备，共7附件、14页、18张页面视图，去除了浏览器工具栏和评分侧栏。模型请求不包含先前分数、判错标签、评语或学生姓名字段。

原题和官方答案在本地按题号复用；每名学生独立请求仍会发送公共图片。是否命中服务端缓存由接口 usage 决定，不能把本地图片复用宣称为模型输入免费。

Qwen 返回逐题状态、原因、已读页面和作答证据归一化坐标。服务端校验全部题号/页面及坐标，用 Pillow 从输入页面裁剪实际学生笔迹；定位本身仍需教师看图复核。每个明确错误大题扣1分、最低95分，待核对题不自动扣分，错章节整份暂不评分。评语按编号生成，不写扣分。异常规则继续沿用课程策略。

只有5人全部成功后才原子替换 `prototypes/local/batch-review.json`，生成新的复核版本，旧确认不复用。失败保留原展示结果，每个成功返回的响应和用量保存在 `prototypes/local/qwen-runs/<run_id>/`，包括失败前已产生的用量。网络超时不自动重试，以免重复计费；这类请求用量可能未知。

`usage.json` 保存每次官方响应原始 usage；成功批次汇总 `prompt_tokens`、`completion_tokens`、`total_tokens`。缓存输入和推理 token 如接口提供则保留在原始明细，不重复加到总量。缺失用量标为不可用，不补造数字。页面仅展示此批 Qwen 用量，不混入 Codex 开发会话用量。

## 当前边界与验证

- 本地只监听127.0.0.1；批改入口要求同源请求，密钥读取在后端，拒绝非官方模型域名及重定向。
- 新作业需要先通过现有浏览器流程采集完整附件并准备公共资料；本地服务没有绕过 Canvas 登录或自动下载接口。输入其他作业链接会明确拒绝，不拿旧答卷充数。
- “确定”仍只确认本地结果；没有 Canvas 写回接口。
- 无密钥 mock 测试覆盖请求模型、完整页面/题号校验、无效裁剪、评分规则、编号评语、token 汇总及失败保留旧结果。真实模型质量、手写理解、裁剪定位及实际 token 待 API key 配置后测量。

运行测试：`python -m unittest discover -s tests -v`。

官方依据：[首次调用千问 API](https://help.aliyun.com/zh/model-studio/first-api-call-to-qwen)、[文本与多模态生成](https://www.alibabacloud.com/help/en/model-studio/text-generation)。Base URL 必须匹配密钥所属地域和业务空间。

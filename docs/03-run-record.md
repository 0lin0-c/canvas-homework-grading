# 运行记录、版本核对与恢复设计

每次建立独立文件；不要把真实学生名单、答卷、成绩提交到项目仓库。建议 JSON 结构：

```json
{
  "mode": "rehearsal|suggest|writeback",
  "assignment_url": "本次真实链接",
  "rules_version": "该次规则的版本或摘要",
  "answer_source": {"resource_url": "稳定资源页", "chapter": "习题一", "pages_read": [1, 2, 3]},
  "students": [{
    "student_id": "从当前页面确认的标识",
    "submission": {"submitted_at": "可见时间", "attempt": null, "attachments": [{"id": "可见附件标识", "name": "文件名", "page_count": 5, "pages_read": [1,2,3,4,5]}]},
    "questions": [{"number": 1, "status": "correct|incorrect|missing|uncertain", "evidence": "附件与页码、必要依据"}],
    "proposed_score": null,
    "review_reasons": [],
    "platform_before": {"grade": null, "comment_evidence": []},
    "authorization": null,
    "status": "pending|read_complete|needs_review|proposed|grade_verified|comment_verified|complete|unknown_after_write|skipped",
    "platform_after": {"grade": null, "comment_evidence": []},
    "verified_at": null
  }]
}
```

去重依据至少含作业ID、学生ID、提交版本证据、规则版本和审批后的结果。不能仅用姓名、列表位置或本地“已写入”布尔值。

处理重复运行：

1. 提交版本一致、本地有核验记录且页面结果一致：跳过。
2. 提交变化：重新读取并生成新建议，不沿用旧批准。
3. 平台分数/评论与上次回读不同：视为可能人工修改，待复核。
4. 分数成功但评论失败：分别记录；恢复时只处理缺失部分。
5. 写入结果不明：先在平台回读；不能排除评论已发布则禁止直接再提交。
6. “未提交”是独立状态，不产生分数建议；不可读与缺题也分开记录。

真实平台不提供可依赖的自动幂等保证。以上是 Agent 的核验流程，不能声称后台系统已经实现。

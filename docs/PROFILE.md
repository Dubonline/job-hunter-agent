# 配置、简历及画像

`job-agent configure`提供中文向导，收集检索、学历、工作年限、项目、英语、个人期望月薪及排除偏好；摘要确认后保存。命名配置放在Git忽略的`.local/profiles/<名称>/config.yaml`；正式命令用全局`--profile 名称`选择，不切换BOSS账号，也不绕过共享防重/额度。

`job-agent resume-intake [文件路径]`在未传路径时询问PDF/DOCX。pypdf/python-docx本地提取文字，再使用`.env`配置的DeepSeek（当前仅官方api.deepseek.com）选择有据优势及重点项目；不发送原PDF/图片做OCR，不切换模型供应商。没有配置有效服务则明确失败。原文自由文本仍可能包含敏感内容，使用前应核对，不承诺任意模板全面匿名化。

待确认材料放在`.local/resume-drafts/<批次>/`。使用`job-agent resume-review <批次>/draft.json`逐项修改确认；未选择字段保留，技能累积，原文证据与用户更正分开记录。`resume-import`仅本地提取辅助入口，不表示已调用DeepSeek。扫描件、加密PDF、旧.doc明确报告支持边界。

项目场景/职责按最近实践时间衰减，新建画像默认重点窗口30个月，可显式设置起点；历史技术累积保留。权重不是技能熟练度或求职成功率，也不能代替必备任职条件核查。HR自动回复尚未实现。当前误投风险及待办见[已知问题](KNOWN_ISSUES.md)。

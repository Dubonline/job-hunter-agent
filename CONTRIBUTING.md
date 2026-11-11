# 参与改进

欢迎提交Issue、文档改进与PR。请描述预期行为、实际行为和最小复现，使用合成职位与画像，隐藏姓名、联系方式、密钥、Cookie和聊天内容。

修改应小而明确。纯文档不要求浏览器测试；修改页面交互时使用临时Profile和自写页面。不得用真实沟通或批量消息验证代码，不自动重试结果未知的动作。

```bash
uv sync --locked
uv run ruff check src tests
uv run pytest -q tests/unit
```

集成测试需本机Chrome和已构建的补丁引擎。默认不启动真实站点授权测试。提交前请执行`python3 tools/check_public.py --staged`；项目不接受真实配置、运行数据或内部开发记录。

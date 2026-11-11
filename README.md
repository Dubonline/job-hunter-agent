# BOSS智能投递助手 · job-hunter-agent

本地运行的 BOSS 岗位筛选与沟通助手：检索条件 → JD读取 → 规则/模型评审 → 收藏及沟通 → 每日分批简报。

项目处于早期阶段，欢迎通过 Issue 提出建议或通过 PR 改进。当前主要验证环境为 macOS、Chrome 和 Python 3.14；筛选选项有限，匹配判断仍需用户核对。

## 功能

- 接入用户已登录的 Chrome，单连接、断线停止，不自动登录或读取个人浏览器资料。
- 固定筛选框的展开、选择和状态核对，逐岗读取已加载DOM并审计读取范围。
- 可选模型评审与本地规则，薪资明文或本机OCR交叉核对。
- 显式启动后，高度契合收藏并沟通，通用岗位只沟通，成功后留在此页。
- 跨运行防重、未知结果停止、每日本机额度和分批简报。

## 安装

请先阅读[安装说明](docs/INSTALL.md)。正式浏览器命令要求带项目补丁的agent-browser，**不会自动下载或运行浏览器引擎**。

```bash
uv sync --locked
cp .env.example .env
cp config/preferences.example.yaml config/preferences.local.yaml
cp config/candidate.example.yaml config/candidate.yaml
```

填写真实画像、检索条件和个人期望月薪；模型服务设置仅存本机`.env`。示例不是任何用户的真实画像。不要提交个人配置、密钥、日志或简报。

```bash
# 检查配置与安装；不连接浏览器
uv run job-agent doctor
# 只读评审，不收藏或沟通
uv run job-agent scan --engine model --limit 5
# 显式目标沟通人数；请先小范围核对结果
uv run job-agent apply --count 1 --limit 5 --no-record-lists
# 不连接浏览器或模型，更新今日简报
uv run job-agent brief
```

`--count`默认50，允许0–150且不能超过本机今日剩余；`--limit`是独立评审上限，默认200，受滚动轮数限制。0沟通目标不启动浏览器。名单默认关闭，`--record-lists`可开启。

简报保存在`daily_briefs/`，同日一份，累计在前，各次实际运行按分钟分段；开启的成功收藏/沟通名单放末尾，跳过只计数。

## 当前边界

- 目前支持的城市为上海；筛选字段和值见`browser/filters.py`的选项表。未支持的非空条件会停止，不静默忽略。
- DOM读取审计不等于已经与站点后台全文核对。模型判断不代表招聘方认可或所有任职资格均已满足。
- 本机额度不包含用户在其他设备的手动沟通，不等于官方实时剩余。未知尝试占额度，禁止自动重试。
- 每次沟通使用平台现有打招呼流程，不追加消息、附件或自动跟聊。

## 开发与参与

```bash
uv run ruff check src tests
uv run pytest -q tests/unit
```

浏览器集成使用临时Profile和合成页面，不能将测试当成真实站点验收。说明见[贡献指南](CONTRIBUTING.md)。请勿在Issue、日志或截图中公开个人资料或服务密钥。

本项目采用 Apache-2.0；第三方补丁归属见[NOTICE](NOTICE)。

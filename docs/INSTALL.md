# 安装与配置

## 环境

- Python 3.14.10或更新的3.14补丁版本，uv。
- macOS、Google Chrome；本机薪资OCR和原生授权助手使用Apple框架与Swift。
- 模型服务支持的API地址、模型标识和密钥，保存到本机`.env`。

## 浏览器引擎

本项目固定使用agent-browser 0.42.0的原生daemon协议，并要求按需Runtime事件订阅补丁。不要直接用未打补丁的新版本替换；CLI连接前会检查引擎能力。以下安装步骤由使用者主动执行：

```bash
mkdir -p .local/tools
git clone --branch v0.42.0 --depth 1 https://github.com/vercel-labs/agent-browser.git .local/tools/agent-browser-experimental
git -C .local/tools/agent-browser-experimental apply ../../../patches/agent-browser-v0.42.0-runtime-events.patch
cargo build --release --manifest-path .local/tools/agent-browser-experimental/cli/Cargo.toml
```

将`.env`中的`AGENT_BROWSER_BINARY`设置为`.local/tools/agent-browser-experimental/cli/target/release/agent-browser`。如果上游标签、源码或协议不匹配，停止并检查，不忽略补丁失败。

## 本机助手

首次安装或Swift源文件修改后执行，日常运行不重复编译：

```bash
sh scripts/build_chrome_permission.sh
swiftc -module-cache-path /tmp/job-salary-swift-cache src/job_hunter/extraction/salary_ocr.swift -o .local/tools/salary-ocr
```

原生授权助手只识别Chrome远程调试允许弹框，不操作BOSS页面。需要现有macOS辅助功能权限；不自动请求额外系统权限或修改Chrome设置。`JOB_AGENT_CHROME_AUTO_ALLOW=0`可禁用该助手，禁用后连接授权需用户自行处理。

## 使用前

1. 从通用模板创建本机配置，填写真实学历、经历、语言和期望月薪。
2. 打开Chrome，人工登录BOSS并开启远程调试；不要提供账号密码或Cookie。
3. 先执行`job-agent doctor`与小规模`scan`核对条件和结果，再显式启动`apply`。

`config/preferences.local.yaml`优先于通用示例；真实候选人画像放`config/candidate.yaml`。示例中不启用任何个人排除偏好。页面检索条件与个人任职资格是两个不同层次。

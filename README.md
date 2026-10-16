# VerdantFlare Google Channel

## 1. 实现状态

Google AI Studio 渠道的独立账号管理服务与 AppDashboard。当前源码实现账号加密持久化、组织隔离、默认与启停、人工计费登记、只读授权检查、原生图片调用与日志读取。图片适配仅通过模拟接口验证，Image MCP 切换、Network 代理、价格结算、etcd 发布及发行部署尚未完成；不得据此宣称已在线可用。

产品设计及接口事实源位于设计仓库的 `docs/design/app/google/README.md` 和 `docs/design/contracts/channel-provider-v1.md`，本仓库不另立业务契约。

## 2. 入口与配置

服务入口为 `python -m google_channel.server`，管理监听端口 8000，仅允许 Studio 内部调用；执行监听端口 8001，仅允许受控 Image MCP 调用。凭据从 `GOOGLE_MANAGEMENT_TOKEN`、`GOOGLE_EXECUTION_TOKEN`、`GOOGLE_ENCRYPTION_KEY` 读取，状态位置由 `GOOGLE_STATE_DB` 指定。整个服务生命周期持有状态卷执行锁。生产环境启动和服务验证使用开发集群，不在本机启动服务。

`GOOGLE_ALLOWED_API_ORIGINS` 可配置管理员已批准的其他 HTTPS 基地址，默认仅允许 Google 原生地址。`GOOGLE_DASHBOARD_DIR` 指向摘要校验后的静态页面包。凭据不能进入源码、页面或日志。

## 3. 本地轻量检查

安装项目声明的依赖后，运行 `python -m unittest discover -s tests -p 'test_*.py'`、`python -m compileall -q google_channel` 和 `node --check dashboard/dashboard.mjs`。测试使用临时 SQLite 与模拟 HTTP 响应，不提交生成请求。

制品资源由 `google_channel.dashboard` 生成并验证摘要。`Dockerfile` 仅打包生产代码和页面，使用非 root 用户运行；`.dockerignore` 采用白名单，不带入测试、凭据及工作区文件。直连客户端禁用环境代理继承。

## 4. 构建交付

`.github/workflows/google-channel.yml` 在 release 分支变更时通过 Actions 构建并推送 `google-channel-v0.1.0`，复用团队 ACR Secrets、GHA 缓存与版本防覆盖检查。构建后从已发布镜像摘要提取页面包，生成含源码提交、Actions 运行 ID、镜像摘要与文件摘要的 release.json，并上传 `google-dashboard-v0.1.0` 制品。正式部署仅使用该制品对应的声明和资源摘要。

工作流已经编写，尚未创建远端仓库、运行 Actions 或发布首版。运行工作流前配置团队 Registry Secrets；发布部署遵循设计仓库 `docs/GithubAction.md` 与目标集群规范。

取得实际 Actions 制品并独立核验源码提交及镜像摘要后，使用设计仓库 `scripts/appdashboard/prepare_channel.py --app-id google` 生成 Studio 登记片段。工具校验整包资源摘要及声明，仅开放管理员账号／日志管理路径，不开放执行提交。生成片段不会部署、安装或授予访问权限；不能用本地模拟 release.json 代替真实发行材料。

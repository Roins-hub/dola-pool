# Studio 增强管理版

这是可独立启动的本地增强版（应用版本 `0.9.0`），与仓库根目录的上游 `2.6.11` 主程序一同提供。两版各自使用自己的 Python 模块、HTTP 接口、账号目录、额度规则和数据库。这里的功能尚未移植到根目录主服务。

## 功能

- 独立的管理端、成员端和登录页面，管理员与成员权限、会话及 API Key 管理。
- Dola 普通任务与插件专家任务、批量申请、双片结果、任务阶段和平台回执展示。
- 图片、音频、视频参考素材，上传顺序、任务归属和结果关联检查。
- 账号 CK / Google 登录导入、线路统计、共享调度和普通／专家额度账本。
- 可选的豆包网页工作流，与 Dola 的账号、调度及结果分别管理。
- 保留浏览器扩展和固定版本的 HTTP 协议依赖，并提供 Python / JavaScript 回归测试。

可用模型、时长和额度取决于平台实际回执；本地测试不代表真实账号的生成验证。豆包入口默认关闭，需要配置后启用。

## 安装与启动

要求 Python 3.11+、Node.js 20+，以及 `PATH` 中可用的 FFmpeg / ffprobe。在仓库根目录创建这版自己的虚拟环境：

```powershell
python -m venv editions/studio/.venv
editions/studio/.venv/Scripts/python.exe -m pip install -r editions/studio/requirements.txt
editions/studio/.venv/Scripts/python.exe -m patchright install chromium
editions/studio/.venv/Scripts/python.exe editions/studio/run.py
```

Linux / macOS 使用对应的 `editions/studio/.venv/bin/python`。浏览器的系统依赖按 Patchright 安装要求准备。

打开 <http://127.0.0.1:8001/login>，首次注册的用户成为管理员；管理端在 `/admin`，成员端在 `/member`。启动器默认只监听本机 `127.0.0.1:8001`，用 `--host`、`--port` 修改监听地址。它会先切换到本版目录，再加载本版 `server:app`。

根目录的主程序仍按原有说明运行在 `8000`。启动器不调用旧的 Windows 启动脚本，也不会停止已有服务。

## 独立配置和数据

将 [`.env.example`](.env.example) 复制为本目录下的 `.env.local`，按需填写配置。已有进程环境变量优先于 `.env.local`，监听地址和端口由启动器参数设置。两版共用部分环境变量名称，分别启动时应使用各自的环境。

默认相对路径都位于 `editions/studio/` 内，包括 `accounts/`、`tasks.db`、`pool_usage.db`、`proxy_usage.db`、`data/doubao/`、`uploads/` 和 `downloads/`。首次运行没有账号或历史任务；通过本版界面导入账号和配置线路。`proxies.json.example` 只包含占位示例。

不要让两版指向同一份数据库或浏览器 profile。两版数据库结构、认证方式和额度口径不同，本次没有提供自动迁移；使用已有数据前需另行备份和确认迁移方式。要设置公开访问地址，可配置 `DOLA_PUBLIC_BASE`；监听地址与该地址是两个独立设置。

旧的个人部署脚本、历史实测记录、真实会话、运行日志和数据库不包含在此版源码中。

## 协议来源

`vendor/dola_http/` 基于上游提交 `c910f3a40ea9282a7b7d7e0799269d93bef89b69`，保留本地 Cookie 作用域修复；来源、补丁说明和摘要见 [`vendor/dola_http/SOURCE.json`](vendor/dola_http/SOURCE.json)。Studio 的 HTTP worker 使用这个固定版本；仓库根目录继续使用其最新 `protocol/` 实现。

浏览器扩展位于 `extensions/`，由本版 `config.py` 配置加载。插件专家功能使用 `extensions/dola30-plugin1660/` 中的实现。

## 验证

在 `editions/studio/` 目录中，用安装了本版依赖的 Python 执行：

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe tests/run_offline.py
$env:DOLA_TEST_PYTHON = (Resolve-Path -LiteralPath .venv/Scripts/python.exe).Path
$cases = Get-ChildItem -LiteralPath tests -Filter 'test_*.cjs' | Select-Object -ExpandProperty FullName
node --test @cases
```

离线入口跳过需要交互浏览器的页面就绪和生日修复测试。浏览器页面夹具需要已安装的 Patchright Chromium。上游测试在仓库根目录使用 `python -m pytest -q tests` 单独执行，避免两套同名模块混合导入。

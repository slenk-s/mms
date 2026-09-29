# MMS 物料管理系统（MMS）

面向车间场景的桌面物料管理系统。MMS 以 MySQL 作为中心数据源，并在桌面端提供 SQLite 本地缓存、离线同步、固定资产管理和产线查询 Web 服务；同时支持指纹仪、NFC 读卡器及 FTP 自动更新。

> **适用对象：** 系统部署人员、车间管理员与二次开发人员。
> **重要提示：** `config.ini` 包含数据库、FTP、API Key 等敏感配置，已被 Git 忽略。请在本地或发布环境创建并维护，切勿提交到仓库。

## 功能概览

- **物料与资产管理：** 库存明细、领用记录、固定资产、员工与用户台账管理。
- **多车间与离线能力：** 支持在线、半离线、完全离线三种运行模式；以 SQLite 缓存支撑网络不稳定场景下的连续使用和后续同步。
- **产线查询：** 内置 FastAPI 查询服务，为浏览器和局域网终端提供查询页面与 `/api` 接口。
- **硬件集成：** 可选串口指纹仪与 NFC 读卡器。
- **自动更新：** 通过 FTP 检查版本、下载更新包，并由独立更新程序完成替换与重启。
- **国际化：** 内置中文与西班牙语界面资源。

## 系统架构

```text
┌─────────────────────────────────────────────────────────┐
│                    MMS-Main（桌面端）                    │
│ PySide6 界面 · SQLite 本地缓存 · 同步引擎 · 硬件接口      │
└───────────────┬───────────────────────────┬─────────────┘
                │                           │
       ┌────────▼────────┐        ┌────────▼─────────────┐
       │   MySQL 中心库   │        │ MMS-WebServices       │
       │  业务主数据源    │        │ FastAPI 查询服务      │
       └─────────────────┘        └──────────┬───────────┘
                                              │
                                   ┌──────────▼───────────┐
                                   │ 浏览器 / 产线查询终端 │
                                   │ 页面与 /api 接口      │
                                   └──────────────────────┘
```

## 技术栈

| 组件 | 技术 |
| --- | --- |
| 桌面端 | Python 3.11、PySide6（Qt 6） |
| 查询服务 | FastAPI、Uvicorn、可选 HTTPS |
| 数据存储 | MySQL（中心库）、SQLite（本地缓存） |
| 同步与网络 | 定时同步、网络连通性监测 |
| 构建发布 | Cython、PyInstaller |
| 自动更新 | FTP、独立 `MMS-Update` 程序 |
| 硬件接口 | 串口指纹仪、NFC 读卡器 |
| 自动化检查 | GitHub Actions、flake8、pytest |

## 仓库结构

```text
mms_823/
├── client/                         # 桌面端及 Web 服务源码
│   ├── main.py                     # 桌面端入口
│   ├── MMS-Update.py               # 独立更新程序入口
│   ├── build_exe.py                # Windows 打包脚本
│   ├── config.py                   # 运行时常量与路径
│   ├── local_db.py                 # SQLite 本地缓存
│   ├── mysql_client.py             # MySQL 客户端封装
│   ├── sync_engine.py              # 在线/离线同步引擎
│   ├── views/                      # 页面视图（登录、库存、资产、用户等）
│   ├── widgets/                    # 自定义 Qt 组件
│   ├── utils/                      # 配置、凭据、导出、更新等工具
│   ├── hardware/                   # 指纹与 NFC 硬件适配层
│   ├── i18n/                       # 中文、西班牙语资源
│   ├── pyd/                        # 可选 Cython 扩展及编译脚本
│   └── web_/                       # FastAPI 查询服务、模板与静态资源
├── MySQL_/                         # 数据库初始化、模块与迁移 SQL
├── tests/                          # 更新链路和自动化测试脚本
├── .github/workflows/ci.yml        # GitHub Actions CI
├── config.ini                      # 本地运行配置（忽略，不提交）
└── version.txt                     # 发布版本号（数字形式，例如 4305）
```

## 环境要求

- **Python：** 3.11（与 CI 保持一致）
- **数据库：** 可访问的 MySQL 服务；离线缓存由程序自动使用 SQLite 文件
- **操作系统：** 桌面端和打包流程面向 Windows；源码测试可在 CI 的 Linux 环境执行
- **可选硬件：** 已安装驱动并确认串口号的指纹仪或 NFC 读卡器

建议在虚拟环境中安装开发依赖：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install PySide6 pymysql fastapi uvicorn cython pytest PyJWT pyinstaller flake8
```

## 配置与安全

首次部署前，在仓库根目录创建或更新 `config.ini`。配置文件包含以下节：

| 配置节 | 用途 |
| --- | --- |
| `[mysql]` | MySQL 地址、端口、库名和访问凭据 |
| `[app]` | 运行模式与日志级别 |
| `[web_query]` | 查询服务开关、监听地址、端口与 API Key |
| `[serial]` | 指纹仪、NFC 读卡器串口及波特率 |
| `[update]` | FTP 自动更新服务器设置 |
| `[workshops]` | 车间列表和当前车间 |

安全要求：

1. 不要将 `config.ini`、证书私钥（`*.key`、`*.pem`）或运行时数据库文件提交到 Git。
2. 生产环境使用独立的 MySQL 账号，并只授予所需权限。
3. Web 查询服务启用 HTTPS 时，将证书放入 `client/web_/certs/`；该目录已被 Git 忽略。
4. 将 API Key、FTP 密码等凭据通过受控渠道分发，不要写入 README、脚本参数或提交消息。

## 运行模式

| 模式 | 说明 |
| --- | --- |
| `online` | 实时连接 MySQL，业务操作直接访问中心库。 |
| `semi_offline` | 以本地缓存为主，按同步策略与服务器交换数据。 |
| `offline` | 仅使用本地 SQLite 缓存，适用于无网络场景。 |

在 `config.ini` 的 `[app]` 节设置 `app_mode`。不同车间会使用独立命名的本地缓存文件，避免数据混用。

## 快速开始

### 1. 初始化数据库

根据部署需要执行 `MySQL_/` 下的初始化 SQL 或对应模块/迁移脚本。典型全量初始化命令如下：

```powershell
mysql -u <用户名> -p < MySQL_/init.sql
```

> 执行迁移前请先备份目标数据库，并按 SQL 文件中的依赖顺序执行。生产数据库的升级应在维护窗口完成。

### 2. 配置本地环境

创建并填写根目录 `config.ini`，至少确认以下配置可用：

- `[mysql]`：数据库可连接；
- `[app]`：选择 `online`、`semi_offline` 或 `offline`；
- `[workshops]`：设置当前车间；
- 使用 Web 查询、硬件或自动更新功能时，再补充对应的 `[web_query]`、`[serial]`、`[update]` 配置。

### 3. 启动桌面端

在仓库根目录执行：

```powershell
python client/main.py
```

程序会启动 PySide6 桌面应用。首次运行前，请确认数据库服务、网络以及可选硬件均已按配置准备完毕。

### 4. 启动查询 Web 服务（可选）

```powershell
Set-Location client/web_
python run.py
```

服务读取 `[web_query]` 配置。若 `client/web_/certs/` 中同时存在 `server.pem` 和 `server.key`，服务将启用 HTTPS；未提供证书时会回退为 HTTP。启动日志会输出实际访问地址和端口。

## 开发、测试与打包

### 编译可选 Cython 扩展

Cython 扩展并非阅读或运行源码的必需前提；需要构建发布版本时，可在根目录执行：

```powershell
python client/pyd/setup.py build_ext --inplace
```

### 运行 CI 等价检查

CI 固定使用 Python 3.11，并执行语法/未定义名称检查和 pytest。开发环境可执行：

```powershell
flake8 client/ --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=__pycache__,.git,build,dist,pyd/_build_c
pytest tests/ -v --tb=short --ignore=tests/upload_update.py --ignore=tests/test_update.py
```

> 图形界面相关测试在无显示器环境中可设置 `QT_QPA_PLATFORM=offscreen`；GitHub Actions 已采用该方式。

### 打包 Windows 发布产物

```powershell
python client/build_exe.py
```

构建脚本会生成以下 Windows 发布程序：

| 文件 | 用途 |
| --- | --- |
| `MMS-Main.exe` | 车间物料管理桌面端（窗口模式） |
| `MMS-WebServices.exe` | 产线/浏览器查询服务（控制台模式） |
| `MMS-Update.exe` | 独立更新程序，用于替换应用文件并重启 |

生成的 `build/`、`dist/` 和临时打包目录均属于构建产物，不应提交。

## 自动更新与手工验证

桌面端登录后会连接 FTP 检查版本号：有新版本时，状态栏会显示版本差异；用户发起更新后，程序下载 `update.zip`，进行验证，并调用 `MMS-Update.exe` 替换文件和重启应用。

发布前可使用以下脚本验证更新链路：

```powershell
# 打包并上传更新包到 FTP（需要有效的本地 update 配置）
python tests/upload_update.py

# 验证下载、校验与解压流程；按脚本提示执行
python tests/test_update.py
```

这些脚本可能访问 FTP 或修改本机测试目录，因此不会作为 CI 的常规 pytest 用例执行。请仅在受控的测试 FTP 和非生产目录中运行。

## 贡献与提交约定

- 提交前运行与改动相关的检查；修改 Python 代码时至少执行 CI 等价的 flake8 与 pytest 命令。
- 不提交本地配置、数据库缓存、构建产物、私钥、日志或真实业务数据。
- SQL 结构变更应使用可审查、可回滚的迁移脚本，并在提交说明中标明升级顺序和影响范围。
- 版本发布时同步检查 `version.txt`、应用版本常量及 FTP 上的更新包版本。

## 版本信息

当前仓库的发布版本号以根目录 `version.txt` 为准。自动更新、构建和部署时，请确保版本号与实际发布包一致。

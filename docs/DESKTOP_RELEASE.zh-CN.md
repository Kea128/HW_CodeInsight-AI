# 桌面预览版发布

当前桌面预览版面向 Windows x64，使用 Tauri 2 承载本地工作台，并以 PyInstaller sidecar 启动 FastAPI 分析引擎。

## 发布前一次性配置

1. 将项目推送到有 Release 写权限的 GitHub 仓库。当前 `origin` 仍指向上游 `AsyncFuncAI/deepwiki-open`，不要在没有维护者授权时向该仓库发布产品安装包。
2. 使用 Tauri CLI 生成更新签名密钥，私钥只保存到 GitHub Actions Secret。
3. 配置以下仓库 Secrets：
   - `TAURI_SIGNING_PRIVATE_KEY`
   - `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`
   - `TAURI_UPDATER_PUBLIC_KEY`
   - `TAURI_UPDATER_ENDPOINT`，例如
     `https://github.com/<owner>/<repo>/releases/latest/download/latest.json`
4. 为 Windows 正式公开发布配置可信代码签名证书。Tauri 更新签名不能替代 Authenticode 证书。

## 构建与发布

在 GitHub Actions 中手动运行 `Desktop release`，或推送 `desktop-v*` 标签。工作流会：

- 安装 Python、Node 和 Rust 构建环境；
- 将 `api/daemon.py` 冻结为 Windows sidecar；
- 构建 MSI 与 NSIS 安装包；
- 使用 Tauri 私钥签署更新包；
- 创建草稿预发布，并生成在线更新清单。

先在干净的 Windows 10/11 虚拟机验证安装、启动、分析、暂停恢复、重启续跑、更新与卸载，再将草稿 Release 转为公开版本。

## 本地数据

默认 SQLite 数据库位于 AdalFlow 本地数据根目录下的 `codeinsight.db`。可通过 `CODEINSIGHT_DB_PATH` 覆盖。API Token 不会写入数据库；私有远程仓库在应用重启后需要宿主重新注入 Token。

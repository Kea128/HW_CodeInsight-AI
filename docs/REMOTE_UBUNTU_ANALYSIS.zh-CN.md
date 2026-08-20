# 通过 SSH 隧道分析远程 Ubuntu 代码

远程引擎必须只监听 Ubuntu 的 `127.0.0.1`。不要把 FastAPI 的 `8001` 端口直接开放到公网；当前 API 面向受信任的本机宿主，不是公网多租户服务。

## 1. 在 Ubuntu 安装引擎

```bash
sudo apt update
sudo apt install -y git python3.11 python3.11-venv python3-pip
python3 -m pip install --user pipx
python3 -m pipx ensurepath
pipx install poetry

git clone https://github.com/Kea128/HW_CodeInsight-AI.git
cd HW_CodeInsight-AI
poetry install -C api --no-interaction
cp .env.example .env
```

编辑 `.env`，填写要使用的模型密钥和嵌入模型配置。然后启动仅本机监听的分析引擎：

```bash
set -a
source .env
set +a
NODE_ENV=production PORT=8001 PYTHONPATH="$PWD" \
  poetry -C api run python -m api.daemon
```

在 Ubuntu 上验证：

```bash
curl http://127.0.0.1:8001/health
```

## 2. 从 Windows 建立 SSH 隧道

在 Windows PowerShell 中执行，并保持窗口运行：

```powershell
ssh -N -L 18001:127.0.0.1:8001 ubuntu@服务器地址
```

该命令只把 Ubuntu 本机的 `8001` 映射到 Windows 本机的 `18001`，不会向公网开放分析 API。

## 3. 在桌面软件中连接

1. 打开 CodeInsight-AI 的“分析引擎连接”。
2. 输入 `http://127.0.0.1:18001`。
3. 点击“连接引擎”。
4. 添加项目时填写 Ubuntu 上的绝对路径，例如 `/srv/code/my-project`。

任务数据库、索引、文件监听和生成结果保存在 Ubuntu 用户的 `~/.adalflow`，而不是 Windows 机器。

## 4. 后台运行

正式长期运行建议创建 systemd 服务，并将模型密钥放在权限为 `600` 的 EnvironmentFile 中。服务仍应使用：

```text
NODE_ENV=production
PORT=8001
PYTHONPATH=/opt/HW_CodeInsight-AI
```

`ExecStart` 示例：

```text
/home/<user>/.local/bin/poetry -C /opt/HW_CodeInsight-AI/api run python -m api.daemon
```

同时通过 systemd 的 `WorkingDirectory=/opt/HW_CodeInsight-AI` 保持项目根目录正确。

"""Install and prepare the local Ollama runtime for desktop analysis."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from api.desktop_settings import save_desktop_settings

OLLAMA_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
REQUIRED_MODELS = ("qwen3:1.7b", "nomic-embed-text")
MINIMUM_FREE_BYTES = 4 * 1024**3
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def find_ollama_executable() -> str | None:
    discovered = shutil.which("ollama")
    if discovered:
        return discovered
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = (
        local_app_data / "Programs" / "Ollama" / "ollama.exe",
        local_app_data / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Ollama" / "ollama.exe",
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def _ollama_tags() -> list[str] | None:
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:11434/api/tags", timeout=0.5
        ) as response:
            payload = json.load(response)
    except (OSError, ValueError):
        return None
    return [
        str(model.get("name") or model.get("model"))
        for model in payload.get("models", [])
        if model.get("name") or model.get("model")
    ]


def _model_available(required: str, available: list[str]) -> bool:
    return required in available or (
        ":" not in required and f"{required}:latest" in available
    )


class OllamaInstaller:
    def __init__(self):
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._status: dict[str, Any] = {
            "state": "idle",
            "step": "检查本地 AI 环境",
            "message": "",
            "progress": 0,
            "restart_required": False,
        }

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._status.update(values)

    def status(self) -> dict[str, Any]:
        with self._lock:
            status = self._status.copy()
        executable = find_ollama_executable()
        models = _ollama_tags() if executable else None
        status.update(
            {
                "installed": executable is not None,
                "running": models is not None,
                "models": models or [],
                "ready": bool(
                    models
                    and all(
                        _model_available(model, models) for model in REQUIRED_MODELS
                    )
                ),
            }
        )
        if status["ready"] and status["state"] == "idle":
            status.update(
                state="ready",
                step="本地 AI 已就绪",
                message="Ollama 和代码分析模型均已安装。",
                progress=100,
            )
        return status

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                return self.status()
            self._status = {
                "state": "installing",
                "step": "准备安装",
                "message": "正在检查磁盘空间和现有组件…",
                "progress": 1,
                "restart_required": False,
            }
            self._worker = threading.Thread(
                target=self._install, name="ollama-installer", daemon=True
            )
            self._worker.start()
        return self.status()

    def _install(self) -> None:
        installer_path = Path(tempfile.gettempdir()) / "CodeInsight-OllamaSetup.exe"
        try:
            free_bytes = shutil.disk_usage(Path.home()).free
            if free_bytes < MINIMUM_FREE_BYTES:
                raise RuntimeError("可用磁盘空间不足 4 GB，无法安装本地 AI 环境")

            executable = find_ollama_executable()
            if not executable:
                self._download_installer(installer_path)
                self._verify_installer(installer_path)
                self._update(
                    step="安装 Ollama",
                    message="正在静默安装本地 AI 运行环境…",
                    progress=35,
                )
                result = subprocess.run(
                    [
                        str(installer_path),
                        "/VERYSILENT",
                        "/SUPPRESSMSGBOXES",
                        "/NORESTART",
                    ],
                    check=False,
                    timeout=600,
                    creationflags=CREATE_NO_WINDOW,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Ollama 安装失败，退出码 {result.returncode}")
                for _ in range(60):
                    executable = find_ollama_executable()
                    if executable:
                        break
                    time.sleep(1)
                if not executable:
                    raise RuntimeError("Ollama 已安装，但找不到 ollama.exe")

            self._ensure_server(executable)
            for index, model in enumerate(REQUIRED_MODELS):
                progress = 55 + index * 20
                self._update(
                    step=f"下载模型 {model}",
                    message="模型只需下载一次，请保持网络连接…",
                    progress=progress,
                )
                result = subprocess.run(
                    [executable, "pull", model],
                    check=False,
                    timeout=7200,
                    creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"模型 {model} 下载失败")

            save_desktop_settings("ollama", None)
            self._update(
                state="ready",
                step="本地 AI 已安装",
                message="模型已准备完成，正在重启软件以应用设置。",
                progress=100,
                restart_required=True,
            )
        except Exception as error:  # noqa: BLE001 - surfaced as sanitized UI status
            self._update(
                state="error",
                step="安装失败",
                message=str(error),
                progress=0,
                restart_required=False,
            )
        finally:
            installer_path.unlink(missing_ok=True)

    def _download_installer(self, target: Path) -> None:
        self._update(
            step="下载 Ollama",
            message="正在从 Ollama 官方网站下载安装程序…",
            progress=5,
        )

        def report(blocks: int, block_size: int, total_size: int) -> None:
            if total_size <= 0:
                return
            ratio = min(1, blocks * block_size / total_size)
            self._update(progress=5 + int(ratio * 20))

        urllib.request.urlretrieve(OLLAMA_INSTALLER_URL, target, reporthook=report)

    def _verify_installer(self, target: Path) -> None:
        self._update(
            step="验证安装包",
            message="正在验证 Ollama 官方数字签名…",
            progress=28,
        )
        script = (
            "$signature=Get-AuthenticodeSignature -LiteralPath $env:OLLAMA_INSTALLER;"
            "if($signature.Status -ne 'Valid' -or "
            "$signature.SignerCertificate.Subject -notmatch 'Ollama'){exit 1}"
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            timeout=60,
            creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "OLLAMA_INSTALLER": str(target)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            raise RuntimeError("Ollama 安装包数字签名验证失败")

    def _ensure_server(self, executable: str) -> None:
        self._update(
            step="启动 Ollama",
            message="正在启动本地 AI 服务…",
            progress=50,
        )
        if _ollama_tags() is None:
            subprocess.Popen(
                [executable, "serve"],
                creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for _ in range(90):
            if _ollama_tags() is not None:
                return
            time.sleep(1)
        raise RuntimeError("Ollama 已安装，但本地服务启动超时")


installer = OllamaInstaller()

use base64::Engine;
use minisign_verify::{PublicKey, Signature};
use serde::Deserialize;
use std::collections::HashMap;
use std::process::Command;
use std::sync::Mutex;
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};

use tauri::Manager;
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::{Update, UpdaterExt};

const RELEASES_URL: &str = "https://github.com/Kea128/HW_CodeInsight-AI/releases/latest";
const UPDATE_MANIFEST_URL: &str =
    "https://github.com/Kea128/HW_CodeInsight-AI/releases/latest/download/latest.json";
const UPDATE_PUBLIC_KEY: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEI5RkM2RUU5Mzc4MkRCOQpSV1M1TFhpVDdzYWZDOGxXczNuWTB3WjB6R0tWb1pmWnF3RXAwcnZCVFY1NFBjV2hORE5mYnhwNAo=";
const UPDATE_ATTEMPTS: usize = 3;
const UPDATE_RETRY_DELAY: Duration = Duration::from_secs(2);
const UPDATE_TIMEOUT: Duration = Duration::from_secs(30);

struct DaemonProcess(Mutex<Option<CommandChild>>);
struct PendingUpdate(Mutex<Option<Update>>);
struct PendingManualUpdate(Mutex<Option<ManualUpdate>>);

#[derive(Clone)]
struct ManualUpdate {
    version: String,
    url: String,
    signature: String,
}

#[derive(Deserialize)]
struct UpdateManifest {
    version: String,
    platforms: HashMap<String, UpdatePlatform>,
}

#[derive(Deserialize)]
struct UpdatePlatform {
    url: String,
    signature: String,
}

fn describe_error(context: &str, error: impl std::fmt::Display) -> String {
    format!("{context}: {error}")
}

fn verify_update_signature(data: &[u8], encoded_signature: &str) -> Result<(), String> {
    let decode = |value: &str| -> Result<String, String> {
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(value)
            .map_err(|error| describe_error("更新签名 Base64 无效", error))?;
        String::from_utf8(bytes).map_err(|error| describe_error("更新签名文本无效", error))
    };
    let public_key = PublicKey::decode(&decode(UPDATE_PUBLIC_KEY)?)
        .map_err(|error| describe_error("更新公钥无效", error))?;
    let signature = Signature::decode(&decode(encoded_signature)?)
        .map_err(|error| describe_error("更新包签名无效", error))?;
    public_key
        .verify(data, &signature, true)
        .map_err(|error| describe_error("更新包签名校验失败", error))
}

async fn download_url_with_windows(url: String) -> Result<Vec<u8>, String> {
    let bytes = tauri::async_runtime::spawn_blocking(move || {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codeinsight-update-{}-{nonce}.bin",
            std::process::id()
        ));
        let path_string = path.to_string_lossy().into_owned();
        let script = "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri $env:CODEINSIGHT_UPDATE_URL -OutFile $env:CODEINSIGHT_UPDATE_PATH";
        let output = Command::new("powershell.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
            .arg(script)
            .env("CODEINSIGHT_UPDATE_URL", &url)
            .env("CODEINSIGHT_UPDATE_PATH", &path_string)
            .output()
            .map_err(|error| describe_error("无法启动 Windows 下载服务", error))?;
        if !output.status.success() {
            let detail = String::from_utf8_lossy(&output.stderr);
            let _ = std::fs::remove_file(&path);
            return Err(format!("Windows 下载服务失败: {}", detail.trim()));
        }
        let result = std::fs::read(&path)
            .map_err(|error| describe_error("无法读取已下载更新包", error));
        let _ = std::fs::remove_file(&path);
        result
    })
    .await
    .map_err(|error| describe_error("Windows 下载任务异常", error))??;
    Ok(bytes)
}

async fn download_with_windows(update: &Update) -> Result<Vec<u8>, String> {
    let bytes = download_url_with_windows(update.download_url.to_string()).await?;
    verify_update_signature(&bytes, &update.signature)?;
    Ok(bytes)
}

async fn check_with_windows(app: &tauri::AppHandle) -> Result<Option<ManualUpdate>, String> {
    let bytes = download_url_with_windows(UPDATE_MANIFEST_URL.to_string()).await?;
    let manifest: UpdateManifest = serde_json::from_slice(&bytes)
        .map_err(|error| describe_error("Windows 更新清单无效", error))?;
    let current = semver::Version::parse(&app.package_info().version.to_string())
        .map_err(|error| describe_error("当前版本号无效", error))?;
    let announced = semver::Version::parse(manifest.version.trim_start_matches('v'))
        .map_err(|error| describe_error("更新版本号无效", error))?;
    if announced <= current {
        return Ok(None);
    }
    let version = manifest.version.clone();
    let platform = manifest
        .platforms
        .get("windows-x86_64")
        .ok_or_else(|| "更新清单缺少 windows-x86_64 安装包".to_string())?;
    Ok(Some(ManualUpdate {
        version,
        url: platform.url.clone(),
        signature: platform.signature.clone(),
    }))
}

async fn install_with_windows(update: ManualUpdate) -> Result<String, String> {
    let bytes = download_url_with_windows(update.url).await?;
    verify_update_signature(&bytes, &update.signature)?;
    let version = update.version;
    tauri::async_runtime::spawn_blocking(move || {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codeinsight-update-{}-{nonce}.msi",
            std::process::id()
        ));
        std::fs::write(&path, bytes)
            .map_err(|error| describe_error("无法保存 Windows 更新包", error))?;
        let output = Command::new("msiexec.exe")
            .arg("/i")
            .arg(&path)
            .args(["/passive", "/norestart"])
            .output()
            .map_err(|error| describe_error("无法启动 Windows 安装程序", error))?;
        let _ = std::fs::remove_file(&path);
        if !output.status.success() {
            return Err(format!(
                "Windows 安装程序失败，退出码 {}",
                output.status.code().unwrap_or(-1)
            ));
        }
        Ok(())
    })
    .await
    .map_err(|error| describe_error("Windows 安装任务异常", error))??;
    Ok(version)
}

#[tauri::command]
async fn check_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let mut last_error = None;
    for attempt in 1..=UPDATE_ATTEMPTS {
        let updater = app
            .updater_builder()
            .timeout(UPDATE_TIMEOUT)
            .build()
            .map_err(|error| describe_error("更新组件初始化失败", error))?;
        match updater.check().await {
            Ok(update) => {
                let version = update
                    .as_ref()
                    .map(|release| release.version.to_string());
                {
                    let state = app.state::<PendingUpdate>();
                    let mut pending = state
                        .0
                        .lock()
                        .map_err(|_| "无法保存待安装更新状态".to_string())?;
                    *pending = update;
                }
                if let Ok(mut pending) = app.state::<PendingManualUpdate>().0.lock() {
                    *pending = None;
                }
                return Ok(version);
            }
            Err(error) => last_error = Some(error),
        }
        if attempt < UPDATE_ATTEMPTS {
            let _ = tauri::async_runtime::spawn_blocking(|| {
                std::thread::sleep(UPDATE_RETRY_DELAY);
            })
            .await;
        }
    }
    let builtin_error = last_error.map(|error| error.to_string());
    match check_with_windows(&app).await {
        Ok(Some(update)) => {
            let version = update.version.clone();
            let state = app.state::<PendingManualUpdate>();
            let mut pending = state
                .0
                .lock()
                .map_err(|_| "无法保存 Windows 待安装更新状态".to_string())?;
            *pending = Some(update);
            Ok(Some(version))
        }
        Ok(None) => Ok(None),
        Err(fallback_error) => Err(format!(
            "内置更新检查失败：{}；Windows 备用检查失败：{fallback_error}",
            builtin_error.unwrap_or_else(|| "未知错误".to_string())
        )),
    }
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let manual_update = {
        let state = app.state::<PendingManualUpdate>();
        let mut pending = state
            .0
            .lock()
            .map_err(|_| "无法读取 Windows 待安装更新状态".to_string())?;
        pending.take()
    };
    if let Some(update) = manual_update {
        return install_with_windows(update).await.map(Some);
    }

    let mut update = {
        let state = app.state::<PendingUpdate>();
        let mut pending = state
            .0
            .lock()
            .map_err(|_| "无法读取待安装更新状态".to_string())?;
        pending.take()
    };
    let mut last_error = None;
    if update.is_none() {
        for attempt in 1..=UPDATE_ATTEMPTS {
            let updater = app
                .updater_builder()
                .timeout(UPDATE_TIMEOUT)
                .build()
                .map_err(|error| describe_error("更新组件初始化失败", error))?;
            match updater.check().await {
                Ok(result) => {
                    update = result;
                    break;
                }
                Err(error) => last_error = Some(error),
            }
            if attempt < UPDATE_ATTEMPTS {
                let _ = tauri::async_runtime::spawn_blocking(|| {
                    std::thread::sleep(UPDATE_RETRY_DELAY);
                })
                .await;
            }
        }
    }
    let Some(update) = update else {
        match check_with_windows(&app).await {
            Ok(Some(manual_update)) => {
                return install_with_windows(manual_update).await.map(Some);
            }
            Ok(None) => return Ok(None),
            Err(fallback_error) => {
                if let Some(error) = last_error {
                    return Err(format!(
                        "内置更新检查失败：{error}；Windows 备用检查失败：{fallback_error}"
                    ));
                }
                return Err(fallback_error);
            }
        }
    };
    let version = update.version.to_string();
    let bytes = match tokio::time::timeout(
        UPDATE_TIMEOUT,
        update.download(|_, _| {}, || {}),
    )
    .await
    {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(_)) | Err(_) => download_with_windows(&update).await?,
    };
    update
        .install(bytes)
        .map_err(|error| describe_error("更新包安装失败", error))?;
    Ok(Some(version))
}

#[tauri::command]
fn open_manual_update(app: tauri::AppHandle) -> Result<(), String> {
    app.opener()
        .open_url(RELEASES_URL, None::<&str>)
        .map_err(|error| describe_error("无法打开下载页面", error))
}

#[tauri::command]
fn restart_app(app: tauri::AppHandle) {
    app.request_restart();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            check_update,
            install_update,
            open_manual_update,
            restart_app
        ])
        .setup(|app| {
            app.manage(PendingUpdate(Mutex::new(None)));
            app.manage(PendingManualUpdate(Mutex::new(None)));
            let process = match app
                .shell()
                .sidecar("codeinsight-daemon")
                .and_then(|command| command.spawn())
            {
                Ok((_events, child)) => Some(child),
                Err(error) => {
                    eprintln!("analysis sidecar failed to start: {error}");
                    None
                }
            };
            app.manage(DaemonProcess(Mutex::new(process)));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build CodeInsight-AI desktop");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Ok(mut process) = app_handle.state::<DaemonProcess>().0.lock() {
                if let Some(child) = process.take() {
                    let _ = child.kill();
                }
            }
        }
    });
}

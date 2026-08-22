use base64::Engine;
use minisign_verify::{PublicKey, Signature};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{Emitter, Manager};
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::{Update, UpdaterExt};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

const RELEASES_URL: &str = "https://github.com/Kea128/HW_CodeInsight-AI/releases/latest";
const UPDATE_RELEASE_API_URL: &str =
    "https://api.github.com/repos/Kea128/HW_CodeInsight-AI/releases/latest";
const UPDATE_PUBLIC_KEY: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEI5RkM2RUU5Mzc4MkRCOQpSV1M1TFhpVDdzYWZDOGxXczNuWTB3WjB6R0tWb1pmWnF3RXAwcnZCVFY1NFBjV2hORE5mYnhwNAo=";
const UPDATE_CHECK_TIMEOUT: Duration = Duration::from_secs(8);
const UPDATE_DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(10 * 60);
const UPDATE_CACHE_TTL: Duration = Duration::from_secs(10 * 60);

struct DaemonProcess(Mutex<Option<CommandChild>>);
struct DesktopSessionToken(String);
struct PendingUpdate(Mutex<Option<Update>>);
struct PendingManualUpdate(Mutex<Option<ManualUpdate>>);
struct UpdateCheckCache(Mutex<Option<(Instant, Option<String>)>>);
struct UpdateControl(AtomicBool);

#[cfg(target_os = "windows")]
fn generate_desktop_token() -> String {
    #[link(name = "bcrypt")]
    unsafe extern "system" {
        fn BCryptGenRandom(
            algorithm: *mut std::ffi::c_void,
            buffer: *mut u8,
            length: u32,
            flags: u32,
        ) -> i32;
    }
    const BCRYPT_USE_SYSTEM_PREFERRED_RNG: u32 = 0x00000002;
    let mut bytes = [0_u8; 32];
    let status = unsafe {
        BCryptGenRandom(
            std::ptr::null_mut(),
            bytes.as_mut_ptr(),
            bytes.len() as u32,
            BCRYPT_USE_SYSTEM_PREFERRED_RNG,
        )
    };
    if status < 0 {
        panic!("Windows random number generation failed: {status}");
    }
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
}

#[cfg(not(target_os = "windows"))]
fn generate_desktop_token() -> String {
    let entropy = format!(
        "{}:{}:{:?}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos(),
        std::thread::current().id()
    );
    base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(entropy)
}

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

#[derive(Deserialize)]
struct GithubRelease {
    assets: Vec<GithubAsset>,
}

#[derive(Deserialize)]
struct GithubAsset {
    name: String,
    url: String,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateProgress {
    phase: String,
    downloaded: u64,
    total: Option<u64>,
    percent: Option<u8>,
    can_cancel: bool,
    message: String,
}

fn emit_update_progress(
    app: &tauri::AppHandle,
    phase: &str,
    downloaded: u64,
    total: Option<u64>,
    can_cancel: bool,
    message: &str,
) {
    let percent = total
        .filter(|value| *value > 0)
        .map(|value| ((downloaded.saturating_mul(100) / value).min(100)) as u8);
    let _ = app.emit(
        "update-progress",
        UpdateProgress {
            phase: phase.to_string(),
            downloaded,
            total,
            percent,
            can_cancel,
            message: message.to_string(),
        },
    );
}

fn stop_daemon(app: &tauri::AppHandle) {
    if let Ok(mut process) = app.state::<DaemonProcess>().0.lock() {
        if let Some(child) = process.take() {
            let _ = child.kill();
        }
    }
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

async fn download_url_with_windows(
    app: tauri::AppHandle,
    url: String,
    report_progress: bool,
) -> Result<Vec<u8>, String> {
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
        let _ = std::fs::remove_file(&path);
        let script = "$ErrorActionPreference='Stop';$job=Start-BitsTransfer -Source $env:CODEINSIGHT_UPDATE_URL -Destination $env:CODEINSIGHT_UPDATE_PATH -Asynchronous;try{while($job.JobState -in @('Queued','Connecting','Transferring')){$job=Get-BitsTransfer -JobId $job.JobId;if($job.BytesTotal -gt 0){Write-Output \"PROGRESS $($job.BytesTransferred) $($job.BytesTotal)\"};Start-Sleep -Milliseconds 300};if($job.JobState -ne 'Transferred'){throw \"BITS download failed: $($job.JobState)\"};Complete-BitsTransfer -BitsJob $job}catch{if($job){Remove-BitsTransfer -BitsJob $job -Confirm:$false -ErrorAction SilentlyContinue};throw}";
        let mut child = Command::new("powershell.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
            .arg(script)
            .env("CODEINSIGHT_UPDATE_URL", &url)
            .env("CODEINSIGHT_UPDATE_PATH", &path_string)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|error| describe_error("无法启动 Windows 下载服务", error))?;
        if let Some(stdout) = child.stdout.take() {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if app.state::<UpdateControl>().0.load(Ordering::Relaxed) {
                    let _ = child.kill();
                    let _ = std::fs::remove_file(&path);
                    return Err("更新下载已取消".to_string());
                }
                if report_progress {
                    let values: Vec<_> = line.split_whitespace().collect();
                    if values.len() == 3 && values[0] == "PROGRESS" {
                        if let (Ok(downloaded), Ok(total)) =
                            (values[1].parse::<u64>(), values[2].parse::<u64>())
                        {
                            emit_update_progress(
                                &app,
                                "downloading",
                                downloaded,
                                Some(total),
                                true,
                                "正在通过 Windows 后台下载更新…",
                            );
                        }
                    }
                }
            }
        }
        let output = child
            .wait_with_output()
            .map_err(|error| describe_error("Windows 下载服务异常", error))?;
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

async fn download_github_asset_with_windows(
    url: String,
    accept: &'static str,
) -> Result<Vec<u8>, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codeinsight-manifest-{}-{nonce}.json",
            std::process::id()
        ));
        let script = "$ProgressPreference='SilentlyContinue';Invoke-WebRequest -UseBasicParsing -TimeoutSec 12 -Headers @{Accept=$env:CODEINSIGHT_ACCEPT;'User-Agent'='CodeInsight-AI'} -Uri $env:CODEINSIGHT_UPDATE_URL -OutFile $env:CODEINSIGHT_UPDATE_PATH";
        let output = Command::new("powershell.exe")
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
            .arg(script)
            .env("CODEINSIGHT_UPDATE_URL", url)
            .env("CODEINSIGHT_ACCEPT", accept)
            .env(
                "CODEINSIGHT_UPDATE_PATH",
                path.to_string_lossy().into_owned(),
            )
            .output()
            .map_err(|error| describe_error("无法启动 GitHub API 更新检查", error))?;
        if !output.status.success() {
            let detail = String::from_utf8_lossy(&output.stderr);
            let _ = std::fs::remove_file(&path);
            return Err(format!("GitHub API 更新检查失败: {}", detail.trim()));
        }
        let result =
            std::fs::read(&path).map_err(|error| describe_error("无法读取更新清单", error));
        let _ = std::fs::remove_file(&path);
        result
    })
    .await
    .map_err(|error| describe_error("GitHub API 更新任务异常", error))?
}

async fn download_with_windows(
    app: tauri::AppHandle,
    update: &Update,
) -> Result<Vec<u8>, String> {
    let bytes =
        download_url_with_windows(app, update.download_url.to_string(), true).await?;
    verify_update_signature(&bytes, &update.signature)?;
    Ok(bytes)
}

async fn check_with_windows(app: &tauri::AppHandle) -> Result<Option<ManualUpdate>, String> {
    let release_bytes =
        download_github_asset_with_windows(
            UPDATE_RELEASE_API_URL.to_string(),
            "application/vnd.github+json",
        )
        .await?;
    let release: GithubRelease = serde_json::from_slice(&release_bytes)
        .map_err(|error| describe_error("GitHub Release 响应无效", error))?;
    let manifest_asset = release
        .assets
        .into_iter()
        .find(|asset| asset.name == "latest.json")
        .ok_or_else(|| "最新 Release 缺少更新清单".to_string())?;
    let bytes =
        download_github_asset_with_windows(manifest_asset.url, "application/octet-stream")
            .await?;
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

async fn install_with_windows(
    app: tauri::AppHandle,
    update: ManualUpdate,
) -> Result<String, String> {
    emit_update_progress(
        &app,
        "downloading",
        0,
        None,
        true,
        "正在下载更新…",
    );
    let bytes = download_url_with_windows(app.clone(), update.url, true).await?;
    verify_update_signature(&bytes, &update.signature)?;
    let version = update.version;
    emit_update_progress(
        &app,
        "installing",
        bytes.len() as u64,
        Some(bytes.len() as u64),
        false,
        "正在安装更新并准备重启…",
    );
    stop_daemon(&app);
    let executable =
        std::env::current_exe().map_err(|error| describe_error("无法定位当前应用", error))?;
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
        let script = "$ErrorActionPreference='Stop';while(Get-Process -Id $env:CODEINSIGHT_PARENT_PID -ErrorAction SilentlyContinue){Start-Sleep -Milliseconds 200};$arguments=@('/i',('\"'+$env:CODEINSIGHT_UPDATE_PATH+'\"'),'/passive','/norestart');$installer=Start-Process msiexec.exe -ArgumentList $arguments -Wait -PassThru;if($installer.ExitCode -notin @(0,3010)){exit $installer.ExitCode};Remove-Item -LiteralPath $env:CODEINSIGHT_UPDATE_PATH -Force -ErrorAction SilentlyContinue;Start-Process -FilePath $env:CODEINSIGHT_APP_PATH";
        let mut command = Command::new("powershell.exe");
        command
            .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command"])
            .arg(script)
            .env("CODEINSIGHT_PARENT_PID", std::process::id().to_string())
            .env("CODEINSIGHT_UPDATE_PATH", path.to_string_lossy().into_owned())
            .env("CODEINSIGHT_APP_PATH", executable);
        #[cfg(target_os = "windows")]
        {
            command.creation_flags(0x08000000);
        }
        command
            .spawn()
            .map_err(|error| describe_error("无法启动 Windows 安装程序", error))?;
        Ok::<(), String>(())
    })
    .await
    .map_err(|error| describe_error("Windows 安装任务异常", error))??;
    let exit_app = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(Duration::from_millis(600)).await;
        exit_app.exit(0);
    });
    Ok(version)
}

#[tauri::command]
async fn check_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
    if let Ok(cache) = app.state::<UpdateCheckCache>().0.lock() {
        if let Some((checked_at, version)) = cache.as_ref() {
            if checked_at.elapsed() < UPDATE_CACHE_TTL {
                return Ok(version.clone());
            }
        }
    }
    emit_update_progress(&app, "checking", 0, None, false, "正在快速检查更新…");
    let updater = app
        .updater_builder()
        .timeout(UPDATE_CHECK_TIMEOUT)
        .build()
        .map_err(|error| describe_error("更新组件初始化失败", error))?;
    let builtin_result = updater.check().await;
    let result = match builtin_result {
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
            Ok(version)
        }
        Err(builtin_error) => match check_with_windows(&app).await {
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
                "内置更新检查失败：{builtin_error}；Windows 备用检查失败：{fallback_error}"
            )),
        },
    };
    if let Ok(version) = &result {
        if let Ok(mut cache) = app.state::<UpdateCheckCache>().0.lock() {
            *cache = Some((Instant::now(), version.clone()));
        }
    }
    emit_update_progress(&app, "idle", 0, None, false, "");
    result
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
    app.state::<UpdateControl>()
        .0
        .store(false, Ordering::Relaxed);
    let manual_update = {
        let state = app.state::<PendingManualUpdate>();
        let mut pending = state
            .0
            .lock()
            .map_err(|_| "无法读取 Windows 待安装更新状态".to_string())?;
        pending.take()
    };
    if let Some(update) = manual_update {
        return install_with_windows(app, update).await.map(Some);
    }

    let mut update = {
        let state = app.state::<PendingUpdate>();
        let mut pending = state
            .0
            .lock()
            .map_err(|_| "无法读取待安装更新状态".to_string())?;
        pending.take()
    };
    if update.is_none() {
        let updater = app
            .updater_builder()
            .timeout(UPDATE_CHECK_TIMEOUT)
            .build()
            .map_err(|error| describe_error("更新组件初始化失败", error))?;
        update = updater.check().await.ok().flatten();
    }
    let Some(update) = update else {
        match check_with_windows(&app).await {
            Ok(Some(manual_update)) => {
                return install_with_windows(app, manual_update).await.map(Some);
            }
            Ok(None) => return Ok(None),
            Err(fallback_error) => return Err(fallback_error),
        }
    };
    let version = update.version.to_string();
    let progress_app = app.clone();
    let mut downloaded = 0_u64;
    let bytes = match tokio::time::timeout(
        UPDATE_DOWNLOAD_TIMEOUT,
        update.download(
            move |chunk_length, content_length| {
                downloaded = downloaded.saturating_add(chunk_length as u64);
                emit_update_progress(
                    &progress_app,
                    "downloading",
                    downloaded,
                    content_length,
                    true,
                    "正在下载更新…",
                );
            },
            || {},
        ),
    )
    .await
    {
        Ok(Ok(bytes)) => bytes,
        Ok(Err(_)) | Err(_) => download_with_windows(app.clone(), &update).await?,
    };
    if app.state::<UpdateControl>().0.load(Ordering::Relaxed) {
        return Err("更新下载已取消".to_string());
    }
    emit_update_progress(
        &app,
        "installing",
        bytes.len() as u64,
        Some(bytes.len() as u64),
        false,
        "下载完成，正在安装并自动重启…",
    );
    stop_daemon(&app);
    update
        .install(bytes)
        .map_err(|error| describe_error("更新包安装失败", error))?;
    Ok(Some(version))
}

#[tauri::command]
fn cancel_update(app: tauri::AppHandle) {
    app.state::<UpdateControl>()
        .0
        .store(true, Ordering::Relaxed);
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

#[tauri::command]
fn desktop_session_token(token: tauri::State<DesktopSessionToken>) -> String {
    token.0.clone()
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
            cancel_update,
            open_manual_update,
            restart_app,
            desktop_session_token
        ])
        .setup(|app| {
            app.manage(PendingUpdate(Mutex::new(None)));
            app.manage(PendingManualUpdate(Mutex::new(None)));
            app.manage(UpdateCheckCache(Mutex::new(None)));
            app.manage(UpdateControl(AtomicBool::new(false)));
            let desktop_token = generate_desktop_token();
            let process = match app
                .shell()
                .sidecar("codeinsight-daemon")
                .map(|command| {
                    command.env("CODEINSIGHT_DESKTOP_TOKEN", desktop_token.clone())
                })
                .and_then(|command| command.spawn())
            {
                Ok((_events, child)) => Some(child),
                Err(error) => {
                    eprintln!("analysis sidecar failed to start: {error}");
                    None
                }
            };
            app.manage(DesktopSessionToken(desktop_token));
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

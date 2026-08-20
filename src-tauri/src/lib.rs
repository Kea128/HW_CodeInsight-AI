use std::sync::Mutex;
use std::time::Duration;

use tauri::Manager;
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::{Update, UpdaterExt};

const RELEASES_URL: &str = "https://github.com/Kea128/HW_CodeInsight-AI/releases/latest";
const UPDATE_ATTEMPTS: usize = 3;
const UPDATE_RETRY_DELAY: Duration = Duration::from_secs(2);
const UPDATE_TIMEOUT: Duration = Duration::from_secs(30);

struct DaemonProcess(Mutex<Option<CommandChild>>);
struct PendingUpdate(Mutex<Option<Update>>);

fn describe_error(context: &str, error: impl std::fmt::Display) -> String {
    format!("{context}: {error}")
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
    match last_error {
        Some(error) => Err(describe_error(
            "重试 3 次后仍无法连接更新服务器",
            error,
        )),
        None => Ok(None),
    }
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
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
        if let Some(error) = last_error {
            return Err(describe_error(
                "重试 3 次后仍无法连接更新服务器",
                error,
            ));
        }
        return Ok(None);
    };
    let version = update.version.to_string();
    let mut download_error = None;
    for attempt in 1..=UPDATE_ATTEMPTS {
        match update.download(|_, _| {}, || {}).await {
            Ok(bytes) => {
                update
                    .install(bytes)
                    .map_err(|error| describe_error("更新包安装失败", error))?;
                return Ok(Some(version));
            }
            Err(error) => download_error = Some(error),
        }
        if attempt < UPDATE_ATTEMPTS {
            let _ = tauri::async_runtime::spawn_blocking(|| {
                std::thread::sleep(UPDATE_RETRY_DELAY);
            })
            .await;
        }
    }
    match download_error {
        Some(error) => Err(describe_error(
            "重试 3 次后仍无法下载更新包",
            error,
        )),
        None => Err("更新包下载未启动".to_string()),
    }
}

#[tauri::command]
fn open_manual_update(app: tauri::AppHandle) -> Result<(), String> {
    app.opener()
        .open_url(RELEASES_URL, None::<&str>)
        .map_err(|error| describe_error("无法打开下载页面", error))
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
            open_manual_update
        ])
        .setup(|app| {
            app.manage(PendingUpdate(Mutex::new(None)));
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

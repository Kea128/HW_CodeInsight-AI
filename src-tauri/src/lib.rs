use std::sync::Mutex;

use tauri::Manager;
use tauri_plugin_opener::OpenerExt;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

const RELEASES_URL: &str = "https://github.com/Kea128/HW_CodeInsight-AI/releases/latest";

struct DaemonProcess(Mutex<Option<CommandChild>>);

fn describe_error(context: &str, error: impl std::fmt::Display) -> String {
    format!("{context}: {error}")
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let updater = app
        .updater()
        .map_err(|error| describe_error("更新组件初始化失败", error))?;
    let Some(update) = updater
        .check()
        .await
        .map_err(|error| describe_error("无法连接更新服务器", error))?
    else {
        return Ok(None);
    };
    let version = update.version.to_string();
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| describe_error("更新包下载或安装失败", error))?;
    Ok(Some(version))
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
        .invoke_handler(tauri::generate_handler![install_update, open_manual_update])
        .setup(|app| {
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

use std::sync::Mutex;

use tauri::Manager;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

struct DaemonProcess(Mutex<Option<CommandChild>>);

#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let updater = app.updater().map_err(|error| error.to_string())?;
    let Some(update) = updater
        .check()
        .await
        .map_err(|error| error.to_string())?
    else {
        return Ok(None);
    };
    let version = update.version.to_string();
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|error| error.to_string())?;
    Ok(Some(version))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![install_update])
        .setup(|app| {
            let command = app.shell().sidecar("codeinsight-daemon")?;
            let (_events, child) = command.spawn()?;
            app.manage(DaemonProcess(Mutex::new(Some(child))));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build CodeInsight-AI desktop");

    app.run(|app_handle, event| {
        if let tauri::RunEvent::Exit = event {
            if let Ok(mut process) = app_handle.state::<DaemonProcess>().0.lock() {
                if let Some(child) = process.as_mut() {
                    let _ = child.kill();
                }
            }
        }
    });
}

mod process_tree;
mod sidecar;

use std::{sync::mpsc, time::Duration};

use sidecar::{observe_sidecar, wait_for_loopback, SidecarState, StartupError, STARTUP_TIMEOUT};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};
use tauri_plugin_shell::ShellExt;

fn stop_owned_sidecar(app: &tauri::AppHandle) {
    app.state::<SidecarState>().stop();
}

fn show_recovery_window<R: tauri::Runtime>(
    app: &mut tauri::App<R>,
) -> Result<(), Box<dyn std::error::Error>> {
    eprintln!("OPC_DESKTOP_ERROR: STARTUP_FAILED");
    WebviewWindowBuilder::new(app, "main", WebviewUrl::App("error.html".into()))
        .title("OPC App — 启动失败")
        .inner_size(720.0, 480.0)
        .min_inner_size(560.0, 400.0)
        .center()
        .build()?;
    Ok(())
}

pub fn run() {
    let builder = tauri::Builder::default()
        // Single-instance must be registered first so a second process cannot
        // race sidecar startup against the owning desktop process.
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .manage(SidecarState::new())
        .setup(|app| {
            let command = match app.shell().sidecar("opc-sidecar") {
                Ok(command) => command.set_raw_out(true).args([
                    "--no-open",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                ]),
                Err(_) => return show_recovery_window(app),
            };
            let (events, child) = match command.spawn() {
                Ok(spawned) => spawned,
                Err(_) => return show_recovery_window(app),
            };
            if app.state::<SidecarState>().set(child).is_err() {
                return show_recovery_window(app);
            }

            let (startup_tx, startup_rx) = mpsc::sync_channel(1);
            observe_sidecar(app.handle().clone(), events, startup_tx);
            let ready = (|| -> Result<_, Box<dyn std::error::Error>> {
                let startup = startup_rx
                    .recv_timeout(STARTUP_TIMEOUT)
                    .map_err(|_| StartupError::Timeout)?;
                let (url, port) = startup?;
                wait_for_loopback(port, Duration::from_secs(2))?;
                let parsed_url = url.parse()?;
                let window =
                    WebviewWindowBuilder::new(app, "main", WebviewUrl::External(parsed_url))
                        .title("OPC App")
                        .inner_size(1180.0, 760.0)
                        .min_inner_size(720.0, 560.0)
                        .center()
                        .build()?;
                Ok(window)
            })();
            if ready.is_err() {
                app.state::<SidecarState>().stop();
                show_recovery_window(app)?;
            }
            Ok(())
        });

    let app = builder
        .build(tauri::generate_context!())
        .expect("failed to build OPC desktop runtime");
    app.run(|app, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            stop_owned_sidecar(app);
        }
    });
}

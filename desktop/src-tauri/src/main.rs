// Prevents additional console window on Windows in release
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::net::TcpStream;
use std::path::PathBuf;
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant};
use tauri::{Manager, RunEvent};

/// Path to the supervisor module at compile time.
/// CARGO_MANIFEST_DIR = <repo>/desktop/src-tauri  →  ../../supervisor = <repo>/supervisor
const SUPERVISOR_DIR: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/../../supervisor");
const SUPERVISOR_PORT: u16 = 7000;

// ---------------------------------------------------------------------------
// App state: holds the supervisor child process if WE started it
// ---------------------------------------------------------------------------
struct SupervisorState(Mutex<Option<Child>>);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Ensure supervisor/venv exists; create and pip-install if not.
fn ensure_supervisor_venv(sup_dir: &PathBuf) {
    let python = sup_dir.join("venv/bin/python");
    if python.exists() {
        return;
    }
    eprintln!("[Seedling] Supervisor venv missing — provisioning...");
    let _ = Command::new("python3")
        .args(["-m", "venv", "venv"])
        .current_dir(sup_dir)
        .output();
    let _ = Command::new(sup_dir.join("venv/bin/pip"))
        .args(["install", "-r", "requirements.txt", "-q"])
        .current_dir(sup_dir)
        .output();
    eprintln!("[Seedling] Supervisor venv ready.");
}

/// Spawn the supervisor uvicorn process.
fn launch_supervisor() -> Option<Child> {
    let sup_dir = PathBuf::from(SUPERVISOR_DIR);
    ensure_supervisor_venv(&sup_dir);

    let python = sup_dir.join("venv/bin/python");
    eprintln!("[Seedling] Starting supervisor on :{SUPERVISOR_PORT}...");
    Command::new(&python)
        .args([
            "-m", "uvicorn", "main:app",
            "--host", "127.0.0.1",
            "--port", &SUPERVISOR_PORT.to_string(),
            "--log-level", "warning",
        ])
        .current_dir(&sup_dir)
        .spawn()
        .map_err(|e| eprintln!("[Seedling] Failed to spawn supervisor: {e}"))
        .ok()
}

/// Poll a TCP port until it accepts connections or timeout elapses.
fn wait_for_port(port: u16, timeout_secs: u64) -> bool {
    let addr = format!("127.0.0.1:{port}");
    let deadline = Instant::now() + Duration::from_secs(timeout_secs);
    while Instant::now() < deadline {
        if TcpStream::connect(&addr).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_http::init())
        .manage(SupervisorState(Mutex::new(None)))
        .setup(|app| {
            let handle = app.handle().clone();

            // Check if supervisor is already up (developer running it separately)
            let already_running =
                TcpStream::connect(format!("127.0.0.1:{SUPERVISOR_PORT}")).is_ok();

            std::thread::spawn(move || {
                if !already_running {
                    let child = launch_supervisor();
                    // try_state returns Option<State<T>> in Tauri 2
                    if let Some(state) = handle.try_state::<SupervisorState>() {
                        *state.0.lock().unwrap() = child;
                    }
                }

                // Wait up to 60 s for supervisor to be reachable
                if wait_for_port(SUPERVISOR_PORT, 60) {
                    eprintln!("[Seedling] Supervisor ready — navigating.");
                    if let Some(window) = handle.get_webview_window("main") {
                        let _ = window.eval(&format!(
                            "window.location.href='http://127.0.0.1:{SUPERVISOR_PORT}/'"
                        ));
                    }
                } else {
                    eprintln!("[Seedling] Supervisor did not come online within 60 s.");
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error building tauri application")
        .run(|app_handle, event| {
            // Kill supervisor on exit (only if we own the process)
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<SupervisorState>() {
                    let mut guard = state.0.lock().unwrap();
                    if let Some(ref mut child) = *guard {
                        let _ = child.kill();
                        let _ = child.wait();
                        eprintln!("[Seedling] Supervisor stopped.");
                    }
                }
            }
        });
}

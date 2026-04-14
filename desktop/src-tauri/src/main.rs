// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use tauri::{WebviewUrl, WebviewWindowBuilder};

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_http::init())
        .setup(|app| {
            let win = WebviewWindowBuilder::new(
                app,
                "main",
                // The Supervisor web dashboard served on localhost
                WebviewUrl::External("http://127.0.0.1:7000".parse().unwrap()),
            )
            .title("Seedling — ZenCode Supervisor")
            .inner_size(1400.0, 900.0)
            .min_inner_size(900.0, 600.0)
            .build()?;

            // On macOS, bring the window into focus immediately
            #[cfg(target_os = "macos")]
            win.set_focus().ok();

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

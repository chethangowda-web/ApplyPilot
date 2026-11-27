#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use serde::Serialize;

#[derive(Serialize)]
struct BackendConnectionStatus {
    reachable: bool,
    status: String,
}

#[tauri::command]
async fn check_backend_health(backend_url: String) -> BackendConnectionStatus {
    let trimmed = backend_url.trim_end_matches('/');
    let url = format!("{trimmed}/health/ready");
    match reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
    {
        Ok(client) => match client.get(&url).send().await {
            Ok(resp) if resp.status().is_success() => BackendConnectionStatus {
                reachable: true,
                status: "ready".to_string(),
            },
            Ok(resp) => BackendConnectionStatus {
                reachable: false,
                status: format!("http_{}", resp.status().as_u16()),
            },
            Err(_) => BackendConnectionStatus {
                reachable: false,
                status: "unreachable".to_string(),
            },
        },
        Err(_) => BackendConnectionStatus {
            reachable: false,
            status: "client_error".to_string(),
        },
    }
}

fn main() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .invoke_handler(tauri::generate_handler![check_backend_health])
        .run(tauri::generate_context!())
        .expect("error while running ApplyPilot Desktop");
}

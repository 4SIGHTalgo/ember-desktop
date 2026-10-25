#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{atomic::{AtomicBool, AtomicU64, Ordering}, Mutex};
use serde_json::{json, Value};
use tauri::{Emitter, Manager, State};
use tauri_plugin_shell::{process::{CommandChild, CommandEvent}, ShellExt};
use tauri_plugin_updater::{Update, UpdaterExt};

#[derive(Default)]
struct RunIntent {
    requested: bool,
    max_bankroll_risk_percent: f64,
    conservative_level: String,
    kelly_multiplier: f64,
}

#[derive(Default)]
struct Worker {
    child: Mutex<Option<CommandChild>>,
    token: Mutex<Option<String>>,
    token_generation: AtomicU64,
    api: Mutex<String>,
    running: AtomicBool,
    pending: Mutex<Option<Update>>,
    ready: AtomicBool,
    quitting: AtomicBool,
    intent: Mutex<RunIntent>,
}

#[cfg(target_os = "windows")]
fn notification_sound() -> bool {
    #[link(name = "kernel32")]
    extern "system" { fn Beep(frequency: u32, duration: u32) -> i32; }
    for frequency in [880, 660, 880] {
        if unsafe { Beep(frequency, 150) } == 0 { return false; }
        std::thread::sleep(std::time::Duration::from_millis(100));
    }
    true
}

#[cfg(target_os = "macos")]
fn notification_sound() -> bool {
    std::process::Command::new("/usr/bin/osascript").args(["-e", "beep 3"])
        .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null())
        .status().map(|status| status.success()).unwrap_or(false)
}

#[cfg(not(any(target_os = "windows", target_os = "macos")))]
fn notification_sound() -> bool { false }

fn audible_alert(app: tauri::AppHandle, value: Value) {
    let _ = app.emit("worker", value);
    tauri::async_runtime::spawn_blocking(move || {
        let played = notification_sound();
        let _ = app.emit("worker", json!({"kind":"alert_sound", "played":played}));
    });
}

#[tauri::command]
async fn test_alert_sound() -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(notification_sound).await.map_err(|_| "Sound system unavailable".into())
}

fn write(worker: &Worker, message: Value) -> Result<(), String> {
    let mut children = worker.child.lock().map_err(|_| "Worker unavailable")?;
    let child = children.as_mut().ok_or("Worker not running")?;
    child.write(format!("{}\n", message).as_bytes()).map_err(|_| "Worker communication failed".into())
}

#[tauri::command]
fn login(worker: State<Worker>, api: String) -> Result<(), String> {
    let url = url::Url::parse(&api).map_err(|_| "Invalid website address")?;
    if url.scheme() != "https" || url.host_str().is_none() || !url.username().is_empty()
        || url.password().is_some() || url.path() != "/" || url.query().is_some() || url.fragment().is_some() {
        return Err("Enter an HTTPS website origin".into());
    }
    if worker.running.load(Ordering::SeqCst) { return Err("Stop before changing accounts".into()); }
    *worker.api.lock().unwrap() = api.trim_end_matches('/').into();
    *worker.token.lock().unwrap() = None;
    write(&worker, json!({"command":"login", "api":api}))
}

#[tauri::command]
fn start(worker: State<Worker>, max_bankroll_risk_percent: f64, conservative_level: String, kelly_multiplier: f64) -> Result<(), String> {
    if !max_bankroll_risk_percent.is_finite() || max_bankroll_risk_percent <= 0.0 || max_bankroll_risk_percent > 100.0 {
        return Err("Max bankroll risk must be greater than 0 and at most 100 percent".into());
    }
    if !["conservative", "very_conservative"].contains(&conservative_level.as_str()) || ![0.25, 0.5].contains(&kelly_multiplier) {
        return Err("Invalid conservative level or Kelly sizing".into());
    }
    if worker.api.lock().unwrap().is_empty() || worker.token.lock().unwrap().is_none() {
        return Err("Sign in to Ember through the browser before pressing Start".into());
    }
    if !worker.ready.load(Ordering::SeqCst) { return Err("Browser worker is reconnecting".into()); }
    let mut intent = worker.intent.lock().unwrap();
    intent.requested = true;
    intent.max_bankroll_risk_percent = max_bankroll_risk_percent;
    intent.conservative_level = conservative_level.clone();
    intent.kelly_multiplier = kelly_multiplier;
    worker.running.store(true, Ordering::SeqCst);
    if let Err(error) = write(&worker, json!({"command":"start", "max_bankroll_risk_percent":max_bankroll_risk_percent,
        "conservative_level":conservative_level, "kelly_multiplier":kelly_multiplier})) {
        intent.requested = false;
        worker.running.store(false, Ordering::SeqCst);
        return Err(error);
    }
    Ok(())
}

#[tauri::command]
fn stop(app: tauri::AppHandle, worker: State<Worker>) -> Result<(), String> {
    // Serialize Stop with automatic resume. A delayed resume cannot be written
    // after Stop using an earlier read of the user's run intent.
    let mut intent = worker.intent.lock().unwrap();
    intent.requested = false;
    if write(&worker, json!({"command":"stop"})).is_err() {
        if let Some(child) = worker.child.lock().unwrap().take() { let _ = child.kill(); }
        worker.running.store(false, Ordering::SeqCst);
        let _ = app.emit("worker", json!({"kind":"stopped", "browser_closed":false}));
    }
    Ok(())
}

#[tauri::command]
fn worker_status(worker: State<Worker>) -> bool { worker.ready.load(Ordering::SeqCst) }

#[tauri::command]
fn missed_history(worker: State<Worker>, filter: Option<String>, offset: Option<u32>) -> Result<(), String> {
    let filter = filter.unwrap_or("missed".into());
    let offset = offset.unwrap_or(0);
    if !["missed", "uncertain", "all"].contains(&filter.as_str()) || offset > 1_000_000 { return Err("Invalid history page".into()); }
    write(&worker, json!({"command":"missed_history", "filter":filter, "offset":offset}))
}

#[tauri::command]
fn sync_missed_history(worker: State<Worker>) -> Result<(), String> {
    write(&worker, json!({"command":"sync_missed_history"}))
}

#[tauri::command]
async fn check_update(app: tauri::AppHandle) -> Result<Value, String> {
    let worker = app.state::<Worker>();
    if worker.api.lock().unwrap().is_empty() {
        return Err("Sign in to Ember through the browser before checking updates".into());
    }
    let generation = worker.token_generation.load(Ordering::SeqCst);
    write(&worker, json!({"command":"update_token"}))?;
    for _ in 0..75 {
        if worker.token_generation.load(Ordering::SeqCst) != generation { break; }
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
    }
    if worker.token_generation.load(Ordering::SeqCst) == generation {
        return Err("Could not refresh update authorization; monitoring is unchanged".into());
    }
    let token = worker.token.lock().unwrap().clone().ok_or("Sign in before checking updates")?;
    let base = worker.api.lock().unwrap().clone();
    let endpoint = url::Url::parse(&format!("{base}/api/v1/updates/{{{{target}}}}/{{{{arch}}}}/{{{{current_version}}}}/")).map_err(|_| "Invalid update endpoint")?;
    let updater = app.updater_builder().endpoints(vec![endpoint]).map_err(|_| "Invalid update endpoint")?
        .header("Authorization", format!("Bearer {token}")).map_err(|_| "Invalid authorization")?
        .build().map_err(|_| "Updater signature configuration is unavailable")?;
    let update = updater.check().await.map_err(|_| "Update check failed")?;
    let result = update.as_ref().map(|u| json!({"available":true,"version":u.version,"notes":u.body})).unwrap_or(json!({"available":false}));
    *worker.pending.lock().unwrap() = update;
    Ok(result)
}

#[tauri::command]
async fn install_update(app: tauri::AppHandle) -> Result<(), String> {
    let worker = app.state::<Worker>();
    if worker.running.load(Ordering::SeqCst) { return Err("Stop and wait for the browser worker before installing updates".into()); }
    let update = worker.pending.lock().unwrap().take().ok_or("Check for an update first")?;
    update.download_and_install(|_, _| {}, || {}).await.map_err(|_| "Signed update installation failed")?;
    app.restart();
}

async fn supervise_worker(handle: tauri::AppHandle) {
    let mut restart = false;
    let mut backoff = 1u64;
    loop {
        if handle.state::<Worker>().quitting.load(Ordering::SeqCst) { break; }
        let launched = std::time::Instant::now();
        let spawned = handle.shell().sidecar("ember-worker").and_then(|command| command.spawn());
        if let Ok((mut receiver, child)) = spawned {
            *handle.state::<Worker>().child.lock().unwrap() = Some(child);
            let mut restoring = restart;
            let mut signed_in = false;
            while let Some(event) = receiver.recv().await {
                match event {
                    CommandEvent::Stdout(line) => {
                        if let Ok(value) = serde_json::from_slice::<Value>(&line) {
                            let worker = handle.state::<Worker>();
                            if value["kind"] == "credential" {
                                *worker.token.lock().unwrap() = value["token"].as_str().map(String::from);
                                worker.token_generation.fetch_add(1, Ordering::SeqCst);
                                continue; // Native memory only, never the WebView.
                            }
                            if value["kind"] == "ready" {
                                worker.ready.store(true, Ordering::SeqCst);
                                if restart && !worker.quitting.load(Ordering::SeqCst) {
                                    let api = worker.api.lock().unwrap().clone();
                                    if !api.is_empty() { let _ = write(&worker, json!({"command":"login", "api":api})); }
                                }
                            }
                            if value["kind"] == "signed_in" { signed_in = true; }
                            if (value["kind"] == "stopped" && value["recoverable"] != true) || (value["kind"] == "alert" && value["code"] == "browser_closed") {
                                worker.running.store(false, Ordering::SeqCst);
                                worker.intent.lock().unwrap().requested = false;
                                restoring = false;
                            }
                            if restoring && signed_in && (value["kind"] == "signed_in" || value["kind"] == "entitlement")
                                && value["enabled"] == true && value["supported"] == true && !worker.quitting.load(Ordering::SeqCst) {
                                let intent = worker.intent.lock().unwrap();
                                if intent.requested && write(&worker, json!({"command":"start",
                                    "max_bankroll_risk_percent":intent.max_bankroll_risk_percent,
                                    "conservative_level":intent.conservative_level,
                                    "kelly_multiplier":intent.kelly_multiplier, "resume":true})).is_ok() {
                                    worker.running.store(true, Ordering::SeqCst);
                                    restoring = false;
                                }
                            }
                            if value["kind"] == "alert" && value["code"] == "browser_closed" && !worker.quitting.load(Ordering::SeqCst) {
                                audible_alert(handle.clone(), value);
                            } else {
                                let _ = handle.emit("worker", value);
                            }
                        }
                    },
                    CommandEvent::Terminated(_) => break,
                    _ => {},
                }
            }
        }
        {
            let worker = handle.state::<Worker>();
            worker.ready.store(false, Ordering::SeqCst);
            *worker.child.lock().unwrap() = None;
            *worker.token.lock().unwrap() = None;
            if worker.quitting.load(Ordering::SeqCst) { break; }
            let one_opportunity = std::env::var("EMBER_DEVICE_TEST_ONCE").as_deref() == Ok("1")
                && std::env::var("EMBER_DEVICE_TEST_SCAN_ONLY").as_deref() != Ok("1")
                && std::env::var("EMBER_DEVICE_TEST_LOGIN_ONLY").as_deref() != Ok("1");
            if one_opportunity && worker.intent.lock().unwrap().requested {
                // A new process must not reset a controlled live test's
                // one-opportunity limit after an interrupted dispatch.
                worker.intent.lock().unwrap().requested = false;
                worker.running.store(false, Ordering::SeqCst);
                audible_alert(handle.clone(), json!({"kind":"alert", "code":"worker_exited",
                    "message":"The one-opportunity validation worker was interrupted. Check provider history; this test will not restart automatically."}));
                let _ = handle.emit("worker", json!({"kind":"stopped", "device_test_complete":true, "outcome":"worker_interrupted"}));
            }
            let active = {
                let intent = worker.intent.lock().unwrap();
                worker.running.store(intent.requested, Ordering::SeqCst);
                intent.requested
            };
            if !active {
                let _ = handle.emit("worker", json!({"kind":"stopped", "browser_closed":false}));
            }
            let _ = handle.emit("worker", json!({"kind":"browser_recovery", "state":"recovering", "reason":"worker_restart",
                "message": if active { "Browser worker interrupted. Reconnecting the owned browser; wagering is paused and prior submissions will not be retried." }
                           else { "Browser worker interrupted. Reconnecting application controls; betting remains stopped." }}));
        }
        if launched.elapsed().as_secs() >= 60 { backoff = 1; }
        for _ in 0..backoff*5 {
            if handle.state::<Worker>().quitting.load(Ordering::SeqCst) { return; }
            tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        }
        backoff = (backoff*2).min(30);
        restart = true;
    }
}

fn main() {
    let app = tauri::Builder::default()
        .manage(Worker::default())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![login, start, stop, worker_status, missed_history, sync_missed_history, check_update, install_update, test_alert_sound])
        .setup(|app| {
            let handle = app.handle().clone();
            tauri::async_runtime::spawn(supervise_worker(handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Could not initialize Ember");
    app.run(|handle, event| {
        if let tauri::RunEvent::ExitRequested { api, .. } = event {
            let worker = handle.state::<Worker>();
            if !worker.quitting.swap(true, Ordering::SeqCst) {
                worker.intent.lock().unwrap().requested = false;
                api.prevent_exit();
                let _ = write(&worker, json!({"command":"quit"}));
                let app = handle.clone();
                tauri::async_runtime::spawn(async move {
                    for _ in 0..40 {
                        if !app.state::<Worker>().ready.load(Ordering::SeqCst) { break; }
                        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
                    }
                    if let Some(child) = app.state::<Worker>().child.lock().unwrap().take() { let _ = child.kill(); }
                    app.exit(0);
                });
            }
        } else if let tauri::RunEvent::Exit = event {
            if let Some(child) = handle.state::<Worker>().child.lock().unwrap().take() { let _ = child.kill(); }
        }
    });
}

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
//! 外设电量圆环 · Rust 版
//! 常驻进程：托盘图标 + 设备轮询；`--ui` 独立进程：egui 主界面。

mod autostart;
mod config;
mod devices;
mod model;
mod ringicon;
mod tray;
mod ui;

use config::{log_line, Config};
use model::Reading;
use std::sync::{mpsc, Mutex, OnceLock};
use std::time::Duration;
use tray::Tray;
use windows::core::w;
use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, WPARAM};
use windows::Win32::System::LibraryLoader::GetModuleHandleW;
use windows::Win32::System::Threading::CreateMutexW;
use windows::Win32::UI::Input::KeyboardAndMouse::{RegisterHotKey, MOD_ALT, MOD_CONTROL};
use windows::Win32::UI::WindowsAndMessaging::{
    CreateWindowExW, DefWindowProcW, DispatchMessageW, FindWindowW, GetMessageW, PostMessageW,
    PostQuitMessage, RegisterClassExW, RegisterWindowMessageW, TranslateMessage, HICON, HMENU, MSG,
    WINDOW_EX_STYLE, WNDCLASSEXW, WNDPROC, WS_OVERLAPPED,
};

pub const WND_CLASS: windows::core::PCWSTR = w!("PeripheralBatteryRingRsWnd");
const MUTEX_NAME: windows::core::PCWSTR = w!("PeripheralBatteryRingRs_SingleInstance");

const WM_APP_BASE: u32 = 0x8000;
const WM_APP_TRAY: u32 = WM_APP_BASE + 1;
const WM_APP_READINGS: u32 = WM_APP_BASE + 2;
pub const WM_APP_RELOAD: u32 = WM_APP_BASE + 3;
pub const WM_APP_REFRESH: u32 = WM_APP_BASE + 4;
const WM_APP_QUIT: u32 = WM_APP_BASE + 6;
const HOTKEY_ID: i32 = 0xB001;

struct App {
    cfg: Config,
    readings: Vec<Reading>,
    tray: Tray,
    refresh_tx: mpsc::Sender<()>,
    taskbar_created: u32,
}

unsafe impl Send for App {}

static APP: OnceLock<Mutex<App>> = OnceLock::new();

fn app() -> &'static Mutex<App> {
    APP.get().expect("APP not initialized")
}

fn sync_tray(app: &mut App) {
    let App { tray, readings, cfg, .. } = app;
    tray.sync(readings, cfg);
}

fn open_ui() {
    let Some(exe) = std::env::current_exe().ok() else { return };
    let mut cmd = std::process::Command::new(exe);
    cmd.arg("--ui");
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000);
    }
    let _ = cmd.spawn();
}

fn handle_menu(hwnd: HWND) {
    let (tray_on, auto_on) = {
        let app = app().lock().unwrap();
        (app.cfg.show_tray_icon, autostart::is_enabled())
    };
    let cmd = tray::show_menu(hwnd, tray_on, auto_on);
    match cmd {
        tray::CMD_OPEN => open_ui(),
        tray::CMD_REFRESH => {
            if let Ok(app) = app().lock() {
                let _ = app.refresh_tx.send(());
            }
        }
        tray::CMD_TOGGLE_TRAY => {
            let mut app = app().lock().unwrap();
            app.cfg.show_tray_icon = !app.cfg.show_tray_icon;
            app.cfg.save();
            sync_tray(&mut app);
        }
        tray::CMD_TOGGLE_AUTOSTART => {
            let enable = !autostart::is_enabled();
            let ok = autostart::set(enable);
            if let Ok(mut app) = app().lock() {
                if ok {
                    app.cfg.autostart = enable;
                    app.cfg.save();
                }
            }
        }
        tray::CMD_QUIT => unsafe {
            let _ = PostMessageW(hwnd, WM_APP_QUIT, WPARAM(0), LPARAM(0));
        },
        _ => {}
    }
}

unsafe extern "system" fn wndproc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
    match msg {
        WM_APP_TRAY => {
            let event = (lparam.0 as u32) & 0xFFFF;
            match event {
                0x0205 => handle_menu(hwnd),
                0x0202 | 0x0203 => open_ui(),
                _ => {}
            }
            LRESULT(0)
        }
        WM_APP_READINGS => {
            if let Ok(mut app) = app().lock() {
                sync_tray(&mut app);
            }
            LRESULT(0)
        }
        WM_APP_RELOAD => {
            if let Ok(mut app) = app().lock() {
                app.cfg = Config::load();
                sync_tray(&mut app);
                log_line("配置已重载");
            }
            LRESULT(0)
        }
        WM_APP_REFRESH => {
            if let Ok(app) = app().lock() {
                let _ = app.refresh_tx.send(());
            }
            LRESULT(0)
        }
        windows::Win32::UI::WindowsAndMessaging::WM_HOTKEY => {
            if wparam.0 as i32 == HOTKEY_ID {
                open_ui();
            }
            LRESULT(0)
        }
        windows::Win32::UI::WindowsAndMessaging::WM_DESTROY => {
            PostQuitMessage(0);
            LRESULT(0)
        }
        _ => {
            let taskbar_created = APP
                .get()
                .and_then(|m| m.lock().ok().map(|a| a.taskbar_created))
                .unwrap_or(0);
            if taskbar_created != 0 && msg == taskbar_created {
                if let Ok(mut app) = app().lock() {
                    app.tray.readd();
                }
                return LRESULT(0);
            }
            DefWindowProcW(hwnd, msg, wparam, lparam)
        }
    }
}

fn run_daemon() -> Result<(), Box<dyn std::error::Error>> {
    unsafe {
        let mutex = CreateMutexW(None, false, MUTEX_NAME)?;
        if windows::Win32::Foundation::GetLastError()
            == windows::Win32::Foundation::ERROR_ALREADY_EXISTS
        {
            if let Ok(existing) = FindWindowW(WND_CLASS, None) {
                let _ = PostMessageW(existing, WM_APP_RELOAD, WPARAM(0), LPARAM(0));
            }
            let _ = mutex;
            return Ok(());
        }

        let cfg = Config::load();
        let hinst = GetModuleHandleW(None)?;
        let wc = WNDCLASSEXW {
            cbSize: std::mem::size_of::<WNDCLASSEXW>() as u32,
            lpfnWndProc: Some(wndproc),
            hInstance: HINSTANCE(hinst.0),
            lpszClassName: WND_CLASS,
            hIcon: HICON::default(),
            ..Default::default()
        };
        RegisterClassExW(&wc);
        let hwnd = CreateWindowExW(
            WINDOW_EX_STYLE(0),
            WND_CLASS,
            w!("外设电量圆环"),
            WS_OVERLAPPED,
            0,
            0,
            0,
            0,
            HWND::default(),
            HMENU::default(),
            HINSTANCE(hinst.0),
            None,
        )?;

        let taskbar_created = RegisterWindowMessageW(w!("TaskbarCreated"));
        let tray = Tray::new(hwnd, WM_APP_TRAY);
        let (tx, rx) = mpsc::channel::<()>();

        APP.set(Mutex::new(App {
            cfg: cfg.clone(),
            readings: Vec::new(),
            tray,
            refresh_tx: tx,
            taskbar_created,
        }))
        .ok();

        let _ = RegisterHotKey(hwnd, HOTKEY_ID, MOD_CONTROL | MOD_ALT, 'B' as u32);

        std::thread::spawn(move || loop {
            let readings = devices::read_all();
            log_line(format!(
                "读数：{}",
                readings
                    .iter()
                    .map(|r| format!("{}={}", r.name, r.level_text()))
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
            let secs = {
                let mut app = app().lock().unwrap();
                app.readings = readings;
                let hwnd = FindWindowW(WND_CLASS, None).unwrap_or_default();
                let _ = PostMessageW(hwnd, WM_APP_READINGS, WPARAM(0), LPARAM(0));
                app.cfg.refresh_seconds.max(1)
            };
            let _ = rx.recv_timeout(Duration::from_secs(secs));
        });

        {
            let mut app = app().lock().unwrap();
            sync_tray(&mut app);
        }

        let mut msg = MSG::default();
        while GetMessageW(&mut msg, None, 0, 0).as_bool() {
            let _ = TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        if let Ok(mut app) = app().lock() {
            app.tray.remove_all();
        }
    }
    Ok(())
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.iter().any(|a| a == "--ui") {
        return ui::run_ui().map_err(|e| e.into());
    }
    run_daemon()
}

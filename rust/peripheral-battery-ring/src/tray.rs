//! 通知区域图标（每台设备一个圆环）+ 原生右键菜单。

use crate::config::Config;
use crate::model::Reading;
use crate::ringicon::make_tray_icon;
use std::collections::HashMap;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{HWND, LPARAM, POINT, WPARAM};
use windows::Win32::UI::Shell::{
    Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NIM_MODIFY,
    NOTIFYICONDATAW,
};
use windows::Win32::UI::WindowsAndMessaging::{
    AppendMenuW, CreatePopupMenu, DestroyIcon, DestroyMenu, GetCursorPos, PostMessageW,
    SetForegroundWindow, TrackPopupMenu, HICON, MENU_ITEM_FLAGS, MF_CHECKED, MF_SEPARATOR, MF_STRING,
    TPM_NONOTIFY, TPM_RETURNCMD, TPM_RIGHTBUTTON, WM_NULL,
};

pub const CMD_OPEN: usize = 1;
pub const CMD_REFRESH: usize = 2;
pub const CMD_TOGGLE_TRAY: usize = 3;
pub const CMD_TOGGLE_AUTOSTART: usize = 4;
pub const CMD_QUIT: usize = 5;

struct Entry {
    uid: u32,
    icon: HICON,
    tip: String,
    key: (Option<u8>, bool),
    added: bool,
}

pub struct Tray {
    hwnd: HWND,
    msg: u32,
    icons: HashMap<String, Entry>,
    next_uid: u32,
    visible: bool,
}

impl Tray {
    pub fn new(hwnd: HWND, msg: u32) -> Self {
        Self { hwnd, msg, icons: HashMap::new(), next_uid: 10, visible: true }
    }

    fn nid(&self, uid: u32, tip: &str) -> NOTIFYICONDATAW {
        nid_for(self.hwnd, self.msg, uid, tip)
    }

    fn add_icon(&mut self, uid: u32, reading: Option<&Reading>, cfg: &Config) {
        let (level, charging) = reading
            .map(|r| (r.level, r.charging || r.status == "charging"))
            .unwrap_or((None, false));
        let icon = make_tray_icon(level, charging, cfg).unwrap_or_default();
        let tip = reading.map(|r| r.tip()).unwrap_or_else(|| "外设电量圆环：没有发现可读设备".into());
        let mut nid = self.nid(uid, &tip);
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
        nid.hIcon = icon;
        let ok = unsafe { Shell_NotifyIconW(NIM_ADD, &nid) }.as_bool();
        let key = (level, charging);
        if let Some(old) = self.icons.get(&uid.to_string()) {
            let _ = old;
        }
        self.icons.insert(
            uid.to_string(),
            Entry { uid, icon, tip, key, added: ok },
        );
    }

    fn modify_icon(&mut self, uid: u32, reading: Option<&Reading>, cfg: &Config) {
        let key = reading
            .map(|r| (r.level, r.charging || r.status == "charging"))
            .unwrap_or((None, false));
        let tip = reading.map(|r| r.tip()).unwrap_or_else(|| "外设电量圆环：没有发现可读设备".into());
        let hwnd = self.hwnd;
        let msg = self.msg;
        let Some(entry) = self.icons.get_mut(&uid.to_string()) else {
            return;
        };
        if entry.key == key && entry.tip == tip && entry.added {
            return;
        }
        if entry.key != key {
            let icon = make_tray_icon(key.0, key.1, cfg).unwrap_or_default();
            if !entry.icon.is_invalid() {
                unsafe { let _ = DestroyIcon(entry.icon); }
            }
            entry.icon = icon;
        }
        let mut nid = nid_for(hwnd, msg, uid, &tip);
        nid.uFlags = NIF_ICON | NIF_TIP | NIF_MESSAGE;
        nid.hIcon = entry.icon;
        let ok = unsafe { Shell_NotifyIconW(NIM_MODIFY, &nid) }.as_bool();
        entry.key = key;
        entry.tip = tip.clone();
        entry.added = ok;
        let icon = entry.icon;
        if !ok {
            // MODIFY 失败（图标被 explorer 清理）→ 重新 ADD
            let mut add = nid_for(hwnd, msg, uid, &tip);
            add.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
            add.hIcon = icon;
            if let Some(entry) = self.icons.get_mut(&uid.to_string()) {
                entry.added = unsafe { Shell_NotifyIconW(NIM_ADD, &add) }.as_bool();
            }
        }
    }

    fn delete_uid(&mut self, uid: u32) {
        if let Some(entry) = self.icons.remove(&uid.to_string()) {
            let nid = self.nid(entry.uid, "");
            unsafe { let _ = Shell_NotifyIconW(NIM_DELETE, &nid); }
            if !entry.icon.is_invalid() {
                unsafe { let _ = DestroyIcon(entry.icon); }
            }
        }
    }

    pub fn sync(&mut self, readings: &[Reading], cfg: &Config) {
        self.visible = cfg.show_tray_icon;
        if !self.visible {
            let uids: Vec<u32> = self.icons.values().map(|e| e.uid).collect();
            for uid in uids {
                self.delete_uid(uid);
            }
            return;
        }
        let visible: Vec<&Reading> = readings
            .iter()
            .filter(|r| !cfg.is_hidden(&r.device_id))
            .take(8)
            .collect();
        if visible.is_empty() {
            let uid = 1;
            if self.icons.contains_key("1") {
                self.modify_icon(uid, None, cfg);
            } else {
                self.add_icon(uid, None, cfg);
            }
            let stale: Vec<u32> = self
                .icons
                .values()
                .filter(|e| e.uid != uid)
                .map(|e| e.uid)
                .collect();
            for u in stale {
                self.delete_uid(u);
            }
            return;
        }
        let mut keep: Vec<String> = Vec::new();
        for reading in &visible {
            let key = reading.device_id.clone();
            let uid = if let Some(entry) = self.icons.get(&key) {
                entry.uid
            } else {
                let uid = self.next_uid;
                self.next_uid += 1;
                self.add_icon(uid, Some(reading), cfg);
                // 修正 key：add_icon 用 uid 作 key，重命名成 device_id
                if let Some(entry) = self.icons.remove(&uid.to_string()) {
                    self.icons.insert(key.clone(), entry);
                }
                self.icons.get(&key).map(|e| e.uid).unwrap_or(uid)
            };
            self.modify_icon(uid, Some(reading), cfg);
            keep.push(key);
        }
        let stale: Vec<u32> = self
            .icons
            .iter()
            .filter(|(k, _)| !keep.contains(k))
            .map(|(_, e)| e.uid)
            .collect();
        for uid in stale {
            self.delete_uid(uid);
        }
    }

    pub fn readd(&mut self) {
        let hwnd = self.hwnd;
        let msg = self.msg;
        let items: Vec<(u32, HICON, String)> = self
            .icons
            .values()
            .map(|e| (e.uid, e.icon, e.tip.clone()))
            .collect();
        for (uid, icon, tip) in items {
            let mut nid = nid_for(hwnd, msg, uid, &tip);
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP;
            nid.hIcon = icon;
            let ok = unsafe { Shell_NotifyIconW(NIM_ADD, &nid) }.as_bool();
            if let Some(entry) = self.icons.get_mut(&uid.to_string()) {
                entry.added = ok;
            }
        }
    }

    pub fn remove_all(&mut self) {
        let uids: Vec<u32> = self.icons.values().map(|e| e.uid).collect();
        for uid in uids {
            self.delete_uid(uid);
        }
    }
}

fn wide(text: &str) -> Vec<u16> {
    text.encode_utf16().chain(std::iter::once(0)).collect()
}

fn nid_for(hwnd: HWND, msg: u32, uid: u32, tip: &str) -> NOTIFYICONDATAW {
    let mut nid = NOTIFYICONDATAW::default();
    nid.cbSize = std::mem::size_of::<NOTIFYICONDATAW>() as u32;
    nid.hWnd = hwnd;
    nid.uID = uid;
    nid.uCallbackMessage = msg;
    let wide: Vec<u16> = tip.encode_utf16().take(120).collect();
    nid.szTip[..wide.len()].copy_from_slice(&wide);
    nid
}

pub fn show_menu(hwnd: HWND, tray_on: bool, autostart_on: bool) -> usize {
    unsafe {
        let Ok(menu) = CreatePopupMenu() else { return 0 };
        let mut add = |flags: MENU_ITEM_FLAGS, id: usize, text: &str| {
            let w = wide(text);
            let _ = AppendMenuW(menu, flags, id, PCWSTR(w.as_ptr()));
        };
        add(MF_STRING, CMD_OPEN, "打开主界面");
        add(MF_STRING, CMD_REFRESH, "立即刷新");
        add(MF_SEPARATOR, 0, "");
        add(if tray_on { MF_STRING | MF_CHECKED } else { MF_STRING }, CMD_TOGGLE_TRAY, "显示通知区域图标");
        add(if autostart_on { MF_STRING | MF_CHECKED } else { MF_STRING }, CMD_TOGGLE_AUTOSTART, "开机自启");
        add(MF_SEPARATOR, 0, "");
        add(MF_STRING, CMD_QUIT, "退出");

        let mut pt = POINT::default();
        let _ = GetCursorPos(&mut pt);
        let _ = SetForegroundWindow(hwnd);
        let cmd = TrackPopupMenu(
            menu,
            TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY,
            pt.x,
            pt.y,
            0,
            hwnd,
            None,
        );
        let _ = PostMessageW(hwnd, WM_NULL, WPARAM(0), LPARAM(0));
        let _ = DestroyMenu(menu);
        cmd.0 as usize
    }
}

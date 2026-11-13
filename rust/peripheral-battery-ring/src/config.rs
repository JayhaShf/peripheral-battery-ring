//! 配置：程序目录 config.json（兼容旧 %APPDATA% 配置并保留未知字段）。

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::path::PathBuf;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ColorLevel {
    pub min: u8,
    pub color: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Config {
    #[serde(default = "d_ring_size")]
    pub ring_size: u32,
    #[serde(default = "d_refresh")]
    pub refresh_seconds: u64,
    #[serde(default)]
    pub device_hidden: Vec<String>,
    #[serde(default = "d_true")]
    pub show_tray_icon: bool,
    #[serde(default = "d_true")]
    pub animate: bool,
    #[serde(default = "d_colors")]
    pub color_levels: Vec<ColorLevel>,
    #[serde(default = "d_charge_color")]
    pub charging_color: String,
    #[serde(default)]
    pub autostart: bool,
    #[serde(default = "d_theme")]
    pub theme: String,
    #[serde(default = "d_tray_ring")]
    pub taskbar_ring_enabled: bool,
    /// 保留 Python 版本 / 未来版本写入的未知字段，保存时不丢。
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

fn d_ring_size() -> u32 { 30 }
fn d_refresh() -> u64 { 5 }
fn d_true() -> bool { true }
fn d_theme() -> String { "dark".into() }
fn d_charge_color() -> String { "#3b82f6".into() }
fn d_tray_ring() -> bool { false }
fn d_colors() -> Vec<ColorLevel> {
    vec![
        ColorLevel { min: 60, color: "#22c55e".into() },
        ColorLevel { min: 20, color: "#f59e0b".into() },
        ColorLevel { min: 0, color: "#ef4444".into() },
    ]
}

impl Default for Config {
    fn default() -> Self {
        Self {
            ring_size: d_ring_size(),
            refresh_seconds: d_refresh(),
            device_hidden: Vec::new(),
            show_tray_icon: true,
            animate: true,
            color_levels: d_colors(),
            charging_color: d_charge_color(),
            autostart: false,
            theme: d_theme(),
            taskbar_ring_enabled: false,
            extra: Map::new(),
        }
    }
}

impl Config {
    pub fn config_path() -> PathBuf {
        if let Ok(p) = std::env::var("PERIPHERAL_BATTERY_CONFIG") {
            return PathBuf::from(p);
        }
        let dir = std::env::current_exe()
            .ok()
            .and_then(|p| p.parent().map(|d| d.to_path_buf()))
            .unwrap_or_else(|| PathBuf::from("."));
        dir.join("config.json")
    }

    fn legacy_path() -> Option<PathBuf> {
        let base = std::env::var("APPDATA").ok()?;
        Some(PathBuf::from(base).join("PeripheralBattery").join("config.json"))
    }

    pub fn load() -> Self {
        let path = Self::config_path();
        if let Ok(text) = std::fs::read_to_string(&path) {
            if let Ok(cfg) = serde_json::from_str::<Config>(&text) {
                return cfg;
            }
        }
        if let Some(legacy) = Self::legacy_path() {
            if let Ok(text) = std::fs::read_to_string(&legacy) {
                if let Ok(cfg) = serde_json::from_str::<Config>(&text) {
                    let _ = cfg.save();
                    log_line(format!("已从旧配置迁移：{}", legacy.display()));
                    return cfg;
                }
            }
        }
        Self::default()
    }

    pub fn save(&self) -> bool {
        let path = Self::config_path();
        if let Some(dir) = path.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        match serde_json::to_string_pretty(self) {
            Ok(text) => {
                let tmp = path.with_extension("json.tmp");
                if std::fs::write(&tmp, text).is_ok() {
                    return std::fs::rename(&tmp, &path).is_ok();
                }
            }
            Err(_) => {}
        }
        false
    }

    pub fn is_hidden(&self, device_id: &str) -> bool {
        self.device_hidden.iter().any(|d| d == device_id)
    }
}

pub fn log_line(msg: impl AsRef<str>) {
    if std::env::var("PBR_DEBUG").is_ok() {
        println!("[{}] {}", now_hms(), msg.as_ref());
    }
}

fn now_hms() -> String {
    // 不引入 chrono：用 SystemTime 粗略换算本地时间即可（调试用）
    use std::time::{SystemTime, UNIX_EPOCH};
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let local = secs + 8 * 3600;
    format!("{:02}:{:02}:{:02}", (local / 3600) % 24, (local / 60) % 60, local % 60)
}

pub fn hex_to_rgb(text: &str, fallback: (u8, u8, u8)) -> (u8, u8, u8) {
    let s = text.trim().trim_start_matches('#');
    let expand = |t: &str| -> Option<(u8, u8, u8)> {
        if t.len() == 6 {
            Some((
                u8::from_str_radix(&t[0..2], 16).ok()?,
                u8::from_str_radix(&t[2..4], 16).ok()?,
                u8::from_str_radix(&t[4..6], 16).ok()?,
            ))
        } else if t.len() == 3 {
            let r = u8::from_str_radix(&t[0..1], 16).ok()? * 17;
            let g = u8::from_str_radix(&t[1..2], 16).ok()? * 17;
            let b = u8::from_str_radix(&t[2..3], 16).ok()? * 17;
            Some((r, g, b))
        } else {
            None
        }
    };
    expand(s).unwrap_or(fallback)
}

impl Config {
    pub fn color_for(&self, level: Option<u8>) -> (u8, u8, u8) {
        let Some(level) = level else { return (120, 126, 136) };
        let mut levels = self.color_levels.clone();
        levels.sort_by(|a, b| b.min.cmp(&a.min));
        for item in levels {
            if level >= item.min {
                return hex_to_rgb(&item.color, (34, 197, 94));
            }
        }
        (120, 126, 136)
    }

    pub fn charging_rgb(&self) -> (u8, u8, u8) {
        hex_to_rgb(&self.charging_color, (59, 130, 246))
    }
}

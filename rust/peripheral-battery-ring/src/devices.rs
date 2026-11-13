//! 设备层：ROG OMNI 接收器 + 罗技 HID++（接收器/有线）。
//! 纯查询，不写设备设置。

use crate::config::log_line;
use crate::model::{Reading, connection_text};
use hidapi::{HidApi, HidDevice};
use std::collections::HashSet;
use std::ffi::CString;
use std::time::{Duration, Instant};

pub const ASUS_VID: u16 = 0x0B05;
pub const OMNI_PID: u16 = 0x1ACE;
pub const LOGITECH_VID: u16 = 0x046D;

const CMD_GET: u8 = 0x12;
const SUB_KEYBOARD: u8 = 0x01;
const SUB_MOUSE: u8 = 0x07;

#[derive(Clone, Debug)]
pub struct Iface {
    pub path: CString,
    pub vid: u16,
    pub pid: u16,
    pub mi: i32,
    pub usage_page: u16,
    pub usage: u16,
    pub product: String,
    pub col: Option<u32>,
}

impl Iface {
    fn instance_key(&self) -> String {
        let s = self.path.to_string_lossy();
        s.split("&col").next().unwrap_or(&s).to_lowercase()
    }
}

pub fn enumerate() -> Vec<Iface> {
    let mut out = Vec::new();
    let Ok(api) = HidApi::new() else { return out };
    for dev in api.device_list() {
        let path = cstr_from(dev.path().to_string_lossy().as_ref());
        if path.as_bytes().is_empty() {
            continue;
        }
        let text = path.to_string_lossy().to_lowercase();
        let col = text.find("col0").and_then(|i| {
            let rest = &text[i + 3..];
            rest.chars().take_while(|c| c.is_ascii_digit()).collect::<String>().parse::<u32>().ok()
        });
        out.push(Iface {
            path,
            vid: dev.vendor_id(),
            pid: dev.product_id(),
            mi: dev.interface_number(),
            usage_page: dev.usage_page(),
            usage: dev.usage(),
            product: dev.product_string().unwrap_or("").to_string(),
            col,
        });
    }
    out
}

fn cstr_from(text: &str) -> CString {
    let bytes: Vec<u8> = text.as_bytes().iter().copied().take_while(|b| *b != 0).collect();
    CString::new(bytes).unwrap_or_default()
}

fn open(path: &CString) -> Option<HidDevice> {
    let api = HidApi::new().ok()?;
    api.open_path(path.as_c_str()).ok()
}

fn write_report(dev: &HidDevice, data: &[u8]) -> bool {
    dev.write(data).is_ok()
}

/// 读一条输入报文，直到 `accept` 返回 true 或超时。
fn read_until(dev: &HidDevice, buf: &mut [u8], timeout_ms: u64, mut accept: impl FnMut(&[u8]) -> bool) -> Option<Vec<u8>> {
    let deadline = Instant::now() + Duration::from_millis(timeout_ms.max(20));
    loop {
        let remain = deadline.saturating_duration_since(Instant::now());
        if remain.is_zero() {
            return None;
        }
        let n = dev.read_timeout(buf, remain.as_millis().min(u32::MAX as u128) as i32).ok()?;
        if n == 0 {
            continue;
        }
        let slice = &buf[..n];
        if accept(slice) {
            return Some(slice.to_vec());
        }
    }
}

// ---------------------------------------------------------------------------
// ROG
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct RogChannel {
    path: CString,
    report_id: u8,
}

fn rog_model(pid: u16) -> Option<(&'static str, &'static str, &'static str, usize, u8)> {
    // (名称, 类型, 连接, 电量偏移, 倍率)
    Some(match pid {
        0x1C25 => ("ROG 夜魔X", "keyboard", "omni_receiver", 6, 1),
        0x1A85 | 0x1A83 => ("ROG 夜魔", "keyboard", "omni_receiver", 6, 1),
        0x1B42 | 0x1CF1 => ("ROG 夜魔 Extreme", "keyboard", "omni_receiver", 6, 1),
        0x1B06 => ("ROG Falchion RX", "keyboard", "omni_receiver", 11, 1),
        0x1AB0 | 0x1B7A => ("ROG Strix Scope II 96", "keyboard", "omni_receiver", 6, 1),
        0x1A72 => ("ROG Gladius III", "mouse", "omni_receiver", 5, 1),
        0x1A94 => ("ROG Harpe Ace", "mouse", "omni_receiver", 5, 1),
        0x1B68 | 0x1B69 => ("ROG Harpe Ace Extreme", "mouse", "omni_receiver", 5, 0),
        0x1B65 => ("ROG Harpe Ace Mini", "mouse", "omni_receiver", 5, 0),
        0x1AD0 => ("ROG Harpe II Ace", "mouse", "receiver", 5, 0),
        0x1A68 | 0x1A6A => ("ROG Keris", "mouse", "omni_receiver", 5, 1),
        0x1B1A | 0x1B18 => ("ROG Keris II Ace", "mouse", "omni_receiver", 5, 0),
        0x1C0E | 0x1D4E => ("ROG Keris II Origin", "mouse", "omni_receiver", 5, 0),
        _ => return None,
    })
}

fn rog_no_answer(reply: &[u8]) -> bool {
    reply.len() >= 3 && reply[1] == 0xFF && reply[2] == 0xAA
}

fn rog_parse_paired(reply: &[u8]) -> Vec<u16> {
    if reply.len() < 9 || rog_no_answer(reply) {
        return Vec::new();
    }
    let mut pids = Vec::new();
    let payload = &reply[5..];
    let mut i = 0;
    while i + 3 < payload.len() {
        let pid = payload[i] as u16 | ((payload[i + 1] as u16) << 8);
        if pid == 0 {
            break;
        }
        pids.push(pid);
        i += 4;
    }
    pids
}

fn rog_parse_keyboard(reply: &[u8], offset: usize) -> Option<(u8, bool)> {
    if reply.len() <= offset.max(9) || rog_no_answer(reply) {
        return None;
    }
    if reply[1] != CMD_GET || reply[2] != SUB_KEYBOARD {
        return None;
    }
    let level = reply[offset];
    if level > 100 {
        return None;
    }
    Some((level, reply[9] == 1))
}

fn rog_parse_mouse(reply: &[u8], offset: usize, scale: u8) -> Option<(u8, bool)> {
    if reply.len() <= offset.max(10) || rog_no_answer(reply) {
        return None;
    }
    if reply[1] != CMD_GET || reply[2] != SUB_MOUSE {
        return None;
    }
    let level = (reply[offset] as u16 * if scale == 0 { 25 } else { scale as u16 }).min(100) as u8;
    let charging = reply[10] != 0;
    if level == 0 && !charging {
        return None;
    }
    Some((level, charging))
}

fn rog_build_request(report_id: u8, cmd: u8, sub: u8, len: usize) -> Vec<u8> {
    let mut packet = vec![0u8; len.max(3)];
    packet[0] = report_id;
    packet[1] = cmd;
    packet[2] = sub;
    packet
}

fn rog_read_paired(path: &CString) -> Vec<u16> {
    let Some(dev) = open(path) else { return Vec::new() };
    let request = rog_build_request(0x01, 0xA0, 0x00, 64);
    if !write_report(&dev, &request) {
        return Vec::new();
    }
    let mut buf = [0u8; 64];
    match read_until(&dev, &mut buf, 500, |r| r.len() >= 9 && r[1] == 0xA0 && r[2] == 0x00) {
        Some(reply) => rog_parse_paired(&reply),
        None => Vec::new(),
    }
}

fn rog_query(path: &CString, report_id: u8, sub: u8, timeout_ms: u64) -> Option<Vec<u8>> {
    let dev = open(path)?;
    let request = rog_build_request(report_id, CMD_GET, sub, 64);
    if !write_report(&dev, &request) {
        return None;
    }
    let mut buf = [0u8; 64];
    read_until(&dev, &mut buf, timeout_ms, |r| {
        r.len() >= 3 && ((r[1] == CMD_GET && r[2] == sub) || rog_no_answer(r))
    })
}

// ---------------------------------------------------------------------------
// Logitech
// ---------------------------------------------------------------------------

const RECEIVER_PIDS: &[u16] = &[
    0xC539, 0xC53A, 0xC53D, 0xC53F, 0xC541, 0xC545, 0xC547, 0xC54D, 0xC52B,
    0xC532, 0xC548,
];

const VOLTAGE_CURVE: &[(u16, u8)] = &[
    (4186, 100), (4067, 90), (3989, 80), (3922, 70), (3859, 60),
    (3811, 50), (3778, 40), (3751, 30), (3717, 20), (3671, 10),
    (3646, 5), (3579, 2), (3500, 0),
];

pub fn voltage_percent(mv: u16) -> u8 {
    if mv >= VOLTAGE_CURVE[0].0 {
        return 100;
    }
    for pair in VOLTAGE_CURVE.windows(2) {
        let (hi_mv, hi_pct) = pair[0];
        let (lo_mv, lo_pct) = pair[1];
        if mv >= lo_mv {
            let span = (hi_pct as f32 - lo_pct as f32) * (mv as f32 - lo_mv as f32);
            return (lo_pct as f32 + span / (hi_mv as f32 - lo_mv as f32)).round() as u8;
        }
    }
    0
}

fn logi_clean_name(product: &str) -> String {
    let mut name = product.trim().to_string();
    for suffix in [" Gaming Mouse", " Gaming Keyboard", " Gaming Headset", " Wireless Gaming Mouse"] {
        if let Some(stripped) = name.strip_suffix(suffix) {
            name = stripped.to_string();
            break;
        }
    }
    if name.is_empty() {
        "罗技设备".into()
    } else if name.to_lowercase().starts_with("logitech") {
        name
    } else {
        format!("罗技 {name}")
    }
}

struct LogiChannel {
    short: HidDevice,
    long: HidDevice,
}

impl LogiChannel {
    fn open(short: &CString, long: &CString) -> Option<Self> {
        let mut api = HidApi::new().ok()?;
        let short_dev = api.open_path(short.as_c_str()).ok()?;
        let long_dev = api.open_path(long.as_c_str()).ok()?;
        Some(Self { short: short_dev, long: long_dev })
    }

    fn call(&self, index: u8, feature: u8, function: u8, params: &[u8]) -> Option<Vec<u8>> {
        let mut packet = [0u8; 7];
        packet[0] = 0x10;
        packet[1] = index;
        packet[2] = feature;
        packet[3] = (function << 4) | 0x0C;
        for (i, b) in params.iter().take(3).enumerate() {
            packet[4 + i] = *b;
        }
        if self.short.write(&packet).is_err() {
            return None;
        }
        let mut buf = [0u8; 20];
        let reply = read_until(&self.long, &mut buf, 1500, |r| {
            r.len() >= 6 && r[1] == index && (r[2] == feature || r[2] == 0x8F || r[2] == 0xFF)
        })?;
        if reply[2] == 0x8F || reply[2] == 0xFF {
            return None;
        }
        if reply[2] != feature || reply[3] != (function << 4) | 0x0C {
            return None;
        }
        Some(reply[4..].to_vec())
    }

    fn read_slot_name(&self, slot: u8) -> Option<String> {
        // HID++1.0 长寄存器读：10 FF 83 B5 <sub> 00 00
        let mut packet = [0u8; 7];
        packet[0] = 0x10;
        packet[1] = 0xFF;
        packet[2] = 0x83;
        packet[3] = 0xB5;
        packet[4] = 0x40 + slot.saturating_sub(1);
        if self.short.write(&packet).is_err() {
            return None;
        }
        let mut buf = [0u8; 20];
        let reply = read_until(&self.long, &mut buf, 600, |r| {
            r.len() >= 6 && r[1] == 0xFF && (r[2] == 0x8F || (r[2] == 0x83 && r[3] == 0xB5))
        })?;
        if reply[2] == 0x8F {
            return None;
        }
        let data = &reply[4..];
        if data.len() < 2 {
            return None;
        }
        let len = (data[1] as usize).min(data.len().saturating_sub(2));
        if len == 0 {
            return None;
        }
        let raw = &data[2..2 + len];
        let name = String::from_utf8_lossy(raw)
            .trim_matches(|c| c == '\0' || c == ' ')
            .to_string();
        if name.is_empty() { None } else { Some(name) }
    }

    fn feature_index(&self, index: u8, feature: u16) -> Option<u8> {
        let data = self.call(index, 0x00, 0x00, &[(feature >> 8) as u8, (feature & 0xFF) as u8])?;
        let idx = *data.first()?;
        if idx == 0 { None } else { Some(idx) }
    }

    fn battery(&self, index: u8) -> Option<BatteryReading> {
        for feature in [0x1004u16, 0x1000, 0x1001, 0x1F20] {
            let Some(feat_idx) = self.feature_index(index, feature) else { continue };
            let function = if feature == 0x1004 { 1 } else { 0 };
            let Some(data) = self.call(index, feat_idx, function, &[]) else { continue };
            if let Some(reading) = decode_battery(feature, &data) {
                return Some(reading);
            }
        }
        None
    }
}

#[derive(Clone, Debug, Default)]
pub struct BatteryReading {
    pub level: Option<u8>,
    pub charging: bool,
    pub estimated: bool,
    pub voltage_mv: Option<u16>,
    pub detail: String,
}

fn decode_battery(feature: u16, data: &[u8]) -> Option<BatteryReading> {
    match feature {
        0x1004 => {
            if data.len() < 3 {
                return None;
            }
            let soc = data[0];
            let level_bits = data[1];
            let status = data[2];
            let charging = (1..=4).contains(&status);
            if soc > 0 {
                return Some(BatteryReading {
                    level: Some(soc.min(100)),
                    charging,
                    detail: format!("0x1004 soc={soc}% status={status}"),
                    ..Default::default()
                });
            }
            for (bit, pct) in [(8u8, 100u8), (4, 50), (2, 20), (1, 5)] {
                if level_bits & bit != 0 {
                    return Some(BatteryReading {
                        level: Some(pct),
                        charging,
                        estimated: true,
                        detail: format!("0x1004 粗档位 bits=0x{level_bits:02X} status={status}"),
                        ..Default::default()
                    });
                }
            }
            None
        }
        0x1000 => {
            if data.len() < 3 || data[0] == 0 {
                return None;
            }
            let level = data[0].min(100);
            Some(BatteryReading {
                level: Some(level),
                charging: (1..=4).contains(&data[2]),
                detail: format!("0x1000 level={level}% status={}", data[2]),
                ..Default::default()
            })
        }
        0x1001 => {
            if data.len() < 3 {
                return None;
            }
            let mv = ((data[0] as u16) << 8) | data[1] as u16;
            if !(2000..=5000).contains(&mv) {
                return None;
            }
            Some(BatteryReading {
                level: Some(voltage_percent(mv)),
                charging: data[2] & 0x80 != 0,
                estimated: true,
                voltage_mv: Some(mv),
                detail: format!("0x1001 电压 {mv} mV flags=0x{:02X}", data[2]),
            })
        }
        0x1F20 => {
            if data.len() < 3 || data[2] & 0x01 == 0 {
                return None;
            }
            let mv = ((data[0] as u16) << 8) | data[1] as u16;
            if !(2000..=5000).contains(&mv) {
                return None;
            }
            Some(BatteryReading {
                level: Some(voltage_percent(mv)),
                charging: data[2] & 0x02 != 0,
                estimated: true,
                voltage_mv: Some(mv),
                detail: format!("0x1F20 电压 {mv} mV flags=0x{:02X}", data[2]),
            })
        }
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// 设备对象与读取
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
enum Backend {
    RogKeyboard { path: CString, report_id: u8, offset: usize },
    RogMouse { path: CString, report_id: u8, offset: usize, scale: u8 },
    LogiSlot { short: CString, long: CString, index: u8 },
    LogiWired { short: CString, long: CString },
    GenericHid { path: CString },
    Atk { path: CString, proto: AtkProto },
}

#[derive(Clone, Debug)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub connection: String,
    pub vid_pid: String,
    backend: Backend,
}

impl Device {
    pub fn read(&self) -> Reading {
        match &self.backend {
            Backend::RogKeyboard { path, report_id, offset } => {
                let reply = rog_query(path, *report_id, SUB_KEYBOARD, 650);
                match reply.as_deref().and_then(|r| rog_parse_keyboard(r, *offset)) {
                    Some((level, charging)) => Reading {
                        device_id: self.id.clone(),
                        name: self.name.clone(),
                        level: Some(level),
                        status: if charging { if level >= 100 { "full" } else { "charging" } } else { "discharging" }.into(),
                        charging,
                        detail: format!("OMNI 键盘通道 · 电量 {level}%"),
                        connection: self.connection.clone(),
                        vid_pid: self.vid_pid.clone(),
                        ..Default::default()
                    },
                    None => self.unavailable("接收器回复 FF AA：设备已关机/休眠（敲一下键盘唤醒）"),
                }
            }
            Backend::RogMouse { path, report_id, offset, scale } => {
                let reply = rog_query(path, *report_id, SUB_MOUSE, 650);
                match reply.as_deref().and_then(|r| rog_parse_mouse(r, *offset, *scale)) {
                    Some((level, charging)) => Reading {
                        device_id: self.id.clone(),
                        name: self.name.clone(),
                        level: Some(level),
                        status: if charging { if level >= 100 { "full" } else { "charging" } } else { "discharging" }.into(),
                        charging,
                        detail: format!("OMNI 鼠标通道 · 电量 {level}%"),
                        connection: self.connection.clone(),
                        vid_pid: self.vid_pid.clone(),
                        ..Default::default()
                    },
                    None => self.unavailable("接收器回复 FF AA：设备已关机/休眠（动一下鼠标唤醒）"),
                }
            }
            Backend::LogiSlot { short, long, index } => {
                match LogiChannel::open(short, long).and_then(|ch| ch.battery(*index)) {
                    Some(b) => self.to_reading(b),
                    None => self.unavailable("设备没有应答（可能已关机/休眠）"),
                }
            }
            Backend::LogiWired { short, long } => {
                match LogiChannel::open(short, long).and_then(|ch| ch.battery(0xFF)) {
                    Some(b) => self.to_reading(b),
                    None => self.unavailable("有线设备没有应答（可能已拔出）"),
                }
            }
            Backend::GenericHid { path } => match generic_read(path) {
                Some(b) => self.to_reading(b),
                None => self.unavailable("设备没有上报电量（可能已休眠/不支持标准 HID 电量）"),
            },
            Backend::Atk { path, proto } => match atk_query(path, proto) {
                Some(reply) => match atk_parse(proto, &reply) {
                    Some((level, wired)) => Reading {
                        device_id: self.id.clone(),
                        name: self.name.clone(),
                        level: Some(level),
                        status: "discharging".into(),
                        charging: false,
                        detail: format!(
                            "{} · 电量[{}]=0x{:02X}{}",
                            proto.name,
                            proto.level_offset,
                            reply[proto.level_offset],
                            if wired { " · 有线连接" } else { "" }
                        ),
                        connection: self.connection.clone(),
                        vid_pid: self.vid_pid.clone(),
                        ..Default::default()
                    },
                    None => self.unavailable("ATK 回复无法解析"),
                },
                None => self.unavailable("接收器没有应答（设备可能已关机/休眠）"),
            },
        }
    }

    fn to_reading(&self, b: BatteryReading) -> Reading {
        let status = if b.charging {
            if b.level.unwrap_or(0) >= 100 { "full" } else { "charging" }
        } else if b.level.is_some() {
            "discharging"
        } else {
            "unknown"
        };
        Reading {
            device_id: self.id.clone(),
            name: self.name.clone(),
            level: b.level,
            status: status.into(),
            charging: b.charging,
            estimated: b.estimated,
            detail: format!("{} · {}", connection_text(&self.connection), b.detail),
            connection: self.connection.clone(),
            vid_pid: self.vid_pid.clone(),
            voltage_mv: b.voltage_mv,
        }
    }

    fn unavailable(&self, detail: &str) -> Reading {
        Reading {
            device_id: self.id.clone(),
            name: self.name.clone(),
            level: None,
            status: "unplugged".into(),
            connection: self.connection.clone(),
            vid_pid: self.vid_pid.clone(),
            detail: detail.into(),
            ..Default::default()
        }
    }
}

pub fn discover() -> Vec<Device> {
    let ifaces = enumerate();
    let mut devices = Vec::new();
    devices.extend(discover_rog(&ifaces));
    devices.extend(discover_logitech(&ifaces));
    devices.extend(discover_generic(&ifaces));
    devices.extend(discover_atk(&ifaces));
    devices
}

fn discover_rog(ifaces: &[Iface]) -> Vec<Device> {
    let mut devices = Vec::new();
    let omni: Vec<&Iface> = ifaces
        .iter()
        .filter(|i| i.vid == ASUS_VID && i.pid == OMNI_PID && i.mi == 2)
        .collect();
    if omni.is_empty() {
        return devices;
    }
    let key = omni[0].instance_key();
    let paired = omni
        .iter()
        .find(|i| i.col == Some(1))
        .map(|i| i.path.clone());
    let keyboard = omni.iter().find(|i| i.col == Some(2)).map(|i| i.path.clone());
    let mouse = omni.iter().find(|i| i.col == Some(3)).map(|i| i.path.clone());
    let Some(paired) = paired else { return devices };
    let pids = rog_read_paired(&paired);
    log_line(format!("ROG OMNI 配对列表: {:04X?}", pids));
    for pid in pids {
        let model = rog_model(pid);
        let (name, kind, conn, offset, scale) = match model {
            Some(m) => m,
            None => ("ROG 设备", "keyboard", "omni_receiver", 6, 1),
        };
        let suffix = "";
        let id = format!("rog:0x{pid:04X}{suffix}");
        let name = if model.is_none() {
            format!("{name} 0x{pid:04X}")
        } else {
            name.to_string()
        };
        let backend = if kind == "mouse" {
            let Some(path) = mouse.clone() else { continue };
            Backend::RogMouse { path, report_id: 0x03, offset, scale }
        } else {
            let Some(path) = keyboard.clone() else { continue };
            Backend::RogKeyboard { path, report_id: 0x02, offset }
        };
        devices.push(Device {
            id,
            name,
            connection: conn.into(),
            vid_pid: format!("{ASUS_VID:04X}:{OMNI_PID:04X}"),
            backend,
        });
    }
    let _ = key;
    devices
}

fn discover_logitech(ifaces: &[Iface]) -> Vec<Device> {
    let mut devices = Vec::new();
    let logi: Vec<&Iface> = ifaces.iter().filter(|i| i.vid == LOGITECH_VID).collect();
    // 接收器：short/long 按实例配对
    let mut instances: Vec<String> = Vec::new();
    for i in &logi {
        let key = i.instance_key();
        if !instances.contains(&key) {
            instances.push(key);
        }
    }
    for key in instances {
        let group: Vec<&Iface> = logi.iter().filter(|i| i.instance_key() == key).copied().collect();
        let short = group.iter().find(|i| i.usage_page == 0xFF00 && i.usage == 0x0001);
        let long = group.iter().find(|i| i.usage_page == 0xFF00 && i.usage == 0x0002);
        let (Some(short), Some(long)) = (short, long) else { continue };
        let pid = short.pid;
        let product = short.product.clone();
        let is_receiver = RECEIVER_PIDS.contains(&pid)
            || product.to_lowercase().contains("receiver")
            || product.contains("接收器");
        if is_receiver {
            if !RECEIVER_PIDS.contains(&pid) {
                continue;
            }
            let Some(ch) = LogiChannel::open(&short.path, &long.path) else { continue };
            for slot in 1u8..=6 {
                let Some(name) = ch.read_slot_name(slot) else { continue };
                devices.push(Device {
                    id: format!("logitech:{pid:04X}#{slot}"),
                    name: logi_clean_name(&name),
                    connection: if pid == 0xC548 { "bolt" } else if matches!(pid, 0xC52B | 0xC532) { "unifying" } else { "lightspeed" }.into(),
                    vid_pid: format!("{LOGITECH_VID:04X}:{pid:04X}"),
                    backend: Backend::LogiSlot {
                        short: short.path.clone(),
                        long: long.path.clone(),
                        index: slot,
                    },
                });
            }
        } else {
            // 有线直连：先 ping 再收
            let Some(ch) = LogiChannel::open(&short.path, &long.path) else { continue };
            if ch.call(0xFF, 0x00, 0x00, &[0x00, 0x00]).is_none() {
                continue;
            }
            let name = logi_clean_name(&product);
            devices.push(Device {
                id: format!("logitech:{pid:04X}#wired"),
                name,
                connection: "wired".into(),
                vid_pid: format!("{LOGITECH_VID:04X}:{pid:04X}"),
                backend: Backend::LogiWired { short: short.path.clone(), long: long.path.clone() },
            });
        }
    }
    devices
}

// ---------------------------------------------------------------------------
// ATK / VXE（373B / 3554）2.4G 鼠标
// ---------------------------------------------------------------------------

const ATK_VID: u16 = 0x373B;
const VXE_VID: u16 = 0x3554;

#[derive(Clone, Debug)]
struct AtkProto {
    name: &'static str,
    usage_page: u16,
    report_id: u8,
    report_len: usize,
    payload: Vec<u8>,       // 不含 report id
    level_offset: usize,
    wired_offset: Option<usize>,
    expect: Vec<(usize, u8)>,
}

fn atk_protocols() -> Vec<AtkProto> {
    vec![
        AtkProto {
            name: "ATK/VXE 协议 1",
            usage_page: 0xFF02,
            report_id: 0x08,
            report_len: 17,
            payload: {
                let mut v = vec![0u8; 16];
                v[0] = 0x04;
                v[15] = 0x49;
                v
            },
            level_offset: 6,
            wired_offset: Some(7),
            expect: Vec::new(),
        },
        AtkProto {
            name: "ATK/VXE 协议 2",
            usage_page: 0xFF05,
            report_id: 0x08,
            report_len: 64,
            payload: vec![0x7D, 0x72, 0x02, 0x00, 0x01, 0x07, 0x01],
            level_offset: 7,
            wired_offset: None,
            expect: vec![(1, 0x72), (5, 0x07)],
        },
    ]
}

fn atk_build_request(proto: &AtkProto) -> Vec<u8> {
    let mut packet = vec![0u8; proto.report_len];
    packet[0] = proto.report_id;
    let take = (proto.report_len - 1).min(proto.payload.len());
    packet[1..1 + take].copy_from_slice(&proto.payload[..take]);
    packet
}

fn atk_parse(proto: &AtkProto, reply: &[u8]) -> Option<(u8, bool)> {
    if reply.len() <= proto.level_offset {
        return None;
    }
    for (off, val) in &proto.expect {
        if reply.len() <= *off || reply[*off] != *val {
            return None;
        }
    }
    let raw = reply[proto.level_offset];
    if raw > 100 {
        return None;
    }
    let wired = proto
        .wired_offset
        .map(|off| reply.len() > off && reply[off] != 0)
        .unwrap_or(false);
    Some((raw, wired))
}

fn atk_query(path: &CString, proto: &AtkProto) -> Option<Vec<u8>> {
    let mut api = HidApi::new().ok()?;
    let dev = api
        .open_path(path.as_c_str())
        .ok()?;
    let packet = atk_build_request(proto);
    for _ in 0..3 {
        if dev.write(&packet).is_err() {
            return None;
        }
        let mut buf = [0u8; 64];
        let deadline = Instant::now() + Duration::from_millis(160);
        while Instant::now() < deadline {
            if let Ok(n) = dev.read_timeout(&mut buf, 80) {
                if n > 0 && atk_parse(proto, &buf[..n]).is_some() {
                    return Some(buf[..n].to_vec());
                }
            }
        }
    }
    None
}

fn discover_atk(ifaces: &[Iface]) -> Vec<Device> {
    let mut out = Vec::new();
    let mut seen: HashSet<(u16, u16)> = HashSet::new();
    for proto in atk_protocols() {
        for item in ifaces {
            if item.vid != ATK_VID && item.vid != VXE_VID {
                continue;
            }
            if item.usage_page != proto.usage_page {
                continue;
            }
            if !seen.insert((item.vid, item.pid)) {
                continue;
            }
            let pid = item.pid;
            let product = item.product.to_lowercase();
            let name = if product.contains("8k") {
                "ATK 8K 无线鼠标"
            } else if product.contains("mouse") {
                "ATK/VXE 无线鼠标"
            } else if product.contains("keyboard") {
                "ATK/VXE 无线键盘"
            } else {
                "ATK/VXE 2.4G 设备"
            };
            out.push(Device {
                id: format!("atk:{:04X}:{:04X}", item.vid, pid),
                name: name.to_string(),
                connection: "receiver".into(),
                vid_pid: format!("{:04X}:{:04X}", item.vid, pid),
                backend: Backend::Atk { path: item.path.clone(), proto: proto.clone() },
            });
            break; // 同一协议只取一个接口
        }
    }
    out
}

/// 标准 HID Battery Strength（usage page 0x06 / usage 0x20）设备。
/// 很多 2.4G 接收器 / 蓝牙外设用它上报电量；只读，不猜协议。
fn generic_read(path: &CString) -> Option<BatteryReading> {
    let dev = open(path)?;
    // 1) Feature Report：常见 report id 0x06 / 0x05 / 0x01
    for rid in [0x06u8, 0x05, 0x01] {
        let mut buf = [0u8; 9];
        buf[0] = rid;
        if let Ok(n) = dev.get_feature_report(&mut buf) {
            for &b in buf[1..n.min(buf.len())].iter() {
                if (1..=100).contains(&b) {
                    return Some(BatteryReading {
                        level: Some(b),
                        charging: false,
                        estimated: false,
                        voltage_mv: None,
                        detail: format!("标准 HID 电量 · feature 0x{rid:02X} = {b}%"),
                    });
                }
            }
        }
    }
    // 2) Input Report：等一小会儿，取第一个 1..=100 的字节
    let mut buf = [0u8; 16];
    for _ in 0..8 {
        match dev.read_timeout(&mut buf, 120) {
            Ok(n) if n > 0 => {
                for &b in buf[..n].iter() {
                    if (1..=100).contains(&b) {
                        return Some(BatteryReading {
                            level: Some(b),
                            charging: false,
                            estimated: false,
                            voltage_mv: None,
                            detail: format!("标准 HID 电量 · input = {b}%"),
                        });
                    }
                }
            }
            _ => {}
        }
    }
    None
}

fn discover_generic(ifaces: &[Iface]) -> Vec<Device> {
    let mut out = Vec::new();
    let mut seen: HashSet<String> = HashSet::new();
    let mut per_pid: std::collections::HashMap<(u16, u16), usize> = std::collections::HashMap::new();
    for item in ifaces {
        // 华硕/罗技的厂商协议已由上面的后端处理
        if item.vid == ASUS_VID || item.vid == LOGITECH_VID {
            continue;
        }
        if !(item.usage_page == 0x06 && item.usage == 0x20) {
            continue;
        }
        if item.product.to_lowercase().contains("aura") {
            continue;
        }
        let key = item.instance_key();
        if !seen.insert(key.clone()) {
            continue;
        }
        let counter = per_pid.entry((item.vid, item.pid)).or_insert(0);
        *counter += 1;
        let suffix = if *counter > 1 { format!("#{}", counter) } else { String::new() };
        let name = if item.product.trim().is_empty() {
            format!("2.4G 设备 {:04X}:{:04X}", item.vid, item.pid)
        } else {
            item.product.trim().to_string()
        };
        let path_low = item.path.to_string_lossy().to_lowercase();
        let connection = if path_low.contains("bth") { "bluetooth" } else { "receiver" };
        out.push(Device {
            id: format!("hid:{:04X}:{:04X}{}", item.vid, item.pid, suffix),
            name,
            connection: connection.into(),
            vid_pid: format!("{:04X}:{:04X}", item.vid, item.pid),
            backend: Backend::GenericHid { path: item.path.clone() },
        });
    }
    out
}

pub fn read_all() -> Vec<Reading> {
    let started = Instant::now();
    let devices = discover();
    log_line(format!("发现 {} 台设备，用时 {:?}", devices.len(), started.elapsed()));
    devices.iter().map(|d| d.read()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn voltage_curve_endpoints() {
        assert_eq!(voltage_percent(4200), 100);
        assert_eq!(voltage_percent(3500), 0);
        let mid = voltage_percent(3811);
        assert!((45..=55).contains(&mid), "mid={mid}");
    }

    #[test]
    fn parse_rog_keyboard_reply() {
        let mut reply = vec![0u8; 64];
        reply[1] = CMD_GET;
        reply[2] = SUB_KEYBOARD;
        reply[6] = 62;
        reply[9] = 1;
        assert_eq!(rog_parse_keyboard(&reply, 6), Some((62, true)));
        let mut off = reply.clone();
        off[1] = 0xFF;
        off[2] = 0xAA;
        assert_eq!(rog_parse_keyboard(&off, 6), None);
    }

    #[test]
    fn parse_rog_paired_pids() {
        let mut reply = vec![0u8; 32];
        reply[1] = 0xA0;
        reply[2] = 0x00;
        reply[5] = 0x25;
        reply[6] = 0x1C;
        reply[9] = 0x00;
        assert_eq!(rog_parse_paired(&reply), vec![0x1C25]);
    }

    #[test]
    fn decode_logitech_voltage() {
        let data = [0x0F, 0x8E, 0x80]; // 3982mV, charging
        let decoded = decode_battery(0x1001, &data).unwrap();
        assert!(decoded.charging);
        assert!(decoded.voltage_mv == Some(3982));
        assert!(decoded.level.unwrap() >= 75);
    }
}

//! 数据模型与状态文案。

#[derive(Clone, Debug, Default)]
pub struct Reading {
    pub device_id: String,
    pub name: String,
    pub level: Option<u8>,
    pub status: String, // charging / full / discharging / unplugged / error / unknown
    pub charging: bool,
    pub estimated: bool,
    pub detail: String,
    pub connection: String,
    pub vid_pid: String,
    pub voltage_mv: Option<u16>,
}

impl Reading {
    pub fn status_text(&self) -> &'static str {
        match self.status.as_str() {
            "charging" => "充电中",
            "full" => "已充满",
            "discharging" => "放电中",
            "unplugged" => "未连接",
            "error" => "读取失败",
            "unknown" => "未知",
            _ => "未知",
        }
    }

    pub fn level_text(&self) -> String {
        match self.level {
            None => "—".to_string(),
            Some(v) => format!("{}{}%", if self.estimated { "≈" } else { "" }, v),
        }
    }

    pub fn tip(&self) -> String {
        format!("{}：{} · {}", self.name, self.level_text(), self.status_text())
    }
}

#[derive(Clone, Debug, Default)]
pub struct DeviceInfo {
    pub device_id: String,
    pub name: String,
    pub connection: String,
    pub vid_pid: String,
    pub note: String,
}

pub fn connection_text(conn: &str) -> &'static str {
    match conn {
        "omni_receiver" => "ROG OMNI 接收器",
        "lightspeed" => "LIGHTSPEED 接收器",
        "unifying" => "Unifying 接收器",
        "bolt" => "Bolt 接收器",
        "receiver" => "2.4G 接收器",
        "wired" => "有线 USB",
        "bluetooth" => "蓝牙",
        _ => "未知连接",
    }
}

//! 主界面（egui）：设备页 / 设置页 / 关于页。

use crate::config::{hex_to_rgb, Config, ColorLevel};
use crate::model::{connection_text, Reading};
use crate::{WM_APP_REFRESH, WM_APP_RELOAD};
use eframe::egui;
use std::sync::{mpsc, Arc, Mutex};
use std::time::Duration;
use windows::Win32::Foundation::{HWND, LPARAM, WPARAM};
use windows::Win32::UI::WindowsAndMessaging::{FindWindowW, PostMessageW};

pub struct UiData {
    pub cfg: Config,
    pub readings: Vec<Reading>,
    pub hwnd: isize,
    pub config_path: String,
}

pub struct RingApp {
    data: Arc<Mutex<UiData>>,
    tab: usize,
    status: String,
    refresh_tx: mpsc::Sender<()>,
}

impl RingApp {
    pub fn new(
        cc: &eframe::CreationContext<'_>,
        data: Arc<Mutex<UiData>>,
        refresh_tx: mpsc::Sender<()>,
    ) -> Self {
        setup_fonts(&cc.egui_ctx);
        setup_style(&cc.egui_ctx);
        Self { data, tab: 0, status: String::new(), refresh_tx }
    }
}

fn setup_fonts(ctx: &egui::Context) {
    let mut fonts = egui::FontDefinitions::default();
    for path in [r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\msyh.ttc"] {
        if let Ok(bytes) = std::fs::read(path) {
            let mut fd = egui::FontData::from_owned(bytes);
            if path.ends_with(".ttc") {
                fd.index = 0;
            }
            fonts.font_data.insert("cjk".to_owned(), fd);
            fonts
                .families
                .entry(egui::FontFamily::Proportional)
                .or_default()
                .insert(0, "cjk".to_owned());
            fonts
                .families
                .entry(egui::FontFamily::Monospace)
                .or_default()
                .push("cjk".to_owned());
            break;
        }
    }
    ctx.set_fonts(fonts);
}

fn setup_style(ctx: &egui::Context) {
    let mut style = (*ctx.style()).clone();
    style.visuals = egui::Visuals::dark();
    style.visuals.panel_fill = egui::Color32::from_rgb(17, 18, 22);
    style.visuals.window_fill = egui::Color32::from_rgb(23, 25, 30);
    style.spacing.item_spacing = egui::vec2(9.0, 8.0);
    style.spacing.button_padding = egui::vec2(12.0, 6.0);
    ctx.set_style(style);
}

fn card_frame() -> egui::Frame {
    egui::Frame::none()
        .fill(egui::Color32::from_rgb(26, 28, 34))
        .stroke(egui::Stroke::new(1.0, egui::Color32::from_rgb(46, 50, 58)))
        .rounding(10.0)
        .inner_margin(egui::Margin::same(14.0))
}

fn status_color(r: &Reading) -> egui::Color32 {
    match r.status.as_str() {
        "charging" | "full" => egui::Color32::from_rgb(90, 162, 255),
        "discharging" => egui::Color32::from_rgb(74, 222, 128),
        "unplugged" => egui::Color32::from_rgb(150, 156, 166),
        "error" => egui::Color32::from_rgb(248, 113, 113),
        _ => egui::Color32::from_rgb(160, 166, 176),
    }
}

fn draw_ring(ui: &mut egui::Ui, size: f32, reading: &Reading, cfg: &Config, time: f64) {
    let (rect, _resp) = ui.allocate_exact_size(egui::vec2(size, size), egui::Sense::hover());
    let painter = ui.painter();
    let center = rect.center();
    let radius = size * 0.5 - 4.0;
    painter.circle_stroke(center, radius, egui::Stroke::new(5.0, egui::Color32::from_gray(64)));
    let charging = reading.charging || reading.status == "charging" || reading.status == "full";
    if let Some(level) = reading.level {
        let rgb = if charging { cfg.charging_rgb() } else { cfg.color_for(Some(level)) };
        let color = egui::Color32::from_rgb(rgb.0, rgb.1, rgb.2);
        let sweep = std::f32::consts::TAU * (level as f32 / 100.0);
        let mut points = Vec::with_capacity(65);
        for i in 0..=64 {
            let a = -std::f32::consts::FRAC_PI_2 + sweep * (i as f32 / 64.0);
            points.push(center + egui::vec2(a.cos(), a.sin()) * radius);
        }
        if points.len() > 1 {
            painter.add(egui::Shape::line(points, egui::Stroke::new(5.0, color)));
        }
    }
    if charging {
        let phase = ((time / 1.9) % 1.0) as f32;
        let start = -std::f32::consts::FRAC_PI_2 + std::f32::consts::TAU * phase;
        let mut points = Vec::with_capacity(9);
        for i in 0..=8 {
            let a = start + 0.35 * (i as f32 / 8.0);
            points.push(center + egui::vec2(a.cos(), a.sin()) * radius);
        }
        painter.add(egui::Shape::line(
            points,
            egui::Stroke::new(4.0, egui::Color32::from_rgb(219, 234, 254)),
        ));
    }
    painter.text(
        center,
        egui::Align2::CENTER_CENTER,
        reading.level_text(),
        egui::FontId::proportional(size * 0.26),
        egui::Color32::from_rgb(240, 244, 250),
    );
}

pub fn run_ui() -> Result<(), Box<dyn std::error::Error>> {
    let cfg = Config::load();
    let hwnd = unsafe {
        FindWindowW(crate::WND_CLASS, None)
            .map(|h| h.0 as isize)
            .unwrap_or(0)
    };
    let data = Arc::new(Mutex::new(UiData {
        cfg: cfg.clone(),
        readings: Vec::new(),
        hwnd,
        config_path: Config::config_path().display().to_string(),
    }));
    let (tx, rx) = mpsc::channel::<()>();
    let data_for_poll = data.clone();
    std::thread::spawn(move || loop {
        let readings = crate::devices::read_all();
        let secs = {
            let mut d = data_for_poll.lock().unwrap();
            d.readings = readings;
            d.cfg.refresh_seconds.max(1)
        };
        let _ = rx.recv_timeout(Duration::from_secs(secs));
    });
    let app_tx = tx.clone();
    let bgra = crate::ringicon::render_bgra(64, Some(76), false, &cfg);
    let mut rgba = Vec::with_capacity(bgra.len());
    for px in bgra.chunks_exact(4) {
        rgba.extend_from_slice(&[px[2], px[1], px[0], px[3]]);
    }
    let icon = egui::IconData { rgba, width: 64, height: 64 };
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_inner_size([640.0, 880.0])
            .with_min_inner_size([520.0, 640.0])
            .with_title("外设电量圆环")
            .with_icon(std::sync::Arc::new(icon)),
        ..Default::default()
    };
    eframe::run_native(
        "外设电量圆环",
        options,
        Box::new(move |cc| Ok(Box::new(RingApp::new(cc, data, app_tx)) as Box<dyn eframe::App>)),
    )?;
    Ok(())
}

impl eframe::App for RingApp {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        ctx.request_repaint_after(Duration::from_millis(400));
        let (mut cfg, readings, hwnd, config_path) = {
            let data = self.data.lock().unwrap();
            (data.cfg.clone(), data.readings.clone(), data.hwnd, data.config_path.clone())
        };
        let mut dirty = false;
        let mut refresh = false;

        egui::TopBottomPanel::top("top")
            .frame(egui::Frame::none().fill(egui::Color32::from_rgb(17, 18, 22)).inner_margin(egui::Margin::symmetric(18.0, 14.0)))
            .show(ctx, |ui| {
                ui.horizontal(|ui| {
                    ui.vertical(|ui| {
                        ui.label(egui::RichText::new("外设电量").size(26.0).strong().color(egui::Color32::from_rgb(240, 244, 250)));
                        let hidden = readings.iter().filter(|r| cfg.is_hidden(&r.device_id)).count();
                        let shown = readings.len().saturating_sub(hidden);
                        let extra = if hidden > 0 { format!("（{hidden} 台已隐藏）") } else { String::new() };
                        ui.label(
                            egui::RichText::new(format!(
                                "{} 台设备 · 每 {} 秒自动刷新{}",
                                shown, cfg.refresh_seconds, extra
                            ))
                            .size(13.0)
                            .color(egui::Color32::from_rgb(150, 156, 166)),
                        );
                    });
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.button("刷新").clicked() {
                            refresh = true;
                        }
                    });
                });
                ui.add_space(8.0);
                ui.horizontal(|ui| {
                    for (idx, name) in ["设备", "设置", "关于"].iter().enumerate() {
                        let selected = self.tab == idx;
                        if ui.selectable_label(selected, egui::RichText::new(*name).size(15.0)).clicked() {
                            self.tab = idx;
                        }
                        ui.add_space(8.0);
                    }
                });
            });

        egui::CentralPanel::default().show(ctx, |ui| {
            egui::ScrollArea::vertical().auto_shrink([false, false]).show(ui, |ui| {
                match self.tab {
                    0 => {
                        if readings.is_empty() {
                            card_frame().show(ui, |ui| {
                                ui.label(egui::RichText::new("没有发现可读的电量设备").size(15.0));
                                ui.add_space(6.0);
                                ui.label(
                                    egui::RichText::new("无线外设请确认接收器已插好、设备已唤醒；有线设备请换 USB 口重试。")
                                        .size(12.0)
                                        .color(egui::Color32::from_rgb(150, 156, 166)),
                                );
                            });
                        }
                        for reading in &readings {
                            let mut visible = !cfg.is_hidden(&reading.device_id);
                            card_frame().show(ui, |ui| {
                                ui.horizontal(|ui| {
                                    draw_ring(ui, 62.0, reading, &cfg, ctx.input(|i| i.time));
                                    ui.add_space(8.0);
                                    ui.vertical(|ui| {
                                        ui.label(egui::RichText::new(&reading.name).size(18.0).strong());
                                        let mut meta = String::new();
                                        if !reading.vid_pid.is_empty() {
                                            meta.push_str(&reading.vid_pid);
                                        }
                                        let conn = connection_text(&reading.connection);
                                        if !conn.is_empty() {
                                            if !meta.is_empty() { meta.push_str(" · "); }
                                            meta.push_str(conn);
                                        }
                                        ui.label(egui::RichText::new(meta).size(12.0).color(egui::Color32::from_rgb(150, 156, 166)));
                                        ui.add_space(2.0);
                                        ui.label(
                                            egui::RichText::new(format!("{} · 刚刚", reading.status_text()))
                                                .size(12.5)
                                                .color(status_color(reading)),
                                        );
                                    });
                                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Min), |ui| {
                                        if ui.button("刷新").clicked() {
                                            refresh = true;
                                        }
                                    });
                                });
                                ui.add_space(4.0);
                                if ui.checkbox(&mut visible, "在状态栏显示").changed() {
                                    if visible {
                                        cfg.device_hidden.retain(|d| d != &reading.device_id);
                                    } else if !cfg.device_hidden.contains(&reading.device_id) {
                                        cfg.device_hidden.push(reading.device_id.clone());
                                    }
                                    dirty = true;
                                }
                            });
                            ui.add_space(10.0);
                        }
                    }
                    1 => {
                        card_frame().show(ui, |ui| {
                            ui.label(egui::RichText::new("显示").size(16.0).strong());
                            ui.add_space(6.0);
                            ui.horizontal(|ui| {
                                ui.label("圆环尺寸");
                                if ui.add(egui::Slider::new(&mut cfg.ring_size, 24..=36).suffix(" px")).changed() {
                                    dirty = true;
                                }
                            });
                            ui.horizontal(|ui| {
                                ui.label("电量刷新间隔");
                                if ui.add(egui::DragValue::new(&mut cfg.refresh_seconds).range(1..=60).suffix(" 秒")).changed() {
                                    dirty = true;
                                    refresh = true;
                                }
                            });
                            if ui.checkbox(&mut cfg.show_tray_icon, "显示通知区域图标").changed() {
                                dirty = true;
                            }
                            let mut auto = crate::autostart::is_enabled();
                            if ui.checkbox(&mut auto, "开机自启（启动文件夹快捷方式）").changed() {
                                if crate::autostart::set(auto) {
                                    cfg.autostart = auto;
                                    dirty = true;
                                    self.status = if auto { "已开启开机自启".into() } else { "已关闭开机自启".into() };
                                } else {
                                    self.status = "开机自启设置失败（权限/路径问题）".into();
                                }
                            }
                            ui.label(
                                egui::RichText::new("通知区域图标尺寸由 Windows 决定；圆环尺寸影响主界面与托盘图标内的环比例。")
                                    .size(11.5)
                                    .color(egui::Color32::from_rgb(140, 146, 156)),
                            );
                        });
                        ui.add_space(10.0);
                        card_frame().show(ui, |ui| {
                            ui.label(egui::RichText::new("圆环颜色").size(16.0).strong());
                            ui.add_space(6.0);
                            let labels = ["高电量 ≥", "中电量 ≥", "低电量 <"];
                            while cfg.color_levels.len() < 3 {
                                cfg.color_levels.push(ColorLevel { min: 0, color: "#22c55e".into() });
                            }
                            let mid_min = cfg.color_levels[1].min;
                            for (idx, label) in labels.iter().enumerate() {
                                let mut min_val = cfg.color_levels[idx].min;
                                let mut color_text = cfg.color_levels[idx].color.clone();
                                let mut rgb_arr = {
                                    let rgb = hex_to_rgb(&color_text, (34, 197, 94));
                                    [rgb.0, rgb.1, rgb.2]
                                };
                                let mut changed = false;
                                ui.horizontal(|ui| {
                                    ui.label(*label);
                                    if idx < 2 {
                                        if ui.add(egui::DragValue::new(&mut min_val).range(1..=100).suffix("%")).changed() {
                                            changed = true;
                                        }
                                    } else {
                                        ui.label(egui::RichText::new(format!("< {mid_min}%")).size(12.0));
                                    }
                                    if ui.color_edit_button_srgb(&mut rgb_arr).changed() {
                                        changed = true;
                                    }
                                });
                                if changed {
                                    cfg.color_levels[idx].min = min_val;
                                    color_text = format!("#{:02x}{:02x}{:02x}", rgb_arr[0], rgb_arr[1], rgb_arr[2]);
                                    cfg.color_levels[idx].color = color_text;
                                    dirty = true;
                                }
                            }
                            ui.horizontal(|ui| {
                                ui.label("充电中");
                                let rgb = hex_to_rgb(&cfg.charging_color, (59, 130, 246));
                                let mut arr = [rgb.0, rgb.1, rgb.2];
                                if ui.color_edit_button_srgb(&mut arr).changed() {
                                    cfg.charging_color = format!("#{:02x}{:02x}{:02x}", arr[0], arr[1], arr[2]);
                                    dirty = true;
                                }
                            });
                        });
                        ui.add_space(10.0);
                        ui.horizontal(|ui| {
                            if ui.button("保存并应用").clicked() {
                                dirty = true;
                                self.status = "已保存并应用".into();
                            }
                            if ui.button("恢复默认").clicked() {
                                let autostart = cfg.autostart;
                                cfg = Config::default();
                                cfg.autostart = autostart;
                                dirty = true;
                                self.status = "已恢复默认".into();
                            }
                            ui.label(egui::RichText::new(&self.status).size(12.0).color(egui::Color32::from_rgb(120, 200, 140)));
                        });
                    }
                    _ => {
                        card_frame().show(ui, |ui| {
                            ui.label(egui::RichText::new("外设电量圆环 · Rust 版").size(18.0).strong());
                            ui.add_space(6.0);
                            ui.label("Rust + windows-rs + hidapi + egui；不需要 .NET / Python 运行时。");
                            ui.label("只读查询设备电量，不写设备设置；不联网、不写日志。");
                            ui.add_space(8.0);
                            ui.label(
                                egui::RichText::new(format!("配置文件：{config_path}"))
                                    .size(12.0)
                                    .color(egui::Color32::from_rgb(150, 156, 166)),
                            );
                            ui.label(
                                egui::RichText::new(format!("程序目录：{}", std::env::current_exe().map(|p| p.display().to_string()).unwrap_or_default()))
                                    .size(12.0)
                                    .color(egui::Color32::from_rgb(150, 156, 166)),
                            );
                            ui.add_space(8.0);
                            ui.horizontal(|ui| {
                                if ui.button("立即刷新").clicked() {
                                    refresh = true;
                                }
                                if ui.button("打开配置目录").clicked() {
                                    let _ = std::process::Command::new("explorer.exe")
                                        .arg(std::path::Path::new(&config_path).parent().unwrap_or(std::path::Path::new(".")))
                                        .spawn();
                                }
                            });
                        });
                    }
                }
            });
        });

        if dirty || refresh {
            let mut data = self.data.lock().unwrap();
            if refresh {
                let _ = self.refresh_tx.send(());
                if hwnd != 0 {
                    unsafe {
                        let _ = PostMessageW(HWND(hwnd as *mut _), WM_APP_REFRESH, WPARAM(0), LPARAM(0));
                    }
                }
            }
            if dirty {
                data.cfg = cfg.clone();
                if cfg.save() {
                    unsafe {
                        let _ = PostMessageW(HWND(hwnd as *mut _), WM_APP_RELOAD, WPARAM(0), LPARAM(0));
                    }
                } else {
                    self.status = "保存失败：配置目录不可写".into();
                }
            }
        }
    }
}

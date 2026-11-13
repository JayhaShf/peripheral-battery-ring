//! 圆环位图渲染 + HICON 创建（托盘图标）。

use crate::config::Config;
use windows::core::Result as WinResult;
use windows::Win32::Foundation::HWND;
use windows::Win32::Graphics::Gdi::{
    CreateBitmap, CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, BITMAPINFO,
    BITMAPINFOHEADER, DIB_RGB_COLORS, HBITMAP, HDC, HGDIOBJ,
};
use windows::Win32::UI::WindowsAndMessaging::{CreateIconIndirect, HICON, ICONINFO};

/// 渲染托盘圆环：BGRA（直通 alpha），供 CreateIconIndirect 使用。
pub fn render_bgra(size: u32, level: Option<u8>, charging: bool, cfg: &Config) -> Vec<u8> {
    let n = size as i32;
    let mut px = vec![0u8; (size * size * 4) as usize];
    let ss = 4i32;
    let ratio = (0.62 + (cfg.ring_size as f32 - 24.0) * 0.025).clamp(0.55, 0.92);
    let ring_d = n as f32 * ratio;
    let lw = (ring_d * 0.17).max(1.2);
    let cx = n as f32 / 2.0;
    let cy = n as f32 / 2.0;
    let r = ring_d / 2.0;
    let (pr, pg, pb) = if charging { cfg.charging_rgb() } else { cfg.color_for(level) };
    let (tr, tg, tb) = (150u8, 156u8, 166u8);

    for y in 0..n {
        for x in 0..n {
            let mut track_cov = 0.0f32;
            let mut prog_cov = 0.0f32;
            let mut shadow_cov = 0.0f32;
            for sy in 0..ss {
                for sx in 0..ss {
                    let fx = x as f32 + (sx as f32 + 0.5) / ss as f32;
                    let fy = y as f32 + (sy as f32 + 0.5) / ss as f32;
                    let dx = fx - cx;
                    let dy = fy - cy;
                    let dist = (dx * dx + dy * dy).sqrt();
                    // 右下偏移的柔和阴影
                    let sdx = fx - (cx + 0.9);
                    let sdy = fy - (cy + 1.2);
                    let sdist = (sdx * sdx + sdy * sdy).sqrt();
                    let sw = (lw * 2.6).max(2.4);
                    let k = ((sdist - r).abs() / (sw * 0.5)).min(1.0);
                    shadow_cov += (1.0 - k).powf(1.2) * 1.0;
                    if (dist - r).abs() > lw / 2.0 {
                        continue;
                    }
                    track_cov += 1.0;
                    let Some(level) = level else { continue };
                    let frac = (level as f32 / 100.0).clamp(0.0, 1.0);
                    let mut ang = dy.atan2(dx).to_degrees() + 90.0;
                    if ang < 0.0 {
                        ang += 360.0;
                    }
                    if ang <= 360.0 * frac {
                        prog_cov += 1.0;
                    } else if charging && ang >= 300.0 {
                        prog_cov += 1.2;
                    }
                }
            }
            let total = (ss * ss) as f32;
            let tc = (track_cov / total).min(1.0);
            let pc = (prog_cov / total).min(1.0);
            let shadow_a = (shadow_cov / total * 0.6).min(1.0) * 0.72;
            let mut a = shadow_a;
            let (mut rr, mut gg, mut bb) = (0.0f32, 0.0f32, 0.0f32);
            let blend = |a_src: f32, sr: f32, sg: f32, sb: f32,
                         a_dst: f32, dr: f32, dg: f32, db: f32| {
                let out = a_src + a_dst * (1.0 - a_src);
                if out <= 0.0 {
                    return (0.0, 0.0, 0.0, 0.0);
                }
                (
                    (sr * a_src + dr * a_dst * (1.0 - a_src)) / out,
                    (sg * a_src + dg * a_dst * (1.0 - a_src)) / out,
                    (sb * a_src + db * a_dst * (1.0 - a_src)) / out,
                    out,
                )
            };
            if tc > 0.0 {
                let (ar, ag, ab) = if pc > 0.0 {
                    (pr as f32, pg as f32, pb as f32)
                } else {
                    (tr as f32, tg as f32, tb as f32)
                };
                let aa = if pc > 0.0 { pc } else { tc * 0.42 };
                let (nr, ng, nb, na) = blend(aa, ar, ag, ab, a, rr, gg, bb);
                rr = nr; gg = ng; bb = nb; a = na;
            }
            if a <= 0.004 {
                continue;
            }
            let idx = ((y * n + x) * 4) as usize;
            px[idx] = bb.min(255.0) as u8;
            px[idx + 1] = gg.min(255.0) as u8;
            px[idx + 2] = rr.min(255.0) as u8;
            px[idx + 3] = (a * 255.0) as u8;
        }
    }
    px
}

/// 把 BGRA 像素做成 HICON（32bpp + 全 0 掩码，保留 alpha）。
pub fn hicon_from_bgra(w: i32, h: i32, bgra: &[u8]) -> WinResult<HICON> {
    unsafe {
        let dc: HDC = CreateCompatibleDC(None);
        let mut info = BITMAPINFO::default();
        info.bmiHeader = BITMAPINFOHEADER {
            biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
            biWidth: w,
            biHeight: -h, // top-down
            biPlanes: 1,
            biBitCount: 32,
            biCompression: 0,
            ..Default::default()
        };
        let mut bits: *mut core::ffi::c_void = std::ptr::null_mut();
        let color: HBITMAP = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &mut bits, None, 0)?;
        if !bits.is_null() {
            std::ptr::copy_nonoverlapping(bgra.as_ptr(), bits as *mut u8, bgra.len());
        }
        let mask: HBITMAP = CreateBitmap(w, h, 1, 1, None);
        let ii = ICONINFO {
            fIcon: true.into(),
            xHotspot: 0,
            yHotspot: 0,
            hbmMask: mask,
            hbmColor: color,
        };
        let icon = CreateIconIndirect(&ii);
        let _ = DeleteObject(HGDIOBJ(color.0));
        let _ = DeleteObject(HGDIOBJ(mask.0));
        let _ = DeleteDC(dc);
        icon
    }
}

pub fn make_tray_icon(level: Option<u8>, charging: bool, cfg: &Config) -> Option<HICON> {
    let size = 32u32;
    let bgra = render_bgra(size, level, charging, cfg);
    hicon_from_bgra(size as i32, size as i32, &bgra).ok()
}

#[allow(dead_code)]
pub fn blank_icon() -> Option<HICON> {
    let bgra = vec![0u8; 32 * 32 * 4];
    hicon_from_bgra(32, 32, &bgra).ok()
}

#[allow(dead_code)]
pub fn hwnd_is_null(h: HWND) -> bool {
    h.0.is_null()
}

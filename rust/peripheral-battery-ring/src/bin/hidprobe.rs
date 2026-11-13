//! 调试：打印 HID 枚举与 ROG / 罗技 原始收发字节。
use hidapi::HidApi;
use std::ffi::CString;

fn cstr_from(text: &str) -> CString {
    let bytes: Vec<u8> = text.as_bytes().iter().copied().take_while(|b| *b != 0).collect();
    CString::new(bytes).unwrap_or_default()
}
use std::time::Duration;

fn main() {
    let api = HidApi::new().expect("hidapi init");
    let mut rog_col1: Option<CString> = None;
    let mut logi_short: Option<CString> = None;
    let mut logi_long: Option<CString> = None;
    println!("== interfaces ==");
    for d in api.device_list() {
        let path = d.path().to_string_lossy().to_string();
        let low = path.to_lowercase();
        let col = low.find("col0").map(|i| &low[i..i + 5]).unwrap_or("");
        if d.vendor_id() == 0x0B05 || d.vendor_id() == 0x046D {
            println!(
                "VID:{:04X} PID:{:04X} mi={} usage={:04X}:{:04X} col={} {}",
                d.vendor_id(),
                d.product_id(),
                d.interface_number(),
                d.usage_page(),
                d.usage(),
                col,
                d.product_string().unwrap_or("")
            );
        }
        if d.vendor_id() == 0x0B05 && d.product_id() == 0x1ACE && col == "col01" {
            rog_col1 = Some(cstr_from(&path));
        }
        if d.vendor_id() == 0x046D && d.usage_page() == 0xFF00 && d.usage() == 0x0001 {
            logi_short = Some(cstr_from(&path));
        }
        if d.vendor_id() == 0x046D && d.usage_page() == 0xFF00 && d.usage() == 0x0002 {
            logi_long = Some(cstr_from(&path));
        }
    }

    if let Some(path) = rog_col1 {
        println!("\n== ROG OMNI col01 paired list ==");
        if let Ok(dev) = api.open_path(path.as_c_str()) {
            let mut req = vec![0u8; 64];
            req[0] = 0x01;
            req[1] = 0xA0;
            req[2] = 0x00;
            println!("write {:02X?}", &req[..8]);
            match dev.write(&req) {
                Ok(n) => println!("write ok {n}"),
                Err(e) => println!("write err {e}"),
            }
            let mut buf = [0u8; 64];
            for i in 0..4 {
                match dev.read_timeout(&mut buf, 400) {
                    Ok(n) if n > 0 => println!("read#{i} n={n}: {:02X?}", &buf[..n.min(32)]),
                    Ok(_) => println!("read#{i} timeout"),
                    Err(e) => println!("read#{i} err {e}"),
                }
            }
        }
    }

    if let (Some(short), Some(long)) = (logi_short, logi_long) {
        println!("\n== Logitech receiver slot 1 name ==");
        if let Ok(dev) = api.open_path(short.as_c_str()) {
            let mut req = [0u8; 7];
            req[0] = 0x10;
            req[1] = 0xFF;
            req[2] = 0x83;
            req[3] = 0xB5;
            req[4] = 0x40;
            println!("write {:02X?}", req);
            match dev.write(&req) {
                Ok(n) => println!("write ok {n}"),
                Err(e) => println!("write err {e}"),
            }
        }
        if let Ok(dev) = api.open_path(long.as_c_str()) {
            let mut buf = [0u8; 20];
            for i in 0..4 {
                match dev.read_timeout(&mut buf, 500) {
                    Ok(n) if n > 0 => println!("read#{i} n={n}: {:02X?}", &buf[..n.min(20)]),
                    Ok(_) => println!("read#{i} timeout"),
                    Err(e) => println!("read#{i} err {e}"),
                }
                std::thread::sleep(Duration::from_millis(60));
            }
        }
    }
}

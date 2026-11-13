//! 开机自启：启动文件夹快捷方式（当前用户，无需管理员）。

use std::path::PathBuf;
use std::process::Command;

pub fn lnk_path() -> Option<PathBuf> {
    let appdata = std::env::var("APPDATA").ok()?;
    Some(
        PathBuf::from(appdata)
            .join("Microsoft")
            .join("Windows")
            .join("Start Menu")
            .join("Programs")
            .join("Startup")
            .join("外设电量圆环.lnk"),
    )
}

pub fn is_enabled() -> bool {
    lnk_path().map(|p| p.exists()).unwrap_or(false)
}

pub fn set(enable: bool) -> bool {
    let Some(lnk) = lnk_path() else { return false };
    if !enable {
        let _ = std::fs::remove_file(&lnk);
        return !lnk.exists();
    }
    let Ok(exe) = std::env::current_exe() else { return false };
    let dir = exe.parent().map(|p| p.to_path_buf()).unwrap_or_default();
    let ps = format!(
        "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut('{}'); $s.TargetPath = '{}'; $s.WorkingDirectory = '{}'; $s.Description = '外设电量圆环'; $s.Save()",
        lnk.display(),
        exe.display(),
        dir.display()
    );
    let mut cmd = Command::new("powershell.exe");
    cmd.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", &ps]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    match cmd.status() {
        Ok(s) => s.success() && lnk.exists(),
        Err(_) => false,
    }
}

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager, WindowEvent,
};
use std::{
    env,
    net::TcpStream,
    path::PathBuf,
    process::{Child, Command, Stdio},
    thread,
    time::{Duration, Instant},
};
#[cfg(windows)]
use std::os::windows::process::CommandExt;

const BACKEND_ADDRESS: &str = "127.0.0.1:8000";
const BACKEND_URL: &str = "http://127.0.0.1:8000";

struct BackendProcess(Child);

impl Drop for BackendProcess {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn project_root() -> Result<PathBuf, String> {
    if let Ok(path) = env::current_dir() {
        if path.join("app").is_dir() && path.join(".venv").is_dir() {
            return Ok(path);
        }
    }
    let executable = env::current_exe().map_err(|error| error.to_string())?;
    executable
        .parent()
        .map(PathBuf::from)
        .filter(|path| path.join("app").is_dir() && path.join(".venv").is_dir())
        .ok_or_else(|| "Đặt XuongSachNoi.exe tại thư mục gốc của project để chạy backend.".into())
}

fn backend_is_ready() -> bool {
    TcpStream::connect_timeout(
        &BACKEND_ADDRESS.parse().expect("backend address hợp lệ"),
        Duration::from_millis(250),
    )
    .is_ok()
}

fn start_backend() -> Result<Option<BackendProcess>, String> {
    if backend_is_ready() {
        return Ok(None);
    }

    let root = project_root()?;
    let python = root.join(".venv").join("Scripts").join("python.exe");
    if !python.is_file() {
        return Err(format!("Không tìm thấy Python environment: {}", python.display()));
    }

    let mut command = Command::new(python);
    command
        .args(["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"])
        .current_dir(root)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    #[cfg(windows)]
    command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    let child = command
        .spawn()
        .map_err(|error| format!("Không thể khởi động backend: {error}"))?;

    let deadline = Instant::now() + Duration::from_secs(30);
    while Instant::now() < deadline {
        if backend_is_ready() {
            return Ok(Some(BackendProcess(child)));
        }
        thread::sleep(Duration::from_millis(250));
    }
    Err("Backend không phản hồi tại http://127.0.0.1:8000 sau 30 giây.".into())
}

fn show_main_window<R: tauri::Runtime>(app: &tauri::AppHandle<R>) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let backend = start_backend().map_err(|error| -> Box<dyn std::error::Error> { error.into() })?;
            if let Some(backend) = backend {
                app.manage(backend);
            }
            let window = app.get_webview_window("main").expect("thiếu cửa sổ chính");
            window.navigate(BACKEND_URL.parse().expect("backend URL hợp lệ"))?;

            let show = MenuItem::with_id(app, "show", "Mở Xưởng Sách Nói", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Thoát", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &quit])?;

            TrayIconBuilder::with_id("main")
                .icon(app.default_window_icon().expect("thiếu biểu tượng ứng dụng").clone())
                .tooltip("Xưởng Sách Nói")
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => show_main_window(app),
                    "quit" => app.exit(0),
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        show_main_window(&tray.app_handle());
                    }
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                // Keep rendering jobs alive; the tray menu offers an explicit exit.
                let _ = window.hide();
                api.prevent_close();
            }
        })
        .run(tauri::generate_context!())
        .expect("không thể chạy Xưởng Sách Nói");
}

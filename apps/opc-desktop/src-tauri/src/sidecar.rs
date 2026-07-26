use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream},
    sync::mpsc,
    time::Duration,
};

use tauri::{AppHandle, Runtime};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

pub const STARTUP_PREFIX: &str = "OPC App: ";
pub const MAX_STARTUP_LINE_BYTES: usize = 512;
pub const STARTUP_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum StartupError {
    Empty,
    TooLong,
    InvalidUtf8,
    UnexpectedLine,
    InvalidPort,
    Timeout,
    Process(String),
    Terminated,
}

impl std::fmt::Display for StartupError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Empty => write!(formatter, "sidecar returned an empty startup line"),
            Self::TooLong => write!(
                formatter,
                "sidecar startup output exceeded the safety limit"
            ),
            Self::InvalidUtf8 => write!(formatter, "sidecar startup output was not UTF-8"),
            Self::UnexpectedLine => write!(formatter, "sidecar returned an untrusted startup URL"),
            Self::InvalidPort => write!(formatter, "sidecar returned an invalid loopback port"),
            Self::Timeout => write!(formatter, "sidecar did not become ready before timeout"),
            Self::Process(_) => write!(formatter, "sidecar reported a startup error"),
            Self::Terminated => write!(formatter, "sidecar stopped before becoming ready"),
        }
    }
}

impl std::error::Error for StartupError {}

pub fn parse_startup_line(bytes: &[u8]) -> Result<(String, u16), StartupError> {
    if bytes.is_empty() {
        return Err(StartupError::Empty);
    }
    if bytes.len() > MAX_STARTUP_LINE_BYTES {
        return Err(StartupError::TooLong);
    }
    let line = std::str::from_utf8(bytes).map_err(|_| StartupError::InvalidUtf8)?;
    if line.trim() != line || !line.starts_with(STARTUP_PREFIX) {
        return Err(StartupError::UnexpectedLine);
    }
    let url = &line[STARTUP_PREFIX.len()..];
    let port_text = url
        .strip_prefix("http://127.0.0.1:")
        .and_then(|value| value.strip_suffix('/'))
        .ok_or(StartupError::UnexpectedLine)?;
    if port_text.is_empty()
        || !port_text.bytes().all(|byte| byte.is_ascii_digit())
        || (port_text.len() > 1 && port_text.starts_with('0'))
    {
        return Err(StartupError::InvalidPort);
    }
    let port = port_text
        .parse::<u16>()
        .map_err(|_| StartupError::InvalidPort)?;
    if port == 0 {
        return Err(StartupError::InvalidPort);
    }
    Ok((url.to_owned(), port))
}

pub fn wait_for_loopback(port: u16, timeout: Duration) -> Result<(), StartupError> {
    let address = SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), port);
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if TcpStream::connect_timeout(&address, Duration::from_millis(200)).is_ok() {
            return Ok(());
        }
        if std::time::Instant::now() >= deadline {
            return Err(StartupError::Timeout);
        }
        std::thread::sleep(Duration::from_millis(50));
    }
}

pub struct SidecarState {
    child: std::sync::Mutex<Option<CommandChild>>,
}

impl SidecarState {
    pub fn new() -> Self {
        Self {
            child: std::sync::Mutex::new(None),
        }
    }

    pub fn set(&self, child: CommandChild) -> Result<(), String> {
        let mut guard = self
            .child
            .lock()
            .map_err(|_| "sidecar ownership lock was poisoned".to_owned())?;
        if guard.is_some() {
            return Err("sidecar already owned by this desktop process".to_owned());
        }
        *guard = Some(child);
        Ok(())
    }

    pub fn stop(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(child) = guard.take() {
                let _ = child.kill();
            }
        }
    }
}

pub fn observe_sidecar<R: Runtime>(
    app: AppHandle<R>,
    mut events: tauri::async_runtime::Receiver<CommandEvent>,
    startup: mpsc::SyncSender<Result<(String, u16), StartupError>>,
) {
    tauri::async_runtime::spawn(async move {
        let mut startup_pending = Some(startup);
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    if let Some(sender) = startup_pending.take() {
                        let _ = sender.send(parse_startup_line(&line));
                    } else {
                        app.exit(1);
                        break;
                    }
                }
                CommandEvent::Stderr(line) => {
                    if let Some(sender) = startup_pending.take() {
                        let safe_code = if line.len() <= MAX_STARTUP_LINE_BYTES {
                            std::str::from_utf8(&line)
                                .ok()
                                .filter(|value| value.starts_with("OPC_APP_ERROR: "))
                                .unwrap_or("OPC_APP_ERROR")
                                .to_owned()
                        } else {
                            "OPC_APP_ERROR".to_owned()
                        };
                        let _ = sender.send(Err(StartupError::Process(safe_code)));
                    }
                }
                CommandEvent::Error(_) => {
                    if let Some(sender) = startup_pending.take() {
                        let _ = sender
                            .send(Err(StartupError::Process("SIDECAR_EVENT_ERROR".to_owned())));
                    }
                    app.exit(1);
                    break;
                }
                CommandEvent::Terminated(_) => {
                    if let Some(sender) = startup_pending.take() {
                        let _ = sender.send(Err(StartupError::Terminated));
                    } else {
                        app.exit(1);
                    }
                    break;
                }
                _ => {}
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_exact_ipv4_loopback_url() {
        assert_eq!(
            parse_startup_line(b"OPC App: http://127.0.0.1:49152/"),
            Ok(("http://127.0.0.1:49152/".to_owned(), 49152))
        );
    }

    #[test]
    fn rejects_remote_or_ambiguous_urls() {
        for line in [
            b"OPC App: http://0.0.0.0:49152/".as_slice(),
            b"OPC App: http://localhost:49152/".as_slice(),
            b"OPC App: https://127.0.0.1:49152/".as_slice(),
            b"OPC App: http://127.0.0.1:49152/path".as_slice(),
            b"OPC App: http://127.0.0.1:49152/?next=evil".as_slice(),
            b" OPC App: http://127.0.0.1:49152/".as_slice(),
            b"OPC App: http://127.0.0.1:49152/\n".as_slice(),
        ] {
            assert!(parse_startup_line(line).is_err(), "{line:?}");
        }
    }

    #[test]
    fn rejects_invalid_ports_and_unbounded_output() {
        for line in [
            b"OPC App: http://127.0.0.1:0/".as_slice(),
            b"OPC App: http://127.0.0.1:01/".as_slice(),
            b"OPC App: http://127.0.0.1:65536/".as_slice(),
            b"OPC App: http://127.0.0.1:not-a-port/".as_slice(),
        ] {
            assert!(parse_startup_line(line).is_err(), "{line:?}");
        }
        assert_eq!(
            parse_startup_line(&vec![b'x'; MAX_STARTUP_LINE_BYTES + 1]),
            Err(StartupError::TooLong)
        );
    }
}

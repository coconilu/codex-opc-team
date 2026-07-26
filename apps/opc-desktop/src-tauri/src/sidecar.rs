use std::{
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream},
    sync::mpsc,
    time::Duration,
};

use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};

use crate::process_tree::ProcessTreeGuard;

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
    UnexpectedOutput,
    Timeout,
    Process(String),
    Terminated,
    Eof,
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
            Self::UnexpectedOutput => {
                write!(formatter, "sidecar emitted unexpected startup output")
            }
            Self::Timeout => write!(formatter, "sidecar did not become ready before timeout"),
            Self::Process(_) => write!(formatter, "sidecar reported a startup error"),
            Self::Terminated => write!(formatter, "sidecar stopped before becoming ready"),
            Self::Eof => write!(formatter, "sidecar output closed before becoming ready"),
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
    let line = line
        .strip_suffix("\r\n")
        .or_else(|| line.strip_suffix('\n'))
        .or_else(|| line.strip_suffix('\r'))
        .unwrap_or(line);
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

#[derive(Debug, Default)]
struct StartupStdoutDecoder {
    line: Vec<u8>,
    complete: bool,
    allow_split_lf: bool,
}

impl StartupStdoutDecoder {
    fn push(&mut self, chunk: &[u8]) -> Result<Option<(String, u16)>, StartupError> {
        if chunk.is_empty() {
            return Ok(None);
        }

        if self.complete {
            let mut remaining = chunk;
            if self.allow_split_lf && remaining.first() == Some(&b'\n') {
                remaining = &remaining[1..];
            }
            self.allow_split_lf = false;
            return if remaining.is_empty() {
                Ok(None)
            } else {
                Err(StartupError::UnexpectedOutput)
            };
        }

        for (index, byte) in chunk.iter().copied().enumerate() {
            if byte == b'\r' || byte == b'\n' {
                self.complete = true;
                let mut next = index + 1;
                if byte == b'\r' {
                    if chunk.get(next) == Some(&b'\n') {
                        next += 1;
                    } else if next == chunk.len() {
                        self.allow_split_lf = true;
                    }
                }
                if next != chunk.len() {
                    return Err(StartupError::UnexpectedOutput);
                }
                return parse_startup_line(&self.line).map(Some);
            }

            // Check before pushing so attacker-controlled output can never
            // grow this allocation beyond the hard protocol limit.
            if self.line.len() == MAX_STARTUP_LINE_BYTES {
                return Err(StartupError::TooLong);
            }
            self.line.push(byte);
        }

        Ok(None)
    }
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
    child: std::sync::Mutex<Option<OwnedSidecar>>,
}

struct OwnedSidecar {
    child: CommandChild,
    tree: ProcessTreeGuard,
}

impl SidecarState {
    pub fn new() -> Self {
        Self {
            child: std::sync::Mutex::new(None),
        }
    }

    pub fn set(&self, child: CommandChild) -> Result<(), String> {
        let tree = match ProcessTreeGuard::attach(child.pid()) {
            Ok(tree) => tree,
            Err(error) => {
                let _ = child.kill();
                return Err(format!("unable to contain sidecar process tree: {error}"));
            }
        };
        let mut guard = self
            .child
            .lock()
            .map_err(|_| "sidecar ownership lock was poisoned".to_owned())?;
        if guard.is_some() {
            return Err("sidecar already owned by this desktop process".to_owned());
        }
        *guard = Some(OwnedSidecar { child, tree });
        Ok(())
    }

    pub fn stop(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(owned) = guard.take() {
                // Closing the Windows job first terminates PyInstaller's
                // onefile worker as well as the launcher handle.
                drop(owned.tree);
                let _ = owned.child.kill();
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
        let mut stdout = StartupStdoutDecoder::default();
        while let Some(event) = events.recv().await {
            match event {
                CommandEvent::Stdout(chunk) => match stdout.push(&chunk) {
                    Ok(Some(ready)) => {
                        if let Some(sender) = startup_pending.take() {
                            let _ = sender.send(Ok(ready));
                        } else {
                            app.state::<SidecarState>().stop();
                            app.exit(1);
                            return;
                        }
                    }
                    Ok(None) => {}
                    Err(error) => {
                        let was_starting = if let Some(sender) = startup_pending.take() {
                            let _ = sender.send(Err(error));
                            true
                        } else {
                            false
                        };
                        app.state::<SidecarState>().stop();
                        if !was_starting {
                            app.exit(1);
                        }
                        return;
                    }
                },
                CommandEvent::Stderr(chunk) => {
                    let was_starting = if let Some(sender) = startup_pending.take() {
                        let safe_code = if chunk.len() <= MAX_STARTUP_LINE_BYTES {
                            std::str::from_utf8(&chunk)
                                .ok()
                                .filter(|value| value.starts_with("OPC_APP_ERROR: "))
                                .unwrap_or("OPC_APP_ERROR")
                                .to_owned()
                        } else {
                            "OPC_APP_ERROR".to_owned()
                        };
                        let _ = sender.send(Err(StartupError::Process(safe_code)));
                        true
                    } else {
                        false
                    };
                    app.state::<SidecarState>().stop();
                    if !was_starting {
                        app.exit(1);
                    }
                    return;
                }
                CommandEvent::Error(_) => {
                    let was_starting = if let Some(sender) = startup_pending.take() {
                        let _ = sender
                            .send(Err(StartupError::Process("SIDECAR_EVENT_ERROR".to_owned())));
                        true
                    } else {
                        false
                    };
                    app.state::<SidecarState>().stop();
                    if !was_starting {
                        app.exit(1);
                    }
                    return;
                }
                CommandEvent::Terminated(_) => {
                    let was_starting = if let Some(sender) = startup_pending.take() {
                        let _ = sender.send(Err(StartupError::Terminated));
                        true
                    } else {
                        false
                    };
                    app.state::<SidecarState>().stop();
                    if !was_starting {
                        app.exit(1);
                    }
                    return;
                }
                _ => {}
            }
        }

        let was_starting = if let Some(sender) = startup_pending.take() {
            let _ = sender.send(Err(StartupError::Eof));
            true
        } else {
            false
        };
        app.state::<SidecarState>().stop();
        if !was_starting {
            app.exit(1);
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_only_exact_ipv4_loopback_url() {
        for line in [
            b"OPC App: http://127.0.0.1:49152/".as_slice(),
            b"OPC App: http://127.0.0.1:49152/\n".as_slice(),
            b"OPC App: http://127.0.0.1:49152/\r".as_slice(),
            b"OPC App: http://127.0.0.1:49152/\r\n".as_slice(),
        ] {
            assert_eq!(
                parse_startup_line(line),
                Ok(("http://127.0.0.1:49152/".to_owned(), 49152))
            );
        }
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
            b"OPC App: http://127.0.0.1:49152/ \n".as_slice(),
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

    #[test]
    fn raw_stream_is_bounded_before_a_newline_arrives() {
        let mut decoder = StartupStdoutDecoder::default();
        assert_eq!(
            decoder.push(&vec![b'x'; MAX_STARTUP_LINE_BYTES + 1]),
            Err(StartupError::TooLong)
        );
        assert_eq!(decoder.line.len(), MAX_STARTUP_LINE_BYTES);
    }

    #[test]
    fn raw_stream_accepts_chunked_line_and_crlf_splits() {
        let expected = ("http://127.0.0.1:49152/".to_owned(), 49152);
        let mut decoder = StartupStdoutDecoder::default();
        assert_eq!(decoder.push(b"OPC App: http://127."), Ok(None));
        assert_eq!(decoder.push(b"0.0.1:49152"), Ok(None));
        assert_eq!(decoder.push(b"/\r"), Ok(Some(expected.clone())));
        assert_eq!(decoder.push(b"\n"), Ok(None));

        let mut same_chunk = StartupStdoutDecoder::default();
        assert_eq!(
            same_chunk.push(b"OPC App: http://127.0.0.1:49152/\r\n"),
            Ok(Some(expected))
        );
    }

    #[test]
    fn raw_stream_rejects_forged_urls_and_extra_stdout() {
        let mut forged = StartupStdoutDecoder::default();
        assert_eq!(
            forged.push(b"OPC App: http://0.0.0.0:49152/\n"),
            Err(StartupError::UnexpectedLine)
        );

        let mut same_chunk = StartupStdoutDecoder::default();
        assert_eq!(
            same_chunk.push(b"OPC App: http://127.0.0.1:49152/\nforged\n"),
            Err(StartupError::UnexpectedOutput)
        );

        let mut later_chunk = StartupStdoutDecoder::default();
        assert!(matches!(
            later_chunk.push(b"OPC App: http://127.0.0.1:49152/\n"),
            Ok(Some(_))
        ));
        assert_eq!(
            later_chunk.push(b"forged\n"),
            Err(StartupError::UnexpectedOutput)
        );
    }
}

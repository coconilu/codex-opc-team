#[cfg(windows)]
mod platform {
    use std::{ffi::c_void, io, mem::size_of, ptr};

    use windows_sys::Win32::{
        Foundation::{CloseHandle, HANDLE},
        System::{
            JobObjects::{
                AssignProcessToJobObject, CreateJobObjectW, JobObjectExtendedLimitInformation,
                SetInformationJobObject, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
                JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            },
            Threading::{OpenProcess, PROCESS_SET_QUOTA, PROCESS_TERMINATE},
        },
    };

    pub struct ProcessTreeGuard {
        job: isize,
    }

    impl ProcessTreeGuard {
        pub fn attach(pid: u32) -> io::Result<Self> {
            // SAFETY: every handle is checked before use and closed on every
            // failure path. The job is unnamed and owned only by this guard.
            unsafe {
                let job = CreateJobObjectW(ptr::null(), ptr::null());
                if job.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                let configured = SetInformationJobObject(
                    job,
                    JobObjectExtendedLimitInformation,
                    (&limits as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast::<c_void>(),
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                );
                if configured == 0 {
                    let error = io::Error::last_os_error();
                    CloseHandle(job);
                    return Err(error);
                }
                let process = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, 0, pid);
                if process.is_null() {
                    let error = io::Error::last_os_error();
                    CloseHandle(job);
                    return Err(error);
                }
                let assigned = AssignProcessToJobObject(job, process);
                CloseHandle(process);
                if assigned == 0 {
                    let error = io::Error::last_os_error();
                    CloseHandle(job);
                    return Err(error);
                }
                Ok(Self { job: job as isize })
            }
        }
    }

    impl Drop for ProcessTreeGuard {
        fn drop(&mut self) {
            // SAFETY: this guard has sole ownership of the non-null job handle.
            unsafe {
                let _ = CloseHandle(self.job as HANDLE);
            }
        }
    }
}

#[cfg(not(windows))]
mod platform {
    use std::io;

    pub struct ProcessTreeGuard;

    impl ProcessTreeGuard {
        pub fn attach(_pid: u32) -> io::Result<Self> {
            Ok(Self)
        }
    }
}

pub use platform::ProcessTreeGuard;

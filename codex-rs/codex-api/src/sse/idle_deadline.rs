use std::future::Future;
use std::time::Duration;
use tokio::time::error::Elapsed;

/// A disabled deadline keeps a quiet local inference request cancellable indefinitely.
pub(super) enum IdleDeadline {
    Disabled,
    Timed(Duration),
}

impl From<Duration> for IdleDeadline {
    fn from(duration: Duration) -> Self {
        if duration.is_zero() {
            Self::Disabled
        } else {
            Self::Timed(duration)
        }
    }
}

impl IdleDeadline {
    pub(super) async fn wait<F: Future>(&self, future: F) -> Result<F::Output, Elapsed> {
        match self {
            Self::Disabled => Ok(future.await),
            Self::Timed(duration) => tokio::time::timeout(*duration, future).await,
        }
    }
}

#[cfg(test)]
#[path = "idle_deadline_tests.rs"]
mod tests;

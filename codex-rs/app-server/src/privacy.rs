use tracing_subscriber::EnvFilter;

/// A global filter covers stderr, the diagnostic database and feedback buffers.
/// Per-layer RUST_LOG settings must not re-enable content logging in Permissive.
pub(crate) fn diagnostic_filter(permissive_build: bool) -> Option<EnvFilter> {
    permissive_build.then(|| EnvFilter::new("off"))
}

#[cfg(test)]
mod tests {
    use super::diagnostic_filter;
    use std::io::Write;
    use std::sync::Arc;
    use std::sync::Mutex;
    use tracing_subscriber::layer::SubscriberExt;

    #[derive(Clone, Default)]
    struct Capture(Arc<Mutex<Vec<u8>>>);

    impl Write for Capture {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0
                .lock()
                .expect("capture lock")
                .extend_from_slice(bytes);
            Ok(bytes.len())
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn permissive_filter_blocks_content_at_every_level() {
        let capture = Capture::default();
        let writer = capture.clone();
        let subscriber = tracing_subscriber::fmt()
            .with_max_level(tracing::Level::TRACE)
            .with_writer(move || writer.clone())
            .finish()
            .with(diagnostic_filter(true));
        tracing::subscriber::with_default(subscriber, || {
            let span = tracing::info_span!("request", prompt = "PRIVATE-PROMPT");
            let _entered = span.enter();
            tracing::trace!("PRIVATE-RESPONSE");
            tracing::debug!("PRIVATE-TOOL-OUTPUT");
            tracing::info!("PRIVATE-INSTRUCTIONS");
            tracing::warn!("PRIVATE-ERROR-BODY");
            tracing::error!("PRIVATE-CREDENTIAL");
        });
        assert!(capture.0.lock().expect("capture lock").is_empty());
    }

    #[test]
    fn upstream_build_keeps_its_existing_logging_policy() {
        let capture = Capture::default();
        let writer = capture.clone();
        let subscriber = tracing_subscriber::fmt()
            .with_max_level(tracing::Level::TRACE)
            .with_writer(move || writer.clone())
            .finish()
            .with(diagnostic_filter(false));
        tracing::subscriber::with_default(subscriber, || {
            tracing::info!("DIAGNOSTIC-CAPTURE-CONTROL");
        });
        let logs = capture.0.lock().expect("capture lock").clone();
        assert!(
            String::from_utf8(logs)
                .expect("UTF-8 logs")
                .contains("DIAGNOSTIC-CAPTURE-CONTROL")
        );
    }
}

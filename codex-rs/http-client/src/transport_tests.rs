use super::*;
use serde_json::json;
use std::io::Read;
use std::io::Write;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use tracing_subscriber::Layer;
use tracing_subscriber::layer::SubscriberExt;

#[tokio::test]
async fn enabled_request_logging_emits_metadata_without_url_or_body() {
    let logs = capture_transport_logs(HttpClient::new(test_reqwest_client())).await;

    assert!(logs.contains("log capture sentinel"));
    assert!(logs.contains("HTTP request"));
    assert!(!logs.contains("url-secret"));
    assert!(!logs.contains("body-secret"));
}

#[tokio::test]
async fn completed_request_logging_omits_response_headers_and_content() {
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).expect("bind listener");
    let address = listener.local_addr().expect("listener address");
    let server = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().expect("accept request");
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .expect("read timeout");
        let mut incoming = [0; 4096];
        stream.read(&mut incoming).expect("read request");
        let body = b"PRIVATE-RESPONSE";
        let headers = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nSet-Cookie: session=PRIVATE-COOKIE\r\nX-Request-Id: PRIVATE-HEADER\r\nConnection: close\r\n\r\n",
            body.len()
        );
        stream
            .write_all(headers.as_bytes())
            .expect("response headers");
        stream.write_all(body).expect("response body");
    });
    let capture = Arc::new(Mutex::new(Vec::new()));
    let writer = Arc::clone(&capture);
    let subscriber = tracing_subscriber::registry().with(
        tracing_subscriber::fmt::layer()
            .with_writer(move || TestLogWriter(Arc::clone(&writer)))
            .with_filter(
                tracing_subscriber::filter::Targets::new()
                    .with_target("codex_http_client", tracing::Level::TRACE),
            ),
    );
    let _guard = tracing::subscriber::set_default(subscriber);
    let transport = ReqwestTransport::from_http_client(HttpClient::new(test_reqwest_client()));
    let mut request = Request::new(
        Method::GET,
        format!("http://{address}/responses?token=PRIVATE-QUERY"),
    );
    request.timeout = Some(Duration::from_secs(5));
    let response = transport
        .execute(request)
        .await
        .expect("successful request");
    assert_eq!(response.body.as_ref(), b"PRIVATE-RESPONSE");
    server.join().expect("server thread");
    let logs =
        String::from_utf8(capture.lock().expect("capture lock").clone()).expect("UTF-8 logs");
    assert!(logs.contains("Request completed"));
    assert!(!logs.contains("PRIVATE-"), "{logs}");
}

#[tokio::test]
async fn disabled_request_logging_suppresses_transport_url_and_body() {
    let logs =
        capture_transport_logs(HttpClient::new(test_reqwest_client()).without_request_logging())
            .await;

    assert!(logs.contains("log capture sentinel"));
    assert!(!logs.contains("url-secret"));
    assert!(!logs.contains("body-secret"));

    let controller = crate::NetworkPolicyController::default();
    let policy = controller.policy();
    controller.publish(policy.revision(), crate::DestinationPolicy::Unrestricted);
    let factory = crate::HttpClientFactory::new(crate::OutboundProxyPolicy::ReqwestDefault)
        .with_network_policy(policy);
    let client =
        crate::RouteAwareClientPool::new(factory, crate::ClientRouteClass::Api).into_client();
    let logs = capture_transport_logs(client.clone()).await;
    assert!(logs.contains("HTTP request"));
    assert!(!logs.contains("url-secret"));
    let logs = capture_transport_logs(client.clone().without_request_logging()).await;
    assert!(!logs.contains("url-secret"), "{logs}");
    assert!(!logs.contains("body-secret"));
    let logs = capture_transport_logs(client).await;
    assert!(logs.contains("HTTP request"));
    assert!(!logs.contains("url-secret"));
    assert!(!logs.contains("body-secret"));
}

#[tokio::test]
async fn connection_failures_are_classified_without_exposing_request_urls() {
    let unavailable_server =
        std::net::TcpListener::bind(("127.0.0.1", 0)).expect("server port should bind");
    let server_addr = unavailable_server
        .local_addr()
        .expect("server listener should have an address");
    drop(unavailable_server);
    let transport = ReqwestTransport::from_http_client(HttpClient::new(test_reqwest_client()));
    let request = Request::new(
        Method::POST,
        format!("http://{server_addr}/responses?token=url-secret"),
    );

    let error = match transport.stream(request).await {
        Err(TransportError::Connection(error)) => error,
        Err(error) => panic!("expected a connection failure, got {error}"),
        Ok(_) => panic!("an unavailable server should not return a response"),
    };
    assert!(!error.to_string().contains("url-secret"));
}

fn test_reqwest_client() -> reqwest::Client {
    reqwest::Client::builder()
        .no_proxy()
        .build()
        .expect("HTTP client should build")
}

async fn capture_transport_logs(client: HttpClient) -> String {
    let unavailable_server =
        std::net::TcpListener::bind(("127.0.0.1", 0)).expect("server port should bind");
    let server_addr = unavailable_server
        .local_addr()
        .expect("server listener should have an address");
    drop(unavailable_server);
    let transport = ReqwestTransport::from_http_client(client);
    let log_buffer = Arc::new(Mutex::new(Vec::new()));
    let writer_buffer = Arc::clone(&log_buffer);
    let subscriber = tracing_subscriber::registry().with(
        tracing_subscriber::fmt::layer()
            .with_ansi(false)
            .with_writer(move || TestLogWriter(Arc::clone(&writer_buffer)))
            .with_filter(
                tracing_subscriber::filter::Targets::new()
                    .with_target("codex_http_client::transport", tracing::Level::TRACE)
                    .with_target("codex_http_client::client", tracing::Level::DEBUG),
            ),
    );
    let _guard = tracing::subscriber::set_default(subscriber);
    tracing::trace!(target: "codex_http_client::transport", "log capture sentinel");
    let mut request = Request::new(
        Method::POST,
        format!("http://{server_addr}/request?token=url-secret"),
    )
    .with_json(&json!({"token": "body-secret"}));
    request.timeout = Some(Duration::from_secs(1));

    let _ = transport.execute(request).await;

    String::from_utf8(
        log_buffer
            .lock()
            .expect("log buffer should not be poisoned")
            .clone(),
    )
    .expect("captured logs should be UTF-8")
}

#[derive(Clone)]
struct TestLogWriter(Arc<Mutex<Vec<u8>>>);

impl Write for TestLogWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        self.0
            .lock()
            .map_err(|_| std::io::Error::other("log buffer should not be poisoned"))?
            .extend_from_slice(buf);
        Ok(buf.len())
    }

    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}

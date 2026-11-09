use super::ResponsesRequest;
use super::ev_completed;
use super::mount_sse_once_match;
use super::sse;
use anyhow::Result;
use codex_http_client::HttpClientBuilder;
use pretty_assertions::assert_eq;
use serde_json::json;
use wiremock::MockServer;
use wiremock::matchers::header;

#[tokio::test]
async fn custom_matcher_records_only_matching_requests() -> Result<()> {
    let server = MockServer::start().await;
    let response_mock = mount_sse_once_match(
        &server,
        header("x-fixture", "selected"),
        sse(vec![ev_completed("selected-response")]),
    )
    .await;
    let client = HttpClientBuilder::new().build_direct()?;
    let url = format!("{}/v1/responses", server.uri());
    let unmatched = json!({"input": [], "marker": "unmatched"});
    let selected = json!({"input": [], "marker": "selected"});

    let response = client.post(&url).json(&unmatched).send().await?;
    assert_eq!(response.status().as_u16(), 404);
    let response = client
        .post(&url)
        .header("x-fixture", "selected")
        .json(&selected)
        .send()
        .await?;
    assert_eq!(response.status().as_u16(), 200);

    let captured = response_mock
        .requests()
        .iter()
        .map(ResponsesRequest::body_json)
        .collect::<Vec<_>>();
    assert_eq!(captured, vec![selected]);
    Ok(())
}

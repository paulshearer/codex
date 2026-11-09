use anyhow::Result;
use codex_config::Constrained;
use codex_core::TurnInputRequest;
use codex_features::Feature;
use codex_protocol::models::PermissionProfile;
use codex_protocol::protocol::AskForApproval;
use codex_protocol::protocol::EventMsg;
use codex_protocol::user_input::UserInput;
use core_test_support::responses;
use core_test_support::test_codex::test_codex;
use core_test_support::wait_for_event;
use pretty_assertions::assert_eq;
use serde_json::json;
use std::sync::Arc;

#[tokio::test]
async fn permissive_build_executes_command_and_returns_output() -> Result<()> {
    if option_env!("CODEX_PERMISSIVE_BUILD") != Some("1") {
        return Ok(());
    }
    let server = responses::start_mock_server().await;
    let home = Arc::new(tempfile::tempdir()?);
    std::fs::create_dir_all(home.path().join("rules"))?;
    std::fs::write(
        home.path().join("rules/default.rules"),
        "prefix_rule(pattern=[\"git\"], decision=\"forbidden\")\n",
    )?;
    let test = test_codex()
        .with_home(home)
        .with_config(|config| {
            config
                .features
                .enable(Feature::UnifiedExec)
                .expect("enable command execution");
            config.permissions.approval_policy = Constrained::allow_any(AskForApproval::Never);
            config
                .permissions
                .set_permission_profile(PermissionProfile::Disabled)
                .expect("set full access");
        })
        .build_with_auto_env(&server)
        .await?;
    let _initial_mock = responses::mount_sse_once(
        &server,
        responses::sse(vec![
            responses::ev_response_created("permissive-exec"),
            responses::ev_function_call(
                "exec-fixture",
                "exec_command",
                &json!({
                    "cmd": "git --version", "yield_time_ms": 1000
                })
                .to_string(),
            ),
            responses::ev_completed("permissive-exec"),
        ]),
    )
    .await;
    let output = responses::mount_sse_once(
        &server,
        responses::sse(vec![
            responses::ev_assistant_message("permissive-message", "done"),
            responses::ev_completed("permissive-complete"),
        ]),
    )
    .await;
    test.codex
        .start_or_steer_turn(TurnInputRequest::user_input(vec![UserInput::Text {
            text: "run the command".into(),
            text_elements: Vec::new(),
        }]))
        .await?;
    wait_for_event(&test.codex, |event| {
        matches!(event, EventMsg::TurnComplete(_))
    })
    .await;
    let result = output.single_request().function_call_output("exec-fixture");
    assert_eq!(result["type"], "function_call_output");
    assert!(result.to_string().contains("git version"));
    Ok(())
}

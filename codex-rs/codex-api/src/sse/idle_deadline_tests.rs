use super::IdleDeadline;
use pretty_assertions::assert_eq;
use std::time::Duration;

#[tokio::test(start_paused = true)]
async fn quiet_disabled_stream_survives_a_long_prefill() -> Result<(), tokio::time::error::Elapsed>
{
    let result = IdleDeadline::Disabled
        .wait(async {
            tokio::time::sleep(Duration::from_secs(/*secs*/ 7_200)).await;
            "completed"
        })
        .await?;
    assert_eq!(result, "completed");
    Ok(())
}

#[tokio::test(start_paused = true)]
async fn timed_stream_reports_expired_idle_deadline() {
    let result = IdleDeadline::Timed(Duration::from_secs(/*secs*/ 1))
        .wait(std::future::pending::<()>())
        .await;
    assert!(result.is_err());
}

#[tokio::test]
async fn disabled_wait_can_be_cancelled() {
    let task = tokio::spawn(async {
        IdleDeadline::Disabled
            .wait(std::future::pending::<()>())
            .await
    });
    task.abort();
    match task.await {
        Err(error) => assert!(error.is_cancelled()),
        Ok(_) => panic!("expected the disabled wait to be cancelled"),
    }
}

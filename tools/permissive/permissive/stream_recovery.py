"""Bounded continuation for specific premature Responses stream failures."""
import json
import re


MAX_STREAM_RECOVERY_ATTEMPTS = 2
STREAM_RECOVERY_DELAYS = (1, 2)
RECOVERY_PROMPT = (
    "The previous response stream ended unexpectedly. Continue the unfinished work in this same "
    "conversation from the recorded progress and tool results. Do not repeat completed tool calls, "
    "commands, browser actions, or other effects. If an action's outcome is uncertain, inspect the "
    "current state and verify whether it already succeeded before attempting it again. Do not restart "
    "the task or repeat the original request. Finish the remaining work and report the result."
)
TOOL_RECOVERY_PROMPT = (
    'The previous provider tool call was rejected before tool execution because it did not match '
    'the advertised JSON function envelope. Continue this same unfinished task from saved progress. '
    'For exec, supply exactly one string field named input containing JavaScript; nested calls use '
    'their declared types, for example text(await tools.exec_command({cmd:"Get-Date"})); '
    'Do not repeat completed actions. Correct the tool contract and finish the remaining work.'
)

_PREMATURE_RESPONSES = {
    "responses completed event has no completed response",
    "responses stream ended before a terminal event",
    "stream closed before response.completed",
    "stream ended before response.completed",
    "provider generation stopped producing output",
}
_STREAM_CODES = {"responseStreamDisconnected", "responseStreamConnectionFailed"}


def _failure_message(error):
    """Accept only the native classifications owned by this recovery path."""
    if isinstance(error, str):
        try:
            error = json.loads(error)
        except (ValueError, TypeError):
            return None
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return None
    classification = error.get("codexErrorInfo")
    if isinstance(classification, dict) and len(classification) == 1:
        code, detail = next(iter(classification.items()))
        if code not in _STREAM_CODES or not isinstance(detail, dict) or detail.keys() - {"httpStatusCode"}:
            return None
        status = detail.get("httpStatusCode")
        if status is not None and (type(status) is not int or status not in {200, 408, 500, 502, 503, 504}):
            return None
    elif not isinstance(classification, str) or classification not in {"other", *_STREAM_CODES}:
        return None
    message = error["message"].strip().casefold()
    prefix = "stream disconnected before completion: "
    if message.startswith(prefix):
        message = message[len(prefix):]
    return message


def is_recoverable_stream_error(error):
    """Require a specific transport failure and a compatible native classification."""
    return _failure_message(error) in _PREMATURE_RESPONSES


def is_recoverable_tool_contract_error(error):
    message = _failure_message(error)
    return bool(message and re.fullmatch(
        r'custom function tool [a-z0-9_.]+ requires exactly one string input: \{"input":"<raw tool input>"\}',
        message))


async def complete_with_stream_recovery(wait_turn, start_continuation, before_retry, emit):
    """Keep the caller's backend alive while retrying at most two failed native turns.

    The caller owns cancellation, pause boundaries, task/thread identity and process lifetime.
    Every event is content-free; failed native events and tool evidence remain in the task log.
    """
    attempt = 0
    while True:
        turn = await wait_turn()
        if turn.get("status") == "completed":
            if attempt:
                emit({"phase": "recovered", "attempt": attempt, "maxAttempts": MAX_STREAM_RECOVERY_ATTEMPTS,
                      "message": "The response stream recovered; work continued from saved progress."})
            return turn
        contract = is_recoverable_tool_contract_error(turn.get('error'))
        if turn.get("status") != "failed" or not (contract or is_recoverable_stream_error(turn.get("error"))):
            return turn
        if attempt >= MAX_STREAM_RECOVERY_ATTEMPTS:
            emit({"phase": "exhausted", "attempt": attempt, "maxAttempts": MAX_STREAM_RECOVERY_ATTEMPTS,
                  "message": "The response stream could not recover automatically. Retry from saved progress."})
            return turn
        attempt += 1
        emit({"phase": "retrying", "attempt": attempt, "maxAttempts": MAX_STREAM_RECOVERY_ATTEMPTS,
              "cause": "tool_contract" if contract else "stream",
              "message": "The rejected tool call is being corrected from saved progress." if contract else
                         "The response stream ended early. Continuing from saved progress."})
        await before_retry(STREAM_RECOVERY_DELAYS[attempt - 1])
        await start_continuation(TOOL_RECOVERY_PROMPT if contract else RECOVERY_PROMPT)

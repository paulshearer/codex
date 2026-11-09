"""Content-free diagnostics and private inference/transport boundaries."""

import ipaddress
import logging
from urllib.parse import urlsplit


INFERENCE_ENDPOINT = "https://sparky1.taileef59.ts.net:8445/v1"

# HTTP errors/access logs can include URLs, headers, bodies and tracebacks.
HTTP_LOGGER = logging.getLogger("permissive.private_http")
HTTP_LOGGER.disabled = True
HTTP_LOGGER.propagate = False


def http_server_options():
    return {"access_log": None, "logger": HTTP_LOGGER, "debug": False}


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_transport(target):
    """Never send prompts/signatures over plaintext to a remote API."""
    try:
        parsed = urlsplit(str(target))
    except ValueError:
        raise ValueError("Invalid API connection URL") from None
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.scheme not in {"http", "https", "ws", "wss"}
        or (parsed.scheme in {"http", "ws"} and not is_loopback(parsed.hostname))
    ):
        raise ValueError("API connections require TLS or a loopback address, without URL credentials or fragments")
    try:
        parsed.port
    except ValueError:
        raise ValueError("API connection has an invalid port") from None
    return parsed


def validate_inference_endpoint(endpoint, gateway=None):
    parsed = validate_transport(endpoint)
    if gateway is not None and endpoint == gateway and parsed.scheme == 'https' and parsed.path.rstrip('/') == '/v1' and not parsed.query:
        return endpoint
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or (
            not is_loopback(parsed.hostname)
            and (
                parsed.scheme != "https"
                or parsed.hostname != "sparky1.taileef59.ts.net"
                or parsed.port != 8445
            )
        )
    ):
        raise ValueError("Inference must use the private Sparky endpoint or a loopback /v1 endpoint")
    return endpoint


def native_environment(environment):
    # Helpers must not inherit verbose tracing or exporter credentials.
    result = {
        key: value
        for key, value in environment.items()
        if key.upper() not in {"RUST_LOG", "CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"}
        and not key.upper().startswith(("OTEL_", "CODEX_OTEL_", "CODEX_TRACE_"))
    }
    result["RUST_LOG"] = "off"
    # App-server otherwise restores cloud remote-control enrollment from disk.
    result["CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"] = "1"
    # Python tools writing chat exports to a pipe otherwise default to the
    # Windows code page and can fail on emoji or other retained message text.
    for key in list(result):
        if key.upper() in {'PYTHONUTF8', 'PYTHONIOENCODING'}:
            del result[key]
    result['PYTHONUTF8'] = '1'
    result['PYTHONIOENCODING'] = 'utf-8'
    return result


def native_overrides():
    return {
        "analytics.enabled": False,
        "feedback.enabled": False,
        "otel.exporter": "none",
        "otel.trace_exporter": "none",
        "otel.metrics_exporter": "none",
        "otel.log_user_prompt": False,
        "otel.log_agent_responses": False,
        "otel.log_guardian_assessments": False,
        "features.image_generation": False,
        "features.realtime_conversation": False,
    }

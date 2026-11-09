"""Per-task usage from the native conversation's cumulative counters."""

TOKEN_FIELDS = ("cachedInputTokens", "inputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens")


class UsageTracker:
    def __init__(self, previous=None):
        self.baseline = None
        self.started = False
        self.previous = previous or {}

    def observe(self, message):
        method, params = message.get("method"), message.get("params", {})
        if method == "turn/started":
            self.started = True
        if method != "thread/tokenUsage/updated":
            return None
        usage = params.get("tokenUsage", {})
        total, last = usage.get("total", {}), usage.get("last", {})
        if not self.started:
            self.baseline = total.copy()
            return None
        if self.baseline is None:
            self.baseline = {key: value - last.get(key, 0) for key, value in total.items()}
        return {key: self.previous.get(key, 0) + max(0, total.get(key, 0) - self.baseline.get(key, 0))
                for key in TOKEN_FIELDS}

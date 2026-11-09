package com.pshea.permissivecodex.ui

import com.pshea.permissivecodex.data.CompanionEvent
import com.pshea.permissivecodex.data.ConversationSnapshot
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

data class ChatBubble(val id: String, val title: String, val text: String, val user: Boolean = false,
                      val thinking: Boolean = false, val activity: Boolean = false, val recovery: Boolean = false, val error: Boolean = false,
                      val live: Boolean = false, val summary: String = "", val taskId: String = "", val messageId: String? = null, val complete: Boolean = true, val segmentId: String? = null,
                      val replyVersions: JSONObject? = null)
data class ArtifactLink(val taskId: String, val id: String, val name: String, val size: Long, val input: Boolean = false)
data class ConversationPresentation(val bubbles: List<ChatBubble> = emptyList(), val activity: List<String> = emptyList(), val artifacts: List<ArtifactLink> = emptyList(), val statistics: String = "", val tokensPerSecond: Double = 0.0)

internal fun activitySentence(text: String, fallback: String = "Completed a thinking step."): String {
    val plain = text.replace(Regex("\\[([^]]+)]\\([^)]*\\)"), "$1").replace(Regex("\\*\\*(\\S(?:[^*]*?\\S)?)\\*\\*"), "$1").replace("`", "").replace(Regex("^\\s*#{1,6}\\s+"), "").replace(Regex("\\s+"), " ").trim()
    if (plain.isBlank()) return fallback
    val sentence = Regex("^.*?[.!?](?=\\s|$)").find(plain)?.value ?: plain
    if (sentence.length > 220) return sentence.take(217).trimEnd() + "…"
    return if (sentence.last() in ".!?…") sentence else "$sentence."
}

private fun JSONObject.text(key: String): String = if (isNull(key)) "" else optString(key)
private fun JSONArray?.textParts(): String = if (this == null) "" else (0 until length()).map { index ->
    when (val part = opt(index)) { is String -> part; is JSONObject -> part.text("text"); else -> "" }
}.filter(String::isNotBlank).joinToString("\n\n")

/** First-seen positions and final-item reconciliation match desktop/conversation.mjs. */
fun presentConversation(snapshot: ConversationSnapshot?, events: List<CompanionEvent>, now: Double = System.currentTimeMillis() / 1000.0, historyComplete: Boolean = true): ConversationPresentation {
    if (snapshot == null) return ConversationPresentation()
    val bubbles = mutableListOf<ChatBubble>()
    val activity = mutableListOf<String>()
    val artifacts = mutableListOf<ArtifactLink>()
    val statistics = mutableListOf<String>()
    var tokensPerSecond = 0.0
    val orderedEvents = events.distinctBy { it.id }.sortedBy { it.id }
    val eventsByTask = orderedEvents.groupBy { it.taskId }
    snapshot.tasks.forEachIndexed { taskIndex, task ->
        val taskId = task.getString("id")
        val taskEvents = eventsByTask[taskId].orEmpty()
        fun inputs(array: JSONArray?) { if (array != null) for (index in 0 until array.length()) {
            val value = array.opt(index)
            val id = if (value is JSONObject) value.text("id") else value as? String ?: continue
            artifacts += ArtifactLink(taskId, id, (value as? JSONObject)?.text("name")?.ifBlank { null } ?: "Attached file ${id.take(8)}", (value as? JSONObject)?.optLong("size", -1) ?: -1, input = true)
        } }
        inputs(task.optJSONObject("request")?.optJSONArray("attachments"))
        taskEvents.filter { it.kind == "steering" }.forEach { inputs(it.data.optJSONArray("attachments")) }
        task.optJSONObject("request")?.text("prompt")?.takeIf(String::isNotBlank)?.let { bubbles += ChatBubble("$taskId:prompt", "You", it, user = true, taskId = taskId, messageId = "prompt") }
        val timeline = linkedMapOf<String, ChatBubble>()
        val completed = mutableSetOf<String>()
        val toolItems = mutableMapOf<String, JSONObject>()
        val reasoningContent = mutableMapOf<String, java.util.SortedMap<Int, String>>()
        val reasoningSummary = mutableMapOf<String, java.util.SortedMap<Int, String>>()
        val accepted = taskEvents.filter { it.kind == "steering" }.associateBy { it.data.text("submissionId") }
        val nativeUsers = taskEvents.mapNotNull { event ->
            event.data.optJSONObject("params")?.optJSONObject("item")
                ?.takeIf { item -> item.text("type") == "userMessage" }
                ?.let { item -> item.text("clientId").ifBlank { item.text("id") } }
        }.toSet()
        var turnId = ""
        val latestRecovery = taskEvents.lastOrNull { it.kind == "recovery" }?.id
        val latestActivity = taskEvents.lastOrNull { it.kind == "agent" && it.data.optString("method").startsWith("item/") }?.id ?: -1L
        val activeTask = task.text("status") !in setOf("completed", "failed", "cancelled", "interrupted")
        fun user(id: String, text: String, eventId: Long) { if (text.isNotBlank()) timeline.putIfAbsent("$taskId:user:$id", ChatBubble("$taskId:user:$id", "You · steering", text, user = true, taskId = taskId, messageId = eventId.toString())) }
        taskEvents.forEach { event ->
            if (event.kind == "recovery") {
                val phase = event.data.text("phase")
                val attempt = event.data.optInt("attempt", 0)
                val maximum = event.data.optInt("maxAttempts", 0)
                val (title, summary) = when (phase) {
                    "retrying" -> "Recovering response" to "Retrying the response automatically."
                    "recovered" -> "Response recovered" to "The response continued successfully."
                    "exhausted" -> "Response recovery stopped" to "Automatic recovery could not finish the response. You can retry this response."
                    "requested" -> "Retry requested" to "A retry was requested in this chat."
                    "history" -> "Restoring conversation" to ""
                    "history_ready" -> "Conversation restored" to ""
                    else -> "Response recovery" to ""
                }
                val progress = if (phase == "retrying" && attempt > 0) {
                    if (maximum >= attempt) "Attempt $attempt of $maximum." else "Attempt $attempt."
                } else ""
                val message = listOf(summary, progress, event.data.text("message")).filter(String::isNotBlank).joinToString("\n\n")
                activity += "**$title**\n\n$message"
                // Routine setup and completed retries belong in Activity, not
                // permanent chat bubbles when historical events are replayed.
                if (event.id == latestRecovery && (phase == "exhausted" && task.text("status") == "failed" ||
                    phase == "retrying" && activeTask && latestActivity <= event.id)) {
                    timeline["$taskId:recovery:${event.id}"] = ChatBubble("$taskId:recovery:${event.id}", title,
                        if (phase == "retrying") listOf(summary, progress).filter(String::isNotBlank).joinToString(" ") else message,
                        recovery = true, live = phase == "retrying")
                }
                return@forEach
            }
            if (event.kind == "steering") {
                val id = event.data.text("submissionId").ifBlank { event.id.toString() }
                if (id !in nativeUsers) user(id, event.data.text("prompt"), event.id)
                return@forEach
            }
            if (event.kind != "agent") return@forEach
            val method = event.data.text("method")
            val params = event.data.optJSONObject("params") ?: JSONObject()
            val item = params.optJSONObject("item")
            if (method == "turn/started" || method == "turn/completed") completed += timeline.values.filter { it.thinking || it.activity }.map { it.id }
            if (method == "turn/started") turnId = params.optJSONObject("turn")?.text("id").orEmpty()
            val itemId = item?.text("id").orEmpty().ifBlank { params.text("itemId") }.ifBlank { event.id.toString() }
            val key = "$taskId:${params.text("turnId").ifBlank { turnId }}:$itemId"
            if (item?.text("type") == "userMessage") {
                val id = item.text("clientId").ifBlank { itemId }
                val steering = accepted[id]
                val content = item.optJSONArray("content")
                val text = steering?.data?.text("prompt") ?: (0 until (content?.length() ?: 0)).mapNotNull { index -> content?.optJSONObject(index)?.takeIf { it.text("type") == "text" }?.text("text") }.joinToString("\n")
                if (steering != null || item.text("clientId").isNotBlank()) user(id, text, event.id)
                return@forEach
            }
            when {
                method == "item/agentMessage/delta" || item?.text("type") == "agentMessage" -> {
                    val previous = timeline[key] ?: ChatBubble(key, "Codex", "", taskId = taskId, messageId = event.id.toString(), complete = false)
                    if (method == "item/completed") { timeline[key] = previous.copy(text = item?.text("text").orEmpty(), complete = true); completed += key }
                    else if (key !in completed) timeline[key] = previous.copy(text = previous.text + if (method.endsWith("/delta")) params.text("delta") else "")
                }
                method in setOf("item/reasoning/textDelta", "item/reasoning/summaryTextDelta") || item?.text("type") == "reasoning" -> {
                    val content = reasoningContent.getOrPut(key) { sortedMapOf() }
                    val summary = reasoningSummary.getOrPut(key) { sortedMapOf() }
                    if (method == "item/completed") {
                        item?.optJSONArray("content")?.textParts()?.takeIf(String::isNotBlank)?.let { content.clear(); content[0] = it }
                        item?.optJSONArray("summary")?.textParts()?.takeIf(String::isNotBlank)?.let { summary.clear(); summary[0] = it }
                        completed += key
                    } else if (key !in completed) {
                        val raw = method == "item/reasoning/textDelta"
                        val parts = if (raw) content else summary
                        val index = params.optInt(if (raw) "contentIndex" else "summaryIndex", 0)
                        parts[index] = parts[index].orEmpty() + params.text("delta")
                    }
                    val text = content.values.joinToString("\n\n").ifBlank { summary.values.joinToString("\n\n") }
                    timeline[key] = ChatBubble(key, "Thinking", text, thinking = true, summary = activitySentence(summary.values.joinToString("\n\n").ifBlank { text }))
                }
                item?.text("type") in TOOL_TYPES || TOOL_METHOD.containsMatchIn(method) -> {
                    val previous = toolItems.getOrPut(key) { JSONObject() }
                    if (item != null) item.keys().forEach { field -> previous.put(field, item.get(field)) }
                    else if (params.text("delta").isNotEmpty()) previous.put("output", previous.text("aggregatedOutput").ifBlank { previous.text("output") } + params.text("delta")).remove("aggregatedOutput")
                    if (method == "item/completed") completed += key
                    timeline[key] = ChatBubble(key, toolTitle(previous), toolText(previous).ifBlank { if (method == "item/completed") "Completed" else "Working…" }, activity = true, summary = toolSummary(previous, method == "item/completed"))
                }
                method == "error" -> {
                    val message = params.optJSONObject("error")?.text("message") ?: params.text("message")
                    if (message.isNotBlank()) timeline["error:${event.id}"] = ChatBubble("error:${event.id}", "Task error", message, error = true)
                }
                method == "turn/completed" -> {
                    val error = params.optJSONObject("turn")?.opt("error")
                    val message = when (error) { is JSONObject -> error.toString(2); is String -> error; else -> "" }
                    if (message.isNotBlank()) timeline["error:${event.id}"] = ChatBubble("error:${event.id}", "Response error details", message, error = true)
                }
            }
        }
        val activeThinking = timeline.values.lastOrNull { it.thinking }?.id
        bubbles += timeline.values.filter { it.text.isNotBlank() }.map { bubble ->
            bubble.copy(live = activeTask && bubble.id !in completed && (bubble.activity || bubble.thinking && bubble.id == activeThinking || bubble.recovery && bubble.live))
        }
        val result = task.text("result")
        val assistant = timeline.values.filter { !it.user && !it.thinking && !it.activity && !it.recovery && !it.error }
        if (activeTask && result.isBlank() && timeline.values.none { it.text.isNotBlank() })
            bubbles += ChatBubble("$taskId:waiting", "Codex", if (task.text("status") == "queued") "Response queued…" else "Generating response…", live = true, taskId = taskId, complete = false)
        if (result.isNotBlank() && (assistant.isEmpty() || !activeTask && assistant.none { it.complete && it.text == result } && assistant.joinToString("\n\n") { it.text } != result))
            bubbles += ChatBubble("$taskId:result", if (assistant.isEmpty()) "Codex" else "Codex · saved reply", result, taskId = taskId, messageId = "response", complete = !activeTask)
        task.text("error").takeIf(String::isNotBlank)?.let { bubbles += ChatBubble("$taskId:error", "Task error", it, error = true) }
        activity += timeline.values.filter { it.activity || it.recovery || it.error }.map { "**${it.title}**\n\n${it.text}" }
        task.optJSONArray("artifacts")?.let { array -> for (index in 0 until array.length()) {
            val artifact = array.optJSONObject(index) ?: continue
            artifacts += ArtifactLink(taskId, artifact.text("id"), artifact.optString("name", artifact.text("id")), artifact.optLong("size"))
        } }
        val telemetry = taskStatistics(task, taskEvents, now)
        statistics += "### Task ${taskIndex + 1}\n${telemetry.first}"
        tokensPerSecond = telemetry.second
    }
    val segmentId = snapshot.segment?.optString("id")?.takeIf(String::isNotBlank)
    snapshot.replyVersions.filter { it.isNull("messageId") }.forEach { choice ->
        val task = snapshot.tasks.firstOrNull { it.optString("id") == choice.optString("taskId") }
        if (task != null && task.optString("status") in setOf("failed", "cancelled", "interrupted")) {
            bubbles += ChatBubble("attempt:${choice.optString("groupId")}", "Response attempt", "This response ${task.optString("status")}.",
                taskId = task.optString("id"), replyVersions = choice)
        }
    }
    return ConversationPresentation(bubbles.map { bubble ->
        val choice = bubble.replyVersions ?: if (bubble.user || bubble.messageId == null) null else snapshot.replyVersions.lastOrNull {
            it.optString("taskId") == bubble.taskId && (it.optString("messageId") == bubble.messageId || bubble.messageId == "response")
        }
        bubble.copy(id = (segmentId?.let { id -> "$id:" } ?: "") + bubble.id, segmentId = segmentId, replyVersions = choice)
    }, activity, artifacts.distinctBy { if (it.input) "input:${it.id}" else "${it.taskId}:${it.id}" }, statistics.joinToString("\n\n"), tokensPerSecond)
}

private val TOOL_TYPES = setOf("commandExecution", "fileChange", "mcpToolCall", "webSearch", "imageView", "dynamicToolCall")
private val TOOL_METHOD = Regex("^item/(commandExecution|fileChange|mcpToolCall|dynamicToolCall)/")
private fun toolTitle(item: JSONObject): String = when (item.text("type")) {
    "commandExecution" -> "Command execution"
    "fileChange" -> "File changes"
    "mcpToolCall", "dynamicToolCall" -> item.text("tool").ifBlank { item.text("name") }.ifBlank { "Tool call" }
    "webSearch" -> "Web search"
    "imageView" -> "Image view"
    else -> "Tool activity"
}
private fun toolSummary(item: JSONObject, completed: Boolean): String {
    val failed = item.text("status") == "failed" || item.optInt("exitCode", 0) != 0
    val action = when { failed -> "Failed"; completed -> "Completed"; else -> "Working on" }
    return activitySentence(when (item.text("type")) {
        "commandExecution" -> "${if (failed) "Command failed" else if (completed) "Ran" else "Running"}: ${item.text("command").ifBlank { "command" }}"
        "fileChange" -> "$action file changes${item.optJSONArray("changes")?.let { " in ${it.length()} files" }.orEmpty()}"
        else -> "$action ${toolTitle(item).lowercase(Locale.ROOT)}"
    }, "$action tool activity.")
}
private fun toolText(item: JSONObject): String = buildList {
    item.text("command").takeIf(String::isNotBlank)?.let { add("```\n$it\n```") }
    item.text("status").takeIf(String::isNotBlank)?.let { add("Status: $it") }
    item.text("aggregatedOutput").ifBlank { item.text("output") }.takeIf(String::isNotBlank)?.let { add("```\n$it\n```") }
    for (field in listOf("arguments", "result", "content", "changes", "query", "error")) {
        if (!item.isNull(field)) {
            val value = item.get(field)
            val text = when (value) { is JSONObject -> value.toString(2); is JSONArray -> value.toString(2); else -> value.toString() }
            if (text.isNotBlank()) add("${field.replaceFirstChar(Char::uppercase)}\n```\n$text\n```")
        }
    }
}.joinToString("\n\n")

private fun taskStatistics(task: JSONObject, events: List<CompanionEvent>, now: Double): Pair<String, Double> {
    var usage = task.optJSONObject("tokenUsage")
    var started: Double? = null; var first: Double? = null; var compute = 0.0; var activeStart: Double? = null
    val active = mutableSetOf<String>(); val samples = mutableListOf<Pair<Double, Double>>()
    val provider = events.any { it.kind == "inference" }
    events.forEach { event ->
        if (event.kind == "usage") usage = event.data
        if (event.kind == "inference") {
            val timestamp = event.data.optDouble("timestamp", event.createdAt)
            if (event.data.text("phase") == "started" && started == null) started = timestamp
            if (event.data.text("phase") == "output") { if (first == null) first = timestamp; samples += timestamp to event.data.optDouble("estimatedTokens", 0.0) }
        }
        if (event.kind != "agent") return@forEach
        val method = event.data.text("method"); val params = event.data.optJSONObject("params") ?: JSONObject(); val item = params.optJSONObject("item")
        if (!provider && method == "turn/started" && started == null) started = event.createdAt
        if (!provider && method in setOf("item/agentMessage/delta", "item/reasoning/textDelta", "item/reasoning/summaryTextDelta", "item/dynamicToolCall/argumentsDelta") && params.text("delta").isNotEmpty()) {
            if (first == null) first = event.createdAt
            samples += event.createdAt to params.text("delta").length / 4.0
        }
        val key = "${params.text("turnId")}:${item?.text("id")}"
        if (method == "item/started" && item?.text("type") in setOf("commandExecution", "dynamicToolCall", "fileChange")) {
            if (active.isEmpty()) activeStart = event.createdAt
            active += key
        } else if (method == "item/completed" && active.remove(key) && active.isEmpty()) { compute += (event.createdAt - (activeStart ?: event.createdAt)).coerceAtLeast(0.0); activeStart = null }
    }
    val complete = task.text("status") in setOf("completed", "cancelled", "failed", "interrupted")
    val end = if (complete) task.optDouble("completedAt", task.optDouble("updatedAt", now)) else if (task.text("status") == "paused") task.optDouble("updatedAt", now) else now
    if (active.isNotEmpty()) compute += (end - (activeStart ?: end)).coerceAtLeast(0.0)
    val rateEnd = if (complete) samples.lastOrNull()?.first ?: end else end
    val window = maxOf(started ?: task.optDouble("createdAt", rateEnd - 2), rateEnd - 2)
    val rate = if (task.text("status") == "paused") 0.0 else samples.filter { it.first > window && it.first <= rateEnd }.sumOf { it.second } / (rateEnd - window).coerceAtLeast(.25)
    val ttft = first?.let { "%.2f s".format(Locale.ROOT, (it - (started ?: task.optDouble("createdAt", it))).coerceAtLeast(0.0)) } ?: "—"
    val elapsed = (end - task.optDouble("createdAt", end)).coerceAtLeast(0.0)
    return buildString {
        append("| Metric | Value |\n| --- | --- |\n| Elapsed | %.1f s |\n| TTFT | %s |\n| Tool compute time | %.1f s |\n| Recent output rate (2 s estimate) | %.1f tokens/s |".format(Locale.ROOT, elapsed, ttft, compute, rate))
        for ((key, label) in listOf("inputTokens" to "Input tokens", "cachedInputTokens" to "Cached input tokens", "outputTokens" to "Output tokens", "reasoningOutputTokens" to "Reasoning tokens", "totalTokens" to "Total tokens")) append("\n| $label | ${if (usage?.isNull(key) == false) usage?.optLong(key) else "—"} |")
    } to rate
}

package com.pshea.permissivecodex.data

import org.json.JSONObject

data class PairedHost(val installationId: String, val origin: String, val name: String,
                      val keyAlias: String, val deviceId: String, val pairedAt: Long)
data class Selection(val hostId: String? = null, val workspaceId: String? = null, val conversationId: String? = null)
data class Workspace(val id: String, val name: String, val workingDirectory: String, val raw: JSONObject)
data class ConversationSummary(val taskId: String, val conversationId: String, val workspaceId: String,
                               val title: String, val status: String, val updatedAt: Long, val raw: JSONObject)
data class ConversationPage(val data: List<ConversationSummary>, val nextCursor: String?)
data class ConversationSnapshot(val id: String, val tasks: List<JSONObject>, val followups: List<JSONObject>,
                                val queueHeld: Boolean, val eventCursor: Long, val title: String? = null,
                                val segment: JSONObject? = null, val chatGroupId: String = id,
                                val replyVersions: List<JSONObject> = emptyList(), val versionsBusy: Boolean = false,
                                val branchPreparing: Boolean = false, val branchPreparationError: String? = null)

fun nativeChatId(task: JSONObject?): String? = task?.takeUnless { it.isNull("threadId") }
    ?.optString("threadId")?.takeIf { it.isNotBlank() }

fun validChatTitle(value: String): Boolean {
    val title = value.trim()
    return title.isNotEmpty() && title.codePointCount(0, title.length) <= 120 &&
        title.none { it.code < 32 || it.code in 127..159 || it == '\u2028' || it == '\u2029' }
}
data class CompanionEvent(val id: Long, val taskId: String, val kind: String, val data: JSONObject, val createdAt: Double)
data class EventReplayPage(val events: List<CompanionEvent>, val nextCursor: Long)

/** Memory-only, conversation-bound replay buffer. Unknown successor tasks await the next snapshot. */
class ConversationBuffer(val conversationId: String) {
    private val events = sortedMapOf<Long, CompanionEvent>()
    private var taskIds = emptySet<String>()
    var snapshot: ConversationSnapshot? = null
        private set
    @Synchronized fun applySnapshot(value: ConversationSnapshot) {
        require(value.id == conversationId) { "Snapshot belongs to another conversation" }
        if (snapshot != null && value.eventCursor < snapshot!!.eventCursor) return
        require(value.tasks.all { it.optString("conversationId") == conversationId })
        snapshot = value
        taskIds = value.tasks.map { it.getString("id") }.toSet()
    }
    @Synchronized fun accept(value: CompanionEvent) { events.putIfAbsent(value.id, value) }
    @Synchronized fun accept(values: List<CompanionEvent>) { values.forEach(::accept) }
    @Synchronized fun visibleEvents(): List<CompanionEvent> = events.values.filter { it.taskId in taskIds }
    @Synchronized fun lastEventId(): Long = events.lastKeyOrNull() ?: 0L
    private fun <K, V> java.util.SortedMap<K, V>.lastKeyOrNull(): K? = if (isEmpty()) null else lastKey()
}

package com.pshea.permissivecodex.network

import com.pshea.permissivecodex.data.CompanionEvent
import com.pshea.permissivecodex.data.ConversationSnapshot
import com.pshea.permissivecodex.data.EventReplayPage
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.launch
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import org.json.JSONObject

internal fun conversationSnapshot(value: JSONObject): ConversationSnapshot {
    val id = value.getString("id")
    fun objects(key: String): List<JSONObject> = value.optJSONArray(key)?.let { array ->
        (0 until array.length()).map { array.getJSONObject(it) }
    }.orEmpty()
    val tasks = objects("tasks")
    require(tasks.all { it.optString("conversationId") == id }) { "Unexpected conversation tasks." }
    return ConversationSnapshot(id, tasks, objects("followups"), value.optBoolean("queueHeld"),
        value.getLong("eventCursor"), value.optString("title").takeUnless { it.isBlank() || it == "null" }, value.optJSONObject("segment"),
        value.optString("chatGroupId", id), objects("replyVersions"), value.optBoolean("versionsBusy"),
        value.optBoolean("branchPreparing"), value.optString("branchPreparationError").takeUnless { it.isBlank() || it == "null" })
}

/** Subscribe at the snapshot cursor immediately; historical pages cannot hold up new output. */
internal suspend fun replayWhileStreaming(
    snapshot: ConversationSnapshot,
    page: suspend (Long, Long, String?) -> EventReplayPage,
    stream: (Long) -> Flow<CompanionEvent>,
    onReplay: (List<CompanionEvent>) -> Unit,
    onEvent: suspend (CompanionEvent) -> Unit,
): Unit = try { coroutineScope {
    launch {
        val saved = mutableListOf<CompanionEvent>()
        var after = snapshot.segment?.optLong("after") ?: 0L
        while (after < snapshot.eventCursor) {
            val result = page(after, snapshot.eventCursor, snapshot.segment?.optString("id"))
            require(result.nextCursor > after) { "Chat history did not advance." }
            saved += result.events
            after = result.nextCursor
        }
        onReplay(saved)
    }
    stream(snapshot.eventCursor).collect { onEvent(it) }
    throw java.io.IOException("The Windows event stream closed")
} } catch (error: Exception) {
    currentCoroutineContext().ensureActive()
    throw error
}

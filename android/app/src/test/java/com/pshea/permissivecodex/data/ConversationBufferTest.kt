package com.pshea.permissivecodex.data

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ConversationBufferTest {
    private fun snapshot(taskIds: List<String>, cursor: Long) = ConversationSnapshot("chat", taskIds.map {
        JSONObject().put("id", it).put("conversationId", "chat")
    }, emptyList(), false, cursor)
    private fun event(id: Long, task: String) = CompanionEvent(id, task, "agent", JSONObject(), 1.0)
    @Test fun replayAndLiveDuplicatesAreMergedInOrderWithoutLosingSuccessor() {
        val buffer = ConversationBuffer("chat")
        buffer.applySnapshot(snapshot(listOf("first"), 10))
        buffer.accept(listOf(event(5, "first"), event(8, "first"), event(5, "first"), event(11, "successor")))
        assertEquals(listOf(5L, 8L), buffer.visibleEvents().map { it.id })
        buffer.applySnapshot(snapshot(listOf("first", "successor"), 11))
        assertEquals(listOf(5L, 8L, 11L), buffer.visibleEvents().map { it.id })
        buffer.applySnapshot(snapshot(listOf("first"), 10))
        assertEquals(2, buffer.snapshot!!.tasks.size)
        assertEquals(11L, buffer.lastEventId())
    }
    @Test fun anotherConversationCannotReplaceCurrentSnapshot() {
        assertThrows(IllegalArgumentException::class.java) {
            ConversationBuffer("chat").applySnapshot(ConversationSnapshot("other", emptyList(), emptyList(), false, 0))
        }
    }
}

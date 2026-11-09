package com.pshea.permissivecodex.network

import com.pshea.permissivecodex.data.CompanionEvent
import com.pshea.permissivecodex.data.EventReplayPage
import com.pshea.permissivecodex.ui.presentConversation
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ConversationReplayTest {
    private fun replacement(status: String = "running") = conversationSnapshot(JSONObject().apply {
        put("id", "replacement"); put("eventCursor", 40000)
        put("segment", JSONObject("""{"id":"replacement-4","number":4,"after":30000,"previousId":"source-3"}"""))
        put("tasks", JSONArray().apply {
            repeat(43) { index -> put(JSONObject().put("id", "kept-$index").put("conversationId", "replacement")
                .put("status", "completed").put("request", JSONObject().put("prompt", "Earlier prompt $index"))
                .put("result", "Earlier response $index")) }
            put(JSONObject().put("id", "regenerated").put("conversationId", "replacement").put("status", status)
                .put("request", JSONObject().put("prompt", "Regenerate this prompt")))
        })
    })

    @Test fun replacementDisplaysAllCurrentSegmentMessagesBeforeReplay() {
        val snapshot = replacement()
        val presentation = presentConversation(snapshot, emptyList(), historyComplete = false)
        assertEquals(44, snapshot.tasks.size)
        assertEquals(43, presentation.bubbles.count { it.text.startsWith("Earlier response") })
        assertEquals(44, presentation.bubbles.count { it.user })
        assertTrue(presentation.bubbles.last().live)
        assertEquals("Generating response…", presentation.bubbles.last().text)
        assertEquals("source-3", snapshot.segment!!.getString("previousId"))
    }

    @Test fun newResponseArrivesWhileLongSegmentReplayIsBlocked() = runBlocking {
        val snapshot = replacement()
        val releaseHistory = CompletableDeferred<Unit>()
        val historyStarted = CompletableDeferred<Unit>()
        val liveReceived = CompletableDeferred<Unit>()
        val replayReceived = CompletableDeferred<Unit>()
        val collected = sortedMapOf<Long, CompanionEvent>()
        val fresh = CompanionEvent(40001, "regenerated", "agent", JSONObject("""{"method":"item/completed","params":{"turnId":"new-turn","item":{"id":"new-answer","type":"agentMessage","text":"NEW RESPONSE"}}}"""), 1.0)
        val replay = launch {
            replayWhileStreaming(snapshot,
                page = { after, through, segment ->
                    assertEquals(30000L, after); assertEquals(40000L, through); assertEquals("replacement-4", segment)
                    historyStarted.complete(Unit); releaseHistory.await()
                    EventReplayPage(listOf(CompanionEvent(39999, "kept-42", "status", JSONObject(), 0.0)), through)
                },
                stream = { cursor -> flow {
                    assertEquals(40000L, cursor)
                    emit(fresh)
                    awaitCancellation()
                } },
                onReplay = { saved -> saved.forEach { collected[it.id] = it }; replayReceived.complete(Unit) },
                onEvent = { collected[it.id] = it; liveReceived.complete(Unit) })
        }
        try {
            withTimeout(3000) { historyStarted.await(); liveReceived.await() }
            assertFalse(replayReceived.isCompleted)
            val during = presentConversation(snapshot, collected.values.toList(), historyComplete = false)
            assertTrue(during.bubbles.any { it.text == "NEW RESPONSE" })
            assertEquals(43, during.bubbles.count { it.text.startsWith("Earlier response") })
            releaseHistory.complete(Unit)
            withTimeout(3000) { replayReceived.await() }
            assertEquals(setOf(39999L, 40001L), collected.keys)
        } finally { replay.cancelAndJoin() }
    }

    @Test fun cancellingChatCancelsBothReplayAndLiveStream() = runBlocking {
        val historyCancelled = CompletableDeferred<Unit>()
        val liveCancelled = CompletableDeferred<Unit>()
        val historyStarted = CompletableDeferred<Unit>()
        val liveStarted = CompletableDeferred<Unit>()
        val job = launch {
            replayWhileStreaming(replacement(),
                page = { _, _, _ -> try { historyStarted.complete(Unit); awaitCancellation() } finally { historyCancelled.complete(Unit) } },
                stream = { flow { try { liveStarted.complete(Unit); awaitCancellation() } finally { liveCancelled.complete(Unit) } } },
                onReplay = {}, onEvent = {})
        }
        withTimeout(3000) { historyStarted.await(); liveStarted.await() }
        job.cancelAndJoin()
        assertTrue(historyCancelled.isCompleted); assertTrue(liveCancelled.isCompleted)
    }

    @Test fun rejectsReplacementContainingTasksFromAnotherChat() {
        val malformed = JSONObject("""{"id":"replacement","eventCursor":1,"tasks":[{"id":"wrong","conversationId":"source"}]}""")
        assertThrows(IllegalArgumentException::class.java) { conversationSnapshot(malformed) }
    }

    @Test fun deliberateCancellationDoesNotBecomeAnHttp2ResetFailure() = runBlocking {
        val started = CompletableDeferred<Unit>()
        val failure = CompletableDeferred<Throwable>()
        val job = launch {
            try {
                replayWhileStreaming(replacement(), page = { _, through, _ -> EventReplayPage(emptyList(), through) },
                    stream = { flow {
                        started.complete(Unit)
                        try { awaitCancellation() } finally { throw java.io.IOException("stream was reset: CANCEL") }
                    } }, onReplay = {}, onEvent = {})
            } catch (error: Throwable) { failure.complete(error) }
        }
        withTimeout(3000) { started.await() }
        job.cancelAndJoin()
        assertTrue(withTimeout(3000) { failure.await() } is kotlinx.coroutines.CancellationException)
    }

    @Test fun responseVersionsSurviveParsingAndSavedResponseReplay() {
        val value = JSONObject("""{"id":"new-branch","chatGroupId":"family","eventCursor":12,"branchPreparing":true,"versionsBusy":true,
            "tasks":[{"id":"task","conversationId":"new-branch","status":"completed","createdAt":0,"request":{"prompt":"Original prompt"},"result":"New answer"}],
            "replyVersions":[{"groupId":"reply","taskId":"task","messageId":"12","selectedVersionId":"v2","versions":[{"id":"v1","conversationId":"old-branch"},{"id":"v2","conversationId":"new-branch"}]}]}""")
        val snapshot = conversationSnapshot(value)
        assertEquals("family", snapshot.chatGroupId)
        assertTrue(snapshot.branchPreparing)
        assertTrue(snapshot.versionsBusy)
        val bubbles = presentConversation(snapshot, emptyList(), historyComplete = false).bubbles
        val reply = bubbles.single { it.messageId == "response" }
        assertEquals("v2", reply.replyVersions!!.getString("selectedVersionId"))
        assertEquals(2, reply.replyVersions.getJSONArray("versions").length())
    }

    @Test fun cancelledResponseWithoutTextStillOffersItsPreviousVersion() {
        val snapshot = conversationSnapshot(JSONObject("""{"id":"branch","eventCursor":1,
            "tasks":[{"id":"task","conversationId":"branch","status":"cancelled","request":{"prompt":"Try again"},"createdAt":0}],
            "replyVersions":[{"groupId":"reply","taskId":"task","messageId":null,"selectedVersionId":"v2","versions":[{"id":"v1","conversationId":"old"},{"id":"v2","conversationId":"branch"}]}]}"""))
        val reply = presentConversation(snapshot, emptyList()).bubbles.single { it.replyVersions != null }
        assertNull(reply.messageId)
        assertTrue(reply.text.contains("cancelled"))
    }
}

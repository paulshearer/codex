package com.pshea.permissivecodex.ui

import com.pshea.permissivecodex.data.CompanionEvent
import com.pshea.permissivecodex.data.ConversationSnapshot
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ConversationPresentationTest {
    @Test fun segmentIdentitiesKeepActionsSeparateForATurnAcrossCompaction() {
        val base = snapshot()
        val first = base.copy(segment = JSONObject("""{"id":"chat-1","number":1}"""))
        val second = base.copy(segment = JSONObject("""{"id":"chat-2","number":2}"""))
        val earlier = presentConversation(first, emptyList()).bubbles.single()
        val current = presentConversation(second, emptyList()).bubbles.single()
        assertEquals("chat-1", earlier.segmentId)
        assertEquals("chat-2", current.segmentId)
        assertEquals(earlier.taskId, current.taskId)
        assertNotEquals(earlier.id, current.id)
    }
    @Test fun savedFinalReplyAppearsWhileReplayOnlyHasEarlierCommentary() {
        val snapshot = snapshot()
        snapshot.tasks.single().put("result", "Checking the data.\n\nThe complete final answer.")
        val commentary = event(1,"item/completed","""{"item":{"id":"commentary","type":"agentMessage","text":"Checking the data."}}""")
        val partial = presentConversation(snapshot,listOf(commentary),historyComplete=false)
        assertTrue(partial.bubbles.last().text.contains("The complete final answer."))
        assertEquals("response", partial.bubbles.last().messageId)
        val complete = presentConversation(snapshot,listOf(commentary,event(2,"item/completed","""{"item":{"id":"final","type":"agentMessage","text":"The complete final answer."}}""")))
        assertEquals(1,complete.bubbles.count { it.text.contains("The complete final answer.") })
        assertEquals("2",complete.bubbles.last().messageId)
    }
    @Test fun largeCoalescedReplayKeepsFullTextAndStableForkPoint() {
        val text="Retained text ".repeat(10000)
        val delta=CompanionEvent(5,"task","agent",JSONObject().put("method","item/agentMessage/delta").put("params",JSONObject().put("itemId","a").put("delta",text)),5.0)
        val done=CompanionEvent(127005,"task","agent",JSONObject().put("method","item/completed").put("params",JSONObject().put("item",JSONObject().put("id","a").put("type","agentMessage").put("text",text))),10.0)
        val result=presentConversation(snapshot(),listOf(delta,done))
        assertEquals(text,result.bubbles.last().text)
        assertEquals("5",result.bubbles.last().messageId)
        assertTrue(result.bubbles.last().complete)
    }
    @Test fun historyActionsUseFirstEventIdentityAndCompletionAcrossReplay() {
        val values = listOf(event(5, "item/agentMessage/delta", """{"itemId":"answer","delta":"Partial"}"""),
            event(6, "item/completed", """{"item":{"id":"answer","type":"agentMessage","text":"Final"}}"""))
        val partial = presentConversation(snapshot(), values.take(1)).bubbles.last()
        assertEquals("5", partial.messageId)
        assertFalse(partial.complete)
        val bubbles = presentConversation(snapshot(), values + values).bubbles
        assertEquals("prompt", bubbles.first().messageId)
        assertEquals("task", bubbles.last().taskId)
        assertEquals("5", bubbles.last().messageId)
        assertTrue(bubbles.last().complete)
    }
    private fun snapshot() = ConversationSnapshot("chat", listOf(JSONObject("""{"id":"task","conversationId":"chat","request":{"prompt":"Original"},"createdAt":0,"completedAt":10,"status":"completed","tokenUsage":{"totalTokens":20}}""")), emptyList(), false, 100)
    private fun event(id: Long, method: String, params: String, timestamp: Double = id.toDouble()) = CompanionEvent(id, "task", "agent", JSONObject().put("method", method).put("params", JSONObject(params)), timestamp)
    @Test fun steeringStaysBetweenAssistantMessagesAndReplayDoesNotDuplicate() {
        val before = event(1, "item/agentMessage/delta", """{"turnId":"turn","itemId":"before","delta":"Before"}""")
        val steer = CompanionEvent(2, "task", "steering", JSONObject("""{"submissionId":"steer","prompt":"Steering"}"""), 2.0)
        val after = event(3, "item/completed", """{"turnId":"turn","item":{"id":"after","type":"agentMessage","text":"After"}}""")
        assertEquals(listOf("Original", "Before", "Steering", "After"), presentConversation(snapshot(), listOf(after, before, steer, before)).bubbles.map { it.text })
    }
    @Test fun finalTextAndTurnIdsReconcileWithoutCrossTurnCollision() {
        val values = listOf(event(1, "item/agentMessage/delta", """{"turnId":"one","itemId":"same","delta":"par"}"""),
            event(2, "item/completed", """{"turnId":"one","item":{"id":"same","type":"agentMessage","text":"Final one"}}"""),
            event(3, "item/agentMessage/delta", """{"turnId":"one","itemId":"same","delta":"late"}"""),
            event(4, "item/completed", """{"turnId":"two","item":{"id":"same","type":"agentMessage","text":"Final two"}}"""))
        assertEquals(listOf("Original", "Final one", "Final two"), presentConversation(snapshot(), values).bubbles.map { it.text })
    }
    @Test fun nativeSteeringUserIsRenderedExactlyOnce() {
        val steer = CompanionEvent(1, "task", "steering", JSONObject("""{"submissionId":"steer","prompt":"Correct prompt"}"""), 1.0)
        val native = event(2, "item/completed", """{"item":{"id":"u","clientId":"steer","type":"userMessage","content":[{"type":"text","text":"Correct prompt"}]}}""")
        assertEquals(listOf("Original", "Correct prompt"), presentConversation(snapshot(), listOf(steer, native)).bubbles.map { it.text })
    }
    @Test fun toolOutputAndProviderTimingAreReadable() {
        val values = listOf(CompanionEvent(1, "task", "inference", JSONObject("""{"phase":"started","timestamp":1}"""), 1.0),
            CompanionEvent(2, "task", "inference", JSONObject("""{"phase":"output","timestamp":2,"estimatedTokens":8}"""), 2.0),
            event(3, "item/started", """{"turnId":"turn","item":{"id":"tool","type":"commandExecution","command":"dir"}}""", 3.0),
            event(4, "item/completed", """{"turnId":"turn","item":{"id":"tool","type":"commandExecution","command":"dir","aggregatedOutput":"file.txt","status":"completed"}}""", 5.0))
        val result = presentConversation(snapshot(), values, 10.0)
        assertTrue(result.activity.single().contains("file.txt"))
        assertTrue(result.statistics.contains("| TTFT | 1.00 s |"))
        assertTrue(result.statistics.contains("| Tool compute time | 2.0 s |"))
        assertTrue(result.statistics.contains("| Total tokens | 20 |"))
    }
    @Test fun pausedElapsedFreezesAtLastUpdate() {
        val snapshot = snapshot()
        snapshot.tasks.single().put("status", "paused").put("updatedAt", 7)
        assertTrue(presentConversation(snapshot, emptyList(), 100.0).statistics.contains("| Elapsed | 7.0 s |"))
    }

    @Test fun recoveryProgressIsOrderedAndKeepsUnderlyingTurnErrorInspectable() {
        val failure = event(1, "turn/completed", """{"turn":{"id":"broken","status":"failed","error":{"message":"Malformed response stream","code":"stream_decode_error"}}}""")
        val retry = CompanionEvent(2, "task", "recovery", JSONObject("""{"phase":"retrying","attempt":1,"maxAttempts":2,"message":"Continuing the existing response."}"""), 2.0)
        val recovered = CompanionEvent(3, "task", "recovery", JSONObject("""{"phase":"recovered","attempt":1,"maxAttempts":2}"""), 3.0)
        val answer = event(4, "item/completed", """{"turnId":"recovered","item":{"id":"answer","type":"agentMessage","text":"Recovered answer"}}""")
        val result = presentConversation(snapshot(), listOf(answer, recovered, retry, failure, retry))
        assertEquals(listOf("You", "Response error details", "Codex"), result.bubbles.map { it.title })
        assertTrue(result.bubbles.first { it.error }.text.contains("stream_decode_error"))
        assertTrue(result.activity.any { it.contains("Malformed response stream") })
        assertEquals(0, result.bubbles.count { it.recovery })
        assertTrue(result.activity.any { it.contains("Response recovered") })
        assertEquals("Recovered answer", result.bubbles.last().text)
    }

    @Test fun exhaustedRecoveryAndManualRetryAreVisibleWithoutDuplicatingUserPrompt() {
        val exhausted = CompanionEvent(1, "task", "recovery", JSONObject("""{"phase":"exhausted","attempt":2,"maxAttempts":2,"message":"The stream still could not finish."}"""), 1.0)
        val requested = CompanionEvent(2, "task", "recovery", JSONObject("""{"phase":"requested","message":"Retrying the same response."}"""), 2.0)
        val result = presentConversation(snapshot(), listOf(exhausted, requested))
        assertEquals(1, result.bubbles.count { it.user })
        assertTrue(result.bubbles.none { it.recovery })
        assertEquals(2, result.activity.size)
        val failed = snapshot().also { it.tasks.single().put("status", "failed") }
        assertTrue(presentConversation(failed, listOf(exhausted)).bubbles.any { it.recovery && it.text.contains("You can retry this response.") })
    }

    @Test fun routineRestorationNeverLeavesChatCardsOnLiveOrSavedReplay() {
        val restoring = CompanionEvent(1, "task", "recovery", JSONObject("""{"phase":"history","message":"Restoring the retained conversation."}"""), 1.0)
        val ready = CompanionEvent(2, "task", "recovery", JSONObject("""{"phase":"history_ready","message":"The retained conversation is ready."}"""), 2.0)
        val current = snapshot().also { it.tasks.single().put("status", "running") }
        for (events in listOf(listOf(restoring), listOf(restoring, ready))) {
            val presentation = presentConversation(current, events)
            assertTrue(presentation.bubbles.none { it.recovery })
            assertEquals(events.size, presentation.activity.size)
        }
        assertTrue(presentConversation(snapshot(), listOf(restoring, ready)).bubbles.none { it.recovery })
    }

    @Test fun retryIndicatorIsTransientAndHidesTransportDetailsFromChat() {
        val retry = CompanionEvent(1, "task", "recovery", JSONObject("""{"phase":"retrying","attempt":1,"maxAttempts":2,"message":"stream was reset: CANCEL"}"""), 1.0)
        val current = snapshot().also { it.tasks.single().put("status", "running") }
        val waiting = presentConversation(current, listOf(retry))
        assertTrue(waiting.bubbles.any { it.recovery && it.live })
        assertTrue(waiting.bubbles.none { it.text.contains("CANCEL") })
        val answer = event(2, "item/agentMessage/delta", """{"itemId":"answer","delta":"A new response"}""")
        assertTrue(presentConversation(current, listOf(retry, answer)).bubbles.none { it.recovery })
    }

    @Test fun recoveryEventsAndOldErrorsDoNotHideSavedFinalResult() {
        val snapshot = snapshot()
        snapshot.tasks.single().put("result", "Final response after recovery")
        val error = event(1, "error", """{"error":{"message":"A recoverable stream error"}}""")
        val recovered = CompanionEvent(2, "task", "recovery", JSONObject("""{"phase":"recovered"}"""), 2.0)
        val result = presentConversation(snapshot, listOf(error, recovered))
        assertEquals("Final response after recovery", result.bubbles.last().text)
        assertTrue(result.bubbles.any { it.error && it.text.contains("recoverable stream error") })
    }

    @Test fun activeThinkingStreamsAndFinishedStepsHaveOneSentenceSummary() {
        val snapshot = snapshot().also { it.tasks.single().put("status", "running") }
        val values = listOf(event(1, "item/reasoning/textDelta", """{"itemId":"first","delta":"Checking "}"""),
            event(2, "item/reasoning/textDelta", """{"itemId":"first","delta":"the files. More detail."}"""))
        val active = presentConversation(snapshot, values).bubbles.last()
        assertTrue(active.live)
        assertEquals("Checking the files. More detail.", active.text)
        val finished = event(3, "item/completed", """{"item":{"id":"first","type":"reasoning","summary":["Checked the files. Found a repair."],"content":[]}}""")
        val next = event(4, "item/reasoning/textDelta", """{"itemId":"next","delta":"Verifying the repair."}""")
        val bubbles = presentConversation(snapshot, values + finished + next).bubbles.filter { it.thinking }
        assertFalse(bubbles.first().live)
        assertEquals("Checked the files.", bubbles.first().summary)
        assertEquals(active.text, bubbles.first().text)
        assertTrue(bubbles.last().live)
        snapshot.tasks.single().put("status", "completed")
        assertTrue(presentConversation(snapshot, values + finished + next).bubbles.none { it.live })
    }

    @Test fun attachmentsIncludeAllTurnsAndSteeringWithoutReplayDuplicates() {
        val snapshot = snapshot()
        snapshot.tasks.single().put("request", JSONObject("""{"prompt":"Original","attachments":["input"]}"""))
            .put("artifacts", org.json.JSONArray("""[{"id":"output","name":"report.zip","size":123}]"""))
        val steer = CompanionEvent(1, "task", "steering", JSONObject("""{"submissionId":"s","attachments":["input","photo"]}"""), 1.0)
        val files = presentConversation(snapshot, listOf(steer, steer)).artifacts
        assertEquals(3, files.size)
        assertEquals(2, files.count { it.input })
        assertEquals("report.zip", files.single { !it.input }.name)
    }

    @Test fun liveTpsDecaysAndProviderTimingIsNotCountedTwice() {
        val snapshot = snapshot().also { it.tasks.single().put("status", "running") }
        val values = listOf(CompanionEvent(1, "task", "inference", JSONObject("""{"phase":"started","timestamp":1}"""), 1.0),
            CompanionEvent(2, "task", "inference", JSONObject("""{"phase":"output","timestamp":2,"estimatedTokens":8}"""), 2.0),
            event(3, "item/reasoning/textDelta", """{"itemId":"r","delta":"Duplicated output"}""", 2.0))
        assertEquals(8.0, presentConversation(snapshot, values, 2.0).tokensPerSecond, .001)
        assertEquals(0.0, presentConversation(snapshot, values, 5.0).tokensPerSecond, .001)
    }

    @Test fun summariesPreserveFileNamesAndCommandGlobs() {
        assertEquals("Ran check_files.py *.jpg.", activitySentence("Ran `check_files.py *.jpg`. More details."))
        assertEquals("Ran check_files.py **/*.jpg **/*.png.", activitySentence("Ran check_files.py **/*.jpg **/*.png"))
    }
}

package com.pshea.permissivecodex.ui

import com.pshea.permissivecodex.*
import com.pshea.permissivecodex.attachments.DraftAttachment
import com.pshea.permissivecodex.data.BrowserSelection
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class PendingDraftTest {
    @Test fun ackLostRestartPreservesExactPinnedRequest() {
        val attachment = DraftAttachment("local-one", "photo.jpg", byteArrayOf(1, 2), "uploaded-one")
        val pending = PendingSubmission(SendAction.STEER, "Please inspect", listOf("uploaded-one"), "same-uuid", listOf("local-one"), "workspace", "chat", "original-task", "original-turn")
        val restored = SavedDraft.decode(SavedDraft(pending.text, listOf(attachment), pending).encode())
        assertEquals(pending, restored.pending)
        assertTrue(restored.pending!!.matches(SendAction.STEER, "Please inspect", restored.attachments))
        assertEquals("original-turn", restored.pending!!.expectedTurnId)
        assertEquals("original-task", restored.pending!!.taskId)
        assertTrue(restored.attachments.single().bytes.isEmpty())
        assertFalse(restored.attachments.single().needsReselect)
    }
    @Test fun changedPayloadNeverReusesSubmissionId() {
        val pending = PendingSubmission(SendAction.CONTINUE, "hello", emptyList(), "original", emptyList(), "w", "c", "t", null)
        assertFalse(pending.matches(SendAction.CONTINUE, "changed", emptyList()))
        assertFalse(pending.matches(SendAction.QUEUE, "hello", emptyList()))
    }
    @Test fun incompleteUploadRequiresReselectionAfterRestart() {
        val attachments = listOf(DraftAttachment("one", "done.txt", byteArrayOf(1), "staged"), DraftAttachment("two", "missing.txt", byteArrayOf(2)))
        val restored = SavedDraft.decode(SavedDraft("draft", attachments).encode())
        assertEquals("staged", restored.attachments[0].uploadedId)
        assertFalse(restored.attachments[0].needsReselect)
        assertTrue(restored.attachments[1].needsReselect)
    }
    @Test fun plainLegacyDraftStillLoads() { assertEquals("draft", SavedDraft.decode("draft").text) }
    @Test fun browserChoiceStaysPinnedAcrossUncertainDeliveryAndRestart() {
        val selection = BrowserSelection("attached", "msedge", "local-123")
        val pending = PendingSubmission(SendAction.CONTINUE, "inspect", emptyList(), "same-id", emptyList(), "w", "c", "t", null,
            selection.toJson().toString())
        val restored = SavedDraft.decode(SavedDraft("inspect", pending = pending).encode()).pending!!
        assertEquals(selection, BrowserSelection.fromJson(JSONObject(restored.options)))
        assertEquals("same-id", restored.submissionId)
        assertEquals("t", restored.taskId)
        assertEquals(pending.options, restored.options)
    }
    @Test fun attachmentOnlyUsesOneStablePrompt() { assertEquals("Please examine the attached files.", effectivePrompt("", true)); assertEquals("", effectivePrompt("", false)) }
}

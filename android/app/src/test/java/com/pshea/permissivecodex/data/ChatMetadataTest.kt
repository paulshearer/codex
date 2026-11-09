package com.pshea.permissivecodex.data

import com.pshea.permissivecodex.CompanionUiState
import com.pshea.permissivecodex.ui.copyChatId
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ChatMetadataTest {
    @Test fun copyUsesNativeIdOfSelectedOrUnselectedChatAndSurvivesContinuationsOffline() {
        val first = JSONObject("""{"id":"task-1","threadId":"native-abc","conversationId":"chat-a"}""")
        val successor = JSONObject("""{"id":"task-2","threadId":"native-abc","conversationId":"chat-a"}""")
        val other = JSONObject("""{"id":"task-3","threadId":"native-other","conversationId":"chat-b"}""")
        val state = CompanionUiState(connected = false, conversationId = "chat-a",
            snapshot = ConversationSnapshot("chat-a", listOf(first, successor), emptyList(), false, 0, "Renamed"),
            chats = listOf(ConversationSummary("task-3", "chat-b", "workspace", "Other", "completed", 0, other)))
        assertEquals("Renamed", state.chatTitle("chat-a"))
        assertEquals("native-abc", state.chatThreadId("chat-a"))
        assertEquals("native-other", state.chatThreadId("chat-b"))
        var clipboard = "old value"
        assertTrue(copyChatId(state.chatThreadId("chat-a")) { clipboard = it })
        assertEquals("native-abc", clipboard)
        assertTrue(copyChatId(state.chatThreadId("chat-b")) { clipboard = it })
        assertEquals("native-other", clipboard)
        assertFalse(copyChatId(null) { error("Must not write a missing ID") })
        assertFalse(copyChatId("native") { throw IllegalStateException("Clipboard unavailable") })
        assertNull(nativeChatId(JSONObject("""{"threadId":null}""")))
        assertNull(nativeChatId(JSONObject()))
    }

    @Test fun titleValidationCountsUnicodeAndRejectsMultilineAndEmptyNames() {
        assertTrue(validChatTitle("  Project 📝  "))
        assertTrue(validChatTitle("📝".repeat(120)))
        listOf(" ", "x".repeat(121), "📝".repeat(121), "two\nlines", "tab\tinside", "two\u2028lines", "nul\u0000inside").forEach {
            assertFalse(it, validChatTitle(it))
        }
    }
}

package com.pshea.permissivecodex.ui

import android.content.ClipData
import android.content.ClipboardManager
import androidx.compose.foundation.layout.Column
import androidx.compose.runtime.*
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test

class ChatActionsTest {
    @get:Rule val compose = createComposeRule()

    @Test fun renameValidatesAndPreservesInputAfterFailureUntilRetrySucceeds() {
        var attempts = 0
        var saved: String? = null
        compose.setContent {
            var error by remember { mutableStateOf<String?>(null) }
            var shown by remember { mutableStateOf(true) }
            PermissiveTheme { if (shown) RenameChatDialog("Original title", false, error,
                onSave = { attempts++; if (attempts == 1) error = "Windows unavailable" else { saved = it; shown = false } },
                onDismiss = { shown = false }) }
        }
        compose.onNodeWithText("Original title").assertIsDisplayed()
        compose.onNode(hasSetTextAction()).performTextReplacement(" ")
        compose.onNodeWithText("Save").assertIsNotEnabled()
        compose.onNode(hasSetTextAction()).performTextReplacement("x".repeat(121))
        compose.onNodeWithText("Save").assertIsNotEnabled()
        compose.onNode(hasSetTextAction()).performTextReplacement("  New project title 📝  ")
        compose.onNodeWithText("Save").performClick()
        compose.onNodeWithText("Windows unavailable").assertIsDisplayed()
        compose.onNodeWithText("  New project title 📝  ").assertIsDisplayed()
        compose.onNodeWithText("Save").performClick()
        compose.runOnIdle { assertEquals("New project title 📝", saved); assertEquals(2, attempts) }
        compose.onNodeWithText("Rename chat").assertDoesNotExist()
    }

    @Test fun cancelDoesNotSaveAndSavingPreventsRepeatedRequests() {
        var saved = 0
        val saving = mutableStateOf(true)
        var cancelled = 0
        compose.setContent { PermissiveTheme { RenameChatDialog("Title", saving.value, null, { saved++ }, { cancelled++ }) } }
        compose.onNodeWithText("Saving…").assertIsNotEnabled()
        compose.onNodeWithText("Cancel").assertIsNotEnabled()
        compose.runOnIdle { assertEquals(0, saved); saving.value = false }
        compose.onNodeWithText("Cancel").performClick()
        compose.runOnIdle { assertEquals(1, cancelled); assertEquals(0, saved) }
    }

    @Test fun nativeChatIdCopiesToSystemClipboardWhileOffline() {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        var copied = false
        compose.setContent { PermissiveTheme { Column {
            ChatActionItems("native-thread-123", false, {}, {
                copied = copyChatId("native-thread-123") { value ->
                    context.getSystemService(ClipboardManager::class.java).setPrimaryClip(ClipData.newPlainText("Chat ID", value))
                }
            }, {})
        } } }
        compose.onNodeWithText("Rename chat").assertIsNotEnabled()
        compose.onNodeWithText("Copy chat ID").assertIsEnabled().performClick()
        compose.runOnIdle {
            assertTrue(copied)
            assertEquals("native-thread-123", context.getSystemService(ClipboardManager::class.java).primaryClip!!.getItemAt(0).text.toString())
        }
    }

    @Test fun missingNativeIdExplainsWhyCopyIsUnavailable() {
        compose.setContent { PermissiveTheme { Column { ChatActionItems(null, true, {}, { error("Unavailable action ran") }, {}) } } }
        compose.onNodeWithText("Copy chat ID").assertIsNotEnabled()
        compose.onNodeWithText("Available when the task starts").assertIsDisplayed()
    }
}

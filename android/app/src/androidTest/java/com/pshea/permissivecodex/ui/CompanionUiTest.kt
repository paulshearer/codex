package com.pshea.permissivecodex.ui

import androidx.compose.runtime.*
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.ui.Modifier
import android.graphics.Bitmap
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.test.platform.app.InstrumentationRegistry
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import com.pshea.permissivecodex.CompanionUiState
import com.pshea.permissivecodex.SendAction
import com.pshea.permissivecodex.data.BrowserSelection
import com.pshea.permissivecodex.data.ConversationSnapshot
import com.pshea.permissivecodex.data.Workspace
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import androidx.compose.foundation.layout.fillMaxWidth
import com.pshea.permissivecodex.data.CompanionEvent
import androidx.compose.ui.semantics.SemanticsProperties

class CompanionUiTest {
    @get:Rule val compose = createComposeRule()

    @Test fun completedAnswerIsVisibleWhileOldHistoryIsStillLoading() {
        val task=JSONObject("""{"id":"task","conversationId":"chat","request":{"prompt":"Continue after deleting"},"status":"completed","createdAt":0,"result":"Checking.\n\nThe saved final answer is visible."}""")
        val snapshot=ConversationSnapshot("chat",listOf(task),emptyList(),false,127000)
        val partial=CompanionEvent(5,"task","agent",JSONObject("""{"method":"item/completed","params":{"item":{"id":"a","type":"agentMessage","text":"Checking."}}}"""),1.0)
        val state=CompanionUiState(conversationId="chat",snapshot=snapshot,presentation=presentConversation(snapshot,listOf(partial),historyComplete=false))
        compose.setContent { PermissiveTheme { MessageList(state,Modifier.fillMaxSize()) } }
        compose.onNodeWithText("The saved final answer is visible.",substring=true).assertIsDisplayed()
        screenshot("completed-answer-during-history-replay")
    }

    @Test fun savedReplyShowsAllThreeTurnActionsBeforeHistoryFinishesLoading() {
        val task = JSONObject("""{"id":"task","status":"completed","request":{"prompt":"Original prompt"},"result":"Checking.\n\nSaved answer"}""")
        val snapshot = ConversationSnapshot("chat", listOf(task), emptyList(), false, 10000)
        val partial = CompanionEvent(5, "task", "agent", JSONObject("""{"method":"item/completed","params":{"item":{"id":"a","type":"agentMessage","text":"Checking."}}}"""), 1.0)
        val state = CompanionUiState(conversationId = "chat", connected = true, draftReady = true, snapshot = snapshot, presentation = presentConversation(snapshot, listOf(partial), historyComplete = false))
        var delivered: Pair<String, String>? = null
        compose.setContent { PermissiveTheme {
            MessageList(state, Modifier.fillMaxSize(), onTurnAction = { bubble, action, _, complete -> delivered = bubble.messageId!! to action; complete(false) }, onEdit = { _, _, _, _ -> })
        } }
        compose.onAllNodesWithContentDescription("Message actions").onLast().performClick()
        compose.onNodeWithText("Edit").assertIsDisplayed().assertIsEnabled()
        compose.onNodeWithText("Delete").assertIsDisplayed().assertIsEnabled()
        compose.onNodeWithText("Regenerate").assertIsDisplayed().assertIsEnabled()
        screenshot("saved-reply-turn-menu")
        compose.onNodeWithText("Delete").performClick()
        compose.onNodeWithText("Delete only this message?").assertIsDisplayed()
        compose.onNodeWithText("Delete message").performClick()
        compose.runOnIdle { assertEquals("response" to "delete-message", delivered) }
        compose.onNodeWithText("Cancel").performClick()
        compose.onAllNodesWithContentDescription("Message actions").onLast().performClick()
        compose.onNodeWithText("Regenerate").performClick()
        compose.onNodeWithText("Regenerate this turn?").assertDoesNotExist()
        compose.runOnIdle { assertEquals("response" to "regenerate", delivered) }
    }

    private fun screenshot(name: String) {
        val folder = InstrumentationRegistry.getInstrumentation().targetContext.getExternalFilesDir("ui-verification")!!
        java.io.File(folder, "$name.png").outputStream().use { compose.onAllNodes(isRoot()).onLast().captureToImage().asAndroidBitmap().compress(Bitmap.CompressFormat.PNG, 100, it) }
    }

    @Test fun replyVersionsNavigateAndDisableWhilePreparing() {
        val choice = JSONObject("""{"groupId":"reply","selectedVersionId":"v2","versions":[{"id":"v1","conversationId":"old"},{"id":"v2","conversationId":"new"}]}""")
        val snapshot = ConversationSnapshot("new", listOf(JSONObject("""{"id":"task","status":"completed"}""")), emptyList(), false, 3)
        val state = mutableStateOf(CompanionUiState(conversationId = "new", connected = true, draftReady = true, snapshot = snapshot,
            presentation = ConversationPresentation(bubbles = listOf(ChatBubble("reply", "Codex Permissive", "New answer", taskId = "task", messageId = "3", replyVersions = choice)))))
        var selected: String? = null
        compose.setContent { PermissiveTheme { MessageList(state.value, Modifier.fillMaxSize(), onVersion = { selected = it }) } }
        compose.onNodeWithText("Reply 2 of 2").assertIsDisplayed()
        compose.onNodeWithContentDescription("Next response").assertIsNotEnabled()
        compose.onNodeWithContentDescription("Previous response").performClick()
        compose.runOnIdle { assertEquals("old", selected); state.value = state.value.copy(snapshot = snapshot.copy(branchPreparing = true, versionsBusy = true)) }
        compose.onNodeWithContentDescription("Previous response").assertIsNotEnabled()
    }

    @Test fun regenerationFollowsNewReplyThroughReplayAndSegmentPublication() {
        val original = (0..39).map { index -> ChatBubble("old:$index", "Original $index", "Old message $index\n\n" + "Earlier conversation. ".repeat(25), taskId = "old", messageId = "$index") }
        val oldSnapshot = ConversationSnapshot("old", listOf(JSONObject("""{"id":"old","status":"completed"}""")), emptyList(), false, 40)
        val runningSnapshot = ConversationSnapshot("branch", listOf(JSONObject("""{"id":"new","status":"running"}""")), emptyList(), false, 42, branchPreparing = true, versionsBusy = true)
        val reply = ChatBubble("new:answer", "Codex", "New response\n\n" + (1..80).joinToString("\n\n") { "Generated paragraph $it" } + "\n\nGenerated tail", live = true, complete = false, taskId = "new")
        val state = mutableStateOf(CompanionUiState(conversationId = "old", connected = true, draftReady = true, snapshot = oldSnapshot, presentation = ConversationPresentation(bubbles = original)))
        compose.setContent { PermissiveTheme {
            MessageList(state.value, Modifier.fillMaxSize(), onTurnAction = { _, action, _, completed ->
                assertEquals("regenerate", action)
                state.value = state.value.copy(conversationId = "branch", snapshot = runningSnapshot,
                    presentation = ConversationPresentation(bubbles = original.take(30).map { it.copy(id = "branch:${it.id}") } + reply))
                completed(true)
            })
        } }
        compose.onNode(hasScrollAction()).performTouchInput { swipeDown() }
        val initialViewport = compose.onRoot().fetchSemanticsNode().boundsInRoot
        val actions = compose.onAllNodesWithContentDescription("Message actions")
        val visibleAction = actions.fetchSemanticsNodes().indexOfFirst {
            it.boundsInRoot.height > 0 && it.boundsInRoot.top >= initialViewport.top && it.boundsInRoot.bottom <= initialViewport.bottom
        }
        actions[visibleAction].performClick()
        compose.onNodeWithText("Regenerate").performClick()
        compose.onNodeWithText("Generated tail").assertIsDisplayed()
        compose.runOnIdle {
            val replay = (0..99).map { ChatBubble("replay:$it", "Earlier activity", "Retained activity $it", activity = true) }
            state.value = state.value.copy(presentation = ConversationPresentation(bubbles = replay + reply.copy(text = reply.text + "\n\nStreaming tail")))
        }
        compose.onNodeWithText("Streaming tail").assertIsDisplayed()
        compose.onNode(hasScrollAction()).performTouchInput { swipeDown() }
        val viewport = compose.onRoot().fetchSemanticsNode().boundsInRoot
        val reading = compose.onAllNodes(hasText("Generated paragraph", substring = true)).fetchSemanticsNodes().first {
            it.boundsInRoot.height > 0 && it.boundsInRoot.top >= viewport.top && it.boundsInRoot.bottom <= viewport.bottom
        }
        val readingText = reading.config[SemanticsProperties.Text].single().text
        val readingTop = reading.boundsInRoot.top
        compose.runOnIdle {
            val extra = (100..139).map { ChatBubble("replay:$it", "Earlier activity", "Retained activity $it", activity = true) }
            state.value = state.value.copy(snapshot = runningSnapshot.copy(segment = JSONObject("""{"id":"branch-2","number":2}"""), branchPreparing = false, versionsBusy = false),
                presentation = ConversationPresentation(bubbles = (extra + state.value.presentation.bubbles).map { it.copy(id = "branch-2:${it.id}", segmentId = "branch-2", live = false, complete = true) }))
        }
        compose.onNodeWithText(readingText).assertIsDisplayed()
        assertEquals(readingTop.toDouble(), compose.onNodeWithText(readingText).fetchSemanticsNode().boundsInRoot.top.toDouble(), 2.0)
        compose.onNodeWithText("Retained activity 0").assertIsNotDisplayed()
        screenshot("regeneration-keeps-new-response-visible")
    }

    @Test fun generationDoesNotClaimHistoryIsPreparingAndSavingStatusClears() {
        val running = ConversationSnapshot("branch", listOf(JSONObject("""{"id":"reply","status":"running"}""")), emptyList(), false, 2, branchPreparing = true, versionsBusy = true)
        val state = mutableStateOf(CompanionUiState(conversationId = "branch", snapshot = running,
            presentation = ConversationPresentation(bubbles = listOf(ChatBubble("reply", "Codex", "Generating the new reply", live = true)))))
        compose.setContent { PermissiveTheme { MessageList(state.value, Modifier.fillMaxSize()) } }
        compose.onNodeWithText("Preparing response history…").assertDoesNotExist()
        compose.onNodeWithText("Saving reply…").assertDoesNotExist()
        compose.runOnIdle { state.value = state.value.copy(snapshot = running.copy(tasks = listOf(JSONObject("""{"id":"reply","status":"completed"}""")))) }
        compose.waitUntil(4_000) { compose.onAllNodesWithText("Saving reply…").fetchSemanticsNodes().isNotEmpty() }
        compose.onNodeWithText("Saving reply…").assertIsDisplayed()
        compose.runOnIdle { state.value = state.value.copy(snapshot = state.value.snapshot!!.copy(branchPreparing = false)) }
        compose.onNodeWithText("Saving reply…").assertDoesNotExist()
    }

    @Test fun deletionDialogDoesNotDisplayUnrelatedStreamCancellation() {
        val task = JSONObject("""{"id":"task","status":"completed","request":{"prompt":"Prompt"},"result":"Saved answer"}""")
        val state = CompanionUiState(conversationId = "chat", connected = true, draftReady = true,
            error = "stream was reset: CANCEL", snapshot = ConversationSnapshot("chat", listOf(task), emptyList(), false, 1),
            presentation = ConversationPresentation(bubbles = listOf(ChatBubble("answer", "Codex", "Saved answer", taskId = "task", messageId = "response"))))
        compose.setContent { PermissiveTheme { MessageList(state, Modifier.fillMaxSize(), onTurnAction = { _, _, _, _ -> }) } }
        compose.onNodeWithContentDescription("Message actions").performClick()
        compose.onNodeWithText("Delete", useUnmergedTree = true).performClick()
        compose.onNodeWithText("Delete only this message?").assertIsDisplayed()
        compose.onNodeWithText("stream was reset: CANCEL").assertDoesNotExist()
    }

    @Test fun workspaceTileChangesAuthorizationContext() {
        var chosen: String? = null
        compose.setContent { PermissiveTheme { WorkspaceStrip(listOf(Workspace("one", "Code", "C:\\code", JSONObject()), Workspace("two", "Reports", "C:\\reports", JSONObject())), "one", true) { chosen = it } } }
        compose.onNodeWithContentDescription("Workspace Reports").performClick()
        compose.runOnIdle { assertEquals("two", chosen) }
    }

    @Test fun messageEditorRetainsFailedDraftAndSubmissionThenSaves() {
        val bubble = ChatBubble("reply", "Codex Permissive", "Original reply", taskId = "task", messageId = "7")
        val task = JSONObject("""{"id":"task","status":"completed"}""")
        val state = mutableStateOf(CompanionUiState(conversationId = "chat", connected = true, draftReady = true,
            snapshot = ConversationSnapshot("chat", listOf(task), emptyList(), false, 7),
            presentation = ConversationPresentation(bubbles = listOf(bubble))))
        val submissions = mutableListOf<String>()
        compose.setContent { PermissiveTheme {
            MessageList(state.value, Modifier.fillMaxSize(), onEdit = { _, text, submission, completed ->
                submissions.add(submission)
                if (submissions.size == 1) {
                    state.value = state.value.copy(historyError = "Save failed; retry")
                    completed(false)
                } else {
                    state.value = state.value.copy(historyError = null, presentation = ConversationPresentation(bubbles = listOf(bubble.copy(text = text))))
                    completed(true)
                }
            }, onHistoryAction = { _, _, _, _ -> })
        } }
        compose.onNodeWithContentDescription("Message actions").performClick()
        compose.onNodeWithText("Edit").performClick()
        compose.onNodeWithText("Message text").performTextReplacement("Discard me")
        compose.onNodeWithText("Cancel").performClick()
        compose.onNodeWithText("Original reply").assertIsDisplayed()
        compose.onNodeWithContentDescription("Message actions").performClick()
        compose.onNodeWithText("Edit").performClick()
        compose.onNodeWithText("Message text").performTextReplacement("Revised reply")
        compose.onNodeWithText("Save message").performClick()
        compose.onNodeWithText("Save failed; retry").assertIsDisplayed()
        compose.onNodeWithText("Revised reply").assertIsDisplayed()
        compose.onNodeWithText("Save message").performClick()
        compose.onNodeWithText("Revised reply").assertIsDisplayed()
        compose.onNodeWithText("Save message").assertDoesNotExist()
        compose.runOnIdle { assertEquals(2, submissions.size); assertEquals(submissions[0], submissions[1]) }
    }

    @Test fun deletingNearEndKeepsReadingPositionWhenTaskIdsAreReplaced() {
        val task = JSONObject("""{"id":"task","status":"completed"}""")
        val bubbles = (0..39).map { index -> ChatBubble("message$index", "Reply $index", "Message $index\n" + "Retained message content. ".repeat(12), taskId = "task", messageId = "$index") }
        val state = mutableStateOf(CompanionUiState(conversationId = "chat", connected = true, draftReady = true,
            snapshot = ConversationSnapshot("chat", listOf(task), emptyList(), false, 40), presentation = ConversationPresentation(bubbles = bubbles)))
        compose.setContent { PermissiveTheme {
            MessageList(state.value, Modifier.fillMaxSize(), onHistoryAction = { bubble, deleting, _, completed ->
                if (deleting) {
                    val retained = bubbles.takeWhile { it.id != bubble.id }.map { it.copy(id = "replacement:${it.id}", taskId = "replacement") }
                    state.value = state.value.copy(presentation = ConversationPresentation(bubbles = retained))
                    completed(true)
                }
            })
        } }
        compose.waitForIdle()
        compose.onNodeWithText("Reply 39").assertIsDisplayed()
        compose.onAllNodesWithContentDescription("Message actions").onLast().performClick()
        compose.onNodeWithText("Delete from here").performClick()
        compose.onNodeWithText("Delete from here").performClick()
        compose.waitForIdle()
        compose.onNodeWithText("Reply 0").assertIsNotDisplayed()
        compose.onNodeWithText("Reply 38").assertIsDisplayed()
        screenshot("deletion-keeps-reading-position")
    }

    @Test fun disconnectedComposerKeepsDraftAndCannotSend() {
        var sent: SendAction? = null
        compose.setContent { PermissiveTheme { Composer(CompanionUiState(workspaceId = "one", draft = "Preserved draft", draftReady = true), false, false, {}, { sent = it }, {}, {}, {}) } }
        compose.onNodeWithText("Preserved draft").assertIsDisplayed()
        compose.onNodeWithContentDescription("Send").assertIsNotEnabled()
        compose.runOnIdle { assertNull(sent) }
    }

    @Test fun plusMenuOpensGenericSecureCredentials() {
        var opened = false
        compose.setContent { PermissiveTheme { Composer(
            CompanionUiState(workspaceId = "one", connected = true, draftReady = true), false, false,
            {}, {}, {}, {}, {}, onCredentials = { opened = true }) } }
        compose.onNodeWithContentDescription("Chat options").performClick()
        compose.onNodeWithText("Secure credentials").assertIsDisplayed()
        compose.onAllNodes(hasText("Bitwarden", substring = true, ignoreCase = true)).assertCountEquals(0)
        compose.onNodeWithText("Secure credentials").performClick()
        compose.runOnIdle { assertEquals(true, opened) }
    }

    @Test fun activeChatQueuesByDefaultAndAllowsExplicitSteering() {
        var sent: SendAction? = null
        val snapshot = ConversationSnapshot("chat", listOf(JSONObject("""{"id":"task","conversationId":"chat","request":{},"status":"running"}""")), emptyList(), false, 0)
        compose.setContent { PermissiveTheme { Composer(CompanionUiState(workspaceId = "one", conversationId = "chat", snapshot = snapshot, draft = "Follow up", connected = true, draftReady = true), true, false, {}, { sent = it }, {}, {}, {}) } }
        compose.onNodeWithText("Queue follow-up").performClick()
        compose.onNodeWithText("Steer active turn").performClick()
        compose.onNodeWithContentDescription("Steer").performClick()
        compose.runOnIdle { assertEquals(SendAction.STEER, sent) }
    }

    @Test fun markdownTableCellsRemainVisibleAndSelectable() {
        compose.setContent { PermissiveTheme { MarkdownContent("| Item | State |\n| --- | --- |\n| Windows | Running |") } }
        compose.onNodeWithText("Windows").assertIsDisplayed()
        compose.onNodeWithText("Running").assertIsDisplayed()
    }

    @Test fun chatOptionsOpensRemoteBrowserPickerAndSavesSelectedSignedInProfile() {
        var saved: BrowserSelection? = null
        val catalog = JSONObject("""{"data":[{"id":"chrome","installed":true,"installedProfiles":[{"id":"local-work","name":"Work","account":"work@example.com","connected":true}]}],"connections":[]}""")
        compose.setContent {
            var showBrowser by remember { mutableStateOf(false) }
            var browser by remember { mutableStateOf(BrowserSelection()) }
            PermissiveTheme {
                Composer(CompanionUiState(workspaceId = "one", connected = true, draftReady = true,
                    browserOverride = browser, browserCatalog = catalog), false, false, {}, {}, {}, {}, {},
                    onBrowser = { showBrowser = true })
                if (showBrowser) BrowserSelectionDialog(browser, catalog, false, null, {},
                    onSave = { saved = it; browser = it; showBrowser = false }, onDismiss = { showBrowser = false })
            }
        }
        compose.onNodeWithContentDescription("Chat options").performClick()
        compose.onNodeWithText("Browser and profile").performClick()
        compose.onNodeWithText("Remote browser").assertIsDisplayed()
        compose.onNodeWithContentDescription("Choose Browser mode").performClick()
        compose.onNodeWithText("Existing signed-in browser").performClick()
        compose.onNodeWithContentDescription("Choose Browser profile").performClick()
        compose.onNodeWithText("Work · work@example.com (connected)").performClick()
        compose.onNodeWithText("Save browser").performClick()
        compose.runOnIdle { assertEquals(BrowserSelection("attached", "chrome", "local-work"), saved) }
        compose.onNodeWithText("Remote browser").assertDoesNotExist()
        compose.onNodeWithText("Google Chrome · Work · work@example.com (connected)").assertIsDisplayed()
    }

    @Test fun browserChangeIsDisabledForSavedDeliveryToPreserveRetryOptions() {
        var opened = 0
        compose.setContent { PermissiveTheme {
            Composer(CompanionUiState(workspaceId = "one", draft = "Saved message", connected = true, draftReady = true,
                pendingAction = SendAction.SEND), false, false, {}, {}, {}, {}, {}, onBrowser = { opened++ })
        } }
        compose.onNodeWithContentDescription("Chat options").performClick()
        compose.onNodeWithText("Browser and profile").assertIsNotEnabled()
        compose.runOnIdle { assertEquals(0, opened) }
    }

    @Test fun selectedChatWaitsForSnapshotBeforeSendingOrChangingItsBrowser() {
        var sent: SendAction? = null
        var browserOpened = 0
        val snapshot = mutableStateOf<ConversationSnapshot?>(null)
        compose.setContent { PermissiveTheme {
            Composer(CompanionUiState(workspaceId = "one", conversationId = "chat", snapshot = snapshot.value,
                draft = "Follow up", connected = true, draftReady = true), true, false, {}, { sent = it }, {}, {}, {},
                onBrowser = { browserOpened++ })
        } }
        compose.onNodeWithContentDescription("Queue").assertIsNotEnabled()
        compose.onNodeWithText("Bundled Chromium · Conversation profile").assertIsNotEnabled()
        compose.onNodeWithContentDescription("Chat options").performClick()
        compose.onNodeWithText("Browser and profile").assertIsNotEnabled()
        compose.runOnIdle {
            assertNull(sent)
            assertEquals(0, browserOpened)
            snapshot.value = ConversationSnapshot("chat", listOf(JSONObject("""{"id":"task","conversationId":"chat","request":{"browserMode":"attached","browser":"msedge","browserProfile":"local-work"},"status":"running"}""")), emptyList(), false, 0)
        }
        compose.onNodeWithText("Browser and profile").assertIsEnabled().performClick()
        compose.onNodeWithText("Microsoft Edge · local-work (disconnected)").assertIsEnabled()
        compose.onNodeWithContentDescription("Queue").assertIsEnabled().performClick()
        compose.runOnIdle {
            assertEquals(1, browserOpened)
            assertEquals(SendAction.QUEUE, sent)
        }
    }

    @Test fun deletionRequiresExplicitClickAndHasNoTypedConfirmation() {
        var deleted = 0
        compose.setContent { PermissiveTheme {
            SecureDeletionDialog("chat", "Example chat", false, null, onDelete = { deleted++ }, onDismiss = {})
        } }
        compose.onNodeWithText("Example chat").assertIsDisplayed()
        compose.onNodeWithText("Delete chat permanently?").assertIsDisplayed()
        compose.onAllNodes(hasSetTextAction()).assertCountEquals(0)
        compose.onNodeWithText("DELETE").assertDoesNotExist()
        compose.runOnIdle { assertEquals(0, deleted) }
        compose.onNodeWithText("Delete permanently").assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(1, deleted) }
    }

    @Test fun keepChatClosesConfirmationWithoutDeleting() {
        var deleted = 0
        compose.setContent {
            var shown by remember { mutableStateOf(true) }
            PermissiveTheme {
                if (shown) SecureDeletionDialog("chat", "Example chat", false, null,
                    onDelete = { deleted++ }, onDismiss = { shown = false })
            }
        }
        compose.onNodeWithText("Keep chat").performClick()
        compose.onNodeWithText("Delete chat permanently?").assertDoesNotExist()
        compose.runOnIdle { assertEquals(0, deleted) }
    }

    @Test fun deletingDisablesRepeatedDeletionAndDismissal() {
        compose.setContent { PermissiveTheme {
            SecureDeletionDialog("workspace", "Example workspace", true, null, onDelete = {}, onDismiss = {})
        } }
        compose.onNodeWithText("Deleting on Windows…").assertIsDisplayed()
        compose.onNodeWithText("Delete permanently").assertIsNotEnabled()
        compose.onNodeWithText("Keep workspace").assertIsNotEnabled()
    }

    @Test fun deletionErrorRemainsVisibleAndAllowsExplicitRetry() {
        var attempts = 0
        val error = "Finish or cancel active chats before deleting."
        compose.setContent { PermissiveTheme {
            SecureDeletionDialog("workspace", "Example workspace", false, error,
                onDelete = { attempts++ }, onDismiss = {})
        } }
        compose.onNodeWithText(error).assertIsDisplayed()
        compose.onNodeWithText("Delete permanently").assertIsEnabled().performClick()
        compose.onNodeWithText("Keep workspace").assertIsEnabled()
        compose.runOnIdle { assertEquals(1, attempts) }
    }

    @Test fun retryResponseControlsExistingTaskWithoutSubmittingComposerDraft() {
        var control: String? = null
        var sent: SendAction? = null
        val task = JSONObject("""{"id":"task","conversationId":"chat","request":{"prompt":"Original message"},"status":"failed","canRetry":true}""")
        val snapshot = ConversationSnapshot("chat", listOf(task), emptyList(), false, 0)
        compose.setContent { PermissiveTheme {
            Column {
                RetryResponseAction(task, true) { control = it }
                Composer(CompanionUiState(workspaceId = "one", conversationId = "chat", snapshot = snapshot,
                    draft = "My unsent follow-up", connected = true, draftReady = true), false, true,
                    {}, { sent = it }, {}, {}, {})
            }
        } }
        compose.onNodeWithText("Retry response").performClick()
        compose.onNodeWithText("My unsent follow-up").assertIsDisplayed()
        compose.runOnIdle {
            assertEquals("retry", control)
            assertNull(sent)
            assertEquals("chat", snapshot.id)
            assertEquals("task", snapshot.tasks.single().getString("id"))
        }
    }

    @Test fun retryResponseAppearsOnlyForRecoverableFailureAndRespectsBusyState() {
        val task = mutableStateOf(JSONObject("""{"status":"failed","canRetry":false}"""))
        val enabled = mutableStateOf(false)
        var controls = 0
        compose.setContent { PermissiveTheme { RetryResponseAction(task.value, enabled.value) { controls++ } } }
        compose.onNodeWithText("Retry response").assertDoesNotExist()
        compose.runOnIdle { task.value = JSONObject("""{"status":"running","canRetry":true}""") }
        compose.onNodeWithText("Retry response").assertDoesNotExist()
        compose.runOnIdle { task.value = JSONObject("""{"status":"completed","canRetry":true}""") }
        compose.onNodeWithText("Retry response").assertDoesNotExist()
        compose.runOnIdle { task.value = JSONObject("""{"status":"failed","canRetry":true}""") }
        compose.onNodeWithText("Retry response").assertIsNotEnabled()
        compose.runOnIdle { assertEquals(0, controls); enabled.value = true }
        compose.onNodeWithText("Retry response").assertIsEnabled().performClick()
        compose.runOnIdle { assertEquals(1, controls) }
    }

    @Test fun activeThinkingIsVisibleThenCollapsesToASentenceWhenFinished() {
        val bubble = mutableStateOf(ChatBubble("step", "Thinking", "Checking the files. More details.", thinking = true, live = true))
        compose.setContent { PermissiveTheme { Column(Modifier.fillMaxSize()) {
            MessageList(CompanionUiState(presentation = ConversationPresentation(bubbles = listOf(bubble.value))), Modifier.weight(1f))
        } } }
        compose.onNodeWithText("Thinking · live").assertIsDisplayed()
        compose.onNodeWithText("Checking the files. More details.").assertIsDisplayed()
        compose.onNodeWithText("View details").assertDoesNotExist()
        screenshot("active-thinking")
        compose.runOnIdle { bubble.value = bubble.value.copy(text = "Checked all files. Full transcript remains available.", live = false, summary = "Checked all files.") }
        compose.onNodeWithText("Checked all files.").assertIsDisplayed()
        compose.onNodeWithText("Checked all files. Full transcript remains available.").assertDoesNotExist()
        screenshot("completed-thinking")
        compose.onNodeWithText("View details").performClick()
        compose.onNodeWithText("Checked all files. Full transcript remains available.").assertIsDisplayed()
        compose.onNodeWithText("Hide details").performClick()
        compose.onNodeWithText("Checked all files. Full transcript remains available.").assertDoesNotExist()
    }

    @Test fun countedAttachmentsStayHiddenUntilOpenedAndCanBeSaved() {
        var saved: ArtifactLink? = null
        val files = listOf(ArtifactLink("task", "zip", "Report.zip", 123), ArtifactLink("task", "photo", "Photo.png", 456, input = true))
        compose.setContent { PermissiveTheme { ConversationAttachments(files, true, {}, { saved = it }) } }
        compose.onNodeWithText("Attachments (2)").assertIsDisplayed()
        compose.onNodeWithText("Report.zip").assertDoesNotExist()
        compose.onNodeWithText("Attachments (2)").performClick()
        compose.onNodeWithText("Report.zip").assertIsDisplayed().performClick()
        compose.runOnIdle { assertEquals(files.first(), saved) }
        compose.onNodeWithText("Photo.png").assertIsDisplayed()
        compose.onNodeWithText("Close attachments").performClick()
        compose.onNodeWithText("Report.zip").assertDoesNotExist()
    }

    @Test fun prominentStatusIncludesTpsAndUsesDoneAndFailedLabels() {
        val status = mutableStateOf("running")
        compose.setContent { PermissiveTheme { TaskStatus(status.value, 12.3) } }
        compose.onNodeWithText("Running (~12.3 TPS)").assertIsDisplayed()
        compose.runOnIdle { status.value = "completed" }
        compose.onNodeWithText("Done").assertIsDisplayed()
        compose.runOnIdle { status.value = "failed" }
        compose.onNodeWithText("Failed").assertIsDisplayed()
        compose.onNodeWithText("Running (~12.3 TPS)").assertDoesNotExist()
    }
}

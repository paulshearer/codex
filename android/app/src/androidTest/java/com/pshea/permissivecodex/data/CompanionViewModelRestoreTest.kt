package com.pshea.permissivecodex.data

import android.app.Application
import android.content.Context
import android.content.ContextWrapper
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.ViewModelStore
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.pshea.permissivecodex.CompanionViewModel
import com.pshea.permissivecodex.PendingSubmission
import com.pshea.permissivecodex.SavedDraft
import com.pshea.permissivecodex.SendAction
import com.pshea.permissivecodex.network.DeviceKeys
import com.pshea.permissivecodex.network.MobileSigning
import kotlinx.coroutines.delay
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID
import kotlinx.coroutines.flow.MutableStateFlow
import com.pshea.permissivecodex.CompanionUiState
import org.json.JSONObject

/** Exercises the real ViewModel startup and background callbacks, without starting any connection. */
@RunWith(AndroidJUnit4::class)
class CompanionViewModelRestoreTest {
    @Test fun deletedRootClearsOldReplayAndSavedFinalReplyRendersBeforeHistoryCompletes() = runBlocking {
        val context=ApplicationProvider.getApplicationContext<Context>()
        val directory=File(context.cacheDir,"history-restore-${UUID.randomUUID()}").apply { mkdirs() }
        val application=IsolatedApplication(context,directory)
        val owner=ViewModelStore()
        val instrumentation=InstrumentationRegistry.getInstrumentation()
        lateinit var model: CompanionViewModel
        try {
            instrumentation.runOnMainSync {
                model=ViewModelProvider(owner,ViewModelProvider.AndroidViewModelFactory(application))[CompanionViewModel::class.java]
                model.onBackground()
            }
            val stateField=CompanionViewModel::class.java.getDeclaredField("_state").apply { isAccessible=true }
            @Suppress("UNCHECKED_CAST") val state=stateField.get(model) as MutableStateFlow<CompanionUiState>
            val eventField=CompanionViewModel::class.java.getDeclaredField("events").apply { isAccessible=true }
            @Suppress("UNCHECKED_CAST") val events=eventField.get(model) as MutableMap<Long,CompanionEvent>
            val oldTask=JSONObject("""{"id":"old-root","conversationId":"chat","request":{"prompt":"Deleted prompt"},"status":"completed","createdAt":0}""")
            val replacement=JSONObject("""{"id":"replacement-root","conversationId":"chat","request":{"prompt":"Retained prompt"},"status":"completed","createdAt":0,"result":"Commentary.\n\nFINAL-ANDROID-ANSWER"}""")
            val old=ConversationSnapshot("chat",listOf(oldTask),emptyList(),false,127000)
            val fresh=ConversationSnapshot("chat",listOf(replacement),emptyList(),false,254000)
            val apply=CompanionViewModel::class.java.getDeclaredMethod("applySnapshot",ConversationSnapshot::class.java,java.lang.Long.TYPE).apply { isAccessible=true }
            val render=CompanionViewModel::class.java.getDeclaredMethod("scheduleRender").apply { isAccessible=true }
            instrumentation.runOnMainSync {
                state.value=state.value.copy(conversationId="chat",snapshot=old,
                    presentation=com.pshea.permissivecodex.ui.presentConversation(old,emptyList()))
                events[126999]=CompanionEvent(126999,"old-root","agent",JSONObject(),1.0)
                assertEquals(true,apply.invoke(model,fresh,0L))
                assertTrue(events.isEmpty())
                events[127001]=CompanionEvent(127001,"replacement-root","agent",JSONObject("""{"method":"item/completed","params":{"item":{"id":"commentary","type":"agentMessage","text":"Commentary."}}}"""),1.0)
                CompanionViewModel::class.java.getDeclaredField("replayLoading").apply { isAccessible=true }.setBoolean(model,true)
                render.invoke(model)
            }
            val rendered=withTimeout(5000) { model.state.first { value -> value.presentation.bubbles.any { it.text.contains("FINAL-ANDROID-ANSWER") } } }
            assertFalse(rendered.presentation.bubbles.any { it.text.contains("Deleted prompt") })
            assertEquals("response", rendered.presentation.bubbles.last().messageId)
            instrumentation.runOnMainSync {
                assertEquals(false,apply.invoke(model,old,0L))
                assertEquals("replacement-root",model.state.value.snapshot!!.tasks.first().getString("id"))
            }
        } finally {
            instrumentation.runOnMainSync { owner.clear() }
            directory.deleteRecursively()
        }
    }
    private class IsolatedApplication(base: Context, private val directory: File) : Application() {
        init { attachBaseContext(object : ContextWrapper(base) {}) }
        override fun getNoBackupFilesDir(): File = File(directory, "no-backup").apply { mkdirs() }
        override fun getCacheDir(): File = File(directory, "cache").apply { mkdirs() }
    }

    @Test fun offlineStartupAndImmediateBackgroundPreservePendingDraftThenAllowOfflineEditing() = runBlocking {
        val realContext = ApplicationProvider.getApplicationContext<Context>()
        val directory = File(realContext.cacheDir, "viewmodel-restore-${UUID.randomUUID()}").apply { mkdirs() }
        val application = IsolatedApplication(realContext, directory)
        val installationId = UUID.randomUUID().toString()
        val keys = DeviceKeys()
        val alias = keys.ensure(installationId)
        val host = PairedHost(installationId, "https://offline-host.tail123.ts.net:9444", "Offline saved host", alias,
            MobileSigning.fingerprint(keys.publicKey(alias)), System.currentTimeMillis() / 1000)
        val pending = PendingSubmission(SendAction.CONTINUE, "Unsent offline draft", emptyList(), UUID.randomUUID().toString(),
            emptyList(), "saved-workspace", "saved-conversation", "frozen-task", null)
        val saved = SavedDraft("Unsent offline draft", emptyList(), pending)
        val conversationId = requireNotNull(pending.conversationId)
        val owner = ViewModelStore()
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        lateinit var viewModel: CompanionViewModel
        try {
            val store = CompanionStore(application)
            store.saveHost(host)
            store.select(Selection(installationId, pending.workspaceId, pending.conversationId))
            store.writeDraft(installationId, conversationId, saved.encode())

            // Run both in one main-thread turn: restoration must not need a successful network call,
            // and a background callback arriving during its IO must never replace the saved text.
            instrumentation.runOnMainSync {
                viewModel = ViewModelProvider(owner, ViewModelProvider.AndroidViewModelFactory(application))[CompanionViewModel::class.java]
                viewModel.onBackground()
            }
            val restored = withTimeout(10_000) {
                viewModel.state.first { it.draftReady && it.draft == saved.text && it.pendingAction == pending.action }
            }
            assertFalse(restored.connected)
            assertEquals(installationId, restored.host?.installationId)
            assertEquals(pending.workspaceId, restored.workspaceId)
            assertEquals(pending.conversationId, restored.conversationId)
            withContext(Dispatchers.Main) {
                assertFalse(viewModel.renameConversation(installationId, conversationId, "Offline rename"))
                assertNotNull(viewModel.state.value.renameError)
                assertFalse(viewModel.state.value.renaming)
                assertEquals(saved.text, viewModel.state.value.draft)
            }
            val persisted = SavedDraft.decode(CompanionStore(application).readDraft(installationId, conversationId))
            assertEquals(saved.text, persisted.text)
            assertEquals(pending, persisted.pending)

            // The user can work on an unsent draft while Windows/Tailscale is unavailable.
            val editedText = "Edited while the Windows host remains offline"
            instrumentation.runOnMainSync { viewModel.setDraft(editedText) }
            val edited = withTimeout(10_000) {
                var value: SavedDraft
                do {
                    delay(50)
                    value = SavedDraft.decode(CompanionStore(application).readDraft(installationId, conversationId))
                } while (value.text != editedText)
                value
            }
            assertEquals(editedText, viewModel.state.value.draft)
            assertNull(edited.pending)
            assertFalse(viewModel.state.value.connected)
            val encrypted = File(application.noBackupFilesDir, "companion-state.bin").readBytes().toString(Charsets.ISO_8859_1)
            assertFalse(encrypted.contains(editedText))
            assertFalse(encrypted.contains(pending.submissionId))
        } finally {
            instrumentation.runOnMainSync { owner.clear() }
            keys.delete(alias)
            directory.deleteRecursively()
        }
    }
}

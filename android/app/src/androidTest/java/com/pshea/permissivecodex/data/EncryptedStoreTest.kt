package com.pshea.permissivecodex.data

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.pshea.permissivecodex.network.DeviceKeys
import com.pshea.permissivecodex.network.MobileSigning
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class EncryptedStoreTest {
    @Test fun hostPreferencesAndUnsentDraftAreEncryptedAndCanBeReopened() = runBlocking {
        // Isolated Context paths avoid touching an enrolled app's existing settings.
        val base = ApplicationProvider.getApplicationContext<Context>()
        val directory = File(base.cacheDir, "store-test-${UUID.randomUUID()}").apply { mkdirs() }
        val context = object : android.content.ContextWrapper(base) { override fun getNoBackupFilesDir(): File = directory }
        val installation = UUID.randomUUID().toString()
        val keys = DeviceKeys(); val alias = keys.ensure(installation)
        val host = PairedHost(installation, "https://windows.tail123.ts.net:9444", "Private Windows host", alias,
            MobileSigning.fingerprint(keys.publicKey(alias)), 123)
        val secretDraft = "PRIVATE UNSENT DRAFT ${UUID.randomUUID()}"
        try {
            val store = CompanionStore(context)
            store.saveHost(host); store.select(Selection(installation, "workspace", "conversation"))
            store.writeDraft(installation, "conversation", secretDraft)
            val browser = BrowserSelection("attached", "msedge", "local-private-profile")
            store.writeBrowserSelection(installation, "conversation", browser)
            val key = java.security.KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
                .getKey("permissive.storage.aes.v1", null) as javax.crypto.SecretKey
            val info = javax.crypto.SecretKeyFactory.getInstance(key.algorithm, "AndroidKeyStore")
                .getKeySpec(key, android.security.keystore.KeyInfo::class.java) as android.security.keystore.KeyInfo
            assertEquals(256, info.keySize)
            val bytes = File(directory, "companion-state.bin").readBytes().toString(Charsets.ISO_8859_1)
            assertFalse(bytes.contains(secretDraft)); assertFalse(bytes.contains(host.origin)); assertFalse(bytes.contains(alias))
            assertFalse(bytes.contains(browser.browserProfile))
            val reopened = CompanionStore(context)
            assertEquals(listOf(host), reopened.hosts.value)
            assertEquals(secretDraft, reopened.readDraft(installation, "conversation"))
            assertEquals("workspace", reopened.selection.value.workspaceId)
            assertEquals(browser, reopened.readBrowserSelection(installation, "conversation"))
            reopened.writeDraft(installation, "other-chat", "Retained draft")
            reopened.writeBrowserSelection(installation, "other-chat", BrowserSelection())
            assertFalse(reopened.removeDraftIfMatches(installation, "conversation", "older envelope"))
            assertEquals(secretDraft, reopened.readDraft(installation, "conversation"))
            assertTrue(reopened.removeDraftIfMatches(installation, "conversation", secretDraft))
            assertEquals("", reopened.readDraft(installation, "conversation"))
            reopened.writeDraft(installation, "conversation", secretDraft)
            reopened.removeContexts(installation, listOf("conversation"))
            assertNull(reopened.readBrowserSelection(installation, "conversation"))
            assertEquals("", reopened.readDraft(installation, "conversation"))
            assertEquals("Retained draft", reopened.readDraft(installation, "other-chat"))
            assertEquals(BrowserSelection(), reopened.readBrowserSelection(installation, "other-chat"))
            reopened.removeHost(installation)
            assertTrue(reopened.hosts.value.isEmpty()); assertEquals("", reopened.readDraft(installation, "conversation"))
            assertNull(reopened.selection.value.hostId)
            assertNull(reopened.readBrowserSelection(installation, "other-chat"))
            var staleRejected = false
            try { reopened.writeDraft(installation, "conversation", "stale background save") }
            catch (_: IllegalArgumentException) { staleRejected = true }
            assertTrue(staleRejected)
            assertEquals("", CompanionStore(context).readDraft(installation, "conversation"))
        } finally { keys.delete(alias); directory.deleteRecursively() }
    }
}

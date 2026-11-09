package com.pshea.permissivecodex.data

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.AtomicFile
import com.pshea.permissivecodex.network.DeviceKeys
import com.pshea.permissivecodex.network.TailnetPolicy
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Only enrolled hosts, UI selections and unsent text drafts persist. No task/event/history cache. */
class CompanionStore(context: Context) {
    private val file = AtomicFile(File(context.noBackupFilesDir, "companion-state.bin"))
    private val mutex = Mutex()
    private var state = readState()
    private val mutableHosts = MutableStateFlow(readHosts(state))
    private val mutableSelection = MutableStateFlow(readSelection(state))
    val hosts: StateFlow<List<PairedHost>> = mutableHosts.asStateFlow()
    val selection: StateFlow<Selection> = mutableSelection.asStateFlow()

    suspend fun saveHost(host: PairedHost) = update {
        TailnetPolicy.origin(host.origin)
        require(host.keyAlias == DeviceKeys().alias(host.installationId))
        val values = readHosts(it).filterNot { old -> old.installationId == host.installationId } + host
        it.put("hosts", JSONArray(values.map(::hostJson)))
    }
    suspend fun removeHost(id: String) = update {
        val old = readHosts(it).firstOrNull { host -> host.installationId == id }
        if (old != null) DeviceKeys().delete(old.keyAlias)
        it.put("hosts", JSONArray(readHosts(it).filterNot { host -> host.installationId == id }.map(::hostJson)))
        val drafts = it.optJSONObject("drafts") ?: JSONObject()
        drafts.keys().asSequence().filter { key -> key.startsWith("$id:") }.toList().forEach(drafts::remove)
        it.put("drafts", drafts)
        val browsers = it.optJSONObject("browserSelections") ?: JSONObject()
        browsers.keys().asSequence().filter { key -> key.startsWith("$id:") }.toList().forEach(browsers::remove)
        it.put("browserSelections", browsers)
        val branches = it.optJSONObject("replyBranches") ?: JSONObject()
        branches.keys().asSequence().filter { key -> key.startsWith("$id:") }.toList().forEach(branches::remove)
        it.put("replyBranches", branches)
        if (readSelection(it).hostId == id) it.put("selection", JSONObject())
    }
    suspend fun select(selection: Selection) = update {
        it.put("selection", JSONObject().put("hostId", selection.hostId).put("workspaceId", selection.workspaceId)
            .put("conversationId", selection.conversationId))
    }
    suspend fun readReplyBranch(hostId: String, groupId: String): String? = mutex.withLock {
        state.optJSONObject("replyBranches")?.optString("$hostId:$groupId")?.takeIf { it.isNotBlank() }
    }
    suspend fun writeReplyBranch(hostId: String, groupId: String, branchId: String) = withContext(Dispatchers.IO) {
        mutex.withLock {
            if (readHosts(state).none { it.installationId == hostId } || state.optJSONObject("replyBranches")?.optString("$hostId:$groupId") == branchId) return@withLock
            val next = JSONObject(state.toString())
            val branches = next.optJSONObject("replyBranches") ?: JSONObject()
            branches.put("$hostId:$groupId", branchId)
            next.put("replyBranches", branches)
            writeState(next); state = next
        }
    }
    suspend fun readDraft(hostId: String, draftId: String): String = withContext(Dispatchers.IO) {
        mutex.withLock { state.optJSONObject("drafts")?.optString("$hostId:$draftId", "") ?: "" }
    }
    suspend fun writeDraft(hostId: String, draftId: String, text: String) = update {
        require(readHosts(it).any { host -> host.installationId == hostId }) { "This Windows host was forgotten. Its draft was not saved." }
        // A 4 MiB prompt may appear twice in the retry envelope and JSON-escape twice.
        require(text.toByteArray().size <= MAX_DRAFT_BYTES) { "Draft envelope exceeds the storage limit." }
        val drafts = it.optJSONObject("drafts") ?: JSONObject()
        if (text.isEmpty()) drafts.remove("$hostId:$draftId") else drafts.put("$hostId:$draftId", text)
        it.put("drafts", drafts)
    }
    suspend fun removeDraft(hostId: String, draftId: String) = writeDraft(hostId, draftId, "")
    suspend fun readBrowserSelection(hostId: String, draftId: String): BrowserSelection? = withContext(Dispatchers.IO) {
        mutex.withLock { state.optJSONObject("browserSelections")?.optJSONObject("$hostId:$draftId")?.let(BrowserSelection::fromJson) }
    }
    suspend fun writeBrowserSelection(hostId: String, draftId: String, selection: BrowserSelection) = update {
        require(readHosts(it).any { host -> host.installationId == hostId }) { "This Windows host was forgotten." }
        val browsers = it.optJSONObject("browserSelections") ?: JSONObject()
        browsers.put("$hostId:$draftId", selection.toJson()); it.put("browserSelections", browsers)
    }
    suspend fun removeContexts(hostId: String, draftIds: List<String>) = update {
        for (field in listOf("drafts", "browserSelections")) {
            val values = it.optJSONObject(field) ?: continue
            draftIds.forEach { draftId -> values.remove("$hostId:$draftId") }
        }
        val branches = it.optJSONObject("replyBranches")
        branches?.keys()?.asSequence()?.filter { key -> key.startsWith("$hostId:") && branches.optString(key) in draftIds }?.toList()?.forEach(branches::remove)
    }
    /** Clear an acknowledged envelope only if a newer edit has not replaced it. */
    suspend fun removeDraftIfMatches(hostId: String, draftId: String, expected: String): Boolean = withContext(Dispatchers.IO) {
        mutex.withLock {
            val key = "$hostId:$draftId"
            if (state.optJSONObject("drafts")?.optString(key, "") != expected) return@withLock false
            val next = JSONObject(state.toString())
            next.optJSONObject("drafts")?.remove(key)
            writeState(next); state = next
            true
        }
    }
    private suspend fun update(change: (JSONObject) -> Unit) = withContext(Dispatchers.IO) {
        mutex.withLock {
            val next = JSONObject(state.toString()); change(next); writeState(next); state = next
            mutableHosts.value = readHosts(next); mutableSelection.value = readSelection(next)
        }
    }
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setKeySize(256)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true).build()); generateKey()
        }
    }
    private fun readState(): JSONObject {
        if (!file.baseFile.exists()) return JSONObject().put("version", 1)
        try {
            val bytes = file.openRead().use { input ->
                require(input.channel.size() <= MAX_STATE_BYTES + 64L) { "Encrypted settings exceed the size limit." }; input.readBytes()
            }
            require(bytes.size > 29 && bytes[0] == 1.toByte())
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, bytes.copyOfRange(1, 13)))
            cipher.updateAAD(AAD)
            return JSONObject(cipher.doFinal(bytes.copyOfRange(13, bytes.size)).toString(Charsets.UTF_8)).also { require(it.getInt("version") == 1) }
        } catch (_: Exception) { throw IllegalStateException("Encrypted host settings could not be opened. Clear app storage and pair again.") }
    }
    private fun writeState(value: JSONObject) {
        val plaintext = value.toString().toByteArray(Charsets.UTF_8)
        require(plaintext.size <= MAX_STATE_BYTES) { "Remove unused drafts before saving more." }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding"); cipher.init(Cipher.ENCRYPT_MODE, key()); cipher.updateAAD(AAD)
        val bytes = byteArrayOf(1) + cipher.iv + cipher.doFinal(plaintext)
        val output = file.startWrite()
        try { output.write(bytes); file.finishWrite(output) } catch (error: Exception) { file.failWrite(output); throw error }
    }
    private fun readHosts(value: JSONObject): List<PairedHost> {
        val list = value.optJSONArray("hosts") ?: return emptyList()
        return (0 until list.length()).map { index -> list.getJSONObject(index).let {
            PairedHost(it.getString("installationId"), it.getString("origin"), it.getString("name"),
                it.getString("keyAlias"), it.getString("deviceId"), it.getLong("pairedAt"))
        } }
    }
    private fun hostJson(host: PairedHost) = JSONObject().put("installationId", host.installationId).put("origin", host.origin)
        .put("name", host.name).put("keyAlias", host.keyAlias).put("deviceId", host.deviceId).put("pairedAt", host.pairedAt)
    private fun readSelection(value: JSONObject): Selection {
        val selected = value.optJSONObject("selection") ?: return Selection()
        fun field(key: String) = selected.optString(key).takeIf { it.isNotEmpty() && it != "null" }
        return Selection(field("hostId"), field("workspaceId"), field("conversationId"))
    }
    companion object {
        internal const val MAX_DRAFT_BYTES = 64 * 1024 * 1024
        internal const val MAX_STATE_BYTES = 128 * 1024 * 1024
        private const val KEY_ALIAS = "permissive.storage.aes.v1"
        private val AAD = "permissive-companion-settings/v1".toByteArray(Charsets.US_ASCII)
    }
}

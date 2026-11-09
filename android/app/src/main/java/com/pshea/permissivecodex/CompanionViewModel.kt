package com.pshea.permissivecodex

import android.app.Application
import android.net.Uri
import android.os.Build
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.pshea.permissivecodex.attachments.AttachmentReader
import com.pshea.permissivecodex.attachments.DraftAttachment
import com.pshea.permissivecodex.data.*
import com.pshea.permissivecodex.network.*
import com.pshea.permissivecodex.ui.ArtifactLink
import com.pshea.permissivecodex.ui.ConversationPresentation
import com.pshea.permissivecodex.ui.presentConversation
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import org.json.JSONObject
import java.util.UUID

data class CompanionUiState(
    val hosts: List<PairedHost> = emptyList(), val host: PairedHost? = null,
    val workspaces: List<Workspace> = emptyList(), val workspaceId: String? = null,
    val chats: List<ConversationSummary> = emptyList(), val nextCursor: String? = null,
    val conversationId: String? = null, val snapshot: ConversationSnapshot? = null,
    val previousSegmentId: String? = null, val oldestSegmentNumber: Int = 1, val loadingSegment: Boolean = false,
    val presentation: ConversationPresentation = ConversationPresentation(),
    val draft: String = "", val attachments: List<DraftAttachment> = emptyList(),
    val pendingAction: SendAction? = null,
    val browserOverride: BrowserSelection? = null,
    val browserCatalog: JSONObject? = null, val browsersLoading: Boolean = false, val browserError: String? = null,
    val deleteError: String? = null,
    val historyError: String? = null, val connectionError: String? = null,
    val renaming: Boolean = false, val renameError: String? = null,
    val draftReady: Boolean = false,
    val connected: Boolean = false, val busy: Boolean = false, val importing: Boolean = false,
    val status: String = "Pair with your Windows install", val error: String? = null,
) {
    fun chatTitle(id: String?): String? = snapshot?.takeIf { it.id == id }?.title
        ?: chats.firstOrNull { it.conversationId == id }?.title
    fun chatThreadId(id: String?): String? = if (snapshot?.id == id && snapshot != null)
        nativeChatId(snapshot.tasks.lastOrNull()) else nativeChatId(chats.firstOrNull { it.conversationId == id }?.raw)
    val browserSelection: BrowserSelection get() = BrowserSelection.resolve(
        workspaces.firstOrNull { it.id == workspaceId }?.raw,
        snapshot?.tasks?.lastOrNull()?.optJSONObject("request"), browserOverride?.toJson())
}

enum class SendAction { SEND, CONTINUE, STEER, QUEUE }

class CompanionViewModel(application: Application) : AndroidViewModel(application) {
    private val store = CompanionStore(application)
    private val keys = DeviceKeys()
    private val reader = AttachmentReader(application)
    private val tunnel = TailnetRecovery(application)
    private val _state = MutableStateFlow(CompanionUiState())
    val state = _state.asStateFlow()
    private var connectionJob: Job? = null
    private var streamJob: Job? = null
    private var streamRevision = 0L
    private var snapshotRefreshJob: Job? = null
    private var snapshotRefreshRequested = false
    private var pollingJob: Job? = null
    private var draftJob: Job? = null
    private var renderJob: Job? = null
    private var renderRequested = 0L
    private var replayLoading = false
    private var client: CompanionClient? = null
    private val events = sortedMapOf<Long, CompanionEvent>()
    private val olderSegments = mutableListOf<Pair<ConversationSnapshot, List<CompanionEvent>>>()
    private var segmentLoadToken: Any? = null
    private var segmentLoadGeneration = -1L
    fun loadPreviousSegment() {
        val current = _state.value; val api = client ?: return
        val previous = current.previousSegmentId ?: return
        val chatId = current.conversationId ?: return
        if (current.loadingSegment) return
        val epoch = generation
        val token = Any()
        segmentLoadToken = token
        segmentLoadGeneration = epoch
        _state.update { it.copy(loadingSegment = true) }
        viewModelScope.launch {
            try {
                val snapshot = api.getConversation(chatId, previous)
                val saved = mutableListOf<CompanionEvent>()
                var after = snapshot.segment?.optLong("after") ?: 0L
                while (after < snapshot.eventCursor) {
                    val page = api.replayEvents(chatId, after, snapshot.eventCursor, previous)
                    saved += page.events
                    if (page.nextCursor <= after) break
                    after = page.nextCursor
                }
                if (epoch != generation || _state.value.conversationId != chatId) return@launch
                olderSegments.add(0, snapshot to saved)
                _state.update { it.copy(previousSegmentId = snapshot.segment?.optString("previousId")?.takeIf { id -> id.isNotBlank() && id != "null" }, oldestSegmentNumber = snapshot.segment?.optInt("number", 1) ?: 1) }
                scheduleRender()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { if (epoch == generation) reportError(failure.message ?: "Previous segment could not be loaded") }
            finally { if (segmentLoadToken === token) { segmentLoadToken = null; _state.update { it.copy(loadingSegment = false) } } }
        }
    }
    private val attachmentMetadata = mutableMapOf<String, JSONObject>()
    private var foreground = false
    private var generation = 0L
    private var draftRevision = 0L
    private var cameraUri: Uri? = null
    private var cameraFile: java.io.File? = null
    private var pendingSubmission: PendingSubmission? = null
    private var maintenanceRunning = false
    private var historyRunning = false
    private var browserRequest = 0L
    private var titleEpoch = 0L
    private var renameRequest = 0L

    init {
        viewModelScope.launch {
            store.hosts.collect { hosts ->
                _state.update { old -> old.copy(hosts = hosts) }
                if (_state.value.host == null && hosts.isNotEmpty()) {
                    val selection = store.selection.value
                    val chosen = hosts.firstOrNull { it.installationId == selection.hostId } ?: hosts.first()
                    _state.update { it.copy(host = chosen, workspaceId = selection.workspaceId, conversationId = selection.conversationId, draftReady = false) }
                    safely { loadDraft() }
                    if (foreground && _state.value.draftReady) connect()
                }
            }
        }
        // Camera files left by a killed import are transient, never chat history.
        java.io.File(application.cacheDir, "camera").listFiles()?.forEach { it.delete() }
    }

    fun onForeground() {
        foreground = true
        if (!_state.value.connected) connect() else {
            if (streamJob?.isActive != true) _state.value.conversationId?.let { startConversation(it) }
            startPolling()
        }
    }
    fun onBackground() {
        foreground = false; connectionJob?.cancel(); stopConversationStream(); pollingJob?.cancel(); renderJob?.cancel()
        _state.update { it.copy(connected = false, status = "Reconnect when the app opens") }
        persistDraft()
    }
    fun clearError() { _state.update { it.copy(error = null, connectionError = null) } }
    fun reportError(message: String) { _state.update { it.copy(error = message) } }

    fun pair(raw: String) {
        if (_state.value.busy) return
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null, status = "Pairing over Tailscale…") }
            try {
                val invitation = PairingInvitation.parse(raw)
                ensureTunnel()
                val host = CompanionClient.enroll(invitation, "${Build.MANUFACTURER} ${Build.MODEL}", keys)
                store.saveHost(host)
                chooseHost(host)
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { _state.update { it.copy(error = error.message ?: "Pairing failed", status = "Pairing was not completed") } }
            finally { _state.update { it.copy(busy = false) } }
        }
    }

    fun chooseHost(host: PairedHost) {
        renameRequest++
        _state.update { it.copy(renaming = false, renameError = null) }
        browserRequest++
        persistDraft(); generation++; connectionJob?.cancel(); stopConversationStream(); pollingJob?.cancel(); renderJob?.cancel(); events.clear()
        client = null; pendingSubmission = null
        draftRevision++
        _state.update { it.copy(host = host, workspaceId = null, conversationId = null, workspaces = emptyList(), chats = emptyList(), snapshot = null, previousSegmentId = null, oldestSegmentNumber = 1, loadingSegment = false, presentation = ConversationPresentation(), draft = "", attachments = emptyList(), pendingAction = null, browserOverride = null, browserCatalog = null, browsersLoading = false, browserError = null, draftReady = false, connected = false, error = null) }
        viewModelScope.launch { safely { store.select(Selection(host.installationId, null, null)); loadDraft(); if (_state.value.draftReady) connect() } }
    }

    fun forgetHost(host: PairedHost) {
        viewModelScope.launch {
            if (_state.value.host?.installationId == host.installationId) {
                browserRequest++
                generation++; stopConversationStream(); connectionJob?.cancel(); pollingJob?.cancel(); client = null; events.clear()
                _state.value = CompanionUiState(hosts = _state.value.hosts.filterNot { it.installationId == host.installationId })
            }
            store.removeHost(host.installationId)
        }
    }

    fun connect() {
        if (maintenanceRunning || historyRunning) return
        val host = _state.value.host ?: return
        if (!_state.value.draftReady) return
        if (connectionJob?.isActive == true) return
        val epoch = generation
        connectionJob = viewModelScope.launch {
            _state.update { it.copy(connected = false, status = "Connecting over Tailscale…", error = null) }
            try {
                ensureTunnel()
                val api = CompanionClient(host, keys)
                api.getIdentity()
                val workspaces = api.getWorkspaces()
                if (epoch != generation) return@launch
                client = api
                val workspace = _state.value.workspaceId?.takeIf { id -> workspaces.any { it.id == id } } ?: workspaces.firstOrNull()?.id
                if (workspace != _state.value.workspaceId) {
                    // The old selection may name a deleted workspace. Its draft belongs to that
                    // exact context and must never follow the fallback workspace into a send.
                    val previous = _state.value; val pending = pendingSubmission
                    draftJob?.cancel()
                    _state.update { it.copy(draftReady = false) }
                    try { saveDraft(previous, pending) }
                    catch (failure: Exception) { if (epoch == generation) _state.update { it.copy(draftReady = true) }; throw failure }
                    if (epoch != generation || client !== api) return@launch
                    generation++; draftRevision++; stopConversationStream(); renderJob?.cancel(); events.clear(); pendingSubmission = null
                    _state.update { it.copy(connected = true, workspaces = workspaces, workspaceId = workspace, conversationId = null, snapshot = null, previousSegmentId = null, oldestSegmentNumber = 1, loadingSegment = false,
                        presentation = ConversationPresentation(), draft = "", attachments = emptyList(), pendingAction = null, browserOverride = null, draftReady = false, status = "${host.name} · Tailscale") }
                    // Draft restoration must finish even if the activity backgrounds and cancels
                    // this connection check. It performs no network work until the draft is ready.
                    viewModelScope.launch { safely { saveSelection(); loadDraft(); if (foreground) { refreshChats(); startPolling() } } }
                    return@launch
                }
                _state.update { it.copy(connected = true, workspaces = workspaces, status = "${host.name} · Tailscale") }
                refreshChats()
                _state.value.conversationId?.let { startConversation(it) }
                startPolling()
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) {
                currentCoroutineContext().ensureActive()
                if (epoch == generation) _state.update { it.copy(connected = false, status = "Windows host unavailable",
                    connectionError = if (error is java.io.IOException) "Could not reach Windows. Check Tailscale and retry." else error.message ?: "Could not connect") }
            }
        }
    }

    private suspend fun ensureTunnel() {
        // Tailnet-only DNS and server identity checks remain mandatory even with a running VPN.
        tunnel.ensureConnected { detail -> _state.update { it.copy(status = detail) } }
    }

    fun chooseWorkspace(id: String) {
        if (!_state.value.draftReady || _state.value.busy) return
        if (_state.value.workspaceId == id) return
        persistDraft(); generation++; stopConversationStream(); renderJob?.cancel(); events.clear(); pendingSubmission = null
        draftRevision++
        _state.update { it.copy(workspaceId = id, conversationId = null, snapshot = null, previousSegmentId = null, oldestSegmentNumber = 1, loadingSegment = false, presentation = ConversationPresentation(), chats = emptyList(), draft = "", attachments = emptyList(), pendingAction = null, browserOverride = null, draftReady = false, error = null) }
        viewModelScope.launch { safely { saveSelection(); loadDraft(); refreshChats() } }
    }
    fun chooseConversation(id: String?) {
        if (!_state.value.draftReady || _state.value.busy) return
        persistDraft(); generation++; draftRevision++; stopConversationStream(); renderJob?.cancel(); events.clear(); olderSegments.clear(); pendingSubmission = null
        _state.update { it.copy(conversationId = id, snapshot = null, previousSegmentId = null, oldestSegmentNumber = 1, loadingSegment = false, presentation = ConversationPresentation(), draft = "", attachments = emptyList(), pendingAction = null, browserOverride = null, draftReady = false, error = null) }
        viewModelScope.launch { safely { saveSelection(); loadDraft(); if (id != null) startConversation(id) } }
    }
    fun selectReplyVersion(id: String) {
        if (_state.value.snapshot?.versionsBusy == true || _state.value.snapshot?.branchPreparing == true) return
        chooseConversation(id)
    }
    fun chooseChatSummary(id: String) {
        val current = _state.value
        val summary = current.chats.firstOrNull { it.conversationId == id }
        val host = current.host ?: return
        val epoch = generation
        viewModelScope.launch {
            val group = summary?.raw?.optString("chatGroupId", id) ?: id
            val saved = store.readReplyBranch(host.installationId, group)
            val target = if (saved != null && saved != id) {
                try { client?.getConversation(saved); saved }
                catch (cancelled: CancellationException) { throw cancelled }
                catch (_: Exception) { id }
            } else id
            if (epoch == generation) chooseConversation(target)
        }
    }
    fun editHistory(chatId: String, taskId: String, messageId: String, submissionId: String, deleting: Boolean, onComplete: (Boolean) -> Unit, text: String? = null, operation: String? = null, segmentId: String? = null) {
        val current = _state.value; val api = client ?: return; val epoch = generation
        if (historyRunning || current.busy || !current.connected || current.conversationId != chatId || !current.draftReady || current.pendingAction != null) return
        if (current.snapshot?.tasks?.any { it.optString("status") !in setOf("completed", "failed", "cancelled", "interrupted") } != false) return
        historyRunning = true
        // The accepted edit retires this source chat. Its stream must not report
        // a spurious 410 or replace the connection while we adopt the successor.
        val previousStream = streamJob; val previousPolling = pollingJob; val previousConnection = connectionJob
        stopConversationStream(); pollingJob?.cancel(); connectionJob?.cancel()
        _state.update { it.copy(busy = true, error = null, historyError = null) }
        viewModelScope.launch {
            var applied = false
            try {
                previousStream?.join(); previousPolling?.join(); previousConnection?.join()
                val result = api.editHistory(chatId, taskId, messageId, submissionId, deleting, text, operation, segmentId)
                if (epoch != generation || client !== api) return@launch
                applied = true
                onComplete(true)
                if (result.id != chatId) {
                    // Use the POST response directly. Do not clear the transcript
                    // and wait for a second fetch and a complete history replay.
                    val presentation = withContext(Dispatchers.Default) { presentConversation(result, emptyList(), historyComplete = false) }
                    if (epoch != generation || client !== api || _state.value.conversationId != chatId) return@launch
                    persistDraft(); generation++; draftRevision++
                    renderJob?.cancel(); events.clear(); olderSegments.clear(); pendingSubmission = null
                    replayLoading = true
                    _state.update { it.copy(conversationId = result.id, snapshot = result, presentation = presentation,
                        previousSegmentId = result.segment?.optString("previousId")?.takeIf { id -> id.isNotBlank() && id != "null" },
                        oldestSegmentNumber = result.segment?.optInt("number", 1) ?: 1, loadingSegment = false,
                        busy = false, connected = true, draftReady = deleting || operation != null, error = null) }
                    // Start output before persistence or chat-list refresh can delay it.
                    startConversation(result.id, result)
                    current.host?.let { host ->
                        if (deleting || operation != null) {
                            store.writeDraft(host.installationId, result.id, SavedDraft(current.draft, current.attachments, null).encode())
                            current.browserOverride?.let { store.writeBrowserSelection(host.installationId, result.id, it) }
                        }
                    }
                    saveSelection()
                    if (!deleting && operation == null) loadDraft()
                } else {
                    applySnapshot(result, titleEpoch)
                    _state.update { it.copy(busy = false) }
                    scheduleRender(); startConversation(result.id, result)
                }
                try { refreshChats() } catch (error: Exception) { currentCoroutineContext().ensureActive() }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                currentCoroutineContext().ensureActive()
                if (epoch == generation && client === api) {
                    if (!applied) {
                        _state.update { it.copy(historyError = if (failure is java.io.IOException && failure !is CompanionException) "The change could not be confirmed. Retry to check the same request." else failure.message ?: "Chat history could not be changed. Try again.") }
                        onComplete(false); startConversation(chatId)
                    }
                }
            } finally {
                historyRunning = false
                if (epoch == generation && _state.value.host?.installationId == current.host?.installationId) _state.update { it.copy(busy = false) }
                if (foreground && !_state.value.connected) connect()
                else if (foreground) startPolling()
            }
        }
    }
    private suspend fun saveSelection() {
        val s = _state.value
        store.select(Selection(s.host?.installationId, s.workspaceId, s.conversationId))
        val snapshot = s.snapshot
        if (s.host != null && snapshot != null) store.writeReplyBranch(s.host.installationId, snapshot.chatGroupId, snapshot.id)
    }
    private fun draftId(s: CompanionUiState) = s.conversationId ?: "new:${s.workspaceId.orEmpty()}"
    fun setDraft(text: String) {
        if (!_state.value.draftReady || _state.value.workspaceId == null || _state.value.busy) return
        draftRevision++
        if (pendingSubmission?.text != text) pendingSubmission = null
        _state.update { it.copy(draft = text, pendingAction = pendingSubmission?.action) }
        draftJob?.cancel()
        draftJob = viewModelScope.launch { delay(350); safely { saveDraft(_state.value) } }
    }
    private fun persistDraft() { draftJob?.cancel(); val value = _state.value; val pending = pendingSubmission; viewModelScope.launch { safely { saveDraft(value, pending) } } }
    private suspend fun saveDraft(s: CompanionUiState, pending: PendingSubmission? = pendingSubmission) {
        if (!s.draftReady) return
        s.host?.let { store.writeDraft(it.installationId, draftId(s), SavedDraft(s.draft, s.attachments, pending).encode()) }
    }
    private suspend fun loadDraft() {
        val current = _state.value; val host = current.host ?: return
        val epoch = generation; val revision = draftRevision
        val encoded = store.readDraft(host.installationId, draftId(current))
        val browser = store.readBrowserSelection(host.installationId, draftId(current))
        val draft = withContext(Dispatchers.Default) { SavedDraft.decode(encoded) }
        if (generation == epoch && draftRevision == revision && _state.value.host?.installationId == host.installationId && draftId(_state.value) == draftId(current)) {
            pendingSubmission = draft.pending
            _state.update { it.copy(draft = draft.text, attachments = draft.attachments, pendingAction = draft.pending?.action, browserOverride = browser, draftReady = true) }
        }
    }

    private suspend fun refreshChats(append: Boolean = false) {
        val api = client ?: return; val selected = _state.value.workspaceId ?: return; val epoch = generation
        val titles = titleEpoch
        val page = api.getConversations(selected, if (append) _state.value.nextCursor else null)
        if (epoch == generation && titles == titleEpoch && client === api && _state.value.workspaceId == selected) _state.update { it.copy(chats = if (append) (it.chats + page.data).associateBy { chat -> chat.raw.optString("chatGroupId", chat.conversationId) }.values.toList() else page.data, nextCursor = page.nextCursor) }
    }
    fun clearRenameError() { _state.update { it.copy(renameError = null) } }
    suspend fun renameConversation(hostId: String, id: String, title: String): Boolean {
        val current = _state.value; val epoch = generation
        if (current.host?.installationId != hostId || current.renaming) return false
        val api = client
        if (api == null || !current.connected || current.busy) {
            _state.update { it.copy(renameError = "Reconnect to Windows and finish any pending action, then try again.") }
            return false
        }
        if (!validChatTitle(title)) {
            _state.update { it.copy(renameError = "Use a single line of 1–120 characters.") }; return false
        }
        val requestId = ++renameRequest
        _state.update { it.copy(renaming = true, renameError = null) }
        try {
            val saved = api.renameConversation(id, title.trim())
            titleEpoch++
            if (epoch != generation || client !== api || _state.value.host?.installationId != hostId) return false
            _state.update { state -> state.copy(
                chats = state.chats.map { if (it.conversationId == id) it.copy(title = saved) else it },
                snapshot = state.snapshot?.let { if (it.id == id) it.copy(title = saved) else it }) }
            return true
        } catch (cancelled: CancellationException) { throw cancelled }
        catch (failure: Exception) {
            if (epoch == generation && client === api) _state.update { it.copy(renameError = failure.message ?: "Chat could not be renamed. Try again.") }
            return false
        } finally {
            if (requestId == renameRequest && _state.value.host?.installationId == hostId) _state.update { it.copy(renaming = false) }
        }
    }
    fun refreshBrowsers() {
        val api = client ?: return; val hostId = _state.value.host?.installationId
        if (!_state.value.connected || _state.value.browsersLoading) return
        val requestId = ++browserRequest
        _state.update { it.copy(browsersLoading = true, browserError = null) }
        viewModelScope.launch {
            try {
                val catalog = api.getBrowsers()
                if (client === api && _state.value.host?.installationId == hostId) _state.update { it.copy(browserCatalog = catalog) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) { if (client === api) _state.update { it.copy(browserError = failure.message ?: "Browser profiles could not be loaded.") } }
            finally { if (requestId == browserRequest && _state.value.host?.installationId == hostId) _state.update { it.copy(browsersLoading = false) } }
        }
    }
    private fun credentialClient(workspaceId: String): CompanionClient {
        check(_state.value.connected && _state.value.workspaceId == workspaceId) { "Reconnect to the selected workspace first." }
        return checkNotNull(client)
    }
    suspend fun temporaryCredentials(workspaceId: String): JSONObject = credentialClient(workspaceId).getCredentials(workspaceId)
    suspend fun provideCredential(workspaceId: String, credential: JSONObject): JSONObject = credentialClient(workspaceId).provideCredential(workspaceId, credential)
    suspend fun revokeCredential(workspaceId: String, id: String?): JSONObject = credentialClient(workspaceId).revokeCredential(workspaceId, id)
    fun setBrowserSelection(selection: BrowserSelection) {
        val current = _state.value; val host = current.host ?: return; val epoch = generation
        if (!current.draftReady || current.busy || current.pendingAction != null || current.conversationId != null && current.snapshot == null) return
        selection.validationError()?.let { reportError(it); return }
        _state.update { it.copy(busy = true) }
        viewModelScope.launch {
            try {
                store.writeBrowserSelection(host.installationId, draftId(current), selection)
                if (epoch == generation) _state.update { it.copy(browserOverride = selection) }
            } catch (cancelled: CancellationException) { throw cancelled
            } catch (failure: Exception) { if (epoch == generation) reportError(failure.message ?: "Browser selection could not be saved.") }
            finally { if (epoch == generation) _state.update { it.copy(busy = false) } }
        }
    }
    fun clearDeleteError() { _state.update { it.copy(deleteError = null) } }
    fun secureDelete(kind: String, id: String, hostId: String, onDeleted: () -> Unit) {
        val current = _state.value; val api = client ?: return; val epoch = generation
        // A partially completed erase can remove the chat before retry. Its event
        // stream then returns 404, but this explicit, host-bound retry must still run.
        if (current.busy || current.importing || !current.draftReady || current.host?.installationId != hostId) return
        require(kind in setOf("chat", "workspace"))
        draftJob?.cancel(); stopConversationStream(); pollingJob?.cancel()
        maintenanceRunning = true
        _state.update { it.copy(busy = true, draftReady = false, error = null, deleteError = null) }
        viewModelScope.launch {
            var resetSelection = false
            try {
                saveDraft(current)
                val result = api.secureDelete(kind, id)
                require(result.optBoolean("deleted") && result.optInt("passes") == 7) { "Windows did not confirm deletion. Refresh before retrying." }
                val ids = result.getJSONArray("conversationIds").let { values -> (0 until values.length()).map(values::getString) }
                store.removeContexts(hostId, ids + if (kind == "workspace") listOf("new:$id") else emptyList())
                if (epoch != generation || client !== api) return@launch
                val reset = current.conversationId in ids || kind == "workspace" && current.workspaceId == id
                val workspaces = try { api.getWorkspaces() }
                    catch (cancelled: CancellationException) { throw cancelled }
                    catch (_: Exception) { current.workspaces.filterNot { kind == "workspace" && it.id == id } }
                if (epoch != generation || client !== api) return@launch
                _state.update { it.copy(workspaces = workspaces) }
                if (reset) {
                    resetSelection = true
                    generation++; draftRevision++; events.clear(); pendingSubmission = null; renderJob?.cancel()
                    val workspace = current.workspaceId?.takeIf { selected -> workspaces.any { it.id == selected } } ?: workspaces.firstOrNull()?.id
                    _state.update { it.copy(workspaceId = workspace, conversationId = null, snapshot = null, previousSegmentId = null, oldestSegmentNumber = 1, loadingSegment = false, presentation = ConversationPresentation(),
                        chats = emptyList(), nextCursor = null, draft = "", attachments = emptyList(), pendingAction = null, browserOverride = null, draftReady = false) }
                    saveSelection(); loadDraft()
                }
                onDeleted(); safely { refreshChats() }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (failure: Exception) {
                if (client === api) _state.update { it.copy(deleteError = if (failure is CompanionException && failure.status in setOf(400, 409))
                    "Deletion could not finish. Finish or cancel active chats before deleting. If they are already stopped, retry when Windows storage is available. Some data may already have been removed."
                    else "${failure.message ?: "Deletion could not finish."} Refresh Windows state before retrying; removal may already have started.") }
            } finally {
                maintenanceRunning = false
                if (client === api) {
                    _state.update { it.copy(busy = false, draftReady = if (resetSelection) it.draftReady else current.draftReady) }
                    if (foreground) {
                        if (!_state.value.connected) connect()
                        else { _state.value.conversationId?.let(::startConversation); startPolling() }
                    }
                }
            }
        }
    }
    fun moreChats() { viewModelScope.launch { safely { refreshChats(true) } } }
    fun refresh() { viewModelScope.launch { safely { refreshChats(); _state.value.conversationId?.let { refreshSnapshot(it) } } } }
    private fun startPolling() {
        pollingJob?.cancel()
        if (!foreground || client == null) return
        pollingJob = viewModelScope.launch {
            var ticks = 0
            while (isActive) {
                delay(1_000)
                ticks++
                if (ticks % 8 == 0) safely { refreshChats() }
                val snapshot = _state.value.snapshot
                val active = snapshot?.branchPreparing == true || snapshot?.tasks?.any { it.optString("status") in setOf("queued", "running", "pausing", "cancelling") } == true
                if (active || ticks % 8 == 0) _state.value.conversationId?.let(::requestSnapshot)
                if (_state.value.snapshot?.tasks?.lastOrNull()?.optString("status") == "running") scheduleRender()
            }
        }
    }
    private fun requestSnapshot(id: String) {
        snapshotRefreshRequested = true
        if (snapshotRefreshJob?.isActive == true) return
        val epoch = generation
        snapshotRefreshJob = viewModelScope.launch {
            do {
                delay(150)
                snapshotRefreshRequested = false
                if (epoch != generation || _state.value.conversationId != id) return@launch
                try { refreshSnapshot(id) }
                catch (error: Exception) {
                    currentCoroutineContext().ensureActive()
                    // The event stream owns connection recovery. A secondary
                    // snapshot/list refresh cannot turn a successful action red.
                    if (error is CompanionException && error.status in setOf(401,403)) {
                        _state.update { it.copy(connected = false, connectionError = "Reconnect to the paired Windows host.") }
                    }
                }
            } while (snapshotRefreshRequested && epoch == generation)
        }
    }
    private suspend fun refreshSnapshot(id: String) {
        val api = client ?: return; val epoch = generation
        val titles = titleEpoch
        val snapshot = api.getConversation(id)
        if (epoch != generation || client !== api || _state.value.conversationId != id) return
        val replaced = applySnapshot(snapshot, titles); scheduleRender()
        if (replaced) startConversation(id)
    }
    private fun applySnapshot(snapshot: ConversationSnapshot, titles: Long): Boolean {
        val previous = _state.value.snapshot
        if (previous?.id == snapshot.id && snapshot.eventCursor < previous.eventCursor) return false
        val oldRoot = _state.value.snapshot?.tasks?.firstOrNull()?.optString("id")
        val replaced = previous != null && (previous.segment?.optString("id") != snapshot.segment?.optString("id") || oldRoot != snapshot.tasks.firstOrNull()?.optString("id"))
        if (previous?.id != snapshot.id) olderSegments.clear()
        if (replaced) { replayLoading = true; events.clear() }
        _state.update { state ->
            val title = if (titles == titleEpoch) snapshot.title else state.chatTitle(snapshot.id)
            val oldest = olderSegments.firstOrNull()?.first?.segment ?: snapshot.segment
            state.copy(snapshot = snapshot.copy(title = title), loadingSegment = state.loadingSegment && segmentLoadGeneration == generation, previousSegmentId = oldest?.optString("previousId")?.takeIf { it.isNotBlank() && it != "null" }, oldestSegmentNumber = oldest?.optInt("number", 1) ?: 1, chats = state.chats.map {
                if (it.conversationId == snapshot.id && title != null) it.copy(title = title) else it
            })
        }
        val host = _state.value.host
        if (host != null) viewModelScope.launch { store.writeReplyBranch(host.installationId, snapshot.chatGroupId, snapshot.id) }
        return replaced
    }
    private fun scheduleRender() {
        renderRequested++
        if (renderJob?.isActive == true) return
        val epoch = generation
        renderJob = viewModelScope.launch {
            do {
                delay(100)
                val requested = renderRequested
                val snapshot = _state.value.snapshot; val visible = events.values.toList()
                val complete = !replayLoading
                val earlier = olderSegments.toList()
                val presentation = withContext(Dispatchers.Default) {
                    val latest = presentConversation(snapshot, visible, historyComplete = complete)
                    val prefix = earlier.flatMap { (saved, savedEvents) ->
                        presentConversation(saved, savedEvents).bubbles + com.pshea.permissivecodex.ui.ChatBubble("boundary:${saved.segment?.optString("id")}", "Compacted context", "Segment ${(saved.segment?.optInt("number") ?: 0) + 1}", recovery = true)
                    }
                    latest.copy(bubbles = prefix + latest.bubbles)
                }
                if (epoch != generation) return@launch
                val hostId = _state.value.host?.installationId
                _state.update { it.copy(presentation = presentation.copy(artifacts = presentation.artifacts.map { file ->
                    attachmentMetadata["$hostId:${file.id}"]?.takeIf { file.input }?.let { info -> file.copy(name = info.optString("name", file.name), size = info.optLong("size", file.size)) } ?: file
                })) }
            } while (requested != renderRequested)
        }
    }
    fun refreshAttachmentDetails() {
        val api = client ?: return
        val epoch = generation
        val hostId = _state.value.host?.installationId
        val files = _state.value.presentation.artifacts.filter { it.input && "$hostId:${it.id}" !in attachmentMetadata }
        viewModelScope.launch { files.forEach { file ->
            val info = try { api.attachmentInfo(file.id) } catch (cancelled: CancellationException) { throw cancelled } catch (_: Exception) { return@forEach }
            if (epoch != generation || api !== client) return@launch
            attachmentMetadata["$hostId:${file.id}"] = info
            scheduleRender()
        } }
    }
    private fun stopConversationStream() {
        streamRevision++
        streamJob?.cancel(); streamJob = null
        snapshotRefreshJob?.cancel(); snapshotRefreshJob = null
        snapshotRefreshRequested = false
    }
    private fun startConversation(id: String, initial: ConversationSnapshot? = null) {
        stopConversationStream()
        val epoch = generation
        val revision = streamRevision
        streamJob = viewModelScope.launch {
            var delayMs = 1_000L
            var accepted = initial
            var disconnectedAt: Long? = null
            while (isActive && epoch == generation && revision == streamRevision && _state.value.conversationId == id && foreground) {
                val api = client ?: break
                try {
                    val titles = titleEpoch
                    val snapshot = accepted ?: api.getConversation(id)
                    accepted = null
                    if (epoch != generation || revision != streamRevision || client !== api || _state.value.conversationId != id) break
                    // Show saved answers before replay, and reset old cursors before
                    // loading the replacement task IDs created by Delete from here.
                    replayLoading = true
                    applySnapshot(snapshot, titles)
                    // Coalesced replay is a snapshot, not an incremental delta feed.
                    // Rebuild it on reconnect so an interrupted message cannot lose
                    // the middle of its output when its first event ID is reused.
                    events.clear(); scheduleRender()
                    _state.update { it.copy(connected = true, status = "${it.host?.name} · Tailscale", connectionError = null) }
                    replayWhileStreaming(snapshot,
                        page = { after, through, segment -> api.replayEvents(id, after, through, segment) },
                        stream = { after -> api.observeEvents(id, after) },
                        onReplay = { saved ->
                            if (epoch == generation && revision == streamRevision && client === api && _state.value.conversationId == id) {
                                saved.forEach { events.putIfAbsent(it.id, it) }
                                replayLoading = false; scheduleRender()
                            }
                        }, onEvent = onEvent@ { event ->
                        if (epoch != generation || revision != streamRevision || client !== api || _state.value.conversationId != id) return@onEvent
                        disconnectedAt = null
                        events[event.id] = event
                        val unknownTask = _state.value.snapshot?.tasks?.none { it.optString("id") == event.taskId } == true
                        scheduleRender()
                        if (event.kind == "history") { startConversation(id); return@onEvent }
                        if (unknownTask || event.kind in setOf("status", "queue") || event.data.optString("method") in setOf("turn/started", "turn/completed")) requestSnapshot(id)
                        delayMs = 1_000L
                    })
                } catch (cancelled: CancellationException) { throw cancelled }
                catch (error: CompanionException) {
                    currentCoroutineContext().ensureActive()
                    if (epoch != generation || revision != streamRevision || client !== api) break
                    if (error.status in setOf(401,403,404,410)) _state.update { it.copy(connected = false, status = "Check companion access on Windows", connectionError = error.message) }
                    if (error.status in setOf(401, 403)) { pollingJob?.cancel(); client = null; break }
                    if (error.status in setOf(404, 410)) break
                    val since = disconnectedAt ?: System.nanoTime().also { disconnectedAt = it }
                    if (System.nanoTime() - since >= 10_000_000_000L) _state.update { it.copy(connected = false,
                        connectionError = "Cannot reach Windows. Reconnecting automatically…") }
                    delay(delayMs); delayMs = (delayMs * 2).coerceAtMost(15_000)
                }
                catch (error: Exception) {
                    currentCoroutineContext().ensureActive()
                    if (epoch != generation || revision != streamRevision || client !== api) break
                    val since = disconnectedAt ?: System.nanoTime().also { disconnectedAt = it }
                    if (System.nanoTime() - since >= 10_000_000_000L) _state.update { it.copy(connected = false,
                        status = "Reconnecting…", connectionError = "Cannot reach Windows. Reconnecting automatically…") }
                    delay(delayMs); delayMs = (delayMs * 2).coerceAtMost(15_000)
                    try { ensureTunnel() } catch (cancelled: CancellationException) { throw cancelled } catch (_: Exception) {
                        currentCoroutineContext().ensureActive()
                        if (revision == streamRevision && System.nanoTime() - since >= 10_000_000_000L) _state.update { it.copy(status = "Open Tailscale to reconnect") }
                    }
                }
            }
        }
    }

    fun addAttachments(uris: List<Uri>) {
        if (!_state.value.draftReady || _state.value.busy) return
        val target = draftId(_state.value); val epoch = generation
        viewModelScope.launch {
            _state.update { it.copy(importing = true) }
            try {
                val result = reader.read(uris, _state.value.attachments)
                if (epoch == generation && draftId(_state.value) == target) { pendingSubmission = null; _state.update { it.copy(attachments = result, pendingAction = null) }; saveDraft(_state.value) }
            } catch (error: Exception) { reportError(error.message ?: "Attachment could not be opened") }
            finally { _state.update { it.copy(importing = false) } }
        }
    }
    fun removeAttachment(id: String) {
        if (!_state.value.draftReady || _state.value.busy) return
        val attachment = _state.value.attachments.firstOrNull { it.id == id }; val api = client
        pendingSubmission = null; _state.update { it.copy(attachments = it.attachments.filterNot { a -> a.id == id }, pendingAction = null) }; persistDraft()
        if (attachment?.uploadedId != null && api != null) viewModelScope.launch { runCatching { api.removeAttachment(attachment.uploadedId) } }
    }
    fun clearPendingDraft() { if (!_state.value.draftReady || _state.value.busy) return; pendingSubmission = null; draftRevision++; _state.update { it.copy(draft = "", attachments = emptyList(), pendingAction = null) }; persistDraft() }
    fun preparePhoto(): Uri {
        val directory = java.io.File(getApplication<Application>().cacheDir, "camera").apply { mkdirs() }
        cameraFile = java.io.File.createTempFile("capture-", ".jpg", directory)
        return androidx.core.content.FileProvider.getUriForFile(getApplication(), "${BuildConfig.APPLICATION_ID}.attachments", cameraFile!!).also { cameraUri = it }
    }
    fun completePhoto(success: Boolean) {
        val uri = cameraUri; val file = cameraFile; cameraUri = null; cameraFile = null
        viewModelScope.launch {
            try { if (success && uri != null) { val attachments = reader.read(listOf(uri), _state.value.attachments); pendingSubmission = null; _state.update { it.copy(attachments = attachments, pendingAction = null) }; saveDraft(_state.value) } }
            catch (error: Exception) { reportError(error.message ?: "Photo could not be opened") }
            finally { file?.delete() }
        }
    }

    fun send(action: SendAction) {
        val initial = _state.value; val api = client ?: return
        if (!initial.draftReady || !initial.connected || initial.busy || initial.importing || initial.conversationId != null && initial.snapshot == null || (initial.draft.isBlank() && initial.attachments.isEmpty())) return
        val workspace = initial.workspaceId ?: return
        val epoch = generation
        val revision = draftRevision
        draftJob?.cancel()
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            try {
                require(initial.attachments.none(DraftAttachment::needsReselect)) { "Some original files need to be selected again. Remove the missing chips and reattach those files before sending." }
                val prompt = effectivePrompt(initial.draft, initial.attachments.isNotEmpty())
                val previous = pendingSubmission?.takeIf { it.matches(action, prompt, initial.attachments) }
                val staged = initial.attachments.toMutableList()
                if (previous == null) staged.forEachIndexed { index, attachment ->
                    if (attachment.uploadedId == null) {
                        val uploadedId = api.uploadAttachment(workspace, attachment.name, attachment.bytes).getString("id")
                        staged[index] = attachment.copy(uploadedId = uploadedId)
                        if (epoch != generation || client !== api) error("The selected Windows install changed")
                        _state.update { it.copy(attachments = staged.toList()) }
                        saveDraft(_state.value, null)
                    }
                }
                val latest = initial.snapshot?.tasks?.lastOrNull()
                val pending = previous ?: PendingSubmission(action, prompt, staged.mapNotNull { it.uploadedId }, UUID.randomUUID().toString(), staged.map(DraftAttachment::id), workspace, initial.conversationId,
                    latest?.getString("id"), if (action == SendAction.STEER) latest?.nonNullString("activeTurnId")?.takeIf(String::isNotBlank) ?: error("Wait for an active turn before steering") else null,
                    options = if (action == SendAction.STEER) "{}" else initial.browserSelection.toJson().toString())
                pendingSubmission = pending
                _state.update { it.copy(pendingAction = pending.action) }
                // Persist the exact target and UUID before the first mutation can leave the phone.
                saveDraft(_state.value, pending)
                val savedEnvelope = SavedDraft(_state.value.draft, _state.value.attachments, pending).encode()
                val response = when (action) {
                    SendAction.SEND -> api.submit(pending.workspaceId, pending.text, JSONObject(pending.options), pending.attachmentIds, pending.submissionId)
                    SendAction.CONTINUE -> api.continueTask(pending.taskId ?: error("Select a finished chat"), pending.text, JSONObject(pending.options), pending.attachmentIds, pending.submissionId)
                    SendAction.STEER -> api.steer(pending.taskId ?: error("Select an active chat"), pending.text, pending.expectedTurnId ?: error("Wait for an active turn before steering"), pending.attachmentIds, pending.submissionId)
                    SendAction.QUEUE -> api.queueFollowup(pending.conversationId ?: error("Select a chat first"), pending.text, JSONObject(pending.options), pending.attachmentIds, pending.submissionId)
                }
                if (epoch != generation || client !== api || revision != draftRevision || pendingSubmission !== pending) return@launch
                if (!store.removeDraftIfMatches(initial.host!!.installationId, draftId(initial), savedEnvelope)) error("The draft changed while delivery completed; its newer text was preserved")
                draftJob?.cancel(); pendingSubmission = null
                _state.update { it.copy(draft = "", attachments = emptyList(), pendingAction = null) }
                val conversationId = response.nonNullString("conversationId").ifBlank { response.nonNullString("id").takeIf { action == SendAction.QUEUE }.orEmpty() }.ifBlank { initial.conversationId.orEmpty() }
                refreshChats()
                if (conversationId.isNotBlank() && conversationId != initial.conversationId) { _state.update { it.copy(busy = false) }; chooseConversation(conversationId) }
                else initial.conversationId?.let { refreshSnapshot(it) }
            } catch (cancelled: CancellationException) { throw cancelled }
            catch (error: Exception) { if (epoch == generation) reportError("${error.message ?: "Request failed"}. Draft preserved.${if (pendingSubmission != null) " Refresh Windows state before retrying the saved delivery." else ""}") }
            finally { if (epoch == generation || _state.value.host?.installationId == initial.host?.installationId) _state.update { it.copy(busy = false) } }
        }
    }
    fun control(action: String, followupId: String? = null) {
        val current = _state.value; val api = client ?: return
        if (!current.connected || current.busy) return
        val latest = current.snapshot?.tasks?.lastOrNull()
        if (action == "retry" && (latest?.optString("status") != "failed" || !latest.optBoolean("canRetry"))) return
        val epoch = generation
        viewModelScope.launch {
            _state.update { it.copy(busy = true, error = null) }
            try {
                safely {
                    val latestId = latest?.getString("id")
                    when (action) {
                        "pause" -> api.pause(latestId ?: error("Select a task"))
                        "resume" -> api.resume(latestId ?: error("Select a task"))
                        "cancel" -> api.cancel(latestId ?: error("Select a task"))
                        "retry" -> api.retry(latestId ?: error("Select a failed response"))
                        "removeFollowup" -> api.removeFollowup(current.conversationId ?: error("Select a chat"), followupId ?: error("Select a follow-up"))
                        "resumeQueue" -> api.resumeQueue(current.conversationId ?: error("Select a chat"))
                        else -> error("Unknown action")
                    }
                    if (epoch != generation || client !== api) return@safely
                    current.conversationId?.let { refreshSnapshot(it) }; refreshChats()
                }
            } finally {
                if (epoch == generation && _state.value.host?.installationId == current.host?.installationId) _state.update { it.copy(busy = false) }
            }
        }
    }
    fun saveArtifact(artifact: ArtifactLink, uri: Uri) {
        val api = client ?: return
        viewModelScope.launch { safely { withContext(Dispatchers.IO) { getApplication<Application>().contentResolver.openOutputStream(uri)?.use {
            if (artifact.input) api.downloadAttachment(artifact.id, it) else api.downloadArtifact(artifact.taskId, artifact.id, it)
        } ?: error("Could not open the selected destination") } } }
    }
    private suspend fun safely(block: suspend () -> Unit) {
        try { block() } catch (cancelled: CancellationException) { throw cancelled }
        catch (error: CompanionException) {
            currentCoroutineContext().ensureActive()
            if (error.status in setOf(401, 403)) { pollingJob?.cancel(); stopConversationStream(); client = null; _state.update { it.copy(connected = false, status = "Check companion access on Windows") } }
            reportError(error.message ?: "Request failed")
        } catch (error: Exception) {
            currentCoroutineContext().ensureActive()
            if (error !is java.io.IOException) reportError(error.message ?: "Request failed")
        }
    }
    private fun JSONObject.nonNullString(key: String): String = if (isNull(key)) "" else optString(key)
    private fun <K : Comparable<K>, V> java.util.SortedMap<K, V>.lastKeyOrNull(): K? = if (isEmpty()) null else lastKey()
}

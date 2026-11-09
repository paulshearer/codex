package com.pshea.permissivecodex.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.widget.Toast
import androidx.compose.foundation.*
import androidx.compose.foundation.interaction.collectIsDraggedAsState
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.selected
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.pshea.permissivecodex.BuildConfig
import com.pshea.permissivecodex.CompanionUiState
import com.pshea.permissivecodex.CompanionViewModel
import com.pshea.permissivecodex.SendAction
import com.pshea.permissivecodex.data.Workspace
import com.pshea.permissivecodex.network.TailnetRecovery
import kotlinx.coroutines.launch
import java.util.Locale

private data class DeletionTarget(val hostId: String, val kind: String, val id: String, val name: String)
private data class RenameTarget(val hostId: String, val id: String, val title: String)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CompanionApp(viewModel: CompanionViewModel, onScan: () -> Unit, onAddFile: () -> Unit, onTakePhoto: () -> Unit, onSaveArtifact: (ArtifactLink) -> Unit) {
    val state by viewModel.state.collectAsStateWithLifecycle()
    val drawer = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    var showHosts by rememberSaveable { mutableStateOf(false) }
    var showPair by remember { mutableStateOf(false) }
    var showActivity by rememberSaveable { mutableStateOf(false) }
    var showStatistics by rememberSaveable { mutableStateOf(false) }
    var showAbout by rememberSaveable { mutableStateOf(false) }
    var cancelConfirm by remember { mutableStateOf(false) }
    var showBrowser by remember(state.host?.installationId, state.workspaceId, state.conversationId) { mutableStateOf(false) }
    var showCredentials by remember(state.host?.installationId, state.workspaceId) { mutableStateOf(false) }
    var deletion by remember(state.host?.installationId) { mutableStateOf<DeletionTarget?>(null) }
    var rename by remember(state.host?.installationId, state.workspaceId, state.conversationId) { mutableStateOf<RenameTarget?>(null) }
    val context = LocalContext.current
    val copyId: (String) -> Unit = { id ->
        val copied = copyChatId(state.chatThreadId(id)) { value ->
            val clipboard = context.getSystemService(ClipboardManager::class.java) ?: error("Clipboard unavailable")
            clipboard.setPrimaryClip(ClipData.newPlainText("Chat ID", value))
        }
        Toast.makeText(context, if (copied) "Chat ID copied" else "Chat ID could not be copied. Try again.", Toast.LENGTH_SHORT).show()
    }
    val latest = state.snapshot?.tasks?.lastOrNull()
    val status = latest?.optString("status").orEmpty()
    val running = status in setOf("queued", "running", "pausing")
    val paused = status == "paused"
    val terminal = status in setOf("completed", "failed", "cancelled", "interrupted")
    val ready = state.connected && !state.busy && state.draftReady

    ModalNavigationDrawer(drawerState = drawer, drawerContent = {
        ModalDrawerSheet {
            Text("Permissive Codex", style = MaterialTheme.typography.titleLarge, modifier = Modifier.padding(20.dp))
            Text(state.host?.name ?: "No Windows host paired", style = MaterialTheme.typography.labelMedium, modifier = Modifier.padding(horizontal = 20.dp))
            NavigationDrawerItem(label = { Text("Windows installs") }, selected = false, icon = { Icon(Icons.Default.Computer, null) }, onClick = { showHosts = true; scope.launch { drawer.close() } })
            NavigationDrawerItem(label = { Text("New chat") }, selected = state.conversationId == null, icon = { Icon(Icons.Default.Add, null) }, onClick = { if (ready && state.workspaceId != null) { viewModel.chooseConversation(null); scope.launch { drawer.close() } } })
            HorizontalDivider()
            Text("${state.workspaces.firstOrNull { it.id == state.workspaceId }?.name ?: "Workspace"} chats", style = MaterialTheme.typography.labelLarge, modifier = Modifier.padding(16.dp))
            LazyColumn(Modifier.weight(1f)) {
                items(state.chats, key = { it.raw.optString("chatGroupId", it.conversationId) }) { chat ->
                    var actions by remember { mutableStateOf(false) }
                    NavigationDrawerItem(label = { Column { Text(chat.title, maxLines = 2, overflow = TextOverflow.Ellipsis); Text(chat.status, style = MaterialTheme.typography.labelSmall) } }, selected = (state.snapshot?.chatGroupId ?: state.conversationId) == chat.raw.optString("chatGroupId", chat.conversationId),
                        badge = { Box {
                            IconButton(onClick = { actions = true }) { Icon(Icons.Default.MoreVert, "Actions for chat ${chat.title}") }
                            DropdownMenu(expanded = actions, onDismissRequest = { actions = false }) {
                                ChatActionItems(state.chatThreadId(chat.conversationId), ready && !state.renaming,
                                    onRename = { actions = false; viewModel.clearRenameError(); rename = RenameTarget(state.host!!.installationId, chat.conversationId, chat.title); scope.launch { drawer.close() } },
                                    onCopy = { actions = false; copyId(chat.conversationId) },
                                    onDelete = { actions = false; viewModel.clearDeleteError(); deletion = DeletionTarget(state.host!!.installationId, "chat", chat.conversationId, chat.title); scope.launch { drawer.close() } })
                            }
                        } },
                        onClick = { if (!state.busy) { viewModel.chooseChatSummary(chat.conversationId); scope.launch { drawer.close() } } })
                }
                if (state.nextCursor != null) item { TextButton(onClick = viewModel::moreChats) { Text("Load older chats") } }
            }
        }
    }) {
        Scaffold(topBar = {
            TopAppBar(title = {
                Column {
                    Text(state.chatTitle(state.conversationId) ?: "Permissive Codex", maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(state.status, style = MaterialTheme.typography.labelSmall, color = if (state.connected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
            }, navigationIcon = { IconButton(onClick = { scope.launch { drawer.open() } }) { Icon(Icons.Default.Menu, "Open chats") } }, actions = {
                IconButton(onClick = viewModel::refresh, enabled = state.connected && !state.busy) { Icon(Icons.Default.Refresh, "Refresh from Windows") }
                var menu by remember { mutableStateOf(false) }
                Box {
                    IconButton(onClick = { menu = true }) { Icon(Icons.Default.MoreVert, "More actions") }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        DropdownMenuItem(text = { Text("Windows installs") }, onClick = { menu = false; showHosts = true })
                        DropdownMenuItem(text = { Text("Activity") }, onClick = { menu = false; showActivity = true })
                        DropdownMenuItem(text = { Text("Statistics") }, onClick = { menu = false; showStatistics = true })
                        if (state.conversationId != null) ChatActionItems(state.chatThreadId(state.conversationId), ready && !state.renaming,
                            onRename = { menu = false; viewModel.clearRenameError(); rename = RenameTarget(state.host!!.installationId, state.conversationId!!, state.chatTitle(state.conversationId) ?: "Selected chat") },
                            onCopy = { menu = false; copyId(state.conversationId!!) },
                            onDelete = { menu = false; viewModel.clearDeleteError(); deletion = DeletionTarget(state.host!!.installationId, "chat", state.conversationId!!, state.chatTitle(state.conversationId) ?: "Selected chat") })
                        if (state.workspaceId != null) DropdownMenuItem(text = { Text("Delete workspace") }, enabled = ready, onClick = {
                            menu = false; viewModel.clearDeleteError(); deletion = DeletionTarget(state.host!!.installationId, "workspace", state.workspaceId!!, state.workspaces.firstOrNull { it.id == state.workspaceId }?.name ?: "Selected workspace")
                        })
                        DropdownMenuItem(text = { Text("About") }, onClick = { menu = false; showAbout = true })
                    }
                }
            })
        }) { padding ->
            Column(Modifier.fillMaxSize().padding(padding).imePadding()) {
                if (!state.connected) {
                    Surface(color = MaterialTheme.colorScheme.secondaryContainer) {
                        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text(if (state.host == null) "Pair with an existing Windows install" else "Reconnect through Tailscale", Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
                            TextButton(onClick = { if (state.host == null) showPair = true else viewModel.connect() }, enabled = !state.busy) { Text(if (state.host == null) "Pair" else "Retry") }
                            TextButton(onClick = { if (!TailnetRecovery(context).openTailscale()) viewModel.reportError("Install Tailscale and connect it to the same tailnet as Windows.") }) { Text("Tailscale") }
                        }
                    }
                }
                WorkspaceStrip(state.workspaces, state.workspaceId, ready, viewModel::chooseWorkspace)
                if (state.busy || state.importing) LinearProgressIndicator(Modifier.fillMaxWidth())
                (state.error ?: state.connectionError)?.let { error ->
                    Surface(color = MaterialTheme.colorScheme.errorContainer) {
                        Row(Modifier.fillMaxWidth().padding(8.dp), verticalAlignment = Alignment.CenterVertically) {
                            Text(error, Modifier.weight(1f), style = MaterialTheme.typography.bodySmall)
                            IconButton(onClick = viewModel::clearError) { Icon(Icons.Default.Close, "Dismiss error") }
                        }
                    }
                }
                MessageList(state, Modifier.weight(1f), onLoadPrevious = viewModel::loadPreviousSegment, onVersion = viewModel::selectReplyVersion, onTurnAction = { bubble, action, submissionId, completed ->
                    state.conversationId?.let { viewModel.editHistory(it, bubble.taskId, bubble.messageId!!, submissionId, false, completed, operation = action, segmentId = bubble.segmentId) }
                }, onEdit = { bubble, text, submissionId, completed ->
                    state.conversationId?.let { viewModel.editHistory(it, bubble.taskId, bubble.messageId!!, submissionId, false, completed, text, segmentId = bubble.segmentId) }
                }) { bubble, deleting, submissionId, completed ->
                    state.conversationId?.let { viewModel.editHistory(it, bubble.taskId, bubble.messageId!!, submissionId, deleting, completed, segmentId = bubble.segmentId) }
                }
                if (state.snapshot != null) {
                    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(2.dp)) {
                        TaskStatus(status, state.presentation.tokensPerSecond)
                        TextButton(onClick = { showStatistics = true }, contentPadding = PaddingValues(horizontal = 6.dp)) { Text("Stats", style = MaterialTheme.typography.labelMedium) }
                        TextButton(onClick = { showActivity = true }, contentPadding = PaddingValues(horizontal = 6.dp)) { Text("Activity", style = MaterialTheme.typography.labelMedium) }
                        key(state.host?.installationId, state.conversationId) {
                            ConversationAttachments(state.presentation.artifacts, state.connected, viewModel::refreshAttachmentDetails, onSaveArtifact)
                        }
                        RetryResponseAction(latest, ready) { viewModel.control(it) }
                        if (running) TextButton(onClick = { viewModel.control("pause") }, enabled = ready) { Icon(Icons.Default.Pause, null, Modifier.size(16.dp)); Text("Pause") }
                        if (paused) TextButton(onClick = { viewModel.control("resume") }, enabled = ready) { Icon(Icons.Default.PlayArrow, null, Modifier.size(16.dp)); Text("Resume") }
                        if (running || paused) TextButton(onClick = { cancelConfirm = true }, enabled = ready) { Icon(Icons.Default.Stop, null, Modifier.size(16.dp)); Text("Cancel") }
                    }
                    if (state.snapshot!!.followups.isNotEmpty() || state.snapshot!!.queueHeld) {
                        Surface(color = MaterialTheme.colorScheme.secondaryContainer) {
                            Column(Modifier.fillMaxWidth().padding(8.dp)) {
                                Row(verticalAlignment = Alignment.CenterVertically) {
                                    Text("${state.snapshot!!.followups.size} queued follow-ups${if (state.snapshot!!.queueHeld) " · held" else ""}", Modifier.weight(1f), style = MaterialTheme.typography.labelMedium)
                                    if (state.snapshot!!.queueHeld) TextButton(onClick = { viewModel.control("resumeQueue") }, enabled = ready) { Text("Resume queue") }
                                }
                                state.snapshot!!.followups.forEach { followup ->
                                    Row(verticalAlignment = Alignment.CenterVertically) {
                                        Text(followup.optJSONObject("body")?.optString("prompt").orEmpty(), Modifier.weight(1f), maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.bodySmall)
                                        IconButton(onClick = { viewModel.control("removeFollowup", followup.getString("id")) }, enabled = ready) { Icon(Icons.Default.Close, "Remove queued follow-up") }
                                    }
                                }
                            }
                        }
                    }
                }
                Composer(state, running, terminal, viewModel::setDraft, viewModel::send, viewModel::removeAttachment, onAddFile, onTakePhoto, viewModel::clearPendingDraft,
                    onBrowser = { viewModel.refreshBrowsers(); showBrowser = true }, onCredentials = { showCredentials = true })
            }
        }
    }
    rename?.let { target ->
        key(target) {
            RenameChatDialog(target.title, state.renaming, state.renameError,
                onSave = { title -> scope.launch {
                    if (viewModel.renameConversation(target.hostId, target.id, title) && rename == target) rename = null
                } }, onDismiss = { rename = null })
        }
    }
    if (showHosts) AlertDialog(onDismissRequest = { showHosts = false }, title = { Text("Windows installs") }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            state.hosts.forEach { host ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    TextButton(onClick = { viewModel.chooseHost(host); showHosts = false }, enabled = !state.busy, modifier = Modifier.weight(1f)) { Column { Text(host.name); Text(host.origin, style = MaterialTheme.typography.labelSmall) } }
                    IconButton(onClick = { viewModel.forgetHost(host) }, enabled = !state.busy) { Icon(Icons.Default.LinkOff, "Forget ${host.name}") }
                }
            }
            Text("Only your paired Windows installs are shown. Access requires Tailscale reachability and this phone's enrolled key.", style = MaterialTheme.typography.bodySmall)
        }
    }, confirmButton = { TextButton(onClick = { showHosts = false; showPair = true }) { Text("Pair another install") } }, dismissButton = { TextButton(onClick = { showHosts = false }) { Text("Close") } })
    if (showPair) PairDialog(onDismiss = { showPair = false }, onScan = { showPair = false; onScan() }, onPair = { showPair = false; viewModel.pair(it) })
    if (showActivity) AlertDialog(onDismissRequest = { showActivity = false }, title = { Text("Activity") }, text = {
        LazyColumn { if (state.presentation.activity.isEmpty()) item { Text("No tool activity yet.") }; items(state.presentation.activity) { MarkdownContent(it, Modifier.padding(vertical = 6.dp)) } }
    }, confirmButton = { TextButton(onClick = { showActivity = false }) { Text("Close") } })
    if (showStatistics) AlertDialog(onDismissRequest = { showStatistics = false }, title = { Text("Statistics") }, text = { MarkdownContent(state.presentation.statistics.ifBlank { "Usage will appear when Windows reports it." }) }, confirmButton = { TextButton(onClick = { showStatistics = false }) { Text("Close") } })
    if (showAbout) AlertDialog(onDismissRequest = { showAbout = false }, title = { Text("Permissive Codex ${BuildConfig.VERSION_NAME}") }, text = { Text("A native companion to your Windows install. Chats and execution stay on Windows. Pairing and drafts are encrypted on this device. Connections require Tailscale.\n\nTheme adapted from Sparky Chat, Copyright 2026 Paul Shearer (Apache 2.0). Local QR scanning uses ZXing (Apache 2.0).") }, confirmButton = { TextButton(onClick = { showAbout = false }) { Text("Close") } })
    if (cancelConfirm) AlertDialog(onDismissRequest = { cancelConfirm = false }, title = { Text("Cancel this task?") }, text = { Text("Windows will stop the active task. The chat remains available.") }, confirmButton = { TextButton(onClick = { cancelConfirm = false; viewModel.control("cancel") }) { Text("Cancel task") } }, dismissButton = { TextButton(onClick = { cancelConfirm = false }) { Text("Keep running") } })
    if (showBrowser) BrowserSelectionDialog(state.browserSelection, state.browserCatalog, state.browsersLoading, state.browserError,
        viewModel::refreshBrowsers, onSave = { viewModel.setBrowserSelection(it); showBrowser = false }, onDismiss = { showBrowser = false })
    if (showCredentials && state.workspaceId != null) SecureCredentialsDialog(state.workspaceId!!, state.connected, viewModel) { showCredentials = false }
    deletion?.let { target -> SecureDeletionDialog(target.kind, target.name, state.busy, state.deleteError,
        onDelete = { viewModel.secureDelete(target.kind, target.id, target.hostId) { deletion = null } }, onDismiss = { deletion = null }) }
}

@Composable
internal fun TaskStatus(status: String, tokensPerSecond: Double, modifier: Modifier = Modifier) {
    val dark = isSystemInDarkTheme()
    val color = when (status) {
        "running" -> if (dark) Color(0xff8ab4ff) else Color(0xff1559bd)
        "failed", "interrupted" -> if (dark) Color(0xffff9999) else Color(0xffba1a1a)
        "completed" -> if (dark) Color(0xff7cdda0) else Color(0xff146c35)
        else -> MaterialTheme.colorScheme.onSurfaceVariant
    }
    val label = when (status) {
        "running" -> "Running (~%.1f TPS)".format(Locale.ROOT, tokensPerSecond)
        "completed" -> "Done"
        else -> status.replaceFirstChar(Char::uppercase).ifBlank { "Ready" }
    }
    Surface(modifier = modifier, shape = RoundedCornerShape(8.dp), color = color.copy(alpha = .12f), border = BorderStroke(1.dp, color)) {
        Text(label, Modifier.padding(horizontal = 6.dp, vertical = 3.dp), color = color, style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold)
    }
}

@Composable
internal fun ConversationAttachments(files: List<ArtifactLink>, connected: Boolean, onOpen: () -> Unit, onSave: (ArtifactLink) -> Unit) {
    var expanded by rememberSaveable { mutableStateOf(false) }
    TextButton(onClick = { onOpen(); expanded = true }, contentPadding = PaddingValues(horizontal = 6.dp)) {
            Text("Attachments (${files.size})", style = MaterialTheme.typography.labelMedium)
    }
    if (expanded) AttachmentsDialog(files, connected, onSave) { expanded = false }
}

@Composable
internal fun AttachmentsDialog(files: List<ArtifactLink>, connected: Boolean, onSave: (ArtifactLink) -> Unit, onDismiss: () -> Unit) {
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Attachments (${files.size})") }, text = {
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(files, key = { "${it.input}:${it.taskId}:${it.id}" }) { file ->
                OutlinedButton(onClick = { onSave(file) }, enabled = connected, modifier = Modifier.fillMaxWidth()) {
                    Icon(Icons.Default.Download, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp))
                    Column(Modifier.weight(1f)) {
                        Text(file.name, maxLines = 3, overflow = TextOverflow.Ellipsis)
                        Text("${if (file.input) "Attached file" else "Generated file"}${if (file.size >= 0) " · ${formatBytes(file.size)}" else ""}", style = MaterialTheme.typography.labelSmall)
                    }
                }
            }
        }
    }, confirmButton = { TextButton(onClick = onDismiss) { Text("Close attachments") } })
}

@Composable
internal fun RetryResponseAction(task: org.json.JSONObject?, enabled: Boolean, onControl: (String) -> Unit) {
    if (task?.optString("status") == "failed" && task.optBoolean("canRetry")) {
        TextButton(onClick = { onControl("retry") }, enabled = enabled) {
            Icon(Icons.Default.Refresh, null, Modifier.size(16.dp)); Text("Retry response")
        }
    }
}

@Composable
internal fun SecureDeletionDialog(kind: String, name: String, deleting: Boolean, error: String?, onDelete: () -> Unit, onDismiss: () -> Unit) {
    AlertDialog(onDismissRequest = { if (!deleting) onDismiss() }, title = { Text("Delete $kind permanently?") }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(name)
            Text("Permanently removes ${if (kind == "workspace") "this workspace and all its chats" else "this entire chat"} from Windows with seven overwrite passes. Finish or cancel active chats first. Files in your working directory stay intact.")
            if (deleting) { LinearProgressIndicator(Modifier.fillMaxWidth()); Text("Deleting on Windows…") }
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        }
    }, confirmButton = { TextButton(onClick = onDelete, enabled = !deleting) { Text("Delete permanently", color = MaterialTheme.colorScheme.error) } },
        dismissButton = { TextButton(onClick = onDismiss, enabled = !deleting) { Text("Keep $kind") } })
}

@Composable
internal fun WorkspaceStrip(workspaces: List<Workspace>, selected: String?, enabled: Boolean, onSelect: (String) -> Unit) {
    Row(Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(8.dp), horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        workspaces.forEach { workspace ->
            Column(Modifier.width(84.dp).clickable(enabled = enabled, role = Role.Button) { onSelect(workspace.id) }.semantics { contentDescription = "Workspace ${workspace.name}"; this.selected = workspace.id == selected }, horizontalAlignment = Alignment.CenterHorizontally) {
                Surface(shape = CircleShape, color = if (workspace.id == selected) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.secondaryContainer, modifier = Modifier.size(48.dp).then(if (workspace.id == selected) Modifier.border(3.dp, MaterialTheme.colorScheme.outline, CircleShape) else Modifier)) {
                    Box(contentAlignment = Alignment.Center) { Text(workspace.name.take(2).uppercase(Locale.ROOT), fontWeight = FontWeight.Bold) }
                }
                Text(workspace.name, maxLines = 2, overflow = TextOverflow.Ellipsis, style = MaterialTheme.typography.labelSmall, modifier = Modifier.padding(top = 4.dp))
            }
        }
    }
}

@Composable
internal fun MessageList(state: CompanionUiState, modifier: Modifier, onLoadPrevious: (() -> Unit)? = null, onTurnAction: ((ChatBubble, String, String, (Boolean) -> Unit) -> Unit)? = null, onEdit: ((ChatBubble, String, String, (Boolean) -> Unit) -> Unit)? = null, onVersion: ((String) -> Unit)? = null, onHistoryAction: ((ChatBubble, Boolean, String, (Boolean) -> Unit) -> Unit)? = null) {
    val scroll = rememberSaveable(state.conversationId, saver = LazyListState.Saver) { LazyListState() }
    val itemCount by remember(scroll) { derivedStateOf { scroll.layoutInfo.totalItemsCount } }
    var following by rememberSaveable(state.conversationId) { mutableStateOf(true) }
    var deletionAnchor by remember(state.conversationId) { mutableStateOf<Triple<String, Int, Int>?>(null) }
    val dragging by scroll.interactionSource.collectIsDraggedAsState()
    LaunchedEffect(scroll, dragging) { if (dragging) following = false }
    LaunchedEffect(scroll, scroll.isScrollInProgress) { if (deletionAnchor == null && !scroll.isScrollInProgress && scroll.layoutInfo.visibleItemsInfo.lastOrNull()?.index == scroll.layoutInfo.totalItemsCount - 1) following = true }
    val bubbles = state.presentation.bubbles
    val currentSegment = state.snapshot?.segment?.optString("id")
    val finalizing = state.snapshot?.let { it.branchPreparing && it.tasks.lastOrNull()?.optString("status") in setOf("completed", "failed", "cancelled", "interrupted") } == true
    var showFinalizing by remember(state.conversationId) { mutableStateOf(false) }
    LaunchedEffect(state.conversationId, finalizing) {
        showFinalizing = false
        if (finalizing) { kotlinx.coroutines.delay(1_500); showFinalizing = true }
    }
    val context = LocalContext.current
    val canChangeHistory = state.connected && state.draftReady && !state.busy && state.pendingAction == null && state.snapshot?.versionsBusy != true && state.snapshot?.branchPreparing != true && state.snapshot?.tasks?.all { it.optString("status") in setOf("completed", "failed", "cancelled", "interrupted") } == true
    LaunchedEffect(scroll, bubbles.map { it.id to it.text.length }, itemCount) {
        val anchor = deletionAnchor
        if (anchor != null && bubbles.firstOrNull()?.id != anchor.first) {
            scroll.scrollToItem(anchor.second.coerceAtMost((bubbles.size - 1).coerceAtLeast(0)), anchor.third)
            deletionAnchor = null
        } else if (following && scroll.layoutInfo.totalItemsCount > 0) scroll.scrollToItem(scroll.layoutInfo.totalItemsCount - 1)
    }
    LazyColumn(state = scroll, modifier = modifier.fillMaxWidth(), contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        if (state.oldestSegmentNumber > 1) item(key = "segment-navigation") {
            Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
                Text("Compacted context · Segment ${state.oldestSegmentNumber}", style = MaterialTheme.typography.labelMedium)
                TextButton(enabled = state.previousSegmentId != null && !state.loadingSegment, onClick = { following = false; onLoadPrevious?.invoke() }) {
                    Text(if (state.loadingSegment) "Loading previous segment…" else "Load previous segment")
                }
            }
        }
        if (bubbles.isEmpty()) item { Column(Modifier.fillParentMaxSize().padding(28.dp), verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally) { Icon(Icons.Default.Code, null, Modifier.size(48.dp), tint = MaterialTheme.colorScheme.primary); Spacer(Modifier.height(12.dp)); Text("Your Windows workspace, here", style = MaterialTheme.typography.titleMedium); Text("Select a workspace, open a chat, or start a new one. Windows remains connected to the same conversation.", style = MaterialTheme.typography.bodyMedium, modifier = Modifier.padding(top = 8.dp)) } }
        // Publishing a regenerated branch adds its segment prefix. Keep the
        // current response's list identity so a reader's viewport survives it.
        items(bubbles, key = { bubble ->
            if (bubble.segmentId == null || bubble.segmentId == currentSegment)
                "current:" + bubble.id.removePrefix(bubble.segmentId?.let { "$it:" }.orEmpty())
            else bubble.id
        }) { bubble ->
            var expanded by rememberSaveable(bubble.id, bubble.live) { mutableStateOf(!bubble.thinking && !bubble.activity) }
            var menu by remember { mutableStateOf(false) }
            var deleting by rememberSaveable(bubble.id) { mutableStateOf(false) }
            var turnAction by rememberSaveable(bubble.id) { mutableStateOf<String?>(null) }
            var editing by rememberSaveable(bubble.id) { mutableStateOf(false) }
            var editedText by rememberSaveable(bubble.id) { mutableStateOf(bubble.text) }
            var editSubmission by remember(bubble.id) { mutableStateOf(java.util.UUID.randomUUID().toString()) }
            val submissionId = remember(bubble.id) { java.util.UUID.randomUUID().toString() }
            if (deleting) AlertDialog(onDismissRequest = { if (!state.busy) deleting = false }, title = { Text("Delete this message and everything below it?") },
                text = { Column { Text("Delete this message and everything after it? Earlier messages stay. Queued follow-ups are also removed."); state.historyError?.let { Text(it, color = MaterialTheme.colorScheme.error) } } },
                confirmButton = { TextButton(enabled = !state.busy, onClick = {
                    following = false
                    deletionAnchor = Triple(bubbles.firstOrNull()?.id.orEmpty(), scroll.firstVisibleItemIndex, scroll.firstVisibleItemScrollOffset)
                    onHistoryAction?.invoke(bubble, true, submissionId) { success -> if (success) deleting = false else deletionAnchor = null }
                }) { Text(if (state.busy) "Deleting…" else "Delete from here") } },
                dismissButton = { TextButton(enabled = !state.busy, onClick = { deleting = false }) { Text("Cancel") } })
            if (turnAction != null) AlertDialog(onDismissRequest = { if (!state.busy) turnAction = null },
                title = { Text("Delete only this message?") },
                text = { Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Delete this message? Other messages stay in the chat.")
                    state.historyError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                } },
                confirmButton = { TextButton(enabled = !state.busy, onClick = {
                    following = false
                    deletionAnchor = Triple(bubbles.firstOrNull()?.id.orEmpty(), scroll.firstVisibleItemIndex, scroll.firstVisibleItemScrollOffset)
                    onTurnAction?.invoke(bubble, turnAction!!, submissionId) { success -> if (success) turnAction = null else deletionAnchor = null }
                }) { Text(if (state.busy) "Working…" else "Delete message") } },
                dismissButton = { TextButton(enabled = !state.busy, onClick = { turnAction = null }) { Text("Cancel") } })
            if (editing) AlertDialog(onDismissRequest = { if (!state.busy) editing = false }, title = { Text("Edit message") }, text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    Text("Later messages stay in this chat. Future responses use your revised text.")
                    OutlinedTextField(value = editedText, onValueChange = { editedText = it; editSubmission = java.util.UUID.randomUUID().toString() }, enabled = !state.busy, label = { Text("Message text") }, modifier = Modifier.fillMaxWidth().heightIn(min = 160.dp, max = 320.dp))
                    state.historyError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                }
            }, confirmButton = { TextButton(enabled = !state.busy && editedText.isNotBlank(), onClick = {
                following = false
                onEdit?.invoke(bubble, editedText, editSubmission) { success -> if (success) editing = false }
            }) { Text(if (state.busy) "Saving…" else "Save message") } }, dismissButton = { TextButton(enabled = !state.busy, onClick = { editing = false }) { Text("Cancel") } })
            Row(Modifier.fillMaxWidth(), horizontalArrangement = if (bubble.user) Arrangement.End else Arrangement.Start) {
                Surface(shape = RoundedCornerShape(18.dp), color = if (bubble.user) MaterialTheme.colorScheme.primaryContainer else MaterialTheme.colorScheme.surfaceVariant, modifier = Modifier.fillMaxWidth(if (bubble.user) .86f else .96f)) {
                    Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            Text(bubble.title + if (bubble.live) " · live" else "", style = MaterialTheme.typography.labelMedium, fontWeight = FontWeight.Bold, modifier = Modifier.weight(1f))
                            if (bubble.messageId != null) Box {
                                FilledTonalIconButton(onClick = { menu = true }, modifier = Modifier.size(40.dp)) { Icon(Icons.Default.MoreVert, "Message actions", Modifier.size(24.dp)) }
                                DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                                    DropdownMenuItem(text = { Text("Edit") }, leadingIcon = { Icon(Icons.Default.Edit, null) }, enabled = canChangeHistory && bubble.complete && onEdit != null, onClick = { menu = false; editedText = bubble.text; editSubmission = java.util.UUID.randomUUID().toString(); editing = true })
                                    DropdownMenuItem(text = { Text("Delete") }, leadingIcon = { Icon(Icons.Default.Delete, null) }, enabled = canChangeHistory && bubble.complete && onTurnAction != null, onClick = { menu = false; turnAction = "delete-message" })
                                    DropdownMenuItem(text = { Text("Regenerate") }, leadingIcon = { Icon(Icons.Default.Refresh, null) }, enabled = canChangeHistory && bubble.complete && onTurnAction != null, onClick = {
                                        menu = false
                                        onTurnAction?.invoke(bubble, "regenerate", submissionId) { success -> if (success) { deletionAnchor = null; following = true } }
                                    })
                                    DropdownMenuItem(text = { Text("Copy") }, leadingIcon = { Icon(Icons.Default.ContentCopy, null) }, onClick = { menu = false; (context.getSystemService(android.content.Context.CLIPBOARD_SERVICE) as ClipboardManager).setPrimaryClip(ClipData.newPlainText("Chat message", bubble.text)) })
                                    if (onHistoryAction != null) {
                                        DropdownMenuItem(text = { Text("Fork from here") }, enabled = canChangeHistory && bubble.complete && bubble.messageId != "response", onClick = { menu = false; onHistoryAction(bubble, false, submissionId) {} })
                                        DropdownMenuItem(text = { Text("Delete from here") }, enabled = canChangeHistory && bubble.messageId != "response", onClick = { menu = false; deleting = true })
                                    }
                                }
                            }
                        }
                        if ((bubble.thinking || bubble.activity) && !bubble.live) {
                            Text(bubble.summary.ifBlank { activitySentence(bubble.text) }, style = MaterialTheme.typography.bodyMedium)
                            TextButton(onClick = { expanded = !expanded }, contentPadding = PaddingValues(0.dp)) { Text(if (expanded) "Hide details" else "View details") }
                        }
                        if (bubble.live || expanded) MarkdownContent(bubble.text)
                        bubble.replyVersions?.let { choice ->
                            val values = choice.optJSONArray("versions")
                            val versions = if (values == null) emptyList() else (0 until values.length()).map(values::getJSONObject)
                            val selected = versions.indexOfFirst { it.optString("id") == choice.optString("selectedVersionId") }
                            if (versions.size > 1) Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End, verticalAlignment = Alignment.CenterVertically) {
                                TextButton(enabled = canChangeHistory && onVersion != null && selected > 0, onClick = { onVersion?.invoke(versions[selected - 1].getString("conversationId")) }, modifier = Modifier.semantics { contentDescription = "Previous response" }) { Text("‹") }
                                Text("Reply ${selected + 1} of ${versions.size}", style = MaterialTheme.typography.labelSmall)
                                TextButton(enabled = canChangeHistory && onVersion != null && selected >= 0 && selected < versions.lastIndex, onClick = { onVersion?.invoke(versions[selected + 1].getString("conversationId")) }, modifier = Modifier.semantics { contentDescription = "Next response" }) { Text("›") }
                            }
                        }
                    }
                }
            }
        }
        if (showFinalizing) item(key = "saving-reply") { Text(if (state.snapshot?.branchPreparationError != null) "Saving reply… Retrying automatically." else "Saving reply…", style = MaterialTheme.typography.bodySmall) }
        item { Spacer(Modifier.height(1.dp)) }
    }
}

@Composable
internal fun Composer(state: CompanionUiState, running: Boolean, terminal: Boolean, onDraft: (String) -> Unit, onSend: (SendAction) -> Unit, onRemoveAttachment: (String) -> Unit, onAddFile: () -> Unit, onTakePhoto: () -> Unit, onClearPending: () -> Unit = {}, onBrowser: () -> Unit = {}, onCredentials: () -> Unit = {}) {
    val chatLoaded = state.conversationId == null || state.snapshot != null
    val canSend = state.draftReady && state.connected && !state.busy && !state.importing && chatLoaded && !(terminal && state.snapshot?.branchPreparing == true) && state.workspaceId != null && (state.draft.isNotBlank() || state.attachments.isNotEmpty()) && state.attachments.none { it.needsReselect }
    var mode by rememberSaveable(state.conversationId, running, terminal) { mutableStateOf(if (state.conversationId == null) SendAction.SEND else if (terminal) SendAction.CONTINUE else SendAction.QUEUE) }
    var menu by remember { mutableStateOf(false) }
    Surface(tonalElevation = 3.dp) {
        Column(Modifier.fillMaxWidth().padding(8.dp)) {
            if (state.workspaceId != null) TextButton(onClick = onBrowser, enabled = chatLoaded && state.draftReady && !state.busy && state.pendingAction == null) {
                Icon(Icons.Default.Language, null, Modifier.size(16.dp)); Spacer(Modifier.width(6.dp)); Text(state.browserSelection.summary(state.browserCatalog), maxLines = 1, overflow = TextOverflow.Ellipsis)
            }
            if (state.pendingAction != null && !state.busy) {
                Text("Delivery may already have succeeded. Refresh Windows before retrying this saved request.", style = MaterialTheme.typography.bodySmall)
                Row {
                    TextButton(onClick = { onSend(state.pendingAction) }, enabled = canSend) { Text("Retry saved delivery") }
                    TextButton(onClick = onClearPending) { Text("Clear draft") }
                }
            }
            if (state.attachments.any { it.needsReselect }) Text("Some original files need to be selected again. Remove the missing chips and reattach those files before sending.", color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
            if (state.attachments.isNotEmpty()) Row(Modifier.horizontalScroll(rememberScrollState())) {
                state.attachments.forEach { attachment -> InputChip(selected = false, onClick = {}, label = { Text("${attachment.name} · ${if (attachment.needsReselect) "reselect required" else formatBytes(attachment.size.toLong())}") }, trailingIcon = { IconButton(onClick = { onRemoveAttachment(attachment.id) }, enabled = !state.busy) { Icon(Icons.Default.Close, "Remove ${attachment.name}") } }, modifier = Modifier.padding(end = 4.dp)) }
            }
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                var attachments by remember { mutableStateOf(false) }
                Box {
                    IconButton(onClick = { attachments = true }, enabled = state.draftReady && !state.busy && !state.importing && state.workspaceId != null) { Icon(Icons.Default.Add, "Chat options") }
                    DropdownMenu(expanded = attachments, onDismissRequest = { attachments = false }) {
                        DropdownMenuItem(text = { Text("Add files") }, onClick = { attachments = false; onAddFile() })
                        DropdownMenuItem(text = { Text("Take picture") }, onClick = { attachments = false; onTakePhoto() })
                        DropdownMenuItem(text = { Text("Browser and profile") }, enabled = chatLoaded && state.pendingAction == null, onClick = { attachments = false; onBrowser() })
                        DropdownMenuItem(text = { Text("Secure credentials") }, enabled = state.connected && state.workspaceId != null, onClick = { attachments = false; onCredentials() })
                    }
                }
                OutlinedTextField(value = state.draft, onValueChange = onDraft, placeholder = { Text(if (state.host != null && !state.draftReady) "Restoring encrypted draft…" else "Message this workspace") }, minLines = 1, maxLines = 6, enabled = state.draftReady && state.workspaceId != null && !state.busy, modifier = Modifier.weight(1f))
                FilledIconButton(onClick = { onSend(state.pendingAction ?: mode) }, enabled = canSend) { Icon(Icons.AutoMirrored.Filled.Send, if (state.pendingAction != null) "Retry saved delivery" else mode.name.lowercase().replaceFirstChar(Char::uppercase), Modifier.size(22.dp)) }
            }
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                Box {
                    TextButton(onClick = { menu = true }, enabled = state.conversationId != null && !state.busy && state.pendingAction == null) { Text(when (state.pendingAction ?: mode) { SendAction.SEND -> "New chat"; SendAction.CONTINUE -> "Continue"; SendAction.STEER -> "Steer active turn"; SendAction.QUEUE -> "Queue follow-up" }); Icon(Icons.Default.ArrowDropDown, null) }
                    DropdownMenu(expanded = menu, onDismissRequest = { menu = false }) {
                        if (terminal) DropdownMenuItem(text = { Text("Continue") }, onClick = { mode = SendAction.CONTINUE; menu = false })
                        if (running) DropdownMenuItem(text = { Text("Steer active turn") }, onClick = { mode = SendAction.STEER; menu = false })
                        DropdownMenuItem(text = { Text("Queue follow-up") }, onClick = { mode = SendAction.QUEUE; menu = false })
                    }
                }
                Text("${state.attachments.size}/10 files · 16 MiB each", style = MaterialTheme.typography.labelSmall, modifier = Modifier.weight(1f))
            }
        }
    }
}

@Composable
private fun PairDialog(onDismiss: () -> Unit, onScan: () -> Unit, onPair: (String) -> Unit) {
    var invitation by remember { mutableStateOf("") }
    AlertDialog(onDismissRequest = onDismiss, title = { Text("Pair a Windows install") }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("On Windows, open Pair Android → Create pairing QR. Connect this phone's Tailscale to the same tailnet as Windows, then scan the invitation or paste its JSON.")
            OutlinedButton(onClick = onScan, modifier = Modifier.fillMaxWidth()) { Icon(Icons.Default.QrCodeScanner, null); Spacer(Modifier.width(8.dp)); Text("Scan invitation locally") }
            OutlinedTextField(value = invitation, onValueChange = { invitation = it }, label = { Text("Or paste invitation JSON") }, minLines = 2, maxLines = 4, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
        }
    }, confirmButton = { TextButton(onClick = { onPair(invitation) }, enabled = invitation.isNotBlank()) { Text("Pair") } }, dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } })
}

private fun formatBytes(bytes: Long): String = if (bytes >= 1024 * 1024) "%.1f MiB".format(Locale.ROOT, bytes / 1048576.0) else "%.1f KiB".format(Locale.ROOT, bytes / 1024.0)

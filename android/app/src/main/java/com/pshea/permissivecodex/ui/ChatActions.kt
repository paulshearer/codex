package com.pshea.permissivecodex.ui

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.ui.unit.dp
import com.pshea.permissivecodex.data.validChatTitle

internal fun copyChatId(threadId: String?, writeClipboard: (String) -> Unit): Boolean {
    if (threadId.isNullOrBlank()) return false
    return try { writeClipboard(threadId); true } catch (_: RuntimeException) { false }
}

@Composable
internal fun ChatActionItems(threadId: String?, ready: Boolean, onRename: () -> Unit, onCopy: () -> Unit, onDelete: () -> Unit) {
    DropdownMenuItem(text = { Text("Rename chat") }, enabled = ready, onClick = onRename)
    DropdownMenuItem(text = { Column {
        Text("Copy chat ID")
        if (threadId == null) Text("Available when the task starts", style = MaterialTheme.typography.bodySmall)
    } }, enabled = threadId != null, onClick = onCopy)
    DropdownMenuItem(text = { Text("Delete chat") }, enabled = ready, onClick = onDelete)
}

@Composable
internal fun RenameChatDialog(initialTitle: String, saving: Boolean, error: String?, onSave: (String) -> Unit, onDismiss: () -> Unit) {
    var title by remember { mutableStateOf(initialTitle) }
    AlertDialog(onDismissRequest = { if (!saving) onDismiss() }, title = { Text("Rename chat") }, text = {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedTextField(value = title, onValueChange = { title = it }, label = { Text("Chat name") },
                singleLine = true, enabled = !saving, modifier = Modifier.fillMaxWidth())
            Text("Use a single line of up to 120 characters.", style = MaterialTheme.typography.bodySmall)
            error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
        }
    }, confirmButton = {
        TextButton(onClick = { onSave(title.trim()) }, enabled = !saving && validChatTitle(title)) { Text(if (saving) "Saving…" else "Save") }
    }, dismissButton = { TextButton(onClick = onDismiss, enabled = !saving) { Text("Cancel") } })
}

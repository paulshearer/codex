package com.pshea.permissivecodex.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.pshea.permissivecodex.data.BrowserCatalog
import com.pshea.permissivecodex.data.BrowserSelection
import org.json.JSONObject

@Composable
fun BrowserSelectionDialog(
    selection: BrowserSelection,
    catalog: JSONObject?,
    loading: Boolean,
    error: String?,
    onRefresh: () -> Unit,
    onSave: (BrowserSelection) -> Unit,
    onDismiss: () -> Unit
) {
    var mode by rememberSaveable(selection) { mutableStateOf(selection.browserMode) }
    var browser by rememberSaveable(selection) { mutableStateOf(selection.browser) }
    var profile by rememberSaveable(selection) { mutableStateOf(selection.browserProfile) }
    var customProfile by rememberSaveable(selection) { mutableStateOf(false) }
    val draft = BrowserSelection(mode, browser, profile)
    val attached = mode == "attached"
    val browsers = BrowserCatalog.fromJson(catalog)
    val profiles = browsers.profileChoices(draft)
    val validationError = if (customProfile && profile.isBlank()) "Enter a name for the new profile." else draft.validationError()

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Remote browser") },
        text = {
            Column(Modifier.fillMaxWidth().heightIn(max = 500.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("Choose the browser on your paired Windows host. This setting applies to your next message; it does not switch a running task.", style = MaterialTheme.typography.bodySmall)
                BrowserChoiceField("Browser mode", mode, listOf(
                    BrowserChoice("dedicated", "Dedicated browser"),
                    BrowserChoice("attached", "Existing signed-in browser")
                )) { next ->
                    mode = next
                    if (next == "attached" && browser == "chromium") browser = "chrome"
                    profile = ""
                    customProfile = false
                }
                val browserChoices = BrowserSelection.browserNames.mapNotNull { (id, name) ->
                    when {
                        attached && id == "chromium" && browser != "chromium" -> null
                        attached && id == "chromium" -> BrowserChoice(id, "Any connected browser (legacy setting)")
                        else -> {
                            val installed = browsers.entries.firstOrNull { it.id == id }?.installed != false
                            BrowserChoice(id, name + if (!attached && !installed) " (not installed)" else "", attached || installed)
                        }
                    }
                }
                BrowserChoiceField("Browser", browser, browserChoices) { next ->
                    browser = next
                    profile = ""
                    customProfile = false
                }
                BrowserChoiceField("Browser profile", if (customProfile && !attached) "__new" else profile,
                    listOf(BrowserChoice("", if (attached) "Any connected profile of this browser" else "Separate profile for each conversation")) +
                        profiles.map { BrowserChoice(it.id, it.label) } +
                        if (attached) emptyList() else listOf(BrowserChoice("__new", "New named profile…"))) { next ->
                    customProfile = next == "__new"
                    profile = if (customProfile) "" else next
                }
                if (customProfile && !attached) {
                    OutlinedTextField(profile, { profile = it }, label = { Text("New profile name") }, placeholder = { Text("work") },
                        singleLine = true, isError = validationError != null, modifier = Modifier.fillMaxWidth(),
                        supportingText = { Text(validationError ?: "Letters, numbers, underscores or hyphens; up to 64 characters.") })
                } else if (validationError != null) Text(validationError, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall)
                if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                TextButton(onClick = onRefresh, enabled = !loading) { Text(if (loading) "Refreshing profiles…" else "Refresh browser profiles") }
                if (attached) {
                    Text("Connected profiles are ready to use. To connect another profile, use the Windows app on the paired host, then refresh here.", style = MaterialTheme.typography.bodySmall)
                } else {
                    Text("Dedicated profiles keep their own sign-ins and cookies. Named profiles can be reused across chats; one task can use a profile at a time.", style = MaterialTheme.typography.bodySmall)
                }
                error?.takeIf(String::isNotBlank)?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
            }
        },
        confirmButton = { TextButton(onClick = { onSave(draft) }, enabled = validationError == null && !loading) { Text("Save browser") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } }
    )
}

private data class BrowserChoice(val id: String, val label: String, val enabled: Boolean = true)

@Composable
private fun BrowserChoiceField(label: String, selected: String, choices: List<BrowserChoice>, onSelect: (String) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(label, style = MaterialTheme.typography.labelLarge)
        Box {
            OutlinedButton(onClick = { expanded = true }, modifier = Modifier.fillMaxWidth()) {
                Text(choices.firstOrNull { it.id == selected }?.label ?: selected, modifier = Modifier.weight(1f))
                Icon(Icons.Default.ArrowDropDown, "Choose $label")
            }
            DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }, modifier = Modifier.heightIn(max = 320.dp)) {
                choices.forEach { choice -> DropdownMenuItem(text = { Text(choice.label) }, enabled = choice.enabled, onClick = { expanded = false; onSelect(choice.id) }) }
            }
        }
    }
}

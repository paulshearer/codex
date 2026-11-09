package com.pshea.permissivecodex.ui

import android.graphics.Color
import android.text.InputType
import android.text.method.PasswordTransformationMethod
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.window.DialogProperties
import androidx.compose.ui.window.SecureFlagPolicy
import androidx.core.widget.doAfterTextChanged
import com.pshea.permissivecodex.CompanionViewModel
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.net.URI
import java.text.DateFormat
import java.util.Date

@Composable
internal fun SecureCredentialsDialog(workspaceId: String, connected: Boolean, viewModel: CompanionViewModel, onDismiss: () -> Unit) {
    val scope = rememberCoroutineScope()
    // No rememberSaveable, ViewModel credential state, or draft storage.
    var label by remember { mutableStateOf("") }
    var origin by remember { mutableStateOf("") }
    var username by remember { mutableStateOf("") }
    var password by remember { mutableStateOf("") }
    var hours by remember { mutableStateOf("4") }
    var timed by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var entries by remember { mutableStateOf(emptyList<JSONObject>()) }
    var usernameView by remember { mutableStateOf<EditText?>(null) }
    var passwordView by remember { mutableStateOf<EditText?>(null) }
    fun clear() { usernameView?.setText(""); passwordView?.setText(""); username = ""; password = "" }
    suspend fun refresh() {
        val result = viewModel.temporaryCredentials(workspaceId).getJSONArray("data")
        entries = (0 until result.length()).map { result.getJSONObject(it) }
    }
    fun revoke(id: String?) {
        scope.launch {
            busy = true; error = null
            try { viewModel.revokeCredential(workspaceId, id); refresh() }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { error = "Revocation could not be confirmed. Refresh and retry." }
            finally { busy = false }
        }
    }
    LaunchedEffect(workspaceId, connected) {
        if (connected) while (true) {
            try { refresh() }
            catch (cancelled: CancellationException) { throw cancelled }
            catch (_: Exception) { error = "Secure credentials are unavailable. Check the execution service." }
            delay(5000)
        }
    }
    DisposableEffect(Unit) { onDispose { clear() } }
    AlertDialog(onDismissRequest = { if (!busy) { clear(); onDismiss() } },
        properties = DialogProperties(securePolicy = SecureFlagPolicy.SecureOn),
        title = { Text("Secure credentials") }, text = {
            Column(Modifier.fillMaxWidth().verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("Available to Codex in this workspace. Each entry uses a fresh key held only in the execution service’s memory.")
                Text("Includes sign-in subdomains of the same registered HTTPS website at the same port. Other websites and hosted tenants stay separate.", style = MaterialTheme.typography.bodySmall)
                Text("Session ends when revoked or the execution service restarts. Timed entries also clear on restart. Expiry stops future fills; it does not sign out websites.", style = MaterialTheme.typography.bodySmall)
                entries.forEach { entry ->
                    Text(entry.getString("label") + "\n" + entry.getString("origin") + "\n" +
                        if (entry.isNull("expiresAt")) "Session" else "Expires " + DateFormat.getDateTimeInstance().format(Date(entry.getLong("expiresAt") * 1000)))
                    TextButton(onClick = { revoke(entry.getString("id")) }, enabled = connected && !busy) { Text("Revoke ${entry.getString("label")}") }
                }
                OutlinedTextField(label, { if (it.length <= 80) label = it }, label = { Text("Label") }, singleLine = true, enabled = !busy)
                OutlinedTextField(origin, { if (it.length <= 512) origin = it }, label = { Text("Website origin (https://example.com)") }, singleLine = true, enabled = !busy)
                Text("Paste or type the username and password, then choose how long they can be used.", style = MaterialTheme.typography.bodySmall)
                SecureCredentialInput("Username", false, busy, { usernameView = it }, { username = it })
                SecureCredentialInput("Password", true, busy, { passwordView = it }, { password = it })
                Row { Checkbox(timed, { timed = it }, enabled = !busy); Text(if (timed) "Number of hours" else "Session") }
                if (timed) OutlinedTextField(hours, { hours = it.filter(Char::isDigit).take(3) }, label = { Text("Hours (1–168)") }, singleLine = true, enabled = !busy)
                Button(enabled = connected && !busy, onClick = {
                    val duration = hours.toIntOrNull()
                    val validOrigin = try { val uri = URI(origin); uri.scheme == "https" && uri.host != null && uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null && uri.path in listOf("", "/") } catch (_: Exception) { false }
                    if (label.isBlank() || !validOrigin || username.isEmpty() || password.isEmpty() || timed && (duration == null || duration !in 1..168)) {
                        error = "Enter a label, HTTPS website origin, username, password, and a valid duration."
                    } else {
                        val payload = JSONObject().put("label", label.trim()).put("origin", origin).put("username", username)
                            .put("password", password).put("lifetime", if (timed) "hours" else "session")
                        if (timed) payload.put("hours", duration)
                        clear(); busy = true; error = null
                        scope.launch {
                            try { viewModel.provideCredential(workspaceId, payload); refresh() }
                            catch (cancelled: CancellationException) { throw cancelled }
                            catch (_: Exception) { error = "Handoff could not be confirmed. Check the list before entering credentials again." }
                            finally { payload.remove("username"); payload.remove("password"); busy = false }
                        }
                    }
                }) { Text("Provide securely") }
                error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                TextButton(onClick = { revoke(null) }, enabled = connected && !busy) { Text("Revoke all in this workspace") }
            }
        }, confirmButton = { TextButton(onClick = { clear(); onDismiss() }, enabled = !busy) { Text("Close") } })
}

/** Native inputs retain Android's manual paste action without requesting a password manager. */
@Composable
internal fun SecureCredentialInput(label: String, secret: Boolean, busy: Boolean, onView: (EditText) -> Unit, onChange: (String) -> Unit) {
    val color = MaterialTheme.colorScheme.onSurface
    AndroidView(modifier = Modifier.fillMaxWidth(), factory = { context ->
        EditText(context).apply {
            id = View.generateViewId(); hint = label; contentDescription = label
            isSaveEnabled = false; isSaveFromParentEnabled = false
            importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
            if (android.os.Build.VERSION.SDK_INT >= 30) importantForContentCapture = View.IMPORTANT_FOR_CONTENT_CAPTURE_NO
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT or (if (secret) InputType.TYPE_TEXT_VARIATION_PASSWORD else InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS)
            if (secret) transformationMethod = PasswordTransformationMethod.getInstance()
            imeOptions = EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING or EditorInfo.IME_FLAG_NO_EXTRACT_UI
            filters = arrayOf(android.text.InputFilter.LengthFilter(if (secret) 8192 else 2048))
            setTextColor(Color.argb(255, (color.red * 255).toInt(), (color.green * 255).toInt(), (color.blue * 255).toInt()))
            setHintTextColor(Color.GRAY)
            doAfterTextChanged { onChange(it?.toString().orEmpty()) }
            onView(this)
        }
    }, update = { it.isEnabled = !busy })
}

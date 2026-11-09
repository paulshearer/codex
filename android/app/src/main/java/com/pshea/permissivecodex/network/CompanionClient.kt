package com.pshea.permissivecodex.network

import com.pshea.permissivecodex.data.CompanionEvent
import com.pshea.permissivecodex.data.EventReplayPage
import com.pshea.permissivecodex.data.ConversationPage
import com.pshea.permissivecodex.data.ConversationSnapshot
import com.pshea.permissivecodex.data.ConversationSummary
import com.pshea.permissivecodex.data.PairedHost
import com.pshea.permissivecodex.data.Workspace
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.launch
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOn
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.ConnectionSpec
import okhttp3.HttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.io.OutputStream
import java.net.Proxy
import java.util.Base64
import java.util.UUID
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

class CompanionException(val status: Int, val code: String, message: String) : IOException(message)

/** No redirects, proxies, public DNS addresses, plaintext HTTP, cookies or persistent response cache. */
class CompanionClient(val host: PairedHost, private val keys: DeviceKeys = DeviceKeys()) {
    private val origin = TailnetPolicy.origin(host.origin)
    private val http = clientBuilder().callTimeout(60, TimeUnit.SECONDS).readTimeout(35, TimeUnit.SECONDS).build()
    private val streamHttp = clientBuilder().callTimeout(0, TimeUnit.SECONDS).readTimeout(35, TimeUnit.SECONDS).build()

    suspend fun getIdentity(): JSONObject = json("GET", "/mobile/v1/identity").also {
        require(it.getString("installationId") == host.installationId && it.getString("deviceId") == host.deviceId) {
            "This endpoint belongs to a different pairing. Pair it again from Windows."
        }
    }
    suspend fun getWorkspaces(): List<Workspace> = json("GET", "/v1/workspaces").objects("data").map {
        Workspace(it.getString("id"), it.getString("name"), it.optString("workingDirectory"), it)
    }
    suspend fun getTask(id: String): JSONObject = json("GET", "/v1/tasks/${segment(id)}")
    suspend fun getBrowsers(): JSONObject = json("GET", "/v1/browsers")
    suspend fun getCredentials(workspaceId: String): JSONObject = json("GET", "/v1/credentials", query = mapOf("workspaceId" to workspaceId))
    suspend fun provideCredential(workspaceId: String, credential: JSONObject): JSONObject {
        val prepared = json("POST", "/v1/credentials/prepare", JSONObject().put("workspaceId", workspaceId))
        val envelope = withContext(Dispatchers.Default) { CredentialEnvelope.seal(prepared, workspaceId, credential) }
        credential.remove("username"); credential.remove("password")
        return json("POST", "/v1/credentials", envelope)
    }
    suspend fun revokeCredential(workspaceId: String, id: String?): JSONObject =
        json("DELETE", "/v1/credentials" + (id?.let { "/${segment(it)}" } ?: ""), query = mapOf("workspaceId" to workspaceId))
    suspend fun secureDelete(kind: String, id: String): JSONObject {
        require(kind in setOf("chat", "workspace"))
        return json("POST", "/v1/${if (kind == "chat") "chats" else "workspaces"}/${segment(id)}/secure-delete",
            JSONObject().put("confirm", id).put("passes", 7), maintenance = true)
    }
    suspend fun editHistory(id: String, taskId: String, messageId: String, submissionId: String, deleting: Boolean, text: String? = null, operation: String? = null, segmentId: String? = null): ConversationSnapshot {
        val body = JSONObject().put("taskId", taskId).put("messageId", messageId).put("submissionId", submissionId)
        require(operation == null || operation in setOf("delete-message", "regenerate"))
        if (deleting || operation == "delete-message") body.put("confirm", id)
        if (text != null) body.put("text", text)
        if (segmentId != null) body.put("segmentId", segmentId)
        return deliverHistoryRequest {
            conversationSnapshot(json("POST", "/v1/chats/${segment(id)}/${operation ?: if (text != null) "edit-message" else if (deleting) "delete-from" else "fork"}", body))
        }
    }
    suspend fun getConversations(workspaceId: String?, cursor: String? = null): ConversationPage {
        val result = json("GET", "/v1/chats", query = mapOf("workspaceId" to workspaceId, "cursor" to cursor))
        return ConversationPage(result.objects("data").map {
            ConversationSummary(it.getString("id"), it.getString("conversationId"), it.getString("workspaceId"),
                it.optString("title", it.optJSONObject("request")?.optString("prompt") ?: "Chat"),
                it.getString("status"), it.optLong("updatedAt"), it)
        }, result.optionalString("nextCursor"))
    }
    suspend fun getConversation(id: String, segmentId: String? = null): ConversationSnapshot {
        val value = json("GET", "/v1/chats/${segment(id)}", query = mapOf("segmentId" to segmentId))
        require(value.getString("id") == id) { "Unexpected conversation response." }
        return conversationSnapshot(value)
    }
    suspend fun renameConversation(id: String, title: String): String {
        val value = json("PATCH", "/v1/chats/${segment(id)}", JSONObject().put("title", title))
        require(value.getString("id") == id) { "Unexpected conversation response." }
        return value.getString("title")
    }
    suspend fun replayEvents(chatId: String, after: Long, through: Long? = null, segmentId: String? = null): EventReplayPage {
        require(after >= 0 && (through == null || through >= after))
        val result = json("GET", "/v1/chats/${segment(chatId)}/events", query = mapOf("format" to "json", "view" to "mobile",
            "after" to after.toString(), "through" to through?.toString(), "segmentId" to segmentId))
        val events = result.objects("data").map(::event)
        val cursor = if (result.has("nextCursor")) result.getLong("nextCursor") else events.lastOrNull()?.id ?: through ?: after
        require(cursor >= after && (through == null || cursor <= through)) { "Invalid history cursor." }
        return EventReplayPage(events, cursor)
    }
    fun observeEvents(chatId: String, after: Long): Flow<CompanionEvent> = flow {
        require(after >= 0)
        val call = streamHttp.newCall(request("GET", "/v1/chats/${segment(chatId)}/events", query = mapOf("after" to after.toString()),
            extra = mapOf("last-event-id" to after.toString())))
        val cancellation = call.cancelWithCoroutine()
        try {
            call.await().use { response ->
                checkResponse(response)
                require(response.header("Content-Type")?.substringBefore(';')?.trim() == "text/event-stream") { "Unexpected event stream." }
                val source = response.body?.source() ?: throw IOException("The event stream is unavailable.")
                val frame = StringBuilder()
                var cursor = after
                while (!source.exhausted()) {
                    currentCoroutineContext().ensureActive()
                    val line = source.readUtf8LineStrict(8L * 1024 * 1024)
                    if (line.isEmpty()) {
                        if (frame.isNotEmpty()) {
                            val value = try { event(JSONObject(frame.toString())) }
                                catch (_: org.json.JSONException) { throw IOException("The host returned an invalid event.") }
                            if (value.id > cursor) { emit(value); cursor = value.id }
                            frame.clear()
                        }
                    } else if (line.startsWith("data:")) {
                        if (frame.isNotEmpty()) frame.append('\n')
                        frame.append(line.substring(5).removePrefix(" "))
                        require(frame.length <= 8 * 1024 * 1024) { "Event exceeds the phone limit." }
                    }
                }
            }
        } finally { cancellation.cancel(); call.cancel() }
    }.flowOn(Dispatchers.IO)

    suspend fun submit(workspaceId: String, prompt: String, options: JSONObject = JSONObject(), attachments: List<String> = emptyList(),
                       submissionId: String = UUID.randomUUID().toString()): JSONObject {
        val body = message(prompt, options, attachments, submissionId).put("workspaceId", workspaceId)
        require(!body.has("workingDirectory") && !body.has("cwd") && !body.has("directory"))
        return json("POST", "/v1/tasks", body, extra = mapOf("idempotency-key" to submissionId))
    }
    suspend fun continueTask(taskId: String, prompt: String, options: JSONObject = JSONObject(), attachments: List<String> = emptyList(),
                             submissionId: String = UUID.randomUUID().toString()): JSONObject =
        json("POST", "/v1/tasks/${segment(taskId)}/continue", message(prompt, options, attachments, submissionId),
            extra = mapOf("idempotency-key" to submissionId))
    suspend fun steer(taskId: String, prompt: String, expectedTurnId: String, attachments: List<String> = emptyList(),
                      submissionId: String = UUID.randomUUID().toString()): JSONObject =
        json("POST", "/v1/tasks/${segment(taskId)}/steer", message(prompt, JSONObject(), attachments, submissionId)
            .put("expectedTurnId", expectedTurnId))
    suspend fun queueFollowup(chatId: String, prompt: String, options: JSONObject = JSONObject(), attachments: List<String> = emptyList(),
                             submissionId: String = UUID.randomUUID().toString()): JSONObject =
        json("POST", "/v1/chats/${segment(chatId)}/followups", message(prompt, options, attachments, submissionId))
    suspend fun pause(taskId: String): JSONObject = control(taskId, "pause")
    suspend fun resume(taskId: String): JSONObject = control(taskId, "resume")
    suspend fun cancel(taskId: String): JSONObject = control(taskId, "cancel")
    suspend fun retry(taskId: String): JSONObject = control(taskId, "retry")
    private suspend fun control(taskId: String, action: String) = json("POST", "/v1/tasks/${segment(taskId)}/$action")
    suspend fun removeFollowup(chatId: String, followupId: String): JSONObject =
        json("DELETE", "/v1/chats/${segment(chatId)}/followups/${segment(followupId)}")
    suspend fun resumeQueue(chatId: String): JSONObject = json("POST", "/v1/chats/${segment(chatId)}/followups/resume")
    suspend fun uploadAttachment(workspaceId: String, name: String, bytes: ByteArray): JSONObject {
        require(bytes.isNotEmpty() && bytes.size <= 16 * 1024 * 1024) { "Files must be nonempty and at most 16 MiB." }
        return json("POST", "/v1/attachments", JSONObject().put("workspaceId", workspaceId).put("name", name)
            .put("data", Base64.getEncoder().encodeToString(bytes)))
    }
    suspend fun removeAttachment(id: String): JSONObject = json("DELETE", "/v1/attachments/${segment(id)}")
    suspend fun attachmentInfo(id: String): JSONObject = json("GET", "/v1/attachments/${segment(id)}")
    suspend fun downloadAttachment(id: String, sink: OutputStream): Long = download("/v1/attachments/${segment(id)}/content", sink)
    suspend fun downloadArtifact(taskId: String, artifactId: String, sink: OutputStream): Long =
        download("/v1/tasks/${segment(taskId)}/artifacts/${segment(artifactId)}", sink)
    private suspend fun download(route: String, sink: OutputStream): Long = withContext(Dispatchers.IO) {
        val call = streamHttp.newCall(request("GET", route))
        val cancellation = call.cancelWithCoroutine()
        try { call.await().use { response ->
            checkResponse(response)
            val body = response.body ?: throw IOException("The download is unavailable.")
            require(body.contentLength() <= MAX_DOWNLOAD_BYTES) { "Download exceeds the Windows artifact limit." }
            var total = 0L
            body.byteStream().use { input ->
                val buffer = ByteArray(64 * 1024)
                while (true) {
                    currentCoroutineContext().ensureActive()
                    val count = input.read(buffer); if (count < 0) break
                    total += count
                    require(total <= MAX_DOWNLOAD_BYTES) { "Download exceeds the Windows artifact limit." }
                    sink.write(buffer, 0, count)
                }
            }
            total
        } } finally { cancellation.cancel(); call.cancel() }
    }

    private fun request(method: String, route: String, body: JSONObject? = null, query: Map<String, String?> = emptyMap(),
                        extra: Map<String, String> = emptyMap()): Request {
        require(route.startsWith("/v1/") || route == "/mobile/v1/identity" || route == "/mobile/v1/enroll")
        val url = origin.newBuilder().encodedPath(route).apply { query.forEach { (name, value) -> if (value != null) addQueryParameter(name, value) } }.build()
        val bytes = body?.toString()?.toByteArray(Charsets.UTF_8) ?: ByteArray(0)
        val headers = if (body == null) extra else extra + ("content-type" to "application/json")
        val authority = url.host + if (url.port == 443) "" else ":${url.port}"
        val signed = keys.headers(host.keyAlias, host.installationId, method, authority, url.encodedPath,
            url.encodedQuery?.let { "?$it" } ?: "?", bytes, headers)
        return Request.Builder().url(url).apply { signed.forEach { (name, value) -> header(name, value) } }
            .method(method, if (body != null) bytes.toRequestBody("application/json".toMediaType())
                else if (method in listOf("POST", "PUT", "PATCH")) ByteArray(0).toRequestBody(null) else null).build()
    }
    private suspend fun json(method: String, route: String, body: JSONObject? = null, query: Map<String, String?> = emptyMap(),
                             extra: Map<String, String> = emptyMap(), maintenance: Boolean = false): JSONObject = withContext(Dispatchers.IO) {
        val transport = if (maintenance) http.newBuilder().callTimeout(10, TimeUnit.MINUTES).readTimeout(10, TimeUnit.MINUTES).build() else http
        val call = transport.newCall(request(method, route, body, query, extra))
        val cancellation = call.cancelWithCoroutine()
        try { call.await().use { response ->
            checkResponse(response)
            val source = response.body?.source() ?: throw IOException("The host returned an empty response.")
            require((response.body?.contentLength() ?: 0) <= MAX_JSON) { "Host response exceeds the phone limit." }
            require(!source.request(MAX_JSON + 1)) { "Host response exceeds the phone limit." }
            val raw = source.readUtf8()
            try { JSONObject(raw) } catch (_: Exception) { throw IOException("The host returned an invalid response.") }
        } } finally { cancellation.cancel(); call.cancel() }
    }
    private fun checkResponse(response: Response) {
        if (response.isSuccessful) return
        // Backend messages are fixed safe text, but never display arbitrary proxy/upstream bodies.
        val code = when (response.code) { 401 -> "unauthorized"; 403 -> "forbidden"; 404 -> "not_found"; 409 -> "conflict"; 410 -> "expired"; 503 -> "unavailable"; else -> "request_failed" }
        val message = when (response.code) {
            401 -> "This phone is not authorized. Check its pairing on Windows."
            403 -> "Connect through Tailscale and check companion access on Windows."
            404 -> "The selected item is unavailable. Refresh this chat."
            409 -> "The task changed or delivery is uncertain. Refresh and check Windows before retrying."
            410 -> "The pairing invitation expired. Create another on Windows."
            503 -> "The Windows companion or controller is unavailable."
            in 300..399 -> "The host redirected this request. Pair the current Tailscale address again."
            else -> "The host could not accept this request (HTTP ${response.code})."
        }
        throw CompanionException(response.code, code, message)
    }
    companion object {
        private const val MAX_JSON = 32L * 1024 * 1024
        internal const val MAX_DOWNLOAD_BYTES = 2L * 1024 * 1024 * 1024
        private fun clientBuilder() = OkHttpClient.Builder().dns(TailnetPolicy.dns).proxy(Proxy.NO_PROXY)
            .followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false)
            .connectionSpecs(listOf(ConnectionSpec.MODERN_TLS)).connectTimeout(10, TimeUnit.SECONDS)
        suspend fun enroll(invitation: PairingInvitation, deviceName: String, keys: DeviceKeys = DeviceKeys()): PairedHost {
            require(deviceName.isNotBlank() && deviceName.length <= 120)
            require(invitation.expiresAt > System.currentTimeMillis() / 1000) { "This pairing QR has expired." }
            val alias = keys.ensure(invitation.installationId)
            val spki = keys.publicKey(alias)
            val host = PairedHost(invitation.installationId, invitation.origin, TailnetPolicy.origin(invitation.origin).host,
                alias, MobileSigning.fingerprint(spki), System.currentTimeMillis() / 1000)
            val client = CompanionClient(host, keys)
            val value = client.json("POST", "/mobile/v1/enroll", JSONObject().put("invitationId", invitation.invitationId)
                .put("secret", invitation.secret).put("deviceName", deviceName).put("publicKey", Base64.getEncoder().encodeToString(spki)))
            require(value.getString("installationId") == host.installationId && value.getString("deviceId") == host.deviceId &&
                TailnetPolicy.origin(value.getString("origin")) == TailnetPolicy.origin(host.origin) && value.getInt("protocolVersion") == 1) {
                "This pairing response does not match the Windows invitation."
            }
            return host
        }
        private fun segment(value: String): String {
            require(value.isNotEmpty() && value.length <= 512 && value !in listOf(".", "..") &&
                value.none { it == '/' || it == '\\' || it.code < 32 }) { "Invalid item identifier." }
            return HttpUrl.Builder().scheme("https").host("unused.invalid").addPathSegment(value).build().encodedPath.removePrefix("/")
        }
        private fun message(prompt: String, options: JSONObject, attachments: List<String>, submissionId: String): JSONObject {
            UUID.fromString(submissionId)
            require(prompt.isNotBlank() && prompt.toByteArray().size <= 4 * 1024 * 1024)
            require(attachments.size <= 10)
            require(options.keys().asSequence().all { it in setOf("mode", "goal", "skills", "browserMode", "browser", "browserProfile") }) { "Unsupported chat option." }
            return JSONObject(options.toString()).put("prompt", prompt).put("submissionId", submissionId).apply {
                if (attachments.isNotEmpty()) put("attachments", JSONArray(attachments))
            }
        }
        private fun event(value: JSONObject) = CompanionEvent(value.getLong("id"), value.getString("taskId"),
            value.getString("kind"), value.optJSONObject("data") ?: JSONObject().put("value", value.opt("data") ?: JSONObject.NULL), value.getDouble("createdAt"))
        private fun JSONObject.objects(key: String): List<JSONObject> {
            val values = optJSONArray(key) ?: return emptyList()
            return (0 until values.length()).map(values::getJSONObject)
        }
        private fun JSONObject.optionalString(key: String): String? = if (isNull(key)) null else optString(key).takeIf(String::isNotEmpty)
        private suspend fun Call.await(): Response = suspendCancellableCoroutine { continuation ->
            continuation.invokeOnCancellation { cancel() }
            enqueue(object : Callback {
                override fun onFailure(call: Call, error: IOException) { if (continuation.isActive) continuation.resumeWithException(IOException("Connection to the paired Windows host failed.", error)) }
                override fun onResponse(call: Call, response: Response) {
                    continuation.resume(response, onCancellation = { _, value, _ -> value.close() })
                }
            })
        }
        private suspend fun Call.cancelWithCoroutine(): Job {
            val call = this
            return CoroutineScope(currentCoroutineContext()).launch(start = CoroutineStart.UNDISPATCHED) {
                try { awaitCancellation() } finally { call.cancel() }
            }
        }
    }
}

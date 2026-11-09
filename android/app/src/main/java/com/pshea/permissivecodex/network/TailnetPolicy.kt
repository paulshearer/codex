package com.pshea.permissivecodex.network

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import kotlinx.coroutines.delay
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.Dns
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import org.json.JSONObject
import java.net.InetAddress
import java.net.UnknownHostException
import java.util.UUID

object TailnetPolicy {
    fun origin(value: String): HttpUrl {
        val url = value.toHttpUrl()
        require(url.scheme == "https" && url.port == 9444 && url.username.isEmpty() && url.password.isEmpty() &&
            url.encodedPath == "/" && url.query == null && url.fragment == null &&
            url.host.endsWith(".ts.net") && url.host.split('.').size >= 4 &&
            url.host.all { it.isLetterOrDigit() || it == '.' || it == '-' }) {
            "Pair with the Windows companion's Tailscale HTTPS address on port 9444."
        }
        return url
    }
    fun isTailscaleAddress(address: InetAddress): Boolean {
        val bytes = address.address
        return (bytes.size == 4 && bytes[0].toInt() and 255 == 100 && bytes[1].toInt() and 255 in 64..127) ||
            (bytes.size == 16 && bytes.take(6).map { it.toInt() and 255 } == listOf(0xfd, 0x7a, 0x11, 0x5c, 0xa1, 0xe0))
    }
    val dns: Dns = object : Dns {
        override fun lookup(hostname: String): List<InetAddress> {
            require(hostname.endsWith(".ts.net")) { "A Tailscale hostname is required." }
            return Dns.SYSTEM.lookup(hostname).filter(::isTailscaleAddress).ifEmpty {
                throw UnknownHostException("The paired host has no Tailscale address.")
            }
        }
    }
}

data class PairingInvitation(val version: Int, val origin: String, val installationId: String,
                             val invitationId: String, val secret: String, val expiresAt: Long) {
    companion object {
        fun parse(raw: String): PairingInvitation {
            require(raw.length <= 4096) { "This pairing QR is too large." }
            val value = JSONObject(raw)
            require(value.keys().asSequence().toSet() == setOf("version", "origin", "installationId", "invitationId", "secret", "expiresAt")) {
                "This is not a Permissive Codex pairing QR."
            }
            require(value.getInt("version") == 1) { "This pairing QR version is unsupported." }
            val origin = TailnetPolicy.origin(value.getString("origin")).toString().removeSuffix("/")
            val installation = value.getString("installationId")
            val invitation = value.getString("invitationId")
            require(UUID.fromString(installation).toString() == installation.lowercase()) { "Invalid host identity." }
            require(UUID.fromString(invitation).toString() == invitation.lowercase()) { "Invalid invitation identity." }
            val secret = value.getString("secret")
            require(secret.matches(Regex("[A-Za-z0-9_-]{43}"))) { "Invalid pairing secret." }
            val expires = value.getLong("expiresAt")
            require(expires > System.currentTimeMillis() / 1000) { "This pairing QR has expired. Create another on Windows." }
            return PairingInvitation(1, origin, installation, invitation, secret, expires)
        }
    }
}

/** Tailscale owns login/VPN approval. This only requests its documented existing receiver. */
class TailnetRecovery(private val context: Context) {
    private val attempts = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val lock = Mutex()
    private var running: Deferred<Boolean>? = null
    fun isConnected(): Boolean {
        val manager = context.getSystemService(ConnectivityManager::class.java) ?: return false
        return manager.allNetworks.any { network ->
            manager.getNetworkCapabilities(network)?.hasTransport(NetworkCapabilities.TRANSPORT_VPN) == true &&
                manager.getLinkProperties(network)?.linkAddresses?.any { TailnetPolicy.isTailscaleAddress(it.address) } == true
        }
    }
    fun openTailscale(): Boolean {
        val intent = context.packageManager.getLaunchIntentForPackage(PACKAGE) ?: return false
        context.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)); return true
    }
    suspend fun ensureConnected(onProgress: (String) -> Unit = {}): Boolean {
        if (isConnected()) return true
        onProgress("Starting Tailscale…")
        val attempt = lock.withLock {
            running?.takeIf { it.isActive } ?: attempts.async { startAndWait() }.also { running = it }
        }
        return attempt.await()
    }
    private suspend fun startAndWait(): Boolean {
        val receiver = ComponentName(PACKAGE, "$PACKAGE.IPNReceiver")
        val installed = runCatching { context.packageManager.getApplicationInfo(PACKAGE, 0).enabled }.getOrDefault(false)
        if (!installed) throw IllegalStateException("Install Tailscale, sign in and approve its VPN, then retry.")
        val enabled = runCatching { context.packageManager.getReceiverInfo(receiver, 0).let { it.enabled && it.exported } }.getOrDefault(false)
        if (!enabled) throw IllegalStateException("Open Tailscale to finish setup, then retry.")
        try { context.sendBroadcast(Intent("$PACKAGE.CONNECT_VPN").setComponent(receiver).addFlags(Intent.FLAG_INCLUDE_STOPPED_PACKAGES)) }
        catch (_: SecurityException) { throw IllegalStateException("Open Tailscale to finish setup, then retry.") }
        val connected = withTimeoutOrNull(60_000) {
            while (!isConnected()) delay(500)
            true
        } ?: false
        if (connected) return true
        throw IllegalStateException("Tailscale did not connect. Open it to finish sign-in or VPN approval, then retry.")
    }
    companion object { private const val PACKAGE = "com.tailscale.ipn" }
}

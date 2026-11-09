package com.pshea.permissivecodex.network

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import java.util.UUID

/** RFC 9421 companion profile. Kept independent of HTTP transport for cross-language vectors. */
object MobileSigning {
    private val optional = listOf("content-type", "idempotency-key", "last-event-id")
    private val required = listOf("@method", "@authority", "@path", "@query", "content-digest", "permissive-instance-id")
    fun digest(body: ByteArray): String = "sha-256=:${Base64.getEncoder().encodeToString(MessageDigest.getInstance("SHA-256").digest(body))}:"
    fun fingerprint(spki: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(spki).joinToString("") { "%02x".format(it.toInt() and 255) }

    data class Canonical(val input: String, val base: ByteArray)
    fun canonical(method: String, authority: String, path: String, query: String, body: ByteArray,
                  installationId: String, headers: Map<String, String>, keyId: String,
                  created: Long, nonce: String): Canonical {
        require(method.matches(Regex("[A-Z]+")) && path.startsWith("/") && query.startsWith("?"))
        require(keyId.matches(Regex("[0-9a-f]{64}")) && nonce.matches(Regex("[A-Za-z0-9_-]{22,64}")))
        require(listOf(authority, path, query, installationId).none { it.contains('\r') || it.contains('\n') })
        val normalized = headers.mapKeys { it.key.lowercase(java.util.Locale.ROOT) }
        require(normalized.size == headers.size && normalized.keys.all { it in optional }) { "Unexpected signature header" }
        val fields = required + optional.filter(normalized::containsKey)
        val params = "(${fields.joinToString(" ") { "\"$it\"" }});created=$created;expires=${created + 60};nonce=\"$nonce\";keyid=\"$keyId\";alg=\"ecdsa-p256-sha256\""
        val values = mapOf("@method" to method, "@authority" to authority, "@path" to path,
            "@query" to query, "content-digest" to digest(body), "permissive-instance-id" to installationId) + normalized
        require(values.values.none { it.contains('\r') || it.contains('\n') })
        val text = fields.joinToString("\n") { "\"$it\": ${values.getValue(it)}" } + "\n\"@signature-params\": $params"
        require(text.all { it.code in 0..127 }) { "Signature components must be ASCII" }
        return Canonical("permissive-mobile=$params", text.toByteArray(Charsets.US_ASCII))
    }

    /** JCA ECDSA uses ASN.1 DER; HTTP Message Signatures requires the fixed-width r || s form. */
    fun derToRaw(der: ByteArray): ByteArray {
        var offset = 0
        fun read(): Int { require(offset < der.size) { "Invalid ECDSA signature" }; return der[offset++].toInt() and 255 }
        fun length(): Int {
            val first = read()
            if (first < 128) return first
            require(first == 0x81) { "Invalid ECDSA signature" }
            return read().also { require(it >= 128) }
        }
        require(read() == 0x30)
        val sequenceLength = length()
        require(sequenceLength == der.size - offset)
        fun integer(): ByteArray {
            require(read() == 0x02)
            val size = length()
            require(size in 1..33 && offset + size <= der.size)
            val value = der.copyOfRange(offset, offset + size); offset += size
            require(value[0].toInt() and 0x80 == 0) { "Negative ECDSA integer" }
            val first = if (value.size > 1 && value[0] == 0.toByte()) 1 else 0
            require(first == 0 || value[1].toInt() and 0x80 != 0) { "Noncanonical ECDSA integer" }
            val unsigned = value.copyOfRange(first, value.size)
            require(unsigned.size <= 32)
            return ByteArray(32).also { unsigned.copyInto(it, 32 - unsigned.size) }
        }
        val r = integer(); val s = integer()
        require(offset == der.size)
        return r + s
    }
}

class DeviceKeys {
    private fun store() = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
    fun alias(installationId: String): String {
        UUID.fromString(installationId)
        return "permissive.phone.p256.$installationId"
    }
    @Synchronized fun ensure(installationId: String): String {
        val alias = alias(installationId)
        if (!store().containsAlias(alias)) {
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore").apply {
                initialize(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY)
                    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                    .setDigests(KeyProperties.DIGEST_SHA256).setUserAuthenticationRequired(false).build())
            }.generateKeyPair()
        }
        return alias
    }
    fun publicKey(alias: String): ByteArray = store().getCertificate(alias)?.publicKey?.encoded
        ?: throw IllegalStateException("The phone key is unavailable. Pair this host again.")
    fun sign(alias: String, bytes: ByteArray): ByteArray {
        val key = store().getKey(alias, null) as? java.security.PrivateKey
            ?: throw IllegalStateException("The phone key is unavailable. Pair this host again.")
        return MobileSigning.derToRaw(Signature.getInstance("SHA256withECDSA").run { initSign(key); update(bytes); sign() })
    }
    fun delete(alias: String) { require(alias.startsWith("permissive.phone.p256.")); store().deleteEntry(alias) }
    fun headers(alias: String, installationId: String, method: String, authority: String, path: String,
                query: String, body: ByteArray, extra: Map<String, String>): Map<String, String> {
        val nonce = Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(24).also(SecureRandom()::nextBytes))
        val canonical = MobileSigning.canonical(method, authority, path, query, body, installationId,
            extra, MobileSigning.fingerprint(publicKey(alias)), System.currentTimeMillis() / 1000, nonce)
        return extra + mapOf("Content-Digest" to MobileSigning.digest(body), "Permissive-Instance-Id" to installationId,
            "Signature-Input" to canonical.input,
            "Signature" to "permissive-mobile=:${Base64.getEncoder().encodeToString(sign(alias, canonical.base))}:")
    }
}

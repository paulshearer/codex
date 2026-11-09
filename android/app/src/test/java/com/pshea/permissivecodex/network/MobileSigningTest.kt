package com.pshea.permissivecodex.network

import org.junit.Assert.*
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.KeyFactory
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64

class MobileSigningTest {
    @Test fun canonicalProfileHasExactOrderAuthorityQueryAndNoTrailingNewline() {
        val value = MobileSigning.canonical("POST", "windows.tail123.ts.net:9444", "/v1/tasks", "?",
            "{}".toByteArray(), "cfc6921e-77d1-4447-8dcf-2af33eb694cd",
            linkedMapOf("last-event-id" to "24", "content-type" to "application/json", "idempotency-key" to "abc"),
            "a".repeat(64), 1700000000, "A".repeat(32))
        assertEquals("permissive-mobile=(\"@method\" \"@authority\" \"@path\" \"@query\" \"content-digest\" \"permissive-instance-id\" \"content-type\" \"idempotency-key\" \"last-event-id\");created=1700000000;expires=1700000060;nonce=\"${"A".repeat(32)}\";keyid=\"${"a".repeat(64)}\";alg=\"ecdsa-p256-sha256\"", value.input)
        val base = value.base.toString(Charsets.US_ASCII)
        assertTrue(base.startsWith("\"@method\": POST\n\"@authority\": windows.tail123.ts.net:9444\n\"@path\": /v1/tasks\n\"@query\": ?\n"))
        assertFalse(base.endsWith("\n"))
        assertEquals("sha-256=:47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=:", MobileSigning.digest(ByteArray(0)))
    }
    @Test fun queryAndAuthorityAreSignedExactly() {
        fun canonical(authority: String, query: String) = MobileSigning.canonical("GET", authority, "/v1/chats/a/events", query,
            ByteArray(0), "installation", emptyMap(), "b".repeat(64), 1700000000, "B".repeat(32)).base
        assertFalse(canonical("first.tail123.ts.net", "?after=1").contentEquals(canonical("second.tail123.ts.net", "?after=1")))
        assertFalse(canonical("first.tail123.ts.net", "?after=1").contentEquals(canonical("first.tail123.ts.net", "?after=2")))
    }
    @Test fun realJcaSignaturesBecomeUnsignedFixedWidthIntegers() {
        val pair = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp256r1")) }.generateKeyPair()
        repeat(32) {
            val der = Signature.getInstance("SHA256withECDSA").run { initSign(pair.private); update("test-$it".toByteArray()); sign() }
            val raw = MobileSigning.derToRaw(der)
            assertEquals(64, raw.size)
            fun integer(bytes: ByteArray): ByteArray {
                val stripped = bytes.dropWhile { b -> b == 0.toByte() }.toByteArray()
                val unsigned = if (stripped.isEmpty()) byteArrayOf(0) else stripped
                val positive = if (unsigned[0].toInt() and 128 != 0) byteArrayOf(0) + unsigned else unsigned
                return byteArrayOf(2, positive.size.toByte()) + positive
            }
            val integers = integer(raw.copyOfRange(0, 32)) + integer(raw.copyOfRange(32, 64))
            val roundTrip = byteArrayOf(0x30, integers.size.toByte()) + integers
            assertTrue(Signature.getInstance("SHA256withECDSA").run { initVerify(pair.public); update("test-$it".toByteArray()); verify(roundTrip) })
        }
        assertEquals(64, MobileSigning.fingerprint(pair.public.encoded).length)
    }
    @Test fun malformedDerAndHeaderInjectionAreRejected() {
        listOf(byteArrayOf(), byteArrayOf(0x30, 2, 2, 0), byteArrayOf(0x30, 6, 2, 1, -1, 2, 1, 1)).forEach { bytes ->
            assertThrows(IllegalArgumentException::class.java) { MobileSigning.derToRaw(bytes) }
        }
        assertThrows(IllegalArgumentException::class.java) {
            MobileSigning.canonical("GET", "windows.tail123.ts.net", "/v1/health", "?", ByteArray(0),
                "host\nforged", emptyMap(), "a".repeat(64), 1, "A".repeat(32))
        }
    }
    @Test fun pythonPhoneSignerVectorVerifiesWithJca() {
        // Public test key from scalar 1. This is never used by the app or paired to a host.
        val spki = Base64.getDecoder().decode("MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEaxfR8uEsQkf4vOblY6RA8ncDfYEt6zOg9KE5RdiYwpZP40Li/hp/m47n60p8D54WK84zV2sxXs7LtkBoN79R9Q==")
        val keyId = "5cd252fb0ce8932436faf8ccd1040981b89ee4ad6b9fe9e2a2b7e71aacb27cd3"
        assertEquals(keyId, MobileSigning.fingerprint(spki))
        val canonical = MobileSigning.canonical("POST", "windows.tail123.ts.net:9444", "/v1/tasks", "?",
            "{}".toByteArray(), "cfc6921e-77d1-4447-8dcf-2af33eb694cd", mapOf("content-type" to "application/json"),
            keyId, 1700000000, "A".repeat(32))
        val raw = Base64.getDecoder().decode("O+8ze1GuAMgkNlEONKnHDUWSEkzfr2U1s6i89NFNT7j+bKn/8Z8j+23xjD2WlLTKewdRGQMc82a/Rl9aZNm6aQ==")
        fun integer(bytes: ByteArray): ByteArray {
            val stripped = bytes.dropWhile { it == 0.toByte() }.toByteArray()
            val value = if (stripped.isEmpty()) byteArrayOf(0) else stripped
            val positive = if (value[0].toInt() and 128 != 0) byteArrayOf(0) + value else value
            return byteArrayOf(2, positive.size.toByte()) + positive
        }
        val integers = integer(raw.copyOfRange(0, 32)) + integer(raw.copyOfRange(32, 64))
        val publicKey = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(spki))
        assertTrue(Signature.getInstance("SHA256withECDSA").run {
            initVerify(publicKey); update(canonical.base); verify(byteArrayOf(0x30, integers.size.toByte()) + integers)
        })
    }
}

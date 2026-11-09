package com.pshea.permissivecodex.network

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.core.app.ApplicationProvider
import android.content.Context
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.security.KeyFactory
import java.security.Signature
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class KeystoreSigningTest {
    @Test fun nonExportablePhoneKeyProducesVerifiableP256MessageSignatures() {
        val keys = DeviceKeys()
        val installation = UUID.randomUUID().toString()
        val alias = keys.ensure(installation)
        try {
            val spki = keys.publicKey(alias)
            assertArrayEquals(spki, keys.publicKey(keys.ensure(installation)))
            val headers = keys.headers(alias, installation, "GET", "windows.tail123.ts.net:9444", "/v1/workspaces", "?", ByteArray(0), emptyMap())
            assertTrue(headers.getValue("Signature-Input").startsWith("permissive-mobile=(\"@method\" \"@authority\""))
            val raw = Base64.getDecoder().decode(headers.getValue("Signature").removePrefix("permissive-mobile=:").removeSuffix(":"))
            assertEquals(64, raw.size)
            val params = headers.getValue("Signature-Input").removePrefix("permissive-mobile=")
            val canonical = "\"@method\": GET\n\"@authority\": windows.tail123.ts.net:9444\n\"@path\": /v1/workspaces\n\"@query\": ?\n" +
                "\"content-digest\": ${MobileSigning.digest(ByteArray(0))}\n\"permissive-instance-id\": $installation\n\"@signature-params\": $params"
            fun integer(bytes: ByteArray): ByteArray {
                val stripped = bytes.dropWhile { it == 0.toByte() }.toByteArray()
                val value = if (stripped.isEmpty()) byteArrayOf(0) else stripped
                val positive = if (value[0].toInt() and 128 != 0) byteArrayOf(0) + value else value
                return byteArrayOf(2, positive.size.toByte()) + positive
            }
            val integers = integer(raw.copyOfRange(0, 32)) + integer(raw.copyOfRange(32, 64))
            val der = byteArrayOf(0x30, integers.size.toByte()) + integers
            val publicKey = KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(spki))
            assertTrue(Signature.getInstance("SHA256withECDSA").run { initVerify(publicKey); update(canonical.toByteArray(Charsets.US_ASCII)); verify(der) })
            // Public, fixed-message fixture for reverse Python interoperability checks on the emulator.
            // It contains no private key, invitation, host enrollment or user content.
            java.io.File(ApplicationProvider.getApplicationContext<Context>().cacheDir, "test-keystore-vector.json").writeText(
                JSONObject().put("publicKey", Base64.getEncoder().encodeToString(spki))
                    .put("signature", Base64.getEncoder().encodeToString(raw))
                    .put("canonical", canonical).put("headers", JSONObject(headers)).toString())
            val privateKey = java.security.KeyStore.getInstance("AndroidKeyStore").apply { load(null) }.getKey(alias, null)
            assertNull(privateKey.encoded)
        } finally { keys.delete(alias) }
    }
}

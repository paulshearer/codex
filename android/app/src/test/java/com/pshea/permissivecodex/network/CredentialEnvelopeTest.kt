package com.pshea.permissivecodex.network

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.spec.MGF1ParameterSpec
import java.util.Base64
import java.util.UUID
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.OAEPParameterSpec
import javax.crypto.spec.PSource
import javax.crypto.spec.SecretKeySpec

class CredentialEnvelopeTest {
    @Test fun sealedCredentialsRequireTheUploadPrivateKeyAndMatchingScope() {
        val pair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
        val upload = UUID.randomUUID().toString()
        val workspace = UUID.randomUUID().toString()
        val prepared = JSONObject().put("algorithm", "RSA-OAEP-256/A256GCM").put("uploadId", upload)
            .put("publicKey", Base64.getEncoder().encodeToString(pair.public.encoded))
        val credential = JSONObject().put("label", "Work").put("origin", "https://example.com")
            .put("username", "user-用户").put("password", "password-秘密").put("lifetime", "session")
        val first = CredentialEnvelope.seal(prepared, workspace, credential)
        val second = CredentialEnvelope.seal(prepared, workspace, credential)
        assertFalse(first.toString().contains("password-秘密"))
        assertFalse(first.toString().contains("user-用户"))
        assertNotEquals(first.getString("wrappedKey"), second.getString("wrappedKey"))
        assertNotEquals(first.getString("iv"), second.getString("iv"))
        val rsa = Cipher.getInstance("RSA/ECB/OAEPPadding")
        rsa.init(Cipher.DECRYPT_MODE, pair.private, OAEPParameterSpec("SHA-256", "MGF1", MGF1ParameterSpec.SHA256, PSource.PSpecified.DEFAULT))
        val key = rsa.doFinal(Base64.getDecoder().decode(first.getString("wrappedKey")))
        assertEquals(32, key.size)
        fun decrypt(scope: String): ByteArray {
            val aes = Cipher.getInstance("AES/GCM/NoPadding")
            aes.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, Base64.getDecoder().decode(first.getString("iv"))))
            aes.updateAAD((upload + "\n" + scope).toByteArray(Charsets.UTF_8))
            return aes.doFinal(Base64.getDecoder().decode(first.getString("ciphertext")))
        }
        assertEquals("password-秘密", JSONObject(String(decrypt(workspace), Charsets.UTF_8)).getString("password"))
        try { decrypt(UUID.randomUUID().toString()); fail("Cross-workspace decryption must fail") }
        catch (_: javax.crypto.AEADBadTagException) { }
        key.fill(0)
    }
}

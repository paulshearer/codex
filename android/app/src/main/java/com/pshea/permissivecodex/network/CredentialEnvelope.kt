package com.pshea.permissivecodex.network

import org.json.JSONObject
import java.security.KeyFactory
import java.security.SecureRandom
import java.security.spec.MGF1ParameterSpec
import java.security.spec.X509EncodedKeySpec
import java.util.Base64
import javax.crypto.Cipher
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.OAEPParameterSpec
import javax.crypto.spec.PSource
import javax.crypto.spec.SecretKeySpec

/** Independent random keys per upload. Never uses the pairing/draft Keystore key. */
object CredentialEnvelope {
    fun seal(prepared: JSONObject, workspaceId: String, credential: JSONObject): JSONObject {
        require(prepared.getString("algorithm") == "RSA-OAEP-256/A256GCM") { "Unsupported secure handoff" }
        val publicKey = KeyFactory.getInstance("RSA").generatePublic(X509EncodedKeySpec(Base64.getDecoder().decode(prepared.getString("publicKey"))))
        val random = SecureRandom()
        val rawKey = ByteArray(32).also(random::nextBytes)
        val iv = ByteArray(12).also(random::nextBytes)
        val clear = credential.toString().toByteArray(Charsets.UTF_8)
        try {
            // Explicit MGF1 digest: Android providers otherwise differ from Web Crypto.
            val rsa = Cipher.getInstance("RSA/ECB/OAEPPadding")
            rsa.init(Cipher.ENCRYPT_MODE, publicKey, OAEPParameterSpec("SHA-256", "MGF1", MGF1ParameterSpec.SHA256, PSource.PSpecified.DEFAULT))
            val wrapped = rsa.doFinal(rawKey)
            val aes = Cipher.getInstance("AES/GCM/NoPadding")
            aes.init(Cipher.ENCRYPT_MODE, SecretKeySpec(rawKey, "AES"), GCMParameterSpec(128, iv))
            aes.updateAAD((prepared.getString("uploadId") + "\n" + workspaceId).toByteArray(Charsets.UTF_8))
            return JSONObject().put("workspaceId", workspaceId).put("uploadId", prepared.getString("uploadId"))
                .put("wrappedKey", Base64.getEncoder().encodeToString(wrapped))
                .put("iv", Base64.getEncoder().encodeToString(iv))
                .put("ciphertext", Base64.getEncoder().encodeToString(aes.doFinal(clear)))
        } finally { rawKey.fill(0); clear.fill(0) }
    }
}

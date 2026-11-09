package com.pshea.permissivecodex.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.text.method.PasswordTransformationMethod
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.EditText
import androidx.compose.foundation.layout.Column
import androidx.compose.ui.test.junit4.createComposeRule
import com.pshea.permissivecodex.network.CredentialEnvelope
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
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

class SecureCredentialInputTest {
    @get:Rule val compose = createComposeRule()

    @Test fun manualPasteFillsUnsavedFieldsAndKeepsPasswordMasked() {
        var userView: EditText? = null
        var passView: EditText? = null
        var username = ""
        var password = ""
        compose.setContent { PermissiveTheme { Column {
            SecureCredentialInput("Username", false, false, { userView = it }, { username = it })
            SecureCredentialInput("Password", true, false, { passView = it }, { password = it })
        } } }
        compose.runOnIdle {
            val user = checkNotNull(userView); val pass = checkNotNull(passView)
            assertFalse(user.isSaveEnabled); assertFalse(pass.isSaveEnabled)
            assertFalse(user.isSaveFromParentEnabled); assertFalse(pass.isSaveFromParentEnabled)
            assertTrue(pass.imeOptions and EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING != 0)
            assertEquals(View.IMPORTANT_FOR_AUTOFILL_NO, user.importantForAutofill)
            assertEquals(View.IMPORTANT_FOR_AUTOFILL_NO, pass.importantForAutofill)
            assertTrue(pass.transformationMethod is PasswordTransformationMethod)
            val clipboard = user.context.getSystemService(ClipboardManager::class.java)
            try {
                clipboard.setPrimaryClip(ClipData.newPlainText("test", "pasted-user"))
                user.requestFocus()
                assertTrue(user.onTextContextMenuItem(android.R.id.paste))
                clipboard.setPrimaryClip(ClipData.newPlainText("test", "pasted-password"))
                pass.requestFocus()
                assertTrue(pass.onTextContextMenuItem(android.R.id.paste))
                assertEquals("pasted-user", username)
                assertEquals("pasted-password", password)
            } finally {
                clipboard.setPrimaryClip(ClipData.newPlainText("test", ""))
            }
            pass.setText(""); user.setText("")
            assertEquals("", password); assertEquals("", username)
        }
    }

    @Test fun actualAndroidProviderUsesExplicitOaepMgf1Sha256AndGcmScopeBinding() {
        val pair = KeyPairGenerator.getInstance("RSA").apply { initialize(2048) }.generateKeyPair()
        val upload = UUID.randomUUID().toString(); val workspace = UUID.randomUUID().toString()
        val prepared = JSONObject().put("algorithm", "RSA-OAEP-256/A256GCM").put("uploadId", upload)
            .put("publicKey", Base64.getEncoder().encodeToString(pair.public.encoded))
        val sealed = CredentialEnvelope.seal(prepared, workspace, JSONObject().put("username", "fixture-user").put("password", "secret-秘密"))
        val rsa = Cipher.getInstance("RSA/ECB/OAEPPadding")
        rsa.init(Cipher.DECRYPT_MODE, pair.private, OAEPParameterSpec("SHA-256", "MGF1", MGF1ParameterSpec.SHA256, PSource.PSpecified.DEFAULT))
        val key = rsa.doFinal(Base64.getDecoder().decode(sealed.getString("wrappedKey")))
        val aes = Cipher.getInstance("AES/GCM/NoPadding")
        aes.init(Cipher.DECRYPT_MODE, SecretKeySpec(key, "AES"), GCMParameterSpec(128, Base64.getDecoder().decode(sealed.getString("iv"))))
        aes.updateAAD((upload + "\n" + workspace).toByteArray(Charsets.UTF_8))
        val clear = aes.doFinal(Base64.getDecoder().decode(sealed.getString("ciphertext")))
        assertEquals("secret-秘密", JSONObject(String(clear, Charsets.UTF_8)).getString("password"))
        clear.fill(0); key.fill(0)
    }
}

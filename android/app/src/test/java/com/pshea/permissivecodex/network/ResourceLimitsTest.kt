package com.pshea.permissivecodex.network

import com.pshea.permissivecodex.data.CompanionStore
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class ResourceLimitsTest {
    @Test fun downloadLimitMatchesTwoGiBHostArtifacts() {
        assertEquals(2147483648L, CompanionClient.MAX_DOWNLOAD_BYTES)
    }
    @Test fun maximumPromptRetryEnvelopeFitsEncryptedDraftCapacity() {
        // Control characters expand through the inner retry envelope and the outer store JSON.
        val prompt = "a" + "\u0001".repeat(1023)
        val scale = 4096L
        val envelope = JSONObject().put("text", prompt).put("pending", JSONObject().put("text", prompt)).toString()
        assertTrue(envelope.toByteArray().size * scale <= CompanionStore.MAX_DRAFT_BYTES)
        val outer = JSONObject().put("drafts", JSONObject().put("host:chat", envelope)).toString()
        assertTrue(outer.toByteArray().size * scale <= CompanionStore.MAX_STATE_BYTES)
    }
}

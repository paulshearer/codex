package com.pshea.permissivecodex.network

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.net.InetAddress

class TailnetPolicyTest {
    @Test fun originsRequireTailnetHttpsWithoutRedirectTargetsOrCredentials() {
        assertEquals("windows.tail123.ts.net", TailnetPolicy.origin("https://windows.tail123.ts.net:9444").host)
        listOf("http://windows.tail123.ts.net", "https://windows.tail123.ts.net", "https://windows.tail123.ts.net:9443", "https://example.com", "https://100.100.1.2:9444",
            "https://windows.tail123.ts.net/evil", "https://windows.tail123.ts.net?x=1",
            "https://user@windows.tail123.ts.net", "https://windows.tail123.ts.net#x", "https://evilts.net").forEach {
            assertThrows(IllegalArgumentException::class.java) { TailnetPolicy.origin(it) }
        }
    }
    @Test fun onlyTailscaleAddressRangesAreAccepted() {
        listOf("100.64.0.1", "100.127.255.254", "fd7a:115c:a1e0::1").forEach { assertTrue(TailnetPolicy.isTailscaleAddress(InetAddress.getByName(it))) }
        listOf("100.63.255.255", "100.128.0.1", "8.8.8.8", "127.0.0.1", "192.168.1.1", "fd7a:115c:a1e1::1", "::1").forEach {
            assertFalse(TailnetPolicy.isTailscaleAddress(InetAddress.getByName(it)))
        }
    }
    @Test fun pairingQrIsVersionedBoundedAndNeverStoresArbitraryFields() {
        val qr = JSONObject().put("version", 1).put("origin", "https://windows.tail123.ts.net:9444")
            .put("installationId", "cfc6921e-77d1-4447-8dcf-2af33eb694cd").put("invitationId", "3e3d3d88-d6d5-472e-b7bf-d534b7fb7870")
            .put("secret", "A".repeat(43)).put("expiresAt", System.currentTimeMillis() / 1000 + 60)
        assertEquals(1, PairingInvitation.parse(qr.toString()).version)
        qr.put("email", "untrusted@example.com")
        assertThrows(IllegalArgumentException::class.java) { PairingInvitation.parse(qr.toString()) }
        qr.remove("email"); qr.put("expiresAt", 1)
        assertThrows(IllegalArgumentException::class.java) { PairingInvitation.parse(qr.toString()) }
    }
}

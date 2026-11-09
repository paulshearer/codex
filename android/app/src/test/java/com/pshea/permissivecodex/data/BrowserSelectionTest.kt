package com.pshea.permissivecodex.data

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class BrowserSelectionTest {
    @Test fun defaultsAndRoundTripMatchHostRequest() {
        assertEquals(BrowserSelection(), BrowserSelection.fromJson(JSONObject()))
        val selection = BrowserSelection("attached", "chrome", "local-123")
        assertEquals(selection, BrowserSelection.fromJson(selection.toJson()))
        assertEquals("attached", selection.toJson().getString("browserMode"))
    }

    @Test fun latestRequestOverridesWorkspaceAndSavedDraftOverridesRequest() {
        val workspace = JSONObject("""{"browserMode":"attached","browser":"chrome","browserProfile":"workspace"}""")
        val request = JSONObject("""{"browser":"msedge","browserProfile":"latest"}""")
        val saved = JSONObject("""{"browserProfile":""}""")
        assertEquals(BrowserSelection("attached", "msedge", ""), BrowserSelection.resolve(workspace, request, saved))
        assertEquals(BrowserSelection("attached", "msedge", "latest"), BrowserSelection.resolve(workspace, request, null))
    }

    @Test fun corruptPersistedSettingsFallBackWithoutCoercingJsonTypes() {
        assertEquals(BrowserSelection(), BrowserSelection.fromJson(JSONObject("""{"browserMode":true,"browser":"firefox","browserProfile":123}""")))
        assertEquals(BrowserSelection("attached", "chrome"), BrowserSelection.fromJson(JSONObject("""{"browserMode":"attached","browser":"chrome","browserProfile":"../escape"}""")))
        assertEquals(BrowserSelection("dedicated", "msedge"), BrowserSelection.fromJson(JSONObject("""{"browser":"msedge","browserProfile":"local-123"}""")))
    }

    @Test fun profileValidationMatchesWindowsServerRules() {
        listOf("CON", "nul", "COM1", "lpt9", "name with spaces", "../profile", "_first", "x".repeat(65)).forEach {
            assertNotNull(it, BrowserSelection(browserProfile = it).validationError())
        }
        listOf("", "work", "a_B-2", "x".repeat(64)).forEach { assertNull(it, BrowserSelection(browserProfile = it).validationError()) }
        assertNotNull(BrowserSelection("attached", "chromium", "local-123").validationError())
        assertNull(BrowserSelection("attached", "msedge", "local-123").validationError())
    }

    @Test fun attachedChoicesUseInstalledProfilesAndDoNotDuplicateTheirLiveConnections() {
        val catalog = BrowserCatalog.fromJson(JSONObject("""{
          "data":[{"id":"chrome","installed":true,"profiles":["work"],"installedProfiles":[
            {"id":"local-one","name":"Work","account":"work@example.com","connected":false},
            {"id":"local-two","name":"Personal","connected":false}]}],
          "connections":[
            {"browser":"chrome","localProfileId":"local-one","profileId":"extension-one","profileName":"Work live"},
            {"browser":"chrome","profileId":"other","profileName":"Other"},
            {"browser":"chrome","profileId":"other","profileName":"Other duplicate"},
            {"browser":"msedge","profileId":"edge","profileName":"Edge profile"}]
        }"""))
        val profiles = catalog.profileChoices(BrowserSelection("attached", "chrome"))
        assertEquals(listOf("local-one", "local-two", "other"), profiles.map { it.id })
        assertEquals(true, profiles[0].connected)
        assertTrue(profiles[0].label.contains("work@example.com"))
        assertEquals(false, profiles[1].connected)
        assertEquals(listOf("work"), catalog.profileChoices(BrowserSelection(browser = "chrome")).map { it.id })
    }

    @Test fun selectedDisconnectedProfileAndLegacyConnectionRemainSelectable() {
        val catalog = BrowserCatalog.fromJson(JSONObject("""{"connections":[{"browser":"msedge","profileId":"edge","profileName":"Edge"}]}"""))
        val missing = catalog.profileChoices(BrowserSelection("attached", "chrome", "local-missing")).single()
        assertEquals("local-missing", missing.id)
        assertEquals(false, missing.connected)
        assertTrue(missing.label.contains("disconnected"))
        assertEquals("edge", catalog.profileChoices(BrowserSelection("attached", "chromium")).single().id)
        assertEquals(emptyList<BrowserProfileChoice>(), BrowserCatalog.fromJson(null).profileChoices(BrowserSelection()))
    }
}

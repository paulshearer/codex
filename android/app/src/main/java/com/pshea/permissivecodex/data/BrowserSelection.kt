package com.pshea.permissivecodex.data

import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale

/** Browser preferences for the paired Windows host, applied to the next submitted message. */
data class BrowserSelection(
    val browserMode: String = "dedicated",
    val browser: String = "chromium",
    val browserProfile: String = ""
) {
    fun validationError(): String? = when {
        browserMode !in setOf("dedicated", "attached") -> "Choose a browser mode."
        browser !in browserNames -> "Choose Chromium, Chrome or Edge."
        browserProfile.isNotEmpty() && (!profilePattern.matches(browserProfile) || browserProfile.uppercase(Locale.ROOT) in reservedProfiles) ->
            "Use up to 64 letters, numbers, underscores or hyphens, starting with a letter or number. Windows device names are unavailable."
        browserProfile.startsWith("local-") && (browserMode != "attached" || browser == "chromium") ->
            "An installed profile requires an existing signed-in Chrome or Edge browser."
        else -> null
    }

    fun toJson(): JSONObject {
        require(validationError() == null) { validationError().orEmpty() }
        return JSONObject().put("browserMode", browserMode).put("browser", browser).put("browserProfile", browserProfile)
    }

    fun summary(catalog: JSONObject? = null): String {
        val profiles = BrowserCatalog.fromJson(catalog).profileChoices(this)
        val profile = profiles.firstOrNull { it.id == browserProfile }?.label
            ?: if (browserMode == "attached") "Any connected profile" else "Conversation profile"
        return "${browserNames[browser] ?: browser} · $profile"
    }

    companion object {
        val browserNames = linkedMapOf("chromium" to "Bundled Chromium", "chrome" to "Google Chrome", "msedge" to "Microsoft Edge")
        private val profilePattern = Regex("[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
        private val reservedProfiles = setOf("CON", "PRN", "AUX", "NUL") + (1..9).flatMap { listOf("COM$it", "LPT$it") }
        private val fields = listOf("browserMode", "browser", "browserProfile")

        /** Ignore corrupt persisted values while retaining valid settings from older app versions. */
        fun fromJson(value: JSONObject): BrowserSelection {
            val mode = value.stringValue("browserMode").takeIf { it in setOf("dedicated", "attached") } ?: "dedicated"
            val browser = value.stringValue("browser").takeIf { it in browserNames } ?: "chromium"
            val candidate = BrowserSelection(mode, browser, value.stringValue("browserProfile"))
            return if (candidate.validationError() == null) candidate else candidate.copy(browserProfile = "")
        }

        /** Match desktop precedence: workspace defaults, latest task request, then the local draft. */
        fun resolve(workspace: JSONObject?, latestRequest: JSONObject?, savedOverride: JSONObject?): BrowserSelection {
            val merged = JSONObject()
            listOfNotNull(workspace, latestRequest, savedOverride).forEach { source ->
                fields.forEach { field -> if (source.opt(field) is String) merged.put(field, source.getString(field)) }
            }
            return fromJson(merged)
        }
    }
}

data class BrowserProfileChoice(val id: String, val label: String, val connected: Boolean? = null)

data class BrowserCatalogEntry(
    val id: String,
    val installed: Boolean,
    val profiles: List<String>,
    val installedProfiles: List<InstalledBrowserProfile>
)

data class InstalledBrowserProfile(val id: String, val name: String, val account: String, val connected: Boolean)
data class BrowserConnection(val browser: String, val profileId: String, val localProfileId: String, val profileName: String)

data class BrowserCatalog(val entries: List<BrowserCatalogEntry>, val connections: List<BrowserConnection>) {
    fun profileChoices(selection: BrowserSelection): List<BrowserProfileChoice> {
        val entry = entries.firstOrNull { it.id == selection.browser }
        val choices = if (selection.browserMode == "attached") {
            val matching = connections.filter { selection.browser == "chromium" || it.browser == selection.browser }
            val installed = entry?.installedProfiles.orEmpty()
            installed.map { profile ->
                val connected = profile.connected || matching.any { it.localProfileId == profile.id }
                val label = listOf(profile.name, profile.account).filter { it.isNotBlank() }.joinToString(" · ")
                BrowserProfileChoice(profile.id, "$label (${if (connected) "connected" else "not connected"})", connected)
            } + matching.filter { connection ->
                connection.profileId.isNotBlank() && (connection.profileId == selection.browserProfile || installed.none { it.id == connection.localProfileId })
            }.map { BrowserProfileChoice(it.profileId, "${it.profileName.ifBlank { it.profileId }} (connected)", true) }
        } else entry?.profiles.orEmpty().map { BrowserProfileChoice(it, it) }
        val distinct = choices.distinctBy { it.id }
        return if (selection.browserProfile.isBlank() || distinct.any { it.id == selection.browserProfile }) distinct else
            distinct + BrowserProfileChoice(selection.browserProfile,
                selection.browserProfile + if (selection.browserMode == "attached") " (disconnected)" else "",
                if (selection.browserMode == "attached") false else null)
    }

    companion object {
        fun fromJson(value: JSONObject?): BrowserCatalog = BrowserCatalog(
            entries = value?.optJSONArray("data").objects().mapNotNull { item ->
                val id = item.stringValue("id").takeIf { it in BrowserSelection.browserNames } ?: return@mapNotNull null
                BrowserCatalogEntry(id, item.optBoolean("installed", true),
                    item.optJSONArray("profiles").strings(),
                    item.optJSONArray("installedProfiles").objects().mapNotNull { profile ->
                        val profileId = profile.stringValue("id").takeIf(String::isNotBlank) ?: return@mapNotNull null
                        InstalledBrowserProfile(profileId, profile.stringValue("name").ifBlank { profileId },
                            profile.stringValue("account"), profile.optBoolean("connected", false))
                    })
            },
            connections = value?.optJSONArray("connections").objects().map { item ->
                BrowserConnection(item.stringValue("browser"), item.stringValue("profileId"),
                    item.stringValue("localProfileId"), item.stringValue("profileName"))
            }
        )
    }
}

private fun JSONObject.stringValue(key: String): String = opt(key) as? String ?: ""
private fun JSONArray?.objects(): List<JSONObject> = if (this == null) emptyList() else (0 until length()).mapNotNull(::optJSONObject)
private fun JSONArray?.strings(): List<String> = if (this == null) emptyList() else (0 until length()).mapNotNull { opt(it) as? String }.filter(String::isNotBlank).distinct()

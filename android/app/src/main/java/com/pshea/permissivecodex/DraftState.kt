package com.pshea.permissivecodex

import com.pshea.permissivecodex.attachments.DraftAttachment
import org.json.JSONArray
import org.json.JSONObject

/** Saved only inside the Keystore-encrypted draft store; never retried without a user action. */
data class PendingSubmission(val action: SendAction, val text: String, val attachmentIds: List<String>,
    val submissionId: String, val draftAttachmentIds: List<String>, val workspaceId: String,
    val conversationId: String?, val taskId: String?, val expectedTurnId: String?, val options: String = "{}") {
    fun matches(action: SendAction, text: String, attachments: List<DraftAttachment>): Boolean =
        this.action == action && this.text == text && draftAttachmentIds == attachments.map { it.id } && attachmentIds == attachments.mapNotNull { it.uploadedId }
    fun toJson() = JSONObject().put("action", action.name).put("text", text).put("attachmentIds", JSONArray(attachmentIds))
        .put("submissionId", submissionId).put("draftAttachmentIds", JSONArray(draftAttachmentIds)).put("workspaceId", workspaceId)
        .put("conversationId", conversationId ?: JSONObject.NULL).put("taskId", taskId ?: JSONObject.NULL)
        .put("expectedTurnId", expectedTurnId ?: JSONObject.NULL).put("options", options)
    companion object {
        fun fromJson(value: JSONObject) = PendingSubmission(SendAction.valueOf(value.getString("action")), value.getString("text"), value.strings("attachmentIds"),
            value.getString("submissionId"), value.strings("draftAttachmentIds"), value.getString("workspaceId"), value.optional("conversationId"), value.optional("taskId"), value.optional("expectedTurnId"), value.optString("options", "{}"))
    }
}

data class SavedDraft(val text: String = "", val attachments: List<DraftAttachment> = emptyList(), val pending: PendingSubmission? = null) {
    fun encode(): String = JSONObject().put("schema", "permissive-draft/v1").put("text", text)
        .put("attachments", JSONArray(attachments.map { JSONObject().put("id", it.id).put("name", it.name).put("size", it.size).put("uploadedId", it.uploadedId ?: JSONObject.NULL) }))
        .put("pending", pending?.toJson() ?: JSONObject.NULL).toString()
    companion object {
        fun decode(raw: String): SavedDraft {
            val value = runCatching { JSONObject(raw) }.getOrNull()
            if (value?.optString("schema") != "permissive-draft/v1") return SavedDraft(raw)
            val array = value.optJSONArray("attachments") ?: JSONArray()
            val attachments = (0 until array.length()).map { index -> val a = array.getJSONObject(index); DraftAttachment(a.getString("id"), a.getString("name"), ByteArray(0), a.optional("uploadedId"), a.getInt("size")) }
            return SavedDraft(value.getString("text"), attachments, value.optJSONObject("pending")?.let(PendingSubmission::fromJson))
        }
    }
}

private fun JSONObject.optional(key: String): String? = if (isNull(key)) null else optString(key).takeIf(String::isNotEmpty)
private fun JSONObject.strings(key: String): List<String> = getJSONArray(key).let { a -> (0 until a.length()).map(a::getString) }

internal fun effectivePrompt(text: String, hasAttachments: Boolean): String = text.takeIf(String::isNotBlank)
    ?: if (hasAttachments) "Please examine the attached files." else ""

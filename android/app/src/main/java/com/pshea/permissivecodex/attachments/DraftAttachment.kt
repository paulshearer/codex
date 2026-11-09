package com.pshea.permissivecodex.attachments

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.ByteArrayOutputStream
import java.util.UUID

data class DraftAttachment(val id: String = UUID.randomUUID().toString(), val name: String, val bytes: ByteArray,
                           val uploadedId: String? = null, val originalSize: Int = bytes.size) {
    val size: Int get() = originalSize
    val needsReselect: Boolean get() = uploadedId == null && bytes.isEmpty()
}

object AttachmentLimits {
    const val MAX_COUNT = 10
    const val MAX_FILE_BYTES = 16 * 1024 * 1024
    const val MAX_TOTAL_BYTES = 32 * 1024 * 1024
    fun validate(sizes: List<Int>) {
        require(sizes.size <= MAX_COUNT) { "Choose at most 10 attachments." }
        require(sizes.all { it in 1..MAX_FILE_BYTES }) { "Each attachment must contain 1 byte to 16 MiB." }
        require(sizes.sumOf(Int::toLong) <= MAX_TOTAL_BYTES) { "Combined attachments exceed 32 MiB." }
    }
}

class AttachmentReader(private val context: Context) {
    suspend fun read(uris: List<Uri>, existing: List<DraftAttachment>): List<DraftAttachment> = withContext(Dispatchers.IO) {
        require(existing.size + uris.size <= AttachmentLimits.MAX_COUNT) { "Choose at most 10 attachments." }
        val added = uris.map { uri ->
            var name = uri.lastPathSegment?.substringAfterLast('/') ?: "attachment"
            context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val nameColumn = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (nameColumn >= 0 && !cursor.isNull(nameColumn)) name = cursor.getString(nameColumn)
                    val sizeColumn = cursor.getColumnIndex(OpenableColumns.SIZE)
                    if (sizeColumn >= 0 && !cursor.isNull(sizeColumn)) require(cursor.getLong(sizeColumn) <= AttachmentLimits.MAX_FILE_BYTES) { "$name exceeds 16 MiB." }
                }
            }
            val bytes = context.contentResolver.openInputStream(uri)?.use { input ->
                val output = ByteArrayOutputStream()
                val block = ByteArray(64 * 1024)
                while (true) {
                    val count = input.read(block)
                    if (count < 0) break
                    require(output.size().toLong() + count <= AttachmentLimits.MAX_FILE_BYTES) { "$name exceeds 16 MiB." }
                    output.write(block, 0, count)
                }
                output.toByteArray()
            } ?: error("Could not open $name.")
            DraftAttachment(name = name.substringAfterLast('/').substringAfterLast('\\').take(240), bytes = bytes)
        }
        (existing + added).also { AttachmentLimits.validate(it.map(DraftAttachment::size)) }
    }
}

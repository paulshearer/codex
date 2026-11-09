package com.pshea.permissivecodex.ui

import com.pshea.permissivecodex.attachments.AttachmentLimits
import org.junit.Assert.*
import org.junit.Test

class MarkdownTest {
    @Test fun tableAndCodeRemainDistinct() {
        val blocks = parseMarkdown("Before\n\n| A | B |\n| --- | :---: |\n| one | two |\n\n```kotlin\nval a = 1\n```\nAfter")
        assertEquals(listOf(listOf("A", "B"), listOf("one", "two")), (blocks[1] as MarkdownBlock.Table).rows)
        assertEquals("val a = 1", (blocks[3] as MarkdownBlock.Code).text)
        assertTrue(blocks.last() is MarkdownBlock.Text)
    }
    @Test fun incompleteStreamingTableStaysReadable() { assertTrue(parseMarkdown("| A |\n| --").single() is MarkdownBlock.Text) }
    @Test fun exactAttachmentBoundsAccepted() { AttachmentLimits.validate(listOf(16 * 1024 * 1024, 16 * 1024 * 1024)) }
    @Test fun oversizeAndTooManyRejected() {
        assertThrows(IllegalArgumentException::class.java) { AttachmentLimits.validate(List(11) { 1 }) }
        assertThrows(IllegalArgumentException::class.java) { AttachmentLimits.validate(listOf(16 * 1024 * 1024 + 1)) }
        assertThrows(IllegalArgumentException::class.java) { AttachmentLimits.validate(listOf(16 * 1024 * 1024, 16 * 1024 * 1024, 1)) }
    }
}

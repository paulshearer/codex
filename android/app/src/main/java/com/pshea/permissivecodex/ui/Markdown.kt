package com.pshea.permissivecodex.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp

sealed interface MarkdownBlock {
    data class Text(val text: String) : MarkdownBlock
    data class Code(val text: String) : MarkdownBlock
    data class Table(val rows: List<List<String>>) : MarkdownBlock
}

internal fun parseMarkdown(text: String): List<MarkdownBlock> {
    val lines = text.lines()
    val blocks = mutableListOf<MarkdownBlock>()
    val paragraph = mutableListOf<String>()
    fun flush() { if (paragraph.isNotEmpty()) { blocks += MarkdownBlock.Text(paragraph.joinToString("\n")); paragraph.clear() } }
    fun cells(line: String) = line.trim().trim('|').split('|').map(String::trim)
    var index = 0
    while (index < lines.size) {
        val line = lines[index]
        when {
            line.trimStart().startsWith("```") -> {
                flush(); index++
                val code = mutableListOf<String>()
                while (index < lines.size && !lines[index].trimStart().startsWith("```")) code += lines[index++]
                blocks += MarkdownBlock.Code(code.joinToString("\n"))
            }
            index + 1 < lines.size && line.contains('|') && cells(lines[index + 1]).all { Regex(":?-{3,}:?").matches(it) } -> {
                flush()
                val rows = mutableListOf(cells(line)); index += 2
                while (index < lines.size && lines[index].contains('|') && lines[index].isNotBlank()) rows += cells(lines[index++])
                blocks += MarkdownBlock.Table(rows)
                continue
            }
            else -> paragraph += line
        }
        index++
    }
    flush()
    return blocks
}

private fun inlineMarkdown(text: String): AnnotatedString = buildAnnotatedString {
    val pattern = Regex("(\\*\\*(.+?)\\*\\*|`([^`]+)`)")
    var cursor = 0
    pattern.findAll(text).forEach { match ->
        append(text.substring(cursor, match.range.first))
        if (match.groupValues[2].isNotEmpty()) withStyle(SpanStyle(fontWeight = FontWeight.Bold)) { append(match.groupValues[2]) }
        else withStyle(SpanStyle(fontFamily = FontFamily.Monospace)) { append(match.groupValues[3]) }
        cursor = match.range.last + 1
    }
    append(text.substring(cursor))
}

@Composable
fun MarkdownContent(text: String, modifier: Modifier = Modifier) {
    val blocks = remember(text) { parseMarkdown(text) }
    SelectionContainer(modifier) {
        Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            blocks.forEach { block ->
                when (block) {
                    is MarkdownBlock.Text -> block.text.lines().forEach { line ->
                        val heading = line.takeWhile { it == '#' }.length.takeIf { it in 1..6 && line.getOrNull(it) == ' ' } ?: 0
                        Text(inlineMarkdown(if (heading > 0) line.drop(heading + 1) else line), style = if (heading > 0) MaterialTheme.typography.titleMedium else MaterialTheme.typography.bodyMedium)
                    }
                    is MarkdownBlock.Code -> Text(block.text, fontFamily = FontFamily.Monospace, modifier = Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface).horizontalScroll(rememberScrollState()).padding(10.dp))
                    is MarkdownBlock.Table -> Column(Modifier.horizontalScroll(rememberScrollState())) {
                        block.rows.forEachIndexed { rowIndex, row ->
                            Row(Modifier.background(if (rowIndex == 0) MaterialTheme.colorScheme.secondaryContainer else MaterialTheme.colorScheme.surface)) {
                                row.forEach { cell -> Text(inlineMarkdown(cell), fontWeight = if (rowIndex == 0) FontWeight.Bold else FontWeight.Normal, modifier = Modifier.width(160.dp).padding(8.dp)) }
                            }
                            HorizontalDivider()
                        }
                    }
                }
            }
        }
    }
}

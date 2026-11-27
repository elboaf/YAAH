package com.yaah.client.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowDropDown
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowRight
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.yaah.client.model.AvailableModels
import com.yaah.client.model.ModelOption
import com.yaah.client.model.ToolCallItem

/** A glyph for a tool name, so chips read at a glance without reading text. */
fun toolGlyph(name: String): String = when {
    name.contains("read", true) || name.contains("file", true) || name.contains("cat", true) -> "📄"
    name.contains("write", true) || name.contains("edit", true) || name.contains("create", true) || name.contains("save", true) -> "✏️"
    name.contains("bash", true) || name.contains("shell", true) || name.contains("exec", true) || name.contains("run", true) -> "🖥️"
    name.contains("search", true) || name.contains("grep", true) || name.contains("find", true) -> "🔍"
    name.contains("fetch", true) || name.contains("web", true) || name.contains("http", true) || name.contains("curl", true) -> "🌐"
    name.contains("agent", true) || name.contains("spawn", true) || name.contains("subagent", true) -> "🤖"
    name.contains("image", true) || name.contains("view_image", true) || name.contains("draw", true) -> "🖼️"
    name.contains("process", true) || name.contains("kill", true) || name.contains("ps", true) -> "⚙️"
    name.contains("lease", true) || name.contains("commit", true) || name.contains("turn", true) -> "♟️"
    else -> "🔧"
}

/** Pretty-print tool-call arguments for display; fall back to the raw text. */
fun formatToolArguments(arguments: String?): String {
    val raw = arguments?.trim().orEmpty()
    if (raw.isEmpty()) return ""
    return try {
        val pretty = org.json.JSONObject(raw).toString(2)
        truncate(pretty)
    } catch (e: Exception) {
        truncate(raw)
    }
}

private fun truncate(s: String): String =
    if (s.length > 4000) s.take(4000) + "…(truncated)" else s

/** A row of compact tool-call chips. Tapping one invokes onClick so the caller
 * can inspect that call's arguments. */
@Composable
fun ToolCallChips(toolCalls: List<ToolCallItem>, onClick: (ToolCallItem) -> Unit) {
    if (toolCalls.isEmpty()) return
    LazyRow(
        modifier = Modifier.fillMaxWidth(),
        contentPadding = PaddingValues(horizontal = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(4.dp)
    ) {
        itemsIndexed(toolCalls, key = { i, tc -> "${tc.id ?: i}_$i" }) { _, tc ->
            ToolCallChip(name = tc.toolName, onClick = { onClick(tc) })
        }
    }
}

@Composable
private fun ToolCallChip(name: String, onClick: () -> Unit) {
    Surface(
        color = MaterialTheme.colorScheme.surfaceVariant,
        shape = MaterialTheme.shapes.small,
        modifier = Modifier.clickable(onClick = onClick)
    ) {
        Row(modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)) {
            Text(text = toolGlyph(name), modifier = Modifier.padding(end = 4.dp))
            Text(
                text = shortToolName(name),
                style = MaterialTheme.typography.labelSmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                fontFamily = FontFamily.Monospace
            )
        }
    }
}

/** Trim a long tool name to what fits a chip. */
private fun shortToolName(name: String): String =
    if (name.length <= 22) name else name.take(21) + "…"

/** Human label for a model id: "<model> · <provider>". */
fun modelLabel(model: String?): String {
    if (model.isNullOrBlank()) return "Model"
    val sep = model.indexOf("::")
    val id = if (sep >= 0) model.substring(sep + 2) else model
    val provider = if (sep >= 0) model.substring(0, sep) else ""
    val label = if (provider.isBlank()) id else "$id · $provider"
    return if (label.length <= 32) label else label.take(31) + "…"
}

/** Build the flat list of selectable "provider::model" options. */
fun AvailableModels.toOptions(): List<ModelOption> =
    providers.entries.flatMap { (provider, pm) -> pm.models.map { ModelOption(provider, it) } }

/** Selectable model chip in the conversation header. */
@Composable
fun ModelSelectorChip(
    model: String?,
    enabled: Boolean,
    onClick: () -> Unit
) {
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
        contentPadding = PaddingValues(horizontal = 8.dp, vertical = 3.dp)
    ) {
        Text(
            text = modelLabel(model),
            style = MaterialTheme.typography.labelMedium,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f)
        )
        Spacer(modifier = Modifier.width(4.dp))
        Icon(Icons.Default.ArrowDropDown, contentDescription = "Change model")
    }
}

/**
 * Dialog listing every available model, grouped under a collapsible header per
 * provider. The current model's provider stays open; every other provider
 * (e.g. openrouter's 50-60 models) starts collapsed so the list reads as a
 * short table of providers instead of one long scroll.
 */
@Composable
fun ModelSelectorDialog(
    models: List<ModelOption>,
    selected: String?,
    onSelected: (String) -> Unit,
    onDismiss: () -> Unit
) {
    // Open only the current model's provider by default.
    val currentProvider = selected?.substringBefore("::")
    var expandedProviders by remember {
        mutableStateOf(if (currentProvider != null) setOf(currentProvider) else emptySet())
    }
    // Group by provider, sorted A-Z, as (provider, models) pairs.
    val grouped = models.groupBy { it.provider }.toList().sortedBy { it.first }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Model") },
        text = {
            if (models.isEmpty()) {
                Text(
                    "Could not load the model list. The current model is kept.",
                    modifier = Modifier.fillMaxWidth()
                )
            } else {
                LazyColumn {
                    itemsIndexed(grouped, key = { i, (provider, _) -> "prov_${i}_${provider}" }) { _, (provider, opts) ->
                        CollapsibleProviderSection(
                            provider = provider,
                            count = opts.size,
                            expanded = provider in expandedProviders,
                            onToggle = {
                                expandedProviders = if (provider in expandedProviders) {
                                    expandedProviders - provider
                                } else {
                                    expandedProviders + provider
                                }
                            }
                        ) {
                            for (opt in opts) {
                                Row(
                                    modifier = Modifier
                                        .fillMaxWidth()
                                        .clickable { onSelected(opt.key) }
                                        .padding(vertical = 8.dp, horizontal = 16.dp),
                                    verticalAlignment = Alignment.CenterVertically
                                ) {
                                    RadioButton(
                                        selected = selected == opt.key,
                                        onClick = { onSelected(opt.key) }
                                    )
                                    Spacer(modifier = Modifier.width(8.dp))
                                    Text(
                                        opt.id,
                                        style = MaterialTheme.typography.bodyMedium,
                                        maxLines = 2,
                                        overflow = TextOverflow.Ellipsis
                                    )
                                }
                            }
                        }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) { Text("Cancel") }
        }
    )
}

/**
 * A provider header (name + count + chevron) that discloses its model list when
 * expanded. Toggling is driven by the caller's expanded-set state.
 */
@Composable
private fun CollapsibleProviderSection(
    provider: String,
    count: Int,
    expanded: Boolean,
    onToggle: () -> Unit,
    content: @Composable () -> Unit
) {
    Column(modifier = Modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .clickable(onClick = onToggle)
                .padding(horizontal = 8.dp, vertical = 6.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Icon(
                imageVector = if (expanded) Icons.Default.KeyboardArrowDown
                else Icons.Default.KeyboardArrowRight,
                contentDescription = if (expanded) "Collapse $provider" else "Expand $provider"
            )
            Text(
                text = "$provider ($count)",
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Bold,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f)
            )
        }
        if (expanded) {
            content()
        }
    }
}

package com.yaah.client.ui.screens

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.material.icons.filled.KeyboardArrowRight
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.yaah.client.api.YaahClient
import com.yaah.client.model.ConversationItem
import kotlinx.coroutines.*

/** Sentinel key for the "Default" (no workspace) group. */
private const val DEFAULT_KEY = "\u0000"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConversationListScreen(
    scope: CoroutineScope,
    host: HostEntry,
    onBack: () -> Unit,
    onConversationSelected: (ConversationItem) -> Unit
) {
    var conversations by remember { mutableStateOf<List<ConversationItem>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    // Collapsed workspace groups, keyed by workspace path (DEFAULT_KEY for the
    // Default group). Survives recomposition; resets if the host changes.
    var collapsed by remember(host) { mutableStateOf<Set<String>>(emptySet()) }

    // Auto-refresh the list so new conversations appear without a manual pull.
    // Runs while this screen is in the composition; stops on error for Retry.
    LaunchedEffect(host) {
        error = null
        while (true) {
            val convs = fetchConversations(host)
            if (convs == null) {
                error = "Failed to load conversations"
                break
            }
            conversations = convs
            loading = false
            error = null
            delay(15_000)
        }
    }

    val groups = remember(conversations) { buildGroups(conversations) }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(host.name) },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.Default.ArrowBack, "Back")
                    }
                }
            )
        }
    ) { padding ->
        if (loading) {
            Box(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentAlignment = androidx.compose.ui.Alignment.Center
            ) {
                CircularProgressIndicator()
            }
        } else if (error != null) {
            Column(
                modifier = Modifier.fillMaxSize().padding(padding).padding(16.dp),
                horizontalAlignment = androidx.compose.ui.Alignment.CenterHorizontally
            ) {
                Text("Error: $error")
                Spacer(modifier = Modifier.height(16.dp))
                Button(onClick = {
                    scope.launch {
                        val convs = fetchConversations(host)
                        if (convs != null) {
                            conversations = convs
                            loading = false
                            error = null
                        }
                    }
                }) {
                    Text("Retry")
                }
            }
        } else {
            // Grouped by workspace: a collapsible header per group, then its rows.
            // Flattened into one entry list so LazyColumn can key every item
            // (header vs row) without the nested-"items" receiver problem.
            val entries = remember(groups, collapsed) {
                val list = mutableListOf<ListEntry>()
                for (group in groups) {
                    val expanded = !collapsed.contains(group.key)
                    list.add(ListEntry.Header(group.key, group.label, group.items.size, expanded))
                    if (expanded) {
                        list.addAll(group.items.map { ListEntry.Row(it) })
                    }
                }
                list
            }
            LazyColumn(modifier = Modifier.fillMaxSize().padding(padding)) {
                items(entries, key = { it.key() }, contentType = { it }) { entry ->
                    when (entry) {
                        is ListEntry.Header -> {
                            WorkspaceGroupHeader(
                                label = entry.label,
                                count = entry.count,
                                expanded = entry.expanded,
                                onClick = {
                                    collapsed = if (entry.expanded) {
                                        collapsed + entry.key
                                    } else {
                                        collapsed - entry.key
                                    }
                                }
                            )
                        }
                        is ListEntry.Row ->
                            ConversationRow(entry.conv, onClick = { onConversationSelected(entry.conv) })
                    }
                }
            }
        }
    }
}

/** Fetch the conversation list, or null if the request failed. */
suspend fun fetchConversations(host: HostEntry): List<ConversationItem>? {
    try {
        val client = YaahClient(host.baseUrl, host.passphrase)
        val response = withContext(Dispatchers.IO) {
            client.getService().listConversations()
        }
        return if (response.isSuccessful && response.body() != null) response.body() else null
    } catch (e: Exception) {
        return null
    }
}

/** A workspace group: its display label plus the conversations filed under it. */
private data class WsGroup(
    val key: String,
    val label: String,
    val isDefault: Boolean,
    val items: List<ConversationItem>
)

/** One row of the grouped list — either a workspace header or a conversation.
 *  Kept flat so LazyColumn can key each item independently. */
private sealed class ListEntry {
    abstract fun key(): String

    data class Header(
        val key: String,
        val label: String,
        val count: Int,
        val expanded: Boolean
    ) : ListEntry() {
        override fun key(): String = "h_$key"
    }

    data class Row(val conv: ConversationItem) : ListEntry() {
        override fun key(): String = "r_${conv.id}"
    }
}

/** Group conversations by workspace. Default (null/blank path) first, then the
 *  rest ordered by most-recent activity. Conversation timestamps move on every
 *  streamed message, so updated_at is the right recency signal here. */
private fun buildGroups(convs: List<ConversationItem>): List<WsGroup> {
    val byKey = LinkedHashMap<String, MutableList<ConversationItem>>()
    for (c in convs) {
        val key = if (c.workspace.isNullOrBlank()) DEFAULT_KEY else c.workspace
        byKey.getOrPut(key) { mutableListOf() }.add(c)
    }
    return byKey.entries.map { (key, items) ->
        val isDefault = key == DEFAULT_KEY
        WsGroup(
            key = key,
            label = if (isDefault) "Default" else basename(key),
            isDefault = isDefault,
            items = items
        )
    }.sortedWith(
        compareBy<WsGroup>({ if (it.isDefault) 0 else 1 })
            .thenByDescending { newestUpdated(it.items) }
    )
}

private fun newestUpdated(items: List<ConversationItem>): String =
    items.maxOfOrNull { it.updated_at ?: "" } ?: ""

/** Last path segment, for both POSIX and Windows paths. */
private fun basename(path: String): String {
    val idx = path.lastIndexOf('/') coalesceLast path.lastIndexOf('\\')
    return if (idx >= 0 && idx < path.length - 1) path.substring(idx + 1) else path
}

private infix fun Int.coalesceLast(other: Int): Int = if (this >= 0) this else other

@Composable
private fun WorkspaceGroupHeader(
    label: String,
    count: Int,
    expanded: Boolean,
    onClick: () -> Unit
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically
    ) {
        Icon(
            imageVector = if (expanded) Icons.Default.KeyboardArrowDown else Icons.Default.KeyboardArrowRight,
            contentDescription = if (expanded) "Collapse group" else "Expand group"
        )
        Spacer(modifier = Modifier.width(8.dp))
        Text(
            text = label,
            style = MaterialTheme.typography.titleSmall,
            modifier = Modifier.weight(1f)
        )
        Text(
            text = "$count",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant
        )
    }
}

package com.yaah.client.ui.screens

import android.util.Log
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.yaah.client.api.YaahClient
import com.yaah.client.model.ModelOption
import com.yaah.client.model.RemoteSnapshot
import com.yaah.client.model.TurnRequest
import com.yaah.client.util.Store
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private const val POLL_INTERVAL_MS = 5_000L

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConversationDetailScreen(
    scope: CoroutineScope,
    convId: String,
    workspaceId: String = "",
    host: HostEntry,
    onBack: () -> Unit
) {
    var workspace by remember { mutableStateOf(workspaceId) }
    var details by remember { mutableStateOf<RemoteSnapshot?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var sending by remember { mutableStateOf(false) }
    var running by remember { mutableStateOf(false) }
    var input by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    // The model chosen for this conversation. Start from what was persisted
    // last session; once a snapshot loads, fall back to the server's model so
    // the switcher reflects the chat's actual model when none was chosen.
    val context = LocalContext.current
    var model by remember {
        mutableStateOf(Store.loadModel(context, convId))
    }
    var availableModels by remember { mutableStateOf<List<ModelOption>>(emptyList()) }
    var modelsLoaded by remember { mutableStateOf(false) }
    var showModelDialog by remember { mutableStateOf(false) }

    // Load the selectable model list once (provider -> models).
    LaunchedEffect(Unit) {
        val resp = try {
            withContext(Dispatchers.IO) {
                YaahClient(host.baseUrl, host.passphrase).getService().getAvailableModels()
            }
        } catch (e: Exception) {
            Log.e("YaahClient", "Failed to load models: ${e.message}")
            null
        }
        val opts = resp?.body()?.toOptions()
        if (!opts.isNullOrEmpty()) {
            availableModels = opts
            modelsLoaded = true
        }
    }

    // Live refresh: poll the snapshot so new agent messages appear
    // automatically. Runs while this screen is in the composition; stops on
    // error so the user can Retry. Keyed on convId+host so reconnecting restarts it.
    LaunchedEffect(convId, host) {
        error = null
        while (true) {
            val snap = fetchSnapshot(host, convId)
            if (snap == null) {
                error = "Failed to load conversation"
                break
            }
            details = snap
            snap.conversation.workspace?.let { workspace = it }
            // Adopt the server model when the user hasn't chosen one yet.
            if (model == null) model = snap.conversation.model
            loading = false
            delay(POLL_INTERVAL_MS)
        }
    }

    // Poll the authoritative running-turn list so the send control can flip to
    // a stop button while a run is live for this conversation. Keyed on
    // convId+host so reconnecting restarts it. Independent of the snapshot
    // poller: a failed status fetch must not tear the screen down.
    LaunchedEffect(convId, host) {
        while (true) {
            try {
                val resp = withContext(Dispatchers.IO) {
                    YaahClient(host.baseUrl, host.passphrase)
                        .getService().runningAgents("local")
                }
                running = resp.body()?.running?.any { it.toString() == convId } ?: false
            } catch (e: Exception) {
                Log.e("YaahClient", "Failed to load running state: ${e.message}")
            }
            delay(4_000L)
        }
    }

    // Auto-scroll to the newest message whenever the list grows. Keyed on the
    // message count so reading older messages (same count) doesn't yank the
    // user back to the bottom. Poll until the newest item has laid out, then
    // jump to it — avoids an experimental-API dependency.
    LaunchedEffect(details?.messages?.size) {
        val count = details?.messages?.size ?: 0
        if (count == 0) return@LaunchedEffect
        var attempts = 0
        while (attempts++ < 100) {
            if (listState.layoutInfo?.visibleItemsInfo?.any { it.index == count - 1 } == true) {
                listState.animateScrollToItem(index = count - 1)
                break
            }
            delay(30)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(details?.conversation?.title ?: "Loading...") },
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
                contentAlignment = Alignment.Center
            ) {
                CircularProgressIndicator()
            }
        } else if (error != null) {
            Column(
                modifier = Modifier.fillMaxSize().padding(padding).padding(16.dp),
                horizontalAlignment = Alignment.CenterHorizontally
            ) {
                Text("Error: $error")
                Spacer(modifier = Modifier.height(16.dp))
                Button(onClick = {
                    scope.launch {
                        val snap = fetchSnapshot(host, convId)
                        if (snap != null) {
                            details = snap
                            snap.conversation.workspace?.let { workspace = it }
                            if (model == null) model = snap.conversation.model
                            loading = false
                            error = null
                        }
                    }
                }) {
                    Text("Retry")
                }
            }
        } else {
            Column(modifier = Modifier.fillMaxSize().padding(padding)) {
                // Model banner: shows the chat's model and opens the picker.
                if (details != null) {
                    Row(
                        modifier = Modifier
                            .fillMaxWidth()
                            .background(MaterialTheme.colorScheme.surfaceVariant)
                            .padding(horizontal = 8.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        ModelSelectorChip(
                            model = model,
                            enabled = modelsLoaded,
                            onClick = { showModelDialog = true }
                        )
                        Spacer(modifier = Modifier.width(8.dp))
                        Text(
                            "Switching applies to your next message.",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.weight(1f),
                            maxLines = 2,
                            overflow = TextOverflow.Ellipsis
                        )
                    }
                }

                // Messages area
                LazyColumn(
                    modifier = Modifier.weight(1f),
                    contentPadding = PaddingValues(8.dp)
                ) {
                    itemsIndexed(details?.messages ?: emptyList(), key = { i, msg -> "${i}_${msg.message_id}" }) { _, msg ->
                        FormattedMessageBubble(msg, isUser = msg.role == "user")
                        Spacer(modifier = Modifier.height(4.dp))
                    }
                }

                // Composer bar
                Surface(
                    modifier = Modifier.fillMaxWidth(),
                    color = MaterialTheme.colorScheme.surfaceVariant,
                    tonalElevation = 2.dp
                ) {
                    Row(
                        modifier = Modifier
                            .padding(horizontal = 8.dp, vertical = 4.dp)
                            .fillMaxWidth(),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        TextField(
                            value = input,
                            onValueChange = { input = it },
                            modifier = Modifier.weight(1f),
                            placeholder = { Text("Send message...") },
                            singleLine = true,
                            trailingIcon = {
                                IconButton(
                                    onClick = {
                                        // A turn is live for this conversation: stop
                                        // it instead of starting a new one (the
                                        // backend would reject a second turn with
                                        // 409). Flip the control via the running
                                        // poller above.
                                        if (running) {
                                            sending = true
                                            scope.launch {
                                                try {
                                                    val resp = withContext(Dispatchers.IO) {
                                                        YaahClient(host.baseUrl, host.passphrase)
                                                            .getService().cancelTurn("local", convId)
                                                    }
                                                    if (!resp.isSuccessful || resp.body()?.cancelled != true) {
                                                        android.widget.Toast.makeText(
                                                            context,
                                                            "Failed to stop the run.",
                                                            android.widget.Toast.LENGTH_LONG
                                                        ).show()
                                                    }
                                                } catch (e: Exception) {
                                                    Log.e("YaahClient", "Failed to cancel turn: ${e.message}")
                                                    android.widget.Toast.makeText(
                                                        context,
                                                        "Failed to stop the run: ${e.message}",
                                                        android.widget.Toast.LENGTH_LONG
                                                    ).show()
                                                } finally {
                                                    sending = false
                                                }
                                            }
                                            return@IconButton
                                        }
                                        if (input.isBlank()) return@IconButton
                                        val msg = input
                                        input = ""
                                        sending = true
                                        scope.launch {
                                            try {
                                                sendChatMessage(host, convId, msg, workspace, model)
                                                // One immediate reload for feedback, then the
                                                // poller keeps the thread live.
                                                fetchSnapshot(host, convId)?.let {
                                                    details = it
                                                    it.conversation.workspace?.let { w -> workspace = w }
                                                }
                                            } catch (e: Exception) {
                                                Log.e("YaahClient", "Failed to send message: ${e.message}")
                                                android.widget.Toast.makeText(
                                                    context,
                                                    "Failed to send: ${e.message}",
                                                    android.widget.Toast.LENGTH_LONG
                                                ).show()
                                            } finally {
                                                loading = false
                                                sending = false
                                            }
                                        }
                                    },
                                    enabled = if (running) !sending else input.isNotBlank() && !sending
                                ) {
                                    when {
                                        sending -> CircularProgressIndicator(
                                            modifier = Modifier.size(20.dp),
                                            strokeWidth = 2.dp
                                        )
                                        running -> Icon(Icons.Default.Close, "Stop run")
                                        else -> Icon(Icons.Default.Send, "Send")
                                    }
                                }
                            }
                        )
                    }
                }
            }
        }
    }

    if (showModelDialog) {
        ModelSelectorDialog(
            models = availableModels,
            selected = model,
            onSelected = { chosen ->
                model = chosen
                Store.saveModel(context, convId, chosen)
                showModelDialog = false
            },
            onDismiss = { showModelDialog = false }
        )
    }
}

/** Fetch a conversation snapshot, or null if the request failed. */
suspend fun fetchSnapshot(host: HostEntry, convId: String): RemoteSnapshot? {
    try {
        val client = YaahClient(host.baseUrl, host.passphrase)
        val response = withContext(Dispatchers.IO) {
            client.getService().getConversationSnapshot(convId)
        }
        return if (response.isSuccessful && response.body() != null) response.body() else null
    } catch (e: Exception) {
        Log.e("YaahClient", "Failed to load conversation: ${e.message}")
        return null
    }
}

suspend fun sendChatMessage(
    host: HostEntry,
    convId: String,
    message: String,
    workspaceId: String,
    model: String?
) {
    val client = YaahClient(host.baseUrl, host.passphrase)
    val turn = TurnRequest(
        workspace = workspaceId,
        model_override = model?.ifBlank { null },
        message = message
    )
    withContext(Dispatchers.IO) {
        val response = client.getService().sendTurn("local", convId, turn)
        if (!response.isSuccessful) {
            throw java.io.IOException("turn failed: HTTP ${response.code()} ${response.message().trim()}")
        }
    }
}

@Composable
fun FormattedMessageBubble(msg: com.yaah.client.model.MessageItem, isUser: Boolean) {
    val bg = if (isUser) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surface
    val text = if (isUser) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface

    // Structured tool calls render as compact chips; tapping one inspects its
    // arguments, instead of dumping them as a big blob of text.
    val toolCalls = msg.tool_calls
    // Chips render on the assistant's tool *calls* (which carry the arguments);
    // tool-result messages show their result text instead, avoiding duplicate chips.
    if (msg.role == "assistant" && !toolCalls.isNullOrEmpty()) {
        var selectedTool by remember { mutableStateOf<com.yaah.client.model.ToolCallItem?>(null) }
        ToolCallChips(toolCalls = toolCalls, onClick = { selectedTool = it })
        if (selectedTool != null) {
            val tc = selectedTool!!
            AlertDialog(
                onDismissRequest = { selectedTool = null },
                title = {
                    Text(
                        "Tool call: ${tc.toolName}",
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis
                    )
                },
                text = {
                    val args = formatToolArguments(tc.toolArguments)
                    if (args.isEmpty()) {
                        Text("(no arguments)", style = MaterialTheme.typography.bodySmall)
                    } else {
                        Text(args, fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
                    }
                },
                confirmButton = {
                    TextButton(onClick = { selectedTool = null }) { Text("Close") }
                }
            )
        }
        if (msg.content.isNotBlank()) {
            Spacer(modifier = Modifier.height(4.dp))
            ContentBubble(msg.content, bg, text, isUser)
        }
    } else {
        ContentBubble(msg.content, bg, text, isUser)
    }
}

@Composable
private fun ContentBubble(content: String, bg: androidx.compose.ui.graphics.Color, text: androidx.compose.ui.graphics.Color, isUser: Boolean) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = if (isUser) 48.dp else 8.dp, vertical = 2.dp),
        colors = CardDefaults.cardColors(containerColor = bg)
    ) {
        Column(modifier = Modifier.padding(12.dp)) {
            if (!isUser) {
                Text(
                    "Agent",
                    style = MaterialTheme.typography.labelSmall,
                    color = text.copy(alpha = 0.7f)
                )
                Spacer(modifier = Modifier.height(4.dp))
            }
            Text(
                text = content,
                style = MaterialTheme.typography.bodyMedium,
                color = text
            )
        }
    }
}

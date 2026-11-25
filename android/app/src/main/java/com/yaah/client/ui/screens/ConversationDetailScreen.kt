package com.yaah.client.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Send
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.dp
import com.yaah.client.api.YaahClient
import com.yaah.client.model.ConversationDetails
import kotlinx.coroutines.*

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
    var details by remember { mutableStateOf<ConversationDetails?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }
    var sending by remember { mutableStateOf(false) }
    var input by remember { mutableStateOf("") }
    val listState = rememberLazyListState()

    LaunchedEffect(convId) {
        loadDetails(scope, host, convId, { details = it }, { loading = false }, { error = it })
    }

    LaunchedEffect(details?.messages?.size) {
        // Newest message last; auto-scroll to the bottom on load / when a new
        // message arrives. The user can still scroll up to read older ones.
        val count = details?.messages?.size ?: 0
        if (count > 0) {
            listState.animateScrollToItem(index = count - 1)
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(details?.title ?: "Loading...") },
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
                        loadDetails(scope, host, convId, { details = it }, { }, { error = it })
                    }
                }) {
                    Text("Retry")
                }
            }
        } else {
            Column(modifier = Modifier.fillMaxSize().padding(padding)) {
                // Messages area
                LazyColumn(
                    modifier = Modifier.weight(1f),
                    contentPadding = PaddingValues(8.dp)
                ) {
                    // Newest last: render oldest-first messages reversed so the
                    // newest sits at the bottom.
                    val ordered = details?.messages?.reversed() ?: emptyList()
                    items(ordered) { msg ->
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
                                        if (input.isBlank()) return@IconButton
                                        val msg = input
                                        sending = true
                                        input = ""
                                        scope.launch {
                                            sendChatMessage(host, convId, msg, workspace)
                                            loadDetails(scope, host, convId, {
                                                details = it
                                                it?.workspace?.let { w -> workspace = w }
                                            }, { }, { error = it })
                                            sending = false
                                        }
                                    },
                                    enabled = input.isNotBlank()
                                ) {
                                    if (sending) {
                                        CircularProgressIndicator(modifier = Modifier.size(20.dp), strokeWidth = 2.dp)
                                    } else {
                                        Icon(Icons.Default.Send, "Send")
                                    }
                                }
                            }
                        )
                    }
                }
            }
        }
    }
}

suspend fun loadDetails(
    scope: CoroutineScope,
    host: HostEntry,
    convId: String,
    onLoaded: (ConversationDetails) -> Unit,
    onDone: () -> Unit,
    onError: (String) -> Unit
) {
    try {
        val client = YaahClient(host.baseUrl, host.passphrase)
        val service = client.getService()
        withContext(Dispatchers.IO) {
            val response = service.getConversationSnapshot(convId)
            if (response.isSuccessful && response.body() != null) {
                onLoaded(response.body()!!)
            } else {
                onError("Failed: ${response.code()}")
            }
        }
    } catch (e: Exception) {
        onError(e.message ?: "Unknown error")
    }
    onDone()
}

suspend fun sendChatMessage(host: HostEntry, convId: String, message: String, workspaceId: String) {
    try {
        val client = YaahClient(host.baseUrl, host.passphrase)
        val service = client.getService()
        val turn = com.yaah.client.model.TurnRequest(
            workspace = workspaceId,
            message = message
        )
        withContext(Dispatchers.IO) {
            service.sendTurn("local", convId, turn)
        }
    } catch (e: Exception) {
        android.util.Log.e("YaahClient", "Failed to send message: " + e.message)
    }
}

@Composable
fun FormattedMessageBubble(msg: com.yaah.client.model.MessageItem, isUser: Boolean) {
    val bg = if (isUser) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surface
    val text = if (isUser) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface

    // Structured tool call: render from tool_calls, not the raw content blob.
    val toolCalls = msg.tool_calls
    if (isUser && toolCalls != null && toolCalls.isNotEmpty()) {
        Surface(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp, vertical = 1.dp),
            color = Color(0xFF3A3A3A),
            shape = MaterialTheme.shapes.small
        ) {
            Column(modifier = Modifier.padding(horizontal = 8.dp, vertical = 4.dp)) {
                Text(
                    text = "Tool call",
                    style = MaterialTheme.typography.labelSmall,
                    color = Color(0xFF8BC34A)
                )
                Spacer(modifier = Modifier.height(2.dp))
                for (tc in toolCalls) {
                    Text(
                        text = tc.function.name,
                        style = MaterialTheme.typography.bodyMedium,
                        color = Color(0xFFE0E0E0)
                    )
                    val args = tc.function.arguments?.trim()
                    if (!args.isNullOrEmpty()) {
                        Text(
                            text = args.take(200),
                            style = MaterialTheme.typography.bodySmall,
                            color = Color(0xFF9E9E9E),
                            maxLines = 3
                        )
                    }
                    Spacer(modifier = Modifier.height(2.dp))
                }
            }
        }
    } else {
        // Regular message bubble
        Card(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = if (isUser) 48.dp else 8.dp, vertical = 2.dp),
            colors = CardDefaults.cardColors(containerColor = bg)
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                if (!isUser) {
                    Text("Agent", style = MaterialTheme.typography.labelSmall, color = text.copy(alpha = 0.7f))
                    Spacer(modifier = Modifier.height(4.dp))
                }
                Text(
                    text = msg.content,
                    style = MaterialTheme.typography.bodyMedium,
                    color = text
                )
            }
        }
    }
}
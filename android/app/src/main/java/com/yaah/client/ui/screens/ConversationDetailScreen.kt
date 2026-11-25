package com.yaah.client.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.yaah.client.api.YaahClient
import com.yaah.client.model.ConversationDetails
import com.yaah.client.model.MessageItem
import java.util.concurrent.CompletableFuture

@Composable
fun ConversationDetailScreen(
    convId: String,
    workspaceId: String,
    host: HostEntry,
    onBack: () -> Unit
) {
    var details by remember { mutableStateOf<ConversationDetails?>(null) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(convId) {
        refreshDetails(host, convId, { details = it }, { loading = false }, { error = it })
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
                Button(onClick = { refreshDetails(host, convId, { details = it }, { }, { error = it }) }) {
                    Text("Retry")
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(16.dp)
            ) {
                details?.messages?.let { msgs ->
                    items(msgs) { msg ->
                        MessageBubble(msg)
                        Spacer(modifier = Modifier.height(8.dp))
                    }
                }
            }
        }
    }
}

fun refreshDetails(
    host: HostEntry,
    convId: String,
    onLoaded: (ConversationDetails) -> Unit,
    onDone: () -> Unit,
    onError: (String) -> Unit
) {
    val client = YaahClient(host.baseUrl, null)
    CompletableFuture.supplyAsync {
        client.getService().getConversationSnapshot(convId).body()
    }.thenAccept { det ->
        if (det != null) onLoaded(det)
        onDone()
    }.exceptionally {
        onError(it.message ?: "Unknown error")
        CompletableFuture.completedFuture(null)
    }
}
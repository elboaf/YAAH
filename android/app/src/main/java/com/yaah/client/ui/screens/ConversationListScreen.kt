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
import com.yaah.client.model.ConversationItem
import java.util.concurrent.CompletableFuture

@Composable
fun ConversationListScreen(
    host: HostEntry,
    onBack: () -> Unit
) {
    var conversations by remember { mutableStateOf<List<ConversationItem>>(emptyList()) }
    var loading by remember { mutableStateOf(true) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(host) {
        refreshConversations(host, { conversations = it }, { loading = false }, { error = it })
    }

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
                Button(onClick = { refreshConversations(host, { conversations = it }, { }, { error = it }) }) {
                    Text("Retry")
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(8.dp)
            ) {
                items(conversations) { conv ->
                    ConversationRow(conv)
                }
            }
        }
    }
}

fun refreshConversations(
    host: HostEntry,
    onLoaded: (List<ConversationItem>) -> Unit,
    onDone: () -> Unit,
    onError: (String) -> Unit
) {
    val client = YaahClient(host.baseUrl, null)
    CompletableFuture.supplyAsync {
        client.getService().listConversations().body()
    }.thenAccept { convs ->
        if (convs != null) onLoaded(convs)
        onDone()
    }.exceptionally {
        onError(it.message ?: "Unknown error")
        CompletableFuture.completedFuture(null)
    }
}
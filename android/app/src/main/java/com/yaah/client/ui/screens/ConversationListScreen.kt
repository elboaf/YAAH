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
import kotlinx.coroutines.*

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

    LaunchedEffect(host) {
        loadConversations(scope, host, { conversations = it }, { loading = false }, { error = it })
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
                Button(onClick = {
                    scope.launch {
                        loadConversations(scope, host, { conversations = it }, { }, { error = it })
                    }
                }) {
                    Text("Retry")
                }
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(8.dp)
            ) {
                items(conversations) { conv ->
                    ConversationRow(conv, onClick = { onConversationSelected(conv) })
                }
            }
        }
    }
}

suspend fun loadConversations(
    scope: CoroutineScope,
    host: HostEntry,
    onLoaded: (List<ConversationItem>) -> Unit,
    onDone: () -> Unit,
    onError: (String) -> Unit
) {
    try {
        val client = YaahClient(host.baseUrl, host.passphrase)
        val service = client.getService()
        withContext(Dispatchers.IO) {
            val response = service.listConversations()
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
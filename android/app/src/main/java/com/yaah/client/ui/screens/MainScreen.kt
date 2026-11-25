package com.yaah.client.ui.screens

import android.content.Context
import android.net.Uri
import androidx.compose.foundation.layout.*
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.yaah.client.api.YaahClient
import com.yaah.client.model.ConversationItem
import com.yaah.client.ui.YAAHTheme
import kotlinx.coroutines.*

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen() {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var showHostPicker by remember { mutableStateOf(true) }
    var selectedHost by remember { mutableStateOf<HostEntry?>(null) }
    var selectedConversation by remember { mutableStateOf<ConversationItem?>(null) }

    if (showHostPicker) {
        HostPickerScreen(
            scope = scope,
            onHostSelected = { host ->
                selectedHost = host
                showHostPicker = false
            }
        )
    } else {
        ConversationListScreen(
            scope = scope,
            host = selectedHost!!,
            onBack = { showHostPicker = true },
            onConversationSelected = { conv -> selectedConversation = conv }
        )
    }

    if (selectedConversation != null && selectedHost != null) {
        ConversationDetailScreen(
            scope = scope,
            convId = selectedConversation!!.id.toString(),
            workspaceId = "",
            host = selectedHost!!,
            onBack = { selectedConversation = null }
        )
    }
}

data class HostEntry(
    val name: String,
    val baseUrl: String,
    val passphrase: String?
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HostPickerScreen(
    scope: CoroutineScope,
    onHostSelected: (HostEntry) -> Unit
) {
    var manualHost by remember { mutableStateOf("") }
    var manualPassphrase by remember { mutableStateOf("") }
    var connecting by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.Center
    ) {
        Text("YAAH Thin Client", style = MaterialTheme.typography.headlineLarge)
        Spacer(modifier = Modifier.height(32.dp))

        Text(
            "Connect to a YAAH desktop on your network",
            style = MaterialTheme.typography.bodyLarge
        )
        Spacer(modifier = Modifier.height(16.dp))

        OutlinedTextField(
            value = manualHost,
            onValueChange = { manualHost = it },
            placeholder = { Text("e.g., 192.168.1.100:8765") },
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = manualPassphrase,
            onValueChange = { manualPassphrase = it },
            placeholder = { Text("Passphrase (if required)") },
            modifier = Modifier.fillMaxWidth()
        )

        if (error != null) {
            Spacer(modifier = Modifier.height(8.dp))
            Text(error!!, color = MaterialTheme.colorScheme.error)
        }

        Spacer(modifier = Modifier.height(16.dp))

        Button(
            onClick = {
                if (manualHost.isBlank()) return@Button
                connecting = true
                error = null

                val host = if (manualHost.contains(":")) manualHost else "$manualHost:8765"
                val client = YaahClient("http://$host/", manualPassphrase.ifBlank { null })

                scope.launch(Dispatchers.IO) {
                    try {
                        val service = client.getService()
                        val response = service.getHostInfo()
                        if (response.isSuccessful) {
                            withContext(Dispatchers.Main) {
                                onHostSelected(HostEntry(host, "http://$host/", manualPassphrase.ifBlank { null }))
                            }
                        } else {
                            withContext(Dispatchers.Main) {
                                error = "Connection failed: ${response.code()}"
                                connecting = false
                            }
                        }
                    } catch (e: Exception) {
                        withContext(Dispatchers.Main) {
                            error = "Connection error: ${e.message}"
                            connecting = false
                        }
                    }
                }
            },
            enabled = !connecting
        ) {
            if (connecting) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp))
            } else {
                Text("Connect")
            }
        }
    }
}
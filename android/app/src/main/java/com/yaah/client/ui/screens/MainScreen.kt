package com.yaah.client.ui.screens

import android.content.Context
import android.net.Uri
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import com.yaah.client.api.YaahClient
import com.yaah.client.model.ConversationItem
import com.yaah.client.ui.YAAHTheme
import java.util.concurrent.CompletableFuture

@Composable
fun MainScreen() {
    val context = LocalContext.current
    var showHostPicker by remember { mutableStateOf(true) }
    var selectedHost by remember { mutableStateOf<HostEntry?>(null) }

    if (showHostPicker) {
        HostPickerScreen(
            onHostSelected = { host ->
                selectedHost = host
                showHostPicker = false
            }
        )
    } else {
        ConversationListScreen(
            host = selectedHost!!,
            onBack = { showHostPicker = true }
        )
    }
}

data class HostEntry(
    val name: String,
    val baseUrl: String
)

@Composable
fun HostPickerScreen(
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

                CompletableFuture.supplyAsync {
                    client.testConnection()
                }.thenAccept { success ->
                    if (success) {
                        onHostSelected(HostEntry(host, "http://$host/"))
                    } else {
                        error = "Failed to connect. Check IP, port, and passphrase."
                        connecting = false
                    }
                }.exceptionally {
                    error = "Connection error: ${it.message}"
                    CompletableFuture.completedFuture(null)
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
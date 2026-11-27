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
import com.yaah.client.model.HostInfo
import com.yaah.client.util.Store
import com.yaah.client.ui.YAAHTheme
import kotlinx.coroutines.*
import retrofit2.Response

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
            context,
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
    context: android.content.Context,
    scope: CoroutineScope,
    onHostSelected: (HostEntry) -> Unit
) {
    val manualHostState = remember { mutableStateOf("") }
    val manualPassphraseState = remember { mutableStateOf("") }
    val connectingState = remember { mutableStateOf(false) }
    val errorState = remember { mutableStateOf<String?>(null) }

    // Restore saved connection settings so they survive app restarts.
    LaunchedEffect(Unit) {
        val (savedHost, savedPass) = Store.load(context)
        if (savedHost.isNotBlank()) {
            manualHostState.value = savedHost
            manualPassphraseState.value = savedPass
            connect(context, scope, savedHost, savedPass, connectingState, errorState, onHostSelected)
        }
    }

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
            value = manualHostState.value,
            onValueChange = { manualHostState.value = it },
            placeholder = { Text("e.g., 192.168.1.100:8765") },
            modifier = Modifier.fillMaxWidth()
        )
        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = manualPassphraseState.value,
            onValueChange = { manualPassphraseState.value = it },
            placeholder = { Text("Passphrase (if required)") },
            modifier = Modifier.fillMaxWidth()
        )

        if (errorState.value != null) {
            Spacer(modifier = Modifier.height(8.dp))
            Text(errorState.value!!, color = MaterialTheme.colorScheme.error)
        }

        Spacer(modifier = Modifier.height(16.dp))

        Button(
            onClick = {
                val hostInput = manualHostState.value
                if (hostInput.isBlank()) return@Button
                connect(context, scope, hostInput, manualPassphraseState.value, connectingState, errorState, onHostSelected)
            },
            enabled = !connectingState.value
        ) {
            if (connectingState.value) {
                CircularProgressIndicator(modifier = Modifier.size(20.dp))
            } else {
                Text("Connect")
            }
        }
    }
}

/**
 * Validate a host/passphrase over the network and persist them on success.
 * Persists before switching screens so a restart reuses the same connection.
 */
private fun connect(
    context: Context,
    scope: CoroutineScope,
    host: String,
    passphrase: String,
    connecting: androidx.compose.runtime.MutableState<Boolean>,
    error: androidx.compose.runtime.MutableState<String?>,
    onHostSelected: (HostEntry) -> Unit
) {
    connecting.value = true
    // Default to the YAAH remote port when the host has none.
    val hostWithPort = if (host.contains(":")) host else "$host:8765"
    scope.launch(Dispatchers.IO) {
        // Retry transient failures (unstable WiFi, slow handshakes) a few
        // times before giving up and showing the error state.
        val response = retryConnect(hostWithPort, passphrase.ifBlank { null })
        if (response != null) {
            Store.save(context, host, passphrase)
            withContext(Dispatchers.Main) {
                onHostSelected(HostEntry(host, "http://$hostWithPort/", passphrase.ifBlank { null }))
            }
        } else {
            withContext(Dispatchers.Main) {
                error.value = "Connection failed: can't reach $hostWithPort"
                connecting.value = false
            }
        }
    }
}

/**
 * Validate the connection by calling getHostInfo, retrying transient failures
 * (unstable USB/WiFi link, slow handshakes) a few times with backoff before
 * returning null so the caller can show the error state.
 */
private suspend fun retryConnect(
    hostWithPort: String,
    passphrase: String?
): Response<HostInfo>? {
    val client = YaahClient("http://$hostWithPort/", passphrase)
    val service = client.getService()
    repeat(4) { attempt ->
        try {
            val response = service.getHostInfo()
            if (response.isSuccessful) return response
        } catch (e: Exception) {
            // transient failure; retry below with backoff
        }
        if (attempt < 3) delay(1500L * (attempt + 1))
    }
    return null
}
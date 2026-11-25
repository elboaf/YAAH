package com.yaah.client.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.yaah.client.model.ConversationItem

@Composable
fun ConversationRow(conv: ConversationItem) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp, horizontal = 8.dp),
        onClick = { }
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(conv.title, style = MaterialTheme.typography.titleMedium)
            Text(
                conv.workspace_name,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Text(
                conv.last_message_at,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}
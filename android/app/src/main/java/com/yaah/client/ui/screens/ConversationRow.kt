package com.yaah.client.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.yaah.client.model.ConversationItem

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ConversationRow(conv: ConversationItem, onClick: () -> Unit) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp, horizontal = 8.dp),
        onClick = onClick
    ) {
        Column(modifier = Modifier.padding(16.dp)) {
            Text(conv.title, style = MaterialTheme.typography.titleMedium)
            if (!conv.workspace.isNullOrBlank()) {
                Text(
                    conv.workspace,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            if (!conv.updated_at.isNullOrBlank()) {
                Text(
                    conv.updated_at,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        }
    }
}
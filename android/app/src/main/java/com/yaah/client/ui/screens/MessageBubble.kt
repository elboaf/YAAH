package com.yaah.client.ui.screens

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import com.yaah.client.model.MessageItem

@Composable
fun MessageBubble(msg: MessageItem) {
    val isUser = msg.role == "user"
    val bg = if (isUser) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.surface
    val text = if (isUser) MaterialTheme.colorScheme.onPrimary else MaterialTheme.colorScheme.onSurface

    Box(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = if (isUser) 48.dp else 8.dp, vertical = 2.dp)
    ) {
        Surface(
            modifier = Modifier
                .padding(horizontal = 4.dp)
                .wrapContentSize(Alignment.BottomStart),
            color = bg,
            shape = MaterialTheme.shapes.medium
        ) {
            Text(
                text = msg.content,
                color = text,
                modifier = Modifier.padding(12.dp),
                style = MaterialTheme.typography.bodyMedium
            )
        }
    }
}
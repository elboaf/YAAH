package com.yaah.client.ui

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

val YAAHDarkColorScheme = darkColorScheme(
    primary = androidx.compose.ui.graphics.Color(0xFF4CAF50),
    secondary = androidx.compose.ui.graphics.Color(0xFF66BB6A),
    background = androidx.compose.ui.graphics.Color(0xFF1E1E1E),
    surface = androidx.compose.ui.graphics.Color(0xFF2C2C2C)
)

@Composable
fun YAAHTheme(
    content: @Composable () -> Unit
) {
    MaterialTheme(
        colorScheme = YAAHDarkColorScheme,
        content = {
            Surface(
                modifier = Modifier.fillMaxSize(),
                color = MaterialTheme.colorScheme.background
            ) {
                content()
            }
        }
    )
}
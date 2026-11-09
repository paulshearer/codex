package com.pshea.permissivecodex.ui

import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext

// Adapted from Sparky Chat (Apache-2.0), Copyright 2026 Paul Shearer.
@Composable
fun PermissiveTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= 31 && dark -> dynamicDarkColorScheme(context)
        Build.VERSION.SDK_INT >= 31 -> dynamicLightColorScheme(context)
        dark -> darkColorScheme(primary = Color(0xff70d6ff), secondary = Color(0xffb8c4ff), surface = Color(0xff111827), background = Color(0xff0b1020))
        else -> lightColorScheme(primary = Color(0xff00658a), secondary = Color(0xff4255a6), surface = Color(0xfff8f9ff), background = Color(0xfff5f7ff))
    }
    MaterialTheme(colorScheme = colors, content = content)
}

package com.yaah.client.util

import android.content.Context

/**
 * Durable storage for the thin client's connection settings so they survive
 * app restarts. Backed by SharedPreferences (private to this app).
 */
object Store {
    private const val NAME = "yaah_client_prefs"
    private const val KEY_HOST = "host"
    private const val KEY_PHRASE = "passphrase"

    fun save(context: Context, host: String, passphrase: String) {
        context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_HOST, host)
            .putString(KEY_PHRASE, passphrase)
            .apply()
    }

    fun load(context: Context): Pair<String, String> {
        val prefs = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
        val host = prefs.getString(KEY_HOST, "") ?: ""
        val pass = prefs.getString(KEY_PHRASE, "") ?: ""
        return host to pass
    }
}

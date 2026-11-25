package com.yaah.client.util

import android.content.Context
import org.json.JSONObject

/**
 * Durable storage for the thin client's connection settings so they survive
 * app restarts. Backed by SharedPreferences (private to this app).
 */
object Store {
    private const val NAME = "yaah_client_prefs"
    private const val KEY_HOST = "host"
    private const val KEY_PHRASE = "passphrase"
    // Per-conversation model override, keyed by conversation id. Stored as a
    // JSON object so each chat keeps its own model (mirrors the desktop, which
    // carries the model in its per-conversation scope and re-sends it each turn).
    private const val KEY_MODELS = "conv_models"

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

    /** Persist the model for one conversation. */
    fun saveModel(context: Context, convId: String, model: String) {
        val prefs = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
        val existing = prefs.getString(KEY_MODELS, "") ?: ""
        val json = if (existing.isBlank()) JSONObject() else JSONObject(existing)
        json.put(convId, model)
        prefs.edit().putString(KEY_MODELS, json.toString()).apply()
    }

    /** The model stored for one conversation, or null if none was chosen. */
    fun loadModel(context: Context, convId: String): String? {
        val prefs = context.getSharedPreferences(NAME, Context.MODE_PRIVATE)
        val existing = prefs.getString(KEY_MODELS, "") ?: ""
        if (existing.isBlank()) return null
        return try {
            val json = JSONObject(existing)
            if (json.has(convId)) json.getString(convId) else null
        } catch (e: Exception) {
            null
        }
    }
}

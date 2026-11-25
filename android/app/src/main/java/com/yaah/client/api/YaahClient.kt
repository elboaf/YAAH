package com.yaah.client.api

import com.yaah.client.model.LeaseRequest
import com.yaah.client.model.TurnRequest
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

class YaahClient(baseUrl: String, private var passphrase: String?) {
    private val retrofit: Retrofit

    init {
        val logging = HttpLoggingInterceptor().apply {
            level = HttpLoggingInterceptor.Level.BODY
        }

        val client = OkHttpClient.Builder()
            .addInterceptor(logging)
            .addInterceptor { chain ->
                val request = chain.request().newBuilder()
                request.header("x-yaah-remote", "1")
                if (passphrase != null) {
                    request.header("x-yaah-passphrase", passphrase!!)
                }
                chain.proceed(request.build())
            }
            // `sendTurn` is a streaming NDJSON turn that runs on the owner
            // machine; a model can take minutes to produce its first byte, so
            // the read timeout must not fire mid-run. connectTimeout still
            // bounds connection stalls; reads are left unbounded.
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(0, TimeUnit.SECONDS)
            .build()

        retrofit = Retrofit.Builder()
            .baseUrl(baseUrl)
            .client(client)
            .addConverterFactory(GsonConverterFactory.create())
            .build()
    }

    fun getService(): YaahApiService {
        return retrofit.create(YaahApiService::class.java)
    }

    fun updatePassphrase(newPassphrase: String?) {
        passphrase = newPassphrase
    }
}
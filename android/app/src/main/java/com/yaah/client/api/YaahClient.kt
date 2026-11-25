package com.yaah.client.api

import com.yaah.client.model.LeaseRequest
import com.yaah.client.model.TurnRequest
import okhttp3.OkHttpClient
import okhttp3.logging.HttpLoggingInterceptor
import retrofit2.Retrofit
import retrofit2.converter.gson.GsonConverterFactory
import java.util.concurrent.TimeUnit

class YaahClient(private val baseUrl: String, private var passphrase: String?) {
    private var retrofit: Retrofit

    init {
        setupRetrofit()
    }

    private fun setupRetrofit() {
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
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(30, TimeUnit.SECONDS)
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
        setupRetrofit()
    }

    suspend fun testConnection(): Boolean {
        try {
            val service = getService()
            val response = service.getHostInfo()
            return response.isSuccessful
        } catch (e: Exception) {
            return false
        }
    }
}
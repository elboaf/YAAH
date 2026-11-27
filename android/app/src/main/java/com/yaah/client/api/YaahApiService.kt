package com.yaah.client.api

import com.yaah.client.model.*
import retrofit2.http.*
import retrofit2.Response
import retrofit2.http.Body

interface YaahApiService {
    // Remote endpoints used by thin client
    @GET("api/remote/info")
    suspend fun getHostInfo(): Response<HostInfo>

    @GET("api/remote/conversations")
    suspend fun listConversations(): Response<List<ConversationItem>>

    @GET("api/remote/conversations/{conversationId}/messages")
    suspend fun getConversationMessages(
        @Path("conversationId") conversationId: String,
        @Query("limit") limit: Int? = null
    ): Response<List<MessageItem>>

    @GET("api/remote/conversations/{conversationId}/snapshot")
    suspend fun getConversationSnapshot(
        @Path("conversationId") conversationId: String
    ): Response<RemoteSnapshot>

    @GET("api/models/available")
    suspend fun getAvailableModels(): Response<AvailableModels>

    @POST("api/remote/conversations/{conversationId}/lease")
    suspend fun acquireLease(
        @Path("conversationId") conversationId: String,
        @Body body: LeaseRequest
    ): Response<LeaseResponse>

    @POST("api/remote/devices/{hostId}/turns/{conversationId}")
    suspend fun sendTurn(
        @Path("hostId") hostId: String,
        @Path("conversationId") conversationId: String,
        @Body body: TurnRequest
    ): Response<Void>

    @POST("api/remote/devices/{hostId}/turns/{conversationId}/cancel")
    suspend fun cancelTurn(
        @Path("hostId") hostId: String,
        @Path("conversationId") conversationId: String
    ): Response<CancelResponse>

    // Authoritative running-turn state for a device: the conversation IDs
    // mid-turn ([running]) and those parked on a pending ask_user ([pending]).
    @GET("api/remote/devices/{hostId}/running")
    suspend fun runningAgents(
        @Path("hostId") hostId: String
    ): Response<RunningAgents>
}
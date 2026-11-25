package com.yaah.client.model

data class HostInfo(
    val instance_id: String,
    val host_id: String,
    val os: String,
    val protocol_version: Int,
    val auth_required: Boolean,
    val display_name: String? = null
)

data class ConversationItem(
    val conversation_id: String,
    val workspace_id: String,
    val workspace_name: String,
    val title: String,
    val chat_type: String,
    val last_message_at: String,
    val revision: Int,
    val pinned: Boolean = false
)

data class ConversationDetails(
    val conversation_id: String,
    val workspace_id: String,
    val title: String,
    val chat_type: String,
    val revision: Int,
    val messages: List<MessageItem>,
    val running_agent: Boolean = false,
    val running_agent_pid: Int? = null,
    val running_agent_task: String? = null
)

data class MessageItem(
    val message_id: Int,
    val role: String,
    val content: String,
    val created_at: String,
    val tool_calls: List<Map<String, String>>? = null
)

data class LeaseRequest(
    val revision: Int,
    val holder_id: String
)

data class LeaseResponse(
    val lease_token: String,
    val revision: Int
)

data class TurnRequest(
    val conversation_id: String,
    val workspace_id: String,
    val model: String,
    val effort: String,
    val message: String,
    val turn_type: String = "chat"
)

data class TurnResponse(
    val turn_id: String,
    val status: String
)
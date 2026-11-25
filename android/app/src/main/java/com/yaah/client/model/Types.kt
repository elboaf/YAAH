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
    val id: Int,
    val title: String,
    val workspace: String?,
    val created_at: String?,
    val updated_at: String?,
    val chat_type: String?,
    val model: String?,
    val effort: String?,
    val revision: String?,
    val cost_spent: Double? = null,
    val completion_tokens_total: Int? = null
)

data class ConversationDetails(
    val conversation_id: String,
    val workspace_id: String,
    val workspace: String? = null,
    val title: String,
    val chat_type: String,
    val revision: String,
    val messages: List<MessageItem>,
    val running_agent: Boolean = false,
    val running_agent_pid: Int? = null,
    val running_agent_task: String? = null
)

data class ToolCallItem(
    val id: String,
    val type: String,
    val function: FunctionCallItem
)

data class FunctionCallItem(
    val name: String,
    val arguments: String
)

data class MessageItem(
    val message_id: Int,
    val role: String,
    val content: String,
    val created_at: String,
    val tool_calls: List<ToolCallItem>? = null
)

data class LeaseRequest(
    val revision: String,
    val holder_id: String
)

data class LeaseResponse(
    val lease_token: String,
    val revision: String
)

data class TurnRequest(
    val workspace: String = "",
    val model_override: String? = null,
    val effort_override: String? = null,
    val message: String
)

data class TurnResponse(
    val turn_id: String,
    val status: String
)
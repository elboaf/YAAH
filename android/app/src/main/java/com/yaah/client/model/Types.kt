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
    val title: String? = null,
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

/**
 * The conversation half of a remote snapshot. The backend nests the whole
 * conversation row under a "conversation" key, so this mirrors those columns.
 * All fields are nullable: the remote row omits some (e.g. running_agent).
 */
data class ConvInfo(
    val id: Int? = null,
    val conversation_id: Int? = null,
    val title: String? = null,
    val workspace: String? = null,
    val workspace_id: String? = null,
    val chat_type: String? = null,
    val model: String? = null,
    val effort: String? = null,
    val subagent_model: String? = null,
    val revision: String? = null,
    val running_agent: Boolean? = null,
    val running_agent_pid: Int? = null,
    val running_agent_task: String? = null
)

/** Remote snapshot: {"conversation": {...}, "messages": [...], "revision": "..."}. */
data class RemoteSnapshot(
    val conversation: ConvInfo,
    val messages: List<MessageItem>,
    val revision: String? = null
)

/** {"providers": {name: {models: [...]|error}}, "active_provider", "model"}. */
data class AvailableModels(
    val providers: Map<String, ProviderModels> = emptyMap(),
    val active_provider: String? = null,
    val model: String? = null
)

data class ProviderModels(
    val models: List<String> = emptyList(),
    val error: String? = null
)

/** A selectable model, "provider::model_id", grouped under its provider. */
data class ModelOption(val provider: String, val id: String) {
    val key: String get() = "$provider::$id"
    override fun equals(other: Any?): Boolean =
        other is ModelOption && other.provider == provider && other.id == id
    override fun hashCode(): Int = 31 * provider.hashCode() + id.hashCode()
}

/** A single tool call. The backend stores it in two shapes: assistant messages
 * nest the name/arguments under `function`, while tool-result messages put
 * `name` at the top level with no `function`. Both parse here; use
 * [toolName]/[toolArguments] to read the effective value regardless of shape. */
data class ToolCallItem(
    val id: String? = null,
    val type: String? = null,
    val name: String? = null,
    val function: FunctionCallItem? = null
) {
    /** Tool name across both backend storage shapes. */
    val toolName: String get() = function?.name ?: name ?: "tool"
    /** Arguments across both shapes (null for tool-result messages, which
     * carry only the name). */
    val toolArguments: String? get() = function?.arguments
}

data class FunctionCallItem(
    val name: String? = null,
    val arguments: String? = null
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

/** Running-turn state for a device: which conversations are mid-turn and which
 * are parked on a pending ask_user answer. */
data class RunningAgents(
    val status: String = "unknown",
    val running: List<String> = emptyList(),
    val pending: List<String> = emptyList()
)

/** {"ok": Boolean, "cancelled": Boolean} returned by the cancel endpoint. */
data class CancelResponse(
    val ok: Boolean = false,
    val cancelled: Boolean = false
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
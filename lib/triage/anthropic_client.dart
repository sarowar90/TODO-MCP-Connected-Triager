// Step 4/6 — Anthropic Messages API client.
//
// A thin wrapper over `POST /v1/messages` supporting tools and streaming. It's
// split behind the [MessagesApi] interface so the triage loop can be driven by
// a scripted fake in tests and the real HTTP client in production.
//
// Streaming (Step 6): [createMessageStreaming] consumes the Server-Sent-Events
// response and rebuilds the *same* `{content, stop_reason}` map the
// non-streaming path returns — so the rest of the code is unchanged — while
// invoking callbacks with text as it's generated for a live UX.

import 'dart:convert';

import 'package:http/http.dart' as http;

/// The API surface the triager needs. An abstract class (not a bare interface)
/// so subclasses inherit the default [createMessageStreaming], which lets test
/// fakes skip SSE and reuse their [createMessage].
abstract class MessagesApi {
  Future<Map<String, dynamic>> createMessage({
    required String model,
    required int maxTokens,
    String? system,
    required List<Map<String, dynamic>> messages,
    List<Map<String, dynamic>>? tools,
    Map<String, dynamic>? toolChoice,
    List<Map<String, dynamic>>? mcpServers,
  });

  /// Streaming variant: returns the same result as [createMessage] but invokes
  /// [onTextDelta] with incremental text and [onToolUseStart] when a tool call
  /// begins. The default delegates to [createMessage] and reports the finished
  /// blocks once — real clients override with true SSE streaming.
  Future<Map<String, dynamic>> createMessageStreaming({
    required String model,
    required int maxTokens,
    String? system,
    required List<Map<String, dynamic>> messages,
    List<Map<String, dynamic>>? tools,
    Map<String, dynamic>? toolChoice,
    List<Map<String, dynamic>>? mcpServers,
    void Function(String textDelta)? onTextDelta,
    void Function(String toolName)? onToolUseStart,
  }) async {
    final response = await createMessage(
      model: model,
      maxTokens: maxTokens,
      system: system,
      messages: messages,
      tools: tools,
      toolChoice: toolChoice,
      mcpServers: mcpServers,
    );
    final content =
        (response['content'] as List?)?.cast<Map<String, dynamic>>() ?? const [];
    for (final block in content) {
      if (block['type'] == 'tool_use') {
        onToolUseStart?.call(block['name'] as String);
      } else if (block['type'] == 'text') {
        onTextDelta?.call(block['text'] as String);
      }
    }
    return response;
  }
}

/// Thrown when the API responds with a non-200 status or a stream `error` event.
class AnthropicException implements Exception {
  AnthropicException(this.statusCode, this.message);

  final int statusCode;
  final String message;

  @override
  String toString() => 'AnthropicException($statusCode): $message';
}

/// Real HTTP implementation talking to the Anthropic API.
class AnthropicClient extends MessagesApi {
  AnthropicClient({required this.apiKey, http.Client? httpClient})
      : _http = httpClient ?? http.Client();

  final String apiKey;
  final http.Client _http;

  static const _url = 'https://api.anthropic.com/v1/messages';
  static const _apiVersion = '2023-06-01';

  /// Opt-in beta for the MCP connector — lets Claude call a remote MCP server's
  /// tools (e.g. GitHub) server-side. Only sent when [mcpServers] is supplied.
  static const _mcpBeta = 'mcp-client-2025-11-20';

  Map<String, String> get _headers => {
        'content-type': 'application/json',
        'x-api-key': apiKey,
        'anthropic-version': _apiVersion,
      };

  /// Adds the MCP-connector beta header when the request uses MCP servers.
  Map<String, String> _headersFor(List<Map<String, dynamic>>? mcpServers) {
    if (mcpServers == null || mcpServers.isEmpty) return _headers;
    return {..._headers, 'anthropic-beta': _mcpBeta};
  }

  /// Wraps the (stable) system prompt in a cached text block. `tools` and
  /// `system` render before `messages`, so one `cache_control` breakpoint on the
  /// system block caches the whole tools+system prefix together — it's byte-for-
  /// byte identical across every tool-loop iteration, both tiers, and every
  /// message triaged, so iterations after the first read it at ~0.1x input cost
  /// instead of reprocessing it. Below the model's minimum cacheable prefix the
  /// breakpoint is a silent no-op (no error, just no cache), so this is safe to
  /// leave on unconditionally.
  static List<Map<String, dynamic>>? _systemBlocks(String? system) =>
      system == null
          ? null
          : [
              {
                'type': 'text',
                'text': system,
                'cache_control': {'type': 'ephemeral'},
              },
            ];

  @override
  Future<Map<String, dynamic>> createMessage({
    required String model,
    required int maxTokens,
    String? system,
    required List<Map<String, dynamic>> messages,
    List<Map<String, dynamic>>? tools,
    Map<String, dynamic>? toolChoice,
    List<Map<String, dynamic>>? mcpServers,
  }) async {
    final mcp = (mcpServers?.isNotEmpty ?? false) ? mcpServers : null;
    final response = await _http.post(
      Uri.parse(_url),
      headers: _headersFor(mcp),
      body: jsonEncode({
        'model': model,
        'max_tokens': maxTokens,
        'messages': messages,
        'system': ?_systemBlocks(system),
        'tools': ?tools,
        'tool_choice': ?toolChoice,
        'mcp_servers': ?mcp,
      }),
    );

    if (response.statusCode != 200) {
      throw AnthropicException(
          response.statusCode, _errorMessage(response.body));
    }
    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  @override
  Future<Map<String, dynamic>> createMessageStreaming({
    required String model,
    required int maxTokens,
    String? system,
    required List<Map<String, dynamic>> messages,
    List<Map<String, dynamic>>? tools,
    Map<String, dynamic>? toolChoice,
    List<Map<String, dynamic>>? mcpServers,
    void Function(String textDelta)? onTextDelta,
    void Function(String toolName)? onToolUseStart,
  }) async {
    final mcp = (mcpServers?.isNotEmpty ?? false) ? mcpServers : null;
    final request = http.Request('POST', Uri.parse(_url))
      ..headers.addAll(_headersFor(mcp))
      ..body = jsonEncode({
        'model': model,
        'max_tokens': maxTokens,
        'messages': messages,
        'stream': true,
        'system': ?_systemBlocks(system),
        'tools': ?tools,
        'tool_choice': ?toolChoice,
        'mcp_servers': ?mcp,
      });

    final streamed = await _http.send(request);
    if (streamed.statusCode != 200) {
      final body = await streamed.stream.bytesToString();
      throw AnthropicException(streamed.statusCode, _errorMessage(body));
    }

    return _reconstructFromSse(streamed.stream, onTextDelta, onToolUseStart);
  }

  /// Rebuilds the `{content, stop_reason}` message from the SSE event stream,
  /// emitting text/tool-start callbacks as events arrive. The reconstructed map
  /// is byte-for-byte the shape [createMessage] returns, so downstream parsing
  /// (including `create_ticket` -> [TriageResult]) is identical.
  Future<Map<String, dynamic>> _reconstructFromSse(
    Stream<List<int>> byteStream,
    void Function(String textDelta)? onTextDelta,
    void Function(String toolName)? onToolUseStart,
  ) async {
    final blocks = <int, Map<String, dynamic>>{};
    // tool_use inputs stream as JSON fragments; buffer them per block index.
    final partialJson = <int, StringBuffer>{};
    String? stopReason;

    final lines =
        byteStream.transform(utf8.decoder).transform(const LineSplitter());

    await for (final line in lines) {
      if (!line.startsWith('data:')) continue; // skip `event:` and `:` comments
      final data = line.substring(5).trim();
      if (data.isEmpty) continue;
      final event = jsonDecode(data) as Map<String, dynamic>;

      switch (event['type']) {
        case 'content_block_start':
          final index = event['index'] as int;
          final block =
              Map<String, dynamic>.from(event['content_block'] as Map);
          blocks[index] = block;
          if (block['type'] == 'tool_use') {
            partialJson[index] = StringBuffer();
            onToolUseStart?.call(block['name'] as String);
          } else if (block['type'] == 'text') {
            block['text'] = block['text'] ?? '';
          }

        case 'content_block_delta':
          final index = event['index'] as int;
          final delta = event['delta'] as Map<String, dynamic>;
          switch (delta['type']) {
            case 'text_delta':
              final text = delta['text'] as String;
              blocks[index]!['text'] =
                  (blocks[index]!['text'] as String? ?? '') + text;
              onTextDelta?.call(text);
            case 'input_json_delta':
              partialJson[index]!.write(delta['partial_json'] as String);
          }

        case 'content_block_stop':
          final index = event['index'] as int;
          final buffer = partialJson[index];
          if (buffer != null) {
            final raw = buffer.toString();
            blocks[index]!['input'] =
                raw.isEmpty ? <String, dynamic>{} : jsonDecode(raw);
          }

        case 'message_delta':
          final delta = event['delta'] as Map<String, dynamic>?;
          stopReason = (delta?['stop_reason'] as String?) ?? stopReason;

        case 'error':
          throw AnthropicException(
              200, (event['error']?['message'] as String?) ?? 'stream error');

        // message_start, message_stop, ping: nothing to accumulate.
      }
    }

    final orderedIndices = blocks.keys.toList()..sort();
    return {
      'content': [for (final i in orderedIndices) blocks[i]!],
      'stop_reason': stopReason,
    };
  }

  static String _errorMessage(String body) {
    try {
      final decoded = jsonDecode(body) as Map<String, dynamic>;
      return (decoded['error']?['message'] as String?) ?? body;
    } catch (_) {
      return body;
    }
  }

  void close() => _http.close();
}

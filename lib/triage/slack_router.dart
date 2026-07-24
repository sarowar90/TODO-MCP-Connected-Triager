// Step 10 — Slack routing.
//
// Delivers a completed [TriageOutcome] to the right team's Slack channel via the
// Slack MCP connector. This is a *separate* step from classification: the triage
// loop ends at `create_ticket` (its terminal action), so notifying Slack happens
// after, keyed off the routed team.
//
// The destination is chosen deterministically in code ([slackChannelFor]) and
// handed to the model as a fixed instruction — the model is a relay that posts
// to the channel it's told, not a decider. That's what makes "reaches the
// correct destination" a property of the routing table, not the LLM.

// Named constructor params can't be private, so they're mapped to private
// fields in the initializer list — which trips this lint spuriously.
// ignore_for_file: prefer_initializing_formals

import 'anthropic_client.dart';
import 'slack_mcp.dart';
import 'triage_service.dart';
import 'triage_taxonomy.dart';

/// The result of posting a triage notification to Slack.
class SlackDelivery {
  const SlackDelivery({
    required this.channel,
    required this.posted,
    required this.toolInput,
    required this.resultContent,
    required this.isError,
  });

  /// The channel this outcome routed to (deterministic, from the team).
  final String channel;

  /// Whether the Slack post tool was actually invoked.
  final bool posted;

  /// The arguments the model passed to the Slack post tool (includes the
  /// `channel` it posted to — verify it matches [channel]).
  final Map<String, dynamic> toolInput;

  /// The raw Slack tool result (e.g. `{"ok":true,"ts":"..."}`).
  final String resultContent;

  final bool isError;

  /// True when the post landed in the intended channel. The model is instructed
  /// to use [channel]; this confirms it did.
  bool get reachedCorrectDestination =>
      posted && !isError && (toolInput['channel'] == channel);

  @override
  String toString() =>
      'SlackDelivery($channel, posted: $posted, result: $resultContent)';
}

/// Posts triage notifications to Slack channels via the MCP connector.
class SlackRouter {
  SlackRouter({
    required MessagesApi api,
    required String authorizationToken,
    Map<Team, String>? channels,
    String model = 'claude-haiku-4-5',
    int maxIterations = 3,
    String serverName = 'slack',
  })  : _api = api,
        _token = authorizationToken,
        _channels = channels ?? defaultTeamChannels,
        _model = model,
        _maxIterations = maxIterations,
        _serverName = serverName;

  final MessagesApi _api;
  final String _token;
  final Map<Team, String> _channels;
  final String _model;
  final int _maxIterations;
  final String _serverName;

  static const _system =
      'You deliver support-triage notifications to Slack via the slack tool. '
      'Post the notification you are given to EXACTLY the channel named in the '
      'instruction — never choose a different channel. Post once, then stop.';

  /// Routes [outcome] to its team's channel and posts a summary there.
  Future<SlackDelivery> route(TriageOutcome outcome) async {
    final channel = slackChannelFor(outcome.result.team, _channels);
    final servers = [
      slackMcpServer(authorizationToken: _token, name: _serverName),
    ];
    final tools = [slackToolset(serverName: _serverName)];
    final messages = <Map<String, dynamic>>[
      {'role': 'user', 'content': _notification(outcome, channel)},
    ];

    for (var i = 0; i < _maxIterations; i++) {
      final response = await _api.createMessage(
        model: _model,
        maxTokens: 512,
        system: _system,
        messages: messages,
        tools: tools,
        mcpServers: servers,
      );
      final content =
          (response['content'] as List).cast<Map<String, dynamic>>();
      messages.add({'role': 'assistant', 'content': content});

      final post = _findPost(content);
      if (post != null) {
        return SlackDelivery(
          channel: channel,
          posted: true,
          toolInput: (post.$1['input'] as Map?)?.cast<String, dynamic>() ??
              const {},
          resultContent: _resultText(post.$2),
          isError: post.$2?['is_error'] == true,
        );
      }

      // Server tool paused mid-work — re-send to let it resume.
      if (response['stop_reason'] == 'pause_turn') continue;

      // The model replied without posting; nudge it once more.
      messages.add({
        'role': 'user',
        'content': 'Post the notification now, to $channel, using the slack '
            'tool.',
      });
    }

    return SlackDelivery(
      channel: channel,
      posted: false,
      toolInput: const {},
      resultContent: '',
      isError: true,
    );
  }

  /// The message posted to the channel — the triage result in one glance.
  String _notification(TriageOutcome outcome, String channel) {
    final r = outcome.result;
    return 'Post this triage notification to Slack channel $channel and '
        'nowhere else:\n'
        '${r.urgency.label} · ${r.topic.label} → ${r.team.label}\n'
        '${r.summary}\n'
        'Ticket ${outcome.ticketId ?? 'n/a'} · '
        'confidence ${r.confidence.toStringAsFixed(2)}'
        '${r.needsHumanReview ? ' · needs human review' : ''}';
  }

  /// Finds the Slack post `mcp_tool_use` in [content] and its paired result.
  (Map<String, dynamic>, Map<String, dynamic>?)? _findPost(
    List<Map<String, dynamic>> content,
  ) {
    final resultsById = {
      for (final b in content)
        if (b['type'] == 'mcp_tool_result') b['tool_use_id'] as String: b,
    };
    for (final b in content) {
      if (b['type'] == 'mcp_tool_use') {
        return (b, resultsById[b['id'] as String]);
      }
    }
    return null;
  }

  static String _resultText(Map<String, dynamic>? result) {
    final content = result?['content'];
    if (content is String) return content;
    if (content is List) {
      return content
          .whereType<Map>()
          .where((b) => b['type'] == 'text')
          .map((b) => b['text'] as String? ?? '')
          .join();
    }
    return '';
  }
}

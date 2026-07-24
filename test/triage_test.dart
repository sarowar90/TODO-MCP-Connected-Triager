import 'dart:convert';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:todo_app/triage/anthropic_client.dart';
import 'package:todo_app/triage/github_mcp.dart';
import 'package:todo_app/triage/model_policy.dart';
import 'package:todo_app/triage/sla_policy.dart';
import 'package:todo_app/triage/slack_mcp.dart';
import 'package:todo_app/triage/slack_router.dart';
import 'package:todo_app/triage/tools.dart';
import 'package:todo_app/triage/triage_service.dart';
import 'package:todo_app/triage/triage_taxonomy.dart';

/// A [MessagesApi] that replays a scripted list of responses, standing in for
/// Claude. Lets us confirm the tool-use loop drives tools correctly with no
/// network call — deterministic and offline. Extends (not implements) so it
/// inherits the default `createMessageStreaming`.
class FakeMessagesApi extends MessagesApi {
  FakeMessagesApi(this.responses);

  final List<Map<String, dynamic>> responses;
  final List<Map<String, dynamic>> requests = [];
  int _index = 0;

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
    // Snapshot the messages list — the service keeps mutating the same
    // instance across the loop, so store a copy to inspect per-request state.
    requests.add({
      'model': model,
      'messages': List<Map<String, dynamic>>.from(messages),
      'tool_choice': toolChoice,
      'tools': tools,
      'mcp_servers': mcpServers,
    });
    if (_index >= responses.length) {
      throw StateError('FakeMessagesApi ran out of scripted responses.');
    }
    return responses[_index++];
  }
}

/// Wraps a [ToolExecutor] to record how many calls run at once — a delay forces
/// overlap so concurrent execution is observable. `maxConcurrent > 1` proves the
/// service ran the calls in parallel rather than sequentially.
class ConcurrencyTrackingExecutor implements ToolExecutor {
  ConcurrencyTrackingExecutor(this._inner);

  final ToolExecutor _inner;
  int _inFlight = 0;
  int maxConcurrent = 0;

  @override
  Future<ToolResult> execute(String name, Map<String, dynamic> input) async {
    _inFlight++;
    maxConcurrent = max(maxConcurrent, _inFlight);
    await Future<void>.delayed(const Duration(milliseconds: 20));
    final result = await _inner.execute(name, input);
    _inFlight--;
    return result;
  }
}

// --- Response builders that mimic the API's content-block shapes ---

Map<String, dynamic> toolUse(String id, String name, Map<String, dynamic> input) =>
    {'type': 'tool_use', 'id': id, 'name': name, 'input': input};

Map<String, dynamic> assistantToolTurn(List<Map<String, dynamic>> blocks) =>
    {'stop_reason': 'tool_use', 'content': blocks};

/// A turn where a built-in server tool ran and the server-side loop paused.
/// `server_tool_use` blocks are NOT client `tool_use` — the loop must resume,
/// not treat them as "answered without routing".
Map<String, dynamic> assistantPauseTurn(List<Map<String, dynamic>> blocks) =>
    {'stop_reason': 'pause_turn', 'content': blocks};

Map<String, dynamic> serverToolUse(
        String id, String name, Map<String, dynamic> input) =>
    {'type': 'server_tool_use', 'id': id, 'name': name, 'input': input};

/// A GitHub MCP tool invocation the server executed, and its result — the shape
/// the MCP connector returns in the response content.
Map<String, dynamic> mcpToolUse(
        String id, String name, Map<String, dynamic> input) =>
    {
      'type': 'mcp_tool_use',
      'id': id,
      'name': name,
      'server_name': 'github',
      'input': input,
    };

Map<String, dynamic> mcpToolResult(String toolUseId, String text,
        {bool isError = false}) =>
    {
      'type': 'mcp_tool_result',
      'tool_use_id': toolUseId,
      'is_error': isError,
      'content': [
        {'type': 'text', 'text': text},
      ],
    };

Map<String, dynamic> assistantTextTurn(String text) => {
      'stop_reason': 'end_turn',
      'content': [
        {'type': 'text', 'text': text},
      ],
    };

Map<String, dynamic> ticketInput({
  String urgency = 'high',
  String topic = 'billing',
  String team = 'billing',
  double confidence = 0.9,
  bool needsReview = false,
  String? customerId = 'CUST-778',
  String? orderId = 'ORD-1002',
}) =>
    {
      'urgency': urgency,
      'topic': topic,
      'team': team,
      'summary': 'Customer reports a duplicate charge on their subscription.',
      'rationale': 'Money actively at stake -> high; billing topic default team.',
      'confidence': confidence,
      'needs_human_review': needsReview,
      'customer_id': customerId,
      'order_id': orderId,
    };

/// A completed triage outcome for a given team — feeds the Slack router tests.
TriageOutcome outcomeFor(Team team, {String ticketId = 'TICK-5000'}) =>
    TriageOutcome(
      result: TriageResult(
        urgency: Urgency.high,
        topic: Topic.complaint,
        team: team,
        summary: 'Customer is threatening to cancel over repeat billing errors.',
        rationale: 'Cancellation threat -> retention, urgency high.',
        confidence: 0.9,
        needsHumanReview: false,
      ),
      tierUsed: ModelTier.fastClassifier,
      escalated: false,
      toolCalls: const [],
      ticketId: ticketId,
    );

/// A Slack post the connector executed server-side, plus its result.
Map<String, dynamic> slackPostTurn(String channel, String resultJson,
        {bool isError = false}) =>
    {
      'stop_reason': 'end_turn',
      'content': [
        {'type': 'text', 'text': 'Posting the notification.'},
        {
          'type': 'mcp_tool_use',
          'id': 's1',
          'name': 'slack_post_message',
          'server_name': 'slack',
          'input': {'channel': channel, 'text': 'triage notification'},
        },
        {
          'type': 'mcp_tool_result',
          'tool_use_id': 's1',
          'is_error': isError,
          'content': [
            {'type': 'text', 'text': resultJson},
          ],
        },
      ],
    };

// --- SSE helpers for the streaming tests ---

/// Serialises events into the wire SSE format Anthropic emits.
String sseBody(List<Map<String, dynamic>> events) =>
    events.map((e) => 'event: ${e['type']}\ndata: ${jsonEncode(e)}\n\n').join();

/// A [MockClient] whose streaming responses deliver [body] in tiny byte chunks
/// (splitting mid-line) to prove the SSE parser reassembles lines across chunks.
MockClient sseMockClient(String body, {int status = 200}) {
  return MockClient.streaming((request, bodyStream) async {
    final bytes = utf8.encode(body);
    final chunks = <List<int>>[
      for (var i = 0; i < bytes.length; i += 7)
        bytes.sublist(i, min(i + 7, bytes.length)),
    ];
    return http.StreamedResponse(Stream.fromIterable(chunks), status);
  });
}

/// Splits [s] into [n] roughly equal fragments — stands in for how the API
/// chunks a tool's JSON input across many input_json_delta events.
List<String> fragments(String s, int n) {
  final size = (s.length / n).ceil();
  return [
    for (var i = 0; i < s.length; i += size)
      s.substring(i, min(i + size, s.length)),
  ];
}

void main() {
  group('MockToolExecutor', () {
    test('look_up_customer returns a known customer', () async {
      final exec = MockToolExecutor();
      final result =
          await exec.execute('look_up_customer', {'email': 'jane@example.com'});
      final data = jsonDecode(result.content) as Map<String, dynamic>;
      expect(data['customer_id'], 'CUST-778');
      expect(result.isError, isFalse);
    });

    test('look_up_customer reports not_found for unknown email', () async {
      final exec = MockToolExecutor();
      final result =
          await exec.execute('look_up_customer', {'email': 'nobody@example.com'});
      final data = jsonDecode(result.content) as Map<String, dynamic>;
      expect(data['error'], 'customer_not_found');
    });

    test('fetch_order returns a known order', () async {
      final exec = MockToolExecutor();
      final result = await exec.execute('fetch_order', {'order_id': 'ORD-1002'});
      final data = jsonDecode(result.content) as Map<String, dynamic>;
      expect(data['status'], 'paid');
      expect(data['amount'], 120.0);
    });

    test('create_ticket records the ticket and returns an id', () async {
      final exec = MockToolExecutor();
      final result = await exec.execute('create_ticket', ticketInput());
      final data = jsonDecode(result.content) as Map<String, dynamic>;
      expect(data['ticket_id'], startsWith('TICK-'));
      expect(data['status'], 'created');
      expect(exec.createdTickets, hasLength(1));
      expect(exec.createdTickets.first['topic'], 'billing');
    });
  });

  group('tool definitions', () {
    test('all tools are strict and well-formed', () {
      final tools = triageTools();
      expect(
          tools.map((t) => t['name']),
          containsAll([
            'look_up_customer',
            'fetch_order',
            'fetch_recent_tickets',
            'create_ticket',
          ]));
      for (final t in tools) {
        expect(t['strict'], isTrue);
        expect((t['input_schema'] as Map)['additionalProperties'], isFalse);
      }
    });

    test('create_ticket enums match the taxonomy wire names', () {
      final createTicket =
          triageTools().firstWhere((t) => t['name'] == 'create_ticket');
      final props =
          (createTicket['input_schema'] as Map)['properties'] as Map;
      expect((props['urgency'] as Map)['enum'],
          Urgency.values.map((e) => e.wireName).toList());
      expect((props['team'] as Map)['enum'],
          Team.values.map((e) => e.wireName).toList());
    });

    test('web_search is the current, domain-scoped server-tool variant', () {
      final ws = webSearchTool();
      expect(ws['type'], 'web_search_20260209');
      expect(ws['name'], 'web_search');
      // Fenced to specific sources so it can't roam the open web.
      expect(ws['allowed_domains'], isNotEmpty);
      expect(ws['max_uses'], isNotNull);
    });

    test('github MCP server + toolset are well-formed and name-matched', () {
      final server = githubMcpServer(authorizationToken: 'ghp_x');
      expect(server['type'], 'url');
      expect(server['url'], githubMcpUrl);
      expect(server['authorization_token'], 'ghp_x');

      final toolset = githubIssueToolset();
      expect(toolset['type'], 'mcp_toolset');
      // The toolset must reference the server by the same name, or the API 400s.
      expect(toolset['mcp_server_name'], server['name']);
      // Allowlisted: default off, issue tools explicitly enabled.
      expect((toolset['default_config'] as Map)['enabled'], isFalse);
      final enabled = (toolset['configs'] as List)
          .where((c) => c['enabled'] == true)
          .map((c) => c['name']);
      expect(enabled, containsAll(['search_issues', 'create_issue']));
    });
  });

  group('TriageResult parsing', () {
    test('round-trips valid JSON', () {
      final result = TriageResult.fromJson(ticketInput());
      expect(result.urgency, Urgency.high);
      expect(result.topic, Topic.billing);
      expect(result.team, Team.billing);
      expect(result.needsHumanReview, isFalse);
    });

    test('throws on an off-spec enum value', () {
      expect(
        () => TriageResult.fromJson(ticketInput(urgency: 'catastrophic')),
        throwsA(isA<FormatException>()),
      );
    });

    test('throws on out-of-range confidence', () {
      final bad = ticketInput()..['confidence'] = 1.5;
      expect(() => TriageResult.fromJson(bad), throwsA(isA<FormatException>()));
    });
  });

  group('ModelPolicy.shouldEscalate', () {
    TriageResult r({
      Urgency urgency = Urgency.normal,
      Topic topic = Topic.billing,
      Team team = Team.billing,
      double confidence = 0.9,
      bool review = false,
    }) =>
        TriageResult(
          urgency: urgency,
          topic: topic,
          team: team,
          summary: 's',
          rationale: 'r',
          confidence: confidence,
          needsHumanReview: review,
        );

    test('confident, low-stakes result is NOT escalated', () {
      expect(ModelPolicy.shouldEscalate(r()), isFalse);
    });

    test('low confidence escalates', () {
      expect(ModelPolicy.shouldEscalate(r(confidence: 0.5)), isTrue);
    });

    test('urgent escalates', () {
      expect(ModelPolicy.shouldEscalate(r(urgency: Urgency.urgent)), isTrue);
    });

    test('sensitive team escalates', () {
      expect(
          ModelPolicy.shouldEscalate(r(team: Team.trustAndSafety)), isTrue);
    });
  });

  group('TriageService loop (scripted Claude)', () {
    test('drives tools then create_ticket, no escalation', () async {
      // Claude: (1) look up customer + fetch order, (2) create the ticket.
      final api = FakeMessagesApi([
        assistantToolTurn([
          toolUse('tu1', 'look_up_customer', {'email': 'jane@example.com'}),
          toolUse('tu2', 'fetch_order', {'order_id': 'ORD-1002'}),
        ]),
        assistantToolTurn([
          toolUse('tu3', 'create_ticket', ticketInput()),
        ]),
      ]);
      final exec = MockToolExecutor();
      final service = TriageService(api: api, executor: exec);

      final outcome = await service.triage('duplicate charge, jane@example.com');

      // The loop executed all three tools in order.
      expect(outcome.toolCalls.map((c) => c.name),
          ['look_up_customer', 'fetch_order', 'create_ticket']);
      // The customer lookup fed back real data.
      expect(outcome.toolCalls.first.resultContent, contains('CUST-778'));
      // A ticket was created and its id captured.
      expect(outcome.ticketId, startsWith('TICK-'));
      expect(exec.createdTickets, hasLength(1));
      // Confident, low-stakes -> stayed on the cheap tier, two API calls total.
      expect(outcome.escalated, isFalse);
      expect(outcome.tierUsed, ModelTier.fastClassifier);
      expect(api.requests, hasLength(2));
      expect(api.requests.every((r) => r['model'] == 'claude-haiku-4-5'), isTrue);
    });

    test('escalates a high-stakes result to the capable tier', () async {
      // First pass (Haiku) tickets it as urgent -> shouldEscalate -> second
      // pass (Opus) re-runs the same loop.
      final api = FakeMessagesApi([
        // Pass 1 — Haiku
        assistantToolTurn([toolUse('a', 'create_ticket',
            ticketInput(urgency: 'urgent', topic: 'technical', team: 'engineering'))]),
        // Pass 2 — Opus
        assistantToolTurn([toolUse('b', 'create_ticket',
            ticketInput(urgency: 'urgent', topic: 'technical', team: 'engineering'))]),
      ]);
      final service = TriageService(api: api, executor: MockToolExecutor());

      final outcome = await service.triage('everything is down!');

      expect(outcome.escalated, isTrue);
      expect(outcome.tierUsed, ModelTier.capableReviewer);
      expect(api.requests, hasLength(2));
      expect(api.requests[0]['model'], 'claude-haiku-4-5');
      expect(api.requests[1]['model'], 'claude-opus-4-8');
    });

    test('attaches web_search only to the escalation tier', () async {
      // Same high-stakes result on both passes, so the loop escalates Haiku ->
      // Opus and we can inspect the tools sent to each tier.
      final api = FakeMessagesApi([
        assistantToolTurn([
          toolUse('a', 'create_ticket',
              ticketInput(urgency: 'urgent', topic: 'technical', team: 'engineering'))
        ]),
        assistantToolTurn([
          toolUse('b', 'create_ticket',
              ticketInput(urgency: 'urgent', topic: 'technical', team: 'engineering'))
        ]),
      ]);
      final service = TriageService(api: api, executor: MockToolExecutor());

      await service.triage('the whole platform is down');

      bool hasWebSearch(Map<String, dynamic> req) =>
          (req['tools'] as List).any((t) => t['name'] == 'web_search');
      // Fast first pass (Haiku): no web search — lean and deterministic.
      expect(api.requests[0]['model'], 'claude-haiku-4-5');
      expect(hasWebSearch(api.requests[0]), isFalse);
      // Capable reviewer (Opus): web search attached for the hard cases.
      expect(api.requests[1]['model'], 'claude-opus-4-8');
      expect(hasWebSearch(api.requests[1]), isTrue);
    });

    test('resumes on pause_turn instead of forcing a ticket', () async {
      // The model runs web_search and the server-side loop pauses. The service
      // must re-send to let it continue — not read the empty client-tool set as
      // "ended without routing" and force create_ticket.
      final api = FakeMessagesApi([
        assistantPauseTurn([
          {'type': 'text', 'text': 'Checking the status page.'},
          serverToolUse('sw1', 'web_search', {'query': 'platform status'}),
        ]),
        assistantToolTurn([toolUse('t', 'create_ticket', ticketInput())]),
      ]);
      final service = TriageService(api: api, executor: MockToolExecutor());

      final outcome = await service.triage('is the API down for everyone?');

      expect(outcome.ticketId, isNotNull);
      expect(api.requests, hasLength(2));
      // The resume kept tool_choice on auto — it did NOT take the forced-ticket
      // path (which would set {'type': 'tool', 'name': 'create_ticket'}).
      expect(api.requests[1]['tool_choice'], {'type': 'auto'});
      // The paused assistant turn (with its server_tool_use) was carried into
      // the resend so the server can continue where it left off.
      final resendMessages =
          api.requests[1]['messages'] as List<Map<String, dynamic>>;
      expect(resendMessages.last['role'], 'assistant');
    });

    test('files a GitHub issue via MCP and captures it in the outcome',
        () async {
      // End-to-end (network scripted): Claude searches existing issues, finds
      // none, creates one via the GitHub MCP server, then routes the ticket.
      const repo = 'sarowar90/TODO-MCP-Connected-Triager';
      const issueUrl =
          'https://github.com/$repo/issues/42';
      final api = FakeMessagesApi([
        // Turn 1 — MCP: search (no match) then create_issue. Server-executed,
        // so it comes back in one turn and the server pauses (pause_turn).
        assistantPauseTurn([
          {'type': 'text', 'text': 'Checking for a duplicate issue.'},
          mcpToolUse('m1', 'search_issues', {'query': 'CSV export 500'}),
          mcpToolResult('m1', '{"total_count":0,"items":[]}'),
          mcpToolUse('m2', 'create_issue', {
            'owner': 'sarowar90',
            'repo': 'TODO-MCP-Connected-Triager',
            'title': 'CSV export returns 500',
            'body': 'Export button 500s from /api/reports/export.',
          }),
          mcpToolResult('m2',
              '{"number":42,"html_url":"$issueUrl","state":"open"}'),
        ]),
        // Turn 2 — route the ticket, referencing the new issue.
        assistantToolTurn([
          toolUse('t', 'create_ticket',
              ticketInput(topic: 'technical', team: 'engineering')),
        ]),
      ]);
      final service = TriageService(
        api: api,
        executor: MockToolExecutor(),
        mcpServers: [githubMcpServer(authorizationToken: 'ghp_x')],
        mcpToolsets: [githubIssueToolset()],
        mcpGuidance: githubIssueGuidance(repo),
      );

      final outcome =
          await service.triage('CSV export button 500s, blocks reporting');

      // The GitHub MCP calls were recorded in the trace, results and all.
      final names = outcome.toolCalls.map((c) => c.name).toList();
      expect(names, containsAll(['github/search_issues', 'github/create_issue']));
      final createCall =
          outcome.toolCalls.firstWhere((c) => c.name == 'github/create_issue');
      // The captured end-to-end result: the created issue.
      expect(createCall.resultContent, contains(issueUrl));
      expect(createCall.input['title'], 'CSV export returns 500');
      expect(createCall.isError, isFalse);

      // A ticket was still routed after the issue was filed.
      expect(outcome.ticketId, startsWith('TICK-'));

      // The request actually carried the MCP connector: server + toolset.
      expect(api.requests.first['mcp_servers'], isNotNull);
      expect(
          (api.requests.first['mcp_servers'] as List).first['name'], 'github');
      expect(
          (api.requests.first['tools'] as List)
              .any((t) => t['type'] == 'mcp_toolset'),
          isTrue);
      // Resumed past pause_turn rather than forcing the ticket.
      expect(api.requests[1]['tool_choice'], {'type': 'auto'});
    });

    test('runs independent tools in parallel, combining results into one turn',
        () async {
      // Claude requests three independent lookups in a single turn, then
      // create_ticket. The three should run concurrently and come back as one
      // combined user message.
      final api = FakeMessagesApi([
        assistantToolTurn([
          toolUse('a', 'look_up_customer', {'email': 'jane@example.com'}),
          toolUse('b', 'fetch_order', {'order_id': 'ORD-1002'}),
          toolUse('c', 'fetch_recent_tickets', {'email': 'jane@example.com'}),
        ]),
        assistantToolTurn([toolUse('d', 'create_ticket', ticketInput())]),
      ]);
      final tracking = ConcurrencyTrackingExecutor(MockToolExecutor());
      final service = TriageService(api: api, executor: tracking);

      final outcome = await service.triage('duplicate charge, jane@example.com');

      // 1. The three independent calls actually ran at the same time.
      expect(tracking.maxConcurrent, 3,
          reason: 'independent tools should execute concurrently');

      // 2. All three were executed and traced.
      expect(outcome.toolCalls.map((c) => c.name).take(3),
          ['look_up_customer', 'fetch_order', 'fetch_recent_tickets']);

      // 3. Their results were returned in a SINGLE user message with matching
      //    tool_use_ids — the shape the API requires for parallel tool use.
      final secondRequestMessages =
          api.requests[1]['messages'] as List<Map<String, dynamic>>;
      final toolResultTurn = secondRequestMessages.last;
      expect(toolResultTurn['role'], 'user');
      final blocks = toolResultTurn['content'] as List;
      expect(blocks, hasLength(3));
      expect(blocks.every((b) => b['type'] == 'tool_result'), isTrue);
      expect(blocks.map((b) => b['tool_use_id']), containsAll(['a', 'b', 'c']));

      // 4. The combined context flowed through correctly.
      final recentTicketsResult = outcome.toolCalls
          .firstWhere((c) => c.name == 'fetch_recent_tickets')
          .resultContent;
      expect(recentTicketsResult, contains('TICK-4820'));
    });

    test('forces create_ticket if the model ends without routing', () async {
      // Claude answers in text (no tool). Service should nudge + force the tool.
      final api = FakeMessagesApi([
        assistantTextTurn('Sure, I can help with that.'),
        assistantToolTurn([toolUse('t', 'create_ticket', ticketInput())]),
      ]);
      final service = TriageService(api: api, executor: MockToolExecutor());

      final outcome = await service.triage('vague message');

      expect(outcome.ticketId, isNotNull);
      // The second request forced the terminal tool.
      expect(api.requests[1]['tool_choice'],
          {'type': 'tool', 'name': 'create_ticket'});
    });
  });

  group('streaming (SSE reconstruction)', () {
    test('streams text deltas in order and rebuilds the text block', () async {
      final body = sseBody([
        {'type': 'message_start', 'message': <String, dynamic>{}},
        {
          'type': 'content_block_start',
          'index': 0,
          'content_block': {'type': 'text', 'text': ''},
        },
        for (final piece in ['Let me ', 'check the ', 'account.'])
          {
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'text_delta', 'text': piece},
          },
        {'type': 'content_block_stop', 'index': 0},
        {
          'type': 'message_delta',
          'delta': {'stop_reason': 'end_turn'},
        },
        {'type': 'message_stop'},
      ]);
      final client =
          AnthropicClient(apiKey: 'k', httpClient: sseMockClient(body));

      final deltas = <String>[];
      final response = await client.createMessageStreaming(
        model: 'claude-haiku-4-5',
        maxTokens: 100,
        messages: [{'role': 'user', 'content': 'hi'}],
        onTextDelta: deltas.add,
      );

      // Deltas arrived incrementally, in order...
      expect(deltas, ['Let me ', 'check the ', 'account.']);
      // ...and the reconstructed message matches the non-streaming shape.
      final content = response['content'] as List;
      expect(content.single['type'], 'text');
      expect(content.single['text'], 'Let me check the account.');
      expect(response['stop_reason'], 'end_turn');
    });

    test('reassembles a fragmented tool_use input into valid JSON', () async {
      final ticketJson = jsonEncode(ticketInput());
      final started = <String>[];

      final body = sseBody([
        {'type': 'message_start', 'message': <String, dynamic>{}},
        {
          'type': 'content_block_start',
          'index': 0,
          'content_block': {
            'type': 'tool_use',
            'id': 'tu_1',
            'name': 'create_ticket',
            'input': <String, dynamic>{},
          },
        },
        // The tool's JSON input arrives split across many events.
        for (final frag in fragments(ticketJson, 5))
          {
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'input_json_delta', 'partial_json': frag},
          },
        {'type': 'content_block_stop', 'index': 0},
        {
          'type': 'message_delta',
          'delta': {'stop_reason': 'tool_use'},
        },
        {'type': 'message_stop'},
      ]);
      final client =
          AnthropicClient(apiKey: 'k', httpClient: sseMockClient(body));

      final response = await client.createMessageStreaming(
        model: 'claude-haiku-4-5',
        maxTokens: 100,
        messages: [{'role': 'user', 'content': 'hi'}],
        onToolUseStart: started.add,
      );

      expect(started, ['create_ticket']);
      expect(response['stop_reason'], 'tool_use');
      final block = (response['content'] as List).single as Map<String, dynamic>;
      expect(block['type'], 'tool_use');
      expect(block['name'], 'create_ticket');
      // The fragmented input reassembled into the exact map...
      final input = (block['input'] as Map).cast<String, dynamic>();
      expect(input, ticketInput());
      // ...and parses through the real code path without error.
      final result = TriageResult.fromJson(input);
      expect(result.topic, Topic.billing);
      expect(result.urgency, Urgency.high);
    });

    test('preserves block order for mixed text + tool_use', () async {
      final body = sseBody([
        {'type': 'message_start', 'message': <String, dynamic>{}},
        {
          'type': 'content_block_start',
          'index': 0,
          'content_block': {'type': 'text', 'text': ''},
        },
        {
          'type': 'content_block_delta',
          'index': 0,
          'delta': {'type': 'text_delta', 'text': 'Looking into it.'},
        },
        {'type': 'content_block_stop', 'index': 0},
        {
          'type': 'content_block_start',
          'index': 1,
          'content_block': {
            'type': 'tool_use',
            'id': 'tu_2',
            'name': 'fetch_order',
            'input': <String, dynamic>{},
          },
        },
        {
          'type': 'content_block_delta',
          'index': 1,
          'delta': {
            'type': 'input_json_delta',
            'partial_json': '{"order_id":"ORD-1002"}',
          },
        },
        {'type': 'content_block_stop', 'index': 1},
        {
          'type': 'message_delta',
          'delta': {'stop_reason': 'tool_use'},
        },
        {'type': 'message_stop'},
      ]);
      final client =
          AnthropicClient(apiKey: 'k', httpClient: sseMockClient(body));

      final response = await client.createMessageStreaming(
        model: 'claude-haiku-4-5',
        maxTokens: 100,
        messages: [{'role': 'user', 'content': 'hi'}],
      );

      final content = (response['content'] as List).cast<Map<String, dynamic>>();
      expect(content.map((b) => b['type']), ['text', 'tool_use']);
      expect(content[1]['input'], {'order_id': 'ORD-1002'});
    });

    test('throws AnthropicException on a non-200 stream response', () async {
      final body = jsonEncode({
        'type': 'error',
        'error': {'type': 'invalid_request_error', 'message': 'bad request'},
      });
      final client = AnthropicClient(
        apiKey: 'k',
        httpClient: sseMockClient(body, status: 400),
      );

      expect(
        () => client.createMessageStreaming(
          model: 'claude-haiku-4-5',
          maxTokens: 100,
          messages: [{'role': 'user', 'content': 'hi'}],
        ),
        throwsA(isA<AnthropicException>()
            .having((e) => e.statusCode, 'statusCode', 400)),
      );
    });

    test('service forwards streamed narration to the callback', () async {
      // Scripted Claude narrates before routing; the service should surface that
      // text through onTextDelta (fake uses the default streaming path).
      final api = FakeMessagesApi([
        assistantToolTurn([
          {'type': 'text', 'text': 'Checking the account first.'},
          toolUse('t', 'create_ticket', ticketInput()),
        ]),
      ]);
      final service = TriageService(api: api, executor: MockToolExecutor());

      final narration = StringBuffer();
      final toolsStarted = <String>[];
      final outcome = await service.triage(
        'duplicate charge',
        onTextDelta: narration.write,
        onToolUseStart: toolsStarted.add,
      );

      expect(narration.toString(), 'Checking the account first.');
      expect(toolsStarted, contains('create_ticket'));
      expect(outcome.ticketId, isNotNull);
    });
  });

  group('MCP connector (wire format)', () {
    test('createMessage sends mcp_servers and the beta header', () async {
      late http.Request captured;
      final mock = MockClient((request) async {
        captured = request;
        return http.Response(
          jsonEncode({
            'content': [
              {'type': 'text', 'text': 'ok'}
            ],
            'stop_reason': 'end_turn',
          }),
          200,
          headers: {'content-type': 'application/json'},
        );
      });
      final client = AnthropicClient(apiKey: 'k', httpClient: mock);

      await client.createMessage(
        model: 'claude-opus-4-8',
        maxTokens: 100,
        messages: [{'role': 'user', 'content': 'hi'}],
        tools: [githubIssueToolset()],
        mcpServers: [githubMcpServer(authorizationToken: 'ghp_x')],
      );

      // Beta opt-in only appears when the connector is used.
      expect(captured.headers['anthropic-beta'], 'mcp-client-2025-11-20');
      final body = jsonDecode(captured.body) as Map<String, dynamic>;
      expect((body['mcp_servers'] as List).first['url'], githubMcpUrl);
    });

    test('omits mcp_servers and the beta header when none configured', () async {
      late http.Request captured;
      final mock = MockClient((request) async {
        captured = request;
        return http.Response(
          jsonEncode({'content': <dynamic>[], 'stop_reason': 'end_turn'}),
          200,
        );
      });
      final client = AnthropicClient(apiKey: 'k', httpClient: mock);

      await client.createMessage(
        model: 'claude-haiku-4-5',
        maxTokens: 100,
        messages: [{'role': 'user', 'content': 'hi'}],
      );

      expect(captured.headers.containsKey('anthropic-beta'), isFalse);
      expect((jsonDecode(captured.body) as Map).containsKey('mcp_servers'),
          isFalse);
    });

    test('github guidance is threaded into the system prompt', () {
      final prompt = buildTriageSystemPrompt(
        extraGuidance: githubIssueGuidance('acme/support'),
      );
      expect(prompt, contains('acme/support'));
      expect(prompt, contains('github tools'));
    });
  });

  group('custom MCP tool (SLA policy)', () {
    test('resolves a policy for every urgency, keyed to the taxonomy', () {
      for (final u in Urgency.values) {
        final policy = slaPolicyFor(u.wireName);
        expect(policy['urgency'], u.wireName);
        expect(policy['first_response_target'], u.slaTarget);
        expect(policy['pages_on_call'], isA<bool>());
        expect(policy['escalation'], isNotEmpty);
      }
    });

    test('only urgent pages on-call', () {
      expect(slaPolicyFor('urgent')['pages_on_call'], isTrue);
      for (final u in ['high', 'normal', 'low']) {
        expect(slaPolicyFor(u)['pages_on_call'], isFalse);
      }
    });

    test('throws on an off-taxonomy urgency', () {
      expect(() => slaPolicyFor('catastrophic'), throwsArgumentError);
    });

    test('tool schema is MCP-shaped and enum matches Urgency', () {
      final schema = slaPolicyToolSchema();
      expect(schema['name'], 'get_sla_policy');
      // MCP uses inputSchema (camelCase), not the Messages API's input_schema.
      expect(schema.containsKey('inputSchema'), isTrue);
      final props = (schema['inputSchema'] as Map)['properties'] as Map;
      expect((props['urgency'] as Map)['enum'],
          Urgency.values.map((u) => u.wireName).toList());
      expect((schema['inputSchema'] as Map)['additionalProperties'], isFalse);
    });
  });

  group('Slack routing', () {
    test('resolves a channel for every team, with override + fallback', () {
      // Total over the taxonomy — routing always has a destination.
      for (final t in Team.values) {
        expect(slackChannelFor(t), isNotEmpty);
      }
      expect(slackChannelFor(Team.retention), '#cx-retention');
      expect(slackChannelFor(Team.trustAndSafety), '#security-escalations');
      // Custom map wins; a team the custom map omits falls back to triage.
      expect(slackChannelFor(Team.billing, {Team.billing: '#money'}), '#money');
      expect(
          slackChannelFor(Team.engineering, {Team.triageReview: '#tri'}),
          '#tri');
    });

    test('slack MCP server + toolset are well-formed and name-matched', () {
      final server = slackMcpServer(authorizationToken: 'xoxb-x');
      expect(server['type'], 'url');
      expect(server['url'], slackMcpUrl);
      expect(server['authorization_token'], 'xoxb-x');

      final toolset = slackToolset();
      expect(toolset['type'], 'mcp_toolset');
      expect(toolset['mcp_server_name'], server['name']);
      expect((toolset['default_config'] as Map)['enabled'], isFalse);
      expect((toolset['configs'] as List).map((c) => c['name']),
          contains('slack_post_message'));
    });

    test('routes a churn outcome to the correct channel via Slack MCP',
        () async {
      // The router picks #cx-retention from the team; the model posts there.
      final api = FakeMessagesApi([
        slackPostTurn('#cx-retention', '{"ok":true,"ts":"1700.0001"}'),
      ]);
      final router = SlackRouter(api: api, authorizationToken: 'xoxb-x');

      final delivery = await router.route(outcomeFor(Team.retention));

      expect(delivery.posted, isTrue);
      expect(delivery.channel, '#cx-retention');
      expect(delivery.reachedCorrectDestination, isTrue);
      expect(delivery.resultContent, contains('"ok":true'));

      // The request actually carried the Slack connector...
      expect((api.requests.first['mcp_servers'] as List).first['name'], 'slack');
      // ...and the instruction named the deterministic channel, not a guess.
      final firstUserMsg = (api.requests.first['messages'] as List).first;
      expect(firstUserMsg['content'].toString(), contains('#cx-retention'));
    });

    test('flags a post that landed in the wrong channel', () async {
      // Model posts somewhere other than the routed channel — the delivery
      // records posted:true but reachedCorrectDestination:false.
      final api = FakeMessagesApi([
        slackPostTurn('#random-chatter', '{"ok":true,"ts":"1700.0002"}'),
      ]);
      final router = SlackRouter(api: api, authorizationToken: 'xoxb-x');

      final delivery = await router.route(outcomeFor(Team.retention));

      expect(delivery.posted, isTrue);
      expect(delivery.channel, '#cx-retention');
      expect(delivery.toolInput['channel'], '#random-chatter');
      expect(delivery.reachedCorrectDestination, isFalse);
    });

    test('resumes past pause_turn before the post lands', () async {
      final api = FakeMessagesApi([
        // Server tool paused mid-work, no post yet.
        {
          'stop_reason': 'pause_turn',
          'content': [
            {'type': 'text', 'text': 'Looking up the channel.'},
          ],
        },
        slackPostTurn('#security-escalations', '{"ok":true,"ts":"1700.0003"}'),
      ]);
      final router = SlackRouter(api: api, authorizationToken: 'xoxb-x');

      final delivery = await router.route(outcomeFor(Team.trustAndSafety));

      expect(delivery.reachedCorrectDestination, isTrue);
      expect(delivery.channel, '#security-escalations');
      expect(api.requests, hasLength(2));
    });
  });
}

import 'dart:convert';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:todo_app/triage/anthropic_client.dart';
import 'package:todo_app/triage/model_policy.dart';
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
  }) async {
    // Snapshot the messages list — the service keeps mutating the same
    // instance across the loop, so store a copy to inspect per-request state.
    requests.add({
      'model': model,
      'messages': List<Map<String, dynamic>>.from(messages),
      'tool_choice': toolChoice,
      'tools': tools,
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
}

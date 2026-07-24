// Step 4 — The triage service.
//
// Orchestrates the whole thing: builds the system prompt from the triage rules,
// runs the tool-use loop against the Messages API, and applies the Step 3 model
// tiering (cheap first pass, escalate the hard cases). Depends on the
// [MessagesApi] interface, so it runs identically against the real client and a
// scripted fake.

// Named constructor params can't be private, so they're mapped to private
// fields in the initializer list — which trips this lint spuriously.
// ignore_for_file: prefer_initializing_formals

import 'dart:convert';

import 'anthropic_client.dart';
import 'model_policy.dart';
import 'tools.dart';
import 'triage_taxonomy.dart';

/// One tool invocation the model made, with the result we returned — the
/// audit trail that makes a triage decision debuggable.
class ToolCall {
  const ToolCall({
    required this.name,
    required this.input,
    required this.resultContent,
    required this.isError,
  });

  final String name;
  final Map<String, dynamic> input;
  final String resultContent;
  final bool isError;

  @override
  String toString() => '$name(${jsonEncode(input)}) -> $resultContent';
}

/// The full outcome of triaging one message.
class TriageOutcome {
  const TriageOutcome({
    required this.result,
    required this.tierUsed,
    required this.escalated,
    required this.toolCalls,
    required this.ticketId,
  });

  final TriageResult result;

  /// The tier that produced [result] (the escalation tier if [escalated]).
  final ModelTier tierUsed;

  /// True if the cheap first pass was re-run on the capable tier.
  final bool escalated;

  /// Every tool the model called, in order.
  final List<ToolCall> toolCalls;

  /// The id of the ticket created by `create_ticket`.
  final String? ticketId;
}

/// Runs the triage workflow.
class TriageService {
  TriageService({
    required MessagesApi api,
    required ToolExecutor executor,
    int maxIterations = 6,
  })  : _api = api,
        _executor = executor,
        _maxIterations = maxIterations;

  final MessagesApi _api;
  final ToolExecutor _executor;
  final int _maxIterations;

  /// Triage a single support message: run the cheap tier, and re-run on the
  /// capable tier if [ModelPolicy.shouldEscalate] flags the result.
  ///
  /// Pass [onTextDelta] / [onToolUseStart] to stream the model's output live
  /// (e.g. to a support agent's screen). When neither is given, the plain
  /// non-streaming path is used.
  Future<TriageOutcome> triage(
    String supportMessage, {
    void Function(String textDelta)? onTextDelta,
    void Function(String toolName)? onToolUseStart,
  }) async {
    final firstPass = await _runOnce(
      ModelPolicy.firstPass,
      supportMessage,
      onTextDelta: onTextDelta,
      onToolUseStart: onToolUseStart,
    );

    if (ModelPolicy.shouldEscalate(firstPass.result)) {
      final escalated = await _runOnce(
        ModelPolicy.escalation,
        supportMessage,
        onTextDelta: onTextDelta,
        onToolUseStart: onToolUseStart,
      );
      return TriageOutcome(
        result: escalated.result,
        tierUsed: escalated.tierUsed,
        escalated: true,
        toolCalls: escalated.toolCalls,
        ticketId: escalated.ticketId,
      );
    }
    return firstPass;
  }

  /// Runs the tool-use loop once on a given tier and returns its outcome.
  Future<TriageOutcome> _runOnce(
    ModelTier tier,
    String supportMessage, {
    void Function(String textDelta)? onTextDelta,
    void Function(String toolName)? onToolUseStart,
  }) async {
    final streaming = onTextDelta != null || onToolUseStart != null;
    final tools = triageTools();
    final system = buildTriageSystemPrompt();
    final messages = <Map<String, dynamic>>[
      {'role': 'user', 'content': supportMessage},
    ];
    final trace = <ToolCall>[];

    // Start letting the model choose tools; if it ever ends without creating a
    // ticket, force the terminal tool so we always get a structured result.
    Map<String, dynamic> toolChoice = {'type': 'auto'};
    var forcedTicket = false;

    for (var i = 0; i < _maxIterations; i++) {
      final response = streaming
          ? await _api.createMessageStreaming(
              model: tier.modelId,
              maxTokens: 1024,
              system: system,
              messages: messages,
              tools: tools,
              toolChoice: toolChoice,
              onTextDelta: onTextDelta,
              onToolUseStart: onToolUseStart,
            )
          : await _api.createMessage(
              model: tier.modelId,
              maxTokens: 1024,
              system: system,
              messages: messages,
              tools: tools,
              toolChoice: toolChoice,
            );

      final content = (response['content'] as List).cast<Map<String, dynamic>>();
      messages.add({'role': 'assistant', 'content': content});

      final toolUses =
          content.where((b) => b['type'] == 'tool_use').toList();

      if (toolUses.isEmpty) {
        // The model answered in text without routing. Nudge it once to finish
        // by calling create_ticket, and force that tool on the next turn.
        if (forcedTicket) {
          throw StateError(
            'Model did not call create_ticket even when forced.',
          );
        }
        forcedTicket = true;
        toolChoice = {'type': 'tool', 'name': 'create_ticket'};
        messages.add({
          'role': 'user',
          'content':
              'Finish by calling create_ticket with your classification.',
        });
        continue;
      }

      // Validate any create_ticket args before running side effects, so an
      // off-spec ticket is a hard error rather than a silently-created one.
      for (final toolUse in toolUses) {
        if (toolUse['name'] == 'create_ticket') {
          TriageResult.fromJson((toolUse['input'] as Map).cast<String, dynamic>());
        }
      }

      // A single assistant turn can contain several independent tool_use blocks.
      // Execute them concurrently — parallel lookups finish in one round instead
      // of one-after-another — then return all results in ONE user message
      // (splitting them across messages trains the model out of parallel calls).
      final results = await Future.wait([
        for (final toolUse in toolUses)
          _executor.execute(
            toolUse['name'] as String,
            (toolUse['input'] as Map).cast<String, dynamic>(),
          ),
      ]);

      final toolResults = <Map<String, dynamic>>[];
      TriageResult? finalResult;
      String? ticketId;

      for (var j = 0; j < toolUses.length; j++) {
        final toolUse = toolUses[j];
        final result = results[j];
        final name = toolUse['name'] as String;
        final id = toolUse['id'] as String;
        final input = (toolUse['input'] as Map).cast<String, dynamic>();

        if (name == 'create_ticket') {
          finalResult = TriageResult.fromJson(input);
          ticketId = _extractTicketId(result.content);
        }

        trace.add(ToolCall(
          name: name,
          input: input,
          resultContent: result.content,
          isError: result.isError,
        ));

        toolResults.add({
          'type': 'tool_result',
          'tool_use_id': id,
          'content': result.content,
          if (result.isError) 'is_error': true,
        });
      }

      if (finalResult != null) {
        return TriageOutcome(
          result: finalResult,
          tierUsed: tier,
          escalated: false,
          toolCalls: trace,
          ticketId: ticketId,
        );
      }

      // Feed the tool results back and let the model continue.
      messages.add({'role': 'user', 'content': toolResults});
      toolChoice = {'type': 'auto'};
    }

    throw StateError(
      'Triage did not produce a ticket within $_maxIterations iterations.',
    );
  }

  static String? _extractTicketId(String toolResultContent) {
    try {
      final decoded = jsonDecode(toolResultContent) as Map<String, dynamic>;
      return decoded['ticket_id'] as String?;
    } catch (_) {
      return null;
    }
  }
}

/// Builds the system prompt from the triage rules. The allowed values are
/// pulled from the taxonomy enums so the prompt can't drift out of sync with
/// the code; the precedence rules mirror `triage_spec.md`.
String buildTriageSystemPrompt() {
  String wire<T extends Enum>(List<T> values) =>
      values.map((v) => (v as dynamic).wireName as String).join(', ');

  return '''
You are a customer-support triage agent. For each customer message: gather any
context you need with the tools, then classify the message and route it by
calling create_ticket exactly once, as your final action.

Tools:
- look_up_customer(email): the customer's plan, tier, and account status. Use it
  for billing or account issues when an email is present.
- fetch_order(order_id): the status and amount of an order. Use it when the
  message references an order, charge, or purchase.
- fetch_recent_tickets(email): the customer's recent tickets, for context on
  repeat issues.
- create_ticket(...): your FINAL action. Submit the classification and route the
  ticket. Call it exactly once, last.

When you need several independent pieces of context — for example the customer
record and their recent tickets — request them together in a single turn so they
run in parallel instead of one after another. Gather everything you need, then
call create_ticket once.

Classify along three axes. Use ONLY these values.
- urgency (${wire(Urgency.values)}) — impact and time-sensitivity only,
  independent of topic:
    urgent = active outage, security breach or data exposure, payments fully
      broken, or a legal/compliance threat (pages on-call).
    high   = core workflow blocked with no workaround, money actively at stake,
      or a credible cancellation threat.
    normal = a standard question or a minor bug that has a workaround (default).
    low    = non-blocking feedback or a minor suggestion.
- topic (${wire(Topic.values)}) — the primary subject. If the product is
  misbehaving it's technical; if it works and the user just needs guidance it's
  how_to. A frustrated tone alone is not a complaint — use complaint only when
  dissatisfaction, escalation, or cancellation is the point of the message.
- team (${wire(Team.values)}) — the owning team.

Routing precedence — apply IN ORDER; the first matching rule wins for the field
it sets, and later rules still fill fields an earlier rule left unset:
1. Safety first: a credible report of account compromise, unauthorized access,
   or exposed/leaked data -> team = trust_and_safety and urgency >= high
   (urgent if the access/exposure is ongoing), regardless of topic.
2. Churn: intent to cancel or a credible threat to leave -> team = retention and
   urgency >= high.
3. Outage: something actively down or payments fully broken -> urgency = urgent
   (team follows the topic default).
4. Otherwise: team follows the topic — billing->billing, technical->engineering,
   account/how_to/feature_request/complaint->customer_success, other->triage_review.
5. Multiple issues: triage the single highest-urgency actionable issue; mention
   any others in the summary.

Confidence: report your certainty from 0.0 to 1.0. If the message is unclear, you
cannot confidently pick a topic, or your confidence is below 0.60, set
needs_human_review = true and team = triage_review.

When you call create_ticket, provide: urgency, topic, team, a one-sentence
summary, a rationale that names the precedence rule when one applied, confidence,
needs_human_review, and customer_id / order_id if you looked them up (else null).
''';
}

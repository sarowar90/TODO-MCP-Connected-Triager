// Step 4 — Tool definitions + a mock backend.
//
// The three business actions the triager can take, defined as strict-schema
// tool definitions the model calls, plus a [MockToolExecutor] that stands in
// for real systems (CRM, order DB, ticketing) so the loop is runnable offline.
//
//   • look_up_customer(email)  — read customer context
//   • fetch_order(order_id)    — read order context
//   • create_ticket(...)       — the terminal action; carries the triage result
//
// `create_ticket` is deliberately the last step: its arguments ARE the
// TriageResult, so routing the ticket and recording the classification are one
// action.

import 'dart:convert';

import 'triage_taxonomy.dart';

/// The tool definitions sent on every request, in the API's `tools` shape.
///
/// `strict: true` guarantees the model's tool inputs validate exactly against
/// the schema — essential for `create_ticket`, whose args we parse straight
/// into a [TriageResult].
List<Map<String, dynamic>> triageTools() {
  List<String> wire<T extends Enum>(List<T> values) =>
      values.map((v) => (v as dynamic).wireName as String).toList();

  return [
    {
      'name': 'look_up_customer',
      'description':
          'Look up a customer account by email address. Returns their plan, '
              'tier, and account status. Use this for billing or account issues '
              'when the message includes an email.',
      'strict': true,
      'input_schema': {
        'type': 'object',
        'properties': {
          'email': {
            'type': 'string',
            'description': "The customer's email address.",
          },
        },
        'required': ['email'],
        'additionalProperties': false,
      },
    },
    {
      'name': 'fetch_order',
      'description':
          'Fetch the status and details of an order by its ID. Use this when a '
              'message references an order, charge, or purchase.',
      'strict': true,
      'input_schema': {
        'type': 'object',
        'properties': {
          'order_id': {
            'type': 'string',
            'description': 'The order identifier, e.g. ORD-1002.',
          },
        },
        'required': ['order_id'],
        'additionalProperties': false,
      },
    },
    {
      'name': 'fetch_recent_tickets',
      'description':
          "List the customer's recent support tickets by email. This is "
              'independent of look_up_customer — when you need both, request them '
              'in the same turn so they run in parallel.',
      'strict': true,
      'input_schema': {
        'type': 'object',
        'properties': {
          'email': {
            'type': 'string',
            'description': "The customer's email address.",
          },
        },
        'required': ['email'],
        'additionalProperties': false,
      },
    },
    {
      'name': 'create_ticket',
      'description':
          'Create the support ticket with the final triage classification and '
              'route it to the owning team. This is your FINAL action — call it '
              'exactly once, last, after gathering any context you need. All '
              'fields must follow the triage specification.',
      'strict': true,
      'input_schema': {
        'type': 'object',
        'properties': {
          'urgency': {'type': 'string', 'enum': wire(Urgency.values)},
          'topic': {'type': 'string', 'enum': wire(Topic.values)},
          'team': {'type': 'string', 'enum': wire(Team.values)},
          'summary': {
            'type': 'string',
            'description': "One sentence restating the customer's core issue.",
          },
          'rationale': {
            'type': 'string',
            'description':
                'Why this urgency/topic/team — cite the precedence rule if one '
                    'applied.',
          },
          'confidence': {
            'type': 'number',
            'description': 'Your certainty, 0.0 to 1.0.',
          },
          'needs_human_review': {'type': 'boolean'},
          'customer_id': {
            'type': ['string', 'null'],
            'description': 'The customer id if you looked one up, else null.',
          },
          'order_id': {
            'type': ['string', 'null'],
            'description': 'The order id if you looked one up, else null.',
          },
        },
        'required': [
          'urgency',
          'topic',
          'team',
          'summary',
          'rationale',
          'confidence',
          'needs_human_review',
          'customer_id',
          'order_id',
        ],
        'additionalProperties': false,
      },
    },
  ];
}

/// The result of running a tool: a string payload fed back to the model as a
/// `tool_result`, plus whether it represents an error.
class ToolResult {
  const ToolResult(this.content, {this.isError = false});

  final String content;
  final bool isError;
}

/// Executes a tool call and returns the result to feed back to the model.
abstract interface class ToolExecutor {
  Future<ToolResult> execute(String name, Map<String, dynamic> input);
}

/// In-memory stand-in for real CRM / order / ticketing systems. Deterministic,
/// so the loop can run and be tested without external dependencies.
class MockToolExecutor implements ToolExecutor {
  /// Tickets created during this run, in order — inspectable after triage.
  final List<Map<String, dynamic>> createdTickets = [];

  int _ticketCounter = 5000;

  static const _customers = <String, Map<String, dynamic>>{
    'jane@example.com': {
      'customer_id': 'CUST-778',
      'name': 'Jane Doe',
      'email': 'jane@example.com',
      'plan': 'Pro',
      'account_status': 'active',
      'customer_since': '2024-03-11',
    },
  };

  static const _orders = <String, Map<String, dynamic>>{
    'ORD-1002': {
      'order_id': 'ORD-1002',
      'status': 'paid',
      'amount': 120.0,
      'currency': 'USD',
      'created_at': '2026-07-01',
      'description': 'Pro plan — monthly subscription',
    },
  };

  static const _recentTickets = <String, List<Map<String, dynamic>>>{
    'jane@example.com': [
      {
        'ticket_id': 'TICK-4820',
        'topic': 'billing',
        'status': 'resolved',
        'created_at': '2026-06-14',
        'summary': 'Question about an invoice line item',
      },
      {
        'ticket_id': 'TICK-4655',
        'topic': 'how_to',
        'status': 'resolved',
        'created_at': '2026-05-02',
        'summary': 'How to export data to CSV',
      },
    ],
  };

  @override
  Future<ToolResult> execute(String name, Map<String, dynamic> input) async {
    switch (name) {
      case 'look_up_customer':
        final email = (input['email'] as String?)?.toLowerCase();
        final customer = _customers[email];
        return ToolResult(
          jsonEncode(customer ?? {'error': 'customer_not_found', 'email': email}),
        );

      case 'fetch_order':
        final order = _orders[input['order_id']];
        return ToolResult(
          jsonEncode(
            order ?? {'error': 'order_not_found', 'order_id': input['order_id']},
          ),
        );

      case 'fetch_recent_tickets':
        final email = (input['email'] as String?)?.toLowerCase();
        final tickets = _recentTickets[email] ?? const [];
        return ToolResult(jsonEncode({'tickets': tickets}));

      case 'create_ticket':
        final ticketId = 'TICK-${_ticketCounter++}';
        createdTickets.add({'ticket_id': ticketId, ...input});
        return ToolResult(
          jsonEncode({'ticket_id': ticketId, 'status': 'created'}),
        );

      default:
        return ToolResult(
          jsonEncode({'error': 'unknown_tool', 'name': name}),
          isError: true,
        );
    }
  }
}

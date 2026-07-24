// Step 2 — Triage taxonomy (the closed category sets).
//
// This file is the machine-readable half of the triage definition. The
// authoritative, human-readable definition — including the routing precedence
// rules and worked examples — lives alongside it in `triage_spec.md`. Keep the
// two in sync: every enum value here must have a matching entry in the spec.
//
// These are *closed* sets on purpose. A reliable triager must map every message
// to exactly one urgency, one topic, and one team — no free-form labels — so the
// output is always something the routing system can act on.

/// How quickly a message must be handled. Drives SLA and paging.
///
/// Urgency is about *impact and time-sensitivity*, independent of topic: a
/// billing question and a bug report can both be [urgent] or [low].
enum Urgency {
  /// P1 — Business-critical, needs a human now. Active outage, security breach
  /// or data exposure, payments completely broken, or a legal/compliance threat.
  /// Target first response: minutes; pages on-call.
  urgent('urgent', 'Urgent (P1)', 'minutes'),

  /// P2 — Seriously impaired but not on fire. A core workflow is blocked with no
  /// workaround, a billing error with money actively at stake, or a credible
  /// churn/cancellation threat. Target first response: within a few hours.
  high('high', 'High (P2)', 'hours'),

  /// P3 — Standard request. A question, a minor bug that has a workaround, or a
  /// routine account/billing task. The default when nothing escalates it.
  /// Target first response: 1–2 business days.
  normal('normal', 'Normal (P3)', '1–2 business days'),

  /// P4 — Non-blocking. General feedback, a "nice to have" request, or a thank-you.
  /// Target first response: best effort.
  low('low', 'Low (P4)', 'best effort');

  const Urgency(this.wireName, this.label, this.slaTarget);

  /// The exact string used in the API schema and prompts. Never change these —
  /// evals and routing are keyed to them.
  final String wireName;

  /// Human-readable name for dashboards and UI.
  final String label;

  /// Target first-response time, used to explain the level (not a hard promise).
  final String slaTarget;
}

/// What the message is fundamentally about. Pick the *primary* subject.
///
/// If a message spans several topics, choose the one tied to the highest-urgency
/// actionable issue (see the precedence rules in `triage_spec.md`).
enum Topic {
  /// Charges, invoices, refunds, subscriptions, pricing, payment methods.
  billing('billing', 'Billing & Payments'),

  /// Bugs, errors, crashes, outages, data problems, integration/API failures.
  technical('technical', 'Technical Issue'),

  /// Login, password reset, access/permissions, profile, plan changes, and
  /// account-security concerns (unauthorized access, suspected compromise).
  account('account', 'Account & Access'),

  /// Suggestions for new capabilities or enhancements to existing ones.
  featureRequest('feature_request', 'Feature Request'),

  /// "How do I…?" usage and guidance questions where the product works as designed.
  howTo('how_to', 'How-To / Guidance'),

  /// Expressed dissatisfaction, escalation, or intent to leave/cancel.
  complaint('complaint', 'Complaint / Escalation'),

  /// Doesn't fit any category above, is unintelligible, or is spam.
  other('other', 'Other / Unclear');

  const Topic(this.wireName, this.label);

  final String wireName;
  final String label;
}

/// Which team should own the message. Derived from topic plus the precedence
/// rules — see [Team.defaultForTopic] and `triage_spec.md`.
enum Team {
  /// Owns billing, invoices, refunds, and subscription changes.
  billing('billing', 'Billing Team'),

  /// Owns bugs, outages, and technical/integration failures.
  engineering('engineering', 'Engineering'),

  /// Owns onboarding, how-to guidance, feature requests, and general account help.
  customerSuccess('customer_success', 'Customer Success'),

  /// Owns account-compromise, unauthorized access, and data-exposure reports.
  trustAndSafety('trust_and_safety', 'Trust & Safety'),

  /// Owns churn risk — cancellation threats and at-risk-account saves.
  retention('retention', 'Retention'),

  /// Human triage queue for anything ambiguous, low-confidence, or [Topic.other].
  triageReview('triage_review', 'Human Triage Review');

  const Team(this.wireName, this.label);

  final String wireName;
  final String label;

  /// The default owning team for a topic, *before* the precedence rules in
  /// `triage_spec.md` (safety, retention, low-confidence) are applied.
  ///
  /// Precedence rules can override this — e.g. an [Topic.account] message that
  /// reports a compromise routes to [trustAndSafety], not [customerSuccess].
  static Team defaultForTopic(Topic topic) {
    switch (topic) {
      case Topic.billing:
        return Team.billing;
      case Topic.technical:
        return Team.engineering;
      case Topic.account:
        return Team.customerSuccess;
      case Topic.featureRequest:
        return Team.customerSuccess;
      case Topic.howTo:
        return Team.customerSuccess;
      case Topic.complaint:
        return Team.customerSuccess;
      case Topic.other:
        return Team.triageReview;
    }
  }
}

/// The structured result of triaging one support message.
///
/// This is the exact shape Step 3 will ask the model to produce (and the shape
/// evals in Step 4 will grade). Every field is required so a result is always
/// actionable.
class TriageResult {
  const TriageResult({
    required this.urgency,
    required this.topic,
    required this.team,
    required this.summary,
    required this.rationale,
    required this.confidence,
    required this.needsHumanReview,
  });

  final Urgency urgency;
  final Topic topic;
  final Team team;

  /// One-sentence restatement of the customer's core issue.
  final String summary;

  /// Why this urgency/topic/team was chosen — cites the precedence rule when one
  /// applied. Makes wrong calls debuggable.
  final String rationale;

  /// The model's self-reported confidence, 0.0–1.0. Below the review threshold
  /// (see `triage_spec.md`), [needsHumanReview] must be true.
  final double confidence;

  /// True when the message is ambiguous, low-confidence, or [Topic.other] — in
  /// which case [team] should be [Team.triageReview].
  final bool needsHumanReview;

  /// Builds a result from the model's decoded JSON. Throws [FormatException]
  /// if any field is missing or not one of the allowed values — an invalid
  /// triage is a hard error, never a silent default.
  factory TriageResult.fromJson(Map<String, dynamic> json) {
    return TriageResult(
      urgency: _byWireName(Urgency.values, json['urgency'], 'urgency'),
      topic: _byWireName(Topic.values, json['topic'], 'topic'),
      team: _byWireName(Team.values, json['team'], 'team'),
      summary: _requireString(json, 'summary'),
      rationale: _requireString(json, 'rationale'),
      confidence: _requireConfidence(json['confidence']),
      needsHumanReview: _requireBool(json, 'needs_human_review'),
    );
  }

  Map<String, dynamic> toJson() => {
        'urgency': urgency.wireName,
        'topic': topic.wireName,
        'team': team.wireName,
        'summary': summary,
        'rationale': rationale,
        'confidence': confidence,
        'needs_human_review': needsHumanReview,
      };

  @override
  String toString() =>
      '[${urgency.label}] ${topic.label} → ${team.label} '
      '(confidence ${confidence.toStringAsFixed(2)}'
      '${needsHumanReview ? ', review' : ''})';
}

// --- JSON parsing helpers (fail loudly on anything off-spec) ---

T _byWireName<T extends Enum>(List<T> values, Object? raw, String field) {
  final wire = raw is String ? raw : null;
  for (final v in values) {
    if ((v as dynamic).wireName == wire) return v;
  }
  final allowed = values.map((v) => (v as dynamic).wireName).join(', ');
  throw FormatException(
    "Invalid '$field': ${raw ?? 'null'}. Allowed values: $allowed.",
  );
}

String _requireString(Map<String, dynamic> json, String field) {
  final value = json[field];
  if (value is String && value.trim().isNotEmpty) return value;
  throw FormatException("Missing or empty '$field'.");
}

bool _requireBool(Map<String, dynamic> json, String field) {
  final value = json[field];
  if (value is bool) return value;
  throw FormatException("Missing or non-boolean '$field'.");
}

double _requireConfidence(Object? raw) {
  final value = raw is num ? raw.toDouble() : null;
  if (value != null && value >= 0.0 && value <= 1.0) return value;
  throw FormatException("Invalid 'confidence': $raw. Must be a number 0.0–1.0.");
}

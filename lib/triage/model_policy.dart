// Step 3 — Model tier policy.
//
// Which Claude model handles which part of the triage workflow, and the rule
// that decides when a message is escalated from the cheap tier to the capable
// one. The justifications live in the doc comments so the *why* travels with the
// decision.
//
// Two tiers, matched to the two shapes of work in this pipeline:
//
//   • Tier 1 — high-volume first-pass classification  → Haiku 4.5
//   • Tier 2 — complex / sensitive re-classification   → Opus 4.8
//
// A small model does the bulk of the sorting; the capable model is spent only on
// the cases where a wrong call is expensive. The confidence gate and precedence
// rules from `triage_spec.md` are the safety net that makes this safe.

import 'triage_taxonomy.dart';

/// The two model tiers this workflow uses.
enum ModelTier {
  /// **Haiku 4.5** — the first pass over *every* inbound message.
  ///
  /// Justification: triage is a well-specified classification task — map a short
  /// message onto a closed taxonomy (`triage_taxonomy.dart`) under strict JSON
  /// output. That is exactly where the smallest, fastest tier performs well, and
  /// it is also the highest-volume, most latency-sensitive step (one call per
  /// ticket). Haiku 4.5 is the cheapest and fastest model ($1 / $5 per MTok,
  /// 200K context — far more than a support message plus the spec needs) and
  /// supports the strict structured-output format the schema depends on. Its
  /// mistakes are contained: anything it is unsure about, or that lands in a
  /// high-stakes bucket, is caught by [ModelPolicy.shouldEscalate] and re-run on
  /// Tier 2 before it can be mis-routed.
  fastClassifier('claude-haiku-4-5', 'Haiku 4.5'),

  /// **Opus 4.8** — re-classifies the complex or sensitive minority.
  ///
  /// Justification: the cases that escalate are the ones where a wrong call costs
  /// the most — a missed account compromise, a churn threat routed to the wrong
  /// queue, a P1 outage sorted as routine, or a message the fast tier simply
  /// wasn't confident about. These need the strongest reasoning and instruction-
  /// following, and there are few enough of them that the higher price ($5 / $25
  /// per MTok) is spent only where it changes the outcome. Opus 4.8 — not
  /// Fable 5 — because triage is short, single-turn classification, not the
  /// long-horizon agentic reasoning Fable's premium is for; paying Fable rates
  /// here would be over-spending with no quality gain on this task shape.
  capableReviewer('claude-opus-4-8', 'Opus 4.8');

  const ModelTier(this.modelId, this.label);

  /// The exact model ID string passed to the API.
  final String modelId;

  /// Human-readable name for logs and dashboards.
  final String label;
}

/// Decides how model tiers are applied across the workflow.
abstract final class ModelPolicy {
  /// Below this first-pass confidence, don't trust the cheap tier — send the
  /// message to the capable tier for a second opinion. Deliberately set *above*
  /// the human-review floor of 0.60 (see `triage_spec.md` §4): the 0.60–0.75
  /// band is "not obviously wrong, but not confident enough to route cheaply."
  static const double escalationConfidenceFloor = 0.75;

  /// Teams where a misroute is expensive enough to always warrant the capable
  /// tier: a security compromise or a churn save is worth the extra tokens.
  static const Set<Team> sensitiveTeams = {
    Team.trustAndSafety,
    Team.retention,
  };

  /// Given Tier-1's result, should this message be re-classified on Tier 2?
  ///
  /// Escalate when the cheap pass is either **uncertain** or **high-stakes** — so
  /// Opus is spent on exactly the messages where accuracy matters most, and Haiku
  /// handles the clear-cut majority alone.
  static bool shouldEscalate(TriageResult firstPass) {
    return firstPass.confidence < escalationConfidenceFloor ||
        firstPass.needsHumanReview ||
        firstPass.urgency == Urgency.urgent ||
        firstPass.topic == Topic.other ||
        sensitiveTeams.contains(firstPass.team);
  }

  /// The tier to use for the initial pass over every message.
  static const ModelTier firstPass = ModelTier.fastClassifier;

  /// The tier used when [shouldEscalate] returns true.
  static const ModelTier escalation = ModelTier.capableReviewer;
}

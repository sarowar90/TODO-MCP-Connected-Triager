# Triage Specification

The authoritative definition of **correct triage**. `triage_taxonomy.dart` encodes
the closed value sets; this document defines what each value *means* and — most
importantly — the **precedence rules** that resolve ambiguity so two people (or
the model and an eval) reach the same answer on the same message.

Every support message is assigned exactly one **urgency**, one **topic**, and one
**team**, plus a one-line summary, a rationale, and a confidence score.

---

## 1. Urgency — *how fast must this be handled?*

Urgency is about **impact and time-sensitivity only**. It is independent of topic.

| Level | Wire | Meaning | Target first response |
|---|---|---|---|
| Urgent (P1) | `urgent` | Active outage, security breach or data exposure, payments completely broken, or a legal/compliance threat. Business-critical, pages on-call. | minutes |
| High (P2) | `high` | Core workflow blocked with no workaround, a billing error with money actively at stake, or a credible cancellation/churn threat. | hours |
| Normal (P3) | `normal` | Standard question, minor bug **with a workaround**, or routine account/billing task. The default. | 1–2 business days |
| Low (P4) | `low` | Non-blocking: general feedback, "nice to have" requests, thank-yous. | best effort |

**Deciding urgency — ask in order:**
1. Is something **actively broken or exposed right now** (outage, breach, payments down, legal threat)? → `urgent`
2. Is the customer **blocked with no workaround**, is **money at stake**, or are they **threatening to leave**? → `high`
3. Is it a normal request or a bug that has a workaround? → `normal`
4. Is it non-blocking feedback or a minor suggestion? → `low`

Pick the **first** level that matches. Urgency can only ever be *raised* by the
precedence rules in §4, never lowered.

---

## 2. Topic — *what is it fundamentally about?*

Choose the **primary** subject. For multi-issue messages, pick the topic tied to
the **highest-urgency actionable issue** (§4, rule 5).

| Topic | Wire | Includes |
|---|---|---|
| Billing & Payments | `billing` | Charges, invoices, refunds, subscriptions, pricing, payment methods |
| Technical Issue | `technical` | Bugs, errors, crashes, outages, data problems, integration/API failures |
| Account & Access | `account` | Login, password reset, permissions, profile, plan changes, **and account-security concerns** (unauthorized access, suspected compromise) |
| Feature Request | `feature_request` | Suggestions for new or enhanced capabilities |
| How-To / Guidance | `how_to` | "How do I…?" where the product works as designed |
| Complaint / Escalation | `complaint` | Expressed dissatisfaction, escalation, or intent to leave/cancel |
| Other / Unclear | `other` | Doesn't fit above, unintelligible, or spam |

**`technical` vs `how_to`:** if the product is *misbehaving*, it's `technical`; if
the product works and the user just doesn't know how to use it, it's `how_to`.

**`complaint` vs the underlying topic:** a frustrated tone alone does **not** make
it a complaint. Use `complaint` only when dissatisfaction/escalation/cancellation
is the *point* of the message. "This billing bug is infuriating, please fix it" is
still `billing`; "I'm done, cancel my account" is `complaint`.

---

## 3. Team — *who owns it?*

| Team | Wire | Owns |
|---|---|---|
| Billing Team | `billing` | Billing, invoices, refunds, subscription changes |
| Engineering | `engineering` | Bugs, outages, technical/integration failures |
| Customer Success | `customer_success` | Onboarding, how-to, feature requests, general account help |
| Trust & Safety | `trust_and_safety` | Account compromise, unauthorized access, data-exposure reports |
| Retention | `retention` | Churn risk — cancellation threats, at-risk saves |
| Human Triage Review | `triage_review` | Anything ambiguous, low-confidence, or `other` |

The **default** team follows the topic:

| Topic | Default team |
|---|---|
| `billing` | Billing Team |
| `technical` | Engineering |
| `account` | Customer Success |
| `feature_request` | Customer Success |
| `how_to` | Customer Success |
| `complaint` | Customer Success |
| `other` | Human Triage Review |

…and is then overridden by the precedence rules below.

---

## 4. Precedence rules (the reliability core)

Apply these **in order**. The first matching rule wins for the field it sets;
later rules still apply to fields an earlier rule didn't set. This ordering is
what makes triage deterministic — without it, "security billing complaint"
messages get sorted differently every time.

1. **Safety first.** If the message credibly reports **account compromise,
   unauthorized access, or exposed/leaked data**, set team = `trust_and_safety`
   and urgency ≥ `high` (`urgent` if the access/exposure is *ongoing*) —
   regardless of topic. Topic stays `account`.

2. **Churn next.** If the message expresses **intent to cancel or a credible
   threat to leave**, set team = `retention` and urgency ≥ `high`. This wins over
   the billing/technical default team, because saving the account is the priority.

3. **Outage/critical impact.** If something is **actively down or payments are
   fully broken**, set urgency = `urgent`. Team follows the topic default
   (`engineering` for technical, `billing` for payments) unless rule 1 already set it.

4. **Otherwise, team = default-for-topic** (§3 table) and urgency = the §1 result.

5. **Multi-issue messages.** Triage the **single highest-urgency actionable
   issue**; mention any secondary issues in the summary. Do not average or split.

6. **Ambiguity / low confidence.** If the message is unclear, you can't confidently
   pick a topic, or confidence `< 0.60`, set `needs_human_review = true` and team =
   `triage_review`. A confident, well-understood message has `needs_human_review = false`.

**Confidence** is the model's self-reported certainty, `0.0`–`1.0`. The review
threshold is **0.60**: at or below it, the message goes to human review regardless
of the other fields.

---

## 5. Output contract

Exactly these fields, matching `TriageResult` in `triage_taxonomy.dart`:

```json
{
  "urgency": "urgent | high | normal | low",
  "topic": "billing | technical | account | feature_request | how_to | complaint | other",
  "team": "billing | engineering | customer_success | trust_and_safety | retention | triage_review",
  "summary": "one sentence restating the customer's core issue",
  "rationale": "why this urgency/topic/team — cite the precedence rule if one applied",
  "confidence": 0.0,
  "needs_human_review": false
}
```

---

## 6. Worked examples

These double as the first eval cases in Step 4.

| Message | urgency | topic | team | Why |
|---|---|---|---|---|
| "The whole app is returning 500s, none of my team can log in." | `urgent` | `technical` | `engineering` | Rule 3: active outage. |
| "I was charged twice for this month's subscription — $240 gone." | `high` | `billing` | `billing` | Money actively at stake → high; topic default team. |
| "Someone logged into my account from Russia and changed my password." | `urgent` | `account` | `trust_and_safety` | Rule 1: ongoing unauthorized access. |
| "This keeps happening and I'm done — cancel my subscription today." | `high` | `complaint` | `retention` | Rule 2: cancellation intent → retention. |
| "How do I export my data to CSV?" | `normal` | `how_to` | `customer_success` | Product works as designed; routine. |
| "Would be great if dark mode remembered my preference." | `low` | `feature_request` | `customer_success` | Non-blocking suggestion. |
| "Chart colors are slightly off on the dashboard, but the CSV export is fine." | `normal` | `technical` | `engineering` | Minor bug with a workaround. |
| "asdkjh test test ignore" | `low` | `other` | `triage_review` | Unintelligible → review. |
| "Your refund policy is a scam and I've reported you to my bank." | `high` | `complaint` | `retention` | Escalation + churn signal; legal mention doesn't reach P1 on its own. |
| "The API returned my invoice with another customer's email on it." | `urgent` | `account` | `trust_and_safety` | Rule 1: data exposure, even though it surfaced via billing/API. |

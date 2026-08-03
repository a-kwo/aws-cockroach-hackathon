# Owner profile and contact routing

Brass Tacks keeps one editable profile per business-owner account. The owner
opens it from the three-line menu in the application header.

## What the owner can view and edit

- Owner name and work email
- Business name, category, location, and public website
- Core customer groups
- Products and services
- Customer-discovery channels
- Current business priority

The profile is stored in CockroachDB. Contact information lives on
`owner_account`; structured business context lives on `business.profile_data`.
Only bounded, sentence-shaped business facts are embedded into `business_fact`.
The owner's email address is never embedded or placed in an LLM prompt.

## Current demo-account email migration

The three legacy demo workspaces predate account-email persistence. The additive
profile migration sets this test inbox only when a business-bound account has no
email yet:

```text
peter.flp.2006@gmail.com
```

It does not overwrite an email already saved by an owner. New signups persist
the address entered during onboarding.

## Maker email routing

Maker resolves the review-email recipient from the business's authenticated
owner profile. The account that approved the task is preferred. The deployment
value `MAKER_REVIEW_EMAIL` remains only a fallback for imported or system-created
tasks that have no owner contact yet.

The sender still comes from `MAKER_EMAIL_FROM` and must be a verified Amazon SES
identity. The model cannot select either the sender or the recipient.

## Operator visibility

Memory Engine includes the recorded owner email in every business row and in the
three-line profile menu's portfolio list. The admin endpoint is authorization-
gated; ordinary owners can read and edit only the profile attached to their own
session.

## API

```http
GET /v1/profile
Authorization: Bearer <session>
```

returns the signed-in owner's profile with zero LLM tokens.

```http
PUT /v1/profile
Authorization: Bearer <session>
Content-Type: application/json
```

updates contact fields, business metadata, and the bounded profile facts in one
database transaction. Profile edits may create embeddings, but they do not call
the reasoning model.

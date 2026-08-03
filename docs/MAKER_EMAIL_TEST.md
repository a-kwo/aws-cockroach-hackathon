# Maker Email End-to-End Test

This setup verifies that one approved recommendation creates one Maker task, one draft, and one review email.

## Test addresses

- SES sender: `peter.flp.2006@gmail.com`
- Test recipient: `virtual.icfd@gmail.com`
- AWS region: `us-east-1`

The sender is controlled by server configuration. The recipient normally comes from the signed-in business profile. For the controlled test, edit one business profile and set its **Work email** to `virtual.icfd@gmail.com`.

## 1. Deploy the full application

Run the GitHub Actions workflow **Deploy Brass Tacks**. The final CloudFormation result must be `UPDATE_COMPLETE`.

## 2. Request SES verification

Run **Configure Maker Email** with:

```text
action: request-verification
```

Open both Gmail inboxes and click the SES verification links. The recipient must also be verified while the SES account remains in sandbox mode.

## 3. Enable Maker email

Run **Configure Maker Email** again with:

```text
action: enable-email
```

The workflow verifies both identities, writes the `/brasstacks` SSM settings, enables email, and refreshes the Maker Email Lambda environment.

## 4. Configure one test business

Sign in to Brass Tacks, open the three-strip profile menu, and set:

```text
Work email: virtual.icfd@gmail.com
```

Save the profile.

## 5. Run one clean Maker task

Choose a recommendation that has never been approved and has no existing draft. Press **Do it** once.

Expected progression:

```text
Saving...
Approved
Maker queued
Maker running
Maker completed
```

## 6. Confirm the result

Memory Engine should show exactly:

```text
1 approved recommendation
1 durable Maker task
1 current draft
1 succeeded SES tool receipt
```

The email should arrive at `virtual.icfd@gmail.com` from `peter.flp.2006@gmail.com` and contain a link to the exact task.

Wait at least five minutes and confirm that reconciliation does not create another draft or send another email.

## Troubleshooting

- **No task:** decision-to-task dispatch failed.
- **Task remains queued:** SQS or Step Functions did not start the worker.
- **Draft exists but email is skipped:** Maker email is disabled or configuration is missing.
- **SES identity error:** sender or sandbox recipient is not verified in `us-east-1`.
- **Two drafts or two emails:** task claim or idempotency protection regressed.

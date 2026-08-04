Brass Tacks v33 - Memory Engine email receipt UI

This drop-in was created from the rebased repository archive supplied by the user.

Changed files:
  site/app.html
  backend/tests/test_site_build.py

What changes:
- Expanded Maker tasks in Memory Engine now display the complete review-email receipt.
- Shows sender, recipient, subject, SES message ID, and delivery timeline.
- Shows the exact plain-text email body behind "View the exact message sent".
- Uses the newest SES email tool receipt when a Maker task has multiple draft revisions.
- Historical/superseded tasks remain visibly archived.
- Adds a regression test proving the task ledger is actually wired to the email receipt renderer.

Install from the repository root in PowerShell:

  Expand-Archive `
    "$env:USERPROFILE\Downloads\brasstacks-v33-memory-engine-email-receipt.zip" `
    -DestinationPath . `
    -Force

Then:

  py scripts\build_web.py
  py -m pytest backend\tests\test_site_build.py -q
  git add site\app.html backend\tests\test_site_build.py
  git commit -m "Show Maker email content and delivery status in Memory Engine"
  git pull --rebase origin main
  git push origin main

Deployment:
- This is frontend-only application logic plus a test.
- Run the fast "Deploy Frontend" GitHub Actions workflow after pushing.
- Hard-refresh the deployed app with Ctrl+Shift+R.

Where to see it:
  Memory Engine -> Maker -> Live task ledger -> expand a task

The expanded task now includes:
  Review email
  From / To / Subject / SES message ID
  SES accepted / Delivered / Opened / Link clicked
  View the exact message sent

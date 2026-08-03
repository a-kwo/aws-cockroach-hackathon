# Brass Tacks v29 — Maker Email Setup

This drop-in includes the v28 owner-profile update plus a GitHub Actions workflow for configuring and enabling the Maker review email.

The workflow uses GitHub repository secrets:

- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

It does not contain AWS credentials.

After extraction:

1. Commit and push all files.
2. Run the full **Deploy Brass Tacks** workflow.
3. Run **Configure Maker Email** with `request-verification`.
4. Click both SES verification links.
5. Run **Configure Maker Email** with `enable-email`.
6. Change one business profile Work email to `virtual.icfd@gmail.com`.
7. Press **Do it** on one brand-new recommendation.

See `docs/MAKER_EMAIL_TEST.md` for the complete acceptance test.

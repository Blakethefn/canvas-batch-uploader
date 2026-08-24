# Security policy

Canvas Batch Uploader handles Canvas personal access tokens and potentially private coursework. Please treat both as sensitive.

## Reporting a vulnerability

Do not open a public issue containing credentials, private course data, exploit details, or a reproducible attack that could affect users. Use GitHub's private vulnerability reporting for this repository when available, or contact the repository maintainers privately through GitHub.

If a Canvas token may have been exposed, revoke it in Canvas immediately and create a replacement. Do not include the token in a report.

## Security expectations

- Keep `API_KEY` in a local, ignored `.env` file or in the process environment.
- Never commit tokens, Canvas responses, downloaded coursework, submission files, or exported results.
- Use an HTTPS Canvas base URL and grant the token only the access needed for the intended workflow.
- Review the exact batch before enabling or confirming a submission.
- Report suspected credential leakage, unauthorized network requests, permission bypasses, unsafe redirects, or accidental submission behavior privately.

This project does not bypass Canvas permissions, deadlines, or access controls. Institution policies and Canvas permissions remain authoritative.

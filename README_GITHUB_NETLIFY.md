# WB Replenishment Dashboard

Static public dashboard for WB stock days and replenishment risks.

Upload these files to GitHub:

- scripts/build_public_dashboard.py
- public/index.html
- public/data.json
- requirements.txt
- .github/workflows/update-dashboard.yml

Required GitHub Actions secrets:

- WB_API_TOKEN
- NETLIFY_AUTH_TOKEN
- NETLIFY_SITE_ID

The workflow runs daily at 04:00 UTC, which is 07:00 Moscow time.

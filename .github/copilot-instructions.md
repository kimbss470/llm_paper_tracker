# Copilot Logging Instruction

When completing any task in this repository:

1. Summarize the completed chat result.
2. Append one entry to today's KST log file in `log/chat_YYYY-MM-DD.md`.
3. Use the script below whenever possible:

```bash
./scripts/log_chat_summary.sh \
  --request "<user request summary>" \
  --actions "<what was changed>" \
  --result "<outcome/validation>" \
  --notes "<optional>"
```

Minimum required fields in each entry:
- Request
- Actions
- Result

This logging step is mandatory for every completed chat task.

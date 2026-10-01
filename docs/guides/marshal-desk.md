# Marshal desk

Marshal explains relay diagnostics in the dashboard. It is a candidate feature; source and local validation do not establish a published or deployed release.

## Availability

The operator must enable `REMEMBRA_MARSHAL_ENABLED=true`. It is off by default. Set `REMEMBRA_MARSHAL_ALLOW_USERS` to approved user IDs for a limited rollout. An explicitly empty list allows nobody; omitting the setting allows any dashboard account once the operator enables the feature. API keys and connected-app grants cannot use the desk.

## Use the desk

Sign in to the dashboard, open the Marshal bar, and ask about your agents' trail, latest handoff, inbox counts or usage. A **why?** slip can prefill a question about one agent; editing it clears that agent scope. Read the evidence chips beside the answer. A rule-generated fallback means the model's answer could not be confirmed.

Commands are suggestions for you to inspect and run. Marshal does not execute commands, make edits, send messages, rotate keys or change billing. It refuses commands for unverified hook adapters and binds only projects supported by returned records. Crew coordination and write enforcement depend on the client and server configuration; a desk answer does not establish enforcement.

## Model, limits and diagnostics

The desk uses the operator's OpenAI-compatible model credentials. It defaults to `gpt-4o-mini`; the operator can change the model or endpoint. With no key, an open provider circuit or exhausted platform budget, model answers are unavailable while rules-only checks remain usable.

Defaults are 40 questions per account per day, a $5 platform daily budget, a $100 platform monthly budget and a $0.02 bound per ask. The desk shows its usage separately and does not debit smart credits. Interrupted calls that reached the provider count and reserve a conservative cost when actual usage is unavailable.

To turn the desk off for your account, go to **Settings → Diagnostics → Marshal desk**. Enrichment model settings do not control this desk.

The operator can inspect error types, counts and spend metadata. Prompt contents and read results are excluded from desk logs; SDK request-option debug logs are suppressed. Key-shaped input is scrubbed before the model receives it, not before the browser sends it to the API. Questions and permitted record excerpts reach the configured model provider. The transcript is held in the current page; reload starts a new conversation. This description is operational guidance, not a new privacy or retention promise.

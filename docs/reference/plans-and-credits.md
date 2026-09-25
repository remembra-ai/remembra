# Remembra Cloud plans and smart credits

Self-hosting Remembra is free and open source (MIT). Everything on this page
applies to the hosted service, Remembra Cloud. Prices are in USD and exclude tax.

## Plans

| Plan | Price | Smart credits | Memories | Recalls / month | Projects | API keys |
|------|-------|---------------|----------|-----------------|----------|----------|
| **Free** | $0, no card | 500 / month (25 until your email is verified) | 10,000 | 10,000 | 3 | 3 |
| **Solo** | $12 / month or $120 / year | 2,200 / month | 50,000 | 50,000 | Unlimited | 10 |
| **Pro** | $29 / month or $290 / year | 5,000 / month | 125,000 | 250,000 | Unlimited | 25 |
| **Team** | $15 per seat / month or $150 per seat / year, 3-seat minimum | 2,200 per seat, pooled | 50,000 per seat, pooled | 50,000 per seat, pooled | Unlimited | 10 per seat |
| **Enterprise** | Custom (from $399 / month) | Contracted | Contracted | Contracted | Contracted | Contracted |

**Founding 100.** The first 100 customers can take Solo at **$108 a year**,
with the price locked for life. It is billed annually only, one per account,
and cannot be combined with other offers.

**Annual plans bank the year up front.** On a yearly plan you get twelve
months of credits on day one (Solo: 26,400) and can use them in any month of
your subscription year. The bank resets when your subscription year renews.

### Input limits

| | Free | Solo, Pro, Team |
|---|------|------------------|
| Characters per stored item | 8,000 | 50,000 |
| Items per batch request | 10 | 100 |
| Queries per batch recall | 5 | 20 |
| Recall burst | 20 / minute | 60 (Solo), 120 (Pro, Team) / minute |
| Relay burst | 30 / minute | 60 (Solo), 120 (Pro, Team) / minute |

## What is free on every plan

Agent relay never uses smart credits:

- handoffs, checkpoints and status values,
- the agent inbox,
- pickup briefs at session start and trail/timeline reads,
- recalls (they have their own monthly and per-minute limits).

Relay events have a fair-use soft cap (Free 5,000, Solo 25,000, Pro 100,000
per month; Team 25,000 per seat). Going over it is reported in the
`X-Remembra-Relay-Soft-Cap: exceeded` header and the usage API; nothing is
rejected.

## Smart credits

A smart credit pays for the AI work that turns what you store into structured
memory: fact extraction, consolidation and the entity graph. One credit is
about one normal note, or $0.0025 of AI spend.

A store costs the larger of:

- one credit per started 8,000 characters of content, and
- the store's actual AI spend divided by $0.0025.

So long or fact-dense content uses more credits than a short note. Bulk
imports through `/memories/bulk` and stores with `skip_extraction: true` do
no AI work and use no credits.

### How metering works

1. Before any AI call, Remembra reserves 16 credits per 8,000-character chunk
   for the whole request (a batch, an import or a conversation is reserved
   as one unit).
2. When the AI work has finished, including background entity linking, the
   reservation is settled from the real token usage and the unused part is
   refunded.
3. If the reservation does not fit in your remaining credits, the write is
   still stored. It is stored as-is, without enrichment, and the response
   says so.

**Running out never blocks you.** Stores keep saving verbatim and stay
searchable; only the AI enrichment pauses until credits renew. The only store
that is rejected (HTTP 429) is one that would pass your plan's memory cap.

### Response headers

| Header | Values |
|--------|--------|
| `X-Remembra-Enrichment` | `full` (enriched), `degraded` (stored without enrichment), `atomic` (relay or `skip_extraction`: no AI by design) |
| `X-Remembra-Credits-Remaining` | Credits left in the current month, or in the yearly bank |
| `X-Remembra-Plan` | Your plan id |

A degraded store also returns `"enrichment": "degraded"` in the JSON body.

### Usage API

`GET /api/v1/cloud/usage/summary` returns everything the dashboard shows:
plan, billing interval, credits (limit, used, reserved, remaining, monthly or
yearly bank, AI dollars spent), enrichment status, relay events against the
soft cap, recalls, memories against the cap, and how many stores were saved
without enrichment this month. `GET /api/v1/cloud/usage/daily` has the
per-day history, including credits used.

## Grandfathered plans

Subscribers of the previous $49 Pro and $199 Team plans keep their price.
Their plans now carry an AI-spend ceiling of 12,000 credits ($30) and 60,000
credits ($150) per month respectively, and can switch to the new plans at any
time. Changes to memory caps take effect only after 30 days' written notice.

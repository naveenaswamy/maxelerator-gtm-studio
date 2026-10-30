# FlexPrice setup

FlexPrice turns studio activity into credits each startup spends from a prepaid wallet.
Maxelerator decides who pays: the program can grant credits as a perk, or sell top-ups.

## How the studio meters

| Action | Event | Credits |
| --- | --- | --- |
| Section saved (first time per playbook) | `maxel.credits`, `action=section` | 10 (executive summary 15) |
| Playbook assembled | `maxel.credits`, `action=finalize` | 5 |
| **Full playbook** | | **90** |

Credits per section live in `config/studio.yaml`. Revising a section is free: the event ID is
`<playbook_id>:<section_id>`, so a retry or revision never double-charges (FlexPrice also
deduplicates on `event_id`).

Example event the studio sends:

```json
{
  "event_name": "maxel.credits",
  "external_customer_id": "maxel-qubit-sense",
  "event_id": "pb-20261030-190438-d820:market_icp",
  "timestamp": "2026-10-30T19:04:38Z",
  "source": "maxel-gtm-studio",
  "properties": {"credits": "10", "action": "section", "section": "market_icp", "playbook_id": "pb-20261030-190438-d820", "agent": "band:market_analyst"}
}
```

## One-time setup in the FlexPrice dashboard

1. Create an API key; put it in `.env` as `FLEXPRICE_API_KEY`. Set `FLEXPRICE_BASE_URL` to
   your account's region (US `https://us.api.flexprice.io/v1`, India `https://api.cloud.flexprice.io/v1`).
2. **Feature:** create a metered feature "GTM Studio credits" with event name `maxel.credits`,
   aggregation **SUM** on property `credits`.
3. **Plan:** create a plan "Maxelerator Cohort" with a usage price on that feature, for example
   $1.00 per credit, so wallet credits (1 credit = $1 at conversion rate 1) pay for usage.
4. Set `MAXEL_METER=flexprice` and run `maxel flexprice ping`; confirm the test event in the
   Event Debugger.

## Per startup (automatic)

`maxel onboard` / `POST /startups` does this for you:

1. `POST /v1/customers` with External ID `maxel-<startup_id>` (409 means it already exists).
2. `POST /v1/wallets` with `initial_credits_to_load` = the startup's grant (default 500).

Then **subscribe the customer to the "Maxelerator Cohort" plan** (dashboard, or your own
automation) so usage is priced and drawn from the wallet. The studio checks the wallet's
real-time balance before it starts a playbook and refuses when it cannot cover 90 credits.

## Top-ups and the founder's view

- Program staff: `maxel top-up <startup_id> 200` or `POST /startups/<id>/credits/top-up`.
- Founders can see invoices and usage in FlexPrice's customer portal
  (`GET /v1/customers/portal/{external_id}` returns a short-lived link).

## Offline mode

`MAXEL_METER=log` writes the same charges to `var/ledger.jsonl` and computes balances
locally. Use it for dry runs and tests; switch to `flexprice` before the cohort starts.

## Going further: startups billing their own customers

The Pricing Architect writes each startup a FlexPrice setup for its own product (feature,
event name, aggregation, plan). Startups that sell to buyers with AWS committed spend can use
FlexPrice's AWS Marketplace integration (beta): FlexPrice reports usage to AWS through
`BatchMeterUsage`, AWS bills the buyer, and the listing must use exactly one usage dimension.
That is the startup's choice and needs no AWS hosting for Maxelerator itself.

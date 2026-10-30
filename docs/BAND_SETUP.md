# BAND setup

The studio is five BAND remote agents. BAND hosts the rooms, routing and history; you run
the agent processes (Docker Compose or any host).

## 1. Create the five remote agents

Sign in at [app.band.ai](https://app.band.ai) with the Maxelerator workspace and
[create a remote agent](https://docs.band.ai/getting-started/connect-remote-agent) for each
role. Use these exact names: the agents @mention each other by them.

| Name | Role key | Env vars | Description to paste |
| --- | --- | --- | --- |
| Maxel Strategist | `strategist` | `STRATEGIST_AGENT_ID`, `STRATEGIST_API_KEY` | Leads Maxelerator GTM playbooks: checks credits, assigns sections, writes the summary, assembles the playbook. |
| Maxel Market Analyst | `market_analyst` | `MARKET_ANALYST_*` | Market sizing, beachhead segment and buying group for deeptech startups. |
| Maxel Pricing Architect | `pricing_architect` | `PRICING_ARCHITECT_*` | Pricing, packaging and a ready-to-use FlexPrice billing setup. |
| Maxel Messaging Writer | `messaging_writer` | `MESSAGING_WRITER_*` | Positioning, persona messaging and the sales play. |
| Maxel Launch Planner | `launch_planner` | `LAUNCH_PLANNER_*` | Channels (including AWS Marketplace when it fits) and the 90-day launch plan. |

Copy each agent's UUID and API key into `.env`. Each agent gets its own pair. If you rename an
agent, change `display_name` in `config/studio.yaml` to match.

## 2. Start the agents

```bash
docker compose up -d --build      # API + five agent containers
# or, without Docker (Python 3.11+):
pip install -e ".[band,llm]"
maxel band run --all
```

Each agent logs `Connected to platform` when it is live. Run exactly one process per agent ID.

## 3. Create the studio room and run a playbook

1. In BAND, create a room per cohort (for example "Maxelerator Cohort 7 · GTM Studio").
2. Add the five agents from the participants panel. Add program managers and mentors too.
3. Onboard the startup (`maxel onboard ...` or `POST /startups`).
4. Post the kickoff message (`maxel band kickoff <startup_id>` prints it):
   `@Maxel Strategist please build the Maxelerator GTM playbook for startup qubit-sense. ...`
5. Watch the room: the Strategist checks credits, opens the playbook and @mentions the
   specialists, who work in parallel and report `SECTION SAVED`. A mentor can step in at any
   point ("@Maxel Pricing Architect assume a paid pilot at $60k") and the agents follow.
6. The Strategist posts `PLAYBOOK COMPLETE` with the credits used. Read it with
   `maxel show <startup_id>` or `GET /startups/<id>/playbooks/latest?format=md`.

## Notes

- Messages without an @mention are not delivered to an agent (BAND behaviour), which keeps
  agents quiet unless addressed.
- Contact requests are ignored by default (`ContactEventStrategy.DISABLED`), so outsiders
  cannot add the studio agents to rooms and spend credits. Keep it that way.
- Every exchange is kept in BAND's audit trail. That is the record of how each playbook was
  produced if a founder or investor asks.
- If the SDK version you install names its tool-handler arguments differently, only
  `src/maxel/band_runtime.py` needs changing; its handler accepts a model, a dict or kwargs.

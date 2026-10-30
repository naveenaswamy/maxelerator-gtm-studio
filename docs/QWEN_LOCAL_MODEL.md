# Your local Qwen model in the studio

Your Qwen2.5-0.5B adapter from Qwen Studio becomes Maxelerator's **pricing model**: it reads a
startup profile and assigns a pricing model, which the studio validates and turns into
FlexPrice objects. BAND's Pricing Architect agent calls it as a tool.

## What the model does, and what it doesn't

| Job | Who does it | Why |
| --- | --- | --- |
| Assign a pricing model (strict JSON) | **Local Qwen** | Narrow, structured, short. A 0.5B model can learn it. |
| Validate the answer | Studio code | Every answer is schema-checked; one retry with the errors; then rules take over |
| Create the pricing in FlexPrice | Studio code (`maxel pricing apply`) | Only after a mentor approves |
| Write the full playbook, coordinate in BAND | Claude via BAND (or Qwen in local mode, for experiments) | Long, nuanced writing needs a larger model |

The adapters you trained (20 iterations on the 20-example practice set) prove the pipeline works,
but they learned generic sales replies, not pricing. Your two runs (`...-140823-train-c24023` and
`...-141202-train-397619`) logged identical losses at every step: Qwen Studio fixes the seed at 42,
so the same data reproduces the same adapter. Retraining only changes anything with new data, so
train a pricing adapter next (step 2).

## 1. Serve your model (on the Mac)

```bash
pip install "mlx-lm[train]"          # already installed for Qwen Studio
mlx_lm.server \
  --model mlx-community/Qwen2.5-0.5B-Instruct-4bit \
  --adapter-path ~/2026/gtm/results/qwen_studio/<run>/adapter \
  --port 8081
```

This exposes an OpenAI-compatible endpoint at `http://127.0.0.1:8081/v1`. Port 8081 avoids the
studio API (8080) and Qwen Studio (8501/8502). Add to `.env`:

```bash
MAXEL_PRICING_LLM=openai                       # local Qwen assigns pricing models
MAXEL_OPENAI_BASE_URL=http://127.0.0.1:8081/v1
# MAXEL_LLM=openai                             # optional: Qwen also writes playbook sections (weak at 0.5B)
```

Check it: `maxel pricing recommend qubit-sense`. The first line says whether the model's answer
was accepted (`assigned by the pricing model`) or replaced (`failed validation, so rules assigned it`).
`maxel pricing show qubit-sense` lists the validation errors, which tell you what to train next.

## 2. Train a pricing adapter in Qwen Studio

```bash
maxel training pricing-seed --count 150 --out training/pricing_seed.jsonl
```

In Qwen Studio: **01 Train model → Upload JSONL** → `pricing_seed.jsonl`, then:

| Setting | Value | Why |
| --- | --- | --- |
| Training iterations | 300 | 120 training examples × about 2.5 passes at batch size 1 |
| Learning rate | 0.0001 | Format learning on a small adapter |
| Maximum sequence length | 1024 | Each example is about 500 tokens |

Watch validation loss: if it rises while training loss keeps falling, the adapter is memorising;
use the checkpoint saved before the rise (Qwen Studio saves every 10 iterations).

Then in **02 Generate answers**, choose **Compare both** and paste a prompt built by the studio:

```bash
python -c "from maxel.pricing import pricing_user, PRICING_SYSTEM; import json; \
from maxel.studio import build_studio; s=build_studio(); print(pricing_user(s.store.get_startup('qubit-sense')))"
```

Put `PRICING_SYSTEM` (from `src/maxel/pricing.py`) into **System instruction**. The trained
answer should be a single JSON object; the original model usually is not.

The seed data is labelled by the studio's rules, so this adapter learns the format and the rules'
judgment, no better. Step 4 is how it gets better than the rules.

## 3. Use it in BAND

Nothing extra to deploy: the Pricing Architect agent has a `RecommendPricingModel` tool that
calls whatever `MAXEL_PRICING_LLM` points at. Because MLX needs Apple Silicon, run the Pricing
Architect agent (and `mlx_lm.server`) on the Mac:

```bash
maxel band run --role pricing_architect      # on the Mac, next to mlx_lm.server
```

The other four agents can run anywhere (Docker, cloud). BAND connects them all through the same
room, so the mixed setup needs no networking between machines.

## 4. Improve it with mentor-approved data

1. Run `maxel pricing recommend <id>` for each cohort startup.
2. A mentor reviews `maxel pricing show <id> --json`. Approve it as-is
   (`maxel pricing approve <id>`), or save an edited copy and approve that
   (`maxel pricing approve <id> --from-file edited.json`).
3. Approve good playbooks too: `maxel playbook approve <id>`.
4. Export and retrain: `maxel training export --with-seed 100 --out training/round2.jsonl`.
   Approved examples teach judgment; the seed keeps the format stable.

Only approved results are exported, and stub output never is. If you label data with a hosted
model, check that provider's terms on training other models with its outputs first.

## 5. Create the pricing in the startup's FlexPrice

```bash
maxel pricing apply qubit-sense                      # dry run: prints the 6 API calls
STARTUP_FLEXPRICE_API_KEY=<startup's key> maxel pricing apply qubit-sense --execute
```

This creates, in the **startup's own** FlexPrice account: a metered feature (with its meter), a
plan, a platform-fee price, a usage price, and, when there is a pilot, a separate one-time "Paid
Pilot" plan. It refuses to execute until a mentor has approved the pricing model. The startup
then subscribes its customers to the plan and sends usage events like the example the studio prints.

## When to move beyond 0.5B

If validation keeps failing after a pricing adapter, or recommendations stay generic, switch the
model in Qwen Studio (`MODEL` at the top of `qwen_studio.py`) to
`mlx-community/Qwen2.5-1.5B-Instruct-4bit` or `Qwen2.5-3B-Instruct-4bit`, raise `--num-layers`
from 4 to 8 or 16, and retrain on the same data. Nothing in the studio changes.

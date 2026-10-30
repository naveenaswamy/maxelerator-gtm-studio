.PHONY: install test demo up down

install:            ## local dev install (BAND SDK needs Python 3.11+)
	pip install -e ".[band,llm,dev]"

test:
	pytest -q

demo:               ## offline dry run: stub LLM, local ledger, no keys needed
	MAXEL_LLM=stub MAXEL_METER=log maxel onboard examples/startups/qubit-sense.json
	MAXEL_LLM=stub MAXEL_METER=log maxel run qubit-sense
	MAXEL_LLM=stub MAXEL_METER=log maxel show qubit-sense | head -40

up:
	docker compose up -d --build

down:
	docker compose down

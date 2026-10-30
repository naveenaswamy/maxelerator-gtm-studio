FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
COPY prompts ./prompts
COPY templates ./templates

# Editable install keeps config/, prompts/ and templates/ resolvable from /app.
RUN pip install -e ".[band,llm,aws]" && useradd --create-home maxel && mkdir -p /app/var && chown maxel /app/var
USER maxel

EXPOSE 8080
# Default: the API. Agent containers override the command with `maxel band run --role <role>`.
CMD ["maxel", "serve", "--port", "8080"]

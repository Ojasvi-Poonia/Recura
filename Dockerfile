# Recura - reproduce the headline number on a clean machine.
#
#   docker build -t recura .
#   docker run --rm recura                 # the metrics table
#   docker run --rm recura make validate   # the negative controls behind it
#   docker run --rm recura make ablate     # what each component contributes
#
# No API key is needed for any of that. `fixtures/` ships 870 cached model responses
# keyed by SHA-256 of (model, prompt, payload), so the LLM path replays exactly rather
# than being re-queried. The Anthropic and Google SDKs are imported lazily and are
# therefore NOT installed here - they are needed only to regenerate fixtures.

FROM python:3.13-slim

# PYTHONHASHSEED is pinned because determinism is the whole point of this image.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=0 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VENV=/usr/local/bin

# `make` is the documented entry point; nothing else is needed at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends make \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so editing source does not invalidate the install layer.
COPY pyproject.toml README.md ./
# fastapi + httpx are here only so the build-time test run covers the webhook surface;
# `make eval` itself never imports them.
RUN pip install "pydantic>=2.9" "pyyaml>=6.0" "sqlalchemy>=2.0" "numpy>=1.26" \
    "hypothesis>=6.100" \
                "fastapi>=0.115" "httpx>=0.27" "pytest>=8.0"

COPY . .

# Fail the BUILD rather than ship an image that silently degrades. Without fixtures,
# `make eval` falls back to the rules-only path and ablation 4 becomes meaningless -
# and nothing in the output would say so.
RUN test -n "$(ls fixtures/*.json 2>/dev/null)" || ( \
      echo "ERROR: fixtures/ is empty. make eval would silently fall back to the" && \
      echo "rules-only path. Run 'make fixtures' and commit them before building." && \
      exit 1 )

# Prove the image works at build time, not on the reviewer's machine.
RUN make seed && python -m pytest -q

CMD ["make", "eval"]

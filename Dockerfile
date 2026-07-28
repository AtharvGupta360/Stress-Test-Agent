# Application image for the API and the worker.
# The judge sandbox is a separate, much more restricted image: sandbox/Dockerfile.
FROM python:3.12-slim

# The worker shells out to the docker CLI to manage sandbox containers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY sandbox ./sandbox
COPY migrations ./migrations
COPY scripts ./scripts

RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["python", "-m", "stressagent.api.main"]

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /workspace

COPY pyproject.toml ./
COPY agent ./agent
COPY services/app/runtime/flags.json ./services/app/runtime/flags.json
COPY services/nginx/site.conf ./services/nginx/site.conf

RUN pip install --no-cache-dir .

EXPOSE 8000

CMD ["python", "-m", "agent.core.service"]

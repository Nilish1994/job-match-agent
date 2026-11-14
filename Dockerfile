FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY agent ./agent
RUN pip install --no-cache-dir . && useradd --create-home --uid 1000 agent && mkdir /data && chown agent /data
USER agent
CMD ["python", "-m", "agent", "serve"]

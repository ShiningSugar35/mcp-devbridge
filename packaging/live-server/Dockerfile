FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/src LOCALDEV_MCP_CONFIG_DIR=/state
WORKDIR /app
COPY packaging/live-server/requirements.txt .
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY src ./src
COPY packaging/live-server/run.py ./run.py
USER 65532:65532
ENTRYPOINT ["python", "/app/run.py"]

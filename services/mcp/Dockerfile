FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
ENV PYTHONUNBUFFERED=1 MCP_TRANSPORT=http PORT=8080
CMD ["python", "server.py"]

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
COPY prompts ./prompts
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "run.py"]
CMD ["edition"]

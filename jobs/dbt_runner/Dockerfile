# Build from the repository root:
#   docker build -f jobs/dbt_runner/Dockerfile -t dbt-runner .
FROM python:3.12-slim
RUN pip install --no-cache-dir dbt-core==1.12.6 dbt-bigquery==1.12.1
WORKDIR /dbt
COPY dbt/ /dbt/
RUN dbt deps --profiles-dir /dbt --project-dir /dbt
COPY jobs/dbt_runner/run.py /app/run.py
ENV DBT_TARGET=prod DBT_PROJECT_DIR=/dbt PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "/app/run.py"]
CMD ["hourly"]

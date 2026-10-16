FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1 PORT=8080
# Cloud Run sets PORT; usage statistics are off. Visitors see a generic
# message on an error (details go to the logs). The toolbar is minimal: no
# print, screen recording or developer options (the theme follows the
# visitor's system setting).
CMD streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 \
    --server.headless=true --browser.gatherUsageStats=false \
    --client.showErrorDetails=none --client.toolbarMode=minimal

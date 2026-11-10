FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/ ./bot/
USER 1000:1000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys,time; sys.exit(time.time()-os.path.getmtime('/tmp/heartbeat')>90)"
CMD ["python", "-m", "bot"]

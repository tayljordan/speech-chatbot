FROM python:3.13-slim

WORKDIR /app
RUN pip install --no-cache-dir flask==3.1.3 requests==2.34.2 python-dotenv gunicorn \
    google-genai==2.31.0 google-cloud-firestore==2.34.1
COPY app.py firestore_client.py voice_studio.html login.html ./

CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 180 app:app

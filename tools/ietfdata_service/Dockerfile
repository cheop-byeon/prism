FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml /app/pyproject.toml
COPY ietfdata_service /app/ietfdata_service

RUN pip install --no-cache-dir .

EXPOSE 8010

CMD ["uvicorn", "ietfdata_service.app:app", "--host", "0.0.0.0", "--port", "8010"]
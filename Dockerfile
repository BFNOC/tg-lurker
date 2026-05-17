FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim

WORKDIR /app

ARG APP_VERSION=dev
ARG APP_COMMIT=unknown

ENV APP_VERSION=${APP_VERSION}
ENV APP_COMMIT=${APP_COMMIT}

COPY --from=builder /install /usr/local

COPY . .

EXPOSE 8080

CMD ["python", "main.py"]

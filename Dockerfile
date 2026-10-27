FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_DIR=/etc/ssl/certs

# The Session Pooler presents Supabase Root 2021 CA, which is not in the
# Mozilla/Debian bundle. Install public CAs and pin that extra root so
# sslrootcert=system (verify-full) can succeed without weakening TLS.
COPY certs/supabase-prod-ca-2021.crt certs/supabase-prod-ca-2021.crt.sha256 /tmp/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && (cd /tmp && sha256sum -c supabase-prod-ca-2021.crt.sha256) \
    && install -d -m 0755 /usr/local/share/ai-nature-company \
    && install -m 0644 /tmp/supabase-prod-ca-2021.crt \
        /usr/local/share/ai-nature-company/supabase-prod-ca-2021.crt \
    && install -m 0644 /tmp/supabase-prod-ca-2021.crt \
        /usr/local/share/ca-certificates/supabase-prod-ca-2021.crt \
    && update-ca-certificates \
    && rm -f /tmp/supabase-prod-ca-2021.crt /tmp/supabase-prod-ca-2021.crt.sha256 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.8.3

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY supabase/migrations ./supabase/migrations
RUN uv sync --frozen --no-dev
RUN chown -R 65532:65532 /app && chmod -R u+rwX,go+rX /app

USER 65532:65532
EXPOSE 8000

CMD ["uv", "run", "--no-sync", "ai-nature-http"]

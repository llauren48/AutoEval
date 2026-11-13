FROM docker.1ms.run/library/python@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
WORKDIR /app
COPY backend/wheels /wheels
RUN cd /wheels && sha256sum -c SHA256SUMS && pip install --no-cache-dir --no-index --no-deps /wheels/*.whl && pip check && rm -rf /wheels
COPY backend/migrations /app/migrations
COPY backend/alembic.ini /app/alembic.ini
COPY backend/tool-locks /app/tool-locks
COPY backend/tool-patches /app/tool-patches
USER 10001:10001
CMD ["uvicorn", "autoeval.api:app", "--host", "0.0.0.0", "--port", "8000"]

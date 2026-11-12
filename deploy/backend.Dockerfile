FROM python:3.12-slim
WORKDIR /app
COPY backend/ /app/
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt \
    && pip install --no-cache-dir --require-hashes --no-deps -r tool-locks/planner-requirements.txt \
    && pip install --no-cache-dir --no-deps .
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "autoeval.api:app", "--host", "0.0.0.0", "--port", "8000"]

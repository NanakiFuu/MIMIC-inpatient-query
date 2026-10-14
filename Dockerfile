ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock.txt pyproject.toml ./
COPY src ./src
# Use the lock only as version constraints so runtime deps are installed while
# test-only packages (pytest, httpx2, ...) stay out of the runtime image.
RUN pip install --no-cache-dir -c requirements.lock.txt . \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --create-home app
USER 10001:10001
EXPOSE 8765
CMD ["python", "-m", "uvicorn", "medical_query.api:app", "--host", "0.0.0.0", "--port", "8765", "--no-access-log"]

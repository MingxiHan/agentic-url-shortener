FROM python:3.13.5-slim

WORKDIR /runner
COPY requirements.txt /opt/runner/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/runner/requirements.txt

ENV PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONUNBUFFERED=1
USER 1000:1000
CMD ["python", "-I", "-B", "/runner/harness.py", "greenfield", "4", "32"]

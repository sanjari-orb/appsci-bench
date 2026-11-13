# Creds-free echo artifact fixture for the deploy smoke. The CMD launches the stdlib server bound
# to 0.0.0.0:8080 -- the deployer publishes that port to the host and health-polls it. No pip install:
# server.py is pure stdlib, so the image builds fast and offline.
FROM python:3.12-slim
WORKDIR /app
COPY server.py /app/server.py
EXPOSE 8080
CMD ["python", "/app/server.py"]

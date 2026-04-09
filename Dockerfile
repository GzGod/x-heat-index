FROM python:3.12-slim

# Install Node.js (for npx xapi-to)
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Pre-install xapi-to globally so npx doesn't download every call
RUN npm install -g xapi-to

WORKDIR /opt/tweet-tracker

COPY scripts/ ./

# Default: run the frontend (dashboard)
# Override CMD for tracker or walker via Railway service config
CMD ["python3", "frontend.py"]

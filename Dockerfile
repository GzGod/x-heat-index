FROM python:3.12-slim

# Install Node.js (for npx xapi-to) and Bun (xapi-to runtime dependency)
RUN apt-get update && apt-get install -y curl unzip && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    curl -fsSL https://bun.sh/install | bash && \
    ln -s /root/.bun/bin/bun /usr/local/bin/bun && \
    ln -s /root/.bun/bin/bunx /usr/local/bin/bunx && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Pre-install xapi-to globally so npx doesn't download every call
RUN npm install -g xapi-to

WORKDIR /opt/tweet-tracker

COPY scripts/ ./

# Ensure data dir exists
RUN mkdir -p /opt/tweet-tracker/data

EXPOSE 3301

# Frontend serves dashboard + manages tracker/walker subprocesses
CMD ["python3", "frontend.py"]

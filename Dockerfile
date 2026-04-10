FROM python:3.12-slim

WORKDIR /opt/tweet-tracker

COPY scripts/ ./

RUN mkdir -p /opt/tweet-tracker/data

EXPOSE 3301

CMD ["python3", "frontend.py"]

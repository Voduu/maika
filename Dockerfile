# rclone for the weekly offsite copy, taken from the official image at
# a pinned version. The binary is a static Go build, so it runs on the
# Debian base below.
FROM rclone/rclone:1.76.0 AS rclone

# python:3.14-slim already installs tzdata, so the TZ variable set on
# the container works. TZ is not set here; the container sets it.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY migrations ./migrations
COPY data ./data

# The database, backups and rclone.conf live in /app/var, usually a
# mounted appdata folder. Without a mount, the folder must still be
# writable by any user ID, because Unraid runs the container as
# 99:100 (--user), not as the maika user below.
RUN mkdir /app/var && chmod 1777 /app/var

RUN useradd --create-home --uid 1000 maika
USER maika

CMD ["python", "-m", "maika"]

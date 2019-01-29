FROM alpine:latest
RUN addgroup -S app && adduser -S -G app app
RUN install -d -o app -g app -m 700 /data
RUN apk update && apk upgrade && apk add --no-cache build-base python3 python3-dev py3-cffi git py3-cryptography uwsgi py3-gevent uwsgi-python3
ADD . /app
RUN python3 -mpip install -e /app
USER app

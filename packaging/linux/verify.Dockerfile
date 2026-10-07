FROM debian:bookworm-slim
COPY *.deb /tmp/viewer.deb
RUN apt-get update && apt-get install -y --no-install-recommends /tmp/viewer.deb \
    && rm -rf /var/lib/apt/lists/* /tmp/viewer.deb
COPY *.tar.gz /tmp/portable.tar.gz
COPY verify.sh /verify.sh
CMD ["sh", "/verify.sh"]

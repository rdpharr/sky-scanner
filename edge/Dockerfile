FROM debian:bookworm-slim AS decoder-build
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git build-essential pkg-config python3 libhackrf-dev \
    libusb-1.0-0-dev zlib1g-dev libzstd-dev libsoapysdr-dev \
    libboost-program-options-dev libboost-regex-dev libboost-filesystem-dev \
    && rm -rf /var/lib/apt/lists/*
ARG READSB_COMMIT=3b5368a195e7006a98c428d05560b0b4baffd2ed
ARG DUMP978_COMMIT=f40aa9a0d2d0a0067c0c628b44ba316f7049b8ce
RUN git clone https://github.com/wiedehopf/readsb.git /build/readsb \
    && cd /build/readsb && git checkout "$READSB_COMMIT"
COPY decoder /patch
RUN python3 /patch/patch_readsb.py /build/readsb \
    && make -C /build/readsb -j4 HACKRF=yes DISABLE_INTERACTIVE=yes
RUN git clone https://github.com/flightaware/dump978.git /build/dump978 \
    && cd /build/dump978 && git checkout "$DUMP978_COMMIT" \
    && make -j4 dump978-fa

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    libhackrf0 libusb-1.0-0 libzstd1 zlib1g libsoapysdr0.8 soapysdr0.8-module-hackrf \
    libboost-program-options1.74.0 libboost-regex1.74.0 libboost-filesystem1.74.0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=decoder-build /build/readsb/readsb /usr/local/bin/readsb
COPY --from=decoder-build /build/dump978/dump978-fa /usr/local/bin/dump978-fa
COPY --from=decoder-build /build/readsb/LICENSE /usr/share/doc/readsb/LICENSE
COPY --from=decoder-build /build/dump978/LICENSE /usr/share/doc/dump978/LICENSE
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY sky_scanner ./sky_scanner
COPY sql ./sql
COPY tests ./tests
COPY decoder ./decoder
ENV PYTHONUNBUFFERED=1
CMD ["python", "-m", "sky_scanner.ingest"]

FROM freqtradeorg/freqtrade:stable

USER root

# Fetch the latest NostalgiaForInfinity strategy directly from the official repo
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && git clone --depth 1 https://github.com/iterativv/NostalgiaForInfinity.git /tmp/nfi \
    && mkdir -p /freqtrade/user_data_seed/strategies \
    && cp /tmp/nfi/NostalgiaForInfinityX7.py /freqtrade/user_data_seed/strategies/ \
    && rm -rf /tmp/nfi \
    && apt-get purge -y git && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

COPY user_data_seed/config.json /freqtrade/user_data_seed/config.json
COPY entrypoint.sh /freqtrade/entrypoint.sh
RUN chmod +x /freqtrade/entrypoint.sh

ENTRYPOINT []
CMD ["/freqtrade/entrypoint.sh"]

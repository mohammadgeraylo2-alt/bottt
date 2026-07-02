FROM freqtradeorg/freqtrade:stable

COPY user_data_seed /freqtrade/user_data_seed
COPY entrypoint.sh /freqtrade/entrypoint.sh
USER root
RUN chmod +x /freqtrade/entrypoint.sh

ENTRYPOINT []
CMD ["/freqtrade/entrypoint.sh"]

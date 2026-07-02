FROM freqtradeorg/freqtrade:stable

COPY user_data /freqtrade/user_data

CMD ["trade", "--config", "/freqtrade/user_data/config.json", "--strategy", "SampleStrategy"]

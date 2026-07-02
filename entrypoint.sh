#!/bin/bash
set -e

if [ ! -f /freqtrade/user_data/config.json ]; then
    echo "No config.json found in volume — seeding default files..."
    cp -r /freqtrade/user_data_seed/. /freqtrade/user_data/
fi

exec freqtrade trade --config /freqtrade/user_data/config.json --strategy SampleStrategy

#!/bin/bash
set -e

if [ ! -f /freqtrade/user_data/config.json ]; then
    echo "No config.json found in volume — seeding default files..."
    cp -r /freqtrade/user_data_seed/. /freqtrade/user_data/
fi

# The volume is owned by root; freqtrade must run as ftuser.
chown -R ftuser:ftuser /freqtrade/user_data

exec su ftuser -c "freqtrade trade --config /freqtrade/user_data/config.json --strategy SampleStrategy"

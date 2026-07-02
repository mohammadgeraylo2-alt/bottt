from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class SampleStrategy(IStrategy):
    """
    Simple starter strategy for testing the Railway deployment pipeline.
    Entry: RSI oversold + price below lower Bollinger Band
    Exit: RSI overbought + price above upper Bollinger Band
    This is for DRY-RUN testing only, not financial advice.
    """

    timeframe = "1h"
    minimal_roi = {
        "0": 0.05,
        "30": 0.03,
        "60": 0.02,
        "120": 0.01
    }
    stoploss = -0.10
    trailing_stop = False
    startup_candle_count = 30

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)

        bollinger = ta.BBANDS(dataframe, timeperiod=20, nbdevup=2, nbdevdn=2)
        dataframe["bb_lowerband"] = bollinger["lowerband"]
        dataframe["bb_upperband"] = bollinger["upperband"]

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] < 30) &
                (dataframe["close"] < dataframe["bb_lowerband"]) &
                (dataframe["volume"] > 0)
            ),
            "enter_long"] = 1

        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe.loc[
            (
                (dataframe["rsi"] > 70) &
                (dataframe["close"] > dataframe["bb_upperband"]) &
                (dataframe["volume"] > 0)
            ),
            "exit_long"] = 1

        return dataframe

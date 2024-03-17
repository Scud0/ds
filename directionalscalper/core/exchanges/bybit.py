import os
import logging
import time
import ta as ta
import uuid
import ccxt
import pandas as pd
import json
import requests, hmac, hashlib
import urllib.parse
import threading
import traceback
from typing import Optional, Tuple, List
from ccxt.base.errors import RateLimitExceeded
from ..strategies.logger import Logger
from requests.exceptions import HTTPError
from datetime import datetime, timedelta
from ccxt.base.errors import NetworkError

logging = Logger(logger_name="Exchange", filename="Exchange.log", stream=True)

""""   
#bybit exchange specific code#

cccxt file with capabilities
https://github.com/ccxt/ccxt/blob/master/python/ccxt/bybit.py

"""
class Exchange:
    # Shared class-level cache variables
    open_positions_shared_cache = None
    last_open_positions_time_shared = None
    open_positions_semaphore = threading.Semaphore()
    class Bybit:
        def __init__(self, parent):
            self.parent = parent  # Refers to the outer Exchange instance
            self.max_retries = 100  # Maximum retries for rate-limited requests
            self.retry_wait = 5  # Seconds to wait between retries

        def get_open_orders(self, symbol):
            """Fetches open orders for the given symbol."""
            for _ in range(self.max_retries):
                try:
                    open_orders = self.parent.fetch_open_orders(symbol)
                    logging.info(f"Open orders {open_orders}")
                    return open_orders
                except ccxt.RateLimitExceeded:
                    logging.warning(f"Rate limit exceeded when fetching open orders for {symbol}. Retrying in {self.retry_wait} seconds...")
                    time.sleep(self.retry_wait)
            logging.error(f"Failed to fetch open orders for {symbol} after {self.max_retries} retries.")
            return []
        
        def get_open_tp_orders(self, symbol):
            long_tp_orders = []
            short_tp_orders = []
            for _ in range(self.max_retries):
                try:
                    all_open_orders = self.parent.exchange.fetch_open_orders(symbol)
                    #logging.info(f"All open orders for {symbol}: {all_open_orders}")
                    
                    for order in all_open_orders:
                        order_details = {
                            'id': order['id'],
                            'qty': float(order['info']['qty']),
                            'price': float(order['price'])  # Extracting the price
                        }
                        
                        if order['info'].get('reduceOnly', False):
                            if order['side'] == 'sell':
                                long_tp_orders.append(order_details)
                            elif order['side'] == 'buy':
                                short_tp_orders.append(order_details)
                    
                    return long_tp_orders, short_tp_orders
                except ccxt.RateLimitExceeded:
                    logging.warning(f"Rate limit exceeded when fetching TP orders for {symbol}. Retrying in {self.retry_wait} seconds...")
                    time.sleep(self.retry_wait)
            logging.error(f"Failed to fetch TP orders for {symbol} after {self.max_retries} retries.")
            return long_tp_orders, short_tp_orders
        
        def get_open_tp_order_count(self, symbol):
            """
            Fetches the count of open take profit (TP) orders for the given symbol.
            
            :param str symbol: The trading pair symbol.
            :return: Dictionary with counts of long and short TP orders for the symbol.
            """
            long_tp_orders, short_tp_orders = self.get_open_tp_orders(symbol)
            return {
                'long_tp_count': len(long_tp_orders),
                'short_tp_count': len(short_tp_orders)
            }

        def get_open_take_profit_orders(self, symbol, side):
            """
            Fetches open take profit orders for the given symbol and side.
            
            :param str symbol: The trading pair symbol.
            :param str side: The side ("buy" or "sell") of the TP orders to fetch.
            :return: A list of take profit order structures.
            """
            # First, fetch the open orders
            response = self.parent.exchange.get_open_orders(symbol)
            
            # Filter the orders for take profits (reduceOnly) and the specified side
            tp_orders = [
                order for order in response
                if order.get('info', {}).get('reduceOnly', False) and order.get('side', '').lower() == side.lower()
            ]
            
            # If necessary, you can further parse the orders here using self.parse_order or similar methods
            
            return tp_orders


    def __init__(self, exchange_id, api_key, secret_key, passphrase=None, market_type='swap'):
        self.order_timestamps = None
        self.exchange_id = exchange_id
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.market_type = market_type  # Store the market type
        self.name = exchange_id
        self.initialise()
        self.symbols = self._get_symbols()
        self.market_precisions = {}
        self.open_positions_cache = None
        self.last_open_positions_time = None

        self.entry_order_ids = {}  # Initialize order history
        self.entry_order_ids_lock = threading.Lock()  # For thread safety

        self.bybit = self.Bybit(self)

    def initialise(self):
        exchange_class = getattr(ccxt, self.exchange_id)
        exchange_params = {
            "apiKey": self.api_key,
            "secret": self.secret_key,
            "enableRateLimit": True,
        }
        if os.environ.get('HTTP_PROXY') and os.environ.get('HTTPS_PROXY'):
            exchange_params["proxies"] = {
                'http': os.environ.get('HTTP_PROXY'),
                'https': os.environ.get('HTTPS_PROXY'),
            }
        if self.passphrase:
            exchange_params["password"] = self.passphrase

        # Set the defaultType based on the market_type parameter
        # Adjusting the defaultType specifically for Bybit Spot
        exchange_params['options'] = {
            'defaultType': 'spot' if self.exchange_id.lower() == 'bybit_spot' else self.market_type,
            'adjustForTimeDifference': True,
        }

        # Add the brokerId option only for Bybit exchanges
        if self.exchange_id.lower().startswith('bybit'):
            exchange_params['options']['brokerId'] = 'Nu000450'

        # Existing condition for Huobi
        if self.exchange_id.lower() == 'huobi' and self.market_type == 'swap':
            exchange_params['options']['defaultSubType'] = 'linear'

        # Initializing the exchange object
        self.exchange = exchange_class(exchange_params)
        
    
    def transfer_funds_bybit(self, code: str, amount: float, from_account: str, to_account: str, params={}):
        """
        Transfer funds between different account types under the same UID.

        :param str code: Unified currency code
        :param float amount: Amount to transfer
        :param str from_account: From account type (e.g., 'UNIFIED', 'CONTRACT')
        :param str to_account: To account type (e.g., 'SPOT', 'CONTRACT')
        :param dict params: Extra parameters specific to the exchange API endpoint
        :return: A transfer structure
        """
        try:
            # Generate a unique transfer ID (UUID)
            transfer_id = str(uuid.uuid4())

            # Add the transfer ID to the params dictionary
            params['transferId'] = transfer_id

            # Use CCXT's transfer function to initiate the internal transfer
            transfer = self.exchange.transfer(code, amount, from_account, to_account, params)

            if transfer:
                logging.info(f"Funds transfer successful. Details: {transfer}")
                return transfer
            else:
                logging.error(f"Error occurred during funds transfer.")
                return None

        except Exception as e:
            logging.error(f"Error occurred during funds transfer: {e}")
            return None
        
    def update_order_history(self, symbol, order_id, timestamp):
        with self.entry_order_ids_lock:
            # Check if the symbol is already in the order history
            if symbol not in self.entry_order_ids:
                self.entry_order_ids[symbol] = []
                logging.info(f"Creating new order history entry for symbol: {symbol}")

            # Append the new order data
            self.entry_order_ids[symbol].append({'id': order_id, 'timestamp': timestamp})
            logging.info(f"Updated order history for {symbol} with order ID {order_id} at timestamp {timestamp}")

            # Optionally, log the entire current order history for the symbol
            logging.debug(f"Current order history for {symbol}: {self.entry_order_ids[symbol]}")
            
    def set_order_timestamps(self, order_timestamps):
        self.order_timestamps = order_timestamps

    def populate_order_history(self, symbols: list, since: int = None, limit: int = 100):
        for symbol in symbols:
            try:
                logging.info(f"Fetching trades for {symbol}")
                recent_trades = self.exchange.fetch_trades(symbol, since=since, limit=limit)

                # Check if recent_trades is None or empty
                if not recent_trades:
                    logging.warning(f"No trade data returned for {symbol}. It might not be a valid symbol or no recent trades.")
                    continue

                last_trade = recent_trades[-1]
                last_trade_time = datetime.fromtimestamp(last_trade['timestamp'] / 1000)  # Convert ms to seconds

                if symbol not in self.order_timestamps:
                    self.order_timestamps[symbol] = []
                self.order_timestamps[symbol].append(last_trade_time)

                logging.info(f"Updated order timestamps for {symbol} with last trade at {last_trade_time}")

            except Exception as e:
                logging.error(f"Exception occurred while processing trades for {symbol}: {e}")

    def _get_symbols(self):
        while True:
            try:
                #self.exchange.set_sandbox_mode(True)
                markets = self.exchange.load_markets()
                symbols = [market['symbol'] for market in markets.values()]
                return symbols
            except ccxt.errors.RateLimitExceeded as e:
                logging.info(f"Rate limit exceeded: {e}, retrying in 10 seconds...")
                time.sleep(10)
            except Exception as e:
                logging.info(f"An error occurred while fetching symbols: {e}, retrying in 10 seconds...")
                time.sleep(10)

    def get_ohlc_data(self, symbol, timeframe='1H', since=None, limit=None):
        """
        Fetches historical OHLC data for the given symbol and timeframe using ccxt's fetch_ohlcv method.
        
        :param str symbol: Symbol of the market to fetch OHLCV data for.
        :param str timeframe: The length of time each candle represents.
        :param int since: Timestamp in ms of the earliest candle to fetch.
        :param int limit: The maximum amount of candles to fetch.
        
        :return: List of OHLCV data.
        """
        ohlc_data = self.fetch_ohlcv(symbol, timeframe, since, limit)
        
        # Parsing the data to a more friendly format (optional)
        parsed_data = []
        for entry in ohlc_data:
            timestamp, open_price, high, low, close_price, volume = entry
            parsed_data.append({
                'timestamp': timestamp,
                'open': open_price,
                'high': high,
                'low': low,
                'close': close_price,
                'volume': volume
            })

        return parsed_data
    
    def calculate_max_trade_quantity(self, symbol, leverage, wallet_exposure, best_ask_price):
        # Fetch necessary data from the exchange
        market_data = self.get_market_data_bybit(symbol)
        dex_equity = self.get_balance_bybit('USDT')

        # Calculate the max trade quantity based on leverage and equity
        max_trade_qty = round(
            (float(dex_equity) * wallet_exposure / float(best_ask_price))
            / (100 / leverage),
            int(float(market_data['min_qty'])),
        )

        return max_trade_qty
    
    def get_market_tick_size_bybit(self, symbol):
        # Fetch the market data
        markets = self.exchange.fetch_markets()

        # Filter for the specific symbol
        for market in markets:
            if market['symbol'] == symbol:
                tick_size = market['info']['priceFilter']['tickSize']
                return tick_size
        
        return None

    def fetch_leverage_tiers(self, symbol: str) -> dict:
        """
        Fetch leverage tiers for a given symbol using CCXT's fetch_market_leverage_tiers method.

        :param symbol: The trading symbol to fetch leverage tiers for.
        :return: A dictionary containing leverage tiers information if successful, None otherwise.
        """
        try:
            params = {'category': 'linear'}  # Adjust parameters based on the specific needs and API documentation
            leverage_tiers = self.exchange.fetch_derivatives_market_leverage_tiers(symbol, params)
            return leverage_tiers
        except Exception as e:
            logging.error(f"Error fetching leverage tiers for {symbol}: {e}")
            return None
        
    def cancel_order_bybit(self, order_id, symbol):
        """
        Wrapper function to cancel an order on the exchange using the CCXT instance.

        :param order_id: The ID of the order to cancel.
        :param symbol: The trading symbol of the market the order was made in.
        :return: The response from the exchange after attempting to cancel the order.
        """
        try:
            # Call the cancel_order method of the ccxt instance
            response = self.exchange.cancel_order(order_id, symbol)
            logging.info(f"Order {order_id} for {symbol} cancelled successfully.")
            return response
        except Exception as e:
            logging.error(f"An error occurred while cancelling order {order_id} for {symbol}: {str(e)}")
            # Handle the exception as needed (e.g., retry, raise, etc.)
            return None
        
    def get_precision_and_limits_bybit(self, symbol):
        # Fetch the market data
        markets = self.exchange.fetch_markets()

        # Filter for the specific symbol
        for market in markets:
            if market['symbol'] == symbol:
                precision_amount = market['precision']['amount']
                precision_price = market['precision']['price']
                min_amount = market['limits']['amount']['min']

                return precision_amount, precision_price, min_amount

        return None, None, None

    def get_symbol_precision_bybit(self, symbol):
        try:
            # Use fetch_markets to retrieve data for all markets
            all_markets = self.exchange.fetch_markets()

            # Find the market data for the specific symbol
            market_data = next((market for market in all_markets if market['id'] == symbol), None)

            if market_data:
                # Extract precision data
                amount_precision = market_data['precision']['amount']
                price_precision = market_data['precision']['price']
                return amount_precision, price_precision
            else:
                print(f"Market data not found for {symbol}")
                return None, None
        except Exception as e:
            print(f"An error occurred: {e}")
            return None, None

    def get_market_precision_data_bybit(self, symbol):
        # Fetch the market data
        markets = self.exchange.fetch_markets()
        
        # Print the first market from the list
        logging.info(markets[0])

        # Filter for the specific symbol
        for market in markets:
            if market['symbol'] == symbol:
                return market['precision']
        
        return None

    # Bybit
    def calculate_trade_quantity(self, symbol, leverage, asset_wallet_exposure, best_ask_price):
        dex_equity = self.get_balance_bybit('USDT')
        asset_exposure = dex_equity * asset_wallet_exposure / 100.0
        trade_qty = asset_exposure / float(best_ask_price) / leverage
        return trade_qty

    # Bybit
    def print_trade_quantities_bybit(self, max_trade_qty, leverage_sizes, wallet_exposure, best_ask_price):
        sorted_leverage_sizes = sorted(leverage_sizes)  # Sort leverage sizes in ascending order

        for leverage in sorted_leverage_sizes:
            trade_qty = max_trade_qty * leverage  # Calculate trade quantity based on leverage
            print(f"Leverage: {leverage}x, Trade Quantity: {trade_qty}")

    # Bybit calc lot size based on spread
    def spread_based_entry_size_bybit(self, symbol, spread, min_order_qty):
        current_price = self.get_current_price(symbol)
        logging.info(f"Current price debug: {current_price}")
        entry_amount = min_order_qty + (spread * current_price) / 100

        return entry_amount

    def debug_derivatives_positions(self, symbol):
        try:
            positions = self.exchange.fetch_derivatives_positions([symbol])
            logging.info(f"Debug positions: {positions}")
        except Exception as e:
            logging.info(f"Exception in debug derivs func: {e}")

    def debug_derivatives_markets_bybit(self):
        try:
            markets = self.exchange.fetch_derivatives_markets({'category': 'linear'})
            logging.info(f"Debug markets: {markets}")
        except Exception as e:
            logging.info(f"Exception in debug_derivatives_markets_bybit: {e}")

    # Bybit
    def bybit_fetch_precision(self, symbol):
        try:
            markets = self.exchange.fetch_derivatives_markets()
            for market in markets['result']['list']:
                if market['symbol'] == symbol:
                    qty_step = market['lotSizeFilter']['qtyStep']
                    self.market_precisions[symbol] = {'amount': float(qty_step)}
                    break
        except Exception as e:
            logging.info(f"Exception in bybit_fetch_precision: {e}")

    # v5
    def get_max_lev_bybit(self, symbol):
        try:
            # Fetch leverage tiers for the symbol
            leverage_tiers = self.exchange.fetch_market_leverage_tiers(symbol)

            # Process leverage tiers to find the maximum leverage
            max_leverage = max([tier['maxLeverage'] for tier in leverage_tiers if 'maxLeverage' in tier])
            logging.info(f"Maximum leverage for symbol {symbol}: {max_leverage}")

            return max_leverage

        except Exception as e:
            logging.error(f"Error retrieving leverage tiers for {symbol}: {e}")
            return None

    # v5
    def get_current_max_leverage_bybit(self, symbol):
        try:
            # Fetch leverage tiers for the symbol
            leverage_tiers = self.exchange.fetch_market_leverage_tiers(symbol)

            # Process leverage tiers to find the maximum leverage
            max_leverage = max([tier['maxLeverage'] for tier in leverage_tiers if 'maxLeverage' in tier])
            logging.info(f"Maximum leverage for symbol {symbol}: {max_leverage}")

            return max_leverage

        except Exception as e:
            logging.error(f"Error retrieving leverage tiers for {symbol}: {e}")
            return None

    # v5    
    def set_symbol_to_cross_margin(self, symbol, leverage):
        """
        Set a specific symbol's margin mode to cross with specified leverage.
        """
        try:
            response = self.exchange.set_margin_mode('cross', symbol=symbol, params={'leverage': leverage})
            
            retCode = response.get('retCode') if isinstance(response, dict) else None

            if retCode == 110026:  # Margin mode is already set to cross
                logging.info(f"Symbol {symbol} is already set to cross margin mode. No changes made.")
                return {"status": "unchanged", "response": response}
            else:
                logging.info(f"Margin mode set to cross for symbol {symbol} with leverage {leverage}. Response: {response}")
                return {"status": "changed", "response": response}

        except Exception as e:
            logging.info(f"Failed to set margin mode or margin mode already set to cross for symbol {symbol} with leverage {leverage}: {e}")
            return {"status": "error", "message": str(e)}

    # Bybit
    def set_leverage_bybit(self, leverage, symbol):
        try:
            self.exchange.set_leverage(leverage, symbol)
            logging.info(f"Leverage set to {leverage} for symbol {symbol}")
        except Exception as e:
            logging.info(f"Error setting leverage: {e}")

    # Bybit
    def setup_exchange_bybit(self, symbol) -> None:
        values = {"position": False, "leverage": False}
        try:
            # Set the position mode to hedge
            self.exchange.set_position_mode(hedged=True, symbol=symbol)
            values["position"] = True
        except Exception as e:
            logging.info(f"An unknown error occurred in with set_position_mode: {e}")

        # market_data = self.get_market_data_bybit(symbol=symbol)
        # try:
        #     # Set the margin mode to cross
        #     self.exchange.set_derivatives_margin_mode(marginMode="cross", symbol=symbol)

        # except Exception as e:
        #     logging.info(f"An unknown error occurred in with set_derivatives_margin_mode: {e}")

        # log.info(values)

    def parse_trading_fee(self, fee_data):
        maker_fee = float(fee_data.get('makerFeeRate', '0'))
        taker_fee = float(fee_data.get('takerFeeRate', '0'))
        return {
            'maker_fee': maker_fee,
            'taker_fee': taker_fee
        }
    
    # Bybit
    def get_market_data_bybit(self, symbol: str) -> dict:
        values = {"precision": 0.0, "leverage": 0.0, "min_qty": 0.0}
        try:
            self.exchange.load_markets()
            symbol_data = self.exchange.market(symbol)
            
            #print("Symbol data:", symbol_data)  # Debug print
            
            if "info" in symbol_data:
                values["precision"] = symbol_data["precision"]["price"]
                values["min_qty"] = symbol_data["limits"]["amount"]["min"]

            # Fetch positions
            positions = self.exchange.fetch_positions()

            for position in positions:
                if position['symbol'] == symbol:
                    values["leverage"] = float(position['leverage'])

        # except Exception as e:
        #     logging.info(f"An unknown error occurred in get_market_data_bybit(): {e}")
        #     logging.info(f"Call Stack: {traceback.format_exc()}")
        except Exception as e:
            logging.info(f"An unknown error occurred in get_market_data_bybit(): {e}")
        return values

    # Bybit
    def fetch_recent_trades(self, symbol, since=None, limit=100):
        """
        Fetch recent trades for a given symbol.

        :param str symbol: The trading pair symbol.
        :param int since: Timestamp in milliseconds for fetching trades since this time.
        :param int limit: The maximum number of trades to fetch.
        :return: List of recent trades.
        """
        try:
            # Ensure the markets are loaded
            self.exchange.load_markets()

            # Fetch trades using ccxt
            trades = self.exchange.fetch_trades(symbol, since=since, limit=limit)
            return trades
        except Exception as e:
            logging.error(f"Error fetching recent trades for {symbol}: {e}")
            return []
        
    def fetch_trades(self, symbol: str, since: int = None, limit: int = None, params={}):
        """
        Get the list of most recent trades for a particular symbol.
        :param str symbol: Unified symbol of the market to fetch trades for.
        :param int since: Timestamp in ms of the earliest trade to fetch.
        :param int limit: The maximum amount of trades to fetch.
        :param dict params: Extra parameters specific to the Bybit API endpoint.
        :returns: A list of trade structures.
        """
        try:
            return self.exchange.fetch_trades(symbol, since=since, limit=limit, params=params)
        except Exception as e:
            logging.error(f"Error fetching trades for {symbol}: {e}")
            return []

    # Bybit
    def get_balance_bybit_unified(self, quote):
        if self.exchange.has['fetchBalance']:
            # Fetch the balance
            balance = self.exchange.fetch_balance()

            # Find the quote balance
            unified_balance = balance.get('USDT', {})
            total_balance = unified_balance.get('total', None)
            
            if total_balance is not None:
                return float(total_balance)

        return None

    # Bybit regular and unified
    # def get_balance_bybit(self, quote):
    #     if self.exchange.has['fetchBalance']:
    #         # Fetch the balance
    #         balance_response = self.exchange.fetch_balance()

    #         # Check if it's the unified structure
    #         if 'result' in balance_response:
    #             coin_list = balance_response.get('result', {}).get('coin', [])
    #             for currency_balance in coin_list:
    #                 if currency_balance['coin'] == quote:
    #                     return float(currency_balance['equity'])
    #         # If not unified, handle the old structure
    #         elif 'info' in balance_response:
    #             for currency_balance in balance_response['info']['result']['list']:
    #                 if currency_balance['coin'] == quote:
    #                     return float(currency_balance['equity'])

    #     return None

    def retry_api_call(self, function, *args, max_retries=100, delay=10, **kwargs):
        for i in range(max_retries):
            try:
                return function(*args, **kwargs)
            except Exception as e:
                logging.info(f"Error occurred during API call: {e}. Retrying in {delay} seconds...")
                time.sleep(delay)
        raise Exception(f"Failed to execute the API function after {max_retries} retries.")

## v5

    def get_balance_bybit_spot(self, quote):
        if self.exchange.has['fetchBalance']:
            try:
                # Specify the type as 'spot' for spot trading
                balance_response = self.exchange.fetch_balance({'type': 'spot'})

                # Logging the raw response for debugging might be useful
                # logging.info(f"Raw balance response from Bybit: {balance_response}")

                # Parse the balance for the quote currency
                if quote in balance_response['total']:
                    total_balance = balance_response['total'][quote]
                    return total_balance
                else:
                    logging.warning(f"Balance for {quote} not found in the response.")
            except Exception as e:
                logging.error(f"Error fetching balance from Bybit: {e}")

        return None

## v5
    def get_balance_bybit(self, quote):
        if self.exchange.has['fetchBalance']:
            try:
                # Fetch the balance with params to specify the account type if needed
                balance_response = self.exchange.fetch_balance({'type': 'swap'})

                # Log the raw response for debugging purposes
                #logging.info(f"Raw balance response from Bybit: {balance_response}")

                # Parse the balance
                if quote in balance_response['total']:
                    total_balance = balance_response['total'][quote]
                    return total_balance
                else:
                    logging.warning(f"Balance for {quote} not found in the response.")
            except Exception as e:
                logging.error(f"Error fetching balance from Bybit: {e}")

        return None

    def get_available_balance_bybit(self, quote):
        if self.exchange.has['fetchBalance']:
            try:
                # Fetch the balance with params to specify the account type
                balance_response = self.exchange.fetch_balance({'type': 'swap'})

                # Log the raw response for debugging purposes
                #logging.info(f"Raw available balance response from Bybit: {balance_response}")

                # Check for the required keys in the response
                if 'free' in balance_response and quote in balance_response['free']:
                    # Return the available balance for the specified currency
                    return float(balance_response['free'][quote])
                else:
                    logging.warning(f"Available balance for {quote} not found in the response.")

            except Exception as e:
                logging.error(f"Error fetching available balance from Bybit: {e}")

        return None
    
    def fetch_unrealized_pnl(self, symbol):
        """
        Fetches the unrealized profit and loss (PNL) for both long and short positions of a given symbol.

        :param symbol: The trading pair symbol.
        :return: A dictionary containing the unrealized PNL for long and short positions.
                The dictionary has keys 'long' and 'short' with corresponding PNL values.
                Returns None for a position if it's not open or the key is not present.
        """
        # Fetch positions for the symbol
        response = self.exchange.fetch_positions([symbol])
        logging.info(f"Response from unrealized pnl: {response}")
        unrealized_pnl = {'long': None, 'short': None}

        # Loop through each position in the response
        for pos in response:
            side = pos['info'].get('side', '').lower()
            pnl = pos['info'].get('unrealisedPnl')
            if pnl is not None:
                pnl = float(pnl)
                if side == 'buy':  # Long position
                    unrealized_pnl['long'] = pnl
                elif side == 'sell':  # Short position
                    unrealized_pnl['short'] = pnl

        return unrealized_pnl  


    def get_precision_ultimate_bybit(self, symbol: str) -> Tuple[int, int]:
        try:
            market = self.exchange.market(symbol)

            smallest_increment_price = market['precision']['price']
            price_precision = len(str(smallest_increment_price).split('.')[-1])

            quantity_precision = int(market['precision']['amount'])

            return price_precision, quantity_precision
        except Exception as e:
            print(f"An error occurred: {e}")
            return None, None


    # def get_symbol_precision_bybit(self, symbol: str) -> Tuple[int, int]:
    #     try:
    #         market = self.exchange.market(symbol)
    #         price_precision = int(market['precision']['price'])
    #         quantity_precision = int(market['precision']['amount'])
    #         return price_precision, quantity_precision
    #     except Exception as e:
    #         print(f"An error occurred: {e}")
    #         return None, None

    def get_price_precision(self, symbol):
        market = self.exchange.market(symbol)
        smallest_increment = market['precision']['price']
        price_precision = len(str(smallest_increment).split('.')[-1])
        return price_precision

    def get_precision_bybit(self, symbol):
        markets = self.exchange.fetch_derivatives_markets()
        for market in markets:
            if market['symbol'] == symbol:
                return market['precision']
        return None


    def get_balance(self, quote: str) -> dict:
        values = {
            "available_balance": 0.0,
            "pnl": 0.0,
            "upnl": 0.0,
            "wallet_balance": 0.0,
            "equity": 0.0,
        }
        try:
            data = self.exchange.fetch_balance()
            if "info" in data:
                if "result" in data["info"]:
                    if quote in data["info"]["result"]:
                        values["available_balance"] = float(
                            data["info"]["result"][quote]["available_balance"]
                        )
                        values["pnl"] = float(
                            data["info"]["result"][quote]["realised_pnl"]
                        )
                        values["upnl"] = float(
                            data["info"]["result"][quote]["unrealised_pnl"]
                        )
                        values["wallet_balance"] = round(
                            float(data["info"]["result"][quote]["wallet_balance"]), 2
                        )
                        values["equity"] = round(
                            float(data["info"]["result"][quote]["equity"]), 2
                        )
        except Exception as e:
            logging.info(f"An unknown error occurred in get_balance(): {e}")
        return values
    
    # Universal

    def fetch_ohlcv(self, symbol, timeframe='1d', limit=None):
        """
        Fetch OHLCV data for the given symbol and timeframe.

        :param symbol: Trading symbol.
        :param timeframe: Timeframe string.
        :param limit: Limit the number of returned data points.
        :return: DataFrame with OHLCV data.
        """
        try:
            # Fetch the OHLCV data from the exchange
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)  # Pass the limit parameter

            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            df.set_index('timestamp', inplace=True)

            return df

        except ccxt.BaseError as e:
            print(f'Failed to fetch OHLCV data: {e}')
            return pd.DataFrame()

    def get_orderbook(self, symbol, max_retries=3, retry_delay=5) -> dict:
        values = {"bids": [], "asks": []}

        for attempt in range(max_retries):
            try:
                data = self.exchange.fetch_order_book(symbol)
                if "bids" in data and "asks" in data:
                    if len(data["bids"]) > 0 and len(data["asks"]) > 0:
                        if len(data["bids"][0]) > 0 and len(data["asks"][0]) > 0:
                            values["bids"] = data["bids"]
                            values["asks"] = data["asks"]
                break  # if the fetch was successful, break out of the loop

            except HTTPError as http_err:
                print(f"HTTP error occurred: {http_err} - {http_err.response.text}")

                if "Too many visits" in str(http_err) or (http_err.response.status_code == 429):
                    if attempt < max_retries - 1:
                        delay = retry_delay * (attempt + 1)  # Variable delay
                        logging.info(f"Rate limit error in get_orderbook(). Retrying in {delay} seconds...")
                        time.sleep(delay)
                        continue
                else:
                    logging.error(f"HTTP error in get_orderbook(): {http_err.response.text}")
                    raise http_err

            except Exception as e:
                if attempt < max_retries - 1:  # if not the last attempt
                    logging.info(f"An unknown error occurred in get_orderbook(): {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logging.error(f"Failed to fetch order book after {max_retries} attempts: {e}")
                    raise e  # If it's still failing after max_retries, re-raise the exception.

        return values
  
      # Bybit
    def get_best_bid_ask_bybit(self, symbol):
        orderbook = self.exchange.get_orderbook(symbol)
        try:
            best_ask_price = orderbook['asks'][0][0]
        except IndexError:
            best_ask_price = None
        try:
            best_bid_price = orderbook['bids'][0][0]
        except IndexError:
            best_bid_price = None

        return best_bid_price, best_ask_price

    # Bybit 
    def get_positions_bybit(self, symbol, max_retries=100, retry_delay=5) -> dict:
        values = {
            "long": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
            "short": {
                "qty": 0.0,
                "price": 0.0,
                "realised": 0,
                "cum_realised": 0,
                "upnl": 0,
                "upnl_pct": 0,
                "liq_price": 0,
                "entry_price": 0,
            },
        }

        for i in range(max_retries):
            try:
                data = self.exchange.fetch_positions(symbol)
                if len(data) == 2:
                    sides = ["long", "short"]
                    for side in [0, 1]:
                        values[sides[side]]["qty"] = float(data[side]["contracts"])
                        values[sides[side]]["price"] = float(data[side]["entryPrice"] or 0)
                        values[sides[side]]["realised"] = round(float(data[side]["info"]["unrealisedPnl"] or 0), 4)
                        values[sides[side]]["cum_realised"] = round(float(data[side]["info"]["cumRealisedPnl"] or 0), 4)
                        values[sides[side]]["upnl"] = round(float(data[side]["info"]["unrealisedPnl"] or 0), 4)
                        values[sides[side]]["upnl_pct"] = round(float(data[side]["percentage"] or 0), 4)
                        values[sides[side]]["liq_price"] = float(data[side]["liquidationPrice"] or 0)
                        values[sides[side]]["entry_price"] = float(data[side]["entryPrice"] or 0)
                break  # If the fetch was successful, break out of the loop
            except Exception as e:
                if i < max_retries - 1:  # If not the last attempt
                    logging.info(f"An unknown error occurred in get_positions_bybit(): {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logging.info(f"Failed to fetch positions after {max_retries} attempts: {e}")
                    raise e  # If it's still failing after max_retries, re-raise the exception.

        return values

    def get_all_open_orders_bybit(self):
        """
        Fetch all open orders for all symbols from the Bybit API.
        
        :return: A list of open orders for all symbols.
        """
        try:
            all_open_orders = self.exchange.fetch_open_orders()
            return all_open_orders
        except Exception as e:
            print(f"An error occurred while fetching all open orders: {e}")
            return []

    def get_all_open_positions_bybit(self, retries=10, delay_factor=10, max_delay=60) -> List[dict]:
        now = datetime.now()

        # Check if the shared cache is still valid
        cache_duration = timedelta(seconds=30)  # Cache duration is 30 seconds
        if self.open_positions_shared_cache and self.last_open_positions_time_shared and now - self.last_open_positions_time_shared < cache_duration:
            return self.open_positions_shared_cache

        # Using a semaphore to limit concurrent API requests
        with self.open_positions_semaphore:
            # Double-checking the cache inside the semaphore to ensure no other thread has refreshed it in the meantime
            if self.open_positions_shared_cache and self.last_open_positions_time_shared and now - self.last_open_positions_time_shared < cache_duration:
                return self.open_positions_shared_cache

            for attempt in range(retries):
                try:
                    all_positions = self.exchange.fetch_positions() 
                    open_positions = [position for position in all_positions if float(position.get('contracts', 0)) != 0] 

                    # Update the shared cache with the new data
                    self.open_positions_shared_cache = open_positions
                    self.last_open_positions_time_shared = now

                    return open_positions
                except Exception as e:
                    is_rate_limit_error = "Too many visits" in str(e) or (hasattr(e, 'response') and e.response.status_code == 403)
                    
                    if is_rate_limit_error and attempt < retries - 1:
                        delay = min(delay_factor * (attempt + 1), max_delay)  # Exponential delay with a cap
                        logging.info(f"Rate limit on get_all_open_positions_bybit hit, waiting for {delay} seconds before retrying...")
                        time.sleep(delay)
                        continue
                    else:
                        logging.error(f"Error fetching open positions: {e}")
                        return []

 
    # Universal
    def get_current_price(self, symbol: str) -> float:
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            if "bid" in ticker and "ask" in ticker:
                return (ticker["bid"] + ticker["ask"]) / 2
        except Exception as e:
            logging.error(f"An error occurred in get_current_price() for {symbol}: {e}")
            return None
        
    def get_moving_averages(self, symbol: str, timeframe: str = "1m", num_bars: int = 20, max_retries=3, retry_delay=5) -> dict:
        values = {"MA_3_H": 0.0, "MA_3_L": 0.0, "MA_6_H": 0.0, "MA_6_L": 0.0}

        for i in range(max_retries):
            try:
                bars = self.exchange.fetch_ohlcv(
                    symbol=symbol, timeframe=timeframe, limit=num_bars
                )
                df = pd.DataFrame(
                    bars, columns=["Time", "Open", "High", "Low", "Close", "Volume"]
                )
                df["Time"] = pd.to_datetime(df["Time"], unit="ms")
                df["MA_3_High"] = df.High.rolling(3).mean()
                df["MA_3_Low"] = df.Low.rolling(3).mean()
                df["MA_6_High"] = df.High.rolling(6).mean()
                df["MA_6_Low"] = df.Low.rolling(6).mean()
                values["MA_3_H"] = df["MA_3_High"].iat[-1]
                values["MA_3_L"] = df["MA_3_Low"].iat[-1]
                values["MA_6_H"] = df["MA_6_High"].iat[-1]
                values["MA_6_L"] = df["MA_6_Low"].iat[-1]
                break  # If the fetch was successful, break out of the loop
            except Exception as e:
                if i < max_retries - 1:  # If not the last attempt
                    logging.info(f"An unknown error occurred in get_moving_averages(): {e}. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    logging.info(f"Failed to fetch moving averages after {max_retries} attempts: {e}")
                    raise e  # If it's still failing after max_retries, re-raise the exception.
        
        return values

    def get_order_status(self, order_id, symbol):
        try:
            # Fetch the order details from the exchange using the order ID
            order_details = self.fetch_order(order_id, symbol)

            logging.info(f"Order details for {symbol}: {order_details}")

            # Extract and return the order status
            return order_details['status']
        except Exception as e:
            logging.error(f"An error occurred fetching order status for {order_id} on {symbol}: {e}")
            return None


    def get_open_orders(self, symbol: str) -> list:
        open_orders_list = []
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            #print(orders)
            if len(orders) > 0:
                for order in orders:
                    if "info" in order:
                        #print(f"Order info: {order['info']}")  # Debug print
                        order_info = {
                            "id": order["info"]["orderId"],
                            "price": float(order["info"]["price"]),
                            "qty": float(order["info"]["qty"]),
                            "order_status": order["info"]["orderStatus"],
                            "side": order["info"]["side"],
                            "reduce_only": order["info"]["reduceOnly"],  # Update this line
                            "position_idx": int(order["info"]["positionIdx"])  # Add this line
                        }
                        open_orders_list.append(order_info)
        except Exception as e:
            logging.info(f"Bybit An unknown error occurred in get_open_orders(): {e}")
        return open_orders_list

    def get_open_orders_bybit_unified(self, symbol: str) -> list:
        open_orders_list = []
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            #print(orders)
            if len(orders) > 0:
                for order in orders:
                    if "info" in order:
                        order_info = {
                            "id": order["id"],
                            "price": float(order["price"]),
                            "qty": float(order["amount"]),
                            "order_status": order["status"],
                            "side": order["side"],
                            "reduce_only": order["reduceOnly"],
                            "position_idx": int(order["info"]["positionIdx"])
                        }
                        open_orders_list.append(order_info)
        except Exception as e:
            logging.info(f"An unknown error occurred in get_open_orders(): {e}")
        return open_orders_list


    def debug_open_orders(self, symbol: str) -> None:
        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
            logging.info(open_orders)
        except:
            logging.info(f"Fuck")

    def cancel_long_entry(self, symbol: str) -> None:
        self._cancel_entry(symbol, order_side="Buy")

    def cancel_short_entry(self, symbol: str) -> None:
        self._cancel_entry(symbol, order_side="Sell")

    def _cancel_entry(self, symbol: str, order_side: str) -> None:
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            for order in orders:
                if "info" in order:
                    order_id = order["info"]["order_id"]
                    order_status = order["info"]["order_status"]
                    side = order["info"]["side"]
                    reduce_only = order["info"]["reduce_only"]
                    if (
                        order_status != "Filled"
                        and side == order_side
                        and order_status != "Cancelled"
                        and not reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling {order_side} order: {order_id}")
        except Exception as e:
            logging.info(f"An unknown error occurred in _cancel_entry(): {e}")

    def cancel_all_orders_for_symbol_bybit(self, symbol):
        try:
            # Assuming 'self.exchange' is your initialized CCXT exchange instance
            cancel_result = self.exchange.cancel_all_orders(symbol)
            logging.info(f"All open orders for {symbol} have been cancelled. Result: {cancel_result}")
            return cancel_result
        except Exception as e:
            logging.error(f"Error cancelling open orders for {symbol}: {e}")
            return None
        
    def cancel_all_open_orders_bybit(self, derivatives: bool = False, params={}):
        """
        Cancel all open orders for all symbols.

        :param bool derivatives: Whether to cancel derivative orders.
        :param dict params: Additional parameters for the API call.
        :return: A list of canceled orders.
        """
        max_retries = 10  # Maximum number of retries
        retry_delay = 5  # Delay (in seconds) between retries

        for retry in range(max_retries):
            try:
                if derivatives:
                    return self.exchange.cancel_all_derivatives_orders(None, params)
                else:
                    return self.exchange.cancel_all_orders(None, params)
            except ccxt.RateLimitExceeded as e:
                # If rate limit error and not the last retry, then wait and try again
                if retry < max_retries - 1:
                    logging.warning(f"Rate limit exceeded. Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:  # If it's the last retry, raise the error
                    logging.error(f"Rate limit exceeded after {max_retries} retries.")
                    raise e
            except Exception as ex:
                # If any other exception, log it and re-raise
                logging.error(f"Error occurred while canceling orders: {ex}")
                raise ex

    def health_check(self, interval_seconds=300):
        """
        Periodically checks the health of the exchange and cancels all open orders.

        :param interval_seconds: The time interval in seconds between each health check.
        """
        while True:
            try:
                logging.info("Performing health check...")  # Log start of health check
                # You can add more health check logic here
                
                # Cancel all open orders
                self.cancel_all_open_orders_bybit()
                
                logging.info("Health check complete.")  # Log end of health check
            except Exception as e:
                logging.error(f"An error occurred during the health check: {e}")  # Log any errors
                
            time.sleep(interval_seconds)


    #v5 
    def cancel_all_reduce_only_orders_bybit(self, symbol: str) -> None:
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            logging.info(f"[Thread ID: {threading.get_ident()}] cancel_all_reduce_only_orders_bybit function accessed")
            logging.info(f"Fetched orders: {orders}")

            for order in orders:
                if order['status'] in ['open', 'partially_filled']:
                    # Check if the order is a reduce-only order
                    if order['reduceOnly']:
                        order_id = order['id']
                        self.exchange.cancel_order(order_id, symbol)
                        logging.info(f"Cancelling reduce-only order: {order_id}")

        except Exception as e:
            logging.warning(f"An error occurred in cancel_all_reduce_only_orders_bybit(): {e}")

    # v5
    def cancel_all_entries_bybit(self, symbol: str) -> None:
        try:
            orders = self.exchange.fetch_open_orders(symbol)
            logging.info(f"[Thread ID: {threading.get_ident()}] cancel_all_entries function in exchange class accessed")
            logging.info(f"Fetched orders: {orders}")

            for order in orders:
                if order['status'] in ['open', 'partially_filled']:
                    # Check if the order is not a reduce-only order
                    if not order['reduceOnly']:
                        order_id = order['id']
                        self.exchange.cancel_order(order_id, symbol)
                        logging.info(f"Cancelling order: {order_id}")

        except Exception as e:
            logging.warning(f"An unknown error occurred in cancel_all_entries_bybit(): {e}")

            
    # Bybit
    def get_contract_size_bybit(self, symbol):
        positions = self.exchange.fetch_derivatives_positions([symbol])
        return positions[0]['contractSize']

    def get_max_leverage_bybit(self, symbol, max_retries=10, backoff_factor=0.5):
        #logging.info(f"Called get_max_leverage_bybit with symbol: {symbol}")
        for retry in range(max_retries):
            try:
                tiers = self.exchange.fetch_derivatives_market_leverage_tiers(symbol)
                for tier in tiers:
                    info = tier.get('info', {})
                    if info.get('symbol') == symbol:
                        return float(info.get('maxLeverage'))
                return None  # If symbol not found in tiers

            except (RateLimitExceeded, NetworkError) as e:  # Include NetworkError
                # Log the exception
                logging.error(f"An error occurred while fetching max leverage: {str(e)}")

                # Wait and retry if not the last attempt
                if retry < max_retries - 1:  
                    sleep_time = backoff_factor * (2 ** retry)  # Exponential backoff
                    time.sleep(sleep_time)

            except Exception as e:
                # For any other types of exceptions, log and re-raise.
                logging.error(f"An unknown error occurred: {str(e)}")
                raise e

        raise Exception(f"Failed to get max leverage for {symbol} after {max_retries} retries.")

    # Bybit
    def get_take_profit_order_quantity_bybit(self, symbol, side):
        side = side.lower()
        side_map = {"long": "buy", "short": "sell"}
        side = side_map.get(side, side)
        total_qty = 0
        
        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
            position_idx_map = {"buy": 1, "sell": 2}

            for order in open_orders:
                if (
                    order['side'].lower() == side
                    and order['info'].get('reduceOnly')
                    and order['info'].get('positionIdx') == position_idx_map[side]
                ):
                    total_qty += order.get('amount', 0)  # Assuming 'amount' contains the order quantity
        except Exception as e:
            logging.error(f"An unknown error occurred in get_take_profit_order_quantity_bybit: {e}")

        return total_qty

    # v5
    #Bybit
    def cancel_take_profit_orders_bybit(self, symbol, side):
        side = side.lower()
        side_map = {"long": "buy", "short": "sell"}
        side = side_map.get(side, side)
        
        try:
            open_orders = self.exchange.fetch_open_orders(symbol)
            position_idx_map = {"buy": 1, "sell": 2}

            for order in open_orders:
                if (
                    order['side'].lower() == side
                    and order['info'].get('reduceOnly')
                    and order['info'].get('positionIdx') == position_idx_map[side]
                ):
                    order_id = order['id']  # Assuming 'id' is the standard format expected by cancel_order
                    self.exchange.cancel_order(order_id, symbol)
                    logging.info(f"Canceled take profit order - ID: {order_id}")

        except Exception as e:
            logging.error(f"An unknown error occurred in cancel_take_profit_orders: {e}")

    # v5
    def cancel_order_by_id(self, order_id, symbol):
        try:
            # Call the updated cancel_order method
            result = self.exchange.cancel_order(id=order_id, symbol=symbol)
            logging.info(f"Canceled order - ID: {order_id}, Response: {result}")
        except Exception as e:
            logging.error(f"Error occurred in cancel_order_by_id: {e}")

    def cancel_close_bybit(self, symbol: str, side: str, max_retries: int = 3) -> None:
        side = side.lower()
        side_map = {"long": "buy", "short": "sell"}
        side = side_map.get(side, side)
        
        position_idx_map = {"buy": 1, "sell": 2}
        
        retries = 0
        while retries < max_retries:
            try:
                orders = self.exchange.fetch_open_orders(symbol)
                if len(orders) > 0:
                    for order in orders:
                        if "info" in order:
                            order_id = order["info"]["orderId"]
                            order_status = order["info"]["orderStatus"]
                            order_side = order["info"]["side"]
                            reduce_only = order["info"]["reduceOnly"]
                            position_idx = order["info"]["positionIdx"]

                            if (
                                order_status != "Filled"
                                and order_side.lower() == side
                                and order_status != "Cancelled"
                                and reduce_only
                                and position_idx == position_idx_map[side]
                            ):
                                # use the new cancel_derivatives_order function
                                self.exchange.cancel_derivatives_order(order_id, symbol)
                                logging.info(f"Cancelling order: {order_id}")
                # If the code reaches this point without an exception, break out of the loop
                break

            except (RateLimitExceeded, NetworkError) as e:
                retries += 1
                logging.warning(f"Encountered an error in cancel_close_bybit(). Retrying... {retries}/{max_retries}")
                time.sleep(5)  # Pause before retrying, this can be adjusted

            except Exception as e:
                # For other exceptions, log and break out of the loop
                logging.warning(f"An unknown error occurred in cancel_close_bybit(): {e}")
                break


    def cancel_close(self, symbol: str, side: str) -> None:
        try:
            order = self.exchange.fetch_open_orders(symbol)
            if len(order) > 0:
                if "info" in order[0]:
                    order_id = order[0]["info"]["order_id"]
                    order_status = order[0]["info"]["order_status"]
                    order_side = order[0]["info"]["side"]
                    reduce_only = order[0]["info"]["reduce_only"]
                    if (
                        order_status != "Filled"
                        and order_side == "Buy"
                        and side == "long"
                        and order_status != "Cancelled"
                        and reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling order: {order_id}")
                    elif (
                        order_status != "Filled"
                        and order_side == "Sell"
                        and side == "short"
                        and order_status != "Cancelled"
                        and reduce_only
                    ):
                        self.exchange.cancel_order(symbol=symbol, id=order_id)
                        logging.info(f"Cancelling order: {order_id}")
        except Exception as e:
            logging.warning(f"{e}")

    # Bybit
    def create_take_profit_order_bybit(self, symbol, order_type, side, amount, price=None, positionIdx=1, reduce_only=True):
        logging.info(f"Calling create_take_profit_order_bybit with symbol={symbol}, order_type={order_type}, side={side}, amount={amount}, price={price}")
        if order_type == 'limit':
            if price is None:
                raise ValueError("A price must be specified for a limit order")

            if side not in ["buy", "sell"]:
                raise ValueError(f"Invalid side: {side}")

            params = {"reduceOnly": reduce_only}
            return self.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

    # Bybit
    def postonly_create_take_profit_order_bybit(self, symbol, order_type, side, amount, price=None, positionIdx=1, reduce_only=True, post_only=True):
        if order_type == 'limit':
            if price is None:
                raise ValueError("A price must be specified for a limit order")

            if side not in ["buy", "sell"]:
                raise ValueError(f"Invalid side: {side}")

            params = {"reduceOnly": reduce_only, "postOnly": post_only}
            return self.create_limit_order_bybit(symbol, side, amount, price, positionIdx=positionIdx, params=params)
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

    def create_take_profit_order(self, symbol, order_type, side, amount, price=None, reduce_only=False):
        if order_type == 'limit':
            if price is None:
                raise ValueError("A price must be specified for a limit order")

            if side not in ["buy", "sell"]:
                raise ValueError(f"Invalid side: {side}")

            params = {"reduceOnly": reduce_only}
            return self.exchange.create_order(symbol, order_type, side, amount, price, params)
        else:
            raise ValueError(f"Unsupported order type: {order_type}")

    def create_market_order(self, symbol: str, side: str, amount: float, params={}, close_position: bool = False) -> None:
        try:
            if side not in ["buy", "sell"]:
                logging.warning(f"side {side} does not exist")
                return

            order_type = "market"

            # Determine the correct order side for closing positions
            if close_position:
                market = self.exchange.market(symbol)
                if market['type'] in ['swap', 'future']:
                    if side == "buy":
                        side = "close_short"
                    elif side == "sell":
                        side = "close_long"

            response = self.exchange.create_order(symbol, order_type, side, amount, params=params)
            return response
        except Exception as e:
            logging.warning(f"An unknown error occurred in create_market_order(): {e}")

    def create_limit_order_bybit_unified(self, symbol: str, side: str, qty: float, price: float, positionIdx=0, params={}):
        try:
            if side == "buy" or side == "sell":
                order = self.exchange.create_unified_account_order(
                    symbol=symbol,
                    type='limit',
                    side=side,
                    amount=qty,
                    price=price,
                    params={**params, 'positionIdx': positionIdx}
                )
                return order
            else:
                logging.warning(f"side {side} does not exist")
        except Exception as e:
            logging.warning(f"An unknown error occurred in create_limit_order(): {e}")

    def create_tagged_limit_order_bybit(self, symbol: str, side: str, qty: float, price: float, positionIdx=0, isLeverage=False, orderLinkId=None, postOnly=True, params={}):
        try:
            # Directly prepare the parameters required by the `create_order` method
            order_type = "limit"  # For limit orders
            time_in_force = "PostOnly" if postOnly else "GTC"
            
            # Include additional parameters
            extra_params = {
                "positionIdx": positionIdx,
                "timeInForce": time_in_force
            }
            if isLeverage:
                extra_params["isLeverage"] = 1
            if orderLinkId:
                extra_params["orderLinkId"] = orderLinkId
            
            # Merge any additional user-provided parameters
            extra_params.update(params)

            # Create the order
            order = self.exchange.create_order(
                symbol=symbol,
                type=order_type,
                side=side,
                amount=qty,
                price=price,
                params=extra_params  # Pass extra params here
            )
            return order
        except Exception as e:
            logging.warning(f"An error occurred in create_tagged_limit_order_bybit() for {symbol}: {e}")
            return {"error": str(e)}


    def create_limit_order_bybit(self, symbol: str, side: str, qty: float, price: float, positionIdx=0, params={}):
        try:
            if side == "buy" or side == "sell":
                order = self.exchange.create_order(
                    symbol=symbol,
                    type='limit',
                    side=side,
                    amount=qty,
                    price=price,
                    params={**params, 'positionIdx': positionIdx}  # Pass the 'positionIdx' parameter here
                )
                return order
            else:
                logging.warning(f"side {side} does not exist")
                return {"error": f"side {side} does not exist"}
        except Exception as e:
            logging.warning(f"An unknown error occurred in create_limit_order() for {symbol}: {e}")
            return {"error": str(e)}

    def create_limit_order_bybit_spot(self, symbol: str, side: str, qty: float, price: float, isLeverage=0, orderLinkId=None):
        try:
            # Define the 'params' dictionary to include any additional parameters required by Bybit's v5 API
            params = {
                'timeInForce': 'PostOnly',  # Set the order as a PostOnly order
                'isLeverage': isLeverage,   # Specify whether to borrow for margin trading
            }
            
            # If 'orderLinkId' is provided, add it to the 'params' dictionary
            if orderLinkId:
                params['orderLinkId'] = orderLinkId

            # Create the limit order using CCXT's 'create_order' function
            order = self.exchange.create_order(
                symbol=symbol,
                type='limit',
                side=side,
                amount=qty,
                price=price,
                params=params
            )
            
            return order
        except Exception as e:
            logging.error(f"An error occurred while creating limit order on Bybit: {e}")
            return None

    def test_func(self):
        try:
            market = self.exchange.market('DOGEUSDT')
            print(market['info'])
        except Exception as e:
            print(f"Exception caught in test func {e}")

    def create_limit_order(self, symbol, side, amount, price, reduce_only=False, **params):
        if side == "buy":
            return self.create_limit_buy_order(symbol, amount, price, reduce_only=reduce_only, **params)
        elif side == "sell":
            return self.create_limit_sell_order(symbol, amount, price, reduce_only=reduce_only, **params)
        else:
            raise ValueError(f"Invalid side: {side}")

    def create_limit_buy_order(self, symbol: str, qty: float, price: float, **params) -> None:
        self.exchange.create_order(
            symbol=symbol,
            type='limit',
            side='buy',
            amount=qty,
            price=price,
            **params
        )

    def create_limit_sell_order(self, symbol: str, qty: float, price: float, **params) -> None:
        self.exchange.create_order(
            symbol=symbol,
            type='limit',
            side='sell',
            amount=qty,
            price=price,
            **params
        )

    def create_order(self, symbol, order_type, side, amount, price=None, reduce_only=False, **params):
        if reduce_only:
            params.update({'reduceOnly': 'true'})

        if self.exchange_id == 'bybit':
            order = self.exchange.create_order(symbol, order_type, side, amount, price, params=params)
        else:
            if order_type == 'limit':
                if side == "buy":
                    order = self.create_limit_buy_order(symbol, amount, price, **params)
                elif side == "sell":
                    order = self.create_limit_sell_order(symbol, amount, price, **params)
                else:
                    raise ValueError(f"Invalid side: {side}")
            elif order_type == 'market':
                # Special handling for market orders
                order = self.exchange.create_order(symbol, 'market', side, amount, None, params=params)
            else:
                raise ValueError("Invalid order type. Use 'limit' or 'market'.")

        return order

    def create_market_order_bybit(self, symbol: str, side: str, qty: float, positionIdx=0, params={}):
        try:
            if side == "buy" or side == "sell":
                request = {
                    'symbol': symbol,
                    'type': 'market',
                    'side': side,
                    'qty': qty,
                    'positionIdx': positionIdx,
                    'closeOnTrigger': True  # Set closeOnTrigger to True for market close order
                }
                order = self.exchange.create_contract_v3_order(symbol, 'market', side, qty, params=request)
                return order
            else:
                logging.warning(f"Side {side} does not exist")
        except Exception as e:
            logging.warning(f"An unknown error occurred in create_market_order(): {e}")
#import os
#import logging
#import time
import ta as ta
#import uuid
#import ccxt
#import pandas as pd
#import hjson
#import requests, hmac, hashlib
#import urllib.parse
#import threading
#import traceback
#from typing import Optional, Tuple, List
#from ccxt.base.errors import RateLimitExceeded
from ..strategies.logger import Logger
#from requests.exceptions import HTTPError
#from datetime import datetime, timedelta
#from ccxt.base.errors import NetworkError
import importlib

"""
this file is used a intermediary to dynamiccally load the exchange file.

todo: move shared stuff in here(?)
"""

logging = Logger(logger_name="Exchange", filename="Exchange.log", stream=True)

def Exchange(self, api_key, secret_key, passphrase):
    ActiveExchange = importlib.import_module(f"{self.exchange_id}")
    self.active_exchange = ActiveExchange
    self.exchange = self.active_exchange.Exchange(self.exchange_name, api_key, secret_key, passphrase)
    return self.exchange

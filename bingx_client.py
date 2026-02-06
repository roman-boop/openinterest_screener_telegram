# bingx_client.py — финальная версия SDK для BingX Swap V2

import time
import hmac
import hashlib
import requests
import json
from typing import List, Dict, Optional
from decimal import Decimal

class BingxClient:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.testnet = testnet
        self.BASE_URL = "https://open-api-vst.bingx.com" if testnet else "https://open-api.bingx.com"
        self.time_offset = self.get_server_time_offset()

    def _to_bingx_symbol(self, symbol: str) -> str:
        s = symbol.replace("-", "")
        s = s.replace("/", "")
        s = s.replace("USDT", "-USDT")
        return s

    def _sign(self, query: str) -> str:
        return hmac.new(self.api_secret.encode("utf-8"),
                        query.encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def parseParam(self, paramsMap: dict) -> str:
        sortedKeys = sorted(paramsMap)
        paramsStr = "&".join(f"{k}={paramsMap[k]}" for k in sortedKeys)
        timestamp = str(int(time.time() * 1000))
        return f"{paramsStr}&timestamp={timestamp}" if paramsStr else f"timestamp={timestamp}"

    def _request(self, method: str, path: str, params=None, data=None):
        if params is None:
            params = {}
        query_string = self.parseParam(params)
        signature = self._sign(query_string)
        url = f"{self.BASE_URL}{path}?{query_string}&signature={signature}"
        headers = {'X-BX-APIKEY': self.api_key}
        response = requests.request(method, url, headers=headers, data=data or {})
        response.raise_for_status()
        return response.json()

    def _public_request(self, path: str, params=None, timeout: int = 10):
        url = f"{self.BASE_URL}{path}"
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def get_server_time_offset(self):
        path = "/openApi/swap/v2/server/time"
        try:
            data = self._public_request(path)
            if data.get("code") == 0:
                server_time = int(data["data"]["serverTime"])
                local_time = int(time.time() * 1000)
                return server_time - local_time
        except:
            pass
        return 0

    def get_mark_price(self, symbol=None):
        path = "/openApi/swap/v2/quote/premiumIndex"
        s = self._to_bingx_symbol(symbol) if symbol else None
        params = {'symbol': s} if s else {}
        try:
            data = self._public_request(path, params)
            if data.get('code') == 0 and data.get('data'):
                item = data['data'][0] if isinstance(data['data'], list) else data['data']
                return float(item.get('markPrice'))
        except:
            pass
        return None

    # === НОВЫЕ МЕТОДЫ ИЗ auto_sltp_manager ===

    def get_positions(self):
        """Получить все открытые позиции"""
        path = "/openApi/swap/v2/user/positions"
        data = self._request("GET", path, {})
        return data.get('data', []) if data.get('code') == 0 else []

    def get_open_orders(self, symbol: str):
        """Получить открытые ордера по символу"""
        path = "/openApi/swap/v2/trade/openOrders"
        params = {'symbol': self._to_bingx_symbol(symbol)}
        data = self._request("GET", path, params)
        if data and data.get('code') == 0 and 'data' in data and 'orders' in data['data']:
            return data['data']['orders']
        return []

    def cancel_order(self, symbol: str, order_id: str):
        """Отменить ордер по orderId"""
        path = "/openApi/swap/v2/trade/order"
        params = {
            'symbol': self._to_bingx_symbol(symbol),
            "timestamp": int(time.time() * 1000) + self.time_offset,
            'orderId': order_id
        }
        return self._request("DELETE", path, params)

    def cancel_existing_orders(self, symbol: str):
        """Отменить все открытые ордера по символу"""
        orders = self.get_open_orders(symbol)
        success = 0
        for order in orders:
            order_id = order.get('orderId')
            if order_id:
                resp = self.cancel_order(symbol, order_id)
                if resp.get('code') == 0:
                    success += 1
                time.sleep(0.3)
        return success

    def get_trades_history(self, days=180):
        """Получить историю сделок за последние days дней"""
        path = "/openApi/swap/v2/user/income"
        now = int(time.time() * 1000)
        from_ts = int((time.time() - days * 24 * 3600) * 1000)
        params = {
            "timestamp": int(time.time() * 1000) + self.time_offset,
            "startTime": from_ts,
            "endTime": now,
            "limit": 100
        }
        all_trades = []
        while True:
            data = self._request("GET", path, params)
            if not data or data.get("code") != 0:
                break
            trades = data.get("data", [])
            if not trades:
                break
            all_trades.extend(trades)
            if len(trades) < params["limit"]:
                break
            params["startTime"] = int(trades[-1]["time"]) + 1
        return all_trades

    # === ОБНОВЛЁННЫЙ place_market_order с reduceOnly ===
    def place_market_order(self, side: str, qty: float, symbol: str = None, stop: float = None,
                           tp: float = None, pos_side_BOTH: bool = False, reduceOnly: bool = False):
        side_param = "BUY" if side == "long" else "SELL"
        s = self._to_bingx_symbol(symbol)
        pos_side = "LONG" if side == "long" else "SHORT"
        if pos_side_BOTH:
            pos_side = 'BOTH'

        params = {
            "symbol": s,
            "side": side_param,
            "positionSide": pos_side,
            "type": "MARKET",
            "quantity": qty,
            "recvWindow": 5000,
            "timeInForce": "GTC",
        }

        if reduceOnly:
            params["reduceOnly"] = "true"

        if stop is not None:
            stopLoss_param = {
                "type": "STOP_MARKET",
                "stopPrice": stop,
                "price": stop,
                "workingType": "MARK_PRICE"
            }
            params["stopLoss"] = json.dumps(stopLoss_param)

        if tp is not None:
            takeProfit_param = {
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp,
                "price": tp,
                "workingType": "MARK_PRICE"
            }
            params["takeProfit"] = json.dumps(takeProfit_param)

        timestamp = int(time.time() * 1000) + self.time_offset
        params["timestamp"] = timestamp

        return self._request("POST", "/openApi/swap/v2/trade/order", params)

    def count_decimal_places(self, number: float) -> int:
        s = str(number).rstrip('0')
        if '.' in s:
            return len(s.split('.')[1])
        return 0

    def set_leverage(self, symbol: str, side: str, leverage: int, one_way_mode = False):
        if one_way_mode == False:
            params = {
                "symbol": self._to_bingx_symbol(symbol),
                "side": 'LONG',
                "leverage": leverage,
                "timestamp": int(time.time() * 1000) + self.time_offset
            }
        else:
            params = {
                "symbol": self._to_bingx_symbol(symbol),
                "side": 'BOTH',
                "leverage": leverage,
                "timestamp": int(time.time() * 1000) + self.time_offset
            }
        return self._request("POST", "/openApi/swap/v2/trade/leverage", params)

    def set_multiple_tp(self, symbol: str, qty: float, mark_price: float, side: str, tp_levels, both=False):
        # Существующая реализация осталась без изменений
        # (твой оригинальный код)
        print(mark_price)
        precision = self.count_decimal_places(mark_price)

        if side == "short":
            tp_side = "BUY"
            pos_side = "SHORT"
        else:
            tp_side = "SELL"
            pos_side = "LONG"
        
        if both == True:
            pos_side = 'BOTH'
        answer = []
        qty_round = 0 if precision >= 3 else 2 if precision == 2 else 3 if precision == 1 else 4
        qty_tp = round(qty / len(tp_levels), qty_round)

        for tp in tp_levels:
            params = {
                "symbol": self._to_bingx_symbol(symbol),
                "side": tp_side,
                "positionSide": pos_side,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": tp,
                "quantity": qty_tp,
                "timestamp": int(time.time()*1000) + self.time_offset,
                "workingType": "MARK_PRICE"
            }
            try:
                resp = self._request("POST", "/openApi/swap/v2/trade/order", params)
                answer.append(resp)
                print(f"[TP] Установлен тейк-профит {tp}")
            except Exception as e:
                print("[TP ERROR]", e)
                answer.append({"code": 1, "msg": str(e)})

        return answer


    def set_trailing(self, symbol, side: str, qty: float, activation_price: float, priceRate: float):
        params = {
            "symbol": symbol,
            "side": 'SELL' if side == 'long' else 'BUY',
            "positionSide": "LONG" if side =='long' else 'SHORT',
            "type": "TRAILING_TP_SL",
            "timestamp": int(time.time() * 1000) + self.time_offset,
            "quantity": qty,
            "recvWindow": 5000,
            'workingType': 'CONTRACT_PRICE',
            'activationPrice': activation_price,
            "newClientOrderId": "",
            'priceRate': priceRate,
        }
        return self._request("POST", "/openApi/swap/v2/trade/order", params)

    def get_account_balance(self) -> Dict:
        data = self._request("GET", "/openApi/swap/v2/user/balance")
        if data.get("code") == 0 and "balance" in data.get("data", {}):
            return data["data"]["balance"]
        return {}

    def place_conditional_order(self, symbol: str, side: str, quantity: float, stop_price: float, order_type: str, position_side: str) -> Optional[str]:
        """Размещение условного ордера (SL/TP)"""
        s = self._to_bingx_symbol(symbol)
        params = {
            "symbol": s,
            "side": side,
            "positionSide": position_side,
            "type": order_type,
            "quantity": quantity,
            "timestamp": int(time.time() * 1000) + self.time_offset,
            "stopPrice": stop_price,
            "workingType": "MARK_PRICE"
        }
        resp = self._request("POST", "/openApi/swap/v2/trade/order", params)
        if resp.get("code") == 0 and "orderId" in resp.get("data", {}):
            return resp["data"]["orderId"]
        return None

    def get_price(self, symbol: str) -> Optional[float]:
        """Получить текущую цену (lastPrice или markPrice)"""
        s = self._to_bingx_symbol(symbol)
        try:
            data = self._public_request("/openApi/swap/v2/quote/ticker", {"symbol": s})
            if data.get("code") == 0 and data.get("data"):
                return float(data["data"][0]["lastPrice"])
        except Exception:
            pass
        return self.get_mark_price(symbol)
#!/usr/bin/env python3
"""
3ook.com NFT Book Downloader
給定任一 OpenSea NFT URL，用錢包驗證持有並下載 epub

用法:
  PRIVATE_KEY=0x... python3 read_nft_book.py <opensea_url>
  PRIVATE_KEY=0x... python3 read_nft_book.py <opensea_url> --token-id 5
  PRIVATE_KEY=0x... python3 read_nft_book.py <opensea_url> --output book.epub

OpenSea URL 範例:
  https://opensea.io/assets/base/0x67018c5ff51e2c84badb13e15e4dad6d32d3498e/0

安全提醒: 使用環境變數傳入私鑰，避免出現在 shell 歷史記錄中
"""

import base64
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
import argparse

from eth_account import Account
from eth_account.messages import encode_defunct

API_BASE = "https://api.like.co"

# OpenSea chain name → 公開 RPC
CHAIN_RPC = {
    "base":      "https://base-rpc.publicnode.com",
    "ethereum":  "https://ethereum-rpc.publicnode.com",
    "matic":     "https://polygon-bor-rpc.publicnode.com",
    "polygon":   "https://polygon-bor-rpc.publicnode.com",
    "optimism":  "https://optimism-rpc.publicnode.com",
    "arbitrum":  "https://arbitrum-one-rpc.publicnode.com",
}


# ── URL 解析 ──────────────────────────────────────────────────────────────────

def parse_opensea_url(url):
    """
    解析 OpenSea URL，回傳 (chain, contract_address, token_id)
    支援:
      https://opensea.io/assets/<chain>/<contract>/<token_id>
      https://opensea.io/assets/<contract>/<token_id>   (預設 ethereum)
    """
    m = re.search(
        r"opensea\.io/(?:assets|item)/([^/]+)/0x([0-9a-fA-F]{40})/(\d+)", url
    )
    if m:
        chain = m.group(1).lower()
        contract = "0x" + m.group(2)
        token_id = int(m.group(3))
        return chain, contract, token_id

    m = re.search(r"opensea\.io/(?:assets|item)/0x([0-9a-fA-F]{40})/(\d+)", url)
    if m:
        return "ethereum", "0x" + m.group(1), int(m.group(2))

    raise ValueError(f"無法解析 OpenSea URL: {url}\n"
                     "格式應為: https://opensea.io/assets/<chain>/<contract>/<token_id>")


# ── RPC 工具 ──────────────────────────────────────────────────────────────────

def rpc_call(rpc_url, method, params):
    payload = json.dumps({
        "jsonrpc": "2.0", "method": method, "params": params, "id": 1
    }).encode()
    req = urllib.request.Request(
        rpc_url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    return json.loads(urllib.request.urlopen(req).read())


def get_token_uri(rpc_url, contract, token_id):
    """呼叫 tokenURI(uint256)，回傳 URI 字串"""
    token_hex = hex(token_id)[2:].zfill(64)
    result = rpc_call(rpc_url, "eth_call", [
        {"to": contract,
         "data": f"0xc87b56dd{token_hex}"},
        "latest"
    ])
    raw = result.get("result", "0x")
    if raw in ("0x", "0x" + "0" * 64):
        raise RuntimeError("tokenURI 回傳空值，合約可能不支援 ERC-721 tokenURI")

    hex_str = raw[2:]
    offset = int(hex_str[0:64], 16) * 2
    length = int(hex_str[offset:offset + 64], 16)
    return bytes.fromhex(hex_str[offset + 64:offset + 64 + length * 2]).decode()


def decode_token_metadata(uri):
    """解碼 tokenURI（支援 data:application/json;base64,... 與 https://）"""
    if uri.startswith("data:application/json;base64,"):
        data = uri.split(",", 1)[1]
        return json.loads(base64.b64decode(data + "=="))
    if uri.startswith("data:application/json,"):
        return json.loads(uri.split(",", 1)[1])
    # HTTP URI
    req = urllib.request.Request(uri, headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req).read())


def find_class_id_from_metadata(metadata, contract):
    """從 tokenURI metadata 找出 3ook/liker.land 的 class_id"""
    external_url = metadata.get("external_url", "")

    # 3ook.com/store/<address>/...  or  liker.land/nft/class/<address>/...
    m = re.search(r"0x[0-9a-fA-F]{40}", external_url)
    if m:
        return m.group(0).lower()

    # fallback: 用合約地址本身
    return contract.lower()


def find_owned_token(rpc_url, contract, wallet_address, hint_token_id=None):
    """
    查詢此錢包在合約上持有的 Token ID。
    依序嘗試：
      1. ERC-721 Enumerable: tokenOfOwnerByIndex(wallet, 0)
      2. 直接 ownerOf(hint_token_id) 比對（適用不實作 Enumerable 的合約）
    """
    addr = wallet_address[2:].lower().zfill(64)

    # 方法 1: balanceOf + tokenOfOwnerByIndex
    try:
        result = rpc_call(rpc_url, "eth_call", [
            {"to": contract, "data": f"0x70a08231000000000000000000000000{addr}"},
            "latest"
        ])
        raw = result.get("result", "0x")
        balance = int(raw, 16) if raw and raw != "0x" else 0
        if balance > 0:
            result2 = rpc_call(rpc_url, "eth_call", [
                {"to": contract,
                 "data": f"0x2f745c59000000000000000000000000{addr}"
                         f"0000000000000000000000000000000000000000000000000000000000000000"},
                "latest"
            ])
            raw2 = result2.get("result", "0x")
            if raw2 and raw2 != "0x":
                return int(raw2, 16)
    except Exception:
        pass

    # 方法 2: ownerOf(hint_token_id)
    if hint_token_id is not None:
        try:
            token_hex = hex(hint_token_id)[2:].zfill(64)
            result = rpc_call(rpc_url, "eth_call", [
                {"to": contract, "data": f"0x6352211e{token_hex}"},
                "latest"
            ])
            raw = result.get("result", "0x")
            if raw and raw != "0x" and len(raw) >= 66:
                owner = "0x" + raw[-40:]
                if owner.lower() == wallet_address.lower():
                    return hint_token_id
        except Exception:
            pass

    return None


# ── LikeCoin API ──────────────────────────────────────────────────────────────

def api_post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        API_BASE + path, data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    )
    try:
        return json.loads(urllib.request.urlopen(req).read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        raise RuntimeError(f"API 錯誤 {e.code}: {msg}") from e


def authorize(private_key):
    """用 personal_sign 取得 LikeCoin JWT"""
    account = Account.from_key(private_key)
    wallet = account.address

    ts = int(time.time() * 1000)
    message = json.dumps({
        "action": "authorize",
        "permissions": ["read:nftbook", "write:nftbook", "write:iscn", "read:iscn"],
        "evmWallet": wallet,
        "ts": ts
    }, separators=(',', ':'))

    signed = account.sign_message(encode_defunct(text=message))
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = "0x" + signature

    result = api_post("/wallet/authorize", {
        "wallet": wallet,
        "signature": signature,
        "message": message,
        "signMethod": "personal_sign",
        "expiresIn": "1d"
    })
    return wallet, result["token"]


def download_epub(class_id, nft_id, jwt_token, index=0, output_path=None):
    """透過 LikeCoin ebook-cors 端點下載 epub"""
    params = urllib.parse.urlencode({
        "class_id": class_id,
        "nft_id": str(nft_id),
        "index": str(index),
        "custom_message": "0"
    })
    url = f"{API_BASE}/ebook-cors/?{params}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {jwt_token}",
        "Origin": "https://liker.land",
        "User-Agent": "Mozilla/5.0"
    })

    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError as e:
        msg = e.read().decode()
        raise RuntimeError(f"下載失敗 {e.code}: {msg}") from e

    total = int(response.headers.get("x-original-content-length")
                or response.headers.get("content-length") or 0)

    if output_path is None:
        output_path = f"nft_book_{class_id[-8:]}_{nft_id}.epub"  # fallback，由呼叫方覆蓋

    downloaded = 0
    with open(output_path, "wb") as f:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            f.write(chunk)
            downloaded += len(chunk)
            if total:
                pct = downloaded * 100 // total
                mb = downloaded / 1024 / 1024
                total_mb = total / 1024 / 1024
                print(f"\r  下載中: {pct}%  {mb:.1f} / {total_mb:.1f} MB",
                      end="", flush=True)

    print()
    return output_path


# ── 主程式 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="3ook.com NFT Book Downloader")
    parser.add_argument("url", help="OpenSea NFT URL")
    parser.add_argument("--token-id", type=int, default=None,
                        help="指定你持有的 Token ID（不指定時自動查詢）")
    parser.add_argument("--index", type=int, default=0,
                        help="書本檔案索引，預設 0")
    parser.add_argument("--output", default=None, help="輸出 epub 路徑")
    args = parser.parse_args()

    private_key = os.environ.get("PRIVATE_KEY")
    if not private_key:
        print("錯誤: 請設定 PRIVATE_KEY 環境變數")
        print("範例: PRIVATE_KEY=0x私鑰 python3 read_nft_book.py <url>")
        sys.exit(1)

    # 解析 OpenSea URL
    chain, contract, url_token_id = parse_opensea_url(args.url)
    rpc_url = CHAIN_RPC.get(chain)
    if not rpc_url:
        print(f"錯誤: 不支援的鏈 '{chain}'，支援: {', '.join(CHAIN_RPC)}")
        sys.exit(1)

    print(f"鏈:    {chain}")
    print(f"合約:  {contract}")
    print(f"URL Token ID: {url_token_id}")
    print()

    # Step 1: 從 tokenURI 取得書本 class_id
    print("1. 讀取 tokenURI 中繼資料...")
    try:
        uri = get_token_uri(rpc_url, contract, url_token_id)
        metadata = decode_token_metadata(uri)
        class_id = find_class_id_from_metadata(metadata, contract)
        book_name = metadata.get("name", "")
        print(f"   書名: {book_name}")
        print(f"   Class ID: {class_id}")
    except Exception as e:
        print(f"   警告: 無法讀取 tokenURI ({e})，直接使用合約地址")
        class_id = contract.lower()
        book_name = ""

    # Step 2: 授權
    print("2. 簽署授權訊息...")
    wallet, jwt = authorize(private_key)
    print(f"   錢包: {wallet}")
    print(f"   JWT 取得成功")

    # Step 3: 確定 nft_id
    if args.token_id is not None:
        nft_id = args.token_id
        print(f"3. 使用指定 Token ID: {nft_id}")
    else:
        print("3. 查詢持有的 Token ID...")
        nft_id = find_owned_token(rpc_url, contract, wallet, hint_token_id=url_token_id)
        if nft_id is None:
            print(f"   此錢包未持有此合約的 NFT")
            print(f"   提示：若確定持有，可用 --token-id <token_id> 強制指定")
            sys.exit(1)
        print(f"   找到 Token ID: {nft_id}")

    # Step 4: 下載
    print(f"4. 下載 epub (index={args.index})...")
    if args.output is None and book_name:
        # 用書名當檔名，移除不合法的路徑字元
        safe_name = re.sub(r'[\\/*?:"<>|]', "_", book_name).strip()
        output_path = f"{safe_name}.epub"
    else:
        output_path = args.output
    output = download_epub(class_id, nft_id, jwt,
                           index=args.index, output_path=output_path)
    print(f"\n完成！儲存至: {output}")


if __name__ == "__main__":
    main()
